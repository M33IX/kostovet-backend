from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

import jwt
from fastapi import Cookie, Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kosto_vet.bootstrap.settings import Settings, get_settings
from kosto_vet.core.errors import DomainError, forbidden
from kosto_vet.core.types import utc_now
from kosto_vet.infrastructure.models import CustomerSession, StaffSession
from kosto_vet.infrastructure.security import AccessClaims, decode_access_token, verify_csrf


@dataclass(frozen=True, slots=True)
class Principal:
    id: UUID
    session_id: UUID
    audience: str
    role: str | None = None


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.database.sessions() as session:
        yield session


async def _principal(
    *,
    token: str | None,
    audience: str,
    settings: Settings,
    session: AsyncSession,
) -> Principal:
    if not token:
        raise DomainError("AUTH_REQUIRED", "Требуется вход.", 401)
    try:
        claims: AccessClaims = decode_access_token(settings, token, audience)
    except jwt.PyJWTError as exc:
        raise DomainError("SESSION_EXPIRED", "Сессия истекла.", 401) from exc
    if audience == "customer":
        record = await session.scalar(
            select(CustomerSession).where(CustomerSession.id == claims.session_id)
        )
    else:
        record = await session.scalar(
            select(StaffSession).where(StaffSession.id == claims.session_id)
        )
    if not record or record.revoked_at is not None or record.expires_at <= utc_now():
        raise DomainError("SESSION_EXPIRED", "Сессия отозвана.", 401)
    return Principal(claims.subject, claims.session_id, audience, claims.role)


async def customer_principal(
    kv_customer_access: str | None = Cookie(default=None),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    return await _principal(
        token=kv_customer_access,
        audience="customer",
        settings=settings,
        session=session,
    )


async def optional_customer_principal(
    kv_customer_access: str | None = Cookie(default=None),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> Principal | None:
    if not kv_customer_access:
        return None
    return await _principal(
        token=kv_customer_access,
        audience="customer",
        settings=settings,
        session=session,
    )


async def staff_principal(
    kv_access: str | None = Cookie(default=None),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    return await _principal(
        token=kv_access,
        audience="staff",
        settings=settings,
        session=session,
    )


def csrf_dependency(audience: str) -> Callable[..., Awaitable[Principal]]:
    async def dependency(
        request: Request,
        x_csrf_token: str | None = Header(default=None),
        settings: Settings = Depends(get_settings),
        session: AsyncSession = Depends(get_session),
    ) -> Principal:
        cookie_name = "kv_customer_access" if audience == "customer" else "kv_access"
        principal = await _principal(
            token=request.cookies.get(cookie_name),
            audience=audience,
            settings=settings,
            session=session,
        )
        if not x_csrf_token or not verify_csrf(
            settings,
            x_csrf_token,
            session_id=principal.session_id,
            audience=audience,
        ):
            raise DomainError("CSRF_INVALID", "Недействительный CSRF token.", 403)
        return principal

    return dependency


customer_mutation = csrf_dependency("customer")
staff_mutation = csrf_dependency("staff")


PERMISSIONS: dict[str, frozenset[str]] = {
    "admin": frozenset({"*"}),
    "manager": frozenset(
        {
            "orders.read",
            "orders.update",
            "customers.read",
            "leads.read",
            "leads.update",
            "integrations.read",
        }
    ),
    "content": frozenset(
        {
            "products.update",
            "articles.read",
            "articles.write",
            "media.read",
            "media.write",
        }
    ),
    "readonly": frozenset({"orders.read", "customers.read", "leads.read", "integrations.read"}),
}


def require_permission(
    permission: str, *, mutation: bool = False
) -> Callable[..., Awaitable[Principal]]:
    principal_dependency = staff_mutation if mutation else staff_principal

    async def dependency(principal: Principal = Depends(principal_dependency)) -> Principal:
        allowed = PERMISSIONS.get(principal.role or "", frozenset())
        if "*" not in allowed and permission not in allowed:
            raise forbidden()
        return principal

    return dependency
