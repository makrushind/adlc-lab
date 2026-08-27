"""The deliberately narrow public ASGI surface for the Hugging Face gateway."""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from aiweekend_target.errors import ErrorCode, TargetError, local_response_status
from aiweekend_target.lab.config import MODEL_PAIR, PROVIDER

from .policy import read_secret, validate_chat_body
from .transport import UpstreamResponse, UpstreamStream, probe_available, send_chat


DEFAULT_SECRET_PATH = "/run/secrets/hf_token"


def _error_response(error: TargetError) -> JSONResponse:
    return JSONResponse(error.as_result(), status_code=local_response_status(error.code))


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
    secret_path: str | Path = DEFAULT_SECRET_PATH,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    trace_sink: Callable[[dict[str, object]], None] | None = None,
) -> Starlette:
    """Create an app exposing only liveness, readiness, selected models, and chat completion."""

    def emit(event: dict[str, object]) -> None:
        document = {"schema": 1, **event}
        try:
            if trace_sink is not None:
                trace_sink(document)
            else:
                sys.stderr.write(json.dumps(document, separators=(",", ":")) + "\n")
        except Exception:
            pass

    def emit_error(error: TargetError) -> None:
        status = error.details.get("upstream_status") if isinstance(error.details, Mapping) else None
        event: dict[str, object] = {"type": "gateway_error", "model": MODEL_PAIR, "code": error.code.value}
        if isinstance(status, int):
            event["status"] = status
        if error.diagnostics is not None:
            event["upstream"] = error.diagnostics
        emit(event)

    async def live(_: Request) -> Response:
        return JSONResponse({"status": "live"})

    async def ready(_: Request) -> Response:
        try:
            await probe_available(read_secret(secret_path), transport)
            return JSONResponse({"status": "ready"})
        except TargetError as error:
            emit_error(error)
            return _error_response(error)

    async def models(_: Request) -> Response:
        try:
            await probe_available(read_secret(secret_path), transport)
            return JSONResponse({"object": "list", "data": [{"id": MODEL_PAIR, "object": "model", "owned_by": PROVIDER}]})
        except TargetError as error:
            emit_error(error)
            return _error_response(error)

    async def chat(request: Request) -> Response:
        try:
            secret = read_secret(secret_path)
            try:
                body = await request.json()
            except (UnicodeDecodeError, ValueError) as error:
                raise TargetError(ErrorCode.POLICY, "request body must be a JSON object") from error
            body = validate_chat_body(body, MODEL_PAIR)
            messages = body.get("messages")
            tools = body.get("tools")
            emit({
                "type": "llm_request",
                "model": MODEL_PAIR,
                "message_count": len(messages) if isinstance(messages, list) else 0,
                "tool_count": len(tools) if isinstance(tools, list) else 0,
                **_request_metadata(body),
            })
            upstream = await send_chat(body, secret, transport)
            if isinstance(upstream, UpstreamResponse):
                emit({"type": "llm_response", "model": MODEL_PAIR, "status": upstream.status_code, "response_count": 1})
                return Response(upstream.content, status_code=upstream.status_code, media_type=upstream.content_type)
            if isinstance(upstream, UpstreamStream):
                emit({"type": "llm_response", "model": MODEL_PAIR, "status": upstream.status_code, "response_count": 1})
                return StreamingResponse(upstream.body, status_code=upstream.status_code, media_type=upstream.content_type)
            raise TargetError(ErrorCode.PROVIDER, "Hugging Face Router returned an invalid response")
        except TargetError as error:
            emit_error(error)
            return _error_response(error)

    return Starlette(
        routes=[
            Route("/health/live", live, methods=["GET"]),
            Route("/health/ready", ready, methods=["GET"]),
            Route("/v1/models", models, methods=["GET"]),
            Route("/v1/chat/completions", chat, methods=["POST"]),
        ]
    )
