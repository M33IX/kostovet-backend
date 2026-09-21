from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from kosto_vet.bootstrap.settings import Settings
from kosto_vet.core.errors import DomainError
from kosto_vet.core.time import utc_now
from kosto_vet.models import RateLimitBucket


def scope_digest(settings: Settings, *, action: str, scope: str) -> str:
    """Return a keyed, non-reversible identifier; raw IP/login never reaches the table."""
    return hmac.new(
        settings.csrf_secret.get_secret_value().encode(),
        f"rate-limit:{action}:{scope}".encode(),
        hashlib.sha256,
    ).hexdigest()


async def enforce_rate_limit(
    session: AsyncSession,
    settings: Settings,
    *,
    action: str,
    scope: str,
    limit: int,
    window_seconds: int,
    block_seconds: int | None = None,
) -> None:
    now = utc_now()
    window_epoch = int(now.timestamp()) // window_seconds * window_seconds
    window_started_at = datetime.fromtimestamp(window_epoch, UTC)
    digest = scope_digest(settings, action=action, scope=scope)
    statement = (
        insert(RateLimitBucket)
        .values(
            action=action,
            scope_hash=digest,
            window_started_at=window_started_at,
            count=1,
        )
        .on_conflict_do_update(
            index_elements=[
                RateLimitBucket.action,
                RateLimitBucket.scope_hash,
                RateLimitBucket.window_started_at,
            ],
            set_={"count": RateLimitBucket.count + 1},
        )
        .returning(RateLimitBucket.count, RateLimitBucket.blocked_until)
    )
    count, blocked_until = (await session.execute(statement)).one()
    retry_at = window_started_at + timedelta(seconds=window_seconds)
    if blocked_until and blocked_until > now:
        retry_at = blocked_until
    elif count > limit:
        retry_at = now + timedelta(seconds=block_seconds or window_seconds)
        await session.execute(
            update(RateLimitBucket)
            .where(
                RateLimitBucket.action == action,
                RateLimitBucket.scope_hash == digest,
                RateLimitBucket.window_started_at == window_started_at,
            )
            .values(blocked_until=retry_at)
        )
    await session.commit()
    if count > limit or (blocked_until and blocked_until > now):
        retry_after = max(1, int((retry_at - now).total_seconds()))
        raise DomainError(
            "RATE_LIMITED",
            "Слишком много запросов. Повторите позже.",
            429,
            retryable=True,
            meta={"retry_after_seconds": retry_after},
        )
