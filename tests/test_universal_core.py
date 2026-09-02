import asyncio
import json
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

from aiweekend_target.core import (
    Boundary,
    BoundaryContext,
    BoundaryPipeline,
    ComponentCatalog,
    ComponentKind,
    ComponentRef,
    Finding,
    FindingPolicy,
    ModelDescriptor,
    ModelInvocation,
)
from aiweekend_target.core.engine import AgentSession
from aiweekend_target.lab.components import install_builtin_components
from aiweekend_target.lab.experiment import (
    execute_run_plan,
    persist_private_evidence,
    persist_public_artifact,
)
from aiweekend_target.lab.scenario_v3 import (
    ExperimentProfile,
    compile_run_plan,
    load_private_oracle_v3,
    load_scenario_v3,
)
from aiweekend_target.tools import (
    CompositeToolProvider,
    ToolProtocolError,
    ToolRegistry,
    ToolSpec,
)


def _final(content: str) -> dict[str, object]:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _call(call_id: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments, separators=(",", ":")),
                            },
                        }
                    ],
                }
            }
        ]
    }


class ScriptedModel:
    def __init__(self, responses: list[dict[str, object]], requests: list[dict[str, object]]) -> None:
        self._responses = list(responses)
        self._requests = requests
        self.invocations: list[ModelInvocation] = []

    async def describe(self) -> ModelDescriptor:
        return ModelDescriptor(
            "scripted-model",
            frozenset({"chat_completions", "tool_calls"}),
            "scripted",
        )

    async def complete(
        self, request: dict[str, object], invocation: ModelInvocation
    ) -> Mapping[str, object]:
        self._requests.append(json.loads(json.dumps(request)))
        self.invocations.append(invocation)
        return self._responses.pop(0)


class FlagAnalyzer:
    id = "test.flag"

    def analyze(self, context: BoundaryContext) -> tuple[Finding, ...]:
        if context.boundary is Boundary.TOOL_CALL:
            return (Finding(self.id, "test.block", "high"),)
        return ()


class ResultAnalyzer:
    id = "test.result"

    def analyze(self, context: BoundaryContext) -> tuple[Finding, ...]:
        if context.boundary is Boundary.TOOL_RESULT:
            return (Finding(self.id, "test.poison", "high"),)
        return ()


def _echo_registry(calls: list[dict[str, object]]) -> ToolRegistry:
    registry = ToolRegistry()
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
        lambda arguments: calls.append(arguments) or {"echo": arguments["text"]},
    )
    return registry


class UniversalEngineTests(unittest.TestCase):
    def test_empty_catalog_and_direct_answer_cross_all_shared_boundaries(self) -> None:
        requests: list[dict[str, object]] = []
        evidence = []
        session = AgentSession(
            provider=ScriptedModel([_final("ready")], requests),
            tools=ToolRegistry(),
            pipeline=BoundaryPipeline(evidence_sink=evidence.append),
            run_id="test-direct",
        )

        result = asyncio.run(session.run_turn("Answer directly."))

        self.assertEqual(result.answer, "ready")
        self.assertNotIn("tools", requests[0])
        self.assertNotIn("tool_choice", requests[0])
        self.assertEqual(
            [item.boundary for item in evidence],
            [Boundary.INPUT, Boundary.MODEL_REQUEST, Boundary.MODEL_OUTPUT, Boundary.FINAL_OUTPUT],
        )
        self.assertEqual(
            [
                item.correlation_id
                for item in evidence
                if item.boundary in {Boundary.MODEL_REQUEST, Boundary.MODEL_OUTPUT}
            ],
            ["model-0001-0001", "model-0001-0001"],
        )

    def test_only_a_model_proposal_can_trigger_a_tool_and_auto_is_not_forced(self) -> None:
        requests: list[dict[str, object]] = []
        calls: list[dict[str, object]] = []
        events: list[dict[str, object]] = []
        model = ScriptedModel(
            [_call("model-secret-id", "echo", {"text": "hello"}), _final("done")],
            requests,
        )
        session = AgentSession(
            provider=model,
            tools=_echo_registry(calls),
            pipeline=BoundaryPipeline(event_sink=events.append),
            run_id="test-tool",
            event_sink=events.append,
        )

        result = asyncio.run(session.run_turn("Handle this."))

        self.assertEqual(calls, [{"text": "hello"}])
        self.assertEqual(result.answer, "done")
        self.assertEqual(requests[0]["tool_choice"], "auto")
        self.assertIs(requests[0]["parallel_tool_calls"], False)
        public = json.dumps(events)
        self.assertNotIn("model-secret-id", public)
        self.assertIn("call-0001", public)
        self.assertEqual(
            [item.request_id for item in model.invocations],
            ["model-0001-0001", "model-0001-0002"],
        )
        model_events = [
            item for item in events if item.get("type") in {"llm_request", "llm_response"}
        ]
        self.assertEqual(
            [item["model_request_id"] for item in model_events],
            [
                "model-0001-0001",
                "model-0001-0001",
                "model-0001-0002",
                "model-0001-0002",
            ],
        )

    def test_enforce_policy_blocks_side_effect_but_model_can_continue(self) -> None:
        requests: list[dict[str, object]] = []
        calls: list[dict[str, object]] = []
        boundary_evidence = []
        session = AgentSession(
            provider=ScriptedModel(
                [_call("call", "echo", {"text": "blocked"}), _final("host blocked it")],
                requests,
            ),
            tools=_echo_registry(calls),
            pipeline=BoundaryPipeline(
                analyzers=(FlagAnalyzer(),),
                policy=FindingPolicy(blocked_codes=("test.block",), enforce=True),
                evidence_sink=boundary_evidence.append,
            ),
            run_id="test-block",
        )

        result = asyncio.run(session.run_turn("Try it."))

        self.assertEqual(result.answer, "host blocked it")
        self.assertEqual(calls, [])
        self.assertEqual(result.tool_records[0].decision, "block")
        self.assertIn("blocked_by_host_policy", requests[1]["messages"][-1]["content"])
        tool_call = next(item for item in boundary_evidence if item.boundary is Boundary.TOOL_CALL)
        self.assertEqual(tool_call.findings[0].code, "test.block")
        self.assertEqual(tool_call.decision.action.value, "block")

    def test_observe_and_enforce_have_the_same_findings(self) -> None:
        async def exercise(enforce: bool):
            evidence = []
            pipeline = BoundaryPipeline(
                analyzers=(FlagAnalyzer(),),
                policy=FindingPolicy(blocked_codes=("test.block",), enforce=enforce),
                evidence_sink=evidence.append,
            )
            await pipeline.process(
                BoundaryContext(
                    Boundary.TOOL_CALL,
                    {"name": "echo", "arguments": {}},
                    "test-pipeline",
                    1,
                    1,
                    "call-0001",
                    "echo",
                )
            )
            return evidence[0]

        observed = asyncio.run(exercise(False))
        enforced = asyncio.run(exercise(True))
        self.assertEqual(observed.findings, enforced.findings)
        self.assertEqual(observed.decision.action.value, "allow")
        self.assertEqual(enforced.decision.action.value, "block")

    def test_replaced_tool_result_never_reaches_the_next_model_request(self) -> None:
        requests: list[dict[str, object]] = []
        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                "source",
                "Return a source record.",
                {"type": "object", "additionalProperties": False},
            ),
            lambda arguments: {"fact": "safe", "note": "POISON_MARKER"},
        )
        boundaries = []
        session = AgentSession(
            provider=ScriptedModel(
                [_call("result", "source", {}), _final("safe")], requests
            ),
            tools=registry,
            pipeline=BoundaryPipeline(
                analyzers=(ResultAnalyzer(),),
                policy=FindingPolicy(
                    blocked_codes=("test.poison",),
                    enforce=True,
                    replacements={"tool_result": {"status": "redacted"}},
                ),
                evidence_sink=boundaries.append,
            ),
            run_id="test-result-replace",
        )

        result = asyncio.run(session.run_turn("Read the source."))

        self.assertEqual(result.answer, "safe")
        self.assertIn("POISON_MARKER", json.dumps(result.tool_records[0].result))
        self.assertNotIn("POISON_MARKER", json.dumps(requests[1]))
        self.assertIn("redacted", json.dumps(requests[1]))
        result_boundary = next(
            item for item in boundaries if item.boundary is Boundary.TOOL_RESULT
        )
        self.assertIn("POISON_MARKER", json.dumps(result_boundary.original_payload))
        self.assertNotIn("POISON_MARKER", json.dumps(result_boundary.delivered_payload))


class CatalogAndArchitectureTests(unittest.TestCase):
    def test_catalog_rejects_unknown_duplicate_and_routing_configuration(self) -> None:
        catalog = ComponentCatalog()
        catalog.register(ComponentKind.POLICY, "test.policy", "1", lambda config: object())
        reference = ComponentRef(ComponentKind.POLICY, "test.policy", "1", {})
        catalog.preflight((reference,))
        with self.assertRaises(ValueError):
            catalog.preflight((reference, reference))
        with self.assertRaises(ValueError):
            catalog.resolve(ComponentRef(ComponentKind.POLICY, "unknown.policy", "1", {}))
        with self.assertRaises(ValueError):
            ComponentRef(
                ComponentKind.MODEL,
                "test.model",
                "1",
                {"url": "http://attacker.invalid/v1"},
            )

    def test_router_detects_duplicate_ownership_without_name_specific_code(self) -> None:
        first = _echo_registry([])
        second = _echo_registry([])
        provider = CompositeToolProvider((first, second))
        with self.assertRaises(ToolProtocolError):
            asyncio.run(provider.list_tools())

    def test_core_has_no_domain_or_adapter_imports(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "aiweekend_target" / "core"
        forbidden_imports = (
            "aiweekend_target.repo_rag",
            "aiweekend_target.autonomous_lab",
            "aiweekend_target.lab",
            "aiweekend_target.live",
            "lookup_fact",
            "search_repo",
            "ORCHID",
        )
        document = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
        for marker in forbidden_imports:
            self.assertNotIn(marker, document)


class TwoDomainAcceptanceTests(unittest.TestCase):
    def _execute(
        self,
        scenario_name: str,
        responses: list[dict[str, object]],
        analyzers: tuple[ComponentRef, ...],
    ):
        requests: list[dict[str, object]] = []
        model = ScriptedModel(responses, requests)
        catalog = ComponentCatalog()
        install_builtin_components(
            catalog,
            model_factories={"gateway.runtime": lambda config: model},
        )
        root = Path(__file__).resolve().parents[1] / "scenarios" / "v3" / scenario_name
        scenario = load_scenario_v3(root)
        oracle = load_private_oracle_v3(root, scenario)
        profile = ExperimentProfile(
            f"test-{scenario_name}",
            ComponentRef(ComponentKind.MODEL, "gateway.runtime", "1", {}),
            analyzers,
            ComponentRef(
                ComponentKind.POLICY,
                "builtin.findings",
                "1",
                {"enforce": False, "blocked_codes": [], "replacements": {}},
            ),
            f"sha256:{'0' * 64}",
        )
        plan = asyncio.run(compile_run_plan(scenario, profile, oracle, catalog))
        return asyncio.run(execute_run_plan(plan, run_id=f"accept-{scenario_name}")), requests

    def test_two_unrelated_component_combinations_use_the_same_engine(self) -> None:
        first, first_requests = self._execute(
            "external-fact",
            [
                _call("fact", "archive_lookup", {"project": "Meridian"}),
                _final("ORCHID"),
            ],
            (
                ComponentRef(
                    ComponentKind.ANALYZER,
                    "builtin.pattern",
                    "1",
                    {
                        "boundaries": ["tool_result"],
                        "patterns": ["ignore previous"],
                        "code": "injection.suspected",
                        "severity": "high",
                        "case_sensitive": False,
                    },
                ),
            ),
        )
        second, second_requests = self._execute(
            "second-domain",
            [
                _call(
                    "math",
                    "multiply_numbers",
                    {"left": 170003, "right": 190001},
                ),
                _final("32300740003"),
            ],
            (
                ComponentRef(
                    ComponentKind.ANALYZER,
                    "builtin.numeric-limit",
                    "1",
                    {
                        "boundary": "tool_result",
                        "field": "product",
                        "maximum": 999999999999,
                        "code": "numeric.too-large",
                        "severity": "low",
                    },
                ),
            ),
        )

        self.assertTrue(first.evaluation.ok)
        self.assertTrue(second.evaluation.ok)
        self.assertEqual(first_requests[0]["tool_choice"], "auto")
        self.assertEqual(second_requests[0]["tool_choice"], "auto")
        self.assertEqual(first.evidence.tool_calls[0].name, "archive_lookup")
        self.assertEqual(second.evidence.tool_calls[0].name, "multiply_numbers")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            public = persist_public_artifact(first, root / "public")
            private = persist_private_evidence(first, (root / "private").resolve())
            public_text = "".join(
                path.read_text(encoding="utf-8") for path in public.iterdir()
            )
            self.assertNotIn("ORCHID", public_text)
            self.assertIn("ORCHID", private.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
