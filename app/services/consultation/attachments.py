"""Resolve authorized image bytes for this turn only."""

from base64 import b64decode
from binascii import Error as Base64Error
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

import httpx

from app.schemas.consultation import MAX_ENCODED_IMAGE_LENGTH, MAX_IMAGE_BYTES, ImageAttachment
from app.services.consultation.identity import PlatformPrincipal
from app.services.consultation.provider_http import ProviderUnavailable, bounded_request
from app.services.consultation.repository import GenerationLease, RepositoryError
from app.services.consultation.safety import ValidatedImage

type AuthorizeImage = Callable[[PlatformPrincipal, str], Awaitable[ImageAttachment | None]]
type DecodeImage = Callable[[ValidatedImage], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class ImageResolver:
    """Only load images returned by the principal-bound original-file authorizer."""

    authorize: AuthorizeImage = field(repr=False)
    decode: DecodeImage | None = field(repr=False)
    transport: httpx.AsyncBaseTransport = field(repr=False)
    approved_hosts: frozenset[str]

    async def load(self, attachment: ImageAttachment) -> ValidatedImage:
        """Validate a previously authorized image transport."""
        if self.decode is None:
            raise RepositoryError("IMAGE_UNSUPPORTED")
        if attachment.read_url is not None:
            url = attachment.read_url
            if (
                url.scheme != "https"
                or url.host not in self.approved_hosts
                or url.port not in (None, 443)
                or url.username is not None
                or url.password is not None
                or url.fragment is not None
            ):
                raise RepositoryError("INVALID_ATTACHMENT")
            try:
                content, media_type = await bounded_request(
                    self.transport, "GET", str(url), headers={}, timeout=3, max_bytes=MAX_IMAGE_BYTES
                )
            except ProviderUnavailable:
                raise RepositoryError("ATTACHMENT_UNAVAILABLE") from None
            if media_type != attachment.media_type:
                raise RepositoryError("INVALID_ATTACHMENT")
        else:
            encoded = attachment.content_base64 or ""
            if len(encoded) > MAX_ENCODED_IMAGE_LENGTH:
                raise RepositoryError("INVALID_ATTACHMENT")
            try:
                content = b64decode(encoded, validate=True)
            except (ValueError, Base64Error):
                raise RepositoryError("INVALID_ATTACHMENT") from None
        signatures = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff"}
        if not 0 < len(content) <= MAX_IMAGE_BYTES or not content.startswith(signatures[attachment.media_type]):
            raise RepositoryError("INVALID_ATTACHMENT")
        image = ValidatedImage(attachment.media_type, content)
        if not await self.decode(image):
            raise RepositoryError("INVALID_ATTACHMENT")
        return image

    async def load_images(
        self, attachments: Sequence[ImageAttachment], principal: PlatformPrincipal
    ) -> list[ValidatedImage]:
        """Refresh only the original file IDs under verified identity."""
        return [await self._load_original(item.file_id, item.media_type, principal) for item in attachments]

    async def _load_original(self, file_id: str, media_type: str, principal: PlatformPrincipal) -> ValidatedImage:
        original = await self.authorize(principal, file_id)
        if original is None or original.file_id != file_id or original.media_type != media_type:
            raise RepositoryError("ATTACHMENT_UNAVAILABLE")
        return await self.load(original)

    async def resolve_lease(self, lease: GenerationLease) -> tuple[ValidatedImage, ...]:
        """Resolve stored metadata without treating it as an image transport."""
        return tuple(
            [
                await self._load_original(item["file_id"], item["media_type"], lease.principal)
                for item in lease.attachments
            ]
        )
