from __future__ import annotations

from sqlalchemy import CheckConstraint, Index, UniqueConstraint

from kosto_vet.infrastructure.models import Base


def test_demo_schema_contains_expected_operational_tables_only() -> None:
    tables = set(Base.metadata.tables)
    assert {
        "orders",
        "payment_attempts",
        "stock_reservations",
        "outbox_events",
        "integration_jobs",
    } <= tables
    assert {"refunds", "fiscal_receipts", "email_deliveries", "media_uploads"}.isdisjoint(tables)
    assert {
        "articles",
        "media_assets",
        "media_variants",
        "product_media",
        "article_media",
        "customer_delivery_addresses",
    } <= tables


def test_every_table_has_a_primary_key() -> None:
    assert all(table.primary_key.columns for table in Base.metadata.sorted_tables)


def test_constraints_and_partial_indexes_exist() -> None:
    constraints = [item for table in Base.metadata.tables.values() for item in table.constraints]
    indexes = [item for table in Base.metadata.tables.values() for item in table.indexes]
    assert any(isinstance(item, CheckConstraint) for item in constraints)
    assert any(isinstance(item, UniqueConstraint) for item in constraints)
    assert any(
        isinstance(item, Index) and item.dialect_options["postgresql"].get("where") is not None
        for item in indexes
    )
