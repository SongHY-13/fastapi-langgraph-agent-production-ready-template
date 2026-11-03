"""Consultation provider boundaries use synthetic responses."""

import asyncio
import json
from base64 import b64encode

import httpx
import pytest
from pydantic import ValidationError

from app.core.langgraph.consultation_graph import Evidence, ScopedInput
from app.schemas.consultation import MAX_IMAGE_BYTES, Answer, AnswerAction, AnswerSource, ImageAttachment

from app.services.consultation.attachments import ImageResolver
from app.services.consultation.deepseek import DeepSeek
from app.services.consultation.identity import PlatformPrincipal
from app.services.consultation.provider_http import ProviderUnavailable
from app.services.consultation.repository import RepositoryError
from app.services.consultation.retrieval import RetrievalScope
from app.services.consultation.safety import ValidatedImage


def test_provider_defaults_fail_closed() -> None:
    """Keep the provider off when no dedicated key is configured."""

    async def run() -> None:
        def unexpected(request: httpx.Request) -> httpx.Response:
            raise AssertionError("unexpected transport call")

        provider = DeepSeek(transport=httpx.MockTransport(unexpected))
        assert (await provider.classify("question", ())).kind == "unavailable"

    asyncio.run(run())
    assert RetrievalScope(()).dataset_ids == ()
    assert ImageResolver is not None


@pytest.mark.parametrize("vision", [False, True])
def test_deepseek_uses_bounded_json_and_inline_vision(vision: bool) -> None:
    """Send text or approved inline vision in a bounded JSON request."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        assert request.headers["authorization"] == "Bearer synthetic"
        payload = json.loads(request.content)
        assert payload["model"] == "deepseek-flash"
        assert payload["thinking"] == {"type": "disabled"}
        assert 0 < payload["max_tokens"] <= 768
        content = payload["messages"][1]["content"]
        if vision:
            assert content[1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,YWJj"}}
        else:
            assert json.loads(content) == {"text": "question"}
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"kind":"normal"}'}}]}
        )

    provider = DeepSeek(httpx.MockTransport(respond), api_key="synthetic", vision_approved=vision)
    images = (ValidatedImage("image/png", b"abc"),) if vision else ()
    assert asyncio.run(provider.classify("question", images)).kind == "normal"


@pytest.mark.parametrize("content", ["malformed", "[]", '{"kind":"high_risk"}', '{"kind":true}'])
def test_deepseek_rejects_untrusted_classification(content: str) -> None:
    """Reject malformed JSON and classifications without approved semantics."""

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})

    provider = DeepSeek(httpx.MockTransport(respond), api_key="synthetic")
    assert asyncio.run(provider.classify("question", ())).kind == "unavailable"


@pytest.mark.parametrize("action_code", ["booking", "forged"])
def test_deepseek_draft_and_review_use_only_approved_actions(action_code: str) -> None:
    """Send allowed action codes and review the full displayed result."""
    action = AnswerAction(action_code="booking", label="预约挂号", params={})
    source = AnswerSource.model_validate(
        {"source_id": "source", "title": "流程", "source_url": "https://example.test"}
    )
    followup = "需要什么材料？"

    def respond(request: httpx.Request) -> httpx.Response:
        user_data = json.loads(json.loads(request.content)["messages"][1]["content"])
        if "evidence" in user_data:
            assert user_data["allowed_action_codes"] == ["booking"]
            assert user_data["history"] == [["先前的问题", "先前的回答"]]
            result = {
                "text": "可预约",
                "source_ids": ["source"],
                "action_codes": [action_code],
                "followups": [followup],
            }
        else:
            assert user_data["actions"] == [action.model_dump()]
            assert user_data["followups"] == [followup]
            result = {"approved": True}
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]}
        )

    async def exercise() -> None:
        provider = DeepSeek(httpx.MockTransport(respond), api_key="synthetic", allowed_actions=(action,))
        scoped = ScopedInput("如何预约？", (), (("先前的问题", "先前的回答"),))
        evidence = (Evidence(source, "流程正文"),)
        if action_code == "forged":
            with pytest.raises(ProviderUnavailable):
                await provider.draft(scoped, evidence)
            return
        draft = await provider.draft(scoped, evidence)
        assert draft.actions == (action,)
        assert await provider.review(
            scoped,
            Answer(
                answer_id="answer",
                text=draft.text,
                sources=[source],
                actions=list(draft.actions),
                followups=list(draft.followups),
            ),
        )

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("host", "status", "media_type", "decoded", "code", "calls"),
    [
        ("other.example", 200, "image/png", True, "INVALID_ATTACHMENT", 0),
        ("images.example", 302, "image/png", True, "ATTACHMENT_UNAVAILABLE", 1),
        ("images.example", 200, "image/jpeg", True, "INVALID_ATTACHMENT", 1),
        ("images.example", 403, "image/png", True, "ATTACHMENT_UNAVAILABLE", 1),
        ("images.example", 200, "image/png", False, "INVALID_ATTACHMENT", 1),
        ("images.example", 200, "image/png", None, "IMAGE_UNSUPPORTED", 0),
    ],
)
def test_image_resolver_rejects_unapproved_or_unreadable_transport(
    host: str, status: int, media_type: str, decoded: bool | None, code: str, calls: int
) -> None:
    """Reject unavailable or unauthorized image input before classification."""
    requests: list[httpx.URL] = []

    async def authorize(principal: PlatformPrincipal, file_id: str) -> ImageAttachment | None:
        raise AssertionError("load accepts an already-authorized attachment")

    async def decode(image: ValidatedImage) -> bool:
        return decoded is True

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.url)
        return httpx.Response(
            status,
            headers={"Content-Type": media_type, "Location": "https://other.example/redirect"},
            content=b"\x89PNG\r\n\x1a\n",
        )

    resolver = ImageResolver(
        authorize, decode if decoded is not None else None, httpx.MockTransport(respond), frozenset({"images.example"})
    )
    attachment = ImageAttachment.model_validate(
        {"file_id": "image", "media_type": "image/png", "read_url": f"https://{host}/original"}
    )
    with pytest.raises(RepositoryError, match=code):
        asyncio.run(resolver.load(attachment))
    assert len(requests) == calls


def test_inline_image_rejects_oversize_decoded_bytes() -> None:
    """Apply the size limit to decoded image bytes."""
    encoded = b64encode(b"x" * (MAX_IMAGE_BYTES + 1)).decode("ascii")
    with pytest.raises(ValidationError, match="inline image exceeds"):
        ImageAttachment(file_id="image", media_type="image/png", content_base64=encoded)
