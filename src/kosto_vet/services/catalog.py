from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from kosto_vet.core.errors import not_found
from kosto_vet.core.inventory import stock_state
from kosto_vet.core.time import utc_now
from kosto_vet.models import (
    Category,
    CustomerConsent,
    Lead,
    MediaAsset,
    Product,
    ProductImage,
    ProductImageVariant,
    ProductMedia,
    ProductSpec,
    RelatedProduct,
    StockItem,
    StockSubscription,
)
from kosto_vet.services.shared import manager, money


class CatalogServiceMixin:
    def public_settings(self) -> dict[str, Any]:
        delivery_price_pending = self.settings.fixed_delivery_price_minor is None
        return {
            "fixed_delivery_price": money(self.settings.fixed_delivery_price_minor or 0),
            "delivery_price_pending": delivery_price_pending,
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
