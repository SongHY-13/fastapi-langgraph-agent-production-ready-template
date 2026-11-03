"""Exercise the consultation transport without initializing an AI provider."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.consultations import router


def test_default_auth_and_validation_are_sanitized() -> None:
    """The enabled router still requires a trusted runtime and never echoes input."""
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        refused = client.get("/generations/unknown/events", headers={"Authorization": "Bearer secret"})
        invalid = client.post("/sessions/demo/generations", json={"text": "private-marker"})
    assert refused.status_code == 401
    assert refused.json()["code"] == "UNAUTHORIZED"
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "INVALID_REQUEST"
    assert "private-marker" not in invalid.text
    assert "secret" not in refused.text


def test_authorization_is_declared_as_a_header_on_all_routes() -> None:
    """Ensure FastAPI binds the shared authorization alias from the header."""
    app = FastAPI()
    app.include_router(router)
    operations = [operation for path in app.openapi()["paths"].values() for operation in path.values()]
    assert len(operations) == 5
    for operation in operations:
        assert any(
            parameter["in"] == "header" and parameter["name"] == "authorization"
            for parameter in operation["parameters"]
        )
    regeneration = app.openapi()["paths"]["/sessions/{session_id}/answers/{answer_id}/regenerate"]["post"]
    assert any(
        parameter["in"] == "header" and parameter["name"].lower() == "idempotency-key"
        for parameter in regeneration["parameters"]
    )
