from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from kosto_vet.core.identifiers import new_id

from .base import Base, UUIDTimestampMixin, VersionMixin


class Lead(Base, UUIDTimestampMixin, VersionMixin):
    __tablename__ = "leads"
    name: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text)
    company: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text)
    product_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("products.id", ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="new")
    consent_version: Mapped[str] = mapped_column(Text, nullable=False, default="demo-v1")
    __table_args__ = (Index("ix_leads_queue", "status", "created_at"),)


class StockSubscription(Base, UUIDTimestampMixin):
    __tablename__ = "stock_subscriptions"
    product_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_contact: Mapped[str] = mapped_column(Text, nullable=False)
    consent_version: Mapped[str] = mapped_column(Text, nullable=False, default="demo-v1")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")


class PublicSetting(Base, UUIDTimestampMixin):
    __tablename__ = "public_settings"
    key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class OutboxEvent(Base, UUIDTimestampMixin):
    __tablename__ = "outbox_events"
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    aggregate_version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deduplication_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    request_id: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (Index("ix_outbox_claim", "status", "available_at", "priority"),)


class IntegrationJob(Base, UUIDTimestampMixin):
    __tablename__ = "integration_jobs"
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    progress: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    cursor: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IntegrationAttempt(Base, UUIDTimestampMixin):
    __tablename__ = "integration_attempts"
    job_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("integration_jobs.id"))
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    http_class: Mapped[str | None] = mapped_column(Text)
    safe_code: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class SyncCursor(Base, UUIDTimestampMixin, VersionMixin):
    __tablename__ = "sync_cursors"
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    resource: Mapped[str] = mapped_column(Text, nullable=False)
    watermark: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("provider", "resource"),)


class IdempotencyRecord(Base, UUIDTimestampMixin):
    __tablename__ = "idempotency_records"
    actor_scope: Mapped[str] = mapped_column(Text, nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    resource_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (UniqueConstraint("actor_scope", "endpoint", "idempotency_key"),)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_id)
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    request_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LegalDocumentVersion(Base, UUIDTimestampMixin):
    __tablename__ = "legal_document_versions"
    document_type: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    public_url: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("document_type", "version"),)


class RateLimitBucket(Base):
    __tablename__ = "rate_limit_buckets"
    action: Mapped[str] = mapped_column(Text, primary_key=True)
    scope_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
