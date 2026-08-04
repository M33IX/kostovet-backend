from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from kosto_vet.bootstrap.settings import Mode, Settings


def test_exact_origins_are_required() -> None:
    assert Settings(frontend_origins="https://shop.example.test").frontend_origins == [
        "https://shop.example.test"
    ]
    with pytest.raises(ValidationError):
        Settings(frontend_origins="*")
    with pytest.raises(ValidationError):
        Settings(frontend_origins="https://shop.example.test/")
    assert Settings(admin_origins="https://admin.example.test").admin_origins == [
        "https://admin.example.test"
    ]
    with pytest.raises(ValidationError):
        Settings(admin_origins="*")


def test_comma_separated_lists_load_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRONTEND_ORIGINS", "https://shop.example.test,https://www.example.test")
    monkeypatch.setenv("TRUSTED_HOSTS", "api.example.test,localhost")
    settings = Settings(_env_file=None)
    assert settings.frontend_origins == [
        "https://shop.example.test",
        "https://www.example.test",
    ]
    assert settings.trusted_hosts == ["api.example.test", "localhost"]


def test_production_requires_independent_secrets_and_https() -> None:
    with pytest.raises(ValidationError):
        Settings(app_env="production")
    valid = Settings(
        app_env="production",
        api_public_base_url="https://api.example.test",
        frontend_origins=["https://shop.example.test"],
        cookie_secure=True,
        customer_session_secret=SecretStr("a" * 40),
        staff_session_secret=SecretStr("b" * 40),
        csrf_secret=SecretStr("c" * 40),
    )
    assert valid.cookie_secure


def test_production_payments_require_fiscalization() -> None:
    with pytest.raises(ValidationError):
        Settings(robokassa_mode=Mode.PRODUCTION, fiscalization_mode=Mode.DISABLED)
    with pytest.raises(ValidationError):
        Settings(robokassa_mode=Mode.PRODUCTION, fiscalization_mode=Mode.PRODUCTION)
