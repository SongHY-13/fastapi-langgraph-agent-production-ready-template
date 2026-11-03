"""Bounded HTTP requests for consultation providers."""

import asyncio
import json
from typing import override

import httpx


class ProviderUnavailable(Exception):
    """Provider call failed without exposing confidential request details."""


class _BorrowedTransport(httpx.AsyncBaseTransport):
    """Forward requests while the runtime retains ownership of the transport."""

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self._transport = transport

    @override
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._transport.handle_async_request(request)


async def bounded_request(
    transport: httpx.AsyncBaseTransport,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, object] | None = None,
    timeout: float,
    max_bytes: int,
) -> tuple[bytes, str]:
    """Use a configured transport without redirects, proxies or unbounded buffering."""
    try:
        async with (
            asyncio.timeout(timeout),
            httpx.AsyncClient(
                transport=_BorrowedTransport(transport), timeout=timeout, trust_env=False, follow_redirects=False
            ) as client,
        ):
            headers = {**headers, "Accept-Encoding": "identity"}
            async with client.stream(method, url, headers=headers, json=payload) as response:
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ProviderUnavailable
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > max_bytes:
                        raise ProviderUnavailable
                    body.extend(chunk)
                return bytes(body), response.headers.get("content-type", "").split(";", 1)[0].lower()
    except (httpx.HTTPError, TimeoutError, ValueError):
        raise ProviderUnavailable from None


def parse_json(body: bytes | str) -> object:
    """Parse untrusted JSON without retaining its values in exception messages."""
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        raise ProviderUnavailable from None
