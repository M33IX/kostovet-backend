from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml
from fastapi.routing import APIRoute
from openapi_spec_validator import validate

from kosto_vet.api.routes import router
from kosto_vet.bootstrap.api import app

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts" / "openapi.v1.yaml"
EXPECTED_SHA256 = "131d150d931055ec3288b06fa6987d9ebde3b0e3a06d8624cd5ad011d9c2068d"


def contract_document() -> dict[str, Any]:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def operation_ids(document: dict[str, Any]) -> set[str]:
    return {
        operation["operationId"]
        for path in document["paths"].values()
        for method, operation in path.items()
        if method in {"get", "post", "put", "patch", "delete", "head", "options"}
    }


def test_golden_contract_is_complete_and_valid() -> None:
    raw = CONTRACT.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == EXPECTED_SHA256
    document = contract_document()
    validate(document)
    assert document["openapi"].startswith("3.1")
    assert len(operation_ids(document)) == 77


def test_all_contract_operations_are_real_routes() -> None:
    expected = operation_ids(contract_document())
    actual = {
        route.operation_id
        for route in router.routes
        if isinstance(route, APIRoute) and route.include_in_schema
    }
    assert actual == expected


def test_route_methods_and_paths_match_contract() -> None:
    document = contract_document()
    expected = {
        operation["operationId"]: (method.upper(), path)
        for path, path_item in document["paths"].items()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete", "head", "options"}
    }
    actual = {
        route.operation_id: (next(iter(route.methods)), route.path)
        for route in router.routes
        if isinstance(route, APIRoute) and route.include_in_schema
    }
    assert actual == expected


def test_success_status_codes_match_contract() -> None:
    document = contract_document()
    operations = [
        operation
        for path_item in document["paths"].values()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    ]
    expected = {
        operation["operationId"]: min(successes)
        for operation in operations
        if (
            successes := [
                int(status) for status in operation["responses"] if str(status).startswith("2")
            ]
        )
    }
    actual = {
        route.operation_id: route.status_code or 200
        for route in router.routes
        if isinstance(route, APIRoute)
        and route.include_in_schema
        and route.operation_id in expected
    }
    assert actual == expected


def test_operational_endpoints_are_not_public_contract() -> None:
    paths = contract_document()["paths"]
    assert "/metrics" not in paths
    assert "/health/integrations" not in paths


def test_security_is_explicit_and_disabled_features_are_marked() -> None:
    document = contract_document()
    operations = [
        operation
        for path in document["paths"].values()
        for method, operation in path.items()
        if method in {"get", "post", "put", "patch", "delete"}
    ]
    assert all("security" in operation for operation in operations)
    disabled = {
        operation["operationId"]
        for operation in operations
        if operation.get("x-release-status") == "disabled"
    }
    assert disabled == {"requestCustomerPasswordReset", "confirmCustomerPasswordReset"}


def test_runtime_openapi_is_golden_contract() -> None:
    assert app.openapi() == contract_document()
