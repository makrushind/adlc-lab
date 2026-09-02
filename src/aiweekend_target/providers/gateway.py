"""Normalized client for the separately pinned HF/LM Studio gateway."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

import httpx

from aiweekend_target.agent_protocol import strict_json
from aiweekend_target.core import ModelDescriptor, ModelInvocation
from aiweekend_target.gateway.correlation import correlation_headers


MAX_GATEWAY_DOCUMENT_BYTES = 512 * 1024


def _endpoint(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or len(value) > 2_048:
        raise ValueError("model gateway URL is invalid")
    parsed = urlsplit(value)
    suffix = "/chat/completions"
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith(suffix)
    ):
        raise ValueError("model gateway URL must be an absolute chat-completions endpoint")
    models_path = parsed.path[: -len(suffix)] + "/models"
    models = urlunsplit((parsed.scheme, parsed.netloc, models_path, "", ""))
    return value, models


async def _document(response: httpx.Response) -> dict[str, object]:
    content = bytearray()
    async for chunk in response.aiter_bytes():
        if len(content) + len(chunk) > MAX_GATEWAY_DOCUMENT_BYTES:
            raise RuntimeError("model gateway returned an oversized document")
        content.extend(chunk)
    try:
        value = strict_json(content.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise RuntimeError("model gateway returned invalid JSON") from error
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"model gateway request failed with status {response.status_code}")
    if not isinstance(value, dict):
        raise RuntimeError("model gateway returned a non-object document")
    return value


class GatewayModelProvider:
    """Provider-neutral engine adapter; backend selection remains in the gateway."""

    def __init__(
        self,
        *,
        model_id: str,
        chat_url: str,
        profile_id: str,
        timeout_seconds: float = 180.0,
    ) -> None:
        if not isinstance(model_id, str) or not model_id or len(model_id.encode()) > 512:
            raise ValueError("model id is invalid")
        if not isinstance(profile_id, str) or not profile_id:
            raise ValueError("model profile id is invalid")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int | float) or timeout_seconds <= 0:
            raise ValueError("model timeout is invalid")
        self._model_id = model_id
        self._chat_url, self._models_url = _endpoint(chat_url)
        self._profile_id = profile_id
        self._timeout = httpx.Timeout(
            connect=10.0,
            read=float(timeout_seconds),
            write=10.0,
            pool=10.0,
        )
        self._descriptor: ModelDescriptor | None = None

    async def describe(self) -> ModelDescriptor:
        if self._descriptor is not None:
            return self._descriptor
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=self._timeout, trust_env=False
        ) as client:
            async with client.stream("GET", self._models_url) as response:
                document = await _document(response)
        data = document.get("data")
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise RuntimeError("model gateway discovery contract failed")
        selected = data[0]
        if selected.get("id") != self._model_id:
            raise RuntimeError("model gateway selected a different model")
        raw_capabilities = selected.get("capabilities")
        capabilities = {"chat_completions"}
        if isinstance(raw_capabilities, Mapping):
            capabilities.update(
                key
                for key, enabled in raw_capabilities.items()
                if isinstance(key, str) and enabled is True
            )
        backend = selected.get("backend")
        provider = (
            f"{self._profile_id}:{backend}"
            if isinstance(backend, str) and backend
            else self._profile_id
        )
        self._descriptor = ModelDescriptor(
            self._model_id, frozenset(capabilities), provider
        )
        return self._descriptor

    async def complete(
        self, request: dict[str, object], invocation: ModelInvocation
    ) -> Mapping[str, object]:
        if request.get("model") != self._model_id:
            raise RuntimeError("model request selected a different model")
        headers = correlation_headers(invocation.run_id, invocation.request_id)
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=self._timeout, trust_env=False
        ) as client:
            async with client.stream(
                "POST", self._chat_url, json=request, headers=headers
            ) as response:
                for name, expected in headers.items():
                    if response.headers.get(name) != expected:
                        raise RuntimeError(
                            "model gateway correlation response contract failed"
                        )
                return await _document(response)


__all__ = ["GatewayModelProvider", "MAX_GATEWAY_DOCUMENT_BYTES"]
