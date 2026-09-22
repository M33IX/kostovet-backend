from __future__ import annotations

import os
from collections.abc import AsyncIterator
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.config import Config
from PIL import Image
from pydantic import SecretStr
from sqlalchemy import func, select, text

from alembic import command
from kosto_vet.bootstrap.settings import Settings
from kosto_vet.core.errors import DomainError
from kosto_vet.core.time import utc_now
from kosto_vet.infrastructure.database import Database
from kosto_vet.infrastructure.rate_limit import enforce_rate_limit
from kosto_vet.models import (
    Cart,
    CartItem,
    Category,
    CustomerAccount,
    IntegrationJob,
    Order,
    PaymentAttempt,
    Product,
    ProductImage,
    ProductImageVariant,
    StockItem,
    StockReservation,
    Warehouse,
)
from kosto_vet.services.application import ApplicationService
from kosto_vet.services.moysklad import sync_moysklad
from kosto_vet.services.moysklad_media import sync_moysklad_product_media

pytestmark = pytest.mark.postgres
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def migrated_database() -> None:
    if os.getenv("RUN_POSTGRES_TESTS") != "1":
        pytest.skip("set RUN_POSTGRES_TESTS=1 with an isolated PostgreSQL database")
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")


@pytest_asyncio.fixture
async def database(migrated_database: None) -> AsyncIterator[Database]:
    instance = Database(Settings(_env_file=None))
    try:
        yield instance
    finally:
        await instance.close()


async def test_migration_creates_only_demo_schema(database: Database) -> None:
    async with database.sessions() as session:
        tables = set(
            await session.scalars(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        )
    assert {"orders", "stock_items", "payment_attempts", "outbox_events"} <= tables
    assert {"refunds", "fiscal_receipts", "email_deliveries", "media_uploads"}.isdisjoint(tables)


async def test_advisory_transaction_lock_is_available(database: Database) -> None:
    async with database.sessions() as session:
        assert await session.scalar(text("SELECT pg_try_advisory_xact_lock(4937562299)")) is True
        await session.rollback()


async def test_rate_limit_bucket_persists_across_sessions(database: Database) -> None:
    settings = Settings(_env_file=None)
    scope = f"integration:{uuid4()}"
    for _ in range(2):
        async with database.sessions() as session:
            await enforce_rate_limit(
                session,
                settings,
                action="integration_test",
                scope=scope,
                limit=2,
                window_seconds=60,
            )
    with pytest.raises(DomainError) as error:
        async with database.sessions() as session:
            await enforce_rate_limit(
                session,
                settings,
                action="integration_test",
                scope=scope,
                limit=2,
                window_seconds=60,
            )
    assert error.value.code == "RATE_LIMITED"


async def test_moysklad_sync_creates_new_products_and_stock(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = uuid4().hex
    product_external_id = f"product-{marker}"
    folder_external_id = f"folder-{marker}"
    warehouse_external_id = f"warehouse-{marker}"
    price_type_id = f"price-{marker}"

    async def fetch_pages(
        _adapter: object,
        path: str,
        *,
        limit: int = 100,
        params: dict[str, str] | None = None,
    ) -> list[dict[str, object]]:
        del limit, params
        if path == "entity/productfolder":
            return [{"id": folder_external_id, "name": "Винты"}]
        if path == "entity/product":
            return [
                {
                    "id": product_external_id,
                    "article": f"AUTO-{marker}",
                    "name": "Автоматически импортированный товар",
                    "productFolder": {
                        "meta": {"href": f"https://example.test/productfolder/{folder_external_id}"}
                    },
                    "salePrices": [
                        {
                            "value": 123_400,
                            "priceType": {
                                "meta": {"href": f"https://example.test/{price_type_id}"}
                            },
                        }
                    ],
                    "archived": False,
                }
            ]
        if path == "report/stock/all":
            return [
                {
                    "assortmentId": product_external_id,
                    "article": f"AUTO-{marker}",
                    "stock": 7,
                    "reserve": 2,
                    "quantity": 5,
                    "inTransit": 1,
                }
            ]
        raise AssertionError(f"unexpected MoySklad path: {path}")

    monkeypatch.setattr(
        "kosto_vet.infrastructure.integrations.MoySkladAdapter.fetch_pages", fetch_pages
    )
    settings = Settings(
        _env_file=None,
        moysklad_mode="sandbox",
        moysklad_access_token=SecretStr("test-token"),
        moysklad_warehouse_id=warehouse_external_id,
        moysklad_price_type_id=price_type_id,
    )
    async with database.sessions() as session:
        first = await sync_moysklad(session, settings, kind="catalog", full=True)
    async with database.sessions() as session:
        second = await sync_moysklad(session, settings, kind="catalog", full=True)
    async with database.sessions() as session:
        stock_result = await sync_moysklad(session, settings, kind="stock", full=True)

    async with database.sessions() as session:
        products = (
            await session.scalars(select(Product).where(Product.moysklad_id == product_external_id))
        ).all()
        product = products[0]
        category = await session.get(Category, product.category_id)
        stock = await session.scalar(select(StockItem).where(StockItem.product_id == product.id))
        media_jobs = (
            await session.scalars(
                select(IntegrationJob).where(
                    IntegrationJob.provider == "moysklad", IntegrationJob.kind == "media"
                )
            )
        ).all()

    assert first["created_count"] == 1
    assert second["created_count"] == 0
    assert len(products) == 1
    assert product.is_active is True
    assert product.is_published is True
    assert product.final_price_minor == 123_400
    assert category is not None and category.slug == "screws"
    assert stock_result["processed_count"] == 1
    assert stock is not None and stock.available_quantity == 5
    assert media_jobs


async def test_moysklad_media_sync_uploads_once_and_hides_missing_images(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = uuid4().hex
    external_id = f"product-{marker}"
    async with database.sessions() as session:
        category = Category(
            slug=f"media-{marker}",
            path=f"media-{marker}",
            title="Media test",
            description="",
        )
        session.add(category)
        await session.flush()
        product = Product(
            category_id=category.id,
            moysklad_id=external_id,
            article=marker,
            slug=f"media-product-{marker}",
            name="Photo product",
            final_price_minor=100,
        )
        session.add(product)
        await session.commit()
        product_id = product.id

    original = BytesIO()
    Image.new("RGB", (800, 600), "green").save(original, format="PNG")
    provider_rows: list[dict[str, object]] = [
        {
            "id": "image-1",
            "meta": {"downloadHref": "https://storage.example.test/image-1"},
        }
    ]
    uploads: list[dict[str, object]] = []

    async def fetch_pages(
        _adapter: object, path: str, **_kwargs: object
    ) -> list[dict[str, object]]:
        assert path == f"entity/product/{external_id}/images"
        return provider_rows

    async def download_image(_adapter: object, _url: str) -> bytes:
        return original.getvalue()

    class FakeS3:
        def put_object(self, **kwargs: object) -> None:
            uploads.append(kwargs)

    monkeypatch.setattr(
        "kosto_vet.infrastructure.integrations.MoySkladAdapter.fetch_pages", fetch_pages
    )
    monkeypatch.setattr(
        "kosto_vet.infrastructure.integrations.MoySkladAdapter.download_image", download_image
    )
    monkeypatch.setattr(
        "kosto_vet.services.moysklad_media.boto3.client", lambda *_args, **_kwargs: FakeS3()
    )
    settings = Settings(
        _env_file=None,
        moysklad_mode="sandbox",
        moysklad_access_token=SecretStr("test-token"),
        moysklad_warehouse_id="warehouse",
        moysklad_price_type_id="price",
        s3_access_key_id=SecretStr("access"),
        s3_secret_access_key=SecretStr("secret"),
        s3_public_media_bucket="public",
        s3_public_base_url="https://cdn.example.test",
    )
    async with database.sessions() as session:
        first = await sync_moysklad_product_media(session, settings, external_id)
    async with database.sessions() as session:
        second = await sync_moysklad_product_media(session, settings, external_id)
    assert first["uploaded_count"] == 1
    assert second["uploaded_count"] == 0
    assert len(uploads) == 3
    async with database.sessions() as session:
        imported = await session.scalar(
            select(ProductImage).where(ProductImage.product_id == product_id)
        )
        assert imported is not None
        assert imported.public_url is not None
        assert imported.is_primary is True
        assert imported.status == "ready"
        first_url = imported.public_url
        variants = (
            await session.scalars(
                select(ProductImageVariant).where(ProductImageVariant.image_id == imported.id)
            )
        ).all()
        assert {variant.kind for variant in variants} == {"thumb", "card", "detail"}
        session.add(
            ProductImage(
                product_id=product_id,
                source="manager",
                status="ready",
                public_url="https://cdn.example.test/manual.webp",
                is_primary=True,
            )
        )
        await session.commit()

    replacement = BytesIO()
    Image.new("RGB", (800, 600), "red").save(replacement, format="PNG")
    original = replacement
    async with database.sessions() as session:
        updated = await sync_moysklad_product_media(session, settings, external_id)
    assert updated["uploaded_count"] == 1
    assert len(uploads) == 6
    assert len({upload["Key"] for upload in uploads}) == 3
    async with database.sessions() as session:
        refreshed = await session.scalar(
            select(ProductImage).where(
                ProductImage.product_id == product_id,
                ProductImage.source == "moysklad:image-1",
            )
        )
        assert refreshed is not None
        assert refreshed.public_url != first_url
        refreshed_variants = (
            await session.scalars(
                select(ProductImageVariant).where(ProductImageVariant.image_id == refreshed.id)
            )
        ).all()
        assert len(refreshed_variants) == 3

    provider_rows.clear()
    async with database.sessions() as session:
        missing = await sync_moysklad_product_media(session, settings, external_id)
    assert missing["removed_count"] == 1
    async with database.sessions() as session:
        images = (
            await session.scalars(select(ProductImage).where(ProductImage.product_id == product_id))
        ).all()
        hidden = next(image for image in images if image.source.startswith("moysklad:"))
        manual = next(image for image in images if image.source == "manager")
        assert hidden.status == "deleted"
        assert hidden.public_url is None
        assert manual.status == "ready"
        assert manual.public_url == "https://cdn.example.test/manual.webp"


async def test_quote_and_manager_confirmed_checkout_accept_json_product_ids(
    database: Database,
) -> None:
    marker = uuid4().hex
    async with database.sessions() as session:
        category = Category(
            slug=f"integration-{marker}",
            path=f"integration-{marker}",
            depth=0,
            title="Integration",
            description="",
            is_active=True,
            is_published=True,
        )
        session.add(category)
        await session.flush()
        product = Product(
            category_id=category.id,
            article=f"TEST-{marker}",
            slug=f"integration-product-{marker}",
            name="Integration product",
            final_price_minor=12_345,
            is_active=True,
            is_published=True,
        )
        warehouse = Warehouse(
            moysklad_id=f"integration-{marker}",
            name="Integration warehouse",
            is_active=True,
        )
        session.add_all([product, warehouse])
        await session.flush()
        customer = CustomerAccount(
            email=f"integration-{marker}@example.test",
            normalized_email=f"integration-{marker}@example.test",
            phone="+79990000000",
            normalized_phone="+79990000000",
            name="Integration customer",
        )
        session.add(customer)
        await session.flush()
        cart = Cart(customer_id=customer.id, status="active")
        session.add(cart)
        await session.flush()
        session.add(
            CartItem(
                cart_id=cart.id,
                product_id=product.id,
                quantity=2,
                price_snapshot_minor=product.final_price_minor,
            )
        )
        session.add(
            StockItem(
                warehouse_id=warehouse.id,
                product_id=product.id,
                stock_quantity=10,
                available_quantity=10,
                synced_at=utc_now(),
                source_updated_at=utc_now(),
                is_stale=False,
            )
        )
        await session.commit()
        product_id = str(product.id)
        customer_id = customer.id

    service = ApplicationService(
        Settings(
            _env_file=None,
            fixed_delivery_price_minor=None,
            robokassa_mode="disabled",
        )
    )
    quote_payload = {
        "customer": {
            "name": "Integration customer",
            "phone": "+79990000000",
            "email": "integration@example.test",
        },
        "legal_entity": {
            "company_name": "Integration LLC",
            "inn": "1234567890",
            "documents_email": "docs@example.test",
        },
        "delivery": None,
        "items": [{"product_id": product_id, "quantity": 2}],
        "comment": None,
        "consent": True,
        "website": "",
    }
    async with database.sessions() as session:
        quote = await service.create_order(
            session,
            payload=quote_payload,
            key=f"quote-{marker}",
            customer_id=customer_id,
            quote=True,
            request_id=f"quote-{marker}",
        )
    assert quote["status"] == "new"
    async with database.sessions() as session:
        quote_order = await session.scalar(
            select(Order).where(Order.public_id == quote["public_id"])
        )
        remaining_cart_items = await session.scalar(
            select(func.count())
            .select_from(CartItem)
            .join(Cart, Cart.id == CartItem.cart_id)
            .where(Cart.customer_id == customer_id)
        )
    assert quote_order.customer_id == customer_id
    assert remaining_cart_items == 0

    checkout_payload = {
        "customer": quote_payload["customer"],
        "delivery": {
            "destination": "voronezh",
            "city": "Воронеж",
            "address_line": "ул. Тестовая, 1",
            "postal_code": None,
            "comment": None,
        },
        "items": quote_payload["items"],
        "payment_method": "sbp",
        "comment": None,
        "consent": True,
        "website": "",
    }
    checkout_key = f"checkout-{marker}"
    async with database.sessions() as session:
        checkout = await service.create_order(
            session,
            payload=checkout_payload,
            key=checkout_key,
            customer_id=None,
            quote=False,
            request_id=checkout_key,
        )
    assert checkout["status"] == "new"
    assert checkout["manager_confirmation_required"] is True
    assert checkout["delivery_price_pending"] is True
    assert checkout["pricing"] == {
        "subtotal": {"amount": 24_690, "currency": "RUB"},
        "delivery": {"amount": 0, "currency": "RUB"},
        "total": {"amount": 24_690, "currency": "RUB"},
    }
    assert "payment" not in checkout
    assert "reservation_expires_at" not in checkout

    async with database.sessions() as session:
        replay = await service.create_order(
            session,
            payload=checkout_payload,
            key=checkout_key,
            customer_id=None,
            quote=False,
            request_id=f"{checkout_key}-replay",
        )
        manual_order = await session.scalar(
            select(Order).where(Order.public_id == checkout["public_id"])
        )
        payment_count = await session.scalar(
            select(func.count())
            .select_from(PaymentAttempt)
            .where(PaymentAttempt.order_id == manual_order.id)
        )
        reservation_count = await session.scalar(
            select(func.count())
            .select_from(StockReservation)
            .where(StockReservation.order_id == manual_order.id)
        )
    assert replay == checkout
    assert manual_order.payment_status == "not_required"
    assert payment_count == 0
    assert reservation_count == 0
