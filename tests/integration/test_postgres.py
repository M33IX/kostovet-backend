from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.config import Config
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
    Order,
    PaymentAttempt,
    Product,
    StockItem,
    StockReservation,
    Warehouse,
)
from kosto_vet.services.application import ApplicationService

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
