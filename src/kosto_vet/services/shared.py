from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kosto_vet.bootstrap.settings import Settings
from kosto_vet.core.errors import DomainError
from kosto_vet.core.time import utc_now
from kosto_vet.infrastructure.integrations import (
    RobokassaAdapter,
    YandexIdAdapter,
    safe_payload_hash,
)
from kosto_vet.models import (
    IdempotencyRecord,
)


@dataclass(frozen=True, slots=True)
class SessionBundle:
    access: str
    refresh: str
    session_id: UUID
    subject_id: UUID
    role: str | None = None


def money(amount: int) -> dict[str, Any]:
    return {"amount": amount, "currency": "RUB"}


def manager(settings: Settings) -> dict[str, Any]:
    return {
        "name": settings.default_manager_name,
        "phone": settings.default_manager_phone,
        "email": settings.default_manager_email,
        "scope": "global",
    }


class SharedServiceMixin:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.robokassa = RobokassaAdapter(settings)
        self.yandex = YandexIdAdapter(settings)
        fernet_key = base64.urlsafe_b64encode(
            hashlib.sha256(settings.csrf_secret.get_secret_value().encode()).digest()
        )
        self._fernet = Fernet(fernet_key)

    async def _idempotency_replay(
        self,
        session: AsyncSession,
        *,
        actor_scope: str,
        endpoint: str,
        key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        request_hash = safe_payload_hash(payload)
        record = await session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.actor_scope == actor_scope,
                IdempotencyRecord.endpoint == endpoint,
                IdempotencyRecord.idempotency_key == key,
            )
        )
        if record:
            if record.request_hash != request_hash:
                raise DomainError(
                    "IDEMPOTENCY_CONFLICT", "Ключ уже использован с другим запросом.", 409
                )
            if record.response_body is None:
                return None
            response = dict(record.response_body)
            protected_token = response.get("order_access_token")
            if isinstance(protected_token, str) and protected_token.startswith("fernet:"):
                response["order_access_token"] = self._fernet.decrypt(
                    protected_token.removeprefix("fernet:").encode()
                ).decode()
            return response
        return None

    async def _save_idempotency(
        self,
        session: AsyncSession,
        *,
        actor_scope: str,
        endpoint: str,
        key: str,
        payload: dict[str, Any],
        response: dict[str, Any],
        resource_id: UUID | None = None,
        financial: bool = False,
    ) -> None:
        stored_response = dict(response)
        order_access_token = stored_response.get("order_access_token")
        if isinstance(order_access_token, str) and order_access_token:
            stored_response["order_access_token"] = (
                "fernet:" + self._fernet.encrypt(order_access_token.encode()).decode()
            )
        session.add(
            IdempotencyRecord(
                actor_scope=actor_scope,
                endpoint=endpoint,
                idempotency_key=key,
                request_hash=safe_payload_hash(payload),
                response_status=201 if financial else 202,
                response_body=stored_response,
                resource_id=resource_id,
                expires_at=utc_now() + timedelta(days=3650 if financial else 1),
            )
        )
