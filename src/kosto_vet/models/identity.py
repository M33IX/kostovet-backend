from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
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


class CustomerAccount(Base, UUIDTimestampMixin, VersionMixin):
    __tablename__ = "customer_accounts"
    email: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    phone: Mapped[str | None] = mapped_column(Text)
    normalized_phone: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    customer_type: Mapped[str] = mapped_column(Text, nullable=False, default="individual")
    company_name: Mapped[str | None] = mapped_column(Text)
    inn: Mapped[str | None] = mapped_column(Text)
    documents_email: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    is_email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    email_verified_source: Mapped[str | None] = mapped_column(Text)
    is_phone_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("customer_type in ('individual','legal_entity')"),
        CheckConstraint("status in ('active','blocked','deleted')"),
    )


class CustomerCredential(Base, UUIDTimestampMixin):
    __tablename__ = "customer_credentials"
    customer_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("customer_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="password")
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    disabled_reason: Mapped[str | None] = mapped_column(Text)
    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (UniqueConstraint("customer_id", "kind"),)


class CustomerOAuthAccount(Base, UUIDTimestampMixin):
    __tablename__ = "customer_oauth_accounts"
    customer_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("customer_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    provider_subject: Mapped[str] = mapped_column(Text, nullable=False)
    provider_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    provider_email: Mapped[str | None] = mapped_column(Text)
    provider_login: Mapped[str | None] = mapped_column(Text)
    profile_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    __table_args__ = (UniqueConstraint("provider", "provider_subject"),)


class CustomerSession(Base, UUIDTimestampMixin):
    __tablename__ = "customer_sessions"
    customer_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("customer_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    family_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[str | None] = mapped_column(Text)
    user_agent_hash: Mapped[str | None] = mapped_column(String(64))
    ip_signal_hash: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        CheckConstraint("expires_at > created_at"),
        Index("ix_customer_sessions_active", "customer_id", "revoked_at", "expires_at"),
        Index("ix_customer_sessions_family", "family_id", "revoked_at"),
    )


class OAuthTransaction(Base, UUIDTimestampMixin):
    __tablename__ = "oauth_transactions"
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    verifier_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    return_path: Mapped[str] = mapped_column(Text, nullable=False, default="/account")
    purpose: Mapped[str] = mapped_column(Text, nullable=False, default="login")
    customer_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("customer_accounts.id", ondelete="CASCADE")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("expires_at > created_at"),)


class CustomerConsent(Base, UUIDTimestampMixin):
    __tablename__ = "customer_consents"
    customer_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("customer_accounts.id", ondelete="SET NULL")
    )
    subject_ref: Mapped[str] = mapped_column(Text, nullable=False)
    consent_type: Mapped[str] = mapped_column(Text, nullable=False)
    document_version: Mapped[str] = mapped_column(Text, nullable=False)
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    request_id: Mapped[str] = mapped_column(Text, nullable=False)


class CustomerDeliveryAddress(Base, UUIDTimestampMixin, VersionMixin):
    __tablename__ = "customer_delivery_addresses"
    customer_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("customer_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(Text, nullable=False, default="Основной")
    destination: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str] = mapped_column(Text, nullable=False)
    address_line: Mapped[str] = mapped_column(Text, nullable=False)
    postal_code: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (
        CheckConstraint("destination in ('voronezh','intercity')"),
        Index("ix_customer_delivery_addresses_customer", "customer_id", "is_default", "updated_at"),
        Index(
            "uq_customer_delivery_addresses_default",
            "customer_id",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )


class StaffUser(Base, UUIDTimestampMixin):
    __tablename__ = "staff_users"
    email: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, default="readonly")
    __table_args__ = (
        CheckConstraint("role in ('admin','manager','content','readonly')"),
        CheckConstraint("status in ('active','blocked')"),
    )


class StaffSession(Base, UUIDTimestampMixin):
    __tablename__ = "staff_sessions"
    staff_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("staff_users.id"))
    family_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (CheckConstraint("expires_at > created_at"),)
