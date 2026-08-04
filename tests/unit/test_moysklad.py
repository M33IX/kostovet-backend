from __future__ import annotations

from kosto_vet.services.moysklad import _integer_quantity, _price_minor


def test_moysklad_quantities_are_integral_and_safe() -> None:
    assert _integer_quantity("12.9") == 12
    assert _integer_quantity("NaN") == 0
    assert _integer_quantity("invalid") == 0


def test_configured_price_type_is_selected_in_minor_units() -> None:
    row = {
        "salePrices": [
            {"value": 100, "priceType": {"meta": {"href": "https://ms/entity/other"}}},
            {"value": 114300, "priceType": {"meta": {"href": "https://ms/entity/demo"}}},
        ]
    }
    assert _price_minor(row, "demo") == 114_300
    assert _price_minor(row, "missing") is None
