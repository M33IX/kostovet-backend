from __future__ import annotations

from datetime import datetime
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
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UUIDTimestampMixin, VersionMixin


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
