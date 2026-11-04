"""Exercise the new deployment modes before any provider or database call."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from app.consultation_main import create_app
from app.core.config import Environment, settings
from app.core.langgraph.consultation_graph import ScopedInput
from app.schemas.consultation import GenerationCreate, RiskNotice
from app.services.consultation import deployment
from app.services.consultation.deployment import (
    ConsultationDeploymentSettings,
    DeploymentPolicy,
    consultation_deployment,
)
from app.services.consultation.identity import PlatformPrincipal
from app.services.consultation.openai_compatible import OpenAICompatibleModel
from app.services.consultation.safety import SafetyDecision, ValidatedImage


async def _classify(text: str, images: tuple[ValidatedImage, ...]) -> SafetyDecision:
    notice = RiskNotice(status="human_handoff_required", message="Synthetic approved notice", resources=[])
    return SafetyDecision("high_risk", notice) if text == "urgent" else SafetyDecision("normal")


async def _in_scope(scoped: ScopedInput) -> bool:
    return bool(scoped.text)


def build_policy(model: OpenAICompatibleModel, transport: httpx.AsyncBaseTransport) -> DeploymentPolicy:
    """Provide the synthetic policy for the configured deployment test."""
    return DeploymentPolicy(_classify, _in_scope)


def test_disabled_app_omits_consultation_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dedicated entrypoint enables no API until a mode is selected."""
    monkeypatch.delenv("CONSULTATION_MODE", raising=False)
    disabled = create_app(ConsultationDeploymentSettings())
    assert disabled.openapi()["paths"] == {}
    enabled = create_app(ConsultationDeploymentSettings(mode="fixture"))
    assert f"{settings.API_V1_STR}/consultations/generations/{{generation_id}}/events" in enabled.openapi()["paths"]


def test_configured_mode_requires_reviewed_policy_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """Incomplete real config fails during startup before consulting the database."""
    monkeypatch.delenv("CONSULTATION_POLICY_MODULE", raising=False)

    async def start() -> None:
        async with consultation_deployment(ConsultationDeploymentSettings(mode="configured")):
            pytest.fail("configured mode started with incomplete settings")

    with pytest.raises(ValidationError) as failure:
        asyncio.run(start())
    assert any(error["loc"] == ("policy_module",) for error in failure.value.errors())


def test_fixture_mode_rejects_unisolated_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Synthetic providers cannot take over an ordinary consultation database."""
    monkeypatch.setattr(settings, "ENVIRONMENT", Environment.DEVELOPMENT)
    monkeypatch.setattr(settings, "POSTGRES_DB", "normal_database")

    async def start() -> None:
        async with consultation_deployment(ConsultationDeploymentSettings(mode="fixture")):
            pytest.fail("fixture mode started against an ordinary database")

    with pytest.raises(RuntimeError, match=r"dedicated \*_integration database"):
        asyncio.run(start())


def test_configured_mode_uses_deployment_policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Real mode uses the configured safety classifier before any model call."""
    catalog = tmp_path / "catalog.json"
    catalog.write_text("[]", encoding="utf-8")
    for name, value in {
        "CALLER_ID": "platform",
        "TENANT_ID": "tenant",
        "POLICY_MODULE": __name__,
        "MODEL_BASE_URL": "http://127.0.0.1:8100/v1",
        "MODEL_ID": "integration-model",
        "RAGFLOW_BASE_URL": "http://127.0.0.1:8101",
        "RAGFLOW_API_KEY": "synthetic-key",
        "RAGFLOW_DATASET_IDS": '["integration-dataset"]',
        "CATALOG_PATH": str(catalog),
        "PUBLISHED_VERSION": "integration-release",
    }.items():
        monkeypatch.setenv(f"CONSULTATION_{name}", value)
    monkeypatch.setattr(deployment, "_check_schema", AsyncMock())

    async def start() -> None:
        async with consultation_deployment(ConsultationDeploymentSettings(mode="configured")) as (components, scope):
            assert scope == ("platform", "tenant")
            decision = await components.runtime.check_admission(
                PlatformPrincipal(*scope, "user"),
                "session",
                GenerationCreate(client_message_id="msg", input_type="text", text="urgent"),
            )
            assert decision is not None and decision.status == "human_handoff_required"

    asyncio.run(start())
