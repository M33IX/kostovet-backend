from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Mode(StrEnum):
    DISABLED = "disabled"
    SANDBOX = "sandbox"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "development"
    app_version: str = "dev"
    api_public_base_url: str = "http://localhost:8000"
    frontend_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]
    admin_origins: Annotated[list[str], NoDecode] = []
    trusted_hosts: Annotated[list[str], NoDecode] = ["localhost", "127.0.0.1", "testserver"]
    internal_allowed_cidrs: Annotated[list[str], NoDecode] = ["127.0.0.0/8", "::1/128"]
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://kosto_vet:kosto_vet@localhost:5432/kosto_vet"
    database_pool_size: int = 5
    database_max_overflow: int = 5
    redis_url: str | None = None

    fixed_delivery_price_minor: int | None = Field(default=None, ge=0)
    stock_reservation_ttl_seconds: Literal[300] = 300
    stock_warning_seconds: int = 300
    stock_hard_stale_seconds: int = 900
    default_manager_name: str = "Kosto-Vet"
    default_manager_phone: str = "+7 (961) 189-89-33"
    default_manager_email: str = "Kosto-Vet@yandex.ru"
    emergency_phone: str | None = None
    emergency_surcharge_percent: int = 10

    customer_session_secret: SecretStr = SecretStr("development-customer-session-secret-change-me")
    staff_session_secret: SecretStr = SecretStr("development-staff-session-secret-change-me")
    csrf_secret: SecretStr = SecretStr("development-csrf-secret-change-me")
    access_ttl_seconds: int = 900
    customer_refresh_ttl_seconds: int = 2_592_000
    staff_refresh_ttl_seconds: int = 43_200
    cookie_secure: bool = False

    moysklad_mode: Mode = Mode.DISABLED
    moysklad_api_base_url: str = "https://api.moysklad.ru/api/remap/1.2"
    moysklad_access_token: SecretStr | None = None
    moysklad_warehouse_id: str | None = None
    moysklad_price_type_id: str | None = None
    moysklad_catalog_sync_interval_seconds: int = Field(default=900, ge=60)
    moysklad_stock_sync_interval_seconds: int = Field(default=60, ge=60)

    robokassa_mode: Mode = Mode.DISABLED
    fiscalization_mode: Mode = Mode.DISABLED
    robokassa_merchant_login: str | None = None
    robokassa_password1: SecretStr | None = None
    robokassa_password2: SecretStr | None = None
    robokassa_hash_algorithm: str = "sha256"
    robokassa_payment_url: str = "https://auth.robokassa.ru/Merchant/Index.aspx"

    yandex_oauth_mode: Mode = Mode.DISABLED
    yandex_oauth_client_id: str | None = None
    yandex_oauth_client_secret: SecretStr | None = None
    yandex_oauth_redirect_uri: str = "http://localhost:8000/api/v1/customer/auth/yandex/callback"
    yandex_oauth_scopes: str = "login:email login:info"

    s3_endpoint_url: str = "https://s3.regru.cloud"
    s3_region: str | None = None
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None
    s3_originals_bucket: str | None = None
    s3_public_media_bucket: str | None = None
    s3_public_base_url: str | None = None

    email_delivery_mode: Mode = Mode.DISABLED
    notification_delivery_mode: Mode = Mode.DISABLED

    @field_validator(
        "frontend_origins",
        "admin_origins",
        "trusted_hosts",
        "internal_allowed_cidrs",
        mode="before",
    )
    @classmethod
    def parse_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("frontend_origins", "admin_origins")
    @classmethod
    def validate_origins(cls, origins: list[str]) -> list[str]:
        for origin in origins:
            parsed = urlparse(origin)
            if (
                origin == "*"
                or not parsed.scheme
                or not parsed.netloc
                or parsed.path not in ("", "/")
                or parsed.query
                or parsed.fragment
                or origin.endswith("/")
            ):
                raise ValueError(f"invalid exact frontend origin: {origin}")
        return origins

    @model_validator(mode="after")
    def production_guards(self) -> Settings:
        if self.robokassa_mode is Mode.PRODUCTION:
            raise ValueError("production Robokassa is blocked in the demo release")
        if self.moysklad_mode is not Mode.DISABLED and (
            not self.moysklad_access_token
            or not self.moysklad_warehouse_id
            or not self.moysklad_price_type_id
        ):
            raise ValueError("enabled MoySklad requires token, warehouse and price type")
        if self.app_env == "production":
            secrets = {
                self.customer_session_secret.get_secret_value(),
                self.staff_session_secret.get_secret_value(),
                self.csrf_secret.get_secret_value(),
            }
            if len(secrets) != 3 or any(
                "development" in item or len(item) < 32 for item in secrets
            ):
                raise ValueError("production requires three independent 256-bit secrets")
            if not self.cookie_secure or not self.api_public_base_url.startswith("https://"):
                raise ValueError("production cookies and public API require HTTPS")
            if any(origin.startswith("http://") for origin in self.frontend_origins):
                raise ValueError("production frontend origins require HTTPS")
            if any(origin.startswith("http://") for origin in self.admin_origins):
                raise ValueError("production admin origins require HTTPS")
            s3_values = (
                self.s3_access_key_id,
                self.s3_secret_access_key,
                self.s3_originals_bucket,
                self.s3_public_media_bucket,
                self.s3_public_base_url,
            )
            if any(value is not None for value in s3_values) and not all(s3_values):
                raise ValueError("production S3 configuration must be complete")
            if all(s3_values) and not self.admin_origins:
                raise ValueError("production S3 media requires an exact admin origin")
            if (
                self.moysklad_mode is not Mode.DISABLED
                and not self.moysklad_api_base_url.startswith("https://")
            ):
                raise ValueError("production MoySklad API requires HTTPS")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
