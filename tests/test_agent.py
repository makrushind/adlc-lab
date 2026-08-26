import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path

from aiweekend_target.agent import AgentPaths, _run
from aiweekend_target.agent_protocol import ProtocolError, parse_first_tool_turn, strict_json, validate_search_response


class _Response:
    def __init__(self, body: dict[str, object], status_code: int = 200) -> None:
        self.text = json.dumps(body)
        self.status_code = status_code


class AgentLoopTests(unittest.TestCase):
    def _exercise_failure(
        self,
        *,
        first_response: _Response,
        second_response: _Response | None = None,
        mcp_response: dict[str, object] | None = None,
    ) -> tuple[int, list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
        async def exercise() -> tuple[int, list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                task = root / "task.md"
                marker = root / "scenario.json"
                task.write_text("Summarize the repository.", encoding="utf-8")
                marker.write_text('{"id":"baseline"}', encoding="utf-8")
                llm_requests: list[dict[str, object]] = []
                mcp_requests: list[dict[str, object]] = []

                async def post_llm(_: str, body: dict[str, object]) -> _Response:
                    llm_requests.append(body)
                    return first_response if len(llm_requests) == 1 else second_response or first_response

                async def mcp_search(_: str, arguments: dict[str, object]) -> dict[str, object]:
                    mcp_requests.append(arguments)
                    return mcp_response or {"results": [{"path": "docs/handbook.md", "line_start": 1, "line_end": 1, "content": "hello"}]}

                output = io.StringIO()
                status = await _run(AgentPaths(task=task, scenario_marker=marker), output, post_llm=post_llm, mcp_search=mcp_search)
                return status, llm_requests, mcp_requests, [json.loads(line) for line in output.getvalue().splitlines()]

        return asyncio.run(exercise())

    def test_protocol_rejects_malformed_llm_and_mcp_documents(self) -> None:
        with self.assertRaises(ValueError):
            strict_json('{"choices":[],"choices":[]}')
        with self.assertRaises(ProtocolError):
            parse_first_tool_turn({"choices": [{"message": {"role": "assistant", "content": "not a tool call"}}]})
        with self.assertRaises(ProtocolError):
            validate_search_response({"results": [{"path": "/etc/passwd", "line_start": 1, "line_end": 1, "content": "no"}]})

    def test_first_llm_failure_stops_without_retry_or_mcp_fallback(self) -> None:
        status, llm_requests, mcp_requests, events = self._exercise_failure(
            first_response=_Response({}, status_code=500)
        )
        self.assertEqual(status, 1)
        self.assertEqual(len(llm_requests), 1)
        self.assertEqual(len(mcp_requests), 0)
        self.assertEqual(events[-2]["code"], "PROVIDER")
        self.assertEqual(events[-1], {"schema": 1, "type": "lab_result", "ok": False, "scenario": "baseline", "stages": ["prompt"], "canaries": []})

    def test_second_llm_failure_stops_without_retry_or_fallback(self) -> None:
        first = _Response({"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "search_repo", "arguments": '{"query":"handbook"}'}}]}}]})
        status, llm_requests, mcp_requests, events = self._exercise_failure(
            first_response=first,
            second_response=_Response({}, status_code=500),
        )
        self.assertEqual(status, 1)
        self.assertEqual(len(llm_requests), 2)
        self.assertEqual(len(mcp_requests), 1)
        self.assertEqual(events[-2]["code"], "PROVIDER")
        self.assertFalse(events[-1]["ok"])

    def test_malformed_mcp_response_stops_before_second_llm(self) -> None:
        first = _Response({"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "search_repo", "arguments": '{"query":"handbook"}'}}]}}]})
        status, llm_requests, mcp_requests, events = self._exercise_failure(
            first_response=first,
            mcp_response={"results": [{"path": "/etc/passwd", "line_start": 1, "line_end": 1, "content": "no"}]},
        )
        self.assertEqual(status, 1)
        self.assertEqual(len(llm_requests), 1)
        self.assertEqual(len(mcp_requests), 1)
        self.assertEqual(events[-2]["code"], "MCP")
        self.assertFalse(events[-1]["ok"])

    def test_malformed_final_assistant_stops_without_retry_or_fallback(self) -> None:
        first = _Response({"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "search_repo", "arguments": '{"query":"handbook"}'}}]}}]})
        malformed_final = _Response({"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": []}}]})
        status, llm_requests, mcp_requests, events = self._exercise_failure(first_response=first, second_response=malformed_final)
        self.assertEqual(status, 1)
        self.assertEqual(len(llm_requests), 2)
        self.assertEqual(len(mcp_requests), 1)
        self.assertEqual(events[-2]["code"], "POLICY")
        self.assertFalse(events[-1]["ok"])

    def test_agent_uses_exactly_two_llm_turns_and_one_mcp_call(self) -> None:
        async def exercise() -> tuple[int, list[dict[str, object]], list[dict[str, object]], list[str]]:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                task = root / "task.md"
                marker = root / "scenario.json"
                task.write_text("Summarize the repository.", encoding="utf-8")
                marker.write_text('{"id":"baseline"}', encoding="utf-8")
                llm_requests: list[dict[str, object]] = []
                mcp_requests: list[dict[str, object]] = []

                async def post_llm(_: str, body: dict[str, object]) -> _Response:
                    llm_requests.append(body)
                    if len(llm_requests) == 1:
                        return _Response({"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "search_repo", "arguments": '{"query":"handbook"}'}}]}}]})
                    return _Response({"choices": [{"message": {"role": "assistant", "content": "Repository summary", "tool_calls": []}}]})

                async def mcp_search(_: str, arguments: dict[str, object]) -> dict[str, object]:
                    mcp_requests.append(arguments)
                    return {"results": [{"path": "docs/handbook.md", "line_start": 1, "line_end": 1, "content": "hello"}]}

                output = io.StringIO()
                status = await _run(AgentPaths(task=task, scenario_marker=marker), output, post_llm=post_llm, mcp_search=mcp_search)
                events = [json.loads(line)["type"] for line in output.getvalue().splitlines()]
                return status, llm_requests, mcp_requests, events

        status, llm_requests, mcp_requests, events = asyncio.run(exercise())
        self.assertEqual(status, 0)
        self.assertEqual(len(llm_requests), 2)
        self.assertEqual(len(mcp_requests), 1)
        self.assertEqual(llm_requests[0]["tool_choice"], {"type": "function", "function": {"name": "search_repo"}})
        self.assertEqual(llm_requests[1]["tool_choice"], "none")
        self.assertEqual(events, ["prompt", "llm_request", "tool_call", "rag", "mcp_request", "mcp_result", "llm_request", "llm_response", "agent", "lab_result"])
