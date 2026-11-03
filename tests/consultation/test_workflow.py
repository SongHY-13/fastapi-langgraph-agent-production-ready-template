"""Deterministic tests for consultation safety and review."""

import asyncio
import importlib
from typing import Never

import pytest

from app.core.langgraph.consultation_graph import Candidate, ConsultationWorkflow, Evidence, ScopedInput
from app.schemas.consultation import Answer, AnswerSource, ImageAttachment, RiskNotice
from app.services.consultation.identity import PlatformPrincipal
from app.services.consultation.repository import GenerationLease
from app.services.consultation.safety import ConsultationTurn, SafetyDecision, SafetyGate, ValidatedImage


def test_absent_safety_provider_fails_closed() -> None:
    """Require an explicit trusted classifier."""
    safety = importlib.import_module("app.services.consultation.safety")
    gate = safety.SafetyGate()
    turn = safety.ConsultationTurn(request_id="request", text="question")
    assert asyncio.run(gate.classify_input(turn)).kind == "unavailable"


def test_text_risk_bypasses_image_loading() -> None:
    """Route text emergencies before attempting any image reads."""
    notice = RiskNotice(status="human_handoff_required", message="Platform attention required", resources=[])

    async def classify(text: str, images: tuple[ValidatedImage, ...]) -> SafetyDecision:
        assert text == "emergency" and not images
        return SafetyDecision("high_risk", notice)

    loaded = False

    async def load(image: ImageAttachment) -> ValidatedImage:
        nonlocal loaded
        loaded = True
        raise RuntimeError(image.file_id)

    image = ImageAttachment.model_validate({"file_id": "x", "media_type": "image/png", "content_base64": "eA=="})
    decision = asyncio.run(SafetyGate(classify, load).classify_input(ConsultationTurn("r", "emergency", (image,))))
    assert decision.risk == notice
    assert not loaded


@pytest.mark.parametrize(
    "mode", ["accepted", "blocked", "human_requested", "unavailable", "retrieval", "rejected", "revoked", "image_only"]
)
def test_reviewed_wire_payload(mode: str) -> None:
    """Expose only the reviewed v0.4 display payload."""

    async def classify(text: str, images: tuple[ValidatedImage, ...]) -> SafetyDecision:
        if mode == "blocked":
            return SafetyDecision("blocked")
        if mode == "human_requested":
            return SafetyDecision("human_requested")
        if mode == "unavailable":
            return SafetyDecision("unavailable")
        return SafetyDecision("normal")

    async def retrieve(scoped: ScopedInput) -> tuple[Evidence, ...]:
        if mode == "retrieval":
            raise TimeoutError
        source = AnswerSource.model_validate(
            {"source_id": "s", "title": "Source", "source_url": "https://example.test"}
        )
        return (Evidence(source, "applicable excerpt"),)

    async def draft(scoped: ScopedInput, evidence: tuple[Evidence, ...]) -> Candidate:
        return Candidate("candidate text", ("s",), followups=("candidate followup",))

    async def review(scoped: ScopedInput, answer: Answer) -> bool:
        assert answer.sources[0].source_id == "s"
        return mode != "rejected"

    async def in_scope(scoped: ScopedInput) -> bool:
        assert mode != "image_only"
        return True

    async def verify_citations(answer: Answer, evidence: tuple[Evidence, ...]) -> bool:
        assert answer.sources == [evidence[0].source]
        return mode != "revoked"

    flow = ConsultationWorkflow(
        SafetyGate(classify), retrieve, draft, review, in_scope, verify_citations=verify_citations
    )
    turn = (
        ConsultationTurn("request", "", images=(ValidatedImage("image/png", b"valid image"),))
        if mode == "image_only"
        else ConsultationTurn("request", "question")
    )
    result = asyncio.run(flow.run_consultation(turn))
    serialized = result.model_dump_json()
    if mode == "accepted":
        assert result.answer and result.answer.text == "candidate text"
        assert "https://example.test" in serialized
    else:
        assert "candidate" not in serialized and "https://example.test" not in serialized
    if mode == "human_requested":
        assert result.answer is None and result.advisory and result.advisory.reason == "user_requested"
    if mode in {"unavailable", "retrieval"}:
        assert result.status == "failed" and result.answer is None
    if mode == "image_only":
        assert result.answer and result.answer.text == "你想了解图片中的哪部分内容？" and result.advisory is None


def test_worker_text_risk_precedes_attachment_resolution() -> None:
    """Persist a changed text risk disposition even when the image is unavailable."""
    notice = RiskNotice(status="human_handoff_required", message="Platform attention required", resources=[])

    async def classify(text: str, images: tuple[ValidatedImage, ...]) -> SafetyDecision:
        return SafetyDecision("high_risk", notice)

    async def unavailable(*args: object) -> Never:
        raise AssertionError("Risk must bypass all remaining providers")

    workflow = ConsultationWorkflow(
        SafetyGate(classify), unavailable, unavailable, unavailable, unavailable, resolve_images=unavailable
    )
    lease = GenerationLease(
        "generation",
        "session",
        "turn",
        PlatformPrincipal("platform", "tenant", "subject"),
        "urgent text",
        "text",
        [{"file_id": "file", "media_type": "image/png"}],
        "token",
        1,
    )
    result = asyncio.run(workflow.run_lease(lease))
    assert result.risk == notice and result.answer is None
