from __future__ import annotations

import asyncio
import hashlib
import secrets
from datetime import timedelta
from io import BytesIO
from time import perf_counter
from uuid import UUID

import boto3
from PIL import Image, UnidentifiedImageError
from sqlalchemy import delete, or_, select

from kosto_vet.bootstrap.settings import get_settings
from kosto_vet.core.types import utc_now
from kosto_vet.infrastructure.database import Database
from kosto_vet.infrastructure.models import (
    IntegrationAttempt,
    IntegrationJob,
    MediaAsset,
    MediaVariant,
    OutboxEvent,
)
from kosto_vet.services.moysklad import sync_moysklad

LEASE_SECONDS = 300
MAX_ATTEMPTS = 8
MAX_PIXELS = 40_000_000
VARIANT_EDGES = {"thumb": 320, "card": 640, "detail": 1280, "zoom": 2048}
ALLOWED_FORMATS = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "AVIF": "image/avif",
}


def retry_delay(attempt: int) -> timedelta:
    base = min(3600, 2 ** min(attempt, 10))
    return timedelta(seconds=base + secrets.randbelow(max(2, base // 4)))


async def _claim_events(database: Database) -> list[OutboxEvent]:
    now = utc_now()
    async with database.sessions() as session:
        rows = (
            await session.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.available_at <= now,
                    or_(
                        OutboxEvent.status == "pending",
                        (OutboxEvent.status == "processing") & (OutboxEvent.leased_until < now),
                    ),
                )
                .order_by(OutboxEvent.priority.desc(), OutboxEvent.created_at)
                .with_for_update(skip_locked=True)
                .limit(50)
            )
        ).all()
        for row in rows:
            row.status = "processing"
            row.leased_until = now + timedelta(seconds=LEASE_SECONDS)
        await session.commit()
        return list(rows)


async def _finish_event(database: Database, event_id: UUID, error: Exception | None) -> None:
    async with database.sessions() as session:
        event = await session.get(OutboxEvent, event_id, with_for_update=True)
        if not event:
            return
        event.leased_until = None
        if error is None:
            event.status = "processed"
            event.processed_at = utc_now()
        else:
            event.attempts += 1
            if event.attempts >= MAX_ATTEMPTS:
                event.status = "dead_letter"
            else:
                event.status = "pending"
                event.available_at = utc_now() + retry_delay(event.attempts)
        await session.commit()


async def _dispatch_event(event: OutboxEvent) -> None:
    # Demo event consumers are intentionally side-effect free. Provider delivery ports remain disabled.
    if not event.event_type:
        raise ValueError("outbox event type is empty")


async def _claim_job(database: Database) -> IntegrationJob | None:
    now = utc_now()
    async with database.sessions() as session:
        job = await session.scalar(
            select(IntegrationJob)
            .where(
                or_(
                    (IntegrationJob.status == "queued")
                    & or_(
                        IntegrationJob.next_retry_at.is_(None),
                        IntegrationJob.next_retry_at <= now,
                    ),
                    (IntegrationJob.status == "running") & (IntegrationJob.leased_until < now),
                )
            )
            .order_by(IntegrationJob.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not job:
            return None
        job.status = "running"
        job.started_at = job.started_at or now
        job.leased_until = now + timedelta(seconds=LEASE_SECONDS)
        job.attempts += 1
        await session.commit()
        return job


async def _run_job(database: Database, job: IntegrationJob) -> dict[str, object]:
    if job.provider == "moysklad":
        async with database.sessions() as session:
            return await sync_moysklad(
                session, get_settings(), kind=job.kind, full=job.cursor == "full"
            )
    if job.provider == "media" and job.kind == "process":
        return await _process_media(database, job)
    if job.provider == "media" and job.kind == "purge":
        return await _purge_media(database, job)
    return {"processed_count": 0, "disabled_provider": job.provider}


async def _process_media(database: Database, job: IntegrationJob) -> dict[str, object]:
    raw_asset_id = job.progress.get("asset_id")
    if not isinstance(raw_asset_id, str):
        raise ValueError("media job has no asset_id")
    settings = get_settings()
    if (
        not settings.s3_originals_bucket
        or not settings.s3_public_media_bucket
        or not settings.s3_access_key_id
        or not settings.s3_secret_access_key
    ):
        raise ValueError("S3 settings are required for media processing")
    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key_id.get_secret_value(),
        aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
    )
    async with database.sessions() as session:
        asset = await session.get(MediaAsset, UUID(raw_asset_id), with_for_update=True)
        if not asset:
            raise ValueError("media asset does not exist")
        if asset.status == "ready":
            return {"asset_id": str(asset.id), "status": "ready"}
        try:
            source = client.get_object(
                Bucket=settings.s3_originals_bucket, Key=asset.original_object_key
            )["Body"].read()
            if len(source) > 20 * 1024 * 1024:
                raise ValueError("image exceeds byte limit")
            Image.MAX_IMAGE_PIXELS = MAX_PIXELS
            with Image.open(BytesIO(source)) as probe:
                source_format = probe.format or ""
                probe.verify()
            if (
                source_format not in ALLOWED_FORMATS
                or ALLOWED_FORMATS[source_format] != asset.mime_type
            ):
                raise ValueError("image signature or mime type is not allowed")
            with Image.open(BytesIO(source)) as decoded:
                decoded.load()
                if decoded.width * decoded.height > MAX_PIXELS:
                    raise ValueError("image exceeds pixel limit")
                normalized = decoded.convert("RGBA" if "A" in decoded.getbands() else "RGB")
                asset.width, asset.height = normalized.size
                asset.checksum = hashlib.sha256(source).hexdigest()
                await session.execute(delete(MediaVariant).where(MediaVariant.asset_id == asset.id))
                for kind, edge in VARIANT_EDGES.items():
                    variant = normalized.copy()
                    variant.thumbnail((edge, edge))
                    encoded = BytesIO()
                    variant.save(encoded, format="WEBP", quality=86, method=6)
                    body = encoded.getvalue()
                    checksum = hashlib.sha256(body).hexdigest()
                    key = f"media/{asset.id}/{checksum}/{kind}.webp"
                    client.put_object(
                        Bucket=settings.s3_public_media_bucket,
                        Key=key,
                        Body=body,
                        ContentType="image/webp",
                        CacheControl="public, max-age=31536000, immutable",
                    )
                    base = settings.s3_public_base_url or (
                        f"{settings.s3_endpoint_url.rstrip('/')}/{settings.s3_public_media_bucket}"
                    )
                    session.add(
                        MediaVariant(
                            asset_id=asset.id,
                            kind=kind,
                            format="webp",
                            public_url=f"{base.rstrip('/')}/{key}",
                            public_object_key=key,
                            width=variant.width,
                            height=variant.height,
                            size_bytes=len(body),
                            checksum=checksum,
                        )
                    )
            asset.status = "ready"
            asset.error_code = None
            await session.commit()
            return {"asset_id": str(asset.id), "status": "ready"}
        except (OSError, UnidentifiedImageError, ValueError) as exc:
            asset.status = "failed"
            asset.error_code = "MEDIA_VALIDATION_FAILED"
            await session.commit()
            raise ValueError("media processing failed") from exc


async def _purge_media(database: Database, job: IntegrationJob) -> dict[str, object]:
    raw_asset_id = job.progress.get("asset_id")
    if not isinstance(raw_asset_id, str):
        raise ValueError("media purge job has no asset_id")
    settings = get_settings()
    if (
        not settings.s3_originals_bucket
        or not settings.s3_public_media_bucket
        or not settings.s3_access_key_id
        or not settings.s3_secret_access_key
    ):
        raise ValueError("S3 settings are required for media purging")
    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key_id.get_secret_value(),
        aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
    )
    async with database.sessions() as session:
        asset = await session.get(MediaAsset, UUID(raw_asset_id), with_for_update=True)
        if not asset:
            return {"asset_id": raw_asset_id, "status": "already_purged"}
        if asset.status != "deleted":
            return {"asset_id": raw_asset_id, "status": "restored"}
        variants = (
            await session.scalars(select(MediaVariant).where(MediaVariant.asset_id == asset.id))
        ).all()
        for variant in variants:
            client.delete_object(
                Bucket=settings.s3_public_media_bucket, Key=variant.public_object_key
            )
        client.delete_object(Bucket=settings.s3_originals_bucket, Key=asset.original_object_key)
        await session.delete(asset)
        await session.commit()
        return {"asset_id": raw_asset_id, "status": "purged"}


async def _finish_job(
    database: Database,
    job_id: UUID,
    *,
    progress: dict[str, object] | None,
    error: Exception | None,
    duration_ms: int,
) -> None:
    async with database.sessions() as session:
        job = await session.get(IntegrationJob, job_id, with_for_update=True)
        if not job:
            return
        job.leased_until = None
        session.add(
            IntegrationAttempt(
                job_id=job.id,
                attempt_number=job.attempts,
                http_class="success" if error is None else "error",
                safe_code=None if error is None else type(error).__name__,
                duration_ms=duration_ms,
            )
        )
        if error is None:
            job.status = "succeeded"
            job.progress = progress or {}
            job.finished_at = utc_now()
            job.error_code = None
        elif job.attempts >= MAX_ATTEMPTS:
            job.status = "failed"
            job.finished_at = utc_now()
            job.error_code = type(error).__name__
        else:
            job.status = "queued"
            job.next_retry_at = utc_now() + retry_delay(job.attempts)
            job.error_code = type(error).__name__
        await session.commit()


async def run_once(database: Database) -> int:
    processed = 0
    for event in await _claim_events(database):
        error: Exception | None = None
        try:
            await _dispatch_event(event)
        except Exception as exc:  # noqa: BLE001 - failure is persisted for retry/dead-letter
            error = exc
        await _finish_event(database, event.id, error)
        processed += 1

    job = await _claim_job(database)
    if job:
        started = perf_counter()
        progress: dict[str, object] | None = None
        error = None
        try:
            progress = await _run_job(database, job)
        except Exception as exc:  # noqa: BLE001 - failure is persisted for retry/dead-letter
            error = exc
        await _finish_job(
            database,
            job.id,
            progress=progress,
            error=error,
            duration_ms=int((perf_counter() - started) * 1000),
        )
        processed += 1
    return processed


async def main() -> None:
    database = Database(get_settings())
    try:
        while True:
            processed = await run_once(database)
            await asyncio.sleep(0.25 if processed else 2.0)
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())
