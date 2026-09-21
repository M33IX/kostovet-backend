from __future__ import annotations

from kosto_vet.services.admin import AdminServiceMixin
from kosto_vet.services.catalog import CatalogServiceMixin
from kosto_vet.services.content import ContentServiceMixin
from kosto_vet.services.customer import CustomerServiceMixin
from kosto_vet.services.identity import IdentityServiceMixin
from kosto_vet.services.orders import OrderServiceMixin
from kosto_vet.services.shared import SessionBundle, SharedServiceMixin, manager, money


class ApplicationService(
    SharedServiceMixin,
    CatalogServiceMixin,
    IdentityServiceMixin,
    CustomerServiceMixin,
    OrderServiceMixin,
    ContentServiceMixin,
    AdminServiceMixin,
):
    """Compatibility facade composed from business-focused service mixins."""


__all__ = ["ApplicationService", "SessionBundle", "manager", "money"]
