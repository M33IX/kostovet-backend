from __future__ import annotations

from fastapi.testclient import TestClient

from kosto_vet.bootstrap.api import app


def test_liveness_and_golden_openapi_are_served() -> None:
    with TestClient(app) as client:
        live = client.get("/health/live")
        assert live.status_code == 200
        assert live.json()["ok"] is True
        assert live.headers["X-Request-ID"]

        schema = client.get("/openapi.json")
        assert schema.status_code == 200
        assert (
            len(
                [
                    operation
                    for path in schema.json()["paths"].values()
                    for method, operation in path.items()
                    if method in {"get", "post", "put", "patch", "delete"}
                ]
            )
            == 73
        )


def test_hostile_origin_is_rejected_before_authentication() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/customer/auth/login",
            headers={"Origin": "https://attacker.example"},
            json={"email": "victim@example.test", "password": "irrelevant"},
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ORIGIN_FORBIDDEN"


def test_cookie_authenticated_mutation_requires_origin() -> None:
    with TestClient(app) as client:
        client.cookies.set("kv_customer_refresh", "not-a-real-session")
        response = client.post(
            "/api/v1/customer/auth/logout",
            headers={"X-CSRF-Token": "not-a-real-csrf"},
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ORIGIN_FORBIDDEN"


def test_operational_metrics_are_hidden_from_non_internal_clients() -> None:
    with TestClient(app) as client:
        response = client.get("/metrics")
    assert response.status_code == 404
