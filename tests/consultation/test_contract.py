"""Tests for the v0.4 consultation wire contract."""

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from app.schemas import consultation as contract


def image() -> dict[str, str]:
    """Provide a valid attachment reference."""
    return {"file_id": "file_1", "media_type": "image/png", "read_url": "https://files.example.com/img"}


def test_text_or_image_required() -> None:
    """Accept text or image, but reject an empty turn."""
    model = contract.GenerationCreate
    base = {"client_message_id": "msg_1", "input_type": "text"}
    assert model.model_validate({**base, "text": "你好"}).text == "你好"
    assert model.model_validate({**base, "attachments": [image()]}).attachments[0].file_id == "file_1"
    with pytest.raises(ValidationError):
        model.model_validate({**base, "text": "   "})
    with pytest.raises(ValidationError):
        model.model_validate({**base, "attachments": [image()] * 4})


def test_exactly_one_attachment_transport() -> None:
    """Reject missing, conflicting or malformed image transports."""
    model = contract.ImageAttachment
    data = image()
    inline = {"file_id": "file_1", "media_type": "image/png", "content_base64": "aGVsbG8="}
    assert model.model_validate(inline).read_url is None
    for bad in (
        {**data, "content_base64": "aGVsbG8="},
        {"file_id": "file_1", "media_type": "image/png"},
        {**inline, "content_base64": "data:image/png;base64,aA=="},
        {**inline, "content_base64": "not base64"},
        {**data, "read_url": "http://files.example.com/img"},
        {**data, "read_url": "https://user:secret@files.example.com/img"},
        {**inline, "content_base64": "YWFh" * 1747627},
    ):
        with pytest.raises(ValidationError):
            model.model_validate(bad)


def test_rejects_document_audio_and_unknown_identity_fields() -> None:
    """Restrict first-phase inputs and prevent self-declared user identity."""
    model = contract.GenerationCreate
    valid = {"client_message_id": "msg_1", "text": "你好", "input_type": "voice_transcript"}
    assert model.model_validate(valid).input_type == "voice_transcript"
    for bad in (
        {**valid, "user_id": "other"},
        {**valid, "input_type": "audio"},
        {**valid, "attachments": [{**image(), "media_type": "application/pdf"}]},
        {**valid, "attachments": [{**image(), "media_type": "audio/mpeg"}]},
    ):
        with pytest.raises(ValidationError):
            model.model_validate(bad)


def test_risk_event_requires_risk_payload() -> None:
    """Reject a completion payload sent under the high-risk event name."""
    adapter = TypeAdapter[contract.GenerationEvent](contract.GenerationEvent)
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "event_id": "g:1",
                "generation_id": "g",
                "seq": 1,
                "type": "risk.notice",
                "payload": {"answer_id": None, "advisory": None},
                "request_id": "req",
            }
        )


def test_mock_fixtures_validate() -> None:
    """Validate mock requests, responses and streams against wire models."""
    root = Path(__file__).resolve().parents[2]
    fixtures = json.loads((root / "docs/zh-CN/mock/consultation-v04.json").read_text(encoding="utf-8"))
    assert set(fixtures) >= {"text", "image", "advisory", "risk", "busy", "cancel", "expired", "regenerate"}
    adapter = TypeAdapter[contract.GenerationEvent](contract.GenerationEvent)
    for case in fixtures.values():
        for key, model in (
            ("submit", contract.GenerationCreate),
            ("regenerate", contract.RegenerateRequest),
            ("accepted", contract.GenerationAccepted),
            ("result", contract.GenerationResult),
            ("cancel_request", contract.CancelRequest),
            ("cancelled", contract.CancelResult),
            ("error", contract.ConsultationError),
        ):
            if key in case:
                model.model_validate(case[key])
        events = [adapter.validate_python(event) for event in case.get("events", [])]
        if events:
            assert all(event.generation_id == case["accepted"]["generation_id"] for event in events)
            assert [event.seq for event in events] == list(range(1, len(events) + 1))
            assert events[-1].type == f"generation.{case['result']['status']}"
            final_answer = case["result"]["answer"]
            if final_answer is not None:
                deltas = [event for event in events if isinstance(event, contract.AnswerDeltaEvent)]
                assert all(delta.payload.answer_id == final_answer["answer_id"] for delta in deltas)
                assert "".join(delta.payload.text for delta in deltas) == final_answer["text"]
    risk = fixtures["risk"]
    assert risk["events"][0]["payload"] == risk["result"]["risk"]
    assert risk["result"]["answer"] is None
    assert fixtures["regenerate"]["accepted"]["user_message_id"] == fixtures["text"]["accepted"]["user_message_id"]
    assert fixtures["cancel"]["cancelled"]["delivery_status"] == "aborted"
    assert fixtures["cancel"]["event_delivery"] == "reconnect_replay"
    assert fixtures["expired"]["error"]["retryable"] is False
    assert fixtures["service_busy"]["http_status"] == 503
    assert fixtures["service_busy"]["error"].get("risk") is None
