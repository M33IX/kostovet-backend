from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import text

from alembic import command
from kosto_vet.bootstrap.settings import Settings
from kosto_vet.core.errors import DomainError
from kosto_vet.infrastructure.database import Database
from kosto_vet.infrastructure.rate_limit import enforce_rate_limit

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
