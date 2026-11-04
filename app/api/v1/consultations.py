"""Scoped consultation HTTP and event transport."""

from collections.abc import Callable, Coroutine
from re import fullmatch
from typing import Annotated, Any, override
from uuid import uuid4

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from slowapi.errors import RateLimitExceeded

from app.core.limiter import limiter
from app.schemas.consultation import (
    CancelRequest,
    CancelResult,
    ConsultationError,
    GenerationAccepted,
    GenerationCreate,
    GenerationResult,
    RegenerateRequest,
)
from app.services.consultation.events import sse_frames
from app.services.consultation.generations import ConsultationRuntime
from app.services.consultation.identity import PlatformAuthenticationRequired, PlatformPrincipal, authenticate_platform
from app.services.consultation.repository import RepositoryError

_ERRORS = {
    "UNAUTHORIZED": (401, "Platform authentication required", False),
    "FORBIDDEN": (403, "Access denied", False),
    "INVALID_REQUEST": (422, "Invalid consultation request", False),
    "GENERATION_NOT_FOUND": (404, "Generation unavailable", False),
    "IDEMPOTENCY_CONFLICT": (409, "Message ID was already used", False),
    "REGENERATION_NOT_ALLOWED": (409, "Answer cannot be regenerated", False),
    "GENERATION_IN_PROGRESS": (409, "A generation is in progress", True),
    "EVENT_HISTORY_EXPIRED": (410, "Event history expired", False),
    "RATE_LIMITED": (429, "Too many requests", True),
    "SERVICE_UNAVAILABLE": (503, "Consultation unavailable", True),
    "INVALID_ATTACHMENT": (422, "Invalid attachment", False),
    "ATTACHMENT_UNAVAILABLE": (422, "Attachment unavailable", True),
    "IMAGE_UNSUPPORTED": (422, "Image input unavailable", False),
}


class ConsultationRoute(APIRoute):
    """Handle local validation errors without rendering their input or location."""

    @override
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap the route with consultation-specific error handling."""
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            request.state.consultation_request_id = uuid4().hex
            try:
                return await handler(request)
            except RequestValidationError:
                code = "INVALID_REQUEST"
            except PlatformAuthenticationRequired:
                code = "UNAUTHORIZED"
            except RateLimitExceeded:
                code = "RATE_LIMITED"
            except RepositoryError as exc:
                code = exc.code
            status, message, retryable = _ERRORS.get(code, _ERRORS["SERVICE_UNAVAILABLE"])
            if code not in _ERRORS:
                code = "SERVICE_UNAVAILABLE"
            error = ConsultationError(
                code=code, message=message, retryable=retryable, request_id=request.state.consultation_request_id
            )
            return JSONResponse(error.model_dump(exclude_none=True), status_code=status)

        return handle


router = APIRouter(route_class=ConsultationRoute)


async def _access(request: Request, authorization: str | None) -> tuple[ConsultationRuntime, PlatformPrincipal]:
    runtime = getattr(request.app.state, "consultation_runtime", None)
    if not isinstance(runtime, ConsultationRuntime):
        await authenticate_platform(authorization)
        raise PlatformAuthenticationRequired
    local_scope: tuple[str, str] | None = getattr(request.app.state, "consultation_local_scope", None)
    if local_scope is not None:
        subjects = request.headers.getlist("x-consultation-subject")
        if (
            request.client is None
            or request.client.host not in {"127.0.0.1", "::1"}
            or "forwarded" in request.headers
            or any(name.startswith("x-forwarded-") for name in request.headers)
            or len(subjects) != 1
            or fullmatch(r"[A-Za-z0-9._:-]{1,128}", subjects[0]) is None
        ):
            raise PlatformAuthenticationRequired
        return runtime, PlatformPrincipal(*local_scope, subjects[0])
    return runtime, await runtime.authenticate(authorization)


Authorization = Annotated[str | None, Header()]


@router.post("/sessions/{session_id}/generations", status_code=202)
@limiter.limit("30/minute")
async def submit(
    request: Request, session_id: str, body: GenerationCreate, authorization: Authorization = None
) -> GenerationAccepted:
    """Classify and register a scoped turn."""
    runtime, principal = await _access(request, authorization)
    return await runtime.submit(principal, session_id, body, request.state.consultation_request_id)


@router.post("/sessions/{session_id}/answers/{answer_id}/regenerate", status_code=202)
@limiter.limit("30/minute")
async def regenerate(
    request: Request,
    session_id: str,
    answer_id: str,
    body: RegenerateRequest,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
    authorization: Authorization = None,
) -> GenerationAccepted:
    """Queue another attempt for the current ordinary answer."""
    runtime, principal = await _access(request, authorization)
    return await runtime.regenerate(
        principal,
        session_id,
        answer_id,
        idempotency_key,
        body.attachments,
        request.state.consultation_request_id,
    )


@router.get("/generations/{generation_id}")
@limiter.limit("120/minute")
async def result(request: Request, generation_id: str, authorization: Authorization = None) -> GenerationResult:
    """Return the durable result for the caller."""
    runtime, principal = await _access(request, authorization)
    return await runtime.repository.get(principal, generation_id, request_id=request.state.consultation_request_id)


@router.get("/generations/{generation_id}/events")
@limiter.limit("60/minute")
async def subscribe(
    request: Request,
    generation_id: str,
    after_seq: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0,
    authorization: Authorization = None,
) -> StreamingResponse:
    """Authorize and check replay availability before starting SSE."""
    runtime, principal = await _access(request, authorization)
    subscription_id = await runtime.repository.open_subscription(principal, generation_id, after_seq)
    return StreamingResponse(
        sse_frames(runtime.repository, principal, generation_id, after_seq, subscription_id),
        media_type="text/event-stream",
        headers={"X-Subscription-ID": subscription_id, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/generations/{generation_id}/cancel", response_model_exclude_none=True)
@limiter.limit("60/minute")
async def cancel(
    request: Request, generation_id: str, body: CancelRequest, authorization: Authorization = None
) -> CancelResult:
    """Cancel active work and optionally stop one delivery."""
    runtime, principal = await _access(request, authorization)
    return await runtime.repository.cancel(
        principal, generation_id, body.subscription_id, request.state.consultation_request_id
    )
