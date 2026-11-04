"""Exercise the consultation transport without initializing an AI provider."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.consultations import router
from app.schemas.consultation import GenerationCreate, RiskNotice
from app.services.consultation.generations import ConsultationRuntime
from app.services.consultation.identity import PlatformPrincipal, authenticate_platform
from app.services.consultation.repository import ConsultationRepository, RepositoryError


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


def test_same_host_scope_requires_direct_peer_and_one_subject() -> None:
    """Only local platform traffic can supply the user scope without credentials."""
    app = FastAPI()
    app.include_router(router)

    async def admission(principal: PlatformPrincipal, session_id: str, body: GenerationCreate) -> RiskNotice | None:
        assert principal == PlatformPrincipal("platform", "tenant", "user-1")
        raise RepositoryError("SERVICE_UNAVAILABLE")

    app.state.consultation_runtime = ConsultationRuntime(
        ConsultationRepository("unused"), authenticate_platform, admission
    )
    app.state.consultation_local_scope = ("platform", "tenant")
    path = "/sessions/demo/generations"
    body = {"client_message_id": "message", "input_type": "text", "text": "fixture"}
    headers = {"X-Consultation-Subject": "user-1"}
    with TestClient(app, client=("127.0.0.1", 12345)) as local:
        assert local.post(path, json=body, headers=headers).status_code == 503
        assert local.post(path, json=body).status_code == 401
        assert local.post(path, json=body, headers={**headers, "X-Forwarded-For": "127.0.0.1"}).status_code == 401
        assert local.post(path, json=body, headers=list(headers.items()) * 2).status_code == 401
    with TestClient(app, client=("192.0.2.1", 12345)) as remote:
        assert remote.post(path, json=body, headers=headers).status_code == 401
