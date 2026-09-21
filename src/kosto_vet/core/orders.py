from __future__ import annotations

from enum import StrEnum


class OrderStatus(StrEnum):
    NEW = "new"
    AWAITING_STOCK_CONFIRMATION = "awaiting_stock_confirmation"
    AWAITING_PAYMENT = "awaiting_payment"
    PAID = "paid"
    ASSEMBLING = "assembling"
    READY_FOR_DISPATCH = "ready_for_dispatch"
    SHIPPED = "shipped"
    COMPLETED = "completed"
    CANCELED = "canceled"


class PaymentStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    CREATED = "created"
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    CANCELED = "canceled"
    EXPIRED = "expired"
    FAILED = "failed"


ORDER_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.NEW: frozenset({OrderStatus.ASSEMBLING, OrderStatus.CANCELED}),
    OrderStatus.AWAITING_STOCK_CONFIRMATION: frozenset({OrderStatus.PAID, OrderStatus.CANCELED}),
    OrderStatus.AWAITING_PAYMENT: frozenset({OrderStatus.PAID, OrderStatus.CANCELED}),
    OrderStatus.PAID: frozenset({OrderStatus.ASSEMBLING, OrderStatus.CANCELED}),
    OrderStatus.ASSEMBLING: frozenset({OrderStatus.READY_FOR_DISPATCH, OrderStatus.CANCELED}),
    OrderStatus.READY_FOR_DISPATCH: frozenset({OrderStatus.SHIPPED, OrderStatus.CANCELED}),
    OrderStatus.SHIPPED: frozenset({OrderStatus.COMPLETED, OrderStatus.CANCELED}),
    OrderStatus.COMPLETED: frozenset(),
    OrderStatus.CANCELED: frozenset(),
}


def ensure_transition(current: OrderStatus, target: OrderStatus) -> None:
    if target not in ORDER_TRANSITIONS[current]:
        raise ValueError(f"invalid order transition: {current} -> {target}")
