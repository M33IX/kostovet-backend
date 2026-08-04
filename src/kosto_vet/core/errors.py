from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class DomainError(Exception):
    code: str
    message: str
    status_code: int = 400
    retryable: bool = False
    field_errors: list[dict[str, str]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


def not_found(code: str = "RESOURCE_NOT_FOUND", message: str = "Ресурс не найден.") -> DomainError:
    return DomainError(code, message, 404)


def forbidden(message: str = "Недостаточно прав.") -> DomainError:
    return DomainError("FORBIDDEN", message, 403)


def feature_disabled(feature: str) -> DomainError:
    return DomainError(
        "FEATURE_DISABLED",
        f"Функция {feature} отключена в demo.",
        503,
    )
