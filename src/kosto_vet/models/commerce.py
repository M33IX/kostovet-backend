from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UUIDTimestampMixin, VersionMixin


class Cart(Base, UUIDTimestampMixin, VersionMixin):
    __tablename__ = "carts"
    customer_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("customer_accounts.id")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    __table_args__ = (
        Index(
            "uq_carts_active_customer",
            "customer_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )


class CartItem(Base, UUIDTimestampMixin):
    __tablename__ = "cart_items"
    cart_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("carts.id", ondelete="CASCADE")
    )
    product_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price_snapshot_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    __table_args__ = (
        UniqueConstraint("cart_id", "product_id"),
        CheckConstraint("quantity > 0"),
    )


class Favorite(Base, UUIDTimestampMixin):
    __tablename__ = "favorites"
    customer_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("customer_accounts.id")
    )
    product_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"))
    __table_args__ = (UniqueConstraint("customer_id", "product_id"),)


class Order(Base, UUIDTimestampMixin, VersionMixin):
    __tablename__ = "orders"
    public_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    order_access_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    customer_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("customer_accounts.id")
    )
    customer_type: Mapped[str] = mapped_column(Text, nullable=False)
    contact_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    legal_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    delivery_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="web")
    comment: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="RUB")
    subtotal_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    delivery_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    discount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    payment_status: Mapped[str] = mapped_column(Text, nullable=False)
    integration_status: Mapped[str] = mapped_column(Text, nullable=False, default="healthy")
    manager_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_tracking_number: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint("currency = 'RUB'"),
        CheckConstraint("subtotal_minor >= 0 and delivery_minor >= 0 and discount_minor >= 0"),
        CheckConstraint("total_minor = subtotal_minor + delivery_minor - discount_minor"),
        Index("ix_orders_customer_history", "customer_id", "created_at"),
        Index("ix_orders_admin_queue", "status", "created_at"),
    )


class OrderItem(Base, UUIDTimestampMixin):
    __tablename__ = "order_items"
    order_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("orders.id", ondelete="RESTRICT")
    )
    product_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT")
    )
    article: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    specs_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    line_total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="RUB")
    __table_args__ = (
        CheckConstraint("quantity > 0"),
        CheckConstraint("line_total_minor = unit_price_minor * quantity"),
    )


class OrderStatusHistory(Base, UUIDTimestampMixin):
    __tablename__ = "order_status_history"
    order_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("orders.id", ondelete="RESTRICT")
    )
    from_status: Mapped[str | None] = mapped_column(Text)
    to_status: Mapped[str] = mapped_column(Text, nullable=False)
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    reason_code: Mapped[str] = mapped_column(Text, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str] = mapped_column(Text, nullable=False)


class PaymentAttempt(Base, UUIDTimestampMixin):
    __tablename__ = "payment_attempts"
    order_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("orders.id", ondelete="RESTRICT")
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False, default="robokassa")
    inv_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    requested_method: Mapped[str] = mapped_column(Text, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="RUB")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="created")
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    signed_payload_hash: Mapped[str | None] = mapped_column(String(64))
    provider_operation_reference: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    callback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciliation_status: Mapped[str] = mapped_column(Text, nullable=False, default="not_due")
    __table_args__ = (
        CheckConstraint("amount_minor >= 0"),
        Index(
            "uq_payment_active_order",
            "order_id",
            unique=True,
            postgresql_where=text("status in ('created','redirect_ready','pending')"),
        ),
    )


class PaymentCallback(Base, UUIDTimestampMixin):
    __tablename__ = "payment_callbacks"
    payment_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("payment_attempts.id")
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    processing_result: Mapped[str] = mapped_column(Text, nullable=False)
    safe_error_code: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
