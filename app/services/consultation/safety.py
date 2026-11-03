"""Fail-closed classification for consultation input."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from app.schemas.consultation import MAX_IMAGE_BYTES, ImageAttachment, RiskNotice


@dataclass(frozen=True, slots=True, repr=False)
class ConsultationTurn:
    """Ephemeral input from the current consultation turn."""

    request_id: str
    text: str
    attachments: tuple[ImageAttachment, ...] = ()
    images: tuple[ValidatedImage, ...] = ()


@dataclass(frozen=True, slots=True, repr=False)
class ValidatedImage:
    """Validated image data supplied by a trusted loader."""

    media_type: Literal["image/png", "image/jpeg"]
    content: bytes


@dataclass(frozen=True, slots=True)
class SafetyDecision:
    """A classified disposition and its approved notice, if any."""

    kind: Literal["high_risk", "blocked", "human_requested", "normal", "unavailable"]
    risk: RiskNotice | None = None

    def __post_init__(self) -> None:
        """Require approved display data for high-risk decisions."""
        if (self.kind == "high_risk") != (self.risk is not None):
            raise ValueError("high risk requires a verified notice")


type SafetyClassifier = Callable[[str, tuple[ValidatedImage, ...]], Awaitable[SafetyDecision]]
type ImageLoader = Callable[[ImageAttachment], Awaitable[ValidatedImage]]


@dataclass(frozen=True, slots=True)
class SafetyGate:
    """Classify text before accessing any image transport."""

    classifier: SafetyClassifier | None = field(default=None, repr=False)
    image_loader: ImageLoader | None = field(default=None, repr=False)
    timeout_seconds: float = 5

    async def inspect(
        self, turn: ConsultationTurn, *, text_decision: SafetyDecision | None = None
    ) -> tuple[SafetyDecision, tuple[ValidatedImage, ...]]:
        """Return the disposition and images permitted for ordinary processing."""
        if self.classifier is None:
            return SafetyDecision("unavailable"), ()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                if text_decision is None and turn.text.strip():
                    text_decision = await self.classifier(turn.text, ())
                if text_decision is not None and text_decision.kind == "high_risk":
                    return text_decision, ()
                if not turn.attachments and not turn.images:
                    return text_decision or SafetyDecision("unavailable"), ()
                images = turn.images
                if turn.attachments:
                    if self.image_loader is None:
                        return SafetyDecision("unavailable"), ()
                    images = tuple(await asyncio.gather(*(self.image_loader(item) for item in turn.attachments)))
                    if any(
                        image.media_type != item.media_type
                        for image, item in zip(images, turn.attachments, strict=True)
                    ):
                        return SafetyDecision("unavailable"), ()
                if any(not 0 < len(image.content) <= MAX_IMAGE_BYTES for image in images):
                    return SafetyDecision("unavailable"), ()
                image_decision = await self.classifier(turn.text, images)
                if image_decision.kind in {"high_risk", "unavailable"}:
                    return image_decision, ()
                if text_decision is not None and text_decision.kind == "blocked":
                    return text_decision, ()
                if image_decision.kind == "blocked":
                    return image_decision, ()
                if text_decision is not None and text_decision.kind in {"unavailable", "human_requested"}:
                    return text_decision, ()
                return image_decision, images
        except Exception:
            return SafetyDecision("unavailable"), ()

    async def classify_input(self, turn: ConsultationTurn) -> SafetyDecision:
        """Return a fail-closed input disposition."""
        decision, _ = await self.inspect(turn)
        return decision
