from __future__ import annotations

from enum import StrEnum


class StockState(StrEnum):
    AVAILABLE = "available"
    LOW = "low"
    OUT = "out"
    UNKNOWN = "unknown"


def stock_state(quantity: int, *, stale: bool = False) -> StockState:
    if stale:
        return StockState.UNKNOWN
    if quantity <= 0:
        return StockState.OUT
    if quantity <= 10:
        return StockState.LOW
    return StockState.AVAILABLE
