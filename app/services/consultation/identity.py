"""Trusted identity supplied by a verified platform authentication adapter."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PlatformPrincipal:
    """Verified caller, tenant and end-user identity."""

    caller_id: str
    tenant_id: str
    subject_id: str

    def __post_init__(self) -> None:
        """Reject incomplete trusted identities."""
        if not all(value.strip() for value in (self.caller_id, self.tenant_id, self.subject_id)):
            raise ValueError("platform principal fields must be nonblank")


class PlatformAuthenticationRequired(Exception):
    """A trusted platform principal could not be established."""


class PlatformAuthenticator(Protocol):
    """Integration boundary for the platform's eventual signing protocol."""

    async def __call__(self, authorization: str | None) -> PlatformPrincipal:
        """Verify authorization and return the trusted identity."""
        ...


async def authenticate_platform(authorization: str | None) -> PlatformPrincipal:
    """Deny external calls until a verified authentication adapter is configured."""
    raise PlatformAuthenticationRequired
