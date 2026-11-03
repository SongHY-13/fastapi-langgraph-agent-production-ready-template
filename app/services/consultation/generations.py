"""Injectable admission and one durable worker iteration."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.schemas.consultation import (
    Advisory,
    Answer,
    FailurePayload,
    GenerationAccepted,
    GenerationCreate,
    ImageAttachment,
    RiskNotice,
)
from app.services.consultation.identity import PlatformAuthenticator, PlatformPrincipal
from app.services.consultation.repository import (
    ConsultationRepository,
    GenerationLease,
    RepositoryError,
    SubmissionPriority,
)


@dataclass(frozen=True, slots=True)
class ReviewedGeneration:
    """Reviewed output and its display metadata."""

    answer: Answer | None
    advisory: Advisory | None = None
    unanswered: bool = False
    effective: bool = True
    risk: RiskNotice | None = None


type AdmissionCheck = Callable[[PlatformPrincipal, str, GenerationCreate], Awaitable[RiskNotice | None]]
type RefreshAttachments = Callable[[PlatformPrincipal, list[ImageAttachment]], Awaitable[None]]
type ReviewedRunner = Callable[[GenerationLease], Awaitable[ReviewedGeneration]]


@dataclass(frozen=True, slots=True)
class ConsultationRuntime:
    """Explicitly configured trusted authentication, admission and durable storage."""

    repository: ConsultationRepository
    authenticate: PlatformAuthenticator
    check_admission: AdmissionCheck
    accepts_images: bool = False
    refresh_attachments: RefreshAttachments | None = None

    async def submit(
        self, principal: PlatformPrincipal, session_id: str, request: GenerationCreate, request_id: str
    ) -> GenerationAccepted:
        """Verify session and attachments, then classify risk before admission."""
        risk = await self.check_admission(principal, session_id, request)
        if risk is None and request.attachments and not self.accepts_images:
            raise RepositoryError("IMAGE_UNSUPPORTED")
        return await self.repository.create_or_get(
            principal,
            session_id,
            request,
            priority=SubmissionPriority.HIGH_RISK if risk else SubmissionPriority.NORMAL,
            risk=risk,
            request_id=request_id,
        )

    async def regenerate(
        self,
        principal: PlatformPrincipal,
        session_id: str,
        answer_id: str,
        idempotency_key: str,
        attachments: list[ImageAttachment],
        request_id: str,
    ) -> GenerationAccepted:
        """Verify ownership before authorizing refreshed attachment reads."""
        replay = await self.repository.regenerate(
            principal,
            session_id,
            answer_id,
            idempotency_key,
            attachments,
            request_id=request_id,
            probe=True,
        )
        if replay is not None:
            return replay
        if attachments:
            if not self.accepts_images:
                raise RepositoryError("IMAGE_UNSUPPORTED")
            if self.refresh_attachments is None:
                raise RepositoryError("ATTACHMENT_UNAVAILABLE")
            await self.refresh_attachments(principal, attachments)
        accepted = await self.repository.regenerate(
            principal,
            session_id,
            answer_id,
            idempotency_key,
            attachments,
            request_id=request_id,
        )
        assert isinstance(accepted, GenerationAccepted)
        return accepted


async def run_one(
    repository: ConsultationRepository,
    generate: ReviewedRunner,
    *,
    lease_seconds: int = 60,
    accepts_images: bool = False,
) -> bool:
    """Claim one task and atomically publish reviewed output with its terminal state."""
    if lease_seconds < 2:
        raise ValueError("worker lease must be at least two seconds")
    lease = await repository.claim(lease_seconds=lease_seconds)
    if lease is None:
        return False
    if lease.attachments and not accepts_images:
        await repository.finish(lease, failure=FailurePayload(code="IMAGE_UNSUPPORTED", retryable=False))
        return True
    try:
        reviewed = await asyncio.wait_for(generate(lease), timeout=lease_seconds - 1)
    except RepositoryError as exc:
        code = (
            exc.code
            if exc.code in {"ATTACHMENT_UNAVAILABLE", "IMAGE_UNSUPPORTED", "INVALID_ATTACHMENT"}
            else "GENERATION_FAILED"
        )
        await repository.finish(lease, failure=FailurePayload(code=code, retryable=code != "IMAGE_UNSUPPORTED"))
    except Exception:
        await repository.finish(lease, failure=FailurePayload(code="GENERATION_FAILED", retryable=True))
    else:
        if (
            lease.regeneration_target is not None
            and reviewed.risk is None
            and (reviewed.answer is None or not reviewed.effective or reviewed.unanswered)
        ):
            await repository.finish(lease, failure=FailurePayload(code="GENERATION_FAILED", retryable=True))
            return True
        await repository.finish(
            lease,
            answer=reviewed.answer,
            advisory=reviewed.advisory,
            unanswered=reviewed.unanswered,
            effective=reviewed.effective,
            risk=reviewed.risk,
        )
    return True
