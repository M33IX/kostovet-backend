from __future__ import annotations

from uuid import uuid7

from kosto_vet.bootstrap.settings import Settings
from kosto_vet.infrastructure.rate_limit import scope_digest
from kosto_vet.infrastructure.security import (
    decode_access_token,
    hash_password,
    issue_access_token,
    issue_csrf,
    normalize_email,
    random_token,
    token_hash,
    verify_csrf,
    verify_password,
)


def test_password_and_normalization() -> None:
    encoded = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password("wrong", None)
    assert normalize_email("  USER@Example.COM ") == "user@example.com"


def test_refresh_token_hash_does_not_store_token() -> None:
    token = random_token(32)
    digest = token_hash(token)
    assert token not in digest
    assert len(digest) == 64


def test_rate_limit_scope_is_keyed_and_does_not_store_pii() -> None:
    settings = Settings()
    raw = "user@example.test"
    digest = scope_digest(settings, action="customer_login", scope=raw)
    assert raw not in digest
    assert len(digest) == 64
    assert digest != scope_digest(settings, action="staff_login", scope=raw)


def test_cookie_audiences_are_cryptographically_separate() -> None:
    settings = Settings()
    subject, session_id = uuid7(), uuid7()
    token = issue_access_token(
        settings,
        subject=subject,
        session_id=session_id,
        audience="customer",
    )
    claims = decode_access_token(settings, token, "customer")
    assert claims.subject == subject
    assert claims.session_id == session_id


def test_csrf_is_bound_to_session_and_audience() -> None:
    settings = Settings()
    first, second = uuid7(), uuid7()
    token = issue_csrf(settings, session_id=first, audience="customer")
    assert verify_csrf(settings, token, session_id=first, audience="customer")
    assert not verify_csrf(settings, token, session_id=second, audience="customer")
    assert not verify_csrf(settings, token, session_id=first, audience="staff")
