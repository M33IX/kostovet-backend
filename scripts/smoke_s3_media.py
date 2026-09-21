from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from sqlalchemy import delete, select

from kosto_vet.bootstrap.scheduler import run_once as run_scheduler_once
from kosto_vet.bootstrap.settings import Settings
from kosto_vet.bootstrap.worker import _process_media
from kosto_vet.core.time import utc_now
from kosto_vet.infrastructure.database import Database
from kosto_vet.models import (
    AuditLog,
    Category,
    IdempotencyRecord,
    IntegrationAttempt,
    IntegrationJob,
    MediaAsset,
    MediaVariant,
    Product,
    ProductMedia,
    StaffUser,
)
from kosto_vet.services.application import ApplicationService


async def wait_for_asset(
    database: Database, asset_id: UUID, *, missing: bool = False, wait_seconds: float = 90
) -> MediaAsset | None:
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while asyncio.get_running_loop().time() < deadline:
        async with database.sessions() as session:
            asset = await session.get(MediaAsset, asset_id)
            if missing and asset is None:
                return None
            if not missing and asset is not None and asset.status in {"ready", "failed"}:
                return asset
        await asyncio.sleep(0.5)
    raise TimeoutError(f"media asset {asset_id} did not reach the expected state")


async def smoke(image_path: Path, *, foreground_processing: bool = False) -> dict[str, object]:
    settings = Settings()
    database = Database(settings)
    service = ApplicationService(settings)
    marker = uuid4().hex
    staff_id: UUID | None = None
    category_id: UUID | None = None
    product_id: UUID | None = None
    asset_id: UUID | None = None
    original_key: str | None = None
    public_keys: list[str] = []
    try:
        image = image_path.read_bytes()
        disabled_password_hash = f"disabled-{marker}"
        async with database.sessions() as session:
            staff = StaffUser(
                email=f"s3-smoke-{marker}@example.invalid",
                normalized_email=f"s3-smoke-{marker}@example.invalid",
                password_hash=disabled_password_hash,
                status="blocked",
                name="S3 smoke",
                role="admin",
            )
            category = Category(
                slug=f"s3-smoke-{marker}",
                path=f"s3-smoke-{marker}",
                depth=0,
                title="S3 smoke",
                description="Temporary media lifecycle category",
                is_active=False,
                is_published=False,
            )
            session.add_all([staff, category])
            await session.flush()
            product = Product(
                category_id=category.id,
                article=f"S3-SMOKE-{marker}",
                slug=f"s3-smoke-{marker}",
                name="S3 smoke",
                final_price_minor=0,
                is_active=False,
                is_published=False,
            )
            session.add(product)
            await session.commit()
            staff_id, category_id, product_id = staff.id, category.id, product.id

        async with database.sessions() as session:
            intent = await service.create_media_upload_intent(
                session,
                {
                    "filename": "s3-smoke.png",
                    "content_type": "image/png",
                    "size_bytes": len(image),
                },
                staff_id,
                f"s3-smoke-{marker}",
                f"s3-smoke-intent-{marker}",
            )
        asset_id = UUID(intent["asset_id"])
        async with database.sessions() as session:
            asset = await session.get(MediaAsset, asset_id)
            if asset is None:
                raise RuntimeError("upload intent did not create a media asset")
            original_key = asset.original_object_key

        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            upload = await client.post(
                intent["upload_url"],
                data=intent["fields"],
                files={"file": ("s3-smoke.png", image, "image/png")},
            )
            upload.raise_for_status()

        async with database.sessions() as session:
            await service.complete_media_upload(
                session,
                asset_id,
                staff_id,
                f"s3-smoke-{marker}",
                f"s3-smoke-complete-{marker}",
            )

        if foreground_processing:
            async with database.sessions() as session:
                job = await session.scalar(
                    select(IntegrationJob).where(
                        IntegrationJob.provider == "media",
                        IntegrationJob.kind == "process",
                        IntegrationJob.progress["asset_id"].astext == str(asset_id),
                    )
                )
            if job is None:
                raise RuntimeError("media processing job is missing")
            await _process_media(database, job)

        asset = await wait_for_asset(database, asset_id)
        if asset is None or asset.status != "ready":
            raise RuntimeError(
                f"media processing failed: {asset.error_code if asset else 'missing'}"
            )

        async with database.sessions() as session:
            variants = list(
                await session.scalars(
                    select(MediaVariant)
                    .where(MediaVariant.asset_id == asset_id)
                    .order_by(MediaVariant.kind)
                )
            )
            public_keys = [variant.public_object_key for variant in variants]
            product = await session.get(Product, product_id)
            if product is None:
                raise RuntimeError("temporary product is missing")
            attached = await service.attach_product_media(
                session,
                product_id,
                {
                    "asset_id": asset_id,
                    "alt": "S3 smoke",
                    "sort_order": 0,
                    "is_primary": True,
                },
                product.version,
                staff_id,
                f"s3-smoke-{marker}",
            )

        if {variant.kind for variant in variants} != {"thumb", "card", "detail", "zoom"}:
            raise RuntimeError("worker did not create the expected media variants")
        if len(attached["items"]) != 1 or not attached["items"][0]["is_primary"]:
            raise RuntimeError("media attachment was not reflected in the product payload")

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            for variant in variants:
                response = await client.get(variant.public_url)
                response.raise_for_status()
                if response.headers.get("content-type", "").split(";", 1)[0] != "image/webp":
                    raise RuntimeError(f"unexpected public MIME for {variant.kind}")

        async with database.sessions() as session:
            product = await session.get(Product, product_id)
            link = await session.scalar(
                select(ProductMedia).where(
                    ProductMedia.product_id == product_id,
                    ProductMedia.asset_id == asset_id,
                )
            )
            if product is None or link is None:
                raise RuntimeError("temporary media link is missing")
            await service.delete_product_media(
                session,
                product_id,
                link.id,
                product.version,
                staff_id,
                f"s3-smoke-{marker}",
            )

        async with database.sessions() as session:
            asset = await session.get(MediaAsset, asset_id, with_for_update=True)
            if asset is None or asset.status != "deleted":
                raise RuntimeError("detached media was not retired")
            asset.purge_after = utc_now()
            await session.commit()
        await run_scheduler_once(database)
        await wait_for_asset(database, asset_id, missing=True)

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            statuses = [(await client.get(variant.public_url)).status_code for variant in variants]
        if any(status < 400 for status in statuses):
            raise RuntimeError("a public variant still exists after purge")

        return {
            "asset_id": str(asset_id),
            "variant_kinds": [variant.kind for variant in variants],
            "public_reads": len(variants),
            "attached": True,
            "purged": True,
        }
    finally:
        client = service._s3_client()
        for key in public_keys:
            client.delete_object(Bucket=settings.s3_public_media_bucket, Key=key)
        if original_key:
            client.delete_object(Bucket=settings.s3_originals_bucket, Key=original_key)
        async with database.sessions() as session:
            if asset_id:
                job_ids = list(
                    await session.scalars(
                        select(IntegrationJob.id).where(
                            IntegrationJob.provider == "media",
                            IntegrationJob.progress["asset_id"].astext == str(asset_id),
                        )
                    )
                )
                if job_ids:
                    await session.execute(
                        delete(IntegrationAttempt).where(IntegrationAttempt.job_id.in_(job_ids))
                    )
                    await session.execute(
                        delete(IntegrationJob).where(IntegrationJob.id.in_(job_ids))
                    )
            if asset_id:
                await session.execute(delete(ProductMedia).where(ProductMedia.asset_id == asset_id))
                await session.execute(delete(MediaAsset).where(MediaAsset.id == asset_id))
            if product_id:
                await session.execute(delete(Product).where(Product.id == product_id))
            if category_id:
                await session.execute(delete(Category).where(Category.id == category_id))
            if staff_id:
                await session.execute(
                    delete(IdempotencyRecord).where(
                        IdempotencyRecord.actor_scope == f"staff:{staff_id}"
                    )
                )
                await session.execute(delete(AuditLog).where(AuditLog.actor_id == staff_id))
                await session.execute(delete(StaffUser).where(StaffUser.id == staff_id))
            await session.commit()
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Exercise the real S3 media lifecycle.")
    parser.add_argument("image", type=Path)
    parser.add_argument("--foreground-processing", action="store_true")
    args = parser.parse_args()
    print(asyncio.run(smoke(args.image, foreground_processing=args.foreground_processing)))


if __name__ == "__main__":
    main()
