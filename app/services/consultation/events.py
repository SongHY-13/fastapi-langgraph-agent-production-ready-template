"""Replay approved events and encode SSE delivery."""

import asyncio
from collections.abc import AsyncIterator
from time import monotonic

from anyio import move_on_after

from app.core.logging import logger
from app.schemas.consultation import GenerationEvent
from app.services.consultation.identity import PlatformPrincipal
from app.services.consultation.repository import ConsultationRepository, RepositoryError


async def _poll(
    repository: ConsultationRepository,
    principal: PlatformPrincipal,
    generation_id: str,
    after_seq: int,
    subscription_id: str | None = None,
) -> AsyncIterator[GenerationEvent | None]:
    heartbeat_at = monotonic() + 15
    while True:
        events, closed = await repository.read_events(principal, generation_id, after_seq, subscription_id)
        for event in events:
            if subscription_id and not await repository.subscription_is_open(generation_id, subscription_id):
                return
            yield event
            after_seq = event.seq
        if closed:
            return
        if monotonic() >= heartbeat_at:
            yield None
            heartbeat_at = monotonic() + 15
        if not events:
            await asyncio.sleep(0.2)


async def stream_events(
    repository: ConsultationRepository, principal: PlatformPrincipal, generation_id: str, after_seq: int = 0
) -> AsyncIterator[GenerationEvent]:
    """Replay persisted events and poll for future events until terminal."""
    async for event in _poll(repository, principal, generation_id, after_seq):
        if event is not None:
            yield event


async def sse_frames(
    repository: ConsultationRepository,
    principal: PlatformPrincipal,
    generation_id: str,
    after_seq: int,
    subscription_id: str,
) -> AsyncIterator[str]:
    """Encode replay and unsequenced heartbeat comments."""
    try:
        async for event in _poll(repository, principal, generation_id, after_seq, subscription_id):
            if event is None:
                yield ": heartbeat\n\n"
            else:
                yield f"id: {event.event_id}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"
    except RepositoryError as exc:
        # Headers have already been sent. A new request can recover the JSON error or result.
        logger.exception("consultation_sse_replay_failed", generation_id=generation_id, code=exc.code)
        return
    finally:
        with move_on_after(2, shield=True):
            await repository.close_subscription(generation_id, subscription_id)
