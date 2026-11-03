"""Lifespan helpers for an explicitly injected consultation runtime.

This module deliberately does not construct platform credentials, provider
clients, or a database repository. Deployments must supply reviewed adapters;
without them the consultation API remains unavailable.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from fastapi import FastAPI
from langchain_core.callbacks import BaseCallbackHandler

from app.core.langgraph.consultation_graph import ConsultationWorkflow, Evidence, InScope, ScopedInput
from app.core.logging import logger
from app.core.observability import langfuse_callback_handler
from app.schemas.consultation import Answer, GenerationCreate, ImageAttachment, RiskNotice
from app.services.consultation.attachments import ImageResolver
from app.services.consultation.generations import ConsultationRuntime, run_one
from app.services.consultation.identity import PlatformAuthenticator, PlatformPrincipal
from app.services.consultation.openai_compatible import OpenAICompatibleModel
from app.services.consultation.repository import ConsultationRepository, RepositoryError
from app.services.consultation.retrieval import RAGFlow, RetrievalScope
from app.services.consultation.safety import ConsultationTurn, SafetyGate


@dataclass(frozen=True, slots=True)
class ConsultationComponents:
    """Fully configured dependencies supplied by an approved deployment."""

    runtime: ConsultationRuntime
    workflow: ConsultationWorkflow
    worker_enabled: bool = True
    lease_seconds: int = 60
    poll_seconds: float = 0.5

    def __post_init__(self) -> None:
        """Reject unsafe worker timing before starting application traffic."""
        if self.lease_seconds < 2:
            raise ValueError("consultation worker lease must be at least two seconds")
        if not 0 < self.poll_seconds <= 30:
            raise ValueError("consultation worker poll interval must be between zero and thirty seconds")


def compose_consultation(
    *,
    repository: ConsultationRepository,
    authenticate: PlatformAuthenticator,
    safety: SafetyGate,
    ragflow: RAGFlow,
    retrieval_scope: RetrievalScope,
    model: OpenAICompatibleModel,
    in_scope: InScope,
    image_resolver: ImageResolver | None = None,
    trace_handler: BaseCallbackHandler | None = None,
    lease_seconds: int = 60,
    poll_seconds: float = 0.5,
) -> ConsultationComponents:
    """Compose only caller supplied, reviewed consultation dependencies.

    No secrets, transports, or network clients are loaded or created here. The
    retrieval scope is deployment supplied and is captured for both retrieval
    and fresh citation validation.
    """
    accepts_images = image_resolver is not None and image_resolver.decode is not None and model.vision_approved

    async def retrieve(scoped: ScopedInput) -> tuple[Evidence, ...]:
        return await ragflow(scoped, retrieval_scope)

    async def verify_citations(answer: Answer, evidence: tuple[Evidence, ...]) -> bool:
        return await ragflow.verify_citations(answer, evidence, retrieval_scope)

    async def refresh_attachments(principal: PlatformPrincipal, attachments: list[ImageAttachment]) -> None:
        if image_resolver is None:
            raise RepositoryError("ATTACHMENT_UNAVAILABLE")
        await image_resolver.load_images(attachments, principal)

    async def check_admission(
        principal: PlatformPrincipal, session_id: str, request: GenerationCreate
    ) -> RiskNotice | None:
        if not request.attachments:
            return await workflow.check_admission(principal, session_id, request)
        text_only = ConsultationTurn("admission", request.text)
        decision = await safety.classify_input(text_only) if request.text.strip() else None
        if decision is not None and decision.risk is not None:
            return decision.risk
        if image_resolver is None or not accepts_images:
            raise RepositoryError("IMAGE_UNSUPPORTED")
        images = await image_resolver.load_images(request.attachments, principal)
        decision, _ = await safety.inspect(replace(text_only, images=tuple(images)), text_decision=decision)
        if decision.kind == "unavailable":
            raise RepositoryError("SERVICE_UNAVAILABLE")
        return decision.risk

    workflow = ConsultationWorkflow(
        safety,
        retrieve,
        model.draft,
        model.review,
        in_scope,
        allowed_actions=model.allowed_actions,
        verify_citations=verify_citations,
        resolve_images=image_resolver.resolve_lease if image_resolver is not None else None,
        trace_handler=trace_handler or langfuse_callback_handler,
    )
    runtime = ConsultationRuntime(
        repository=repository,
        authenticate=authenticate,
        check_admission=check_admission,
        accepts_images=accepts_images,
        refresh_attachments=refresh_attachments if accepts_images else None,
    )
    return ConsultationComponents(runtime, workflow, lease_seconds=lease_seconds, poll_seconds=poll_seconds)


async def _worker_loop(components: ConsultationComponents) -> None:
    """Poll durable work sequentially; each run is bounded by its lease."""
    while True:
        try:
            worked = await run_one(
                components.runtime.repository,
                components.workflow.run_lease,
                lease_seconds=components.lease_seconds,
                accepts_images=components.runtime.accepts_images,
            )
            if not worked:
                await asyncio.sleep(components.poll_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("consultation_worker_iteration_failed")
            await asyncio.sleep(components.poll_seconds)


async def start_consultation(
    app: FastAPI,
    components_factory: Callable[[], Awaitable[ConsultationComponents | None]],
) -> asyncio.Task[None] | None:
    """Install explicitly approved runtime components and start its worker.

    The factory returns ``None`` when consultation is not configured. This is
    the normal default-deny state. It must not manufacture a fallback identity
    adapter or provider when deployment configuration is incomplete.
    """
    components = await components_factory()
    state = app.state
    if getattr(state, "consultation_components", None) is not None:
        raise RuntimeError("consultation runtime is already activated")
    if components is None:
        state.consultation_runtime = None
        state.consultation_components = None
        state.consultation_worker = None
        return None

    state.consultation_runtime = components.runtime
    state.consultation_components = components
    if not components.worker_enabled:
        state.consultation_worker = None
        return None

    task = asyncio.create_task(_worker_loop(components), name="consultation-worker")
    state.consultation_worker = task
    return task


async def stop_consultation(app: FastAPI) -> None:
    """Stop the worker cleanly during application shutdown."""
    state = app.state
    task: asyncio.Task[None] | None = getattr(state, "consultation_worker", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    state.consultation_worker = None
    state.consultation_runtime = None
    state.consultation_components = None
