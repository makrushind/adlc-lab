import unittest

from aiweekend_target.errors import ErrorCode, classify_upstream_status, local_response_status, match_gateway_error


class ErrorClassificationTests(unittest.TestCase):
    def test_upstream_auth_and_quota_aliases_keep_canonical_codes(self) -> None:
        self.assertEqual(classify_upstream_status(401), ErrorCode.AUTH)
        self.assertEqual(classify_upstream_status(403), ErrorCode.AUTH)
        self.assertEqual(classify_upstream_status(402), ErrorCode.QUOTA)
        self.assertEqual(classify_upstream_status(429), ErrorCode.QUOTA)

    def test_missing_model_and_other_upstream_statuses_keep_distinct_codes(self) -> None:
        self.assertEqual(classify_upstream_status(404), ErrorCode.MODEL_UNAVAILABLE)
        self.assertEqual(classify_upstream_status(500), ErrorCode.PROVIDER)

    def test_local_gateway_statuses_do_not_follow_upstream_aliases(self) -> None:
        self.assertEqual(local_response_status(ErrorCode.AUTH), 401)
        self.assertEqual(local_response_status(ErrorCode.QUOTA), 402)
        self.assertEqual(local_response_status(ErrorCode.MODEL_UNAVAILABLE), 404)
        self.assertEqual(local_response_status(ErrorCode.PROVIDER), 400)

    def test_chat_gateway_matcher_accepts_only_actual_chat_codes_at_exact_statuses(self) -> None:
        document = {"ok": False, "error": {"code": "POLICY", "message": "invalid request", "details": None}, "exit_code": 1}
        self.assertEqual(match_gateway_error(document, 400), ErrorCode.POLICY)
        self.assertEqual(match_gateway_error(document, 500), None)
        for code, status in (("CONFIG", 400), ("MCP", 500), ("BUSY", 500), ("OTHER", 500)):
            with self.subTest(code=code):
                malformed = {"ok": False, "error": {"code": code, "message": "fabricated", "details": None}, "exit_code": 1}
                self.assertIsNone(match_gateway_error(malformed, status))

    def test_readiness_gateway_matcher_rejects_chat_only_and_malformed_documents(self) -> None:
        readiness_error = {"ok": False, "error": {"code": "AUTH", "message": "credential unavailable", "details": None}, "exit_code": 1}
        self.assertEqual(match_gateway_error(readiness_error, 401, readiness=True), ErrorCode.AUTH)
        policy_error = {"ok": False, "error": {"code": "POLICY", "message": "invalid request", "details": None}, "exit_code": 1}
        self.assertIsNone(match_gateway_error(policy_error, 400, readiness=True))
        self.assertIsNone(match_gateway_error({"ok": False, "error": {"code": "AUTH"}, "exit_code": 1}, 401, readiness=True))

    def test_gateway_matcher_rejects_boolean_integer_fields(self) -> None:
        document = {"ok": False, "error": {"code": "POLICY", "message": "invalid request", "details": None}, "exit_code": 1}
        boolean_exit_code = {**document, "exit_code": True}
        self.assertIsNone(match_gateway_error(boolean_exit_code, 400))
        self.assertIsNone(match_gateway_error(document, True))
