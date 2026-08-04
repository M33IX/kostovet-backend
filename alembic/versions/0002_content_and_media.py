"""Add CMS articles and generic media metadata.

Revision ID: 0002_content_and_media
Revises: 0001_demo_schema
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from alembic import op
from kosto_vet.infrastructure.models import (
    Base,
    MediaAsset,
    MediaVariant,
    ProductImage,
    ProductImageVariant,
    ProductMedia,
)

revision = "0002_content_and_media"
down_revision = "0001_demo_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The initial demo migration deliberately uses metadata.create_all.  Keeping this
    # migration additive makes it safe for both an existing demo database and a fresh
    # install where the current metadata has already created the tables.
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)
    session = Session(bind=op.get_bind())
    try:
        for image in session.scalars(select(ProductImage)).all():
            existing = session.scalar(
                select(ProductMedia.id).where(
                    ProductMedia.product_id == image.product_id,
                    ProductMedia.asset_id == image.id,
                )
            )
            if existing:
                continue
            asset = MediaAsset(
                id=image.id,
                status="ready" if image.status == "ready" else "failed",
                original_object_key=image.original_object_key or f"legacy/{image.id}",
                checksum=image.checksum,
                width=image.width,
                height=image.height,
                created_by_staff_id=None,
            )
            session.add(asset)
            session.flush()
            session.add(
                ProductMedia(
                    product_id=image.product_id,
                    asset_id=asset.id,
                    alt=image.alt,
                    sort_order=image.sort_order,
                    is_primary=image.is_primary,
                )
            )
            for variant in session.scalars(
                select(ProductImageVariant).where(ProductImageVariant.image_id == image.id)
            ):
                session.add(
                    MediaVariant(
                        asset_id=asset.id,
                        kind=variant.kind,
                        format=variant.format,
                        public_url=variant.public_url,
                        public_object_key=variant.public_object_key,
                        width=variant.width,
                        height=variant.height,
                        size_bytes=variant.size_bytes,
                        checksum=variant.checksum,
                    )
                )
        session.commit()
    finally:
        session.close()


def downgrade() -> None:
    for table in (
        "article_media",
        "product_media",
        "media_variants",
        "media_assets",
        "articles",
    ):
        Base.metadata.tables[table].drop(bind=op.get_bind(), checkfirst=True)
