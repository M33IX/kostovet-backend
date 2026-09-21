from __future__ import annotations

from enum import StrEnum


class StaffRole(StrEnum):
    ADMIN = "admin"
    MANAGER = "manager"
    CONTENT = "content"
    READONLY = "readonly"
