"""The deliberately narrow public ASGI surface for the pinned model gateway."""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from aiweekend_target.agent_protocol import strict_json
from aiweekend_target.errors import ErrorCode, TargetError, local_response_status
from aiweekend_target.lab.config import (
    DEFAULT_HF_SECRET_PATH,
    DEFAULT_MODEL_PROFILE,
    ModelProfile,
    load_model_profile,
)

from .correlation import (
    MODEL_REQUEST_ID_HEADER,
    RUN_ID_HEADER,
    correlation_headers,
)
from .policy import read_secret, validate_chat_body
from .transport import ModelCapabilities, UpstreamResponse, UpstreamStream, discover_model, send_chat


DEFAULT_SECRET_PATH = str(DEFAULT_HF_SECRET_PATH)
MAX_CHAT_REQUEST_BYTES = 512 * 1024
MAX_CONTENT_LENGTH_DIGITS = 20


def _error_response(error: TargetError) -> JSONResponse:
    return JSONResponse(error.as_result(), status_code=local_response_status(error.code))


async def _bounded_request_json(request: Request) -> object:
    declared = request.headers.get("content-length")
    if declared is not None:
        if (
            not declared
            or len(declared) > MAX_CONTENT_LENGTH_DIGITS
            or not declared.isascii()
            or not declared.isdigit()
            or int(declared) > MAX_CHAT_REQUEST_BYTES
        ):
            raise TargetError(ErrorCode.POLICY, "request body exceeds the byte limit")
    content = bytearray()
    try:
        async for chunk in request.stream():
            if len(content) + len(chunk) > MAX_CHAT_REQUEST_BYTES:
                raise TargetError(
                    ErrorCode.POLICY, "request body exceeds the byte limit"
                )
            content.extend(chunk)
    except ClientDisconnect as error:
        raise TargetError(ErrorCode.POLICY, "request body is incomplete") from error
    try:
        return strict_json(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise TargetError(
            ErrorCode.POLICY, "request body must be a JSON object"
        ) from error


def _request_metadata(body: Mapping[str, object]) -> dict[str, object]:
    messages = body.get("messages")
    tools = body.get("tools")
    message_bytes = (
        [len(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) for message in messages]
        if isinstance(messages, list)
        else []
    )
    tool_schema_sha256 = (
        hashlib.sha256(
            json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if isinstance(tools, list)
        else None
    )
    tool_choice = body.get("tool_choice")
    if isinstance(tool_choice, Mapping) and tool_choice.get("type") == "function":
        tool_choice_kind = "named"
    elif isinstance(tool_choice, str) and tool_choice in {"auto", "none", "required"}:
        tool_choice_kind = tool_choice
    elif tool_choice is None:
        tool_choice_kind = "unset"
    else:
        tool_choice_kind = "other"
    output_token_cap = body.get("max_completion_tokens")
    if type(output_token_cap) is not int or output_token_cap < 0:
        output_token_cap = body.get("max_tokens")
    if type(output_token_cap) is not int or output_token_cap < 0:
        output_token_cap = None
    return {
        "message_bytes": message_bytes,
        "tool_choice_kind": tool_choice_kind,
        "tool_schema_sha256": tool_schema_sha256,
        "output_token_cap": output_token_cap,
    }


def create_app(
    secret_path: str | Path | None = None,
    *,
    profile: ModelProfile | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    trace_sink: Callable[[dict[str, object]], None] | None = None,
) -> Starlette:
    """Create an app exposing only liveness, readiness, selected models, and chat completion."""

    selected = load_model_profile() if profile is None else profile

    def credential() -> str | None:
        if selected.auth_mode == "none":
            return None
        path = Path(secret_path) if secret_path is not None else selected.secret_path
        if path is None:
            if selected.auth_mode == "optional":
                return None
            raise TargetError(ErrorCode.AUTH, f"{selected.label} credential is unavailable")
        if selected.auth_mode == "optional" and not path.is_file():
            return None
        return read_secret(path, f"{selected.label} credential")

    async def capabilities() -> ModelCapabilities:
        return await discover_model(credential(), transport, selected)

    def emit(event: dict[str, object]) -> None:
        document = {"schema": 1, **event}
        try:
            if trace_sink is not None:
                trace_sink(document)
            else:
                sys.stderr.write(json.dumps(document, separators=(",", ":")) + "\n")
        except Exception:
            pass

    def emit_error(
        error: TargetError, correlation: Mapping[str, object] | None = None
    ) -> None:
        status = error.details.get("upstream_status") if isinstance(error.details, Mapping) else None
        event: dict[str, object] = {
            "type": "gateway_error",
            "model": selected.model_id,
            "backend": selected.backend,
            "code": error.code.value,
        }
        if isinstance(status, int):
            event["status"] = status
        if error.diagnostics is not None:
            event["upstream"] = error.diagnostics
        if correlation is not None:
            event.update(correlation)
        emit(event)

    def request_correlation(
        request: Request,
    ) -> tuple[dict[str, object], dict[str, str]]:
        run_id = request.headers.get(RUN_ID_HEADER)
        model_request_id = request.headers.get(MODEL_REQUEST_ID_HEADER)
        if run_id is None and model_request_id is None:
            return {}, {}
        if run_id is None or model_request_id is None:
            raise TargetError(
                ErrorCode.POLICY, "model correlation headers must be paired"
            )
        try:
            headers = correlation_headers(run_id, model_request_id)
        except ValueError as error:
            raise TargetError(
                ErrorCode.POLICY, "model correlation headers are invalid"
            ) from error
        return {
            "run_id": run_id,
            "model_request_id": model_request_id,
        }, headers

    async def live(_: Request) -> Response:
        return JSONResponse({"status": "live"})

    async def ready(_: Request) -> Response:
        try:
            await capabilities()
            return JSONResponse({"status": "ready"})
        except TargetError as error:
            emit_error(error)
            return _error_response(error)

    async def models(_: Request) -> Response:
        try:
            available = await capabilities()
            return JSONResponse({
                "object": "list",
                "data": [{
                    "id": selected.model_id,
                    "object": "model",
                    "owned_by": selected.owner,
                    "backend": selected.backend,
                    "capabilities": available.as_dict(),
                }],
            })
        except TargetError as error:
            emit_error(error)
            return _error_response(error)

    async def chat(request: Request) -> Response:
        correlation: dict[str, object] = {}
        response_headers: dict[str, str] = {}
        try:
            correlation, response_headers = request_correlation(request)
            body = await _bounded_request_json(request)
            body = validate_chat_body(body, selected.model_id)
            secret = credential()
            messages = body.get("messages")
            tools = body.get("tools")
            emit({
                "type": "llm_request",
                "model": selected.model_id,
                "backend": selected.backend,
                "message_count": len(messages) if isinstance(messages, list) else 0,
                "tool_count": len(tools) if isinstance(tools, list) else 0,
                **correlation,
                **_request_metadata(body),
            })
            if selected == DEFAULT_MODEL_PROFILE:
                upstream = await send_chat(body, secret, transport)
            else:
                upstream = await send_chat(body, secret, transport, selected)
            if isinstance(upstream, UpstreamResponse):
                emit({
                    "type": "llm_response",
                    "model": selected.model_id,
                    "backend": selected.backend,
                    "status": upstream.status_code,
                    "response_count": 1,
                    **correlation,
                })
                return Response(
                    upstream.content,
                    status_code=upstream.status_code,
                    media_type=upstream.content_type,
                    headers=response_headers,
                )
            if isinstance(upstream, UpstreamStream):
                emit({
                    "type": "llm_response",
                    "model": selected.model_id,
                    "backend": selected.backend,
                    "status": upstream.status_code,
                    "response_count": 1,
                    **correlation,
                })
                return StreamingResponse(
                    upstream.body,
                    status_code=upstream.status_code,
                    media_type=upstream.content_type,
                    headers=response_headers,
                )
            raise TargetError(ErrorCode.PROVIDER, f"{selected.label} returned an invalid response")
        except TargetError as error:
            emit_error(error, correlation)
            response = _error_response(error)
            response.headers.update(response_headers)
            return response

    return Starlette(
        routes=[
            Route("/health/live", live, methods=["GET"]),
            Route("/health/ready", ready, methods=["GET"]),
            Route("/v1/models", models, methods=["GET"]),
            Route("/v1/chat/completions", chat, methods=["POST"]),
        ]
    )
