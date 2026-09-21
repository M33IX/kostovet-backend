from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from kosto_vet.bootstrap.settings import Mode
from kosto_vet.core.errors import DomainError, not_found
from kosto_vet.core.orders import OrderStatus, PaymentStatus, ensure_transition
from kosto_vet.core.time import utc_now
from kosto_vet.models import (
    AuditLog,
    CustomerAccount,
    IntegrationJob,
    Lead,
    Order,
    OrderStatusHistory,
    Product,
    StockReservation,
)
from kosto_vet.services.shared import money


class AdminServiceMixin:
    async def update_admin_order(
        self,
        session: AsyncSession,
        order_id: UUID,
        payload: dict[str, Any],
        actor_id: UUID,
        request_id: str,
    ) -> dict[str, Any]:
        order = await session.get(Order, order_id, with_for_update=True)
        if not order:
            raise not_found()
        if order.version != payload["version"]:
            raise DomainError("VERSION_CONFLICT", "Заказ был изменён.", 409)
        current = OrderStatus(order.status)
        target = OrderStatus(payload["status"])
        if (
            current is OrderStatus.AWAITING_STOCK_CONFIRMATION
            and target is OrderStatus.ASSEMBLING
            and order.payment_status == PaymentStatus.SUCCEEDED.value
        ):
            session.add(
                OrderStatusHistory(
                    order_id=order.id,
                    from_status=current.value,
                    to_status=OrderStatus.PAID.value,
                    actor_type="staff",
                    actor_id=actor_id,
                    reason_code="manual_stock_confirmation",
                    request_id=request_id,
                )
            )
            current = OrderStatus.PAID
        try:
            ensure_transition(current, target)
        except ValueError as exc:
            raise DomainError(
                "INVALID_STATE_TRANSITION", "Недопустимый переход заказа.", 409
            ) from exc
        previous = order.status
        order.status = target.value
        order.version += 1
        if target is OrderStatus.COMPLETED:
            order.completed_at = utc_now()
        if target is OrderStatus.CANCELED:
            order.canceled_at = utc_now()
            await session.execute(
                update(StockReservation)
                .where(StockReservation.order_id == order.id, StockReservation.status == "active")
                .values(status="released", released_at=utc_now())
            )
        session.add(
            OrderStatusHistory(
                order_id=order.id,
                from_status=previous,
                to_status=target.value,
                actor_type="staff",
                actor_id=actor_id,
                reason_code=payload.get("reason") or "manual_admin_transition",
                comment=payload.get("comment"),
                request_id=request_id,
            )
        )
        session.add(
            AuditLog(
                actor_type="staff",
                actor_id=actor_id,
                action="order.status.update",
                target_type="order",
                target_id=order.id,
                before={"status": previous},
                after={"status": target.value},
                request_id=request_id,
            )
        )
        await session.commit()
        return await self.order_payload(session, order, admin=True)

    async def list_admin_customers(
        self, session: AsyncSession, q: str | None, page: int, limit: int
    ) -> dict[str, Any]:
        where = []
        if q:
            where.append(
                or_(CustomerAccount.name.ilike(f"%{q}%"), CustomerAccount.email.ilike(f"%{q}%"))
            )
        total = int(await session.scalar(select(func.count(CustomerAccount.id)).where(*where)) or 0)
        customers = (
            await session.scalars(
                select(CustomerAccount)
                .where(*where)
                .order_by(CustomerAccount.created_at.desc())
                .offset((page - 1) * limit)
                .limit(limit)
            )
        ).all()
        return {
            "items": [await self.admin_customer(session, customer) for customer in customers],
            "page": page,
            "limit": limit,
            "total": total,
        }

    async def admin_customer(
        self, session: AsyncSession, customer: CustomerAccount
    ) -> dict[str, Any]:
        payload = await self._customer_payload(session, customer)
        count, spent = (
            await session.execute(
                select(func.count(Order.id), func.coalesce(func.sum(Order.total_minor), 0)).where(
                    Order.customer_id == customer.id
                )
            )
        ).one()
        return {
            **payload,
            "created_at": customer.created_at.isoformat(),
            "orders_count": int(count),
            "total_spent": money(int(spent)),
        }

    async def list_leads(
        self, session: AsyncSession, status: str | None, source: str | None, page: int, limit: int
    ) -> dict[str, Any]:
        where = []
        if status:
            where.append(Lead.status == status)
        if source:
            where.append(Lead.source == source)
        total = int(await session.scalar(select(func.count(Lead.id)).where(*where)) or 0)
        rows = (
            await session.scalars(
                select(Lead)
                .where(*where)
                .order_by(Lead.created_at.desc())
                .offset((page - 1) * limit)
                .limit(limit)
            )
        ).all()
        return {
            "items": [self.lead_payload(row) for row in rows],
            "page": page,
            "limit": limit,
            "total": total,
        }

    def lead_payload(self, row: Lead) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "name": row.name,
            "phone": row.phone,
            "email": row.email,
            "company": row.company,
            "message": row.message,
            "source": row.source,
            "status": row.status,
            "consent_version": row.consent_version,
            "version": row.version,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }

    async def update_lead(
        self,
        session: AsyncSession,
        lead_id: UUID,
        payload: dict[str, Any],
        actor_id: UUID,
        request_id: str,
    ) -> dict[str, Any]:
        row = await session.get(Lead, lead_id, with_for_update=True)
        if not row:
            raise not_found()
        if row.version != payload["version"]:
            raise DomainError("VERSION_CONFLICT", "Заявка была изменена.", 409)
        before = row.status
        row.status = payload["status"]
        row.version += 1
        session.add(
            AuditLog(
                actor_type="staff",
                actor_id=actor_id,
                action="lead.status.update",
                target_type="lead",
                target_id=row.id,
                before={"status": before},
                after={"status": row.status},
                request_id=request_id,
            )
        )
        await session.commit()
        return self.lead_payload(row)

    async def update_product(
        self,
        session: AsyncSession,
        product_id: UUID,
        payload: dict[str, Any],
        actor_id: UUID,
        request_id: str,
    ) -> dict[str, Any]:
        product = await session.get(Product, product_id, with_for_update=True)
        if not product:
            raise not_found()
        before = {"name": product.name, "is_active": product.is_active}
        seo = payload.pop("seo", None)
        for key, value in payload.items():
            if value is not None:
                setattr(product, key, value)
        if seo:
            if seo.get("title") is not None:
                product.seo_title = seo["title"]
            if seo.get("description") is not None:
                product.seo_description = seo["description"]
        product.version += 1
        session.add(
            AuditLog(
                actor_type="staff",
                actor_id=actor_id,
                action="product.update",
                target_type="product",
                target_id=product.id,
                before=before,
                after={"name": product.name, "is_active": product.is_active},
                request_id=request_id,
            )
        )
        await session.commit()
        return await self.product_detail(session, product.id, include_inactive=True)

    async def list_jobs(self, session: AsyncSession) -> dict[str, Any]:
        rows = (
            await session.scalars(
                select(IntegrationJob).order_by(IntegrationJob.created_at.desc()).limit(100)
            )
        ).all()
        return {
            "items": [
                {
                    "id": str(row.id),
                    "provider": row.provider,
                    "kind": row.kind,
                    "status": row.status,
                    "attempts": row.attempts,
                    "started_at": (row.started_at or row.created_at).isoformat(),
                    "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                    "error_code": row.error_code,
                }
                for row in rows
            ]
        }

    async def trigger_sync(self, session: AsyncSession) -> dict[str, Any]:
        if self.settings.moysklad_mode is Mode.DISABLED:
            raise DomainError("FEATURE_DISABLED", "МойСклад отключён.", 503)
        job = IntegrationJob(
            provider="moysklad", kind="catalog", status="queued", cursor="full", progress={}
        )
        session.add(job)
        await session.commit()
        return {"ok": True, "id": str(job.id)}
