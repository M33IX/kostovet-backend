from __future__ import annotations

import pytest

from kosto_vet.bootstrap.settings import Settings
from kosto_vet.core.errors import DomainError
from kosto_vet.services.application import ApplicationService


def test_markdown_is_normalized_and_raw_html_is_rejected() -> None:
    service = ApplicationService(Settings())
    assert service._validate_markdown("  # Заголовок\r\n") == "# Заголовок"
    with pytest.raises(DomainError, match="HTML"):
        service._validate_markdown("<script>alert(1)</script>")
