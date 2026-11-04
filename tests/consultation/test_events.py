"""Exercise SSE replay failure after the response has started."""

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from app.services.consultation.events import sse_frames
from app.services.consultation.identity import PlatformPrincipal
from app.services.consultation.repository import ConsultationRepository, RepositoryError


def test_sse_logs_repository_failure_without_emitting_a_frame(caplog: pytest.LogCaptureFixture) -> None:
    """Leave a diagnostic when replay expires midstream without changing the wire format."""

    async def exercise() -> None:
        repository = ConsultationRepository("unused")
        repository.read_events = AsyncMock(side_effect=RepositoryError("EVENT_HISTORY_EXPIRED"))
        repository.close_subscription = AsyncMock()
        principal = PlatformPrincipal("platform", "tenant", "subject")

        frames = [frame async for frame in sse_frames(repository, principal, "generation", 4, "subscription")]

        assert frames == []

    asyncio.run(exercise())
    assert any(
        record.levelno == logging.ERROR
        and "consultation_sse_replay_failed" in record.getMessage()
        and "EVENT_HISTORY_EXPIRED" in record.getMessage()
        and "Traceback" in record.getMessage()
        for record in caplog.records
    )
