"""Exercise consultation registration and trusted lookup."""

import asyncio
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg import AsyncConnection
from pydantic import AnyUrl

from alembic import command
from alembic.config import Config
from app.api.v1.consultations import router
from app.services.consultation.generations import ConsultationRuntime
from app.schemas.consultation import Answer, GenerationCreate, ImageAttachment, RiskNotice
from app.services.consultation.identity import PlatformPrincipal, PlatformAuthenticationRequired, authenticate_platform
from app.services.consultation.repository import (
    ConsultationRepository,
    SubmissionPriority,
    RepositoryError,
    fingerprint,
)


def test_fingerprint_uses_stable_image_id() -> None:
    """Refreshed image credentials do not change request identity."""
    request = GenerationCreate(
        client_message_id="one",
        input_type="text",
        attachments=[ImageAttachment(file_id="a", media_type="image/png", content_base64="eA==")],
    )
    refreshed = request.model_copy(
        update={
            "attachments": [
                ImageAttachment(
                    file_id="a", media_type="image/png", read_url=AnyUrl("https://files.example.test/refreshed")
                )
            ]
        }
    )
    changed = request.model_copy(
        update={"attachments": [ImageAttachment(file_id="b", media_type="image/png", content_base64="eA==")]}
    )
    assert fingerprint(request) == fingerprint(refreshed)
    assert fingerprint(request) != fingerprint(changed)


def test_default_platform_authentication_fails_closed() -> None:
    """Reject an unverified authorization header."""
    with pytest.raises(PlatformAuthenticationRequired):
        authenticate_platform("Bearer unverified").send(None)


@dataclass(frozen=True)
class _IsolatedDatabase:
    dsn: str = field(repr=False)


@pytest.fixture
def isolated_dsn() -> Iterator[_IsolatedDatabase]:
    """Migrate the explicitly provided disposable database."""
    dsn = os.getenv("CONSULTATION_TEST_DSN")
    if not dsn:
        pytest.skip("CONSULTATION_TEST_DSN must point to a disposable PostgreSQL database")
    config = Config(
        "alembic.ini", attributes={"database_url": dsn.replace("postgresql://", "postgresql+psycopg://", 1)}
    )
    command.upgrade(config, "head")
    try:
        yield _IsolatedDatabase(dsn)
    finally:
        command.downgrade(config, "base")


def test_scoped_concurrent_registration_survives_restart(isolated_dsn: _IsolatedDatabase) -> None:
    """Exercise race-safe admission and durable scoped results."""

    async def exercise() -> None:
        repository = ConsultationRepository(isolated_dsn.dsn)
        principal = PlatformPrincipal("platform", "tenant", "alice")
        request = GenerationCreate(client_message_id="one", input_type="text", text="Question")
        first, second = await asyncio.gather(
            *(repository.create_or_get(principal, "session", request) for _ in range(2))
        )
        assert first.generation_id == second.generation_id
        assert first.user_message_id == second.user_message_id
        restarted = ConsultationRepository(isolated_dsn.dsn)
        assert (await restarted.get(principal, first.generation_id)).status == "queued"
        for foreign in (
            PlatformPrincipal("platform", "tenant", "bob"),
            PlatformPrincipal("platform", "other", "alice"),
            PlatformPrincipal("other", "tenant", "alice"),
        ):
            with pytest.raises(RepositoryError, match="GENERATION_NOT_FOUND"):
                await restarted.get(foreign, first.generation_id)
        with pytest.raises(RepositoryError, match="IDEMPOTENCY_CONFLICT"):
            await restarted.create_or_get(principal, "session", request.model_copy(update={"text": "Changed"}))
        followup = request.model_copy(update={"client_message_id": "two"})
        with pytest.raises(RepositoryError, match="GENERATION_IN_PROGRESS"):
            await restarted.create_or_get(principal, "session", followup)
        notice = RiskNotice(status="human_handoff_required", message="Review", resources=[])
        risk = await restarted.create_or_get(
            principal,
            "session",
            followup,
            priority=SubmissionPriority.HIGH_RISK,
            risk=notice,
        )
        assert risk.generation_id != first.generation_id
        async with await AsyncConnection.connect(isolated_dsn.dsn) as conn:
            cursor = await conn.execute("SELECT count(*) FROM consultation_turns")
            assert await cursor.fetchone() == (2,)
            cursor = await conn.execute("SELECT count(*) FROM consultation_generations")
            assert await cursor.fetchone() == (2,)
            cursor = await conn.execute(
                "SELECT type FROM consultation_events WHERE generation_id = %s ORDER BY seq",
                (risk.generation_id,),
            )
            assert await cursor.fetchall() == [("risk.notice",), ("generation.completed",)]
        assert (await restarted.get(principal, first.generation_id)).status == "cancelled"
        assert (await restarted.get(principal, risk.generation_id)).risk == notice

    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(exercise())


def test_durable_delivery_and_cancel_race(isolated_dsn: _IsolatedDatabase) -> None:
    """Verify lease recovery, scoped SSE replay and one terminal event using PostgreSQL."""

    async def exercise() -> None:
        repository = ConsultationRepository(isolated_dsn.dsn)
        principal = PlatformPrincipal("platform", "tenant", "alice")
        foreign = PlatformPrincipal("platform", "tenant", "bob")

        async def authenticate(authorization: str | None) -> PlatformPrincipal:
            if authorization == "test-alice":
                return principal
            if authorization == "test-bob":
                return foreign
            raise PlatformAuthenticationRequired

        async def check_admission(_principal: PlatformPrincipal, _session: str, _body: GenerationCreate) -> None:
            return None

        app = FastAPI()
        app.include_router(router)
        app.state.consultation_runtime = ConsultationRuntime(repository, authenticate, check_admission)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            headers = {"Authorization": "test-alice"}
            post = await client.post(
                "/sessions/session/generations",
                headers=headers,
                json={
                    "client_message_id": "one",
                    "text": "question",
                    "input_type": "text",
                },
            )
            assert post.status_code == 202
            generation_id = post.json()["generation_id"]
            path = f"/generations/{generation_id}"
            first = await repository.claim()
            assert first is not None
            async with await AsyncConnection.connect(isolated_dsn.dsn) as conn:
                await conn.execute(
                    "UPDATE consultation_generations SET lease_until = now() WHERE id = %s", (generation_id,)
                )
            restarted = ConsultationRepository(isolated_dsn.dsn)
            second = await restarted.claim()
            assert second is not None and second.version > first.version
            answer = Answer(answer_id="answer", text="Reviewed", sources=[], actions=[], followups=[])
            assert not await repository.finish(first, answer=answer)
            assert await restarted.finish(second, answer=answer)
            assert (await client.get(path + "/events", headers={"Authorization": "test-bob"})).status_code == 404
            replay = await client.get(path + "/events", headers=headers)
            assert replay.status_code == 200
            assert replay.headers["x-subscription-id"]
            assert [line for line in replay.text.splitlines() if line.startswith("event:")] == [
                "event: answer.delta",
                "event: answer.metadata",
                "event: generation.completed",
            ]
            resume = await client.get(path + "/events?after_seq=2", headers=headers)
            assert "event: generation.completed" in resume.text
            assert "event: answer." not in resume.text
            lookup = await client.get(path, headers=headers)
            assert lookup.json()["answer"] == answer.model_dump()
            selected = await restarted.open_subscription(principal, generation_id, 0)
            other = await restarted.open_subscription(principal, generation_id, 0)
            stopped = await client.post(path + "/cancel", headers=headers, json={"subscription_id": selected})
            assert stopped.json()["status"] == "completed"
            assert stopped.json()["delivery_status"] == "aborted"
            assert await restarted.subscription_is_open(generation_id, other)
            assert not await restarted.subscription_is_open(generation_id, selected)
            async with await AsyncConnection.connect(isolated_dsn.dsn) as conn:
                await conn.execute("DELETE FROM consultation_events WHERE generation_id = %s", (generation_id,))
                await conn.execute(
                    "UPDATE consultation_generations SET replay_floor = 3 WHERE id = %s", (generation_id,)
                )
            expired = await client.get(path + "/events", headers=headers)
            assert expired.status_code == 410
            assert expired.json()["code"] == "EVENT_HISTORY_EXPIRED"
            assert (await client.get(path, headers=headers)).json()["status"] == "completed"

            next_task = await repository.create_or_get(
                principal, "session", GenerationCreate(client_message_id="two", input_type="text", text="Next")
            )
            lease = await repository.claim()
            assert lease is not None
            await asyncio.gather(
                restarted.finish(lease, answer=answer),
                repository.cancel(principal, next_task.generation_id, None, "request"),
            )
            events, closed = await restarted.read_events(principal, next_task.generation_id, 0)
            assert closed
            assert sum(event.type.startswith("generation.") for event in events) == 1

    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(exercise())


def test_worker_risk_and_advisory_outcomes(isolated_dsn: _IsolatedDatabase) -> None:
    """Publish only reviewed output and count only finalized unanswered turns."""

    async def exercise() -> None:
        repository = ConsultationRepository(isolated_dsn.dsn)
        principal = PlatformPrincipal("platform", "tenant", "alice")
        reply = Answer(answer_id="reviewed", text="Reviewed", sources=[], actions=[], followups=[])

        async def register(key: str) -> str:
            result = await repository.create_or_get(
                principal, "session", GenerationCreate(client_message_id=key, input_type="text", text="Question")
            )
            return result.generation_id

        first = await register("one")
        lease = await repository.claim()
        assert lease and lease.generation_id == first
        assert await repository.finish(lease, answer=reply, unanswered=True, effective=False)
        assert (await repository.get(principal, first)).advisory is None
        assert await register("one") == first

        cancelled = await register("cancelled")
        lease = await repository.claim()
        assert lease and lease.generation_id == cancelled
        await repository.cancel(principal, cancelled, None, "request")
        assert not await repository.finish(lease, answer=reply, unanswered=True, effective=False)

        second = await register("two")
        lease = await repository.claim()
        assert lease and lease.generation_id == second
        assert await repository.finish(lease, answer=reply, unanswered=True, effective=False)
        result = await repository.get(principal, second)
        assert result.advisory and result.advisory.reason == "consecutive_unanswered"

        third = await register("three")
        lease = await repository.claim()
        assert lease and lease.generation_id == third
        notice = RiskNotice(status="human_handoff_required", message="Platform attention required", resources=[])
        assert await repository.finish(lease, risk=notice)
        result = await repository.get(principal, third)
        assert result.risk == notice and result.answer is None
        events, closed = await repository.read_events(principal, third, 0)
        assert closed
        assert [event.type for event in events] == ["risk.notice", "generation.completed"]

    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(exercise())
