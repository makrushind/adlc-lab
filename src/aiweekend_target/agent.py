"""A fixed, transparent two-step Python agent for the attack lab."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
from typing import Any, Awaitable, Callable, IO
from urllib.parse import urlsplit

import httpx
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from aiweekend_target.agent_protocol import (
    ProtocolError,
    TOOL_NAME,
    parse_final_assistant,
    parse_first_tool_turn,
    strict_json,
    validate_search_response,
)
from aiweekend_target.errors import ErrorCode, classify_upstream_status, local_response_status
from aiweekend_target.lab.config import GATEWAY_BASE_URL, MCP_URL, MODEL_PAIR
from aiweekend_target.lab.trace import CANARIES, TraceObserver, canaries_in, safe_preview
from aiweekend_target.repo_rag.types import SearchResponse


_SCENARIOS = frozenset({"baseline", "rag-poisoning", "mcp-poisoning", "llm-injection", "custom"})
_AGENT_ERROR_CODES = frozenset(
    {
        ErrorCode.AUTH.value,
        ErrorCode.QUOTA.value,
        ErrorCode.MODEL_UNAVAILABLE.value,
        ErrorCode.PROVIDER.value,
        ErrorCode.MCP.value,
        ErrorCode.POLICY.value,
    }
)
_MAX_TASK_BYTES = 65_536
_MAX_CONTENT = 8_192
_TOOL_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "Search the active repository corpus.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 256},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5},
                "path_glob": {"type": ["string", "null"], "maxLength": 256},
            },
            "required": ["query"],
        },
    },
}
_NAMED_TOOL_CHOICE = {"type": "function", "function": {"name": TOOL_NAME}}
LLM_TIMEOUT = httpx.Timeout(connect=10.0, read=200.0, write=10.0, pool=10.0)


@dataclass(frozen=True)
class AgentPaths:
    task: Path = Path("/target/workspace/task.md")
    workspace: Path = Path("/target/workspace")
    scenario_marker: Path = Path("/target/rag-index/scenario.json")


class _AgentFailure(Exception):
    def __init__(self, code: str, stage: str) -> None:
        self.code = code if code in _AGENT_ERROR_CODES else "POLICY"
        self.stage = stage
        super().__init__(self.code)


def _write_json(output: IO[str], value: Mapping[str, object]) -> None:
    output.write(json.dumps(dict(value), ensure_ascii=False, separators=(",", ":")) + "\n")
    output.flush()


def _read_task(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise _AgentFailure("POLICY", "prompt") from error
    if not value or len(value.encode("utf-8")) > _MAX_TASK_BYTES:
        raise _AgentFailure("POLICY", "prompt")
    return value


def _scenario_id(path: Path) -> str:
    try:
        value = strict_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise _AgentFailure("POLICY", "prompt") from error
    if not isinstance(value, dict) or value.get("id") not in _SCENARIOS:
        raise _AgentFailure("POLICY", "prompt")
    return str(value["id"])


def _endpoint(name: str, default: str) -> str:
    value = os.environ.get(name, default)
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise _AgentFailure("POLICY", "prompt") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise _AgentFailure("POLICY", "prompt")
    return value


def _gateway_code(response: object) -> str | None:
    status = getattr(response, "status_code", None)
    text = getattr(response, "text", "")
    if not isinstance(status, int) or not isinstance(text, str) or len(text.encode("utf-8")) > _MAX_CONTENT:
        return None
    try:
        value = strict_json(text)
    except (UnicodeError, ValueError):
        return None
    if not isinstance(value, dict) or set(value) != {"ok", "error", "exit_code"}:
        return None
    error = value.get("error")
    if (
        value.get("ok") is not False
        or value.get("exit_code") != 1
        or not isinstance(error, dict)
        or set(error) != {"code", "message", "details"}
        or not isinstance(error.get("code"), str)
        or not isinstance(error.get("message"), str)
        or (error.get("details") is not None and not isinstance(error.get("details"), dict))
    ):
        return None
    try:
        code = ErrorCode(error["code"])
    except ValueError:
        return None
    return code.value if local_response_status(code) == status else None


def _response_document(response: object, stage: str) -> dict[str, object]:
    status = getattr(response, "status_code", None)
    if not isinstance(status, int):
        raise _AgentFailure("PROVIDER", stage)
    if status < 200 or status >= 300:
        raise _AgentFailure(_gateway_code(response) or _status_code(status), stage)
    text = getattr(response, "text", "")
    if not isinstance(text, str) or len(text.encode("utf-8")) > _MAX_CONTENT:
        raise _AgentFailure("PROVIDER", stage)
    try:
        value = strict_json(text)
    except (UnicodeError, ValueError) as error:
        raise _AgentFailure("POLICY", stage) from error
    if not isinstance(value, dict):
        raise _AgentFailure("POLICY", stage)
    return value


def _status_code(status: int) -> str:
    return classify_upstream_status(status).value


async def _mcp_search(url: str, arguments: dict[str, object]) -> SearchResponse:
    try:
        async with httpx2.AsyncClient(follow_redirects=False, timeout=10.0, trust_env=False) as client:
            async with streamable_http_client(url, http_client=client) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    if (
                        getattr(listing, "result_type", None) != "complete"
                        or getattr(listing, "next_cursor", None) is not None
                        or [getattr(tool, "name", None) for tool in getattr(listing, "tools", [])] != [TOOL_NAME]
                    ):
                        raise _AgentFailure("MCP", "mcp")
                    result = await session.call_tool(TOOL_NAME, arguments)
                    if getattr(result, "result_type", None) != "complete" or getattr(result, "is_error", True):
                        raise _AgentFailure("MCP", "mcp")
                    try:
                        return validate_search_response(getattr(result, "structured_content", None))
                    except ProtocolError as error:
                        raise _AgentFailure("MCP", "mcp") from error
    except _AgentFailure:
        raise
    except Exception as error:
        raise _AgentFailure("MCP", "mcp") from error


LLMPost = Callable[[str, dict[str, object]], Awaitable[object]]
MCPSearch = Callable[[str, dict[str, object]], Awaitable[SearchResponse]]


async def _run(
    paths: AgentPaths,
    output: IO[str],
    *,
    post_llm: LLMPost | None = None,
    mcp_search: MCPSearch = _mcp_search,
) -> int:
    scenario = "unknown"
    observer: TraceObserver | None = None
    try:
        task = _read_task(paths.task)
        scenario = _scenario_id(paths.scenario_marker)
        observer = TraceObserver(scenario)
        llm_url = _endpoint("ADLC_LLM_URL", f"{GATEWAY_BASE_URL}/chat/completions")
        mcp_url = _endpoint("ADLC_MCP_URL", MCP_URL)
        prompt = f"Scenario: {scenario}\nTask: {task}"
        _write_json(output, observer.emit("prompt", stage="prompt", prompt_chars=len(prompt), status="prepared", canaries=canaries_in(prompt, allowed=CANARIES[2:])))
        first_body = {
            "model": MODEL_PAIR,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [_TOOL_SCHEMA],
            "tool_choice": _NAMED_TOOL_CHOICE,
            "stream": False,
        }
        _write_json(output, observer.emit("llm_request", turn=1, model=MODEL_PAIR, tool=TOOL_NAME, status="sent"))
        try:
            async with httpx.AsyncClient(follow_redirects=False, timeout=LLM_TIMEOUT, trust_env=False) as client:
                if post_llm is None:
                    async def send_llm(url: str, body: dict[str, object]) -> object:
                        return await client.post(url, json=body)
                else:
                    send_llm = post_llm
                first_response = await send_llm(llm_url, first_body)
                first = _response_document(first_response, "llm")
                try:
                    first_turn = parse_first_tool_turn(first)
                except ProtocolError as error:
                    raise _AgentFailure("POLICY", "llm") from error
                _write_json(output, observer.emit("tool_call", turn=1, model=MODEL_PAIR, tool=TOOL_NAME, query_preview=safe_preview(str(first_turn.arguments["query"]), 160), status="accepted"))
                _write_json(output, observer.emit("rag", stage="rag", query_preview=safe_preview(str(first_turn.arguments["query"]), 160), status="prepared", canaries=canaries_in(first_turn.arguments, allowed=CANARIES[:2])))
                _write_json(output, observer.emit("mcp_request", tool=TOOL_NAME, query_preview=safe_preview(str(first_turn.arguments["query"]), 160), status="sent"))
                search = await mcp_search(mcp_url, first_turn.arguments)
                try:
                    search = validate_search_response(search)
                except ProtocolError as error:
                    raise _AgentFailure("MCP", "mcp") from error
                paths_preview = [safe_preview(str(item["path"]), 128) for item in search["results"][:10] if isinstance(item, dict)]
                _write_json(output, observer.emit("mcp_result", stage="mcp", tool=TOOL_NAME, result_count=len(search["results"]), paths=paths_preview, status="completed", canaries=canaries_in(search, allowed=CANARIES[:2])))
                second_body = {
                    "model": MODEL_PAIR,
                    "messages": [
                        {"role": "user", "content": prompt},
                        first_turn.assistant,
                        {"role": "tool", "tool_call_id": first_turn.call_id, "content": json.dumps(search, ensure_ascii=False, separators=(",", ":"))},
                    ],
                    "tools": [_TOOL_SCHEMA],
                    "tool_choice": "none",
                    "stream": False,
                }
                _write_json(output, observer.emit("llm_request", turn=2, model=MODEL_PAIR, tool=TOOL_NAME, status="sent"))
                second_response = await send_llm(llm_url, second_body)
                try:
                    answer = parse_final_assistant(_response_document(second_response, "llm"))
                except ProtocolError as error:
                    raise _AgentFailure("POLICY", "llm") from error
        except _AgentFailure:
            raise
        except httpx.HTTPError as error:
            raise _AgentFailure("PROVIDER", "llm") from error
        _write_json(output, observer.emit("llm_response", stage="llm", turn=2, model=MODEL_PAIR, status="completed"))
        _write_json(output, observer.emit("agent", stage="agent", status="completed", text_preview=safe_preview(answer, 512)))
        _write_json(output, observer.result(True))
        return 0
    except _AgentFailure as error:
        if observer is None:
            observer = TraceObserver(scenario)
        _write_json(output, observer.emit("agent_error", stage=None, status="failed", code=error.code))
        _write_json(output, observer.result(False))
        return 1
    except Exception:
        if observer is None:
            observer = TraceObserver(scenario)
        _write_json(output, observer.emit("agent_error", stage=None, status="failed", code="PROVIDER"))
        _write_json(output, observer.result(False))
        return 1


def run_agent(*, paths: AgentPaths = AgentPaths(), output: IO[str] = sys.stdout, **_: Any) -> int:
    """Run exactly one provider/tool/provider sequence with no retry or fallback."""
    return asyncio.run(_run(paths, output))


__all__ = ["AgentPaths", "run_agent"]
