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
