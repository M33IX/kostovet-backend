from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import boto3
from cryptography.fernet import Fernet
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from kosto_vet.bootstrap.settings import Mode, Settings
from kosto_vet.core.errors import DomainError, not_found
from kosto_vet.core.types import (
    OrderStatus,
    PaymentStatus,
    ensure_transition,
    new_id,
    stock_state,
    utc_now,
)
from kosto_vet.infrastructure.integrations import (
    RobokassaAdapter,
    YandexIdAdapter,
    encode_pkce_verifier,
    safe_payload_hash,
)
from kosto_vet.infrastructure.models import (
    Article,
    ArticleMedia,
    AuditLog,
    Cart,
    CartItem,
    Category,
    CustomerAccount,
    CustomerConsent,
    CustomerCredential,
    CustomerDeliveryAddress,
    CustomerOAuthAccount,
    CustomerSession,
    Favorite,
    IdempotencyRecord,
    IntegrationJob,
    Lead,
    MediaAsset,
    MediaVariant,
    OAuthTransaction,
    Order,
    OrderItem,
    OrderStatusHistory,
    OutboxEvent,
    PaymentAttempt,
    PaymentCallback,
    Product,
    ProductImage,
    ProductImageVariant,
    ProductMedia,
    ProductSpec,
    RelatedProduct,
    StaffSession,
    StaffUser,
    StockItem,
    StockReservation,
    StockSubscription,
)
from kosto_vet.infrastructure.security import (
    hash_password,
    issue_access_token,
    normalize_email,
    random_token,
    token_hash,
    verify_password,
)


@dataclass(frozen=True, slots=True)
class SessionBundle:
    access: str
    refresh: str
    session_id: UUID
    subject_id: UUID
    role: str | None = None


def money(amount: int) -> dict[str, Any]:
    return {"amount": amount, "currency": "RUB"}


def manager(settings: Settings) -> dict[str, Any]:
    return {
        "name": settings.default_manager_name,
        "phone": settings.default_manager_phone,
        "email": settings.default_manager_email,
        "scope": "global",
    }


class ApplicationService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.robokassa = RobokassaAdapter(settings)
        self.yandex = YandexIdAdapter(settings)
        fernet_key = base64.urlsafe_b64encode(
            hashlib.sha256(settings.csrf_secret.get_secret_value().encode()).digest()
        )
        self._fernet = Fernet(fernet_key)

    async def start_yandex(
        self,
        session: AsyncSession,
        *,
        return_path: str,
        customer_id: UUID | None = None,
    ) -> str:
        if not return_path.startswith("/") or return_path.startswith("//"):
            raise DomainError("INVALID_REQUEST", "Недопустимый адрес возврата.", 400)
        state = random_token()
        verifier = random_token(48)
        nonce = random_token()
        authorization_url = self.yandex.authorization_url(
            state=state, challenge=encode_pkce_verifier(verifier)
        )
        session.add(
            OAuthTransaction(
                state_hash=token_hash(state),
                verifier_ciphertext=self._fernet.encrypt(verifier.encode()),
                nonce_hash=token_hash(nonce),
                return_path=return_path,
                purpose="link" if customer_id else "login",
                customer_id=customer_id,
                expires_at=utc_now() + timedelta(minutes=10),
            )
        )
        await session.commit()
        return authorization_url

    async def complete_yandex(
        self,
        session: AsyncSession,
        *,
        state: str,
        code: str,
    ) -> tuple[str, SessionBundle | None]:
        transaction = await session.scalar(
            select(OAuthTransaction)
            .where(OAuthTransaction.state_hash == token_hash(state))
            .with_for_update()
        )
        if (
            not transaction
            or transaction.consumed_at is not None
            or transaction.expires_at <= utc_now()
        ):
            raise DomainError("OAUTH_STATE_INVALID", "OAuth-сессия истекла.", 400)
        transaction.consumed_at = utc_now()
        verifier = self._fernet.decrypt(transaction.verifier_ciphertext).decode()
        purpose = transaction.purpose
        link_customer_id = transaction.customer_id
        return_path = transaction.return_path
        await session.commit()
        identity = await self.yandex.identity(code=code, verifier=verifier)
        oauth = await session.scalar(
            select(CustomerOAuthAccount).where(
                CustomerOAuthAccount.provider == "yandex",
                CustomerOAuthAccount.provider_subject == identity["subject"],
            )
        )
        customer: CustomerAccount | None = None
        if oauth:
            customer = await session.get(CustomerAccount, oauth.customer_id)
            oauth.last_login_at = utc_now()
        elif purpose == "link" and link_customer_id:
            customer = await session.get(CustomerAccount, link_customer_id)
        elif identity.get("email"):
            customer = await session.scalar(
                select(CustomerAccount).where(
                    CustomerAccount.normalized_email == normalize_email(identity["email"])
                )
            )
        if not customer:
            email = identity.get("email")
            if not email:
                raise DomainError("OAUTH_EMAIL_REQUIRED", "Яндекс не передал email.", 400)
            customer = CustomerAccount(
                email=email,
                normalized_email=normalize_email(email),
                name=identity.get("name") or "Покупатель",
                customer_type="individual",
                status="active",
                is_email_verified=True,
                email_verified_source="yandex",
            )
            session.add(customer)
            await session.flush()
        if not oauth:
            conflict = await session.scalar(
                select(CustomerOAuthAccount).where(
                    CustomerOAuthAccount.provider == "yandex",
                    CustomerOAuthAccount.customer_id == customer.id,
                )
            )
            if conflict and conflict.provider_subject != identity["subject"]:
                raise DomainError("OAUTH_LINK_CONFLICT", "Яндекс ID уже связан иначе.", 409)
            oauth = CustomerOAuthAccount(
                customer_id=customer.id,
                provider="yandex",
                provider_subject=identity["subject"],
                provider_user_id=identity["user_id"],
                provider_email=identity.get("email"),
                provider_login=identity.get("login"),
                profile_snapshot={
                    "name": identity.get("name"),
                    "login": identity.get("login"),
                },
            )
            session.add(oauth)
            if not customer.is_email_verified:
                customer.is_email_verified = True
                customer.email_verified_source = "yandex"
                await session.execute(
                    update(CustomerSession)
                    .where(
                        CustomerSession.customer_id == customer.id,
                        CustomerSession.revoked_at.is_(None),
                    )
                    .values(revoked_at=utc_now(), revoke_reason="secure_yandex_auto_link")
                )
                credential = await session.scalar(
                    select(CustomerCredential).where(
                        CustomerCredential.customer_id == customer.id,
                        CustomerCredential.kind == "password",
                    )
                )
                if credential:
                    credential.status = "disabled"
                    credential.disabled_reason = "secure_yandex_auto_link"
        session.add(
            AuditLog(
                actor_type="customer",
                actor_id=customer.id,
                action="identity.yandex.link",
                target_type="customer",
                target_id=customer.id,
                after={"provider": "yandex"},
                request_id="oauth",
            )
        )
        if purpose == "link":
            await session.commit()
            return return_path, None
        bundle = await self._create_customer_session(session, customer.id)
        await session.commit()
        return return_path, bundle

    async def unlink_yandex(self, session: AsyncSession, customer_id: UUID) -> None:
        oauth = await session.scalar(
            select(CustomerOAuthAccount).where(
                CustomerOAuthAccount.customer_id == customer_id,
                CustomerOAuthAccount.provider == "yandex",
            )
        )
        if not oauth:
            return
        credential = await session.scalar(
            select(CustomerCredential).where(
                CustomerCredential.customer_id == customer_id,
                CustomerCredential.kind == "password",
                CustomerCredential.status == "active",
            )
        )
        if not credential:
            raise DomainError("LAST_LOGIN_METHOD", "Нельзя удалить единственный способ входа.", 400)
        await session.delete(oauth)
        await session.commit()

    async def _idempotency_replay(
        self,
        session: AsyncSession,
        *,
        actor_scope: str,
        endpoint: str,
        key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        request_hash = safe_payload_hash(payload)
        record = await session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.actor_scope == actor_scope,
                IdempotencyRecord.endpoint == endpoint,
                IdempotencyRecord.idempotency_key == key,
            )
        )
        if record:
            if record.request_hash != request_hash:
                raise DomainError(
                    "IDEMPOTENCY_CONFLICT", "Ключ уже использован с другим запросом.", 409
                )
            if record.response_body is None:
                return None
            response = dict(record.response_body)
            protected_token = response.get("order_access_token")
            if isinstance(protected_token, str) and protected_token.startswith("fernet:"):
                response["order_access_token"] = self._fernet.decrypt(
                    protected_token.removeprefix("fernet:").encode()
                ).decode()
            return response
        return None

    async def _save_idempotency(
        self,
        session: AsyncSession,
        *,
        actor_scope: str,
        endpoint: str,
        key: str,
        payload: dict[str, Any],
        response: dict[str, Any],
        resource_id: UUID | None = None,
        financial: bool = False,
    ) -> None:
        stored_response = dict(response)
        order_access_token = stored_response.get("order_access_token")
        if isinstance(order_access_token, str) and order_access_token:
            stored_response["order_access_token"] = (
                "fernet:" + self._fernet.encrypt(order_access_token.encode()).decode()
            )
        session.add(
            IdempotencyRecord(
                actor_scope=actor_scope,
                endpoint=endpoint,
                idempotency_key=key,
                request_hash=safe_payload_hash(payload),
                response_status=201 if financial else 202,
                response_body=stored_response,
                resource_id=resource_id,
                expires_at=utc_now() + timedelta(days=3650 if financial else 1),
            )
        )

    def public_settings(self) -> dict[str, Any]:
        if self.settings.fixed_delivery_price_minor is None:
            raise DomainError(
                "DELIVERY_PRICE_NOT_CONFIGURED",
                "Стоимость доставки не настроена.",
                503,
            )
        return {
            "fixed_delivery_price": money(self.settings.fixed_delivery_price_minor),
            "stock_reservation_ttl_seconds": self.settings.stock_reservation_ttl_seconds,
            "manager": manager(self.settings),
            "emergency_phone": self.settings.emergency_phone,
            "emergency_surcharge_percent": self.settings.emergency_surcharge_percent,
        }

    async def _stock_map(
        self, session: AsyncSession, product_ids: list[UUID]
    ) -> dict[UUID, StockItem]:
        rows = (
            await session.scalars(select(StockItem).where(StockItem.product_id.in_(product_ids)))
        ).all()
        return {row.product_id: row for row in rows}

    async def _images(
        self, session: AsyncSession, product_ids: list[UUID]
    ) -> dict[UUID, dict[str, Any]]:
        generic_rows = (
            await session.execute(
                select(ProductMedia, MediaAsset)
                .join(MediaAsset, MediaAsset.id == ProductMedia.asset_id)
                .where(
                    ProductMedia.product_id.in_(product_ids),
                    ProductMedia.is_primary.is_(True),
                    MediaAsset.status == "ready",
                )
                .order_by(ProductMedia.sort_order)
            )
        ).all()
        result: dict[UUID, dict[str, Any]] = {}
        for link, asset in generic_rows:
            payload = await self._media_payload(session, asset, link.alt)
            if payload["url"]:
                result[link.product_id] = {
                    "url": payload["url"],
                    "alt": link.alt,
                    "width": payload["width"],
                    "height": payload["height"],
                    "variants": payload["variants"],
                }
        images = (
            await session.scalars(
                select(ProductImage)
                .where(
                    ProductImage.product_id.in_(product_ids),
                    ProductImage.status == "ready",
                    ProductImage.is_primary.is_(True),
                    ProductImage.public_url.is_not(None),
                )
                .order_by(ProductImage.sort_order)
            )
        ).all()
        for image in images:
            if image.product_id in result:
                continue
            variants = (
                await session.scalars(
                    select(ProductImageVariant).where(ProductImageVariant.image_id == image.id)
                )
            ).all()
            result[image.product_id] = {
                "url": image.public_url,
                "alt": image.alt,
                "variants": [
                    {
                        "kind": variant.kind,
                        "format": variant.format,
                        "url": variant.public_url,
                        "width": variant.width,
                        "height": variant.height,
                    }
                    for variant in variants
                ],
            }
        return result

    async def products_to_public(
        self, session: AsyncSession, products: list[Product]
    ) -> list[dict[str, Any]]:
        if not products:
            return []
        category_ids = {p.category_id for p in products}
        categories = {
            row.id: row
            for row in (
                await session.scalars(select(Category).where(Category.id.in_(category_ids)))
            ).all()
        }
        stock = await self._stock_map(session, [p.id for p in products])
        images = await self._images(session, [p.id for p in products])
        specs = (
            await session.scalars(
                select(ProductSpec)
                .where(ProductSpec.product_id.in_([p.id for p in products]))
                .order_by(ProductSpec.sort_order)
            )
        ).all()
        specs_by_product: dict[UUID, list[dict[str, Any]]] = {}
        for spec in specs:
            spec_payload = {"name": spec.label, "value": spec.value}
            if spec.unit is not None:
                spec_payload["unit"] = spec.unit
            specs_by_product.setdefault(spec.product_id, []).append(spec_payload)
        result = []
        for product in products:
            category = categories[product.category_id]
            item = stock.get(product.id)
            quantity = item.available_quantity if item else 0
            product_payload: dict[str, Any] = {
                "id": str(product.id),
                "slug": product.slug,
                "category_slug": category.slug,
                "category_path": category.path,
                "name": product.name,
                "article": product.article,
                "price": money(product.final_price_minor),
                "stock": {
                    "quantity": quantity,
                    "state": stock_state(quantity, stale=not item or item.is_stale).value,
                    "label": "Остатки уточняются"
                    if not item or item.is_stale
                    else ("В наличии" if quantity > 0 else "Нет в наличии"),
                    "synced_at": item.synced_at.isoformat()
                    if item and item.synced_at
                    else utc_now().isoformat(),
                    "is_stale": True if not item else item.is_stale,
                },
                "specs_preview": specs_by_product.get(product.id, [])[:4],
            }
            if product.subtitle is not None:
                product_payload["subtitle"] = product.subtitle
            if product.id in images:
                product_payload["image"] = images[product.id]
            result.append(product_payload)
        return result

    async def list_products(
        self,
        session: AsyncSession,
        *,
        category_slug: str | None,
        include_descendants: bool,
        q: str | None,
        in_stock: bool | None,
        state: str | None,
        sort: str,
        page: int,
        limit: int,
    ) -> dict[str, Any]:
        stmt = select(Product).where(Product.is_published.is_(True), Product.is_active.is_(True))
        if category_slug:
            category = await session.scalar(
                select(Category).where(
                    Category.slug == category_slug, Category.is_published.is_(True)
                )
            )
            if not category:
                raise not_found("CATEGORY_NOT_FOUND", "Категория не найдена.")
            if include_descendants:
                stmt = stmt.join(Category, Product.category_id == Category.id).where(
                    or_(Category.path == category.path, Category.path.like(f"{category.path}/%"))
                )
            else:
                stmt = stmt.where(Product.category_id == category.id)
        if q:
            pattern = f"%{q.strip()}%"
            stmt = stmt.where(or_(Product.name.ilike(pattern), Product.article.ilike(pattern)))
        if in_stock is not None or state:
            stmt = stmt.join(StockItem, StockItem.product_id == Product.id)
            if in_stock is True:
                stmt = stmt.where(StockItem.available_quantity > 0, StockItem.is_stale.is_(False))
            elif in_stock is False:
                stmt = stmt.where(StockItem.available_quantity <= 0)
            if state == "available":
                stmt = stmt.where(StockItem.available_quantity > 10, StockItem.is_stale.is_(False))
            elif state == "low":
                stmt = stmt.where(
                    StockItem.available_quantity.between(1, 10), StockItem.is_stale.is_(False)
                )
            elif state == "out":
                stmt = stmt.where(StockItem.available_quantity == 0)
            elif state == "unknown":
                stmt = stmt.where(StockItem.is_stale.is_(True))
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int(await session.scalar(count_stmt) or 0)
        if sort == "price_asc":
            stmt = stmt.order_by(Product.final_price_minor, Product.name)
        elif sort == "price_desc":
            stmt = stmt.order_by(Product.final_price_minor.desc(), Product.name)
        elif sort == "name_asc":
            stmt = stmt.order_by(Product.name)
        elif sort == "stock_desc":
            if in_stock is None and state is None:
                stmt = stmt.outerjoin(StockItem, StockItem.product_id == Product.id)
            stmt = stmt.order_by(StockItem.available_quantity.desc().nullslast(), Product.name)
        else:
            stmt = stmt.order_by(Product.created_at, Product.name)
        products = (
            (await session.scalars(stmt.offset((page - 1) * limit).limit(limit))).unique().all()
        )
        return {
            "items": await self.products_to_public(session, list(products)),
            "page": page,
            "limit": limit,
            "total": total,
        }

    async def product_detail(
        self, session: AsyncSession, slug_or_id: str | UUID, *, include_inactive: bool = False
    ) -> dict[str, Any]:
        condition = (
            Product.id == slug_or_id if isinstance(slug_or_id, UUID) else Product.slug == slug_or_id
        )
        product = await session.scalar(select(Product).where(condition))
        if not product or (
            not include_inactive and (not product.is_published or not product.is_active)
        ):
            raise not_found()
        payload = (await self.products_to_public(session, [product]))[0]
        specs = (
            await session.scalars(
                select(ProductSpec)
                .where(ProductSpec.product_id == product.id)
                .order_by(ProductSpec.sort_order)
            )
        ).all()
        related_ids = (
            await session.scalars(
                select(RelatedProduct.related_product_id)
                .where(RelatedProduct.product_id == product.id)
                .order_by(RelatedProduct.sort_order)
            )
        ).all()
        related = (
            (
                await session.scalars(
                    select(Product).where(
                        Product.id.in_(related_ids),
                        Product.is_published.is_(True),
                        Product.is_active.is_(True),
                    )
                )
            ).all()
            if related_ids
            else []
        )
        all_images = (
            await session.scalars(
                select(ProductImage)
                .where(
                    ProductImage.product_id == product.id,
                    ProductImage.status == "ready",
                    ProductImage.public_url.is_not(None),
                )
                .order_by(ProductImage.sort_order)
            )
        ).all()
        images = []
        for image in all_images:
            variants = (
                await session.scalars(
                    select(ProductImageVariant).where(ProductImageVariant.image_id == image.id)
                )
            ).all()
            images.append(
                {
                    "url": image.public_url,
                    "alt": image.alt,
                    "variants": [
                        {
                            "kind": v.kind,
                            "format": v.format,
                            "url": v.public_url,
                            "width": v.width,
                            "height": v.height,
                        }
                        for v in variants
                    ],
                }
            )
        detail_specs = []
        for spec in specs:
            spec_payload = {"name": spec.label, "value": spec.value}
            if spec.unit is not None:
                spec_payload["unit"] = spec.unit
            detail_specs.append(spec_payload)
        seo = {}
        if product.seo_title is not None:
            seo["title"] = product.seo_title
        if product.seo_description is not None:
            seo["description"] = product.seo_description
        payload.update(
            {
                "specs": detail_specs,
                "images": images,
                "related": await self.products_to_public(session, list(related)),
                "seo": seo,
            }
        )
        if product.description is not None:
            payload["description"] = product.description
        return payload

    async def category_tree(self, session: AsyncSession) -> dict[str, Any]:
        categories = (
            await session.scalars(
                select(Category)
                .where(Category.is_active.is_(True), Category.is_published.is_(True))
                .order_by(Category.depth, Category.sort_order, Category.title)
            )
        ).all()
        count_rows = (
            await session.execute(
                select(Product.category_id, func.count(Product.id))
                .where(Product.is_active.is_(True), Product.is_published.is_(True))
                .group_by(Product.category_id)
            )
        ).tuples()
        counts: dict[UUID, int] = {category_id: int(count) for category_id, count in count_rows}
        by_parent: dict[UUID | None, list[Category]] = {}
        for category in categories:
            by_parent.setdefault(category.parent_id, []).append(category)

        def node(category: Category) -> dict[str, Any]:
            children = [node(child) for child in by_parent.get(category.id, [])]
            subtree_count = int(counts.get(category.id, 0)) + sum(
                child["subtree_product_count"] for child in children
            )
            payload: dict[str, Any] = {
                "id": str(category.id),
                "parent_id": str(category.parent_id) if category.parent_id else None,
                "parent_slug": None,
                "slug": category.slug,
                "path": category.path,
                "title": category.title,
                "description": category.description,
                "depth": category.depth,
                "is_leaf": not children,
                "direct_product_count": int(counts.get(category.id, 0)),
                "subtree_product_count": subtree_count,
                "children": children,
            }
            seo = {}
            if category.seo_title is not None:
                seo["title"] = category.seo_title
            if category.seo_description is not None:
                seo["description"] = category.seo_description
            if seo:
                payload["seo"] = seo
            return payload

        slug_by_id = {category.id: category.slug for category in categories}
        roots = [node(category) for category in by_parent.get(None, [])]

        def fill_parent(items: list[dict[str, Any]]) -> None:
            for item in items:
                parent_id = UUID(item["parent_id"]) if item["parent_id"] else None
                item["parent_slug"] = slug_by_id.get(parent_id) if parent_id else None
                fill_parent(item["children"])

        fill_parent(roots)
        return {"items": roots}

    async def category_detail(self, session: AsyncSession, slug: str) -> dict[str, Any]:
        category = await session.scalar(
            select(Category).where(
                Category.slug == slug, Category.is_published.is_(True), Category.is_active.is_(True)
            )
        )
        if not category:
            raise not_found("CATEGORY_NOT_FOUND", "Категория не найдена.")
        tree = await self.category_tree(session)
        flat: list[dict[str, Any]] = []

        def walk(items: list[dict[str, Any]]) -> dict[str, Any] | None:
            for item in items:
                flat.append(item)
                if item["slug"] == slug:
                    return item
                found = walk(item["children"])
                if found:
                    return found
            return None

        node = walk(tree["items"])
        if node is None:
            raise not_found("CATEGORY_NOT_FOUND", "Категория не найдена.")
        path_parts = category.path.split("/")
        ancestors = []
        current = ""
        for part in path_parts:
            current = f"{current}/{part}".strip("/")
            row = await session.scalar(select(Category).where(Category.path == current))
            if row:
                ancestors.append(
                    {"id": str(row.id), "slug": row.slug, "path": row.path, "title": row.title}
                )
        return {**node, "breadcrumbs": ancestors, "subtree": node["children"]}

    async def suggestions(self, session: AsyncSession, q: str) -> dict[str, Any]:
        pattern = f"%{q.strip()}%"
        products = (
            await session.scalars(
                select(Product)
                .where(
                    Product.is_published.is_(True),
                    or_(Product.name.ilike(pattern), Product.article.ilike(pattern)),
                )
                .limit(8)
            )
        ).all()
        categories = (
            await session.scalars(
                select(Category)
                .where(Category.is_published.is_(True), Category.title.ilike(pattern))
                .limit(4)
            )
        ).all()
        return {
            "items": [
                {"label": p.name, "type": "product", "url": f"/catalog/{p.slug}"} for p in products
            ]
            + [
                {"label": c.title, "type": "category", "url": f"/catalog/{c.slug}"}
                for c in categories
            ]
        }

    async def create_lead(
        self, session: AsyncSession, *, key: str, payload: dict[str, Any], request_id: str
    ) -> dict[str, Any]:
        replay = await self._idempotency_replay(
            session, actor_scope="guest", endpoint="createLead", key=key, payload=payload
        )
        if replay:
            return replay
        product = None
        if payload.get("product_slug"):
            product = await session.scalar(
                select(Product).where(Product.slug == payload["product_slug"])
            )
        lead = Lead(
            name=payload["name"],
            phone=payload["phone"],
            email=payload.get("email"),
            company=payload.get("company"),
            message=payload.get("message"),
            product_id=product.id if product else None,
            source=payload.get("source", "contact"),
            consent_version="demo-v1",
        )
        session.add(lead)
        await session.flush()
        session.add(
            CustomerConsent(
                subject_ref=f"lead:{lead.id}",
                consent_type="personal_data",
                document_version="demo-v1",
                accepted=True,
                source="web_lead",
                request_id=request_id,
            )
        )
        response = {"ok": True, "id": str(lead.id)}
        await self._save_idempotency(
            session,
            actor_scope="guest",
            endpoint="createLead",
            key=key,
            payload=payload,
            response=response,
            resource_id=lead.id,
        )
        await session.commit()
        return response

    async def create_stock_subscription(
        self, session: AsyncSession, *, key: str, payload: dict[str, Any], request_id: str
    ) -> dict[str, Any]:
        replay = await self._idempotency_replay(
            session,
            actor_scope="guest",
            endpoint="createStockSubscription",
            key=key,
            payload=payload,
        )
        if replay:
            return replay
        product = await session.scalar(
            select(Product).where(Product.slug == payload["product_slug"])
        )
        if not product:
            raise not_found()
        item = StockSubscription(
            product_id=product.id,
            name=payload["name"],
            normalized_contact=payload["contact"].strip().casefold(),
            consent_version="demo-v1",
        )
        session.add(item)
        await session.flush()
        session.add(
            CustomerConsent(
                subject_ref=f"stock_subscription:{item.id}",
                consent_type="stock_notification",
                document_version="demo-v1",
                accepted=True,
                source="web_stock_subscription",
                request_id=request_id,
            )
        )
        response = {"ok": True, "id": str(item.id)}
        await self._save_idempotency(
            session,
            actor_scope="guest",
            endpoint="createStockSubscription",
            key=key,
            payload=payload,
            response=response,
            resource_id=item.id,
        )
        await session.commit()
        return response

    def customer_public(
        self,
        customer: CustomerAccount,
        providers: list[CustomerOAuthAccount] | None = None,
        credential: CustomerCredential | None = None,
    ) -> dict[str, Any]:
        auth_providers = []
        if credential:
            auth_providers.append(
                {
                    "provider": "password",
                    "status": credential.status,
                    "linked_at": credential.created_at.isoformat(),
                }
            )
        for provider in providers or []:
            auth_providers.append(
                {
                    "provider": provider.provider,
                    "status": "active",
                    "linked_at": provider.linked_at.isoformat(),
                }
            )
        return {
            "id": str(customer.id),
            "name": customer.name,
            "phone": customer.phone,
            "email": customer.email,
            "customer_type": customer.customer_type,
            "company_name": customer.company_name,
            "inn": customer.inn,
            "documents_email": customer.documents_email,
            "is_email_verified": customer.is_email_verified,
            "is_phone_verified": customer.is_phone_verified,
            "manager": manager(self.settings),
            "auth_providers": auth_providers,
        }

    async def _customer_payload(
        self, session: AsyncSession, customer: CustomerAccount
    ) -> dict[str, Any]:
        providers = (
            await session.scalars(
                select(CustomerOAuthAccount).where(CustomerOAuthAccount.customer_id == customer.id)
            )
        ).all()
        credential = await session.scalar(
            select(CustomerCredential).where(
                CustomerCredential.customer_id == customer.id, CustomerCredential.kind == "password"
            )
        )
        return self.customer_public(customer, list(providers), credential)

    async def _create_customer_session(
        self, session: AsyncSession, customer_id: UUID
    ) -> SessionBundle:
        refresh = random_token()
        family = new_id()
        row = CustomerSession(
            customer_id=customer_id,
            family_id=family,
            token_hash=token_hash(refresh),
            expires_at=utc_now() + timedelta(seconds=self.settings.customer_refresh_ttl_seconds),
        )
        session.add(row)
        await session.flush()
        access = issue_access_token(
            self.settings, subject=customer_id, session_id=row.id, audience="customer"
        )
        return SessionBundle(access, refresh, row.id, customer_id)

    async def register(
        self, session: AsyncSession, payload: dict[str, Any], *, request_id: str
    ) -> tuple[dict[str, Any], SessionBundle]:
        normalized = normalize_email(payload["email"])
        if await session.scalar(
            select(CustomerAccount.id).where(CustomerAccount.normalized_email == normalized)
        ):
            raise DomainError("ACCOUNT_EXISTS", "Аккаунт уже существует.", 409)
        customer = CustomerAccount(
            email=payload["email"],
            normalized_email=normalized,
            phone=payload.get("phone"),
            normalized_phone=payload.get("phone"),
            name=payload["name"],
            customer_type=payload.get("customer_type", "individual"),
            company_name=payload.get("company_name"),
            inn=payload.get("inn"),
            documents_email=payload.get("documents_email"),
            status="active",
        )
        session.add(customer)
        await session.flush()
        session.add(
            CustomerConsent(
                customer_id=customer.id,
                subject_ref=f"customer:{customer.id}",
                consent_type="personal_data",
                document_version="demo-v1",
                accepted=True,
                source="web_registration",
                request_id=request_id,
            )
        )
        credential = CustomerCredential(
            customer_id=customer.id,
            password_hash=hash_password(payload["password"]),
            status="active",
        )
        session.add(credential)
        bundle = await self._create_customer_session(session, customer.id)
        await session.commit()
        return {"customer": self.customer_public(customer, [], credential)}, bundle

    async def customer_login(
        self, session: AsyncSession, email: str, password: str
    ) -> tuple[dict[str, Any], SessionBundle]:
        customer = await session.scalar(
            select(CustomerAccount).where(
                CustomerAccount.normalized_email == normalize_email(email)
            )
        )
        credential = (
            await session.scalar(
                select(CustomerCredential).where(
                    CustomerCredential.customer_id == customer.id,
                    CustomerCredential.kind == "password",
                )
            )
            if customer
            else None
        )
        if (
            not customer
            or customer.status != "active"
            or not credential
            or credential.status != "active"
            or not verify_password(password, credential.password_hash if credential else None)
        ):
            if not credential:
                verify_password(password, None)
            raise DomainError("AUTH_INVALID", "Неверный email или пароль.", 401)
        bundle = await self._create_customer_session(session, customer.id)
        await session.commit()
        return {"customer": await self._customer_payload(session, customer)}, bundle

    async def staff_login(
        self, session: AsyncSession, email: str, password: str
    ) -> tuple[dict[str, Any], SessionBundle]:
        staff = await session.scalar(
            select(StaffUser).where(StaffUser.normalized_email == normalize_email(email))
        )
        if (
            not staff
            or staff.status != "active"
            or not verify_password(password, staff.password_hash if staff else None)
        ):
            if not staff:
                verify_password(password, None)
            raise DomainError("AUTH_INVALID", "Неверный email или пароль.", 401)
        refresh = random_token()
        family = new_id()
        row = StaffSession(
            staff_id=staff.id,
            family_id=family,
            token_hash=token_hash(refresh),
            expires_at=utc_now() + timedelta(seconds=self.settings.staff_refresh_ttl_seconds),
        )
        session.add(row)
        await session.flush()
        access = issue_access_token(
            self.settings, subject=staff.id, session_id=row.id, audience="staff", role=staff.role
        )
        await session.commit()
        return {
            "user": {"id": str(staff.id), "email": staff.email, "role": staff.role}
        }, SessionBundle(access, refresh, row.id, staff.id, staff.role)

    async def rotate_session(
        self, session: AsyncSession, *, refresh_token: str, audience: str
    ) -> tuple[dict[str, Any], SessionBundle]:
        if audience == "customer":
            row = await session.scalar(
                select(CustomerSession)
                .where(CustomerSession.token_hash == token_hash(refresh_token))
                .with_for_update()
            )
        else:
            row = await session.scalar(
                select(StaffSession)
                .where(StaffSession.token_hash == token_hash(refresh_token))
                .with_for_update()
            )
        if not row:
            raise DomainError("SESSION_EXPIRED", "Сессия истекла.", 401)
        if row.revoked_at is not None:
            if audience == "customer":
                await session.execute(
                    update(CustomerSession)
                    .where(
                        CustomerSession.family_id == row.family_id,
                        CustomerSession.revoked_at.is_(None),
                    )
                    .values(revoked_at=utc_now(), revoke_reason="refresh_replay")
                )
            else:
                await session.execute(
                    update(StaffSession)
                    .where(
                        StaffSession.family_id == row.family_id,
                        StaffSession.revoked_at.is_(None),
                    )
                    .values(revoked_at=utc_now(), revoke_reason="refresh_replay")
                )
            await session.commit()
            raise DomainError(
                "SESSION_EXPIRED", "Обнаружено повторное использование refresh token.", 401
            )
        if row.expires_at <= utc_now():
            row.revoked_at = utc_now()
            row.revoke_reason = "expired"
            await session.commit()
            raise DomainError("SESSION_EXPIRED", "Сессия истекла.", 401)
        row.revoked_at = utc_now()
        row.revoke_reason = "rotated"
        refresh = random_token()
        new_row: CustomerSession | StaffSession
        if isinstance(row, CustomerSession):
            new_row = CustomerSession(
                customer_id=row.customer_id,
                family_id=row.family_id,
                token_hash=token_hash(refresh),
                expires_at=utc_now()
                + timedelta(seconds=self.settings.customer_refresh_ttl_seconds),
            )
            subject_id = row.customer_id
            role = None
            customer = await session.get(CustomerAccount, subject_id)
            if not customer:
                raise DomainError("SESSION_EXPIRED", "Аккаунт не найден.", 401)
            payload = {"customer": await self._customer_payload(session, customer)}
        else:
            new_row = StaffSession(
                staff_id=row.staff_id,
                family_id=row.family_id,
                token_hash=token_hash(refresh),
                expires_at=utc_now() + timedelta(seconds=self.settings.staff_refresh_ttl_seconds),
            )
            subject_id = row.staff_id
            staff = await session.get(StaffUser, subject_id)
            if not staff:
                raise DomainError("SESSION_EXPIRED", "Сотрудник не найден.", 401)
            role = staff.role
            payload = {"user": {"id": str(staff.id), "email": staff.email, "role": staff.role}}
        session.add(new_row)
        await session.flush()
        access = issue_access_token(
            self.settings, subject=subject_id, session_id=new_row.id, audience=audience, role=role
        )
        await session.commit()
        return payload, SessionBundle(access, refresh, new_row.id, subject_id, role)

    async def revoke_refresh(
        self, session: AsyncSession, *, refresh_token: str | None, audience: str
    ) -> None:
        if refresh_token:
            if audience == "customer":
                row = await session.scalar(
                    select(CustomerSession).where(
                        CustomerSession.token_hash == token_hash(refresh_token)
                    )
                )
            else:
                row = await session.scalar(
                    select(StaffSession).where(StaffSession.token_hash == token_hash(refresh_token))
                )
            if row and row.revoked_at is None:
                if audience == "customer":
                    await session.execute(
                        update(CustomerSession)
                        .where(
                            CustomerSession.family_id == row.family_id,
                            CustomerSession.revoked_at.is_(None),
                        )
                        .values(revoked_at=utc_now(), revoke_reason="logout")
                    )
                else:
                    await session.execute(
                        update(StaffSession)
                        .where(
                            StaffSession.family_id == row.family_id,
                            StaffSession.revoked_at.is_(None),
                        )
                        .values(revoked_at=utc_now(), revoke_reason="logout")
                    )
                await session.commit()

    async def customer_me(self, session: AsyncSession, customer_id: UUID) -> dict[str, Any]:
        customer = await session.get(CustomerAccount, customer_id)
        if not customer:
            raise not_found()
        return {"customer": await self._customer_payload(session, customer)}

    async def update_customer(
        self, session: AsyncSession, customer_id: UUID, payload: dict[str, Any]
    ) -> dict[str, Any]:
        customer = await session.get(CustomerAccount, customer_id, with_for_update=True)
        if not customer:
            raise not_found()
        expected = payload.pop("version", None)
        if expected is not None and expected != customer.version:
            raise DomainError("VERSION_CONFLICT", "Профиль был изменён.", 409)
        for key, value in payload.items():
            if value is not None and hasattr(customer, key):
                setattr(customer, key, value)
        customer.version += 1
        await session.commit()
        return {"customer": await self._customer_payload(session, customer)}

    @staticmethod
    def delivery_address_payload(address: CustomerDeliveryAddress) -> dict[str, Any]:
        return {
            "id": str(address.id),
            "label": address.label,
            "destination": address.destination,
            "city": address.city,
            "address_line": address.address_line,
            "postal_code": address.postal_code,
            "comment": address.comment,
            "is_default": address.is_default,
            "version": address.version,
            "created_at": address.created_at.isoformat(),
            "updated_at": address.updated_at.isoformat(),
        }

    async def list_delivery_addresses(
        self, session: AsyncSession, customer_id: UUID
    ) -> dict[str, Any]:
        addresses = (
            await session.scalars(
                select(CustomerDeliveryAddress)
                .where(CustomerDeliveryAddress.customer_id == customer_id)
                .order_by(
                    CustomerDeliveryAddress.is_default.desc(),
                    CustomerDeliveryAddress.updated_at.desc(),
                )
            )
        ).all()
        return {"items": [self.delivery_address_payload(address) for address in addresses]}

    async def create_delivery_address(
        self, session: AsyncSession, customer_id: UUID, payload: dict[str, Any]
    ) -> dict[str, Any]:
        customer = await session.get(CustomerAccount, customer_id, with_for_update=True)
        if not customer:
            raise not_found()
        requested_default = bool(payload.pop("is_default", False))
        current_default = await session.scalar(
            select(CustomerDeliveryAddress.id).where(
                CustomerDeliveryAddress.customer_id == customer_id,
                CustomerDeliveryAddress.is_default.is_(True),
            )
        )
        is_default = requested_default or current_default is None
        if is_default:
            await session.execute(
                update(CustomerDeliveryAddress)
                .where(CustomerDeliveryAddress.customer_id == customer_id)
                .values(is_default=False)
            )
        address = CustomerDeliveryAddress(
            customer_id=customer_id,
            is_default=is_default,
            **payload,
        )
        session.add(address)
        await session.flush()
        await session.commit()
        return self.delivery_address_payload(address)

    async def update_delivery_address(
        self,
        session: AsyncSession,
        customer_id: UUID,
        address_id: UUID,
        payload: dict[str, Any],
        expected_version: int,
    ) -> dict[str, Any]:
        customer = await session.get(CustomerAccount, customer_id, with_for_update=True)
        if not customer:
            raise not_found()
        address = await session.scalar(
            select(CustomerDeliveryAddress)
            .where(
                CustomerDeliveryAddress.id == address_id,
                CustomerDeliveryAddress.customer_id == customer_id,
            )
            .with_for_update()
        )
        if not address:
            raise not_found()
        if address.version != expected_version:
            raise DomainError("VERSION_CONFLICT", "Адрес был изменён.", 409)
        if not payload:
            raise DomainError("INVALID_REQUEST", "Не переданы поля для изменения.", 400)
        requested_default = payload.pop("is_default", None)
        if requested_default is True:
            await session.execute(
                update(CustomerDeliveryAddress)
                .where(
                    CustomerDeliveryAddress.customer_id == customer_id,
                    CustomerDeliveryAddress.id != address.id,
                )
                .values(is_default=False)
            )
            address.is_default = True
        elif requested_default is False and address.is_default:
            raise DomainError(
                "DEFAULT_ADDRESS_REQUIRED", "Сначала выберите другой адрес по умолчанию.", 409
            )
        required_fields = {"label", "destination", "city", "address_line"}
        if any(payload.get(field) is None for field in required_fields & payload.keys()):
            raise DomainError(
                "INVALID_REQUEST", "Обязательные поля адреса не могут быть пустыми.", 400
            )
        for field, value in payload.items():
            setattr(address, field, value)
        address.version += 1
        await session.commit()
        return self.delivery_address_payload(address)

    async def delete_delivery_address(
        self, session: AsyncSession, customer_id: UUID, address_id: UUID, expected_version: int
    ) -> None:
        customer = await session.get(CustomerAccount, customer_id, with_for_update=True)
        if not customer:
            raise not_found()
        address = await session.scalar(
            select(CustomerDeliveryAddress)
            .where(
                CustomerDeliveryAddress.id == address_id,
                CustomerDeliveryAddress.customer_id == customer_id,
            )
            .with_for_update()
        )
        if not address:
            raise not_found()
        if address.version != expected_version:
            raise DomainError("VERSION_CONFLICT", "Адрес был изменён.", 409)
        if address.is_default:
            address.is_default = False
            await session.flush()
            replacement = await session.scalar(
                select(CustomerDeliveryAddress)
                .where(
                    CustomerDeliveryAddress.customer_id == customer_id,
                    CustomerDeliveryAddress.id != address.id,
                )
                .order_by(CustomerDeliveryAddress.updated_at.desc())
                .with_for_update()
                .limit(1)
            )
            if replacement:
                replacement.is_default = True
                replacement.version += 1
        await session.delete(address)
        await session.commit()

    async def _cart(
        self, session: AsyncSession, customer_id: UUID, *, create: bool = True
    ) -> Cart | None:
        cart = await session.scalar(
            select(Cart).where(Cart.customer_id == customer_id, Cart.status == "active")
        )
        if not cart and create:
            cart = Cart(customer_id=customer_id, status="active")
            session.add(cart)
            await session.flush()
        return cart

    async def cart_payload(self, session: AsyncSession, customer_id: UUID) -> dict[str, Any]:
        cart = await self._cart(session, customer_id)
        if cart is None:
            raise RuntimeError("cart creation invariant failed")
        rows = (
            await session.execute(
                select(CartItem, Product)
                .join(Product, Product.id == CartItem.product_id)
                .where(CartItem.cart_id == cart.id)
                .order_by(CartItem.created_at)
            )
        ).all()
        stocks = await self._stock_map(session, [item.product_id for item, _ in rows])
        products_payload = {
            UUID(item["id"]): item
            for item in await self.products_to_public(session, [product for _, product in rows])
        }
        items = []
        subtotal = 0
        conflicts = False
        for cart_item, product in rows:
            public = products_payload[product.id]
            line_total = product.final_price_minor * cart_item.quantity
            subtotal += line_total
            stock = stocks.get(product.id)
            conflict = None
            if not stock or stock.is_stale or stock.available_quantity <= 0:
                conflict = "out_of_stock"
            elif cart_item.quantity > stock.available_quantity:
                conflict = "quantity_exceeds_stock"
            elif cart_item.price_snapshot_minor != product.final_price_minor:
                conflict = "price_changed"
            conflicts = conflicts or conflict is not None
            items.append(
                {
                    "id": str(cart_item.id),
                    "product": public,
                    "quantity": cart_item.quantity,
                    "unit_price": money(product.final_price_minor),
                    "line_total": money(line_total),
                    "stock": public["stock"],
                    "conflict": conflict,
                }
            )
        delivery = self.settings.fixed_delivery_price_minor or 0
        return {
            "id": str(cart.id),
            "version": cart.version,
            "items": items,
            "subtotal": money(subtotal),
            "delivery": money(delivery),
            "total": money(subtotal + delivery),
            "has_conflicts": conflicts,
            "updated_at": cart.updated_at.isoformat(),
        }

    async def upsert_cart_item(
        self,
        session: AsyncSession,
        customer_id: UUID,
        product_id: UUID,
        quantity: int,
        expected_version: int,
    ) -> dict[str, Any]:
        cart = await self._cart(session, customer_id)
        if cart is None:
            raise RuntimeError("cart creation invariant failed")
        await session.refresh(cart, with_for_update=True)
        if cart.version != expected_version:
            raise DomainError(
                "CART_VERSION_CONFLICT",
                "Корзина была изменена.",
                409,
                meta={"cart": await self.cart_payload(session, customer_id)},
            )
        product = await session.get(Product, product_id)
        if not product or not product.is_active:
            raise not_found()
        item = await session.scalar(
            select(CartItem).where(CartItem.cart_id == cart.id, CartItem.product_id == product_id)
        )
        if item:
            item.quantity = quantity
            item.price_snapshot_minor = product.final_price_minor
        else:
            session.add(
                CartItem(
                    cart_id=cart.id,
                    product_id=product_id,
                    quantity=quantity,
                    price_snapshot_minor=product.final_price_minor,
                )
            )
        cart.version += 1
        await session.commit()
        return await self.cart_payload(session, customer_id)

    async def update_cart_item(
        self,
        session: AsyncSession,
        customer_id: UUID,
        item_id: UUID,
        quantity: int,
        expected_version: int,
    ) -> dict[str, Any]:
        cart = await self._cart(session, customer_id)
        if cart is None:
            raise RuntimeError("cart creation invariant failed")
        await session.refresh(cart, with_for_update=True)
        if cart.version != expected_version:
            raise DomainError("CART_VERSION_CONFLICT", "Корзина была изменена.", 409)
        item = await session.scalar(
            select(CartItem).where(CartItem.id == item_id, CartItem.cart_id == cart.id)
        )
        if not item:
            raise not_found()
        item.quantity = quantity
        cart.version += 1
        await session.commit()
        return await self.cart_payload(session, customer_id)

    async def delete_cart_item(
        self, session: AsyncSession, customer_id: UUID, item_id: UUID, expected_version: int
    ) -> dict[str, Any]:
        cart = await self._cart(session, customer_id)
        if cart is None:
            raise RuntimeError("cart creation invariant failed")
        await session.refresh(cart, with_for_update=True)
        if cart.version != expected_version:
            raise DomainError("CART_VERSION_CONFLICT", "Корзина была изменена.", 409)
        deleted_id = await session.scalar(
            delete(CartItem)
            .where(CartItem.id == item_id, CartItem.cart_id == cart.id)
            .returning(CartItem.id)
        )
        if deleted_id is None:
            raise not_found()
        cart.version += 1
        await session.commit()
        return await self.cart_payload(session, customer_id)

    async def merge_cart(
        self, session: AsyncSession, customer_id: UUID, items: list[dict[str, Any]], key: str
    ) -> dict[str, Any]:
        payload = {"items": items}
        scope = f"customer:{customer_id}"
        replay = await self._idempotency_replay(
            session, actor_scope=scope, endpoint="mergeCart", key=key, payload=payload
        )
        if replay:
            return replay
        cart = await self._cart(session, customer_id)
        if cart is None:
            raise RuntimeError("cart creation invariant failed")
        for incoming in items:
            product = await session.get(Product, incoming["product_id"])
            if not product:
                continue
            row = await session.scalar(
                select(CartItem).where(
                    CartItem.cart_id == cart.id, CartItem.product_id == product.id
                )
            )
            if row:
                row.quantity = min(999, row.quantity + incoming["quantity"])
            else:
                session.add(
                    CartItem(
                        cart_id=cart.id,
                        product_id=product.id,
                        quantity=incoming["quantity"],
                        price_snapshot_minor=product.final_price_minor,
                    )
                )
        cart.version += 1
        await session.flush()
        response = await self.cart_payload(session, customer_id)
        await self._save_idempotency(
            session,
            actor_scope=scope,
            endpoint="mergeCart",
            key=key,
            payload=payload,
            response=response,
        )
        await session.commit()
        return response

    async def favorites(self, session: AsyncSession, customer_id: UUID) -> dict[str, Any]:
        rows = (
            await session.execute(
                select(Favorite, Product)
                .join(Product, Product.id == Favorite.product_id)
                .where(Favorite.customer_id == customer_id)
                .order_by(Favorite.created_at.desc())
            )
        ).all()
        product_payloads = {
            UUID(p["id"]): p
            for p in await self.products_to_public(session, [product for _, product in rows])
        }
        return {
            "items": [
                {
                    "id": str(favorite.id),
                    "product": product_payloads[product.id],
                    "created_at": favorite.created_at.isoformat(),
                }
                for favorite, product in rows
            ]
        }

    async def add_favorite(
        self, session: AsyncSession, customer_id: UUID, product_id: UUID
    ) -> dict[str, Any]:
        product = await session.get(Product, product_id)
        if not product:
            raise not_found()
        favorite = await session.scalar(
            select(Favorite).where(
                Favorite.customer_id == customer_id, Favorite.product_id == product_id
            )
        )
        if not favorite:
            favorite = Favorite(customer_id=customer_id, product_id=product_id)
            session.add(favorite)
            await session.commit()
        return {
            "id": str(favorite.id),
            "product": (await self.products_to_public(session, [product]))[0],
            "created_at": favorite.created_at.isoformat(),
        }

    async def delete_favorite(
        self, session: AsyncSession, customer_id: UUID, product_id: UUID
    ) -> None:
        await session.execute(
            delete(Favorite).where(
                Favorite.customer_id == customer_id, Favorite.product_id == product_id
            )
        )
        await session.commit()

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
        if not quote:
            if self.settings.fixed_delivery_price_minor is None:
                raise DomainError(
                    "DELIVERY_PRICE_NOT_CONFIGURED", "Стоимость доставки не настроена.", 503
                )
            if self.settings.robokassa_mode is Mode.DISABLED:
                raise DomainError("FEATURE_DISABLED", "Оплата отключена.", 503)
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
        delivery = 0 if quote else int(self.settings.fixed_delivery_price_minor or 0)
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
            status=OrderStatus.NEW.value if quote else OrderStatus.AWAITING_PAYMENT.value,
            payment_status=PaymentStatus.NOT_REQUIRED.value
            if quote
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
            if not quote and stock:
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
                reason_code="checkout" if not quote else "quote",
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
                    "pricing": {
                        "subtotal": money(subtotal),
                        "delivery": money(delivery),
                        "total": money(subtotal + delivery),
                    },
                    "reservation_expires_at": expires.isoformat(),
                }
            )
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

    @staticmethod
    def _validate_markdown(value: str) -> str:
        normalized = value.replace("\r\n", "\n").strip()
        if re.search(r"<\s*/?\s*[a-zA-Z!][^>]*>", normalized):
            raise DomainError("INVALID_REQUEST", "HTML в Markdown не допускается.", 422)
        return normalized

    async def _media_payload(
        self, session: AsyncSession, asset: MediaAsset, alt: str
    ) -> dict[str, Any]:
        variants = (
            await session.scalars(
                select(MediaVariant)
                .where(MediaVariant.asset_id == asset.id)
                .order_by(MediaVariant.kind, MediaVariant.format)
            )
        ).all()
        fallback = next((item for item in variants if item.kind == "detail"), None)
        return {
            "id": str(asset.id),
            "status": asset.status,
            "url": fallback.public_url if fallback else None,
            "alt": alt,
            "width": asset.width,
            "height": asset.height,
            "variants": [
                {
                    "kind": item.kind,
                    "format": item.format,
                    "url": item.public_url,
                    "width": item.width,
                    "height": item.height,
                }
                for item in variants
            ],
            "error_code": asset.error_code,
        }

    async def article_payload(self, session: AsyncSession, article: Article) -> dict[str, Any]:
        media_links = (
            await session.execute(
                select(ArticleMedia, MediaAsset)
                .join(MediaAsset, MediaAsset.id == ArticleMedia.asset_id)
                .where(ArticleMedia.article_id == article.id, MediaAsset.status == "ready")
                .order_by(ArticleMedia.role, ArticleMedia.sort_order, ArticleMedia.created_at)
            )
        ).all()
        cover_payload = None
        media = []
        for link, asset in media_links:
            payload = {"link_id": str(link.id), "role": link.role}
            payload.update(await self._media_payload(session, asset, link.alt))
            media.append(payload)
            if link.role == "article_cover" and cover_payload is None:
                cover_payload = payload
        return {
            "id": str(article.id),
            "slug": article.slug,
            "title": article.title,
            "excerpt": article.excerpt,
            "content_markdown": article.content_markdown,
            "status": article.status,
            "published_at": article.published_at.isoformat() if article.published_at else None,
            "seo": {"title": article.seo_title, "description": article.seo_description},
            "cover": cover_payload,
            "media": media,
            "version": article.version,
        }

    async def list_public_articles(
        self, session: AsyncSession, page: int, limit: int
    ) -> dict[str, Any]:
        where = [Article.status == "published", Article.published_at.is_not(None)]
        total = int(await session.scalar(select(func.count(Article.id)).where(*where)) or 0)
        rows = (
            await session.scalars(
                select(Article)
                .where(*where)
                .order_by(Article.published_at.desc(), Article.id.desc())
                .offset((page - 1) * limit)
                .limit(limit)
            )
        ).all()
        items = []
        for article in rows:
            payload = await self.article_payload(session, article)
            payload.pop("content_markdown")
            payload.pop("media")
            items.append(payload)
        return {
            "items": items,
            "page": page,
            "limit": limit,
            "total": total,
        }

    async def public_article(self, session: AsyncSession, slug: str) -> dict[str, Any]:
        article = await session.scalar(
            select(Article).where(
                Article.slug == slug,
                Article.status == "published",
                Article.published_at.is_not(None),
            )
        )
        if not article:
            raise not_found()
        return await self.article_payload(session, article)

    async def list_admin_articles(
        self, session: AsyncSession, page: int, limit: int
    ) -> dict[str, Any]:
        total = int(await session.scalar(select(func.count(Article.id))) or 0)
        rows = (
            await session.scalars(
                select(Article)
                .order_by(Article.updated_at.desc(), Article.id.desc())
                .offset((page - 1) * limit)
                .limit(limit)
            )
        ).all()
        return {
            "items": [await self.article_payload(session, article) for article in rows],
            "page": page,
            "limit": limit,
            "total": total,
        }

    async def create_article(
        self,
        session: AsyncSession,
        payload: dict[str, Any],
        actor_id: UUID,
        request_id: str,
        key: str,
    ) -> dict[str, Any]:
        replay = await self._idempotency_replay(
            session,
            actor_scope=f"staff:{actor_id}",
            endpoint="adminCreateArticle",
            key=key,
            payload=payload,
        )
        if replay:
            return replay
        content = self._validate_markdown(payload.get("content_markdown", ""))
        seo = payload.get("seo") or {}
        article = Article(
            slug=payload["slug"],
            title=payload["title"],
            excerpt=payload.get("excerpt", ""),
            content_markdown=content,
            seo_title=seo.get("title"),
            seo_description=seo.get("description"),
            author_staff_id=actor_id,
        )
        session.add(article)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise DomainError(
                "ARTICLE_SLUG_EXISTS", "Такой URL статьи уже существует.", 409
            ) from exc
        session.add(
            AuditLog(
                actor_type="staff",
                actor_id=actor_id,
                action="article.create",
                target_type="article",
                target_id=article.id,
                after={"slug": article.slug, "status": article.status},
                request_id=request_id,
            )
        )
        result = await self.article_payload(session, article)
        await self._save_idempotency(
            session,
            actor_scope=f"staff:{actor_id}",
            endpoint="adminCreateArticle",
            key=key,
            payload=payload,
            response=result,
            resource_id=article.id,
        )
        await session.commit()
        return result

    async def admin_article(self, session: AsyncSession, article_id: UUID) -> dict[str, Any]:
        article = await session.get(Article, article_id)
        if not article:
            raise not_found()
        return await self.article_payload(session, article)

    async def update_article(
        self,
        session: AsyncSession,
        article_id: UUID,
        payload: dict[str, Any],
        expected_version: int,
        actor_id: UUID,
        request_id: str,
    ) -> dict[str, Any]:
        article = await session.get(Article, article_id, with_for_update=True)
        if not article:
            raise not_found()
        if article.version != expected_version:
            raise DomainError("VERSION_CONFLICT", "Статья была изменена.", 409)
        before = {"slug": article.slug, "status": article.status, "version": article.version}
        if "content_markdown" in payload:
            article.content_markdown = self._validate_markdown(payload["content_markdown"])
        for field in ("slug", "title", "excerpt"):
            if field in payload:
                setattr(article, field, payload[field])
        if "seo" in payload:
            seo = payload["seo"] or {}
            article.seo_title = seo.get("title")
            article.seo_description = seo.get("description")
        if "status" in payload:
            article.status = payload["status"]
            article.published_at = utc_now() if article.status == "published" else None
        article.version += 1
        try:
            await session.flush()
        except IntegrityError as exc:
            raise DomainError(
                "ARTICLE_SLUG_EXISTS", "Такой URL статьи уже существует.", 409
            ) from exc
        session.add(
            AuditLog(
                actor_type="staff",
                actor_id=actor_id,
                action="article.update",
                target_type="article",
                target_id=article.id,
                before=before,
                after={"slug": article.slug, "status": article.status, "version": article.version},
                request_id=request_id,
            )
        )
        await session.commit()
        return await self.article_payload(session, article)

    def _s3_client(self) -> Any:
        if (
            not self.settings.s3_originals_bucket
            or not self.settings.s3_public_media_bucket
            or not self.settings.s3_access_key_id
            or not self.settings.s3_secret_access_key
        ):
            raise DomainError("FEATURE_DISABLED", "Хранилище изображений не настроено.", 503)
        return boto3.client(
            "s3",
            endpoint_url=self.settings.s3_endpoint_url,
            region_name=self.settings.s3_region,
            aws_access_key_id=self.settings.s3_access_key_id.get_secret_value(),
            aws_secret_access_key=self.settings.s3_secret_access_key.get_secret_value(),
        )

    async def create_media_upload_intent(
        self,
        session: AsyncSession,
        payload: dict[str, Any],
        actor_id: UUID,
        request_id: str,
        key: str,
    ) -> dict[str, Any]:
        replay = await self._idempotency_replay(
            session,
            actor_scope=f"staff:{actor_id}",
            endpoint="createMediaUploadIntent",
            key=key,
            payload=payload,
        )
        if replay:
            return replay
        client = self._s3_client()
        asset = MediaAsset(
            original_object_key="pending",
            mime_type=payload["content_type"],
            size_bytes=payload["size_bytes"],
            upload_expires_at=utc_now() + timedelta(minutes=10),
            created_by_staff_id=actor_id,
        )
        session.add(asset)
        await session.flush()
        suffix = re.sub(r"[^a-zA-Z0-9._-]", "_", payload["filename"])[-120:]
        asset.original_object_key = f"originals/{asset.id}/{suffix or 'upload'}"
        upload = client.generate_presigned_post(
            self.settings.s3_originals_bucket,
            asset.original_object_key,
            Fields={"Content-Type": asset.mime_type},
            Conditions=[
                {"Content-Type": asset.mime_type},
                ["content-length-range", 1, 20 * 1024 * 1024],
            ],
            ExpiresIn=600,
        )
        session.add(
            AuditLog(
                actor_type="staff",
                actor_id=actor_id,
                action="media.upload_intent.create",
                target_type="media_asset",
                target_id=asset.id,
                after={"content_type": asset.mime_type, "size_bytes": asset.size_bytes},
                request_id=request_id,
            )
        )
        result = {
            "asset_id": str(asset.id),
            "status": asset.status,
            "upload_url": upload["url"],
            "fields": upload["fields"],
            "expires_at": asset.upload_expires_at.isoformat() if asset.upload_expires_at else None,
        }
        await self._save_idempotency(
            session,
            actor_scope=f"staff:{actor_id}",
            endpoint="createMediaUploadIntent",
            key=key,
            payload=payload,
            response=result,
            resource_id=asset.id,
        )
        await session.commit()
        return result

    async def complete_media_upload(
        self, session: AsyncSession, asset_id: UUID, actor_id: UUID, request_id: str, key: str
    ) -> dict[str, Any]:
        idempotency_payload = {"asset_id": str(asset_id)}
        replay = await self._idempotency_replay(
            session,
            actor_scope=f"staff:{actor_id}",
            endpoint="completeMediaUpload",
            key=key,
            payload=idempotency_payload,
        )
        if replay:
            return replay
        asset = await session.get(MediaAsset, asset_id, with_for_update=True)
        if not asset:
            raise not_found()
        if asset.status == "ready":
            return await self._media_payload(session, asset, "")
        if (
            asset.status != "pending_upload"
            or not asset.upload_expires_at
            or asset.upload_expires_at < utc_now()
        ):
            raise DomainError("MEDIA_UPLOAD_INVALID", "Загрузка недоступна для завершения.", 409)
        client = self._s3_client()
        try:
            head = client.head_object(
                Bucket=self.settings.s3_originals_bucket, Key=asset.original_object_key
            )
        except Exception as exc:  # noqa: BLE001 - provider response is intentionally not exposed
            raise DomainError("MEDIA_UPLOAD_MISSING", "Файл не найден в хранилище.", 409) from exc
        size = int(head.get("ContentLength", 0))
        content_type = str(head.get("ContentType", "")).split(";", 1)[0].lower()
        if size < 1 or size > 20 * 1024 * 1024 or content_type != asset.mime_type:
            raise DomainError("MEDIA_UPLOAD_INVALID", "Файл не прошёл проверку загрузки.", 422)
        asset.size_bytes = size
        asset.status = "processing"
        session.add(
            IntegrationJob(
                provider="media",
                kind="process",
                status="queued",
                progress={"asset_id": str(asset.id)},
            )
        )
        session.add(
            AuditLog(
                actor_type="staff",
                actor_id=actor_id,
                action="media.upload.complete",
                target_type="media_asset",
                target_id=asset.id,
                after={"status": "processing"},
                request_id=request_id,
            )
        )
        result = await self._media_payload(session, asset, "")
        await self._save_idempotency(
            session,
            actor_scope=f"staff:{actor_id}",
            endpoint="completeMediaUpload",
            key=key,
            payload=idempotency_payload,
            response=result,
            resource_id=asset.id,
        )
        await session.commit()
        return result

    async def admin_media(self, session: AsyncSession, asset_id: UUID) -> dict[str, Any]:
        asset = await session.get(MediaAsset, asset_id)
        if not asset:
            raise not_found()
        return await self._media_payload(session, asset, "")

    async def attach_article_media(
        self,
        session: AsyncSession,
        article_id: UUID,
        payload: dict[str, Any],
        expected_version: int,
        actor_id: UUID,
        request_id: str,
    ) -> dict[str, Any]:
        article = await session.get(Article, article_id, with_for_update=True)
        asset = await session.get(MediaAsset, UUID(str(payload["asset_id"])))
        if not article or not asset:
            raise not_found()
        if article.version != expected_version:
            raise DomainError("VERSION_CONFLICT", "Статья была изменена.", 409)
        if asset.status != "ready":
            raise DomainError("MEDIA_NOT_READY", "Изображение ещё не опубликовано.", 409)
        if payload["role"] == "article_cover":
            await session.execute(
                update(ArticleMedia)
                .where(ArticleMedia.article_id == article.id, ArticleMedia.role == "article_cover")
                .values(role="article_inline")
            )
        link = await session.scalar(
            select(ArticleMedia).where(
                ArticleMedia.article_id == article.id, ArticleMedia.asset_id == asset.id
            )
        )
        if not link:
            link = ArticleMedia(article_id=article.id, asset_id=asset.id)
            session.add(link)
        link.role = payload["role"]
        link.alt = payload.get("alt", "")
        link.sort_order = payload.get("sort_order", 0)
        article.version += 1
        session.add(
            AuditLog(
                actor_type="staff",
                actor_id=actor_id,
                action="article.media.attach",
                target_type="article",
                target_id=article.id,
                after={"asset_id": str(asset.id), "role": link.role},
                request_id=request_id,
            )
        )
        await session.commit()
        return await self.article_payload(session, article)

    async def update_article_media(
        self,
        session: AsyncSession,
        article_id: UUID,
        link_id: UUID,
        payload: dict[str, Any],
        expected_version: int,
        actor_id: UUID,
        request_id: str,
    ) -> dict[str, Any]:
        article = await session.get(Article, article_id, with_for_update=True)
        link = await session.get(ArticleMedia, link_id)
        if not article or not link or link.article_id != article.id:
            raise not_found()
        if article.version != expected_version:
            raise DomainError("VERSION_CONFLICT", "Статья была изменена.", 409)
        if payload.get("role") == "article_cover":
            await session.execute(
                update(ArticleMedia)
                .where(ArticleMedia.article_id == article.id)
                .values(role="article_inline")
            )
        for field in ("alt", "sort_order", "role"):
            if field in payload and payload[field] is not None:
                setattr(link, field, payload[field])
        article.version += 1
        session.add(
            AuditLog(
                actor_type="staff",
                actor_id=actor_id,
                action="article.media.update",
                target_type="article",
                target_id=article.id,
                after={"link_id": str(link.id)},
                request_id=request_id,
            )
        )
        await session.commit()
        return await self.article_payload(session, article)

    async def delete_article_media(
        self,
        session: AsyncSession,
        article_id: UUID,
        link_id: UUID,
        expected_version: int,
        actor_id: UUID,
        request_id: str,
    ) -> dict[str, Any]:
        article = await session.get(Article, article_id, with_for_update=True)
        link = await session.get(ArticleMedia, link_id)
        if not article or not link or link.article_id != article.id:
            raise not_found()
        if article.version != expected_version:
            raise DomainError("VERSION_CONFLICT", "Статья была изменена.", 409)
        asset_id = link.asset_id
        await session.delete(link)
        article.version += 1
        await session.flush()
        await self._retire_unused_asset(session, asset_id)
        session.add(
            AuditLog(
                actor_type="staff",
                actor_id=actor_id,
                action="article.media.detach",
                target_type="article",
                target_id=article.id,
                after={"link_id": str(link_id)},
                request_id=request_id,
            )
        )
        await session.commit()
        return await self.article_payload(session, article)

    async def product_media_payload(
        self, session: AsyncSession, product: Product
    ) -> dict[str, Any]:
        rows = (
            await session.execute(
                select(ProductMedia, MediaAsset)
                .join(MediaAsset, MediaAsset.id == ProductMedia.asset_id)
                .where(ProductMedia.product_id == product.id)
                .order_by(ProductMedia.sort_order, ProductMedia.created_at)
            )
        ).all()
        return {
            "product_id": str(product.id),
            "version": product.version,
            "items": [
                {
                    "link_id": str(link.id),
                    "sort_order": link.sort_order,
                    "is_primary": link.is_primary,
                    **(await self._media_payload(session, asset, link.alt)),
                }
                for link, asset in rows
            ],
        }

    async def attach_product_media(
        self,
        session: AsyncSession,
        product_id: UUID,
        payload: dict[str, Any],
        expected_version: int,
        actor_id: UUID,
        request_id: str,
    ) -> dict[str, Any]:
        product = await session.get(Product, product_id, with_for_update=True)
        asset = await session.get(MediaAsset, UUID(str(payload["asset_id"])))
        if not product or not asset:
            raise not_found()
        if product.version != expected_version:
            raise DomainError("VERSION_CONFLICT", "Товар был изменён.", 409)
        if asset.status != "ready":
            raise DomainError("MEDIA_NOT_READY", "Изображение ещё не опубликовано.", 409)
        link = await session.scalar(
            select(ProductMedia).where(
                ProductMedia.product_id == product.id, ProductMedia.asset_id == asset.id
            )
        )
        if not link:
            count = int(
                await session.scalar(
                    select(func.count(ProductMedia.id)).where(ProductMedia.product_id == product.id)
                )
                or 0
            )
            if count >= 12:
                raise DomainError("MEDIA_LIMIT_EXCEEDED", "У товара может быть до 12 фото.", 409)
            link = ProductMedia(product_id=product.id, asset_id=asset.id)
            session.add(link)
        if payload.get("is_primary"):
            await session.execute(
                update(ProductMedia)
                .where(ProductMedia.product_id == product.id)
                .values(is_primary=False)
            )
        link.alt = payload.get("alt", "")
        link.sort_order = payload.get("sort_order", 0)
        link.is_primary = payload.get("is_primary", False)
        product.version += 1
        session.add(
            AuditLog(
                actor_type="staff",
                actor_id=actor_id,
                action="product.media.attach",
                target_type="product",
                target_id=product.id,
                after={"asset_id": str(asset.id), "is_primary": link.is_primary},
                request_id=request_id,
            )
        )
        await session.commit()
        return await self.product_media_payload(session, product)

    async def _retire_unused_asset(self, session: AsyncSession, asset_id: UUID) -> None:
        product_count = int(
            await session.scalar(
                select(func.count(ProductMedia.id)).where(ProductMedia.asset_id == asset_id)
            )
            or 0
        )
        article_count = int(
            await session.scalar(
                select(func.count(ArticleMedia.id)).where(ArticleMedia.asset_id == asset_id)
            )
            or 0
        )
        if product_count or article_count:
            return
        asset = await session.get(MediaAsset, asset_id, with_for_update=True)
        if asset and asset.status == "ready":
            asset.status = "deleted"
            asset.deleted_at = utc_now()
            asset.purge_after = utc_now() + timedelta(days=30)

    async def update_product_media(
        self,
        session: AsyncSession,
        product_id: UUID,
        link_id: UUID,
        payload: dict[str, Any],
        expected_version: int,
        actor_id: UUID,
        request_id: str,
    ) -> dict[str, Any]:
        product = await session.get(Product, product_id, with_for_update=True)
        link = await session.get(ProductMedia, link_id)
        if not product or not link or link.product_id != product.id:
            raise not_found()
        if product.version != expected_version:
            raise DomainError("VERSION_CONFLICT", "Товар был изменён.", 409)
        if payload.get("is_primary"):
            await session.execute(
                update(ProductMedia)
                .where(ProductMedia.product_id == product.id)
                .values(is_primary=False)
            )
        for field in ("alt", "sort_order", "is_primary"):
            if field in payload and payload[field] is not None:
                setattr(link, field, payload[field])
        product.version += 1
        session.add(
            AuditLog(
                actor_type="staff",
                actor_id=actor_id,
                action="product.media.update",
                target_type="product",
                target_id=product.id,
                after={"link_id": str(link.id)},
                request_id=request_id,
            )
        )
        await session.commit()
        return await self.product_media_payload(session, product)

    async def delete_product_media(
        self,
        session: AsyncSession,
        product_id: UUID,
        link_id: UUID,
        expected_version: int,
        actor_id: UUID,
        request_id: str,
    ) -> dict[str, Any]:
        product = await session.get(Product, product_id, with_for_update=True)
        link = await session.get(ProductMedia, link_id)
        if not product or not link or link.product_id != product.id:
            raise not_found()
        if product.version != expected_version:
            raise DomainError("VERSION_CONFLICT", "Товар был изменён.", 409)
        asset_id = link.asset_id
        await session.delete(link)
        product.version += 1
        await session.flush()
        await self._retire_unused_asset(session, asset_id)
        session.add(
            AuditLog(
                actor_type="staff",
                actor_id=actor_id,
                action="product.media.detach",
                target_type="product",
                target_id=product.id,
                after={"link_id": str(link_id)},
                request_id=request_id,
            )
        )
        await session.commit()
        return await self.product_media_payload(session, product)

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
