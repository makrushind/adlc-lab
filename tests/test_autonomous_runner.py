import asyncio
import json
import unittest
from types import SimpleNamespace

from aiweekend_target.runners import (
    AgentBudgetExceeded,
    AgentLimits,
    AgentProtocolError,
    ModelInvocationError,
    run_autonomous,
)
from aiweekend_target.tools import (
    MCPToolProvider,
    ToolProtocolError,
    ToolRegistry,
    ToolSpec,
    UnknownToolError,
)


def _final(content: str, *, tokens: int | None = None) -> dict[str, object]:
    document: dict[str, object] = {
        "choices": [{"message": {"role": "assistant", "content": content, "tool_calls": []}}]
    }
    if tokens is not None:
        document["usage"] = {"total_tokens": tokens}
    return document


def _calls(*calls: tuple[str, str, str], tokens: int | None = None) -> dict[str, object]:
    document: dict[str, object] = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                        for call_id, name, arguments in calls
                    ],
                }
            }
        ]
    }
    if tokens is not None:
        document["usage"] = {"total_tokens": tokens}
    return document


def _registry(order: list[str] | None = None) -> ToolRegistry:
    registry = ToolRegistry()

    async def echo(arguments: dict[str, object]) -> dict[str, object]:
        if order is not None:
            order.append(f"echo:{arguments['text']}")
        return {"echo": arguments["text"]}

    registry.register(
        ToolSpec(
            "echo",
            "Echo text.",
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        ),
        echo,
        argument_validator=lambda value: (
            value
            if isinstance(value, dict) and set(value) == {"text"} and isinstance(value["text"], str)
            else (_ for _ in ()).throw(ValueError("bad echo arguments"))
        ),
    )
    return registry


class AutonomousRunnerTests(unittest.TestCase):
    def test_model_can_finish_without_any_tool(self) -> None:
        requests: list[dict[str, object]] = []
        events: list[dict[str, object]] = []

        async def complete(request: dict[str, object]) -> dict[str, object]:
            requests.append(request)
            return _final("No repository lookup is needed.", tokens=9)

        result = asyncio.run(
            run_autonomous(
                "Say whether a lookup is necessary.",
                model="local-model",
                complete=complete,
                tools=ToolRegistry(),
                event_sink=events.append,
            )
        )

        self.assertEqual(result.answer, "No repository lookup is needed.")
        self.assertEqual((result.turns, result.tool_calls, result.reported_tokens), (1, 0, 9))
        self.assertTrue(result.usage_complete)
        self.assertNotIn("tools", requests[0])
        self.assertNotIn("tool_choice", requests[0])
        self.assertEqual([event["type"] for event in events], ["llm_request", "llm_response", "final_answer"])

    def test_lm_studio_reasoning_field_and_empty_tool_content_are_accepted(self) -> None:
        requests = 0

        async def complete(_: dict[str, object]) -> dict[str, object]:
            nonlocal requests
            requests += 1
            if requests == 1:
                document = _calls(("call_1", "echo", '{"text":"local"}'))
                document["choices"][0]["message"]["content"] = ""
                document["choices"][0]["message"]["reasoning_content"] = "provider-specific reasoning"
                return document
            document = _final("Local answer")
            document["choices"][0]["message"]["reasoning_content"] = "provider-specific reasoning"
            return document

        result = asyncio.run(
            run_autonomous(
                "Use the local model.",
                model="gemma-local",
                complete=complete,
                tools=_registry(),
            )
        )
        self.assertEqual((result.answer, result.turns, result.tool_calls), ("Local answer", 2, 1))

    def test_model_selects_zero_to_many_calls_and_host_executes_them_sequentially(self) -> None:
        requests: list[dict[str, object]] = []
        order: list[str] = []

        async def complete(request: dict[str, object]) -> dict[str, object]:
            requests.append(json.loads(json.dumps(request)))
            if len(requests) == 1:
                return _calls(
                    ("call_1", "echo", '{"text":"first"}'),
                    ("call_2", "echo", '{"text":"second"}'),
                    tokens=10,
                )
            return _final("Both results received.", tokens=12)

        result = asyncio.run(
            run_autonomous(
                "Use echo when useful.",
                model="local-model",
                complete=complete,
                tools=_registry(order),
            )
        )

        self.assertEqual(order, ["echo:first", "echo:second"])
        self.assertEqual((result.turns, result.tool_calls, result.reported_tokens), (2, 2, 22))
        self.assertEqual(
            [(record.call_id, record.name, record.decision, record.executed) for record in result.tool_records],
            [("call_1", "echo", "allow", True), ("call_2", "echo", "allow", True)],
        )
        self.assertEqual(result.tool_records[0].arguments, {"text": "first"})
        self.assertEqual(
            [record.result for record in result.tool_records],
            [{"echo": "first"}, {"echo": "second"}],
        )
        self.assertEqual(requests[0]["tool_choice"], "auto")
        self.assertIs(requests[0]["parallel_tool_calls"], False)
        self.assertEqual(requests[0]["max_tokens"], 2_048)
        self.assertEqual(
            [message["role"] for message in requests[1]["messages"]],
            ["user", "assistant", "tool", "tool"],
        )
        self.assertEqual(json.loads(requests[1]["messages"][2]["content"]), {"echo": "first"})
        self.assertEqual(json.loads(requests[1]["messages"][3]["content"]), {"echo": "second"})

    def test_unknown_and_malformed_calls_stop_before_any_execution(self) -> None:
        cases = {
            "unknown": _calls(("call_1", "shell", "{}")),
            "bad json": _calls(("call_1", "echo", "{")),
            "non object": _calls(("call_1", "echo", "[]")),
            "duplicate key": _calls(("call_1", "echo", '{"text":"a","text":"b"}')),
            "duplicate id": _calls(("call_1", "echo", '{"text":"a"}'), ("call_1", "echo", '{"text":"b"}')),
        }
        for name, response in cases.items():
            with self.subTest(name=name):
                order: list[str] = []

                async def complete(_: dict[str, object]) -> dict[str, object]:
                    return response

                expected = UnknownToolError if name == "unknown" else AgentProtocolError
                with self.assertRaises(expected):
                    asyncio.run(
                        run_autonomous(
                            "Try one tool.",
                            model="local-model",
                            complete=complete,
                            tools=_registry(order),
                        )
                    )
                self.assertEqual(order, [])

    def test_schema_invalid_arguments_are_blocked_before_execution(self) -> None:
        events: list[dict[str, object]] = []
        evidence: list[object] = []
        order: list[str] = []

        async def complete(_: dict[str, object]) -> dict[str, object]:
            return _calls(("call_bad", "echo", '{"text":7,"extra":true}'))

        with self.assertRaises(AgentProtocolError):
            asyncio.run(
                run_autonomous(
                    "Try an invalid call.",
                    model="local-model",
                    complete=complete,
                    tools=_registry(order),
                    event_sink=events.append,
                    evidence_sink=evidence.append,
                )
            )

        self.assertEqual(order, [])
        self.assertEqual(len(evidence), 1)
        self.assertEqual((evidence[0].decision, evidence[0].executed), ("block", False))
        decisions = [event for event in events if event["type"] == "policy_decision"]
        self.assertEqual(decisions[0]["reason"], "invalid_arguments")

    def test_unknown_call_is_traced_and_recorded_as_blocked(self) -> None:
        events: list[dict[str, object]] = []
        evidence: list[object] = []

        async def complete(_: dict[str, object]) -> dict[str, object]:
            return _calls(("call_1", "shell", '{"command":"secret command"}'))

        with self.assertRaises(UnknownToolError):
            asyncio.run(
                run_autonomous(
                    "Try a forbidden tool.",
                    model="local-model",
                    complete=complete,
                    tools=_registry(),
                    event_sink=events.append,
                    evidence_sink=evidence.append,
                )
            )
        self.assertEqual(
            [event["type"] for event in events],
            ["llm_request", "llm_response", "tool_call_proposed", "policy_decision"],
        )
        self.assertEqual(events[-1]["outcome"], "block")
        self.assertEqual(events[-1]["reason"], "unknown_tool")
        self.assertNotIn("secret command", json.dumps(events))
        self.assertEqual(len(evidence), 1)
        self.assertEqual((evidence[0].decision, evidence[0].executed), ("block", False))

    def test_call_and_turn_budgets_are_checked_before_side_effects(self) -> None:
        response = _calls(
            ("call_1", "echo", '{"text":"a"}'),
            ("call_2", "echo", '{"text":"b"}'),
        )
        for name, limits in (
            ("calls", AgentLimits(max_tool_calls=1)),
            ("turns", AgentLimits(max_turns=1)),
        ):
            with self.subTest(name=name):
                order: list[str] = []

                async def complete(_: dict[str, object]) -> dict[str, object]:
                    return response

                with self.assertRaises(AgentBudgetExceeded) as raised:
                    asyncio.run(
                        run_autonomous(
                            "Try tools.",
                            model="local-model",
                            complete=complete,
                            tools=_registry(order),
                            limits=limits,
                        )
                    )
                self.assertEqual(raised.exception.budget, "tool_calls" if name == "calls" else "turns")
                self.assertEqual(order, [])

    def test_reused_call_id_and_reported_token_overage_fail_closed(self) -> None:
        for name in ("id", "tokens"):
            with self.subTest(name=name):
                requests = 0
                order: list[str] = []

                async def complete(_: dict[str, object]) -> dict[str, object]:
                    nonlocal requests
                    requests += 1
                    if name == "tokens":
                        return _calls(("call_1", "echo", '{"text":"a"}'), tokens=11)
                    return _calls(("same", "echo", f'{{"text":"{requests}"}}'))

                expected = AgentBudgetExceeded if name == "tokens" else AgentProtocolError
                with self.assertRaises(expected):
                    asyncio.run(
                        run_autonomous(
                            "Try tools.",
                            model="local-model",
                            complete=complete,
                            tools=_registry(order),
                            limits=AgentLimits(max_reported_tokens=10) if name == "tokens" else AgentLimits(),
                        )
                    )
                self.assertEqual(order, [] if name == "tokens" else ["echo:1"])

    def test_model_failures_and_wall_timeout_are_distinct(self) -> None:
        async def broken(_: dict[str, object]) -> dict[str, object]:
            raise RuntimeError("provider detail must stay behind the boundary")

        with self.assertRaises(ModelInvocationError):
            asyncio.run(run_autonomous("Task", model="local-model", complete=broken, tools=ToolRegistry()))

        async def slow(_: dict[str, object]) -> dict[str, object]:
            await asyncio.sleep(0.05)
            return _final("late")

        with self.assertRaises(AgentBudgetExceeded) as raised:
            asyncio.run(
                run_autonomous(
                    "Task",
                    model="local-model",
                    complete=slow,
                    tools=ToolRegistry(),
                    limits=AgentLimits(max_wall_seconds=0.001),
                )
            )
        self.assertEqual(raised.exception.budget, "wall_time")

    def test_trace_contains_boundary_metadata_but_not_content(self) -> None:
        events: list[dict[str, object]] = []
        secret = "hf_secret_should_not_be_traced"

        async def complete(_: dict[str, object]) -> dict[str, object]:
            return _calls(("call_1", "echo", json.dumps({"text": secret}))) if not any(
                event["type"] == "tool_result" for event in events
            ) else _final(secret)

        result = asyncio.run(
            run_autonomous(
                "Task",
                model="local-model",
                complete=complete,
                tools=_registry(),
                event_sink=events.append,
            )
        )
        self.assertEqual(result.answer, secret)
        self.assertNotIn(secret, json.dumps(events))
        self.assertEqual(
            [event["type"] for event in events],
            [
                "llm_request",
                "llm_response",
                "tool_call_proposed",
                "policy_decision",
                "tool_execution_started",
                "tool_result",
                "llm_request",
                "llm_response",
                "final_answer",
            ],
        )


class ToolProviderTests(unittest.TestCase):
    def test_registry_detaches_data_and_applies_validators(self) -> None:
        schema = {"type": "object", "properties": {"value": {"type": "integer"}}}
        registry = ToolRegistry()
        registry.register(
            ToolSpec("double", "Double a number.", schema),
            lambda arguments: {"value": arguments["value"] * 2},
            argument_validator=lambda value: value
            if isinstance(value, dict) and type(value.get("value")) is int
            else (_ for _ in ()).throw(ValueError("bad")),
        )
        schema["properties"] = {}

        async def exercise() -> tuple[object, object]:
            tools = await registry.list_tools()
            return tools[0].as_openai_tool(), await registry.call_tool("double", {"value": 3})

        public, result = asyncio.run(exercise())
        self.assertEqual(public["function"]["parameters"]["properties"], {"value": {"type": "integer"}})
        self.assertEqual(result, {"value": 6})
        with self.assertRaises(ToolProtocolError):
            asyncio.run(registry.call_tool("double", {"value": "3"}))

    def test_mcp_adapter_exposes_only_allowlisted_tools_and_normalizes_results(self) -> None:
        class Session:
            def __init__(self) -> None:
                self.initialized = 0
                self.calls: list[tuple[str, dict[str, object]]] = []

            async def initialize(self) -> None:
                self.initialized += 1

            async def list_tools(self) -> object:
                return SimpleNamespace(
                    result_type="complete",
                    next_cursor=None,
                    tools=[
                        SimpleNamespace(name="dangerous", description="Do not expose", inputSchema={"type": "object"}),
                        SimpleNamespace(name="echo", description="Echo", inputSchema={"type": "object"}),
                    ],
                )

            async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
                self.calls.append((name, arguments))
                return SimpleNamespace(result_type="complete", is_error=False, structured_content={"ok": True})

        session = Session()
        provider = MCPToolProvider(session, allowlist={"echo"})

        async def exercise() -> tuple[object, object, object]:
            first = await provider.list_tools()
            second = await provider.list_tools()
            result = await provider.call_tool("echo", {"text": "hello"})
            return first, second, result

        first, second, result = asyncio.run(exercise())
        self.assertEqual([tool.name for tool in first], ["echo"])
        self.assertIs(first, second)
        self.assertEqual(session.initialized, 1)
        self.assertEqual(session.calls, [("echo", {"text": "hello"})])
        self.assertEqual(result, {"ok": True})


if __name__ == "__main__":
    unittest.main()
