"""Controlled consultation graph with request data held outside checkpoint state."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypedDict
from uuid import uuid4

from langchain_core.callbacks import BaseCallbackHandler
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.schemas.consultation import (
    Advisory,
    Answer,
    AnswerAction,
    AnswerSource,
    GenerationCreate,
    GenerationResult,
    RiskNotice,
)
from app.services.consultation.generations import ReviewedGeneration
from app.services.consultation.identity import PlatformPrincipal
from app.services.consultation.repository import GenerationLease, RepositoryError
from app.services.consultation.safety import ConsultationTurn, SafetyDecision, SafetyGate, ValidatedImage


@dataclass(frozen=True, slots=True, repr=False)
class Evidence:
    """A trusted applicable source with its supporting passage."""

    source: AnswerSource
    excerpt: str


@dataclass(frozen=True, slots=True, repr=False)
class ScopedInput:
    """Current content approved for provider processing."""

    text: str
    images: tuple[ValidatedImage, ...]
    history: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True, repr=False)
class Candidate:
    """Draft output awaiting citation, action and safety review."""

    text: str
    source_ids: tuple[str, ...]
    actions: tuple[AnswerAction, ...] = ()
    followups: tuple[str, ...] = ()


type Retrieve = Callable[[ScopedInput], Awaitable[tuple[Evidence, ...]]]
type Draft = Callable[[ScopedInput, tuple[Evidence, ...]], Awaitable[Candidate]]
type Review = Callable[[ScopedInput, Answer], Awaitable[bool]]
type VerifyCitations = Callable[[Answer, tuple[Evidence, ...]], Awaitable[bool]]
type InScope = Callable[[ScopedInput], Awaitable[bool]]
type ResolveImages = Callable[[GenerationLease], Awaitable[tuple[ValidatedImage, ...]]]


class _State(TypedDict):
    outcome: str


@dataclass(slots=True, repr=False)
class _Run:
    turn: ConsultationTurn
    text_decision: SafetyDecision | None = None
    history: tuple[tuple[str, str], ...] = ()
    result: GenerationResult | None = None
    unanswered: bool = False
    effective: bool = False


class ConsultationWorkflow:
    """Run with trusted providers and pass only outcome markers to graph tracing."""

    def __init__(
        self,
        safety: SafetyGate,
        retrieve: Retrieve,
        draft: Draft,
        review: Review,
        in_scope: InScope,
        *,
        allowed_actions: tuple[AnswerAction, ...] = (),
        verify_citations: VerifyCitations | None = None,
        resolve_images: ResolveImages | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
        trace_handler: BaseCallbackHandler | None = None,
    ) -> None:
        """Build the graph with explicitly trusted provider contracts."""
        self.safety = safety
        self._retrieve = retrieve
        self._draft = draft
        self._review = review
        self._in_scope = in_scope
        self._allowed_actions = allowed_actions
        self._verify_citations = verify_citations
        self._resolve_images = resolve_images
        self._trace_handler = trace_handler
        graph = StateGraph(_State, context_schema=_Run)
        graph.add_node("controlled_consultation", self._consult)
        graph.add_edge(START, "controlled_consultation")
        graph.add_edge("controlled_consultation", END)
        self._graph = graph.compile(checkpointer=checkpointer)

    async def _consult(self, state: _State, runtime: Runtime[_Run]) -> _State:
        run = runtime.context
        turn = run.turn
        decision, images = await self.safety.inspect(turn, text_decision=run.text_decision)
        result = GenerationResult(
            status="completed", answer=None, risk=decision.risk, advisory=None, request_id=turn.request_id
        )
        run.result = result
        if decision.kind == "high_risk":
            return {"outcome": "high_risk"}
        if decision.kind == "unavailable":
            result.status = "failed"
            return {"outcome": "safety_unavailable"}
        if decision.kind == "human_requested":
            result.advisory = Advisory(code="human_handoff_suggested", reason="user_requested")
            return {"outcome": "human_requested"}
        if decision.kind == "blocked":
            result.answer = self._message("抱歉，无法回应该请求。")
            return {"outcome": "input_blocked"}
        if images and not turn.text.strip():
            result.answer = self._message("你想了解图片中的哪部分内容？")
            return {"outcome": "clarification"}

        scoped = ScopedInput(turn.text, images, run.history)
        if not await self._in_scope(scoped):
            result.answer = self._message("该问题超出本咨询的服务范围。")
            run.unanswered = True
            return {"outcome": "out_of_scope"}
        try:
            evidence = await self._retrieve(scoped)
        except Exception:
            result.status = "failed"
            return {"outcome": "retrieval_unavailable"}
        if not evidence:
            result.answer = self._message("暂未找到适用的依据。")
            run.unanswered = True
            return {"outcome": "no_evidence"}

        try:
            candidate = await self._draft(scoped, evidence)
            sources = {item.source.source_id: item.source for item in evidence}
            if (
                not candidate.text.strip()
                or not candidate.source_ids
                or any(key not in sources for key in candidate.source_ids)
                or any(action not in self._allowed_actions for action in candidate.actions)
            ):
                raise ValueError("unverified candidate")
            answer = Answer(
                answer_id=uuid4().hex,
                text=candidate.text,
                sources=[sources[key] for key in dict.fromkeys(candidate.source_ids)],
                actions=list(candidate.actions),
                followups=list(candidate.followups),
            )
            if not await self._review(scoped, answer):
                raise ValueError("rejected candidate")
            if self._verify_citations is None or not await self._verify_citations(answer, evidence):
                raise ValueError("citation approval expired")
        except Exception:
            result.answer = self._message("暂无法提供经核实的答复。")
            return {"outcome": "output_blocked"}
        result.answer = answer
        run.effective = True
        return {"outcome": "answered"}

    @staticmethod
    def _message(text: str) -> Answer:
        return Answer(answer_id=uuid4().hex, text=text, sources=[], actions=[], followups=[])

    async def _run(
        self,
        turn: ConsultationTurn,
        text_decision: SafetyDecision | None = None,
        *,
        history: tuple[tuple[str, str], ...] = (),
        thread_id: str | None = None,
    ) -> _Run:
        run = _Run(turn, text_decision, history)
        await self._graph.ainvoke(
            {"outcome": "started"},
            config={
                "configurable": {"thread_id": thread_id or uuid4().hex},
                "callbacks": [self._trace_handler] if self._trace_handler else [],
            },
            context=run,
        )
        return run

    async def run_consultation(self, turn: ConsultationTurn) -> GenerationResult:
        """Return reviewed wire data for the current turn."""
        run = await self._run(turn)
        assert run.result is not None
        return run.result

    async def check_admission(
        self, principal: PlatformPrincipal, session_id: str, request: GenerationCreate
    ) -> RiskNotice | None:
        """Classify an already platform-authorized submission before slot admission."""
        decision = await self.safety.classify_input(
            ConsultationTurn(uuid4().hex, request.text, tuple(request.attachments))
        )
        if decision.kind == "unavailable":
            raise RepositoryError("SERVICE_UNAVAILABLE")
        return decision.risk

    async def run_lease(self, lease: GenerationLease) -> ReviewedGeneration:
        """Adapt reviewed output to the durable worker finalizer."""
        images = ()
        text_decision = None
        if lease.attachments and lease.text.strip():
            text_decision = await self.safety.classify_input(ConsultationTurn(lease.generation_id, lease.text))
        if lease.attachments and (text_decision is None or text_decision.kind != "high_risk"):
            if self._resolve_images is None:
                raise RepositoryError("ATTACHMENT_UNAVAILABLE")
            images = await self._resolve_images(lease)
            if len(images) != len(lease.attachments) or any(
                image.media_type != attachment["media_type"]
                for image, attachment in zip(images, lease.attachments, strict=True)
            ):
                raise RepositoryError("ATTACHMENT_UNAVAILABLE")
        run = await self._run(
            ConsultationTurn(lease.generation_id, lease.text, images=images),
            text_decision,
            history=lease.history,
            thread_id=lease.session_id,
        )
        result = run.result
        assert result is not None
        if result.status != "completed":
            raise RepositoryError("GENERATION_FAILED")
        return ReviewedGeneration(result.answer, result.advisory, run.unanswered, run.effective, result.risk)
