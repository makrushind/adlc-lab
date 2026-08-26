"""The fixed Hugging Face Router transport boundary."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from aiweekend_target.errors import ErrorCode, TargetError, classify_upstream_status
from aiweekend_target.lab.config import BASE_MODEL, PROVIDER, ROUTER_URL

CHAT_URL = f"{ROUTER_URL}/chat/completions"
PROBE_TIMEOUT = httpx.Timeout(connect=2.0, read=2.0, write=2.0, pool=2.0)
CHAT_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=10.0, pool=10.0)


@dataclass(frozen=True)
class UpstreamResponse:
    status_code: int
    content: bytes
    content_type: str


@dataclass(frozen=True)
class UpstreamStream:
    status_code: int
    content_type: str
    body: AsyncIterator[bytes]


def _headers(secret: str, accept: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}", "Content-Type": "application/json", "Accept": accept}


def _upstream_error(status_code: int) -> TargetError:
    return TargetError(classify_upstream_status(status_code), "Hugging Face Router request failed", {"upstream_status": status_code})


def _provider_error() -> TargetError:
    return TargetError(ErrorCode.PROVIDER, "Hugging Face Router is unavailable")


async def probe_available(secret: str, transport: httpx.AsyncBaseTransport | None) -> None:
    """Verify only the selected provider's live tool-capable availability at the fixed Router."""
    url = f"{ROUTER_URL}/models/{BASE_MODEL}"
    try:
        async with httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            timeout=PROBE_TIMEOUT,
            trust_env=False,
        ) as client:
            request = httpx.Request("GET", url, headers=_headers(secret, "application/json"))
            response = await client.send(request)
    except httpx.HTTPError as error:
        raise _provider_error() from error
    if not 200 <= response.status_code < 300:
        raise _upstream_error(response.status_code)
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError) as error:
        raise _provider_error() from error
    if not isinstance(payload, dict):
        raise _provider_error()
    data = payload.get("data")
    if (
        not isinstance(data, dict)
        or data.get("id") != BASE_MODEL
        or not isinstance(data.get("providers"), list)
    ):
        raise _provider_error()
    for entry in data["providers"]:
        if (
            isinstance(entry, dict)
            and entry.get("provider") == PROVIDER
            and entry.get("status") == "live"
            and entry.get("supports_tools") is True
        ):
            return
    raise TargetError(ErrorCode.MODEL_UNAVAILABLE, "selected model provider is not live and tool-capable")


async def send_chat(
    body: Mapping[str, Any], secret: str, transport: httpx.AsyncBaseTransport | None
) -> UpstreamResponse | UpstreamStream:
    """Perform exactly one fixed-router chat call, preserving the upstream response form."""
    streaming = body.get("stream") is True
    accept = "text/event-stream" if streaming else "application/json"
    headers = _headers(secret, accept)
    client = httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        timeout=CHAT_TIMEOUT,
        trust_env=False,
    )
    try:
        request = httpx.Request(
            "POST",
            CHAT_URL,
            headers=headers,
            content=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )
        response = await client.send(request, stream=streaming)
    except httpx.HTTPError as error:
        await client.aclose()
        raise _provider_error() from error
    if not 200 <= response.status_code < 300:
        await response.aclose()
        await client.aclose()
        raise _upstream_error(response.status_code)
    content_type = response.headers.get("content-type", "application/json" if not streaming else "text/event-stream")
    if not streaming:
        try:
            content = await response.aread()
        finally:
            await response.aclose()
            await client.aclose()
        return UpstreamResponse(response.status_code, content, content_type)

    async def stream_body() -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return UpstreamStream(response.status_code, content_type, stream_body())
