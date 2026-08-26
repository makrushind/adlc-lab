"""Strict, side-effect-free validation for the agent's fixed two-turn protocol."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import re

from aiweekend_target.repo_rag.types import SearchResponse, SearchResult


TOOL_NAME = "search_repo"
MAX_QUERY = 256
MAX_PATH_GLOB = 256
MAX_ARGUMENTS = 4_096
MAX_RESULTS = 20
MAX_PATH = 512
MAX_CONTENT = 8_192
MAX_LINE = 1_000_000
_CALL_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class ProtocolError(ValueError):
    """A peer supplied an invalid document for the fixed agent protocol."""


@dataclass(frozen=True)
class FirstToolTurn:
    assistant: dict[str, object]
    call_id: str
    arguments: dict[str, object]


def _json_pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise ValueError("non-standard JSON number")


def strict_json(document: str) -> object:
    """Decode JSON while rejecting duplicate keys and non-standard numbers."""
    return json.loads(document, object_pairs_hook=_json_pairs, parse_constant=_reject_constant)


def _text(value: object, maximum: int) -> str | None:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        return None
    return value


def parse_tool_arguments(value: object) -> dict[str, object]:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > MAX_ARGUMENTS:
        raise ProtocolError("tool arguments must be bounded JSON")
    try:
        arguments = strict_json(value)
    except (UnicodeError, ValueError) as error:
        raise ProtocolError("tool arguments must be strict JSON") from error
    if not isinstance(arguments, dict) or not set(arguments) <= {"query", "limit", "path_glob"}:
        raise ProtocolError("tool arguments have unexpected fields")
    query = _text(arguments.get("query"), MAX_QUERY)
    limit = arguments.get("limit", 5)
    path_glob = arguments.get("path_glob")
    if query is None or not query.strip() or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5:
        raise ProtocolError("tool arguments are invalid")
    if path_glob is not None and (_text(path_glob, MAX_PATH_GLOB) is None or ".." in path_glob or path_glob.startswith("/")):
        raise ProtocolError("tool path glob is invalid")
    checked: dict[str, object] = {"query": query.strip(), "limit": limit}
    if path_glob is not None:
        checked["path_glob"] = path_glob
    return checked


def parse_first_tool_turn(document: Mapping[str, object]) -> FirstToolTurn:
    choices = document.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ProtocolError("first LLM response has invalid choices")
    message = choices[0].get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ProtocolError("first LLM response has invalid assistant message")
    content = message.get("content")
    calls = message.get("tool_calls")
    if content not in (None, "") or not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        raise ProtocolError("first LLM response must contain one tool call")
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
        or function.get("name") != TOOL_NAME
    ):
        raise ProtocolError("first LLM tool call is invalid")
    arguments = parse_tool_arguments(function.get("arguments"))
    canonical_call = {"id": call_id, "type": "function", "function": {"name": TOOL_NAME, "arguments": function["arguments"]}}
    assistant = {"role": "assistant", "content": content, "tool_calls": [canonical_call]}
    return FirstToolTurn(assistant, call_id, arguments)


def parse_final_assistant(document: Mapping[str, object]) -> str:
    choices = document.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ProtocolError("final LLM response has invalid choices")
    message = choices[0].get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant" or message.get("tool_calls") not in (None, []):
        raise ProtocolError("final LLM response must not contain tool calls")
    content = _text(message.get("content"), MAX_CONTENT)
    if content is None or not content.strip():
        raise ProtocolError("final LLM response must contain text")
    return content.strip()


def validate_search_response(value: object) -> SearchResponse:
    if not isinstance(value, dict) or set(value) != {"results"} or not isinstance(value["results"], list) or len(value["results"]) > MAX_RESULTS:
        raise ProtocolError("MCP response has invalid results")
    results: list[SearchResult] = []
    for item in value["results"]:
        if not isinstance(item, dict) or set(item) != {"path", "line_start", "line_end", "content"}:
            raise ProtocolError("MCP result has invalid fields")
        path = _text(item.get("path"), MAX_PATH)
        content = _text(item.get("content"), MAX_CONTENT)
        start, end = item.get("line_start"), item.get("line_end")
        if (
            path is None or content is None or path.startswith("/") or ".." in path.split("/")
            or isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int)
            or not 1 <= start <= end <= MAX_LINE or end - start > MAX_LINE
        ):
            raise ProtocolError("MCP result is invalid")
        results.append({"path": path, "line_start": start, "line_end": end, "content": content})
    return {"results": results}
