from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Form, Header, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kosto_vet.api.dependencies import (
    Principal,
    customer_mutation,
    customer_principal,
    get_session,
    require_permission,
    staff_principal,
)
from kosto_vet.api.schemas import (
    AdminLeadUpdate,
    AdminOrderUpdate,
    ArticleCreate,
    ArticleMediaAttach,
    ArticleUpdate,
    CartItemUpdate,
    CartItemUpsert,
    CartMerge,
    CheckoutCreate,
    CustomerRegister,
    CustomerUpdate,
    DeliveryAddressCreate,
    DeliveryAddressUpdate,
    FavoriteCreate,
    LeadCreate,
    Login,
    MediaAttach,
    MediaLinkUpdate,
    MediaUploadIntentCreate,
    PasswordResetConfirm,
    PasswordResetRequest,
    PaymentAttemptCreate,
    ProductUpdate,
    QuoteCreate,
    StockSubscriptionCreate,
)
from kosto_vet.bootstrap.settings import Settings, get_settings
from kosto_vet.core.errors import DomainError, feature_disabled, not_found
from kosto_vet.core.time import utc_now
from kosto_vet.infrastructure.rate_limit import enforce_rate_limit
from kosto_vet.infrastructure.security import issue_csrf, normalize_email, token_hash, verify_csrf
from kosto_vet.models import (
    CustomerAccount,
    CustomerSession,
    Lead,
    Order,
    Product,
    StaffSession,
    StaffUser,
)
from kosto_vet.services.application import ApplicationService, SessionBundle, manager

router = APIRouter()


def _client_scope(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def service(settings: Settings = Depends(get_settings)) -> ApplicationService:
    return ApplicationService(settings)


def _set_cookies(
    response: Response, bundle: SessionBundle, settings: Settings, audience: str
) -> None:
    if audience == "customer":
        access_name, refresh_name, refresh_path = (
            "kv_customer_access",
            "kv_customer_refresh",
            "/api/v1/customer/auth",
        )
        refresh_age = settings.customer_refresh_ttl_seconds
    else:
        access_name, refresh_name, refresh_path = "kv_access", "kv_refresh", "/api/v1/admin/auth"
        refresh_age = settings.staff_refresh_ttl_seconds
    response.set_cookie(
        access_name,
        bundle.access,
        max_age=settings.access_ttl_seconds,
        path="/api/v1",
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )
    response.set_cookie(
        refresh_name,
        bundle.refresh,
        max_age=refresh_age,
        path=refresh_path,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )


def _clear_cookies(response: Response, settings: Settings, audience: str) -> None:
    secure = settings.cookie_secure
    if audience == "customer":
        response.delete_cookie("kv_customer_access", path="/api/v1", secure=secure, samesite="lax")
        response.delete_cookie(
            "kv_customer_refresh", path="/api/v1/customer/auth", secure=secure, samesite="lax"
        )
    else:
        response.delete_cookie("kv_access", path="/api/v1", secure=secure, samesite="lax")
        response.delete_cookie(
            "kv_refresh", path="/api/v1/admin/auth", secure=secure, samesite="lax"
        )


def _etag_version(value: str) -> int:
    if (
        len(value) < 3
        or not value.startswith('"')
        or not value.endswith('"')
        or not value[1:-1].isdigit()
    ):
        raise DomainError("INVALID_REQUEST", "If-Match должен содержать ETag версии.", 400)
    return int(value[1:-1])


async def _refresh_row(
    session: AsyncSession, refresh: str | None, audience: str, *, allow_revoked: bool = False
) -> CustomerSession | StaffSession:
    if not refresh:
        raise DomainError("AUTH_REQUIRED", "Требуется refresh session.", 401)
    if audience == "customer":
        row = await session.scalar(
            select(CustomerSession).where(CustomerSession.token_hash == token_hash(refresh))
        )
    else:
        row = await session.scalar(
            select(StaffSession).where(StaffSession.token_hash == token_hash(refresh))
        )
    if not row or (row.revoked_at is not None and not allow_revoked) or row.expires_at <= utc_now():
        raise DomainError("SESSION_EXPIRED", "Сессия истекла.", 401)
    return row


async def _verify_refresh_csrf(
    session: AsyncSession, refresh: str | None, csrf: str | None, audience: str, settings: Settings
) -> CustomerSession | StaffSession:
    row = await _refresh_row(session, refresh, audience, allow_revoked=True)
    if not csrf or not verify_csrf(settings, csrf, session_id=row.id, audience=audience):
        raise DomainError("CSRF_INVALID", "Недействительный CSRF token.", 403)
    return row


@router.get("/health/live", operation_id="getLiveness")
async def live(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    return {"ok": True, "version": settings.app_version}


@router.get("/health/ready", operation_id="getReadiness")
async def ready(
    session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    await session.execute(select(1))
    return {"ok": True, "version": settings.app_version, "checks": {"database": "ok"}}


@router.get("/api/v1/catalog/categories", operation_id="listCategories")
async def list_categories(
    session: AsyncSession = Depends(get_session), app: ApplicationService = Depends(service)
) -> dict[str, Any]:
    return await app.category_tree(session)


@router.get("/api/v1/catalog/categories/{slug}", operation_id="getCategory")
async def get_category(
    slug: str,
    session: AsyncSession = Depends(get_session),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    return await app.category_detail(session, slug)


@router.get("/api/v1/settings/public", operation_id="getPublicSettings")
async def public_settings(app: ApplicationService = Depends(service)) -> dict[str, Any]:
    return app.public_settings()


@router.get("/api/v1/catalog/products", operation_id="listProducts")
async def list_products(
    category: str | None = None,
    include_descendants: bool = True,
    q: str | None = Query(default=None, max_length=120),
    in_stock: bool | None = None,
    stock_state: str | None = None,
    sort: str = "popular",
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=24, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    return await app.list_products(
        session,
        category_slug=category,
        include_descendants=include_descendants,
        q=q,
        in_stock=in_stock,
        state=stock_state,
        sort=sort,
        page=page,
        limit=limit,
    )


@router.get("/api/v1/catalog/products/{slug}", operation_id="getProduct")
async def get_product(
    slug: str,
    session: AsyncSession = Depends(get_session),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    return await app.product_detail(session, slug)


@router.get("/api/v1/catalog/search-suggestions", operation_id="getSearchSuggestions")
async def search_suggestions(
    q: str = Query(min_length=2, max_length=80),
    session: AsyncSession = Depends(get_session),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    return await app.suggestions(session, q)


@router.get("/api/v1/articles", operation_id="listArticles")
async def list_articles(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    return await app.list_public_articles(session, page, limit)


@router.get("/api/v1/articles/{slug}", operation_id="getArticle")
async def get_article(
    slug: str,
    session: AsyncSession = Depends(get_session),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    return await app.public_article(session, slug)


@router.post("/api/v1/leads", status_code=202, operation_id="createLead")
async def create_lead(
    payload: LeadCreate,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
    session: AsyncSession = Depends(get_session),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    await enforce_rate_limit(
        session,
        app.settings,
        action="create_lead",
        scope=_client_scope(request),
        limit=10,
        window_seconds=900,
    )
    return await app.create_lead(
        session,
        key=idempotency_key,
        payload=payload.model_dump(mode="json"),
        request_id=request.state.request_id,
    )


@router.post("/api/v1/stock-subscriptions", status_code=202, operation_id="createStockSubscription")
async def create_stock_subscription(
    payload: StockSubscriptionCreate,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
    session: AsyncSession = Depends(get_session),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    await enforce_rate_limit(
        session,
        app.settings,
        action="stock_subscription",
        scope=_client_scope(request),
        limit=10,
        window_seconds=900,
    )
    return await app.create_stock_subscription(
        session,
        key=idempotency_key,
        payload=payload.model_dump(mode="json"),
        request_id=request.state.request_id,
    )


@router.post("/api/v1/orders/quote", status_code=202, operation_id="createQuoteOrder")
async def create_quote(
    payload: QuoteCreate,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    await enforce_rate_limit(
        session,
        app.settings,
        action="quote_order",
        scope=_client_scope(request),
        limit=10,
        window_seconds=900,
    )
    principal: Principal | None = None
    if request.cookies.get("kv_customer_access"):
        from kosto_vet.api.dependencies import _principal

        principal = await _principal(
            token=request.cookies.get("kv_customer_access"),
            audience="customer",
            settings=settings,
            session=session,
        )
        if not x_csrf_token or not verify_csrf(
            settings, x_csrf_token, session_id=principal.session_id, audience="customer"
        ):
            raise DomainError("CSRF_INVALID", "Недействительный CSRF token.", 403)
    return await app.create_order(
        session,
        payload=payload.model_dump(mode="json"),
        key=idempotency_key,
        customer_id=principal.id if principal else None,
        quote=True,
        request_id=request.state.request_id,
    )


@router.post("/api/v1/orders/checkout", status_code=201, operation_id="createCheckoutOrder")
async def checkout(
    payload: CheckoutCreate,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    await enforce_rate_limit(
        session,
        settings,
        action="checkout_order",
        scope=_client_scope(request),
        limit=10,
        window_seconds=900,
    )
    principal: Principal | None = None
    if request.cookies.get("kv_customer_access"):
        from kosto_vet.api.dependencies import _principal

        principal = await _principal(
            token=request.cookies.get("kv_customer_access"),
            audience="customer",
            settings=settings,
            session=session,
        )
        if not x_csrf_token or not verify_csrf(
            settings, x_csrf_token, session_id=principal.session_id, audience="customer"
        ):
            raise DomainError("CSRF_INVALID", "Недействительный CSRF token.", 403)
    return await app.create_order(
        session,
        payload=payload.model_dump(mode="json"),
        key=idempotency_key,
        customer_id=principal.id if principal else None,
        quote=False,
        request_id=request.state.request_id,
    )


@router.get("/api/v1/orders/{public_id}", operation_id="getPublicOrder")
async def get_public_order(
    public_id: str,
    x_order_access_token: Annotated[str | None, Header(alias="X-Order-Access-Token")] = None,
    session: AsyncSession = Depends(get_session),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    order = await app.verify_order_token(session, public_id, x_order_access_token)
    return await app.order_payload(session, order)


@router.post(
    "/api/v1/orders/{public_id}/payment-attempts",
    status_code=201,
    operation_id="createOrderPaymentAttempt",
)
async def retry_payment(
    public_id: str,
    payload: PaymentAttemptCreate,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
    x_order_access_token: Annotated[str | None, Header(alias="X-Order-Access-Token")] = None,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    await enforce_rate_limit(
        session,
        settings,
        action="payment_attempt",
        scope=f"{_client_scope(request)}:{public_id}",
        limit=10,
        window_seconds=900,
    )
    order = await app.verify_order_token(session, public_id, x_order_access_token)
    return await app.retry_payment(
        session,
        order=order,
        method=payload.payment_method,
        key=idempotency_key,
        request_id=request.state.request_id,
        order_access_token=x_order_access_token or "",
    )


@router.post("/api/v1/customer/auth/register", status_code=201, operation_id="registerCustomer")
async def register_customer(
    payload: CustomerRegister,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    await enforce_rate_limit(
        session,
        settings,
        action="customer_register",
        scope=_client_scope(request),
        limit=5,
        window_seconds=3600,
    )
    result, bundle = await app.register(
        session, payload.model_dump(mode="json"), request_id=request.state.request_id
    )
    _set_cookies(response, bundle, settings, "customer")
    return result


@router.post("/api/v1/customer/auth/login", operation_id="loginCustomer")
async def login_customer(
    payload: Login,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    for scope in (_client_scope(request), normalize_email(str(payload.email))):
        await enforce_rate_limit(
            session,
            settings,
            action="customer_login",
            scope=scope,
            limit=10,
            window_seconds=900,
            block_seconds=900,
        )
    result, bundle = await app.customer_login(session, str(payload.email), payload.password)
    _set_cookies(response, bundle, settings, "customer")
    return result


@router.post("/api/v1/customer/auth/refresh", operation_id="refreshCustomerSession")
async def refresh_customer(
    response: Response,
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    kv_customer_refresh: str | None = Cookie(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    await _verify_refresh_csrf(session, kv_customer_refresh, x_csrf_token, "customer", settings)
    result, bundle = await app.rotate_session(
        session, refresh_token=kv_customer_refresh or "", audience="customer"
    )
    _set_cookies(response, bundle, settings, "customer")
    return result


@router.get("/api/v1/customer/auth/csrf", operation_id="issueCustomerCsrfToken")
async def customer_csrf(
    kv_customer_refresh: str | None = Cookie(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    row = await _refresh_row(session, kv_customer_refresh, "customer")
    return JSONResponse(
        {"csrf_token": issue_csrf(settings, session_id=row.id, audience="customer")},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/v1/customer/auth/logout", status_code=204, operation_id="logoutCustomer")
async def logout_customer(
    response: Response,
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    kv_customer_refresh: str | None = Cookie(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    app: ApplicationService = Depends(service),
) -> None:
    await _verify_refresh_csrf(session, kv_customer_refresh, x_csrf_token, "customer", settings)
    await app.revoke_refresh(session, refresh_token=kv_customer_refresh, audience="customer")
    _clear_cookies(response, settings, "customer")


@router.get("/api/v1/customer/auth/yandex/start", operation_id="startYandexLogin")
async def yandex_start(
    request: Request,
    return_to: str = "/account",
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    app: ApplicationService = Depends(service),
) -> RedirectResponse:
    await enforce_rate_limit(
        session,
        settings,
        action="yandex_oauth_start",
        scope=_client_scope(request),
        limit=20,
        window_seconds=900,
    )
    return RedirectResponse(await app.start_yandex(session, return_path=return_to), status_code=302)


@router.get("/api/v1/customer/auth/yandex/callback", operation_id="completeYandexLogin")
async def yandex_callback(
    state: str,
    code: str | None = None,
    error: str | None = None,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    app: ApplicationService = Depends(service),
) -> RedirectResponse:
    frontend = settings.frontend_origins[0]
    if error or not code:
        return RedirectResponse(f"{frontend}/login?oauth_error=provider_denied", status_code=302)
    return_path, bundle = await app.complete_yandex(session, state=state, code=code)
    response = RedirectResponse(f"{frontend}{return_path}", status_code=302)
    if bundle:
        _set_cookies(response, bundle, settings, "customer")
    return response


@router.post("/api/v1/customer/auth/yandex/link", operation_id="linkYandexAccount")
async def yandex_link(
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(customer_mutation),
    app: ApplicationService = Depends(service),
) -> RedirectResponse:
    return RedirectResponse(
        await app.start_yandex(session, return_path="/account", customer_id=principal.id),
        status_code=302,
    )


@router.delete(
    "/api/v1/customer/auth/yandex/unlink", status_code=204, operation_id="unlinkYandexAccount"
)
async def yandex_unlink(
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(customer_mutation),
    app: ApplicationService = Depends(service),
) -> None:
    await app.unlink_yandex(session, principal.id)


@router.post(
    "/api/v1/customer/auth/password-reset/request", operation_id="requestCustomerPasswordReset"
)
async def password_reset_request(_: PasswordResetRequest) -> None:
    raise feature_disabled("password_reset")


@router.post(
    "/api/v1/customer/auth/password-reset/confirm", operation_id="confirmCustomerPasswordReset"
)
async def password_reset_confirm(_: PasswordResetConfirm) -> None:
    raise feature_disabled("password_reset")


@router.get("/api/v1/account/me", operation_id="getCustomerMe")
async def customer_me(
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(customer_principal),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    return await app.customer_me(session, principal.id)


@router.patch("/api/v1/account/me", operation_id="updateCustomerMe")
async def customer_update(
    payload: CustomerUpdate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(customer_mutation),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    return await app.update_customer(
        session, principal.id, payload.model_dump(mode="json", exclude_unset=True)
    )


@router.get("/api/v1/account/delivery-addresses", operation_id="listCustomerDeliveryAddresses")
async def list_delivery_addresses(
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(customer_principal),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    return await app.list_delivery_addresses(session, principal.id)


@router.post(
    "/api/v1/account/delivery-addresses",
    status_code=201,
    operation_id="createCustomerDeliveryAddress",
)
async def create_delivery_address(
    payload: DeliveryAddressCreate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(customer_mutation),
    app: ApplicationService = Depends(service),
) -> Response:
    result = await app.create_delivery_address(
        session, principal.id, payload.model_dump(mode="python")
    )
    return JSONResponse(result, status_code=201, headers={"ETag": f'"{result["version"]}"'})


@router.patch(
    "/api/v1/account/delivery-addresses/{id}", operation_id="updateCustomerDeliveryAddress"
)
async def update_delivery_address(
    id: UUID,
    payload: DeliveryAddressUpdate,
    if_match: Annotated[str, Header(alias="If-Match")],
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(customer_mutation),
    app: ApplicationService = Depends(service),
) -> Response:
    result = await app.update_delivery_address(
        session,
        principal.id,
        id,
        payload.model_dump(mode="python", exclude_unset=True),
        _etag_version(if_match),
    )
    return JSONResponse(result, headers={"ETag": f'"{result["version"]}"'})


@router.delete(
    "/api/v1/account/delivery-addresses/{id}",
    status_code=204,
    operation_id="deleteCustomerDeliveryAddress",
)
async def delete_delivery_address(
    id: UUID,
    if_match: Annotated[str, Header(alias="If-Match")],
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(customer_mutation),
    app: ApplicationService = Depends(service),
) -> None:
    await app.delete_delivery_address(session, principal.id, id, _etag_version(if_match))


@router.get("/api/v1/account/manager", operation_id="getCustomerManager")
async def customer_manager(
    _: Principal = Depends(customer_principal), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    return manager(settings)


@router.get("/api/v1/account/orders", operation_id="listCustomerOrders")
async def customer_orders(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(customer_principal),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    return await app.list_customer_orders(session, principal.id, page, limit)


@router.get("/api/v1/account/orders/{public_id}", operation_id="getCustomerOrder")
async def customer_order(
    public_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(customer_principal),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    order = await session.scalar(
        select(Order).where(Order.public_id == public_id, Order.customer_id == principal.id)
    )
    if not order:
        raise not_found()
    return await app.order_payload(session, order)


@router.get("/api/v1/account/cart", operation_id="getCustomerCart")
async def get_cart(
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(customer_principal),
    app: ApplicationService = Depends(service),
) -> Response:
    payload = await app.cart_payload(session, principal.id)
    return JSONResponse(payload, headers={"ETag": f'"{payload["version"]}"'})


@router.put("/api/v1/account/cart/items", operation_id="upsertCustomerCartItem")
async def upsert_cart(
    payload: CartItemUpsert,
    if_match: Annotated[str, Header(alias="If-Match")],
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(customer_mutation),
    app: ApplicationService = Depends(service),
) -> Response:
    result = await app.upsert_cart_item(
        session, principal.id, payload.product_id, payload.quantity, _etag_version(if_match)
    )
    return JSONResponse(result, headers={"ETag": f'"{result["version"]}"'})


@router.patch("/api/v1/account/cart/items/{id}", operation_id="updateCustomerCartItem")
async def update_cart(
    id: UUID,
    payload: CartItemUpdate,
    if_match: Annotated[str, Header(alias="If-Match")],
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(customer_mutation),
    app: ApplicationService = Depends(service),
) -> Response:
    result = await app.update_cart_item(
        session, principal.id, id, payload.quantity, _etag_version(if_match)
    )
    return JSONResponse(result, headers={"ETag": f'"{result["version"]}"'})


@router.delete("/api/v1/account/cart/items/{id}", operation_id="deleteCustomerCartItem")
async def delete_cart(
    id: UUID,
    if_match: Annotated[str, Header(alias="If-Match")],
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(customer_mutation),
    app: ApplicationService = Depends(service),
) -> Response:
    result = await app.delete_cart_item(session, principal.id, id, _etag_version(if_match))
    return JSONResponse(result, headers={"ETag": f'"{result["version"]}"'})


@router.post("/api/v1/account/cart/merge", operation_id="mergeCustomerCart")
async def merge_cart(
    payload: CartMerge,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(customer_mutation),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    return await app.merge_cart(
        session, principal.id, payload.model_dump(mode="python")["items"], idempotency_key
    )


@router.get("/api/v1/account/favorites", operation_id="listCustomerFavorites")
async def list_favorites(
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(customer_principal),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    return await app.favorites(session, principal.id)


@router.post("/api/v1/account/favorites", status_code=201, operation_id="addCustomerFavorite")
async def add_favorite(
    payload: FavoriteCreate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(customer_mutation),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    return await app.add_favorite(session, principal.id, payload.product_id)


@router.delete(
    "/api/v1/account/favorites/{product_id}", status_code=204, operation_id="deleteCustomerFavorite"
)
async def delete_favorite(
    product_id: UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(customer_mutation),
    app: ApplicationService = Depends(service),
) -> None:
    await app.delete_favorite(session, principal.id, product_id)


@router.post("/api/v1/payments/robokassa/result", operation_id="handleRobokassaResult")
async def robokassa_result(
    request: Request,
    OutSum: str = Form(),
    InvId: str = Form(),
    SignatureValue: str = Form(),
    session: AsyncSession = Depends(get_session),
    app: ApplicationService = Depends(service),
) -> PlainTextResponse:
    form = await request.form()
    values = {str(key): str(value) for key, value in form.multi_items()}
    values.update({"OutSum": OutSum, "InvId": InvId, "SignatureValue": SignatureValue})
    return PlainTextResponse(await app.process_robokassa(session, values, request.state.request_id))


@router.get("/api/v1/payments/robokassa/success", operation_id="handleRobokassaSuccessReturn")
async def robokassa_success(settings: Settings = Depends(get_settings)) -> RedirectResponse:
    return RedirectResponse(
        f"{settings.frontend_origins[0]}/account/orders?payment=checking", status_code=302
    )


@router.get("/api/v1/payments/robokassa/fail", operation_id="handleRobokassaFailReturn")
async def robokassa_fail(settings: Settings = Depends(get_settings)) -> RedirectResponse:
    return RedirectResponse(
        f"{settings.frontend_origins[0]}/account/orders?payment=failed", status_code=302
    )


@router.post("/api/v1/admin/auth/login", operation_id="adminLogin")
async def admin_login(
    payload: Login,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    for scope in (_client_scope(request), normalize_email(str(payload.email))):
        await enforce_rate_limit(
            session,
            settings,
            action="staff_login",
            scope=scope,
            limit=5,
            window_seconds=900,
            block_seconds=1800,
        )
    result, bundle = await app.staff_login(session, str(payload.email), payload.password)
    _set_cookies(response, bundle, settings, "staff")
    return result


@router.post("/api/v1/admin/auth/refresh", operation_id="refreshAdminSession")
async def admin_refresh(
    response: Response,
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    kv_refresh: str | None = Cookie(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    await _verify_refresh_csrf(session, kv_refresh, x_csrf_token, "staff", settings)
    result, bundle = await app.rotate_session(
        session, refresh_token=kv_refresh or "", audience="staff"
    )
    _set_cookies(response, bundle, settings, "staff")
    return result


@router.get("/api/v1/admin/auth/csrf", operation_id="issueAdminCsrfToken")
async def admin_csrf(
    kv_refresh: str | None = Cookie(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    row = await _refresh_row(session, kv_refresh, "staff")
    return JSONResponse(
        {"csrf_token": issue_csrf(settings, session_id=row.id, audience="staff")},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/v1/admin/auth/logout", status_code=204, operation_id="adminLogout")
async def admin_logout(
    response: Response,
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    kv_refresh: str | None = Cookie(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    app: ApplicationService = Depends(service),
) -> None:
    await _verify_refresh_csrf(session, kv_refresh, x_csrf_token, "staff", settings)
    await app.revoke_refresh(session, refresh_token=kv_refresh, audience="staff")
    _clear_cookies(response, settings, "staff")


@router.get("/api/v1/admin/auth/me", operation_id="getAdminMe")
async def admin_me(
    session: AsyncSession = Depends(get_session), principal: Principal = Depends(staff_principal)
) -> dict[str, Any]:
    staff = await session.get(StaffUser, principal.id)
    if not staff:
        raise not_found()
    return {"user": {"id": str(staff.id), "email": staff.email, "role": staff.role}}


@router.get("/api/v1/admin/orders", operation_id="adminListOrders")
async def admin_orders(
    status: str | None = None,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=30, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_permission("orders.read")),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    return await app.list_admin_orders(session, status, page, limit)


@router.get("/api/v1/admin/orders/{id}", operation_id="adminGetOrder")
async def admin_order(
    id: UUID,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_permission("orders.read")),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    order = await session.get(Order, id)
    if not order:
        raise not_found()
    return await app.order_payload(session, order, admin=True)


@router.patch("/api/v1/admin/orders/{id}", operation_id="adminUpdateOrder")
async def admin_update_order(
    id: UUID,
    payload: AdminOrderUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_permission("orders.update", mutation=True)),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    return await app.update_admin_order(
        session, id, payload.model_dump(mode="json"), principal.id, request.state.request_id
    )


@router.get("/api/v1/admin/customers", operation_id="adminListCustomers")
async def admin_customers(
    q: str | None = Query(default=None, max_length=120),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=30, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_permission("customers.read")),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    return await app.list_admin_customers(session, q, page, limit)


@router.get("/api/v1/admin/customers/{id}", operation_id="adminGetCustomer")
async def admin_customer(
    id: UUID,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_permission("customers.read")),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    customer = await session.get(CustomerAccount, id)
    if not customer:
        raise not_found()
    return await app.admin_customer(session, customer)


@router.get("/api/v1/admin/leads", operation_id="adminListLeads")
async def admin_leads(
    status: str | None = None,
    source: str | None = None,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=30, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_permission("leads.read")),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    return await app.list_leads(session, status, source, page, limit)


@router.get("/api/v1/admin/leads/{id}", operation_id="adminGetLead")
async def admin_lead(
    id: UUID,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_permission("leads.read")),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    row = await session.get(Lead, id)
    if not row:
        raise not_found()
    return app.lead_payload(row)


@router.patch("/api/v1/admin/leads/{id}", operation_id="adminUpdateLead")
async def admin_update_lead(
    id: UUID,
    payload: AdminLeadUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_permission("leads.update", mutation=True)),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    return await app.update_lead(
        session, id, payload.model_dump(mode="json"), principal.id, request.state.request_id
    )


@router.patch("/api/v1/admin/products/{id}", operation_id="adminUpdateProduct")
async def admin_update_product(
    id: UUID,
    payload: ProductUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_permission("products.update", mutation=True)),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    return await app.update_product(
        session,
        id,
        payload.model_dump(mode="json", exclude_unset=True),
        principal.id,
        request.state.request_id,
    )


@router.get("/api/v1/admin/articles", operation_id="adminListArticles")
async def admin_list_articles(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=30, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_permission("articles.read")),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    return await app.list_admin_articles(session, page, limit)


@router.post("/api/v1/admin/articles", status_code=201, operation_id="adminCreateArticle")
async def admin_create_article(
    payload: ArticleCreate,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_permission("articles.write", mutation=True)),
    app: ApplicationService = Depends(service),
) -> Response:
    result = await app.create_article(
        session,
        payload.model_dump(mode="json"),
        principal.id,
        request.state.request_id,
        idempotency_key,
    )
    return JSONResponse(result, status_code=201, headers={"ETag": f'"{result["version"]}"'})


@router.get("/api/v1/admin/articles/{id}", operation_id="adminGetArticle")
async def admin_get_article(
    id: UUID,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_permission("articles.read")),
    app: ApplicationService = Depends(service),
) -> Response:
    result = await app.admin_article(session, id)
    return JSONResponse(result, headers={"ETag": f'"{result["version"]}"'})


@router.patch("/api/v1/admin/articles/{id}", operation_id="adminUpdateArticle")
async def admin_update_article(
    id: UUID,
    payload: ArticleUpdate,
    if_match: Annotated[str, Header(alias="If-Match")],
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_permission("articles.write", mutation=True)),
    app: ApplicationService = Depends(service),
) -> Response:
    result = await app.update_article(
        session,
        id,
        payload.model_dump(mode="json", exclude_unset=True),
        _etag_version(if_match),
        principal.id,
        request.state.request_id,
    )
    return JSONResponse(result, headers={"ETag": f'"{result["version"]}"'})


@router.post(
    "/api/v1/admin/media/upload-intents", status_code=201, operation_id="createMediaUploadIntent"
)
async def create_media_upload_intent(
    payload: MediaUploadIntentCreate,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_permission("media.write", mutation=True)),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    return await app.create_media_upload_intent(
        session,
        payload.model_dump(mode="json"),
        principal.id,
        request.state.request_id,
        idempotency_key,
    )


@router.get("/api/v1/admin/media/{id}", operation_id="getAdminMedia")
async def get_admin_media(
    id: UUID,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_permission("media.read")),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    return await app.admin_media(session, id)


@router.post(
    "/api/v1/admin/media/{id}/complete", status_code=202, operation_id="completeMediaUpload"
)
async def complete_media_upload(
    id: UUID,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_permission("media.write", mutation=True)),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    return await app.complete_media_upload(
        session, id, principal.id, request.state.request_id, idempotency_key
    )


@router.post("/api/v1/admin/articles/{id}/media", operation_id="attachArticleMedia")
async def attach_article_media(
    id: UUID,
    payload: ArticleMediaAttach,
    if_match: Annotated[str, Header(alias="If-Match")],
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_permission("media.write", mutation=True)),
    app: ApplicationService = Depends(service),
) -> Response:
    result = await app.attach_article_media(
        session,
        id,
        payload.model_dump(mode="json"),
        _etag_version(if_match),
        principal.id,
        request.state.request_id,
    )
    return JSONResponse(result, headers={"ETag": f'"{result["version"]}"'})


@router.patch("/api/v1/admin/articles/{id}/media/{media_id}", operation_id="updateArticleMedia")
async def update_article_media(
    id: UUID,
    media_id: UUID,
    payload: MediaLinkUpdate,
    if_match: Annotated[str, Header(alias="If-Match")],
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_permission("media.write", mutation=True)),
    app: ApplicationService = Depends(service),
) -> Response:
    result = await app.update_article_media(
        session,
        id,
        media_id,
        payload.model_dump(mode="json", exclude_unset=True),
        _etag_version(if_match),
        principal.id,
        request.state.request_id,
    )
    return JSONResponse(result, headers={"ETag": f'"{result["version"]}"'})


@router.delete("/api/v1/admin/articles/{id}/media/{media_id}", operation_id="deleteArticleMedia")
async def delete_article_media(
    id: UUID,
    media_id: UUID,
    if_match: Annotated[str, Header(alias="If-Match")],
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_permission("media.write", mutation=True)),
    app: ApplicationService = Depends(service),
) -> Response:
    result = await app.delete_article_media(
        session, id, media_id, _etag_version(if_match), principal.id, request.state.request_id
    )
    return JSONResponse(result, headers={"ETag": f'"{result["version"]}"'})


@router.get("/api/v1/admin/products/{id}/media", operation_id="adminListProductMedia")
async def admin_list_product_media(
    id: UUID,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_permission("media.read")),
    app: ApplicationService = Depends(service),
) -> Response:
    product = await session.get(Product, id)
    if not product:
        raise not_found()
    result = await app.product_media_payload(session, product)
    return JSONResponse(result, headers={"ETag": f'"{result["version"]}"'})


@router.post("/api/v1/admin/products/{id}/media", operation_id="attachProductMedia")
async def attach_product_media(
    id: UUID,
    payload: MediaAttach,
    if_match: Annotated[str, Header(alias="If-Match")],
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_permission("media.write", mutation=True)),
    app: ApplicationService = Depends(service),
) -> Response:
    result = await app.attach_product_media(
        session,
        id,
        payload.model_dump(mode="json"),
        _etag_version(if_match),
        principal.id,
        request.state.request_id,
    )
    return JSONResponse(result, headers={"ETag": f'"{result["version"]}"'})


@router.patch("/api/v1/admin/products/{id}/media/{media_id}", operation_id="updateProductMedia")
async def update_product_media(
    id: UUID,
    media_id: UUID,
    payload: MediaLinkUpdate,
    if_match: Annotated[str, Header(alias="If-Match")],
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_permission("media.write", mutation=True)),
    app: ApplicationService = Depends(service),
) -> Response:
    result = await app.update_product_media(
        session,
        id,
        media_id,
        payload.model_dump(mode="json", exclude_unset=True),
        _etag_version(if_match),
        principal.id,
        request.state.request_id,
    )
    return JSONResponse(result, headers={"ETag": f'"{result["version"]}"'})


@router.delete("/api/v1/admin/products/{id}/media/{media_id}", operation_id="deleteProductMedia")
async def delete_product_media(
    id: UUID,
    media_id: UUID,
    if_match: Annotated[str, Header(alias="If-Match")],
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_permission("media.write", mutation=True)),
    app: ApplicationService = Depends(service),
) -> Response:
    result = await app.delete_product_media(
        session, id, media_id, _etag_version(if_match), principal.id, request.state.request_id
    )
    return JSONResponse(result, headers={"ETag": f'"{result["version"]}"'})


@router.get("/api/v1/admin/integrations/sync-jobs", operation_id="listSyncJobs")
async def integration_jobs(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_permission("integrations.read")),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    return await app.list_jobs(session)


@router.post(
    "/api/v1/admin/integrations/moysklad/sync", status_code=202, operation_id="triggerMoySkladSync"
)
async def trigger_sync(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_permission("*", mutation=True)),
    app: ApplicationService = Depends(service),
) -> dict[str, Any]:
    return await app.trigger_sync(session)
