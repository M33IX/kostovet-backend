from __future__ import annotations

import ipaddress
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import structlog
import yaml
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from sqlalchemy import select
from starlette.middleware.trustedhost import TrustedHostMiddleware

from kosto_vet.api.routes import router
from kosto_vet.bootstrap.settings import Settings, get_settings
from kosto_vet.core.errors import DomainError
from kosto_vet.core.time import utc_now
from kosto_vet.infrastructure.database import Database
from kosto_vet.infrastructure.security import decode_access_token
from kosto_vet.models import StaffSession

LOGGER = structlog.get_logger("kosto_vet.api")
REQUESTS = Counter(
    "kosto_vet_http_requests_total",
    "HTTP requests",
    ("method", "route", "status"),
)
LATENCY = Histogram(
    "kosto_vet_http_request_seconds",
    "HTTP request duration",
    ("method", "route"),
)


def _error_payload(
    code: str,
    message: str,
    request_id: str,
    *,
    retryable: bool = False,
    field_errors: list[dict[str, str]] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": request_id,
        "retryable": retryable,
    }
    if field_errors:
        detail["field_errors"] = field_errors
    if meta:
        detail["meta"] = meta
    return {"ok": False, "error": detail}


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", str(uuid4()))


def _is_internal(request: Request, settings: Settings) -> bool:
    if request.client is None:
        return False
    try:
        address = ipaddress.ip_address(request.client.host)
        return any(
            address in ipaddress.ip_network(cidr) for cidr in settings.internal_allowed_cidrs
        )
    except ValueError:
        return False


async def _is_admin(request: Request, settings: Settings) -> bool:
    token = request.cookies.get("kv_access")
    if not token:
        return False
    try:
        claims = decode_access_token(settings, token, "staff")
    except Exception:  # noqa: BLE001 - authentication failure must remain indistinguishable
        return False
    if claims.role != "admin":
        return False
    async with request.app.state.database.sessions() as session:
        record = await session.scalar(
            select(StaffSession).where(StaffSession.id == claims.session_id)
        )
        return record is not None and record.revoked_at is None and record.expires_at > utc_now()


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    database = Database(settings)
    application.state.database = database
    LOGGER.info("api_started", environment=settings.app_env, version=settings.app_version)
    try:
        yield
    finally:
        await database.close()
        LOGGER.info("api_stopped")


settings = get_settings()
app = FastAPI(
    title="Kosto-Vet API",
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs" if settings.app_env != "production" else None,
    redoc_url=None,
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.frontend_origins,
    allow_credentials=True,
    allow_methods=["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=[
        "Content-Type",
        "Idempotency-Key",
        "If-Match",
        "X-CSRF-Token",
        "X-Order-Access-Token",
    ],
)


@app.middleware("http")
async def security_and_observability(request: Request, call_next: Any) -> Any:
    started = time.perf_counter()
    request.state.request_id = request.headers.get("X-Request-ID") or str(uuid4())
    unsafe = request.method not in {"GET", "HEAD", "OPTIONS"}
    provider_callback = request.url.path == "/api/v1/payments/robokassa/result"
    origin = request.headers.get("Origin")
    cookie_authenticated = any(
        name in request.cookies
        for name in ("kv_customer_access", "kv_customer_refresh", "kv_access", "kv_refresh")
    )
    allowed_origins = {*settings.frontend_origins, *settings.admin_origins}
    invalid_browser_origin = bool(origin) and origin not in allowed_origins
    missing_cookie_origin = cookie_authenticated and origin not in allowed_origins
    if unsafe and not provider_callback and (invalid_browser_origin or missing_cookie_origin):
        return JSONResponse(
            _error_payload("ORIGIN_FORBIDDEN", "Недопустимый Origin.", request.state.request_id),
            status_code=403,
        )
    response = await call_next(request)
    route = request.scope.get("route")
    route_path = getattr(route, "path", "unmatched")
    elapsed = time.perf_counter() - started
    REQUESTS.labels(request.method, route_path, str(response.status_code)).inc()
    LATENCY.labels(request.method, route_path).observe(elapsed)
    response.headers["X-Request-ID"] = request.state.request_id
    if request.url.path.startswith(("/api/v1/account", "/api/v1/admin", "/api/v1/customer/auth")):
        response.headers["Cache-Control"] = "no-store"
    LOGGER.info(
        "http_request",
        request_id=request.state.request_id,
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=round(elapsed * 1000, 2),
    )
    return response


@app.exception_handler(DomainError)
async def handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
    headers: dict[str, str] = {}
    retry_after = exc.meta.get("retry_after_seconds")
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(
        _error_payload(
            exc.code,
            exc.message,
            _request_id(request),
            retryable=exc.retryable,
            field_errors=exc.field_errors,
            meta=exc.meta,
        ),
        status_code=exc.status_code,
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        _error_payload(
            "VALIDATION_ERROR",
            "Запрос не прошёл валидацию.",
            _request_id(request),
            field_errors=[
                {
                    "field": ".".join(map(str, item["loc"])),
                    "code": item["type"],
                    "message": item["msg"],
                }
                for item in exc.errors()
            ],
        ),
        status_code=422,
    )


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    LOGGER.exception("unhandled_error", request_id=_request_id(request))
    return JSONResponse(
        _error_payload("INTERNAL_ERROR", "Внутренняя ошибка сервера.", _request_id(request)),
        status_code=500,
    )


app.include_router(router)


@app.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> PlainTextResponse:
    if not _is_internal(request, settings) and not await _is_admin(request, settings):
        return PlainTextResponse("not found", status_code=404)
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health/integrations", include_in_schema=False)
async def integration_health(request: Request) -> JSONResponse:
    if not _is_internal(request, settings) and not await _is_admin(request, settings):
        return JSONResponse(
            _error_payload("NOT_FOUND", "Ресурс не найден.", _request_id(request)), status_code=404
        )
    return JSONResponse(
        {
            "moysklad": settings.moysklad_mode,
            "robokassa": settings.robokassa_mode,
            "yandex": settings.yandex_oauth_mode,
            "email": settings.email_delivery_mode,
        }
    )


def canonical_openapi() -> dict[str, Any]:
    contract_path = Path(__file__).resolve().parents[3] / "contracts" / "openapi.v1.yaml"
    with contract_path.open(encoding="utf-8") as contract:
        document = yaml.safe_load(contract)
    if not isinstance(document, dict):
        raise RuntimeError("canonical OpenAPI document is invalid")
    return document


app.openapi = canonical_openapi  # type: ignore[method-assign]
