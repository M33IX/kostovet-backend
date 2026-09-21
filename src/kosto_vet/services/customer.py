from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from kosto_vet.core.errors import DomainError, not_found
from kosto_vet.models import (
    Cart,
    CartItem,
    Favorite,
    Product,
)
from kosto_vet.services.shared import money


class CustomerServiceMixin:
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
