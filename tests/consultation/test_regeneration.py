"""Exercise canonical regeneration against migrated PostgreSQL."""

import asyncio
import sys

import pytest
from psycopg import AsyncConnection

from app.schemas.consultation import (
    Answer,
    FailurePayload,
    GenerationAccepted,
    GenerationCreate,
    ImageAttachment,
    RiskNotice,
)
from app.services.consultation.identity import PlatformPrincipal
from app.services.consultation.repository import ConsultationRepository, RepositoryError
from .test_repository import _IsolatedDatabase, isolated_dsn  # noqa: F401


def answer(answer_id: str) -> Answer:
    """Build an effective worker answer."""
    return Answer(answer_id=answer_id, text=answer_id, sources=[], actions=[], followups=[])


def test_regeneration_replay_and_continuity(isolated_dsn: _IsolatedDatabase) -> None:  # noqa: F811
    """Keep the old answer through failed retries and read only the effective replacement."""

    async def exercise() -> None:
        repo = ConsultationRepository(isolated_dsn.dsn)
        principal = PlatformPrincipal("caller", "tenant", "subject")
        request = GenerationCreate(client_message_id="first", input_type="text", text="question")
        original = await repo.create_or_get(principal, "session", request)
        first = await repo.claim()
        assert first and first.history == ()
        assert await repo.finish(first, answer=answer("original"))

        async def regenerate(key: str) -> GenerationAccepted:
            result = await repo.regenerate(principal, "session", "original", key, [], request_id="test")
            assert isinstance(result, GenerationAccepted)
            assert result.user_message_id == original.user_message_id
            return result

        failed = await regenerate("initial")
        assert (await regenerate("initial")).generation_id == failed.generation_id
        lease = await repo.claim()
        assert lease and lease.history == ()
        assert await repo.finish(lease, failure=FailurePayload(code="GENERATION_FAILED", retryable=True))

        cancelled = await regenerate("cancel")
        stale = await repo.claim()
        assert stale and stale.generation_id == cancelled.generation_id
        await repo.cancel(principal, cancelled.generation_id, None, "test")
        assert not await repo.finish(stale, answer=answer("late-result"))

        risk_only = await regenerate("risk")
        lease = await repo.claim()
        assert lease and lease.generation_id == risk_only.generation_id
        notice = RiskNotice(status="human_handoff_required", message="Platform attention required", resources=[])
        assert await repo.finish(lease, risk=notice)
        with pytest.raises(RepositoryError, match="REGENERATION_NOT_ALLOWED"):
            await repo.regenerate(principal, "session", risk_only.generation_id, "late", [], request_id="test")
        accepted = await regenerate("replace")
        lease = await repo.claim()
        assert lease and lease.generation_id == accepted.generation_id
        assert await repo.finish(lease, answer=answer("replacement"))

        repo = ConsultationRepository(isolated_dsn.dsn)
        await repo.create_or_get(
            principal, "session", GenerationCreate(client_message_id="next", input_type="text", text="follow-up")
        )
        next_lease = await repo.claim()
        assert next_lease and next_lease.history == (("question", "replacement"),)
        assert (await regenerate("replace")).generation_id == accepted.generation_id
        with pytest.raises(RepositoryError, match="REGENERATION_NOT_ALLOWED"):
            await repo.regenerate(principal, "session", "replacement", "late", [], request_id="test")
        async with await AsyncConnection.connect(isolated_dsn.dsn) as conn:
            count = await (await conn.execute("SELECT count(*) FROM consultation_turns")).fetchone()
        assert count == (2,)

    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(exercise())


def test_regeneration_attachment_identity_and_normal_slot(isolated_dsn: _IsolatedDatabase) -> None:  # noqa: F811
    """Accept refreshed transport for original images and share the normal admission slot."""

    async def exercise() -> None:
        repo = ConsultationRepository(isolated_dsn.dsn)
        principal = PlatformPrincipal("caller", "tenant", "subject")
        original = ImageAttachment(file_id="image", media_type="image/png", content_base64="eA==")
        await repo.create_or_get(
            principal,
            "session",
            GenerationCreate(client_message_id="first", input_type="text", attachments=[original]),
        )
        lease = await repo.claim()
        assert lease and await repo.finish(lease, answer=answer("original"))
        with pytest.raises(RepositoryError, match="INVALID_ATTACHMENT"):
            await repo.regenerate(
                principal,
                "session",
                "original",
                "replace",
                [original.model_copy(update={"file_id": "other"})],
                request_id="test",
            )
        refreshed = ImageAttachment.model_validate(
            {"file_id": "image", "media_type": "image/png", "read_url": "https://files.example.test/new"}
        )
        outcomes = await asyncio.gather(
            repo.regenerate(principal, "session", "original", "replace", [refreshed], request_id="test"),
            repo.create_or_get(
                principal, "session", GenerationCreate(client_message_id="next", input_type="text", text="follow-up")
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(item, GenerationAccepted) for item in outcomes) == 1
        assert any(
            isinstance(item, RepositoryError) and item.code in {"GENERATION_IN_PROGRESS", "REGENERATION_NOT_ALLOWED"}
            for item in outcomes
        )

    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(exercise())
