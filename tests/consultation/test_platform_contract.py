"""Synthetic application lifecycle contract for consultation composition."""

import asyncio
from typing import Never

import httpx
import pytest
from fastapi import FastAPI

from app.core.langgraph.consultation_graph import ConsultationWorkflow
from app.schemas.consultation import GenerationCreate, ImageAttachment, RiskNotice
from app.services.consultation.deepseek import DeepSeek
from app.services.consultation.generations import ConsultationRuntime
from app.services.consultation.identity import PlatformPrincipal, authenticate_platform
from app.services.consultation.repository import ConsultationRepository, RepositoryError
from app.services.consultation.retrieval import RAGFlow, RetrievalScope
from app.services.consultation.runtime import (
    ConsultationComponents,
    compose_consultation,
    start_consultation,
    stop_consultation,
)
from app.services.consultation.safety import SafetyDecision, SafetyGate, ValidatedImage


def test_consultation_lifecycle_is_injected_and_default_denied() -> None:
    """Use explicitly installed components and release them at shutdown."""

    async def exercise() -> None:
        app = FastAPI()

        async def disabled() -> None:
            return None

        assert await start_consultation(app, disabled) is None
        assert app.state.consultation_runtime is None
        assert app.state.consultation_components is None
        await stop_consultation(app)

        async def unexpected(*args: object) -> Never:
            raise AssertionError("No provider should be called")

        workflow = ConsultationWorkflow(SafetyGate(), unexpected, unexpected, unexpected, unexpected)
        runtime = ConsultationRuntime(
            ConsultationRepository("unused"), authenticate_platform, workflow.check_admission
        )

        async def configured() -> ConsultationComponents:
            return ConsultationComponents(runtime, workflow, poll_seconds=30)

        task = await start_consultation(app, configured)
        assert task is not None
        assert app.state.consultation_runtime is runtime
        with pytest.raises(RuntimeError, match="already activated"):
            await start_consultation(app, configured)
        await stop_consultation(app)
        assert task.cancelled()
        assert app.state.consultation_runtime is None
        assert app.state.consultation_components is None
        assert app.state.consultation_worker is None

    asyncio.run(exercise())


def test_composed_admission_checks_text_risk_before_image_availability() -> None:
    """The runtime keeps the text emergency path active with vision disabled."""

    async def exercise() -> None:
        notice = RiskNotice(status="human_handoff_required", message="Test notice", resources=[])

        async def classify(text: str, images: tuple[ValidatedImage, ...]) -> SafetyDecision:
            assert not images
            return SafetyDecision("high_risk", notice) if text == "urgent" else SafetyDecision("normal")

        async def unused(*args: object) -> Never:
            raise AssertionError("No other provider should be called")

        def unexpected(request: httpx.Request) -> Never:
            raise AssertionError("No provider HTTP call is needed")

        transport = httpx.MockTransport(unexpected)
        components = compose_consultation(
            repository=ConsultationRepository("unused"),
            authenticate=authenticate_platform,
            safety=SafetyGate(classify),
            ragflow=RAGFlow(transport, unused),
            retrieval_scope=RetrievalScope(()),
            deepseek=DeepSeek(transport),
            in_scope=unused,
        )
        principal = PlatformPrincipal("caller", "tenant", "subject")
        request = GenerationCreate(
            client_message_id="message",
            input_type="text",
            text="urgent",
            attachments=[ImageAttachment(file_id="a", media_type="image/png", content_base64="eA==")],
        )
        assert await components.runtime.check_admission(principal, "session", request) == notice
        with pytest.raises(RepositoryError, match="IMAGE_UNSUPPORTED"):
            await components.runtime.check_admission(
                principal, "session", request.model_copy(update={"text": "normal"})
            )

    asyncio.run(exercise())
