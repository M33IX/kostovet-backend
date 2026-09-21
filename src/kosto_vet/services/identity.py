from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from kosto_vet.core.errors import DomainError, not_found
from kosto_vet.core.identifiers import new_id
from kosto_vet.core.time import utc_now
from kosto_vet.infrastructure.integrations import (
    encode_pkce_verifier,
)
from kosto_vet.infrastructure.security import (
    hash_password,
    issue_access_token,
    normalize_email,
    random_token,
    token_hash,
    verify_password,
)
from kosto_vet.models import (
    AuditLog,
    CustomerAccount,
    CustomerConsent,
    CustomerCredential,
    CustomerDeliveryAddress,
    CustomerOAuthAccount,
    CustomerSession,
    OAuthTransaction,
    StaffSession,
    StaffUser,
)
from kosto_vet.services.shared import SessionBundle, manager


class IdentityServiceMixin:
    async def start_yandex(
        self,
        session: AsyncSession,
        *,
        return_path: str,
        customer_id: UUID | None = None,
    ) -> str:
        if not return_path.startswith("/") or return_path.startswith("//"):
            raise DomainError("INVALID_REQUEST", "Недопустимый адрес возврата.", 400)
        state = random_token()
        verifier = random_token(48)
        nonce = random_token()
        authorization_url = self.yandex.authorization_url(
            state=state, challenge=encode_pkce_verifier(verifier)
        )
        session.add(
            OAuthTransaction(
                state_hash=token_hash(state),
                verifier_ciphertext=self._fernet.encrypt(verifier.encode()),
                nonce_hash=token_hash(nonce),
                return_path=return_path,
                purpose="link" if customer_id else "login",
                customer_id=customer_id,
                expires_at=utc_now() + timedelta(minutes=10),
            )
        )
        await session.commit()
        return authorization_url

    async def complete_yandex(
        self,
        session: AsyncSession,
        *,
        state: str,
        code: str,
    ) -> tuple[str, SessionBundle | None]:
        transaction = await session.scalar(
            select(OAuthTransaction)
            .where(OAuthTransaction.state_hash == token_hash(state))
            .with_for_update()
        )
        if (
            not transaction
            or transaction.consumed_at is not None
            or transaction.expires_at <= utc_now()
        ):
            raise DomainError("OAUTH_STATE_INVALID", "OAuth-сессия истекла.", 400)
        transaction.consumed_at = utc_now()
        verifier = self._fernet.decrypt(transaction.verifier_ciphertext).decode()
        purpose = transaction.purpose
        link_customer_id = transaction.customer_id
        return_path = transaction.return_path
        await session.commit()
        identity = await self.yandex.identity(code=code, verifier=verifier)
        oauth = await session.scalar(
            select(CustomerOAuthAccount).where(
                CustomerOAuthAccount.provider == "yandex",
                CustomerOAuthAccount.provider_subject == identity["subject"],
            )
        )
        customer: CustomerAccount | None = None
        if oauth:
            customer = await session.get(CustomerAccount, oauth.customer_id)
            oauth.last_login_at = utc_now()
        elif purpose == "link" and link_customer_id:
            customer = await session.get(CustomerAccount, link_customer_id)
        elif identity.get("email"):
            customer = await session.scalar(
                select(CustomerAccount).where(
                    CustomerAccount.normalized_email == normalize_email(identity["email"])
                )
            )
        if not customer:
            email = identity.get("email")
            if not email:
                raise DomainError("OAUTH_EMAIL_REQUIRED", "Яндекс не передал email.", 400)
            customer = CustomerAccount(
                email=email,
                normalized_email=normalize_email(email),
                name=identity.get("name") or "Покупатель",
                customer_type="individual",
                status="active",
                is_email_verified=True,
                email_verified_source="yandex",
            )
            session.add(customer)
            await session.flush()
        if not oauth:
            conflict = await session.scalar(
                select(CustomerOAuthAccount).where(
                    CustomerOAuthAccount.provider == "yandex",
                    CustomerOAuthAccount.customer_id == customer.id,
                )
            )
            if conflict and conflict.provider_subject != identity["subject"]:
                raise DomainError("OAUTH_LINK_CONFLICT", "Яндекс ID уже связан иначе.", 409)
            oauth = CustomerOAuthAccount(
                customer_id=customer.id,
                provider="yandex",
                provider_subject=identity["subject"],
                provider_user_id=identity["user_id"],
                provider_email=identity.get("email"),
                provider_login=identity.get("login"),
                profile_snapshot={
                    "name": identity.get("name"),
                    "login": identity.get("login"),
                },
            )
            session.add(oauth)
            if not customer.is_email_verified:
                customer.is_email_verified = True
                customer.email_verified_source = "yandex"
                await session.execute(
                    update(CustomerSession)
                    .where(
                        CustomerSession.customer_id == customer.id,
                        CustomerSession.revoked_at.is_(None),
                    )
                    .values(revoked_at=utc_now(), revoke_reason="secure_yandex_auto_link")
                )
                credential = await session.scalar(
                    select(CustomerCredential).where(
                        CustomerCredential.customer_id == customer.id,
                        CustomerCredential.kind == "password",
                    )
                )
                if credential:
                    credential.status = "disabled"
                    credential.disabled_reason = "secure_yandex_auto_link"
        session.add(
            AuditLog(
                actor_type="customer",
                actor_id=customer.id,
                action="identity.yandex.link",
                target_type="customer",
                target_id=customer.id,
                after={"provider": "yandex"},
                request_id="oauth",
            )
        )
        if purpose == "link":
            await session.commit()
            return return_path, None
        bundle = await self._create_customer_session(session, customer.id)
        await session.commit()
        return return_path, bundle

    async def unlink_yandex(self, session: AsyncSession, customer_id: UUID) -> None:
        oauth = await session.scalar(
            select(CustomerOAuthAccount).where(
                CustomerOAuthAccount.customer_id == customer_id,
                CustomerOAuthAccount.provider == "yandex",
            )
        )
        if not oauth:
            return
        credential = await session.scalar(
            select(CustomerCredential).where(
                CustomerCredential.customer_id == customer_id,
                CustomerCredential.kind == "password",
                CustomerCredential.status == "active",
            )
        )
        if not credential:
            raise DomainError("LAST_LOGIN_METHOD", "Нельзя удалить единственный способ входа.", 400)
        await session.delete(oauth)
        await session.commit()

    def customer_public(
        self,
        customer: CustomerAccount,
        providers: list[CustomerOAuthAccount] | None = None,
        credential: CustomerCredential | None = None,
    ) -> dict[str, Any]:
        auth_providers = []
        if credential:
            auth_providers.append(
                {
                    "provider": "password",
                    "status": credential.status,
                    "linked_at": credential.created_at.isoformat(),
                }
            )
        for provider in providers or []:
            auth_providers.append(
                {
                    "provider": provider.provider,
                    "status": "active",
                    "linked_at": provider.linked_at.isoformat(),
                }
            )
        return {
            "id": str(customer.id),
            "name": customer.name,
            "phone": customer.phone,
            "email": customer.email,
            "customer_type": customer.customer_type,
            "company_name": customer.company_name,
            "inn": customer.inn,
            "documents_email": customer.documents_email,
            "is_email_verified": customer.is_email_verified,
            "is_phone_verified": customer.is_phone_verified,
            "manager": manager(self.settings),
            "auth_providers": auth_providers,
        }

    async def _customer_payload(
        self, session: AsyncSession, customer: CustomerAccount
    ) -> dict[str, Any]:
        providers = (
            await session.scalars(
                select(CustomerOAuthAccount).where(CustomerOAuthAccount.customer_id == customer.id)
            )
        ).all()
        credential = await session.scalar(
            select(CustomerCredential).where(
                CustomerCredential.customer_id == customer.id, CustomerCredential.kind == "password"
            )
        )
        return self.customer_public(customer, list(providers), credential)

    async def _create_customer_session(
        self, session: AsyncSession, customer_id: UUID
    ) -> SessionBundle:
        refresh = random_token()
        family = new_id()
        row = CustomerSession(
            customer_id=customer_id,
            family_id=family,
            token_hash=token_hash(refresh),
            expires_at=utc_now() + timedelta(seconds=self.settings.customer_refresh_ttl_seconds),
        )
        session.add(row)
        await session.flush()
        access = issue_access_token(
            self.settings, subject=customer_id, session_id=row.id, audience="customer"
        )
        return SessionBundle(access, refresh, row.id, customer_id)

    async def register(
        self, session: AsyncSession, payload: dict[str, Any], *, request_id: str
    ) -> tuple[dict[str, Any], SessionBundle]:
        normalized = normalize_email(payload["email"])
        if await session.scalar(
            select(CustomerAccount.id).where(CustomerAccount.normalized_email == normalized)
        ):
            raise DomainError("ACCOUNT_EXISTS", "Аккаунт уже существует.", 409)
        customer = CustomerAccount(
            email=payload["email"],
            normalized_email=normalized,
            phone=payload.get("phone"),
            normalized_phone=payload.get("phone"),
            name=payload["name"],
            customer_type=payload.get("customer_type", "individual"),
            company_name=payload.get("company_name"),
            inn=payload.get("inn"),
            documents_email=payload.get("documents_email"),
            status="active",
        )
        session.add(customer)
        await session.flush()
        session.add(
            CustomerConsent(
                customer_id=customer.id,
                subject_ref=f"customer:{customer.id}",
                consent_type="personal_data",
                document_version="demo-v1",
                accepted=True,
                source="web_registration",
                request_id=request_id,
            )
        )
        credential = CustomerCredential(
            customer_id=customer.id,
            password_hash=hash_password(payload["password"]),
            status="active",
        )
        session.add(credential)
        bundle = await self._create_customer_session(session, customer.id)
        await session.commit()
        return {"customer": self.customer_public(customer, [], credential)}, bundle

    async def customer_login(
        self, session: AsyncSession, email: str, password: str
    ) -> tuple[dict[str, Any], SessionBundle]:
        customer = await session.scalar(
            select(CustomerAccount).where(
                CustomerAccount.normalized_email == normalize_email(email)
            )
        )
        credential = (
            await session.scalar(
                select(CustomerCredential).where(
                    CustomerCredential.customer_id == customer.id,
                    CustomerCredential.kind == "password",
                )
            )
            if customer
            else None
        )
        if (
            not customer
            or customer.status != "active"
            or not credential
            or credential.status != "active"
            or not verify_password(password, credential.password_hash if credential else None)
        ):
            if not credential:
                verify_password(password, None)
            raise DomainError("AUTH_INVALID", "Неверный email или пароль.", 401)
        bundle = await self._create_customer_session(session, customer.id)
        await session.commit()
        return {"customer": await self._customer_payload(session, customer)}, bundle

    async def staff_login(
        self, session: AsyncSession, email: str, password: str
    ) -> tuple[dict[str, Any], SessionBundle]:
        staff = await session.scalar(
            select(StaffUser).where(StaffUser.normalized_email == normalize_email(email))
        )
        if (
            not staff
            or staff.status != "active"
            or not verify_password(password, staff.password_hash if staff else None)
        ):
            if not staff:
                verify_password(password, None)
            raise DomainError("AUTH_INVALID", "Неверный email или пароль.", 401)
        refresh = random_token()
        family = new_id()
        row = StaffSession(
            staff_id=staff.id,
            family_id=family,
            token_hash=token_hash(refresh),
            expires_at=utc_now() + timedelta(seconds=self.settings.staff_refresh_ttl_seconds),
        )
        session.add(row)
        await session.flush()
        access = issue_access_token(
            self.settings, subject=staff.id, session_id=row.id, audience="staff", role=staff.role
        )
        await session.commit()
        return {
            "user": {"id": str(staff.id), "email": staff.email, "role": staff.role}
        }, SessionBundle(access, refresh, row.id, staff.id, staff.role)

    async def rotate_session(
        self, session: AsyncSession, *, refresh_token: str, audience: str
    ) -> tuple[dict[str, Any], SessionBundle]:
        if audience == "customer":
            row = await session.scalar(
                select(CustomerSession)
                .where(CustomerSession.token_hash == token_hash(refresh_token))
                .with_for_update()
            )
        else:
            row = await session.scalar(
                select(StaffSession)
                .where(StaffSession.token_hash == token_hash(refresh_token))
                .with_for_update()
            )
        if not row:
            raise DomainError("SESSION_EXPIRED", "Сессия истекла.", 401)
        if row.revoked_at is not None:
            if audience == "customer":
                await session.execute(
                    update(CustomerSession)
                    .where(
                        CustomerSession.family_id == row.family_id,
                        CustomerSession.revoked_at.is_(None),
                    )
                    .values(revoked_at=utc_now(), revoke_reason="refresh_replay")
                )
            else:
                await session.execute(
                    update(StaffSession)
                    .where(
                        StaffSession.family_id == row.family_id,
                        StaffSession.revoked_at.is_(None),
                    )
                    .values(revoked_at=utc_now(), revoke_reason="refresh_replay")
                )
            await session.commit()
            raise DomainError(
                "SESSION_EXPIRED", "Обнаружено повторное использование refresh token.", 401
            )
        if row.expires_at <= utc_now():
            row.revoked_at = utc_now()
            row.revoke_reason = "expired"
            await session.commit()
            raise DomainError("SESSION_EXPIRED", "Сессия истекла.", 401)
        row.revoked_at = utc_now()
        row.revoke_reason = "rotated"
        refresh = random_token()
        new_row: CustomerSession | StaffSession
        if isinstance(row, CustomerSession):
            new_row = CustomerSession(
                customer_id=row.customer_id,
                family_id=row.family_id,
                token_hash=token_hash(refresh),
                expires_at=utc_now()
                + timedelta(seconds=self.settings.customer_refresh_ttl_seconds),
            )
            subject_id = row.customer_id
            role = None
            customer = await session.get(CustomerAccount, subject_id)
            if not customer:
                raise DomainError("SESSION_EXPIRED", "Аккаунт не найден.", 401)
            payload = {"customer": await self._customer_payload(session, customer)}
        else:
            new_row = StaffSession(
                staff_id=row.staff_id,
                family_id=row.family_id,
                token_hash=token_hash(refresh),
                expires_at=utc_now() + timedelta(seconds=self.settings.staff_refresh_ttl_seconds),
            )
            subject_id = row.staff_id
            staff = await session.get(StaffUser, subject_id)
            if not staff:
                raise DomainError("SESSION_EXPIRED", "Сотрудник не найден.", 401)
            role = staff.role
            payload = {"user": {"id": str(staff.id), "email": staff.email, "role": staff.role}}
        session.add(new_row)
        await session.flush()
        access = issue_access_token(
            self.settings, subject=subject_id, session_id=new_row.id, audience=audience, role=role
        )
        await session.commit()
        return payload, SessionBundle(access, refresh, new_row.id, subject_id, role)

    async def revoke_refresh(
        self, session: AsyncSession, *, refresh_token: str | None, audience: str
    ) -> None:
        if refresh_token:
            if audience == "customer":
                row = await session.scalar(
                    select(CustomerSession).where(
                        CustomerSession.token_hash == token_hash(refresh_token)
                    )
                )
            else:
                row = await session.scalar(
                    select(StaffSession).where(StaffSession.token_hash == token_hash(refresh_token))
                )
            if row and row.revoked_at is None:
                if audience == "customer":
                    await session.execute(
                        update(CustomerSession)
                        .where(
                            CustomerSession.family_id == row.family_id,
                            CustomerSession.revoked_at.is_(None),
                        )
                        .values(revoked_at=utc_now(), revoke_reason="logout")
                    )
                else:
                    await session.execute(
                        update(StaffSession)
                        .where(
                            StaffSession.family_id == row.family_id,
                            StaffSession.revoked_at.is_(None),
                        )
                        .values(revoked_at=utc_now(), revoke_reason="logout")
                    )
                await session.commit()

    async def customer_me(self, session: AsyncSession, customer_id: UUID) -> dict[str, Any]:
        customer = await session.get(CustomerAccount, customer_id)
        if not customer:
            raise not_found()
        return {"customer": await self._customer_payload(session, customer)}

    async def update_customer(
        self, session: AsyncSession, customer_id: UUID, payload: dict[str, Any]
    ) -> dict[str, Any]:
        customer = await session.get(CustomerAccount, customer_id, with_for_update=True)
        if not customer:
            raise not_found()
        expected = payload.pop("version", None)
        if expected is not None and expected != customer.version:
            raise DomainError("VERSION_CONFLICT", "Профиль был изменён.", 409)
        for key, value in payload.items():
            if value is not None and hasattr(customer, key):
                setattr(customer, key, value)
        customer.version += 1
        await session.commit()
        return {"customer": await self._customer_payload(session, customer)}

    @staticmethod
    def delivery_address_payload(address: CustomerDeliveryAddress) -> dict[str, Any]:
        return {
            "id": str(address.id),
            "label": address.label,
            "destination": address.destination,
            "city": address.city,
            "address_line": address.address_line,
            "postal_code": address.postal_code,
            "comment": address.comment,
            "is_default": address.is_default,
            "version": address.version,
            "created_at": address.created_at.isoformat(),
            "updated_at": address.updated_at.isoformat(),
        }

    async def list_delivery_addresses(
        self, session: AsyncSession, customer_id: UUID
    ) -> dict[str, Any]:
        addresses = (
            await session.scalars(
                select(CustomerDeliveryAddress)
                .where(CustomerDeliveryAddress.customer_id == customer_id)
                .order_by(
                    CustomerDeliveryAddress.is_default.desc(),
                    CustomerDeliveryAddress.updated_at.desc(),
                )
            )
        ).all()
        return {"items": [self.delivery_address_payload(address) for address in addresses]}

    async def create_delivery_address(
        self, session: AsyncSession, customer_id: UUID, payload: dict[str, Any]
    ) -> dict[str, Any]:
        customer = await session.get(CustomerAccount, customer_id, with_for_update=True)
        if not customer:
            raise not_found()
        requested_default = bool(payload.pop("is_default", False))
        current_default = await session.scalar(
            select(CustomerDeliveryAddress.id).where(
                CustomerDeliveryAddress.customer_id == customer_id,
                CustomerDeliveryAddress.is_default.is_(True),
            )
        )
        is_default = requested_default or current_default is None
        if is_default:
            await session.execute(
                update(CustomerDeliveryAddress)
                .where(CustomerDeliveryAddress.customer_id == customer_id)
                .values(is_default=False)
            )
        address = CustomerDeliveryAddress(
            customer_id=customer_id,
            is_default=is_default,
            **payload,
        )
        session.add(address)
        await session.flush()
        await session.commit()
        return self.delivery_address_payload(address)

    async def update_delivery_address(
        self,
        session: AsyncSession,
        customer_id: UUID,
        address_id: UUID,
        payload: dict[str, Any],
        expected_version: int,
    ) -> dict[str, Any]:
        customer = await session.get(CustomerAccount, customer_id, with_for_update=True)
        if not customer:
            raise not_found()
        address = await session.scalar(
            select(CustomerDeliveryAddress)
            .where(
                CustomerDeliveryAddress.id == address_id,
                CustomerDeliveryAddress.customer_id == customer_id,
            )
            .with_for_update()
        )
        if not address:
            raise not_found()
        if address.version != expected_version:
            raise DomainError("VERSION_CONFLICT", "Адрес был изменён.", 409)
        if not payload:
            raise DomainError("INVALID_REQUEST", "Не переданы поля для изменения.", 400)
        requested_default = payload.pop("is_default", None)
        if requested_default is True:
            await session.execute(
                update(CustomerDeliveryAddress)
                .where(
                    CustomerDeliveryAddress.customer_id == customer_id,
                    CustomerDeliveryAddress.id != address.id,
                )
                .values(is_default=False)
            )
            address.is_default = True
        elif requested_default is False and address.is_default:
            raise DomainError(
                "DEFAULT_ADDRESS_REQUIRED", "Сначала выберите другой адрес по умолчанию.", 409
            )
        required_fields = {"label", "destination", "city", "address_line"}
        if any(payload.get(field) is None for field in required_fields & payload.keys()):
            raise DomainError(
                "INVALID_REQUEST", "Обязательные поля адреса не могут быть пустыми.", 400
            )
        for field, value in payload.items():
            setattr(address, field, value)
        address.version += 1
        await session.commit()
        return self.delivery_address_payload(address)

    async def delete_delivery_address(
        self, session: AsyncSession, customer_id: UUID, address_id: UUID, expected_version: int
    ) -> None:
        customer = await session.get(CustomerAccount, customer_id, with_for_update=True)
        if not customer:
            raise not_found()
        address = await session.scalar(
            select(CustomerDeliveryAddress)
            .where(
                CustomerDeliveryAddress.id == address_id,
                CustomerDeliveryAddress.customer_id == customer_id,
            )
            .with_for_update()
        )
        if not address:
            raise not_found()
        if address.version != expected_version:
            raise DomainError("VERSION_CONFLICT", "Адрес был изменён.", 409)
        if address.is_default:
            address.is_default = False
            await session.flush()
            replacement = await session.scalar(
                select(CustomerDeliveryAddress)
                .where(
                    CustomerDeliveryAddress.customer_id == customer_id,
                    CustomerDeliveryAddress.id != address.id,
                )
                .order_by(CustomerDeliveryAddress.updated_at.desc())
                .with_for_update()
                .limit(1)
            )
            if replacement:
                replacement.is_default = True
                replacement.version += 1
        await session.delete(address)
        await session.commit()
