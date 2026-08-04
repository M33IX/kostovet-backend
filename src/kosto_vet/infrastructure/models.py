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
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from kosto_vet.core.types import new_id


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB}


class UUIDTimestampMixin:
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class VersionMixin:
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


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


class Category(Base, UUIDTimestampMixin, VersionMixin):
    __tablename__ = "categories"
    parent_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("categories.id", ondelete="RESTRICT")
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    seo_title: Mapped[str | None] = mapped_column(Text)
    seo_description: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint("depth >= 0"),
        Index("ix_categories_parent_sort", "parent_id", "sort_order", "title"),
    )


class Product(Base, UUIDTimestampMixin, VersionMixin):
    __tablename__ = "products"
    category_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False
    )
    moysklad_id: Mapped[str | None] = mapped_column(Text, unique=True)
    article: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    subtitle: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    manufacturer: Mapped[str | None] = mapped_column(Text)
    material: Mapped[str | None] = mapped_column(Text)
    size: Mapped[str | None] = mapped_column(Text)
    fixation_type: Mapped[str | None] = mapped_column(Text)
    final_price_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="RUB")
    unit: Mapped[str] = mapped_column(Text, nullable=False, default="pcs")
    expected_restock_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    seo_title: Mapped[str | None] = mapped_column(Text)
    seo_description: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint("final_price_minor >= 0"),
        CheckConstraint("currency = 'RUB'"),
        Index("ix_products_catalog", "category_id", "is_published", "is_active", "name"),
    )


class ProductSpec(Base, UUIDTimestampMixin):
    __tablename__ = "product_specs"
    product_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"))
    key: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (UniqueConstraint("product_id", "key"),)


class RelatedProduct(Base):
    __tablename__ = "related_products"
    product_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("products.id"), primary_key=True
    )
    related_product_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("products.id"), primary_key=True
    )
    relation_type: Mapped[str] = mapped_column(Text, default="similar")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (CheckConstraint("product_id <> related_product_id"),)


class ProductImage(Base, UUIDTimestampMixin):
    __tablename__ = "product_images"
    product_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"))
    original_object_key: Mapped[str | None] = mapped_column(Text)
    public_url: Mapped[str | None] = mapped_column(Text)
    alt: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(Text, default="import")
    status: Mapped[str] = mapped_column(Text, default="ready")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    checksum: Mapped[str | None] = mapped_column(Text)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)


class ProductImageVariant(Base, UUIDTimestampMixin):
    __tablename__ = "product_image_variants"
    image_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("product_images.id"))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    format: Mapped[str] = mapped_column(Text, nullable=False)
    public_url: Mapped[str] = mapped_column(Text, nullable=False)
    public_object_key: Mapped[str] = mapped_column(Text, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (UniqueConstraint("image_id", "kind", "format"),)


class Article(Base, UUIDTimestampMixin, VersionMixin):
    __tablename__ = "articles"
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_markdown: Mapped[str] = mapped_column(Text, nullable=False, default="")
    seo_title: Mapped[str | None] = mapped_column(Text)
    seo_description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    author_staff_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("staff_users.id", ondelete="RESTRICT")
    )
    __table_args__ = (
        CheckConstraint("status in ('draft','published','archived')"),
        Index("ix_articles_public", "status", "published_at"),
    )


class MediaAsset(Base, UUIDTimestampMixin):
    __tablename__ = "media_assets"
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending_upload")
    original_object_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    mime_type: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    checksum: Mapped[str | None] = mapped_column(String(64))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(Text)
    upload_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_staff_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("staff_users.id", ondelete="RESTRICT"), nullable=False
    )
    __table_args__ = (
        CheckConstraint(
            "status in ('pending_upload','uploaded','processing','ready','failed','deleted')"
        ),
        Index("ix_media_assets_processing", "status", "created_at"),
    )


class MediaVariant(Base, UUIDTimestampMixin):
    __tablename__ = "media_variants"
    asset_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("media_assets.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    format: Mapped[str] = mapped_column(Text, nullable=False)
    public_url: Mapped[str] = mapped_column(Text, nullable=False)
    public_object_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (UniqueConstraint("asset_id", "kind", "format"),)


class ProductMedia(Base, UUIDTimestampMixin):
    __tablename__ = "product_media"
    product_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    asset_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("media_assets.id", ondelete="RESTRICT"), nullable=False
    )
    alt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (
        UniqueConstraint("product_id", "asset_id"),
        Index("ix_product_media_sort", "product_id", "sort_order"),
    )


class ArticleMedia(Base, UUIDTimestampMixin):
    __tablename__ = "article_media"
    article_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("articles.id", ondelete="CASCADE"), nullable=False
    )
    asset_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("media_assets.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False, default="article_inline")
    alt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    __table_args__ = (
        CheckConstraint("role in ('article_cover','article_inline')"),
        UniqueConstraint("article_id", "asset_id"),
        Index("ix_article_media_sort", "article_id", "role", "sort_order"),
    )


class Warehouse(Base, UUIDTimestampMixin):
    __tablename__ = "warehouses"
    moysklad_id: Mapped[str | None] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, default="Europe/Moscow")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class StockItem(Base, UUIDTimestampMixin, VersionMixin):
    __tablename__ = "stock_items"
    warehouse_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("warehouses.id"))
    product_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"))
    stock_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    in_transit_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sync_job_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    is_stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (
        UniqueConstraint("warehouse_id", "product_id"),
        CheckConstraint("available_quantity >= 0"),
        Index("ix_stock_health", "warehouse_id", "is_stale", "synced_at"),
    )


class StockReservation(Base, UUIDTimestampMixin):
    __tablename__ = "stock_reservations"
    product_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("products.id"))
    warehouse_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("warehouses.id"))
    order_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("orders.id"))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("quantity > 0"),
        Index("ix_reservations_active", "product_id", "warehouse_id", "status", "expires_at"),
    )


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
