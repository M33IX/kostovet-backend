from __future__ import annotations

import hashlib

import pytest
from pydantic import SecretStr

from kosto_vet.bootstrap.settings import Mode, Settings
from kosto_vet.core.errors import DomainError
from kosto_vet.infrastructure.integrations import RobokassaAdapter


def robokassa_settings() -> Settings:
    return Settings(
        robokassa_mode=Mode.SANDBOX,
        robokassa_merchant_login="merchant",
        robokassa_password1=SecretStr("password-one"),
        robokassa_password2=SecretStr("password-two"),
    )


def test_robokassa_confirmation_uses_exact_decimal_and_canonical_shp() -> None:
    confirmation = RobokassaAdapter(robokassa_settings()).create_confirmation(
        inv_id=42,
        amount_minor=12_345,
        description="Заказ KV-42",
        email="vet@example.test",
        payment_method="sbp",
        order_public_id="KV-42",
        expiration="2026-08-01T12:00:00+00:00",
    )
    fields = confirmation.fields
    assert fields["OutSum"] == "123.45"
    assert fields["Shp_order"] == "KV-42"
    assert fields["PaymentMethods"] == "SBP"
    canonical = "merchant:123.45:42:password-one:Shp_order=KV-42"
    assert fields["SignatureValue"] == hashlib.sha256(canonical.encode()).hexdigest()


def test_robokassa_result_signature_is_constant_time_validated() -> None:
    values = {"OutSum": "123.45", "InvId": "42", "Shp_order": "KV-42"}
    canonical = "123.45:42:password-two:Shp_order=KV-42"
    values["SignatureValue"] = hashlib.sha256(canonical.encode()).hexdigest().upper()
    adapter = RobokassaAdapter(robokassa_settings())
    assert adapter.verify_result(values)
    values["OutSum"] = "123.46"
    assert not adapter.verify_result(values)


def test_op_state_is_blocked_in_sandbox() -> None:
    with pytest.raises(DomainError) as error:
        RobokassaAdapter(robokassa_settings()).operation_state_request(42)
    assert error.value.code == "FEATURE_DISABLED"
