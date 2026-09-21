from __future__ import annotations

from uuid import UUID

import pytest

from kosto_vet.core.identifiers import new_id
from kosto_vet.core.inventory import StockState, stock_state
from kosto_vet.core.money import Money
from kosto_vet.core.orders import OrderStatus, ensure_transition
from kosto_vet.services.shared import SessionBundle


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


def test_session_bundle_is_constructible() -> None:
    session_id = new_id()
    subject_id = new_id()

    bundle = SessionBundle("access", "refresh", session_id, subject_id)

    assert bundle.access == "access"
    assert bundle.refresh == "refresh"
    assert bundle.session_id == session_id
    assert bundle.subject_id == subject_id
    assert bundle.role is None
