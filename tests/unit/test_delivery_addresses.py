from __future__ import annotations

import pytest
from pydantic import ValidationError

from kosto_vet.api.schemas import DeliveryAddressCreate, DeliveryAddressUpdate


def test_delivery_address_schema_accepts_checkout_compatible_address() -> None:
    address = DeliveryAddressCreate(
        destination="intercity",
        city="Москва",
        address_line="ул. Пример, д. 1",
        postal_code="101000",
        is_default=True,
    )

    assert address.label == "Основной"
    assert address.destination == "intercity"
    assert address.is_default is True


def test_delivery_address_schema_rejects_unknown_or_incomplete_data() -> None:
    with pytest.raises(ValidationError):
        DeliveryAddressCreate(destination="intercity", city="Москва")
    with pytest.raises(ValidationError):
        DeliveryAddressCreate(
            destination="intercity",
            city="Москва",
            address_line="ул. Пример, д. 1",
            unexpected=True,
        )


def test_delivery_address_update_supports_clearing_optional_fields() -> None:
    update = DeliveryAddressUpdate(comment=None, postal_code=None)

    assert update.model_dump(exclude_unset=True) == {"postal_code": None, "comment": None}
