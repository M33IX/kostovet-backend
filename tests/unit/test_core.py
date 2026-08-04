from __future__ import annotations

from uuid import UUID

import pytest

from kosto_vet.core.types import (
    Money,
    OrderStatus,
    StockState,
    ensure_transition,
    new_id,
    stock_state,
)


def test_uuid7_is_generated() -> None:
    identifier = new_id()
    assert isinstance(identifier, UUID)
    assert identifier.version == 7


@pytest.mark.parametrize(
    ("quantity", "stale", "expected"),
    [
        (11, False, StockState.AVAILABLE),
        (10, False, StockState.LOW),
        (1, False, StockState.LOW),
        (0, False, StockState.OUT),
        (-1, False, StockState.OUT),
        (100, True, StockState.UNKNOWN),
    ],
)
def test_stock_state(quantity: int, stale: bool, expected: StockState) -> None:
    assert stock_state(quantity, stale=stale) is expected


def test_money_is_integer_minor_units() -> None:
    assert Money(114_300).amount == 114_300
    with pytest.raises(ValueError):
        Money(-1)
    with pytest.raises(ValueError):
        Money(100, "USD")


def test_order_state_machine() -> None:
    ensure_transition(OrderStatus.NEW, OrderStatus.ASSEMBLING)
    ensure_transition(OrderStatus.PAID, OrderStatus.ASSEMBLING)
    ensure_transition(OrderStatus.SHIPPED, OrderStatus.COMPLETED)
    with pytest.raises(ValueError):
        ensure_transition(OrderStatus.NEW, OrderStatus.SHIPPED)
    with pytest.raises(ValueError):
        ensure_transition(OrderStatus.COMPLETED, OrderStatus.CANCELED)
