from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from kosto_vet.bootstrap.settings import Settings
from kosto_vet.core.time import utc_now

_password_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
_dummy_hash = _password_hasher.hash("not-a-real-password-used-for-timing-only")


def normalize_email(value: str) -> str:
    return value.strip().casefold()


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password: str, encoded: str | None) -> bool:
    candidate = encoded or _dummy_hash
    try:
        valid = _password_hasher.verify(candidate, password)
    except VerifyMismatchError, InvalidHashError:
        return False
    return bool(valid and encoded)


def random_token(bytes_count: int = 32) -> str:
    return secrets.token_urlsafe(bytes_count)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class AccessClaims:
    subject: UUID
    session_id: UUID
    audience: str
    role: str | None = None


def issue_access_token(
    settings: Settings,
    *,
    subject: UUID,
    session_id: UUID,
    audience: str,
    role: str | None = None,
) -> str:
    now = utc_now()
    secret = (
        settings.customer_session_secret
        if audience == "customer"
        else settings.staff_session_secret
    ).get_secret_value()
    payload: dict[str, Any] = {
        "sub": str(subject),
        "sid": str(session_id),
        "aud": audience,
        "iss": "kosto-vet",
        "iat": now,
        "exp": now + timedelta(seconds=settings.access_ttl_seconds),
    }
    if role:
        payload["role"] = role
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_access_token(settings: Settings, token: str, audience: str) -> AccessClaims:
    secret = (
        settings.customer_session_secret
        if audience == "customer"
        else settings.staff_session_secret
    ).get_secret_value()
    payload = jwt.decode(
        token,
        secret,
        algorithms=["HS256"],
        audience=audience,
        issuer="kosto-vet",
        options={"require": ["sub", "sid", "aud", "iss", "iat", "exp"]},
    )
    return AccessClaims(
        subject=UUID(payload["sub"]),
        session_id=UUID(payload["sid"]),
        audience=audience,
        role=payload.get("role"),
    )


def issue_csrf(settings: Settings, *, session_id: UUID, audience: str) -> str:
    nonce = random_token(16)
    message = f"{audience}:{session_id}:{nonce}"
    signature = hmac.new(
        settings.csrf_secret.get_secret_value().encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{nonce}.{signature}"


def verify_csrf(
    settings: Settings,
    token: str,
    *,
    session_id: UUID,
    audience: str,
) -> bool:
    try:
        nonce, received = token.split(".", 1)
    except ValueError:
        return False
    message = f"{audience}:{session_id}:{nonce}"
    expected = hmac.new(
        settings.csrf_secret.get_secret_value().encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, received)
