"""A fixed, transparent two-step Python agent for the attack lab."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, IO
from urllib.parse import urlsplit

import httpx
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from aiweekend_target.lab.config import GATEWAY_BASE_URL, MCP_URL, MODEL_PAIR
from aiweekend_target.lab.trace import CANARIES, TraceObserver, canaries_in, safe_preview


_SCENARIOS = frozenset({"baseline", "rag-poisoning", "mcp-poisoning", "llm-injection", "custom"})
_GATEWAY_CODES = {"AUTH": 401, "QUOTA": 402, "MODEL_UNAVAILABLE": 404, "PROVIDER": 400, "POLICY": 400}
_CALL_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MAX_TASK_BYTES = 65_536
_MAX_PROMPT_PREVIEW = 240
_MAX_QUERY = 256
_MAX_PATH_GLOB = 256
_MAX_ARGUMENTS = 4_096
_MAX_RESULTS = 20
_MAX_PATH = 512
_MAX_CONTENT = 8_192
_MAX_LINE = 1_000_000
_TOOL_NAME = "search_repo"
_TOOL_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": _TOOL_NAME,
        "description": "Search the active repository corpus.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": _MAX_QUERY},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5},
                "path_glob": {"type": ["string", "null"], "maxLength": _MAX_PATH_GLOB},
            },
            "required": ["query"],
        },
    },
}
_NAMED_TOOL_CHOICE = {"type": "function", "function": {"name": _TOOL_NAME}}
LLM_TIMEOUT = httpx.Timeout(connect=10.0, read=200.0, write=10.0, pool=10.0)


@dataclass(frozen=True)
class AgentPaths:
    task: Path = Path("/target/workspace/task.md")
    workspace: Path = Path("/target/workspace")
    scenario_marker: Path = Path("/target/rag-index/scenario.json")


class _AgentFailure(Exception):
    def __init__(self, code: str, stage: str) -> None:
        self.code = code if code in _GATEWAY_CODES or code == "MCP" else "POLICY"
        self.stage = stage
        super().__init__(self.code)


def _json_pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise ValueError("non-standard JSON number")


def _strict_json(document: str) -> object:
    return json.loads(document, object_pairs_hook=_json_pairs, parse_constant=_reject_constant)


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
        value = _strict_json(path.read_text(encoding="utf-8"))
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


def _text(value: object, maximum: int) -> str | None:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        return None
    return value


def _gateway_code(response: object) -> str | None:
    status = getattr(response, "status_code", None)
    text = getattr(response, "text", "")
    if not isinstance(status, int) or not isinstance(text, str) or len(text.encode("utf-8")) > _MAX_CONTENT:
        return None
    try:
        value = _strict_json(text)
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
    code = error["code"]
    return code if _GATEWAY_CODES.get(code) == status else None


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
        value = _strict_json(text)
    except (UnicodeError, ValueError) as error:
        raise _AgentFailure("POLICY", stage) from error
    if not isinstance(value, dict):
        raise _AgentFailure("POLICY", stage)
    return value


def _status_code(status: int) -> str:
    return {401: "AUTH", 402: "QUOTA", 404: "MODEL_UNAVAILABLE"}.get(status, "PROVIDER")


def _tool_arguments(value: object) -> dict[str, object]:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > _MAX_ARGUMENTS:
        raise _AgentFailure("POLICY", "llm")
    try:
        arguments = _strict_json(value)
    except (UnicodeError, ValueError) as error:
        raise _AgentFailure("POLICY", "llm") from error
    if not isinstance(arguments, dict) or not set(arguments) <= {"query", "limit", "path_glob"}:
        raise _AgentFailure("POLICY", "llm")
    query = _text(arguments.get("query"), _MAX_QUERY)
    limit = arguments.get("limit", 5)
    path_glob = arguments.get("path_glob")
    if query is None or not query.strip() or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5:
        raise _AgentFailure("POLICY", "llm")
    if path_glob is not None and (_text(path_glob, _MAX_PATH_GLOB) is None or ".." in path_glob or path_glob.startswith("/")):
        raise _AgentFailure("POLICY", "llm")
    result: dict[str, object] = {"query": query.strip(), "limit": limit}
    if path_glob is not None:
        result["path_glob"] = path_glob
    return result


def _first_message(document: dict[str, object]) -> tuple[dict[str, object], dict[str, object], str, dict[str, object]]:
    choices = document.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise _AgentFailure("POLICY", "llm")
    message = choices[0].get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise _AgentFailure("POLICY", "llm")
    content = message.get("content")
    calls = message.get("tool_calls")
    if (content not in (None, "")) or not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        raise _AgentFailure("POLICY", "llm")
    call = calls[0]
    function = call.get("function")
    call_id = call.get("id")
    if (
        set(call) != {"id", "type", "function"}
        or call.get("type") != "function"
        or not isinstance(call_id, str)
        or not _CALL_ID.fullmatch(call_id)
        or not isinstance(function, dict)
        or set(function) != {"name", "arguments"}
        or function.get("name") != _TOOL_NAME
    ):
        raise _AgentFailure("POLICY", "llm")
    arguments = _tool_arguments(function.get("arguments"))
    canonical_call = {"id": call_id, "type": "function", "function": {"name": _TOOL_NAME, "arguments": function["arguments"]}}
    assistant = {"role": "assistant", "content": content, "tool_calls": [canonical_call]}
    return assistant, canonical_call, call_id, arguments


def _final_text(document: dict[str, object]) -> str:
    choices = document.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise _AgentFailure("POLICY", "llm")
    message = choices[0].get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant" or message.get("tool_calls") not in (None, []):
        raise _AgentFailure("POLICY", "llm")
    content = _text(message.get("content"), _MAX_CONTENT)
    if content is None or not content.strip():
        raise _AgentFailure("POLICY", "llm")
    return content.strip()


def _validate_search(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"results"} or not isinstance(value["results"], list) or len(value["results"]) > _MAX_RESULTS:
        raise _AgentFailure("MCP", "mcp")
    results: list[dict[str, object]] = []
    for item in value["results"]:
        if not isinstance(item, dict) or set(item) != {"path", "line_start", "line_end", "content"}:
            raise _AgentFailure("MCP", "mcp")
        path = _text(item.get("path"), _MAX_PATH)
        content = _text(item.get("content"), _MAX_CONTENT)
        start, end = item.get("line_start"), item.get("line_end")
        if (
            path is None
            or content is None
            or path.startswith("/")
            or ".." in path.split("/")
            or isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or not 1 <= start <= end <= _MAX_LINE
            or end - start > _MAX_LINE
        ):
            raise _AgentFailure("MCP", "mcp")
        results.append({"path": path, "line_start": start, "line_end": end, "content": content})
    return {"results": results}


async def _mcp_search(url: str, arguments: dict[str, object]) -> dict[str, object]:
    try:
        async with httpx2.AsyncClient(follow_redirects=False, timeout=10.0, trust_env=False) as client:
            async with streamable_http_client(url, http_client=client) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    if (
                        getattr(listing, "result_type", None) != "complete"
                        or getattr(listing, "next_cursor", None) is not None
                        or [getattr(tool, "name", None) for tool in getattr(listing, "tools", [])] != [_TOOL_NAME]
                    ):
                        raise _AgentFailure("MCP", "mcp")
                    result = await session.call_tool(_TOOL_NAME, arguments)
                    if getattr(result, "result_type", None) != "complete" or getattr(result, "is_error", True):
                        raise _AgentFailure("MCP", "mcp")
                    return _validate_search(getattr(result, "structured_content", None))
    except _AgentFailure:
        raise
    except Exception as error:
        raise _AgentFailure("MCP", "mcp") from error


async def _run(paths: AgentPaths, output: IO[str]) -> int:
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
        _write_json(output, observer.emit("llm_request", turn=1, model=MODEL_PAIR, tool=_TOOL_NAME, status="sent"))
        try:
            async with httpx.AsyncClient(follow_redirects=False, timeout=LLM_TIMEOUT, trust_env=False) as client:
                first_response = await client.post(llm_url, json=first_body)
                first = _response_document(first_response, "llm")
                assistant, call, call_id, arguments = _first_message(first)
                _write_json(output, observer.emit("tool_call", turn=1, model=MODEL_PAIR, tool=_TOOL_NAME, query_preview=safe_preview(str(arguments["query"]), 160), status="accepted"))
                _write_json(output, observer.emit("rag", stage="rag", query_preview=safe_preview(str(arguments["query"]), 160), status="prepared", canaries=canaries_in(arguments, allowed=CANARIES[:2])))
                _write_json(output, observer.emit("mcp_request", tool=_TOOL_NAME, query_preview=safe_preview(str(arguments["query"]), 160), status="sent"))
                search = await _mcp_search(mcp_url, arguments)
                paths_preview = [safe_preview(str(item["path"]), 128) for item in search["results"][:10] if isinstance(item, dict)]
                _write_json(output, observer.emit("mcp_result", stage="mcp", tool=_TOOL_NAME, result_count=len(search["results"]), paths=paths_preview, status="completed", canaries=canaries_in(search, allowed=CANARIES[:2])))
                second_body = {
                    "model": MODEL_PAIR,
                    "messages": [
                        {"role": "user", "content": prompt},
                        assistant,
                        {"role": "tool", "tool_call_id": call_id, "content": json.dumps(search, ensure_ascii=False, separators=(",", ":"))},
                    ],
                    "tools": [_TOOL_SCHEMA],
                    "tool_choice": "none",
                    "stream": False,
                }
                _write_json(output, observer.emit("llm_request", turn=2, model=MODEL_PAIR, tool=_TOOL_NAME, status="sent"))
                second_response = await client.post(llm_url, json=second_body)
                answer = _final_text(_response_document(second_response, "llm"))
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
