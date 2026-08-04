from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from kosto_vet.bootstrap.settings import Mode, Settings
from kosto_vet.core.errors import DomainError, feature_disabled


def _hash(algorithm: str, value: str) -> str:
    try:
        digest = hashlib.new(algorithm.lower())
    except ValueError as exc:
        raise DomainError(
            "INTEGRATION_CONFIG_INVALID", "Неизвестный алгоритм подписи.", 500
        ) from exc
    digest.update(value.encode())
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class RobokassaConfirmation:
    url: str
    fields: dict[str, str]


class RobokassaAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def create_confirmation(
        self,
        *,
        inv_id: int,
        amount_minor: int,
        description: str,
        email: str | None,
        payment_method: str,
        order_public_id: str,
        expiration: str,
    ) -> RobokassaConfirmation:
        if self.settings.robokassa_mode is Mode.DISABLED:
            raise feature_disabled("robokassa")
        login = self.settings.robokassa_merchant_login
        password = self.settings.robokassa_password1
        if not login or not password:
            raise DomainError("PAYMENT_PROVIDER_ERROR", "Robokassa не настроена.", 502, True)
        out_sum = f"{Decimal(amount_minor) / Decimal(100):.2f}"
        shp = {"Shp_order": order_public_id}
        canonical = ":".join(
            [login, out_sum, str(inv_id), password.get_secret_value()]
            + [f"{key}={shp[key]}" for key in sorted(shp)]
        )
        fields = {
            "MerchantLogin": login,
            "OutSum": out_sum,
            "InvId": str(inv_id),
            "Description": description[:100],
            "SignatureValue": _hash(self.settings.robokassa_hash_algorithm, canonical),
            "ExpirationDate": expiration,
            "Culture": "ru",
            "IsTest": "1" if self.settings.robokassa_mode is Mode.SANDBOX else "0",
            "PaymentMethods": "SBP" if payment_method == "sbp" else "BankCard",
            **shp,
        }
        if email:
            fields["Email"] = email
        return RobokassaConfirmation(self.settings.robokassa_payment_url, fields)

    def verify_result(self, values: dict[str, str]) -> bool:
        password = self.settings.robokassa_password2
        if not password:
            return False
        out_sum = values.get("OutSum", "")
        inv_id = values.get("InvId", "")
        shp = {key: value for key, value in values.items() if key.startswith("Shp_")}
        canonical = ":".join(
            [out_sum, inv_id, password.get_secret_value()]
            + [f"{key}={shp[key]}" for key in sorted(shp)]
        )
        expected = _hash(self.settings.robokassa_hash_algorithm, canonical)
        return hmac.compare_digest(expected.casefold(), values.get("SignatureValue", "").casefold())

    def operation_state_request(self, inv_id: int) -> tuple[str, dict[str, str]]:
        if self.settings.robokassa_mode is not Mode.PRODUCTION:
            raise feature_disabled("robokassa_op_state_ext_in_sandbox")
        login = self.settings.robokassa_merchant_login
        password = self.settings.robokassa_password2
        if not login or not password:
            raise DomainError("PAYMENT_PROVIDER_ERROR", "Robokassa не настроена.", 502)
        signature = _hash(
            self.settings.robokassa_hash_algorithm,
            f"{login}:{inv_id}:{password.get_secret_value()}",
        )
        return (
            "https://auth.robokassa.ru/Merchant/WebService/Service.asmx/OpStateExt",
            {"MerchantLogin": login, "InvoiceID": str(inv_id), "Signature": signature},
        )


class YandexIdAdapter:
    authorize_url = "https://oauth.yandex.ru/authorize"
    exchange_url = "https://oauth.yandex.ru/token"
    profile_url = "https://login.yandex.ru/info"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def authorization_url(self, *, state: str, challenge: str) -> str:
        if self.settings.yandex_oauth_mode is Mode.DISABLED:
            raise feature_disabled("yandex_oauth")
        return f"{self.authorize_url}?{urlencode({'response_type': 'code', 'client_id': self.settings.yandex_oauth_client_id or '', 'redirect_uri': self.settings.yandex_oauth_redirect_uri, 'scope': self.settings.yandex_oauth_scopes, 'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'})}"

    async def identity(self, *, code: str, verifier: str) -> dict[str, Any]:
        secret = self.settings.yandex_oauth_client_secret
        if not self.settings.yandex_oauth_client_id or not secret:
            raise DomainError("OAUTH_CONFIG_INVALID", "Яндекс ID не настроен.", 503)
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
            token_response = await client.post(
                self.exchange_url,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": self.settings.yandex_oauth_client_id,
                    "client_secret": secret.get_secret_value(),
                    "code_verifier": verifier,
                },
            )
            token_response.raise_for_status()
            token = token_response.json()["access_token"]
            profile_response = await client.get(
                self.profile_url,
                params={"format": "json"},
                headers={"Authorization": f"OAuth {token}"},
            )
            profile_response.raise_for_status()
        profile = profile_response.json()
        return {
            "subject": str(profile.get("psuid") or profile["id"]),
            "user_id": str(profile["id"]),
            "email": profile.get("default_email"),
            "login": profile.get("login"),
            "name": profile.get("real_name") or profile.get("display_name") or profile.get("login"),
        }


class MoySkladAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def fetch_pages(
        self,
        path: str,
        *,
        limit: int = 100,
        params: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        if self.settings.moysklad_mode is Mode.DISABLED:
            raise feature_disabled("moysklad")
        token = self.settings.moysklad_access_token
        if not token:
            raise DomainError("INTEGRATION_CONFIG_INVALID", "МойСклад не настроен.", 503)
        rows: list[dict[str, Any]] = []
        offset = 0
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
            while True:
                response = await client.get(
                    f"{self.settings.moysklad_api_base_url}/{path.lstrip('/')}",
                    params={"limit": limit, "offset": offset, **(params or {})},
                    headers={"Authorization": f"Bearer {token.get_secret_value()}"},
                )
                if response.status_code == 429:
                    raise DomainError(
                        "PROVIDER_RATE_LIMITED", "МойСклад ограничил запросы.", 503, True
                    )
                response.raise_for_status()
                remaining = response.headers.get("X-RateLimit-Remaining")
                if remaining == "0":
                    retry_after = min(30, int(response.headers.get("Retry-After", "1")))
                    await asyncio.sleep(max(1, retry_after))
                page = response.json().get("rows", [])
                rows.extend(page)
                if len(page) < limit:
                    break
                offset += limit
        return rows


def encode_pkce_verifier(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )


def safe_payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
