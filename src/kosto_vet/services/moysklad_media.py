from __future__ import annotations

import hashlib
import re
from io import BytesIO
from typing import Any
from urllib.parse import quote

import boto3
from PIL import Image, ImageOps
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kosto_vet.bootstrap.settings import Settings
from kosto_vet.infrastructure.integrations import MoySkladAdapter
from kosto_vet.models import IntegrationJob, Product, ProductImage, ProductImageVariant

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
VARIANT_EDGES = {"thumb": 320, "card": 640, "detail": 1280}
SOURCE_PREFIX = "moysklad:"


async def enqueue_moysklad_media(session: AsyncSession) -> dict[str, object]:
    external_ids = (
        await session.scalars(select(Product.moysklad_id).where(Product.moysklad_id.is_not(None)))
    ).all()
    active_ids = set(
        (
            await session.scalars(
                select(IntegrationJob.cursor).where(
                    IntegrationJob.provider == "moysklad",
                    IntegrationJob.kind == "product_media",
                    IntegrationJob.status.in_(["queued", "running"]),
                )
            )
        ).all()
    )
    queued = 0
    for external_id in external_ids:
        if not external_id or external_id in active_ids:
            continue
        session.add(
            IntegrationJob(
                provider="moysklad",
                kind="product_media",
                status="queued",
                cursor=external_id,
                progress={},
            )
        )
        queued += 1
    await session.commit()
    return {"processed_count": len(external_ids), "queued_count": queued, "resource": "media"}


def _image_id(row: dict[str, Any]) -> str:
    value = str(row.get("id") or row.get("meta", {}).get("href", "").rstrip("/").rsplit("/", 1)[-1])
    if not re.fullmatch(r"[a-zA-Z0-9-]{1,100}", value):
        raise ValueError("MoySklad image has no safe ID")
    return value


def _variants(source: bytes) -> dict[str, bytes]:
    if not source or len(source) > MAX_IMAGE_BYTES:
        raise ValueError("MoySklad image exceeds size limit")
    with Image.open(BytesIO(source)) as probe:
        if probe.width * probe.height > MAX_IMAGE_PIXELS:
            raise ValueError("MoySklad image exceeds pixel limit")
        probe.verify()
    result: dict[str, bytes] = {}
    with Image.open(BytesIO(source)) as image:
        normalized = ImageOps.exif_transpose(image)
        if normalized.mode not in ("RGB", "RGBA"):
            normalized = normalized.convert("RGBA" if "A" in normalized.getbands() else "RGB")
        for kind, edge in VARIANT_EDGES.items():
            variant = normalized.copy()
            variant.thumbnail((edge, edge), Image.Resampling.LANCZOS)
            output = BytesIO()
            variant.save(output, format="WEBP", quality=86, method=4)
            result[kind] = output.getvalue()
    return result


async def sync_moysklad_product_media(
    session: AsyncSession, settings: Settings, external_id: str
) -> dict[str, object]:
    if not (
        settings.s3_public_media_bucket
        and settings.s3_public_base_url
        and settings.s3_access_key_id
        and settings.s3_secret_access_key
    ):
        raise ValueError("public S3 media settings are required for MoySklad images")
    product = await session.scalar(select(Product).where(Product.moysklad_id == external_id))
    if product is None:
        return {"processed_count": 0, "skipped_count": 1, "resource": "product_media"}

    adapter = MoySkladAdapter(settings)
    rows = await adapter.fetch_pages(f"entity/product/{quote(external_id, safe='')}/images")
    existing = (
        await session.scalars(
            select(ProductImage).where(
                ProductImage.product_id == product.id,
                ProductImage.source.like(f"{SOURCE_PREFIX}%"),
            )
        )
    ).all()
    by_source = {image.source: image for image in existing}
    manual_primary = await session.scalar(
        select(ProductImage.id).where(
            ProductImage.product_id == product.id,
            ProductImage.is_primary.is_(True),
            ProductImage.status == "ready",
            ProductImage.public_url.is_not(None),
            ~ProductImage.source.like(f"{SOURCE_PREFIX}%"),
        )
    )
    s3 = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key_id.get_secret_value(),
        aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
    )
    seen: set[str] = set()
    uploaded = 0
    changed = False
    for index, row in enumerate(rows[:12]):
        image_id = _image_id(row)
        source = f"{SOURCE_PREFIX}{image_id}"
        if source in seen:
            continue
        seen.add(source)
        download_href = row.get("meta", {}).get("downloadHref")
        if not isinstance(download_href, str) or not download_href:
            raise ValueError("MoySklad image has no download URL")
        body = await adapter.download_image(download_href)
        checksum = hashlib.sha256(body).hexdigest()
        image = by_source.get(source)
        if (
            image is not None
            and image.checksum == checksum
            and image.public_url
            and image.status == "ready"
        ):
            if image.sort_order != 100 + index or image.is_primary != (
                index == 0 and not manual_primary
            ):
                image.sort_order = 100 + index
                image.is_primary = index == 0 and not manual_primary
                changed = True
            if image.alt != product.name:
                image.alt = product.name
                changed = True
            continue
        variants = _variants(body)
        if image is None:
            image = ProductImage(product_id=product.id, source=source, status="ready")
            session.add(image)
            await session.flush()
        existing_variants = {
            variant.kind: variant
            for variant in (
                await session.scalars(
                    select(ProductImageVariant).where(ProductImageVariant.image_id == image.id)
                )
            ).all()
        }
        image.checksum = checksum
        image.status = "ready"
        image.alt = product.name
        image.sort_order = 100 + index
        image.is_primary = index == 0 and not manual_primary
        for kind, variant_body in variants.items():
            variant_checksum = hashlib.sha256(variant_body).hexdigest()
            key = f"moysklad/{product.id}/{image_id}/{kind}.webp"
            s3.put_object(
                Bucket=settings.s3_public_media_bucket,
                Key=key,
                Body=variant_body,
                ContentType="image/webp",
                CacheControl="public, max-age=300",
            )
            url = f"{settings.s3_public_base_url.rstrip('/')}/{key}?v={variant_checksum[:12]}"
            with Image.open(BytesIO(variant_body)) as variant:
                width, height = variant.size
            variant_record = existing_variants.get(kind)
            if variant_record is None:
                variant_record = ProductImageVariant(
                    image_id=image.id,
                    kind=kind,
                    format="webp",
                    public_url=url,
                    public_object_key=key,
                    width=width,
                    height=height,
                    size_bytes=len(variant_body),
                    checksum=variant_checksum,
                )
                session.add(variant_record)
            else:
                variant_record.format = "webp"
                variant_record.public_url = url
                variant_record.public_object_key = key
                variant_record.width = width
                variant_record.height = height
                variant_record.size_bytes = len(variant_body)
                variant_record.checksum = variant_checksum
            if kind == "detail":
                image.public_url = url
                image.original_object_key = key
        uploaded += 1
        changed = True

    removed = 0
    for image in existing:
        if image.source not in seen and image.status == "ready":
            image.status = "deleted"
            image.public_url = None
            image.is_primary = False
            removed += 1
            changed = True
    if changed:
        product.version += 1
    await session.commit()
    return {
        "processed_count": len(seen),
        "uploaded_count": uploaded,
        "removed_count": removed,
        "resource": "product_media",
        "product_id": str(product.id),
    }
