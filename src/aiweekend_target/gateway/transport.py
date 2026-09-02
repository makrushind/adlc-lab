"""Provider-neutral transport boundary for one pinned model profile."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from aiweekend_target.errors import ErrorCode, TargetError, classify_upstream_status
from aiweekend_target.lab.config import DEFAULT_MODEL_PROFILE, ModelProfile


CHAT_URL = f"{DEFAULT_MODEL_PROFILE.base_url}/chat/completions"
PROBE_TIMEOUT = httpx.Timeout(connect=2.0, read=2.0, write=2.0, pool=2.0)
CHAT_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=10.0, pool=10.0)
MAX_ERROR_SAMPLE_BYTES = 16 * 1024
MAX_DISCOVERY_BYTES = 1024 * 1024
MAX_CHAT_RESPONSE_BYTES = 512 * 1024
MAX_CONTENT_LENGTH_DIGITS = 20
_SAFE_ERROR_TYPES = frozenset({
    "authentication_error",
    "generation_error",
    "invalid_request_error",
    "not_found_error",
    "permission_error",
    "rate_limit_error",
    "server_error",
})
_SAFE_ERROR_CODES = frozenset({
    "context_length_exceeded",
    "generation_failed",
    "invalid_api_key",
    "model_not_found",
    "rate_limit_exceeded",
    "tool_validation_failed",
    "unsupported_value",
})
_SAFE_ERROR_PARAMS = frozenset({
    "max_completion_tokens",
    "max_tokens",
    "messages",
    "model",
    "response_format",
    "stop",
    "stream",
    "temperature",
    "tool_choice",
    "tools",
    "top_p",
})


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


@dataclass(frozen=True)
class ModelCapabilities:
    """Capabilities asserted by provider metadata, not inferred from a model name."""

    model_id: str
    backend: str
    chat_completions: bool
    tool_calls: bool | None
    vision: bool | None = None
    max_context_length: int | None = None
    loaded: bool | None = None
    source: str = "provider-metadata"

    def as_dict(self) -> dict[str, object]:
        return {
            "chat_completions": self.chat_completions,
            "tool_calls": self.tool_calls,
            "vision": self.vision,
            "max_context_length": self.max_context_length,
            "loaded": self.loaded,
            "source": self.source,
        }


def _headers(secret: str | None, accept: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": accept}
    if secret is not None:
        headers["Authorization"] = f"Bearer {secret}"
    return headers


def _upstream_error(
    status_code: int,
    diagnostics: Mapping[str, object] | None = None,
    profile: ModelProfile = DEFAULT_MODEL_PROFILE,
) -> TargetError:
    return TargetError(
        classify_upstream_status(status_code),
        f"{profile.label} request failed",
        {"upstream_status": status_code},
        diagnostics=diagnostics,
    )


def _provider_error(profile: ModelProfile = DEFAULT_MODEL_PROFILE) -> TargetError:
    return TargetError(ErrorCode.PROVIDER, f"{profile.label} is unavailable")


def _declared_error_body_length(response: httpx.Response) -> int | None:
    value = response.headers.get("content-length")
    if (
        type(value) is not str
        or not value
        or len(value) > MAX_CONTENT_LENGTH_DIGITS
        or not value.isascii()
        or not value.isdecimal()
    ):
        return None
    return int(value)


def _safe_upstream_field(value: object, allowed: frozenset[str]) -> str | None:
    if type(value) is str and value in allowed:
        return value
    return None


def _error_diagnostics(status_code: int, sample: bytes, truncated: bool) -> dict[str, object]:
    error_type: str | None = None
    error_code: str | None = None
    error_param: str | None = None
    has_failed_generation = False
    try:
        payload = json.loads(sample)
    except (UnicodeDecodeError, ValueError):
        payload = None
    if isinstance(payload, Mapping):
        has_failed_generation = "failed_generation" in payload
        error = payload.get("error")
        if isinstance(error, Mapping):
            error_type = _safe_upstream_field(error.get("type"), _SAFE_ERROR_TYPES)
            error_code = _safe_upstream_field(error.get("code"), _SAFE_ERROR_CODES)
            error_param = _safe_upstream_field(error.get("param"), _SAFE_ERROR_PARAMS)
    return {
        "status": status_code,
        "error_type": error_type,
        "error_code": error_code,
        "error_param": error_param,
        "has_failed_generation": has_failed_generation,
        "sample_bytes": len(sample),
        "sample_sha256": hashlib.sha256(sample).hexdigest(),
        "truncated": truncated,
    }


async def _bounded_error_diagnostics(response: httpx.Response) -> dict[str, object]:
    declared_length = _declared_error_body_length(response)
    if declared_length is None or declared_length > MAX_ERROR_SAMPLE_BYTES:
        return _error_diagnostics(response.status_code, b"", True)
    if response.is_stream_consumed:
        retained = response.content[:MAX_ERROR_SAMPLE_BYTES]
        return _error_diagnostics(
            response.status_code,
            retained,
            len(response.content) > MAX_ERROR_SAMPLE_BYTES,
        )
    sample = bytearray()
    truncated = False
    async for chunk in response.aiter_raw(chunk_size=MAX_ERROR_SAMPLE_BYTES):
        remaining = MAX_ERROR_SAMPLE_BYTES - len(sample)
        if len(chunk) > remaining:
            sample.extend(chunk[:remaining])
            truncated = True
            break
        sample.extend(chunk)
        if len(sample) > MAX_ERROR_SAMPLE_BYTES:
            del sample[MAX_ERROR_SAMPLE_BYTES:]
            truncated = True
            break
        if response.num_bytes_downloaded > MAX_ERROR_SAMPLE_BYTES:
            truncated = True
        if len(sample) == MAX_ERROR_SAMPLE_BYTES:
            break
    return _error_diagnostics(response.status_code, bytes(sample), truncated)


async def _bounded_json(response: httpx.Response, profile: ModelProfile) -> object:
    declared_length = _declared_error_body_length(response)
    if declared_length is not None and declared_length > MAX_DISCOVERY_BYTES:
        raise _provider_error(profile)
    content = bytearray()
    if response.is_stream_consumed:
        if len(response.content) > MAX_DISCOVERY_BYTES:
            raise _provider_error(profile)
        content.extend(response.content)
    else:
        async for chunk in response.aiter_raw(chunk_size=64 * 1024):
            if len(content) + len(chunk) > MAX_DISCOVERY_BYTES:
                raise _provider_error(profile)
            content.extend(chunk)
    try:
        return json.loads(content)
    except (UnicodeDecodeError, ValueError) as error:
        raise _provider_error(profile) from error


async def _bounded_chat_content(response: httpx.Response, profile: ModelProfile) -> bytes:
    declared_length = _declared_error_body_length(response)
    if declared_length is not None and declared_length > MAX_CHAT_RESPONSE_BYTES:
        raise _provider_error(profile)
    if response.is_stream_consumed:
        if len(response.content) > MAX_CHAT_RESPONSE_BYTES:
            raise _provider_error(profile)
        return response.content

    content = bytearray()
    async for chunk in response.aiter_raw(chunk_size=64 * 1024):
        if len(content) + len(chunk) > MAX_CHAT_RESPONSE_BYTES:
            raise _provider_error(profile)
        content.extend(chunk)
    return bytes(content)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object member")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> None:
    raise ValueError("non-standard JSON numeric constant")


def _validate_chat_response(content: bytes, profile: ModelProfile) -> None:
    try:
        payload = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise _provider_error(profile) from error
    if type(payload) is not dict:
        raise _provider_error(profile)

    allowed_model_ids = {profile.model_id}
    if profile.backend == "huggingface" and profile.discovery_model_id is not None:
        allowed_model_ids.add(profile.discovery_model_id)
    response_model = payload.get("model")
    if type(response_model) is not str or response_model not in allowed_model_ids:
        raise TargetError(
            ErrorCode.MODEL_UNAVAILABLE,
            "upstream chat response does not identify the selected model",
        )


async def _request_json(
    client: httpx.AsyncClient,
    url: str,
    secret: str | None,
    profile: ModelProfile,
) -> object:
    request = httpx.Request("GET", url, headers=_headers(secret, "application/json"))
    response = await client.send(request, stream=True)
    if not 200 <= response.status_code < 300:
        try:
            diagnostics = await _bounded_error_diagnostics(response)
        finally:
            await response.aclose()
        raise _upstream_error(response.status_code, diagnostics, profile)
    try:
        return await _bounded_json(response, profile)
    finally:
        await response.aclose()


def _lmstudio_native_models_url(profile: ModelProfile) -> str:
    parsed = urlsplit(profile.base_url)
    base_path = parsed.path.rstrip("/")[:-3].rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, f"{base_path}/api/v1/models", "", ""))


def _hf_capabilities(payload: object, profile: ModelProfile) -> ModelCapabilities:
    if not isinstance(payload, dict):
        raise _provider_error(profile)
    data = payload.get("data")
    if (
        not isinstance(data, dict)
        or data.get("id") != profile.discovery_model_id
        or not isinstance(data.get("providers"), list)
    ):
        raise _provider_error(profile)
    for entry in data["providers"]:
        if isinstance(entry, dict) and entry.get("provider") == profile.provider and entry.get("status") == "live":
            tool_calls = entry.get("supports_tools") if type(entry.get("supports_tools")) is bool else None
            if profile.require_tools and tool_calls is not True:
                raise TargetError(
                    ErrorCode.MODEL_UNAVAILABLE,
                    "selected model provider is not live and tool-capable",
                )
            return ModelCapabilities(
                model_id=profile.model_id,
                backend=profile.backend,
                chat_completions=True,
                tool_calls=tool_calls,
                loaded=True,
                source="huggingface-provider-metadata",
            )
    raise TargetError(ErrorCode.MODEL_UNAVAILABLE, "selected model provider is not live and tool-capable")


def _openai_model_is_available(payload: object, profile: ModelProfile) -> None:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise _provider_error(profile)
    for entry in payload["data"]:
        if isinstance(entry, dict) and entry.get("id") == profile.model_id and entry.get("object") == "model":
            return
    raise TargetError(ErrorCode.MODEL_UNAVAILABLE, "selected model is not advertised by LM Studio")


def _lmstudio_capabilities(payload: object, profile: ModelProfile) -> ModelCapabilities:
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise _provider_error(profile)
    selected: Mapping[str, object] | None = None
    for entry in payload["models"]:
        if not isinstance(entry, Mapping):
            continue
        instances = entry.get("loaded_instances")
        instance_match = isinstance(instances, list) and any(
            isinstance(instance, Mapping) and instance.get("id") == profile.model_id for instance in instances
        )
        if entry.get("key") == profile.model_id or instance_match:
            selected = entry
            break
    if selected is None:
        raise TargetError(ErrorCode.MODEL_UNAVAILABLE, "selected model metadata is unavailable from LM Studio")
    if selected.get("type") != "llm":
        raise TargetError(ErrorCode.MODEL_UNAVAILABLE, "selected LM Studio model is not a chat model")

    capabilities = selected.get("capabilities")
    if not isinstance(capabilities, Mapping):
        capabilities = {}
    tool_calls = capabilities.get("trained_for_tool_use")
    if type(tool_calls) is not bool:
        tool_calls = None
    if profile.require_tools and tool_calls is not True:
        raise TargetError(ErrorCode.MODEL_UNAVAILABLE, "selected LM Studio model is not declared tool-capable")
    vision = capabilities.get("vision")
    if type(vision) is not bool:
        vision = None
    context_length = selected.get("max_context_length")
    if type(context_length) is not int or context_length <= 0:
        context_length = None
    instances = selected.get("loaded_instances")
    loaded = None if not isinstance(instances, list) else any(
        isinstance(instance, Mapping) and instance.get("id") == profile.model_id for instance in instances
    )
    return ModelCapabilities(
        model_id=profile.model_id,
        backend=profile.backend,
        chat_completions=True,
        tool_calls=tool_calls,
        vision=vision,
        max_context_length=context_length,
        loaded=loaded,
        source="lmstudio-native-metadata",
    )


async def discover_model(
    secret: str | None,
    transport: httpx.AsyncBaseTransport | None,
    profile: ModelProfile = DEFAULT_MODEL_PROFILE,
) -> ModelCapabilities:
    """Discover only the exact pinned model and provider-declared capabilities."""

    try:
        async with httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            timeout=PROBE_TIMEOUT,
            trust_env=False,
        ) as client:
            if profile.backend == "huggingface":
                model_path = quote(profile.discovery_model_id or "", safe="/")
                payload = await _request_json(client, f"{profile.base_url}/models/{model_path}", secret, profile)
                return _hf_capabilities(payload, profile)

            advertised = await _request_json(client, f"{profile.base_url}/models", secret, profile)
            _openai_model_is_available(advertised, profile)
            try:
                metadata = await _request_json(client, _lmstudio_native_models_url(profile), secret, profile)
            except TargetError:
                if profile.require_tools:
                    raise
                return ModelCapabilities(
                    model_id=profile.model_id,
                    backend=profile.backend,
                    chat_completions=True,
                    tool_calls=None,
                    source="openai-compatible-model-list",
                )
            return _lmstudio_capabilities(metadata, profile)
    except httpx.HTTPError as error:
        raise _provider_error(profile) from error


async def probe_available(
    secret: str | None,
    transport: httpx.AsyncBaseTransport | None,
    profile: ModelProfile = DEFAULT_MODEL_PROFILE,
) -> None:
    """Backward-compatible readiness probe for the selected exact model."""

    await discover_model(secret, transport, profile)


async def send_chat(
    body: Mapping[str, Any],
    secret: str | None,
    transport: httpx.AsyncBaseTransport | None,
    profile: ModelProfile = DEFAULT_MODEL_PROFILE,
) -> UpstreamResponse | UpstreamStream:
    """Perform one pinned-upstream chat call, preserving the response form."""

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
            f"{profile.base_url}/chat/completions",
            headers=headers,
            content=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )
        response = await client.send(request, stream=True)
    except httpx.HTTPError as error:
        await client.aclose()
        raise _provider_error(profile) from error
    if not 200 <= response.status_code < 300:
        try:
            diagnostics = await _bounded_error_diagnostics(response)
        except httpx.HTTPError as error:
            raise _provider_error(profile) from error
        finally:
            await response.aclose()
            await client.aclose()
        raise _upstream_error(response.status_code, diagnostics, profile)
    content_type = response.headers.get(
        "content-type", "application/json" if not streaming else "text/event-stream"
    )
    if not streaming:
        try:
            content = await _bounded_chat_content(response, profile)
            _validate_chat_response(content, profile)
        except httpx.HTTPError as error:
            raise _provider_error(profile) from error
        finally:
            await response.aclose()
            await client.aclose()
        return UpstreamResponse(response.status_code, content, content_type)

    declared_length = _declared_error_body_length(response)
    if declared_length is not None and declared_length > MAX_CHAT_RESPONSE_BYTES:
        await response.aclose()
        await client.aclose()
        raise _provider_error(profile)
    if response.is_stream_consumed and len(response.content) > MAX_CHAT_RESPONSE_BYTES:
        await response.aclose()
        await client.aclose()
        raise _provider_error(profile)

    async def stream_body() -> AsyncIterator[bytes]:
        delivered = 0
        try:
            chunks: AsyncIterator[bytes]
            if response.is_stream_consumed:

                async def retained_content() -> AsyncIterator[bytes]:
                    yield response.content

                chunks = retained_content()
            else:
                chunks = response.aiter_raw(chunk_size=64 * 1024)
            async for chunk in chunks:
                delivered += len(chunk)
                if delivered > MAX_CHAT_RESPONSE_BYTES:
                    raise _provider_error(profile)
                yield chunk
        except httpx.HTTPError as error:
            raise _provider_error(profile) from error
        finally:
            await response.aclose()
            await client.aclose()

    return UpstreamStream(response.status_code, content_type, stream_body())


__all__ = [
    "MAX_CHAT_RESPONSE_BYTES",
    "ModelCapabilities",
    "UpstreamResponse",
    "UpstreamStream",
    "discover_model",
    "probe_available",
    "send_chat",
]
