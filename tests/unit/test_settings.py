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
        fixed_delivery_price_minor=50000,
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


def test_enabled_moysklad_requires_complete_read_only_sync_configuration() -> None:
    with pytest.raises(ValidationError, match="enabled MoySklad"):
        Settings(moysklad_mode=Mode.SANDBOX)
    settings = Settings(
        moysklad_mode=Mode.SANDBOX,
        moysklad_access_token=SecretStr("m" * 32),
        moysklad_warehouse_id="warehouse-id",
        moysklad_price_type_id="price-type-id",
    )
    assert settings.moysklad_catalog_sync_interval_seconds == 900


def test_production_requires_delivery_price_and_complete_s3_when_configured() -> None:
    common = {
        "app_env": "production",
        "api_public_base_url": "https://api.example.test",
        "frontend_origins": ["https://shop.example.test"],
        "cookie_secure": True,
        "customer_session_secret": SecretStr("a" * 40),
        "staff_session_secret": SecretStr("b" * 40),
        "csrf_secret": SecretStr("c" * 40),
    }
    with pytest.raises(ValidationError):
        Settings(**common)
    with pytest.raises(ValidationError):
        Settings(**common, fixed_delivery_price_minor=50000, s3_originals_bucket="originals")
    with pytest.raises(ValidationError, match="exact admin origin"):
        Settings(
            **common,
            fixed_delivery_price_minor=50000,
            s3_access_key_id=SecretStr("a" * 32),
            s3_secret_access_key=SecretStr("s" * 32),
            s3_originals_bucket="originals",
            s3_public_media_bucket="public",
            s3_public_base_url="https://media.example.test",
        )
    assert Settings(**common, fixed_delivery_price_minor=50000).fixed_delivery_price_minor == 50000
