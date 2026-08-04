from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid7


def new_id() -> UUID:
    return uuid7()


def utc_now() -> datetime:
    return datetime.now(UTC)


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


class StockState(StrEnum):
    AVAILABLE = "available"
    LOW = "low"
    OUT = "out"
    UNKNOWN = "unknown"


class StaffRole(StrEnum):
    ADMIN = "admin"
    MANAGER = "manager"
    CONTENT = "content"
    READONLY = "readonly"


@dataclass(frozen=True, slots=True)
class Money:
    amount: int
    currency: str = "RUB"

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise ValueError("money amount cannot be negative")
        if self.currency != "RUB":
            raise ValueError("demo supports RUB only")


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


def stock_state(quantity: int, *, stale: bool = False) -> StockState:
    if stale:
        return StockState.UNKNOWN
    if quantity <= 0:
        return StockState.OUT
    if quantity <= 10:
        return StockState.LOW
    return StockState.AVAILABLE


def ensure_transition(current: OrderStatus, target: OrderStatus) -> None:
    if target not in ORDER_TRANSITIONS[current]:
        raise ValueError(f"invalid order transition: {current} -> {target}")
