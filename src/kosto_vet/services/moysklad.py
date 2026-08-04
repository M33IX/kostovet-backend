from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from kosto_vet.bootstrap.settings import Settings
from kosto_vet.core.errors import DomainError
from kosto_vet.core.types import utc_now
from kosto_vet.infrastructure.integrations import MoySkladAdapter
from kosto_vet.infrastructure.models import Product, StockItem, SyncCursor, Warehouse


def _integer_quantity(value: Any) -> int:
    try:
        quantity = Decimal(str(value))
    except InvalidOperation:
        return 0
    if not quantity.is_finite():
        return 0
    return int(quantity)


def _provider_updated_at(row: dict[str, Any]) -> datetime:
    value = row.get("updated")
    if not isinstance(value, str):
        return utc_now()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return utc_now()


def _price_minor(row: dict[str, Any], price_type_id: str) -> int | None:
    for price in row.get("salePrices", []):
        href = price.get("priceType", {}).get("meta", {}).get("href", "")
        if href.rstrip("/").endswith(f"/{price_type_id}"):
            value = price.get("value")
            return int(value) if isinstance(value, int | float) and value >= 0 else None
    return None


async def _checkpoint(session: AsyncSession, resource: str) -> None:
    cursor = await session.scalar(
        select(SyncCursor).where(SyncCursor.provider == "moysklad", SyncCursor.resource == resource)
    )
    watermark = utc_now().isoformat()
    if cursor:
        cursor.watermark = watermark
        cursor.version += 1
    else:
        session.add(SyncCursor(provider="moysklad", resource=resource, watermark=watermark))


async def _product_by_identity(
    session: AsyncSession, external_id: str, article: str
) -> Product | None:
    product = await session.scalar(select(Product).where(Product.moysklad_id == external_id))
    if product is None and article:
        product = await session.scalar(select(Product).where(Product.article == article))
    return product


async def sync_moysklad(
    session: AsyncSession, settings: Settings, *, kind: str, full: bool = False
) -> dict[str, object]:
    if not settings.moysklad_warehouse_id or not settings.moysklad_price_type_id:
        raise DomainError(
            "INTEGRATION_CONFIG_INVALID",
            "Для МойСклад обязательны warehouse и price type.",
            503,
        )
    locked = await session.scalar(text("SELECT pg_try_advisory_xact_lock(4937562282)"))
    if not locked:
        raise DomainError("INTEGRATION_BUSY", "Синхронизация МойСклад уже выполняется.", 409, True)

    adapter = MoySkladAdapter(settings)
    processed = 0
    skipped = 0
    if kind == "catalog":
        cursor = await session.scalar(
            select(SyncCursor).where(
                SyncCursor.provider == "moysklad", SyncCursor.resource == "catalog"
            )
        )
        params = None
        if cursor and cursor.watermark and not full:
            params = {"filter": f"updated>{cursor.watermark}"}
        rows = await adapter.fetch_pages("entity/product", params=params)
        for row in rows:
            external_id = str(row.get("id") or "")
            article = str(row.get("article") or row.get("code") or "")
            if not external_id:
                skipped += 1
                continue
            product = await _product_by_identity(session, external_id, article)
            if not product:
                skipped += 1
                continue
            price_minor = _price_minor(row, settings.moysklad_price_type_id)
            product.moysklad_id = external_id
            product.article = article or product.article
            product.name = str(row.get("name") or product.name)
            product.description = str(row.get("description") or product.description or "")
            if price_minor is not None:
                product.final_price_minor = price_minor
            product.updated_at = _provider_updated_at(row)
            product.version += 1
            processed += 1
        await _checkpoint(session, "catalog")
    elif kind == "stock":
        rows = await adapter.fetch_pages(
            "report/stock/all",
            params={"store.id": settings.moysklad_warehouse_id},
        )
        warehouse = await session.scalar(
            select(Warehouse).where(Warehouse.moysklad_id == settings.moysklad_warehouse_id)
        )
        if not warehouse:
            warehouse = Warehouse(
                moysklad_id=settings.moysklad_warehouse_id,
                name="МойСклад",
                timezone="Europe/Moscow",
                is_active=True,
            )
            session.add(warehouse)
            await session.flush()
        for row in rows:
            external_id = str(
                row.get("assortmentId")
                or row.get("meta", {}).get("href", "").rstrip("/").rsplit("/", 1)[-1]
            )
            article = str(row.get("article") or row.get("code") or "")
            if not external_id:
                skipped += 1
                continue
            product = await _product_by_identity(session, external_id, article)
            if not product:
                skipped += 1
                continue
            product.moysklad_id = external_id
            stock = await session.scalar(
                select(StockItem).where(
                    StockItem.warehouse_id == warehouse.id,
                    StockItem.product_id == product.id,
                )
            )
            if not stock:
                stock = StockItem(warehouse_id=warehouse.id, product_id=product.id)
                session.add(stock)
            stock.stock_quantity = _integer_quantity(row.get("stock", 0))
            stock.reserved_quantity = max(0, _integer_quantity(row.get("reserve", 0)))
            stock.available_quantity = max(0, _integer_quantity(row.get("quantity", 0)))
            stock.in_transit_quantity = max(0, _integer_quantity(row.get("inTransit", 0)))
            stock.source_updated_at = _provider_updated_at(row)
            stock.synced_at = utc_now()
            stock.is_stale = False
            stock.version += 1
            processed += 1
        await _checkpoint(session, "stock")
    else:
        raise DomainError("INTEGRATION_JOB_INVALID", "Неизвестный тип sync job.", 400)
    await session.commit()
    return {
        "processed_count": processed,
        "skipped_count": skipped,
        "resource": kind,
        "mode": "full" if full else "incremental",
    }
