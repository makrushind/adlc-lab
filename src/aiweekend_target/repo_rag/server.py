"""Scenario-aware Streamable HTTP MCP adapter for repository search."""

from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
from typing import Protocol, cast

import anyio
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server import MCPServer
from starlette.applications import Starlette

from aiweekend_target.errors import ErrorCode, TargetError
from aiweekend_target.lab.scenarios import load_scenario
from aiweekend_target.lab.trace import CANARIES, safe_preview
from aiweekend_target.repo_rag.lint import LintResponse, lint_pr, validate_lint_response
from aiweekend_target.repo_rag.search import DATABASE_ENV, RepoSearch
from aiweekend_target.repo_rag.types import SearchResponse, SearchResult


DEFAULT_MARKER_PATH = Path("/target/rag-index/scenario.json")
DEFAULT_SCENARIOS_ROOT = Path("/opt/adlc/scenarios")
DEFAULT_CORPUS_ROOT = Path("/target/corpus")
DEFAULT_LOOPBACK_URL = "http://127.0.0.1:8000/mcp"
REVIEW_MODE_ENV = "ADLC_PR_REVIEW_MODE"


class HealthSession(Protocol):
    async def initialize(self) -> object: ...

    async def list_tools(self) -> object: ...

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object: ...


def _mcp_error(message: str) -> TargetError:
    return TargetError(ErrorCode.MCP, message)


def _review_mode(explicit: bool | None = None) -> bool:
    value = os.environ.get(REVIEW_MODE_ENV)
    if value is not None and value not in {"0", "1"}:
        raise TargetError(ErrorCode.CONFIG, "pull-request review mode is invalid")
    if explicit is not None and type(explicit) is not bool:
        raise TargetError(ErrorCode.CONFIG, "pull-request review mode is invalid")
    return explicit is True or value == "1"


def _default_trace(document: dict[str, object]) -> None:
    print(json.dumps(document, ensure_ascii=False, separators=(",", ":")), flush=True)


def _validate_response(value: object, *, require_mcp_canary: bool = False) -> SearchResponse:
    if not isinstance(value, dict) or set(value) != {"results"} or not isinstance(value["results"], list):
        raise _mcp_error("repository search returned an invalid result")
    checked: list[SearchResult] = []
    for item in value["results"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "line_start", "line_end", "content"}
            or not isinstance(item["path"], str)
            or isinstance(item["line_start"], bool)
            or not isinstance(item["line_start"], int)
            or isinstance(item["line_end"], bool)
            or not isinstance(item["line_end"], int)
            or not isinstance(item["content"], str)
            or item["line_start"] < 1
            or item["line_end"] < item["line_start"]
        ):
            raise _mcp_error("repository search returned an invalid result")
        checked.append(cast(SearchResult, dict(item)))
    if require_mcp_canary:
        serialized = json.dumps({"results": checked}, ensure_ascii=False, separators=(",", ":"))
        if serialized.count("ADLC_CANARY_MCP_4DB2E8") != 1:
            raise _mcp_error("MCP poisoning fixture is invalid")
    return {"results": checked}


class ScenarioRepoSearch:
    """Select real SQLite search or the one strict MCP-poisoning fixture per call."""

    def __init__(
        self,
        database_path: str | Path,
        scenario_marker_path: str | Path,
        scenarios_root: str | Path,
        *,
        trace_sink: Callable[[dict[str, object]], None] = _default_trace,
    ) -> None:
        self.repository = RepoSearch(database_path)
        self.marker_path = Path(scenario_marker_path)
        self.scenarios_root = Path(scenarios_root)
        self.trace_sink = trace_sink

    def _scenario_id(self) -> str:
        try:
            marker = json.loads(self.marker_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise _mcp_error("active scenario marker is unavailable") from error
        if not isinstance(marker, dict) or not isinstance(marker.get("id"), str):
            raise _mcp_error("active scenario marker is invalid")
        scenario_id = marker["id"]
        try:
            load_scenario(self.scenarios_root.parent, scenario_id)
        except TargetError as error:
            raise _mcp_error("active scenario marker is invalid") from error
        return scenario_id

    def _fixture(self) -> SearchResponse:
        path = self.scenarios_root / "mcp-poisoning" / "mcp-result.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise _mcp_error("MCP poisoning fixture is unavailable") from error
        return _validate_response(value, require_mcp_canary=True)

    def _emit(self, event_type: str, **facts: object) -> None:
        document = {"schema": 1, "type": event_type, **facts}
        try:
            self.trace_sink(document)
        except Exception:
            pass

    def search_repo(self, query: str, limit: int = 5, path_glob: str | None = None) -> SearchResponse:
        scenario_id = self._scenario_id()
        self._emit("mcp_request", query_preview=safe_preview(query.strip(), 80) if isinstance(query, str) else "")
        if scenario_id == "mcp-poisoning":
            response = self._fixture()
        else:
            response = _validate_response(self.repository.search_repo(query, limit, path_glob))
        paths = [safe_preview(item["path"], 128) for item in response["results"][:20]]
        canaries = [canary for canary in CANARIES if any(canary in item["content"] for item in response["results"])]
        self._emit("retrieval", result_count=len(response["results"]), paths=paths)
        self._emit("mcp_result", result_count=len(response["results"]), paths=paths, canaries=canaries)
        return response


def create_server(
    database_path: str | Path | None = None,
    scenario_marker_path: str | Path = DEFAULT_MARKER_PATH,
    scenarios_root: str | Path = DEFAULT_SCENARIOS_ROOT,
    *,
    trace_sink: Callable[[dict[str, object]], None] = _default_trace,
    review_mode: bool | None = None,
    corpus_root: str | Path = DEFAULT_CORPUS_ROOT,
) -> MCPServer:
    """Create the safe search server, optionally with pull-request linting."""
    configured_path = os.fspath(database_path) if database_path is not None else os.environ.get(DATABASE_ENV)
    if not configured_path:
        raise _mcp_error("repository index path is not configured")
    repository = ScenarioRepoSearch(configured_path, scenario_marker_path, scenarios_root, trace_sink=trace_sink)
    server = MCPServer("repo-rag")

    @server.tool(name="search_repo", structured_output=True)
    def search_repo_tool(query: str, limit: int = 5, path_glob: str | None = None) -> SearchResponse:
        return repository.search_repo(query, limit, path_glob)

    if _review_mode(review_mode):

        @server.tool(name="lint_pr", structured_output=True)
        def lint_pr_tool(targets: list[dict[str, object]]) -> LintResponse:
            return validate_lint_response(lint_pr(corpus_root, targets))

    return server


async def health_check(
    database_path: str | Path,
    scenario_marker_path: str | Path = DEFAULT_MARKER_PATH,
    scenarios_root: str | Path = DEFAULT_SCENARIOS_ROOT,
) -> dict[str, str]:
    """Initialize the MCP surface, list its allowed tools, and call safe search once."""
    server = create_server(database_path, scenario_marker_path, scenarios_root, trace_sink=lambda _: None)

    class LocalSession:
        async def initialize(self) -> None:
            return None

        async def list_tools(self) -> object:
            class Listing:
                result_type = "complete"
                next_cursor = None
                tools: list[object] = []

            listing = Listing()
            listing.tools = await server.list_tools()
            return listing

        async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
            return await server.call_tool(name, arguments)

    return await health_session(LocalSession())


async def health_session(session: HealthSession) -> dict[str, str]:
    """Perform the exact scenario-neutral initialize/list/call MCP health handshake."""
    try:
        await session.initialize()
        listing = await session.list_tools()
        if (
            getattr(listing, "result_type", None) != "complete"
            or getattr(listing, "next_cursor", None) is not None
            or [tool.name for tool in getattr(listing, "tools", [])]
            != (["search_repo", "lint_pr"] if _review_mode() else ["search_repo"])
        ):
            raise _mcp_error("repo-rag health contract failed")
        result = await session.call_tool("search_repo", {"query": "health", "limit": 1})
        if (
            getattr(result, "result_type", None) != "complete"
            or getattr(result, "is_error", True)
        ):
            raise _mcp_error("repo-rag health contract failed")
        _validate_response(getattr(result, "structured_content", None))
    except TargetError:
        raise
    except Exception as error:
        raise _mcp_error("repo-rag health contract failed") from error
    return {"status": "ready"}


async def health_http(url: str = DEFAULT_LOOPBACK_URL) -> dict[str, str]:
    """Probe the live loopback MCP service without proxy or redirect behavior."""
    async with httpx2.AsyncClient(follow_redirects=False, timeout=3.0, trust_env=False) as client:
        async with streamable_http_client(url, http_client=client) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                return await health_session(session)


def create_http_app(
    database_path: str | Path | None = None,
    scenario_marker_path: str | Path = DEFAULT_MARKER_PATH,
    scenarios_root: str | Path = DEFAULT_SCENARIOS_ROOT,
) -> Starlette:
    """Build the fixed JSON Streamable HTTP application mounted at ``/mcp``."""
    return create_server(database_path, scenario_marker_path, scenarios_root).streamable_http_app(
        streamable_http_path="/mcp", json_response=True, host="0.0.0.0"
    )


def serve(
    database_path: str | Path | None = None,
    scenario_marker_path: str | Path = DEFAULT_MARKER_PATH,
    scenarios_root: str | Path = DEFAULT_SCENARIOS_ROOT,
) -> None:
    """Serve repo-rag at the fixed internal Streamable HTTP endpoint."""
    server = create_server(database_path, scenario_marker_path, scenarios_root)

    async def run() -> None:
        await server.run_streamable_http_async(
            host="0.0.0.0", port=8000, streamable_http_path="/mcp", json_response=True
        )

    anyio.run(run)


__all__ = [
    "ScenarioRepoSearch",
    "HealthSession",
    "SearchResponse",
    "create_http_app",
    "create_server",
    "health_check",
    "health_http",
    "health_session",
    "serve",
]
