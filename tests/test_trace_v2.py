import json
import unittest
from datetime import datetime, timezone

from aiweekend_target.lab.trace_v2 import (
    Taint,
    TraceRecorder,
    TraceValidationError,
    validate_event_stream,
)


class TraceV2ContractTests(unittest.TestCase):
    def test_normalizes_orders_and_redacts_sensitive_facts(self) -> None:
        recorder = TraceRecorder(
            "run-1",
            "tool-result-attack",
            "lmstudio-local",
            sensitive_values={"mcp-canary": "CANARY-V2-123"},
            clock=lambda: datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
        )
        first = recorder.emit(
            "llm_request",
            turn=1,
            correlation_id="turn-1",
            taints=(Taint.PROMPT_UNTRUSTED,),
            facts={"preview": "CANARY-V2-123 Authorization: Bearer top-secret"},
        )
        recorder.emit("llm_response", turn=1, facts={"tool_calls": 0})
        validate_event_stream(recorder.events)
        document = first.as_dict()
        self.assertEqual(document["schema"], 2)
        self.assertEqual(document["seq"], 1)
        self.assertEqual(document["timestamp"], "2026-08-28T12:00:00Z")
        self.assertEqual(document["redactions"], ["credential", "mcp-canary"])
        with self.assertRaises(TypeError):
            first.facts["preview"] = "mutated"  # type: ignore[index]
        rendered = recorder.to_jsonl()
        self.assertNotIn("CANARY-V2-123", rendered)
        self.assertNotIn("top-secret", rendered)
        self.assertEqual(
            [json.loads(line)["seq"] for line in rendered.splitlines()], [1, 2]
        )

    def test_rejects_unknown_taint_non_json_fact_and_mixed_stream(self) -> None:
        first = TraceRecorder("run-1", "baseline-v2", "local")
        second = TraceRecorder("run-2", "baseline-v2", "local")
        event = first.emit("run_started")
        other = second.emit("run_started")
        with self.assertRaises(TraceValidationError):
            first.emit("bad", taints=("UNKNOWN",))
        with self.assertRaises(TraceValidationError):
            first.emit("bad", facts={"bytes": b"not-json"})
        with self.assertRaises(TraceValidationError):
            validate_event_stream((event, other))


if __name__ == "__main__":
    unittest.main()
