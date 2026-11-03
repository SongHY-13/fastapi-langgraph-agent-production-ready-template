"""Wire models for the AI consultation v0.4 contract."""

from base64 import b64decode
from binascii import Error as Base64Error
from typing import Annotated, Literal, Self

from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    UrlConstraints,
    model_validator,
)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_ENCODED_IMAGE_LENGTH = 4 * ((MAX_IMAGE_BYTES + 2) // 3)

type Identifier = Annotated[str, Field(min_length=1)]
type HttpsUrl = Annotated[AnyUrl, UrlConstraints(allowed_schemes=["https"], host_required=True)]
type GenerationStatus = Literal["queued", "running", "cancelling", "completed", "cancelled", "failed"]


class ConsultationModel(BaseModel):
    """Reject undocumented fields and omit input values in formatted validation errors."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ImageAttachment(ConsultationModel):
    """An uploaded image reference with exactly one transport."""

    file_id: Identifier
    media_type: Literal["image/jpeg", "image/png"]
    read_url: HttpsUrl | None = Field(default=None, repr=False)
    content_base64: str | None = Field(default=None, max_length=MAX_ENCODED_IMAGE_LENGTH, repr=False)

    @model_validator(mode="after")
    def validate_transport(self) -> Self:
        """Validate syntax and inline size without accessing remote storage."""
        if (self.read_url is None) == (self.content_base64 is None):
            raise ValueError("provide exactly one image transport")
        if self.read_url is not None and (self.read_url.username is not None or self.read_url.password is not None):
            raise ValueError("image URL must not include user credentials")
        if self.content_base64 is not None:
            try:
                decoded = b64decode(self.content_base64, validate=True)
            except (Base64Error, ValueError):
                raise ValueError("image content must be raw Base64") from None
            if not 0 < len(decoded) <= MAX_IMAGE_BYTES:
                raise ValueError("inline image exceeds the allowed size or is empty")
        return self


class AttachmentRequest(ConsultationModel):
    """Optional attachments shared by submission and regeneration."""

    attachments: list[ImageAttachment] = Field(default_factory=list, max_length=3)


class GenerationCreate(AttachmentRequest):
    """Submit the current user turn without client-provided identity or history."""

    client_message_id: Identifier
    text: str = Field(default="", repr=False)
    input_type: Literal["text", "voice_transcript"]

    @model_validator(mode="after")
    def require_content(self) -> Self:
        """Require at least one nonblank question or image."""
        if not self.text.strip() and not self.attachments:
            raise ValueError("text or at least one image is required")
        return self


class RegenerateRequest(AttachmentRequest):
    """Refresh access to original attachments without resubmitting the question."""


class CancelRequest(ConsultationModel):
    """Optionally identify the subscription to stop."""

    subscription_id: Identifier | None = None


class GenerationAccepted(ConsultationModel):
    """Return the task and user message IDs after submission."""

    generation_id: Identifier
    user_message_id: Identifier
    status: GenerationStatus
    request_id: Identifier


class CancelResult(ConsultationModel):
    """Report business status separately from stopped delivery."""

    status: GenerationStatus
    delivery_status: Literal["aborted"] | None = None
    request_id: Identifier


class RiskResource(ConsultationModel):
    """A verified resource for the platform to display."""

    label: str
    contact: str


class RiskNotice(ConsultationModel):
    """A risk signal, not confirmation of dispatch or human acceptance."""

    status: Literal["human_handoff_required"]
    message: str
    resources: list[RiskResource]


class Advisory(ConsultationModel):
    """An ordinary suggestion that the platform may choose to display."""

    code: Literal["human_handoff_suggested"]
    reason: Literal["consecutive_unanswered", "user_requested"]


class AnswerSource(ConsultationModel):
    """An approved source link and optional policy metadata."""

    source_id: Identifier
    title: str
    source_url: HttpsUrl
    document_number: str | None = None
    effective_status: Literal["effective"] | None = None


class AnswerAction(ConsultationModel):
    """A platform-defined action identifier and its parameters."""

    action_code: Identifier
    label: str
    params: dict[str, JsonValue]


class AnswerMetadata(ConsultationModel):
    """Metadata shared between the event stream and saved answer."""

    answer_id: Identifier
    sources: list[AnswerSource]
    actions: list[AnswerAction]
    followups: list[str]


class Answer(AnswerMetadata):
    """The complete reviewed answer."""

    text: str


class GenerationResult(ConsultationModel):
    """Recoverable result after reconnecting or completing a stream."""

    status: GenerationStatus
    answer: Answer | None
    risk: RiskNotice | None
    advisory: Advisory | None
    request_id: Identifier


class FailurePayload(ConsultationModel):
    """Stable error fields for a failed generation."""

    code: Identifier
    retryable: bool


class ConsultationError(FailurePayload):
    """JSON response for request errors and optional risk recovery."""

    message: str
    request_id: Identifier
    risk: RiskNotice | None = None


class DeltaPayload(ConsultationModel):
    """One reviewed text segment."""

    answer_id: Identifier
    text: str


class CompletedPayload(ConsultationModel):
    """Terminal event fields, including risk-only completions without an answer."""

    answer_id: Identifier | None
    advisory: Advisory | None


class EventEnvelope(ConsultationModel):
    """Fields shared by all server-sent events."""

    event_id: Identifier
    generation_id: Identifier
    seq: int = Field(ge=1)
    request_id: Identifier


class AnswerDeltaEvent(EventEnvelope):
    """Send one reviewed answer segment."""

    type: Literal["answer.delta"]
    payload: DeltaPayload


class AnswerMetadataEvent(EventEnvelope):
    """Send sources, actions and follow-up questions."""

    type: Literal["answer.metadata"]
    payload: AnswerMetadata


class RiskNoticeEvent(EventEnvelope):
    """Send a high-risk notice independently of answer text."""

    type: Literal["risk.notice"]
    payload: RiskNotice


class GenerationCompletedEvent(EventEnvelope):
    """Notify subscribers that the result is available."""

    type: Literal["generation.completed"]
    payload: CompletedPayload


class GenerationFailedEvent(EventEnvelope):
    """Notify subscribers about a terminal failure."""

    type: Literal["generation.failed"]
    payload: FailurePayload


class GenerationCancelledEvent(EventEnvelope):
    """Notify subscribers about a terminal cancellation."""

    type: Literal["generation.cancelled"]
    payload: ConsultationModel = Field(default_factory=ConsultationModel)


type GenerationEvent = Annotated[
    AnswerDeltaEvent
    | AnswerMetadataEvent
    | RiskNoticeEvent
    | GenerationCompletedEvent
    | GenerationFailedEvent
    | GenerationCancelledEvent,
    Field(discriminator="type"),
]
