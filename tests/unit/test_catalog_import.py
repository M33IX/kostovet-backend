from __future__ import annotations

import json
from pathlib import Path

import pytest

from kosto_vet.cli import _catalog_allowlist, _catalog_manifest


def test_catalog_manifest_requires_explicit_structured_data(tmp_path: Path) -> None:
    manifest = tmp_path / "catalog.json"
    manifest.write_text(
        json.dumps(
            {
                "warehouse": {"external_id": "warehouse-1", "name": "Основной"},
                "categories": [{"slug": "plates", "title": "Пластины"}],
                "products": [
                    {
                        "external_id": "product-1",
                        "category": "plates",
                        "article": "KV-1",
                        "slug": "plate-1",
                        "name": "Пластина",
                        "price_minor": 100,
                        "stock_quantity": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    warehouse, categories, products = _catalog_manifest(manifest)

    assert warehouse["external_id"] == "warehouse-1"
    assert categories[0]["slug"] == "plates"
    assert products[0]["external_id"] == "product-1"


def test_catalog_manifest_rejects_unstructured_input(tmp_path: Path) -> None:
    manifest = tmp_path / "catalog.json"
    manifest.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="JSON object"):
        _catalog_manifest(manifest)


def test_allowlist_supports_reviewed_text_or_json(tmp_path: Path) -> None:
    text_allowlist = tmp_path / "allowlist.txt"
    text_allowlist.write_text("# approved\nproduct-1\nproduct-2\n", encoding="utf-8")
    json_allowlist = tmp_path / "allowlist.json"
    json_allowlist.write_text('["product-3"]', encoding="utf-8")

    assert _catalog_allowlist(text_allowlist) == {"product-1", "product-2"}
    assert _catalog_allowlist(json_allowlist) == {"product-3"}
