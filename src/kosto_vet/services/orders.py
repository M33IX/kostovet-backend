from __future__ import annotations

import hashlib
import json
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from kosto_vet.bootstrap.settings import Mode
from kosto_vet.core.errors import DomainError, not_found
from kosto_vet.core.identifiers import new_id
from kosto_vet.core.orders import OrderStatus, PaymentStatus
from kosto_vet.core.time import utc_now
from kosto_vet.infrastructure.integrations import (
    safe_payload_hash,
)
from kosto_vet.infrastructure.security import (
    random_token,
    token_hash,
)
from kosto_vet.models import (
    Cart,
    CartItem,
    CustomerConsent,
    Order,
    OrderItem,
    OrderStatusHistory,
    OutboxEvent,
    PaymentAttempt,
    PaymentCallback,
    Product,
    ProductSpec,
    StockItem,
    StockReservation,
)
from kosto_vet.services.shared import manager, money


class OrderServiceMixin:
    async def _order_products(
        self, session: AsyncSession, items: list[dict[str, Any]], *, lock: bool
    ) -> list[tuple[Product, int, StockItem | None]]:
        try:
            normalized_items = [
                {**item, "product_id": UUID(str(item["product_id"]))} for item in items
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainError("INVALID_REQUEST", "Некорректный идентификатор товара.", 400) from exc
        product_ids = sorted((item["product_id"] for item in normalized_items), key=str)
        products = (
            await session.scalars(
                select(Product).where(
                    Product.id.in_(product_ids),
                    Product.is_active.is_(True),
                    Product.is_published.is_(True),
                )
            )
        ).all()
        by_id = {p.id: p for p in products}
        if len(by_id) != len(set(product_ids)):
            raise DomainError("INVALID_REQUEST", "Некоторые товары недоступны.", 400)
        stock_stmt = (
            select(StockItem)
            .where(StockItem.product_id.in_(product_ids))
            .order_by(StockItem.product_id)
        )
        if lock:
            stock_stmt = stock_stmt.with_for_update()
        stocks = {row.product_id: row for row in (await session.scalars(stock_stmt)).all()}
        return [
            (by_id[item["product_id"]], item["quantity"], stocks.get(item["product_id"]))
            for item in normalized_items
        ]

    async def create_order(
        self,
        session: AsyncSession,
        *,
        payload: dict[str, Any],
        key: str,
        customer_id: UUID | None,
        quote: bool,
        request_id: str,
    ) -> dict[str, Any]:
        scope = f"customer:{customer_id}" if customer_id else "guest"
        endpoint = "createQuoteOrder" if quote else "createCheckoutOrder"
        replay = await self._idempotency_replay(
            session, actor_scope=scope, endpoint=endpoint, key=key, payload=payload
        )
        if replay:
            return replay
        manual_confirmation = not quote and self.settings.robokassa_mode is Mode.DISABLED
        payment_checkout = not quote and not manual_confirmation
        if payment_checkout and self.settings.fixed_delivery_price_minor is None:
            raise DomainError(
                "DELIVERY_PRICE_NOT_CONFIGURED", "Стоимость доставки не настроена.", 503
            )
        rows = await self._order_products(session, payload["items"], lock=not quote)
        now = utc_now()
        expires = now + timedelta(seconds=self.settings.stock_reservation_ttl_seconds)
        if not quote:
            for product, quantity, stock in rows:
                if not stock or stock.synced_at is None or stock.is_stale:
                    raise DomainError("STOCK_DATA_STALE", "Остатки требуют обновления.", 503)
                active_reserved = int(
                    await session.scalar(
                        select(func.coalesce(func.sum(StockReservation.quantity), 0)).where(
                            StockReservation.product_id == product.id,
                            StockReservation.warehouse_id == stock.warehouse_id,
                            StockReservation.status == "active",
                            StockReservation.expires_at > now,
                        )
                    )
                    or 0
                )
                if stock.available_quantity - active_reserved < quantity:
                    raise DomainError(
                        "STOCK_CONFLICT", "Количество некоторых товаров изменилось.", 409
                    )
        subtotal = sum(product.final_price_minor * quantity for product, quantity, _ in rows)
        delivery_price_pending = not quote and self.settings.fixed_delivery_price_minor is None
        delivery = (
            0
            if quote or delivery_price_pending
            else int(self.settings.fixed_delivery_price_minor or 0)
        )
        raw_token = random_token()
        order = Order(
            public_id=f"KV-{new_id().hex[:16].upper()}",
            order_access_token_hash=token_hash(raw_token),
            customer_id=customer_id,
            customer_type="legal_entity" if quote else "individual",
            contact_snapshot=payload["customer"],
            legal_snapshot=payload.get("legal_entity"),
            delivery_snapshot=payload.get("delivery"),
            source="web",
            comment=payload.get("comment"),
            subtotal_minor=subtotal,
            delivery_minor=delivery,
            total_minor=subtotal + delivery,
            status=OrderStatus.NEW.value
            if quote or manual_confirmation
            else OrderStatus.AWAITING_PAYMENT.value,
            payment_status=PaymentStatus.NOT_REQUIRED.value
            if quote or manual_confirmation
            else PaymentStatus.CREATED.value,
            manager_snapshot=manager(self.settings),
        )
        session.add(order)
        await session.flush()
        session.add(
            CustomerConsent(
                customer_id=customer_id,
                subject_ref=f"order:{order.id}",
                consent_type="personal_data",
                document_version="demo-v1",
                accepted=True,
                source="web_checkout",
                request_id=request_id,
            )
        )
        for product, quantity, stock in rows:
            product_specs = (
                await session.scalars(
                    select(ProductSpec)
                    .where(ProductSpec.product_id == product.id)
                    .order_by(ProductSpec.sort_order)
                )
            ).all()
            session.add(
                OrderItem(
                    order_id=order.id,
                    product_id=product.id,
                    article=product.article,
                    name=product.name,
                    specs_snapshot={spec.label: spec.value for spec in product_specs},
                    quantity=quantity,
                    unit_price_minor=product.final_price_minor,
                    line_total_minor=product.final_price_minor * quantity,
                )
            )
            if payment_checkout and stock:
                session.add(
                    StockReservation(
                        product_id=product.id,
                        warehouse_id=stock.warehouse_id,
                        order_id=order.id,
                        quantity=quantity,
                        status="active",
                        expires_at=expires,
                    )
                )
        session.add(
            OrderStatusHistory(
                order_id=order.id,
                from_status=None,
                to_status=order.status,
                actor_type="customer" if customer_id else "guest",
                actor_id=customer_id,
                reason_code=(
                    "manager_confirmation"
                    if manual_confirmation
                    else "checkout"
                    if not quote
                    else "quote"
                ),
                request_id=request_id,
            )
        )
        session.add(
            OutboxEvent(
                aggregate_type="order",
                aggregate_id=order.id,
                aggregate_version=0,
                event_type="order.created",
                payload={"order_id": str(order.id)},
                deduplication_key=f"order.created:{order.id}",
                request_id=request_id,
            )
        )
        response: dict[str, Any] = {
            "ok": True,
            "public_id": order.public_id,
            "order_access_token": raw_token,
            "status": order.status,
        }
        if not quote:
            response.update(
                {
                    "pricing": {
                        "subtotal": money(subtotal),
                        "delivery": money(delivery),
                        "total": money(subtotal + delivery),
                    },
                    "delivery_price_pending": delivery_price_pending,
                    "manager_confirmation_required": manual_confirmation,
                }
            )
            if payment_checkout:
                inv_id = secrets.randbelow(9_000_000_000_000_000) + 1
                attempt = PaymentAttempt(
                    order_id=order.id,
                    inv_id=inv_id,
                    requested_method=payload["payment_method"],
                    amount_minor=order.total_minor,
                    status="redirect_ready",
                    idempotency_key=key,
                    expires_at=expires,
                )
                session.add(attempt)
                await session.flush()
                confirmation = self.robokassa.create_confirmation(
                    inv_id=inv_id,
                    amount_minor=order.total_minor,
                    description=f"Оплата заказа {order.public_id}",
                    email=payload["customer"].get("email"),
                    payment_method=payload["payment_method"],
                    order_public_id=order.public_id,
                    expiration=expires.strftime("%Y-%m-%dT%H:%M"),
                )
                attempt.signed_payload_hash = safe_payload_hash(confirmation.fields)
                response.update(
                    {
                        "payment": {
                            "provider": "robokassa",
                            "environment": self.settings.robokassa_mode.value,
                            "status": "redirect_ready",
                            "confirmation_method": "redirect_post",
                            "confirmation_url": confirmation.url,
                            "form_fields": confirmation.fields,
                        },
                        "reservation_expires_at": expires.isoformat(),
                    }
                )
        if customer_id:
            product_ids = [product.id for product, _, _ in rows]
            cart = await session.scalar(
                select(Cart).where(Cart.customer_id == customer_id, Cart.status == "active")
            )
            if cart:
                deleted = await session.execute(
                    delete(CartItem).where(
                        CartItem.cart_id == cart.id,
                        CartItem.product_id.in_(product_ids),
                    )
                )
                if deleted.rowcount:
                    cart.version += 1
        await self._save_idempotency(
            session,
            actor_scope=scope,
            endpoint=endpoint,
            key=key,
            payload=payload,
            response=response,
            resource_id=order.id,
            financial=True,
        )
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise DomainError(
                "IDEMPOTENCY_CONFLICT", "Повторный запрос конфликтует с уже созданным.", 409
            ) from exc
        return response

    async def verify_order_token(
        self, session: AsyncSession, public_id: str, token: str | None
    ) -> Order:
        order = await session.scalar(select(Order).where(Order.public_id == public_id))
        if (
            not order
            or not token
            or not secrets.compare_digest(order.order_access_token_hash, token_hash(token))
        ):
            raise not_found()
        return order

    async def order_payload(
        self, session: AsyncSession, order: Order, *, admin: bool = False
    ) -> dict[str, Any]:
        rows = (
            await session.scalars(
                select(OrderItem)
                .where(OrderItem.order_id == order.id)
                .order_by(OrderItem.created_at)
            )
        ).all()
        if admin:
            return {
                "id": str(order.id),
                "public_id": order.public_id,
                "status": order.status,
                "payment_status": order.payment_status,
                "customer_type": order.customer_type,
                "customer": order.contact_snapshot,
                "legal_entity": order.legal_snapshot,
                "delivery": order.delivery_snapshot,
                "created_at": order.created_at.isoformat(),
                "updated_at": order.updated_at.isoformat(),
                "version": order.version,
                "items": [
                    {
                        "product_name": row.name,
                        "article": row.article,
                        "quantity": row.quantity,
                        "unit_price": money(row.unit_price_minor),
                    }
                    for row in rows
                ],
                "pricing": {
                    "subtotal": money(order.subtotal_minor),
                    "delivery": money(order.delivery_minor),
                    "total": money(order.total_minor),
                },
                "manager": order.manager_snapshot,
            }
        products = []
        for row in rows:
            product = await session.get(Product, row.product_id)
            if product:
                public = (await self.products_to_public(session, [product]))[0]
                public["name"] = row.name
                public["article"] = row.article
                public["price"] = money(row.unit_price_minor)
                products.append(public)
        return {
            "public_id": order.public_id,
            "status": order.status,
            "payment_status": order.payment_status,
            "delivery_tracking_number": order.delivery_tracking_number,
            "items": products,
            "pricing": {
                "subtotal": money(order.subtotal_minor),
                "delivery": money(order.delivery_minor),
                "total": money(order.total_minor),
            },
            "manager": order.manager_snapshot,
        }

    async def retry_payment(
        self,
        session: AsyncSession,
        *,
        order: Order,
        method: str,
        key: str,
        request_id: str,
        order_access_token: str,
    ) -> dict[str, Any]:
        if self.settings.robokassa_mode is Mode.DISABLED:
            raise DomainError("FEATURE_DISABLED", "Оплата отключена.", 503)
        idempotency_payload = {"order_id": str(order.id), "payment_method": method}
        replay = await self._idempotency_replay(
            session,
            actor_scope=f"order:{order.id}",
            endpoint="createOrderPaymentAttempt",
            key=key,
            payload=idempotency_payload,
        )
        if replay:
            replay["order_access_token"] = order_access_token
            return replay
        if (
            order.payment_status == PaymentStatus.SUCCEEDED.value
            or order.status == OrderStatus.CANCELED.value
        ):
            raise DomainError("PAYMENT_CONFLICT", "Заказ уже оплачен или отменён.", 409)
        await session.execute(
            update(PaymentAttempt)
            .where(
                PaymentAttempt.order_id == order.id,
                PaymentAttempt.status.in_(["created", "redirect_ready", "pending"]),
                PaymentAttempt.expires_at <= utc_now(),
            )
            .values(status="expired")
        )
        active = await session.scalar(
            select(PaymentAttempt).where(
                PaymentAttempt.order_id == order.id,
                PaymentAttempt.status.in_(["created", "redirect_ready", "pending"]),
                PaymentAttempt.expires_at > utc_now(),
            )
        )
        if active:
            raise DomainError("PAYMENT_CONFLICT", "У заказа уже есть активная попытка оплаты.", 409)
        items = (
            await session.scalars(select(OrderItem).where(OrderItem.order_id == order.id))
        ).all()
        rows = await self._order_products(
            session,
            [{"product_id": item.product_id, "quantity": item.quantity} for item in items],
            lock=True,
        )
        now = utc_now()
        expires = now + timedelta(seconds=self.settings.stock_reservation_ttl_seconds)
        await session.execute(
            update(StockReservation)
            .where(StockReservation.order_id == order.id, StockReservation.status == "active")
            .values(status="expired")
        )
        for product, quantity, stock in rows:
            if not stock or stock.synced_at is None or stock.is_stale:
                raise DomainError("STOCK_DATA_STALE", "Остатки требуют обновления.", 503)
            active_reserved = int(
                await session.scalar(
                    select(func.coalesce(func.sum(StockReservation.quantity), 0)).where(
                        StockReservation.product_id == product.id,
                        StockReservation.warehouse_id == stock.warehouse_id,
                        StockReservation.status == "active",
                        StockReservation.expires_at > now,
                    )
                )
                or 0
            )
            if stock.available_quantity - active_reserved < quantity:
                raise DomainError("STOCK_CONFLICT", "Товар закончился.", 409)
            session.add(
                StockReservation(
                    product_id=product.id,
                    warehouse_id=stock.warehouse_id,
                    order_id=order.id,
                    quantity=quantity,
                    status="active",
                    expires_at=expires,
                )
            )
        inv_id = secrets.randbelow(9_000_000_000_000_000) + 1
        attempt = PaymentAttempt(
            order_id=order.id,
            inv_id=inv_id,
            requested_method=method,
            amount_minor=order.total_minor,
            status="redirect_ready",
            idempotency_key=key,
            expires_at=expires,
        )
        session.add(attempt)
        await session.flush()
        confirmation = self.robokassa.create_confirmation(
            inv_id=inv_id,
            amount_minor=order.total_minor,
            description=f"Оплата заказа {order.public_id}",
            email=order.contact_snapshot.get("email"),
            payment_method=method,
            order_public_id=order.public_id,
            expiration=expires.strftime("%Y-%m-%dT%H:%M"),
        )
        response = {
            "ok": True,
            "public_id": order.public_id,
            "order_access_token": order_access_token,
            "status": order.status,
            "payment": {
                "provider": "robokassa",
                "environment": self.settings.robokassa_mode.value,
                "status": "redirect_ready",
                "confirmation_method": "redirect_post",
                "confirmation_url": confirmation.url,
                "form_fields": confirmation.fields,
            },
            "pricing": {
                "subtotal": money(order.subtotal_minor),
                "delivery": money(order.delivery_minor),
                "total": money(order.total_minor),
            },
            "reservation_expires_at": expires.isoformat(),
        }
        await self._save_idempotency(
            session,
            actor_scope=f"order:{order.id}",
            endpoint="createOrderPaymentAttempt",
            key=key,
            payload=idempotency_payload,
            response={key: value for key, value in response.items() if key != "order_access_token"},
            resource_id=attempt.id,
            financial=True,
        )
        await session.commit()
        return response

    async def process_robokassa(
        self, session: AsyncSession, values: dict[str, str], request_id: str
    ) -> str:
        if not self.robokassa.verify_result(values):
            raise DomainError("PAYMENT_SIGNATURE_INVALID", "Недействительная подпись.", 400)
        try:
            inv_id = int(values["InvId"])
            amount = Decimal(values["OutSum"])
            if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal("0.01")):
                raise ValueError
            amount_minor = int(amount * 100)
        except KeyError, ValueError, InvalidOperation:
            raise DomainError("INVALID_REQUEST", "Некорректные параметры оплаты.", 400) from None
        payment = await session.scalar(
            select(PaymentAttempt).where(PaymentAttempt.inv_id == inv_id).with_for_update()
        )
        if not payment or payment.amount_minor != amount_minor:
            raise DomainError("PAYMENT_MISMATCH", "Платёж не сопоставлен.", 400)
        fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        existing = await session.scalar(
            select(PaymentCallback).where(PaymentCallback.fingerprint == fingerprint)
        )
        if existing:
            return f"OK{inv_id}"
        order = await session.get(Order, payment.order_id, with_for_update=True)
        if not order:
            raise DomainError("PAYMENT_MISMATCH", "Заказ платежа не найден.", 400)
        payment.status = "succeeded"
        payment.callback_at = utc_now()
        payment.reconciliation_status = "matched"
        order.payment_status = PaymentStatus.SUCCEEDED.value
        reservations = (
            await session.scalars(
                select(StockReservation).where(
                    StockReservation.order_id == order.id, StockReservation.status == "active"
                )
            )
        ).all()
        if reservations and all(row.expires_at > utc_now() for row in reservations):
            for row in reservations:
                row.status = "consumed"
                row.consumed_at = utc_now()
            previous = order.status
            order.status = OrderStatus.PAID.value
        else:
            previous = order.status
            order.status = OrderStatus.AWAITING_STOCK_CONFIRMATION.value
            order.integration_status = "degraded"
        session.add(
            PaymentCallback(
                payment_id=payment.id,
                fingerprint=fingerprint,
                amount_minor=amount_minor,
                signature_valid=True,
                processing_result="applied",
                received_at=utc_now(),
                processed_at=utc_now(),
            )
        )
        session.add(
            OrderStatusHistory(
                order_id=order.id,
                from_status=previous,
                to_status=order.status,
                actor_type="provider",
                reason_code="robokassa_result",
                request_id=request_id,
            )
        )
        session.add(
            OutboxEvent(
                aggregate_type="payment",
                aggregate_id=payment.id,
                aggregate_version=1,
                event_type="payment.succeeded",
                payload={"order_id": str(order.id)},
                deduplication_key=f"payment.succeeded:{payment.id}",
                request_id=request_id,
            )
        )
        await session.commit()
        return f"OK{inv_id}"

    async def list_customer_orders(
        self, session: AsyncSession, customer_id: UUID, page: int, limit: int
    ) -> dict[str, Any]:
        total = int(
            await session.scalar(
                select(func.count(Order.id)).where(Order.customer_id == customer_id)
            )
            or 0
        )
        orders = (
            await session.scalars(
                select(Order)
                .where(Order.customer_id == customer_id)
                .order_by(Order.created_at.desc())
                .offset((page - 1) * limit)
                .limit(limit)
            )
        ).all()
        return {
            "items": [await self.order_payload(session, order) for order in orders],
            "page": page,
            "limit": limit,
            "total": total,
        }

    async def list_admin_orders(
        self, session: AsyncSession, status: str | None, page: int, limit: int
    ) -> dict[str, Any]:
        where = [Order.status == status] if status else []
        total = int(await session.scalar(select(func.count(Order.id)).where(*where)) or 0)
        orders = (
            await session.scalars(
                select(Order)
                .where(*where)
                .order_by(Order.created_at.desc())
                .offset((page - 1) * limit)
                .limit(limit)
            )
        ).all()
        return {
            "items": [await self.order_payload(session, order, admin=True) for order in orders],
            "page": page,
            "limit": limit,
            "total": total,
        }
