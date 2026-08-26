import unittest

from aiweekend_target.lab.trace import TraceObserver, safe_preview


class TraceContractTests(unittest.TestCase):
    def test_success_events_keep_stage_out_of_wire_shape_and_result_order(self) -> None:
        observer = TraceObserver("baseline")
        event = observer.emit("prompt", stage="prompt", status="prepared")
        self.assertNotIn("stage", event)
        observer.emit("rag", stage="rag")
        observer.emit("mcp_result", stage="mcp")
        observer.emit("llm_response", stage="llm")
        observer.emit("agent", stage="agent")
        self.assertEqual(observer.result(True)["stages"], ["prompt", "rag", "mcp", "llm", "agent"])

    def test_preview_redacts_canaries_and_credentials(self) -> None:
        preview = safe_preview("Authorization: Bearer secret-token ADLC_CANARY_MCP_4DB2E8", 200)
        self.assertNotIn("secret-token", preview)
        self.assertNotIn("ADLC_CANARY_MCP_4DB2E8", preview)

