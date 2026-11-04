"""Synthetic provider responses for the isolated consultation integration environment."""

import asyncio
import json
from base64 import b64decode

import httpx
from langchain_core.callbacks import BaseCallbackHandler
from pydantic import AnyUrl

from app.core.langgraph.consultation_graph import ScopedInput
from app.schemas.consultation import AnswerAction, AnswerSource, ImageAttachment, RiskNotice
from app.services.consultation.attachments import ImageResolver
from app.services.consultation.identity import PlatformPrincipal, authenticate_platform
from app.services.consultation.openai_compatible import OpenAICompatibleModel
from app.services.consultation.repository import ConsultationRepository
from app.services.consultation.retrieval import ApprovedDocument, RAGFlow, RetrievalScope
from app.services.consultation.runtime import ConsultationComponents, compose_consultation
from app.services.consultation.safety import SafetyDecision, SafetyGate, ValidatedImage

_ORDINARY = "联调：普通"
_SLOW = "联调：慢速"
_HIGH_RISK = "联调：高风险"
_REFUSED = "联调：拒答"
_HUMAN = "联调：人工"
_NO_EVIDENCE = "联调：无依据"
_OUT_OF_SCOPE = "联调：超范围"
_IMAGE = "联调：图片"
_GROUNDED_SCENARIOS = {_ORDINARY, _SLOW, _IMAGE}
_PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
_PNG = b64decode(_PNG_BASE64)
_DATASET_ID = "fixture-dataset"
_DOCUMENT_ID = "fixture-document"
_SOURCE_ID = "fixture-source"
_VERSION = "fixture-v1"
_ACTION_CODE = "fixture_booking"
_ANSWER = "这是一条联调用合成答复。下方卡片用于测试前端展示。"


async def _catalog() -> tuple[ApprovedDocument, ...]:
    return (
        ApprovedDocument(
            source=AnswerSource(
                source_id=_SOURCE_ID,
                title="联调示例资料",
                source_url=AnyUrl("https://example.com/"),
                effective_status="effective",
            ),
            dataset_id=_DATASET_ID,
            document_id=_DOCUMENT_ID,
            published_version=_VERSION,
            status="published",
            jurisdiction=None,
            effective_from=None,
            effective_until=None,
        ),
    )


async def _classify(text: str, images: tuple[ValidatedImage, ...]) -> SafetyDecision:
    scenario = text.strip()
    if scenario == _HIGH_RISK:
        return SafetyDecision(
            "high_risk",
            RiskNotice(status="human_handoff_required", message="联调场景已触发风险提示。", resources=[]),
        )
    if scenario == _HUMAN:
        return SafetyDecision("human_requested")
    if scenario == _REFUSED:
        return SafetyDecision("blocked")
    if scenario in _GROUNDED_SCENARIOS or scenario in {_NO_EVIDENCE, _OUT_OF_SCOPE} or (not scenario and images):
        return SafetyDecision("normal")
    return SafetyDecision("unavailable")


async def _in_scope(scoped: ScopedInput) -> bool:
    return scoped.text.strip() != _OUT_OF_SCOPE


async def _authorize_image(principal: PlatformPrincipal, file_id: str) -> ImageAttachment | None:
    if file_id != f"fixture-{principal.subject_id}":
        return None
    return ImageAttachment(file_id=file_id, media_type="image/png", content_base64=_PNG_BASE64)


async def _decode_image(image: ValidatedImage) -> bool:
    return image.media_type == "image/png" and image.content == _PNG


async def _retrieve(payload: dict[str, object]) -> httpx.Response:
    question = payload.get("question")
    if payload.get("dataset_ids") != [_DATASET_ID] or payload.get("document_ids") != [_DOCUMENT_ID]:
        return httpx.Response(400)
    scenario = question.strip() if isinstance(question, str) else ""
    if scenario == _SLOW:
        await asyncio.sleep(3)
    chunks = []
    if scenario in _GROUNDED_SCENARIOS:
        chunks.append(
            {
                "id": "fixture-chunk",
                "dataset_id": _DATASET_ID,
                "document_id": _DOCUMENT_ID,
                "content": _ANSWER,
                "similarity": 1,
                "positions": [],
            }
        )
    return httpx.Response(200, json={"code": 0, "data": {"chunks": chunks}})


def _model_response(payload: dict[str, object]) -> httpx.Response:
    messages = payload.get("messages")
    if not isinstance(messages, list) or len(messages) != 2:
        return httpx.Response(400)
    content = messages[1]["content"]
    data = json.loads(content[0]["text"] if isinstance(content, list) else content)
    scenario = data["question"].strip()
    if scenario not in _GROUNDED_SCENARIOS or (scenario == _IMAGE and not isinstance(content, list)):
        return httpx.Response(400)
    result: dict[str, object]
    if "evidence" in data:
        result = {
            "text": _ANSWER,
            "source_ids": [_SOURCE_ID],
            "action_codes": [_ACTION_CODE],
            "followups": [_NO_EVIDENCE, _HUMAN],
        }
    else:
        result = {"approved": data.get("draft") == _ANSWER}
    return httpx.Response(
        200,
        json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]},
    )


async def _respond(request: httpx.Request) -> httpx.Response:
    if request.method == "POST" and request.url.path == "/api/v1/retrieval":
        return await _retrieve(json.loads(request.content))
    if request.method == "POST" and request.url.path == "/v1/chat/completions":
        return _model_response(json.loads(request.content))
    return httpx.Response(404)


def build_integration_fixture(repository: ConsultationRepository) -> ConsultationComponents:
    """Compose the real workflow with fixed inputs and an in-process provider transport."""
    transport = httpx.MockTransport(_respond)
    return compose_consultation(
        repository=repository,
        authenticate=authenticate_platform,
        safety=SafetyGate(_classify),
        ragflow=RAGFlow(transport, _catalog, "https://retrieval.invalid", "fixture", _VERSION, timeout=5),
        retrieval_scope=RetrievalScope((_DATASET_ID,)),
        model=OpenAICompatibleModel(
            transport,
            "https://model.invalid/v1",
            "fixture",
            vision_approved=True,
            allowed_actions=(AnswerAction(action_code=_ACTION_CODE, label="联调预约卡片", params={}),),
        ),
        in_scope=_in_scope,
        image_resolver=ImageResolver(_authorize_image, _decode_image, transport, frozenset()),
        trace_handler=BaseCallbackHandler(),
    )
