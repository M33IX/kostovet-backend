from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import delete, select, text, update

from kosto_vet.bootstrap.settings import Mode, get_settings
from kosto_vet.core.time import utc_now
from kosto_vet.infrastructure.database import Database
from kosto_vet.models import (
    IdempotencyRecord,
    IntegrationJob,
    MediaAsset,
    OAuthTransaction,
    RateLimitBucket,
    StockReservation,
)


async def run_once(database: Database) -> None:
    settings = get_settings()
    async with database.sessions() as session:
        locked = await session.scalar(text("SELECT pg_try_advisory_xact_lock(4937562281)"))
        if not locked:
            return
        await session.execute(
            update(StockReservation)
            .where(StockReservation.status == "active", StockReservation.expires_at <= utc_now())
            .values(status="expired")
        )
        await session.execute(
            delete(OAuthTransaction).where(
                OAuthTransaction.expires_at < utc_now() - timedelta(hours=1)
            )
        )
        await session.execute(
            delete(IdempotencyRecord).where(IdempotencyRecord.expires_at < utc_now())
        )
        await session.execute(
            delete(RateLimitBucket).where(
                RateLimitBucket.window_started_at < utc_now() - timedelta(days=2)
            )
        )
        expired_media = (
            await session.scalars(
                select(MediaAsset.id).where(
                    MediaAsset.status == "deleted",
                    MediaAsset.purge_after.is_not(None),
                    MediaAsset.purge_after <= utc_now(),
                )
            )
        ).all()
        for asset_id in expired_media:
            active = await session.scalar(
                select(IntegrationJob.id).where(
                    IntegrationJob.provider == "media",
                    IntegrationJob.kind == "purge",
                    IntegrationJob.cursor == str(asset_id),
                    IntegrationJob.status.in_(["queued", "running"]),
                )
            )
            if not active:
                session.add(
                    IntegrationJob(
                        provider="media",
                        kind="purge",
                        status="queued",
                        cursor=str(asset_id),
                        progress={"asset_id": str(asset_id)},
                    )
                )
        if settings.moysklad_mode is not Mode.DISABLED:
            schedules = (
                ("catalog", settings.moysklad_catalog_sync_interval_seconds),
                ("stock", settings.moysklad_stock_sync_interval_seconds),
            )
            for kind, interval_seconds in schedules:
                recent = await session.scalar(
                    select(IntegrationJob.id).where(
                        IntegrationJob.provider == "moysklad",
                        IntegrationJob.kind == kind,
                        IntegrationJob.created_at
                        >= utc_now() - timedelta(seconds=interval_seconds),
                    )
                )
                if not recent:
                    session.add(
                        IntegrationJob(provider="moysklad", kind=kind, status="queued", progress={})
                    )
        await session.commit()


async def main() -> None:
    database = Database(get_settings())
    try:
        while True:
            await run_once(database)
            await asyncio.sleep(60)
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())
