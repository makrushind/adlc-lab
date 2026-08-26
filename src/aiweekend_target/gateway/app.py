"""The deliberately narrow public ASGI surface for the Hugging Face gateway."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable, Mapping
import json
import sys

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from aiweekend_target.errors import ErrorCode, TargetError
from aiweekend_target.lab.config import MODEL_PAIR, PROVIDER

from .policy import read_secret, validate_chat_body
from .transport import UpstreamResponse, UpstreamStream, probe_available, send_chat


DEFAULT_SECRET_PATH = "/run/secrets/hf_token"


def _error_response(error: TargetError) -> JSONResponse:
    statuses = {
        ErrorCode.AUTH: 401,
        ErrorCode.QUOTA: 402,
        ErrorCode.MODEL_UNAVAILABLE: 404,
        ErrorCode.POLICY: 400,
        ErrorCode.CONFIG: 400,
        ErrorCode.PROVIDER: 400,
    }
    return JSONResponse(error.as_result(), status_code=statuses.get(error.code, 500))


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
