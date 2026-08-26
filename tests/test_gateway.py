import unittest
from unittest.mock import patch

from aiweekend_target.__main__ import _health
from aiweekend_target.errors import ErrorCode, TargetError
from aiweekend_target.gateway.transport import _upstream_error


class GatewayErrorTests(unittest.TestCase):
    def test_forbidden_upstream_response_is_authentication_failure(self) -> None:
        self.assertEqual(_upstream_error(403).code, ErrorCode.AUTH)

    def test_rate_limited_upstream_response_is_quota_failure(self) -> None:
        self.assertEqual(_upstream_error(429).code, ErrorCode.QUOTA)

    def test_health_command_unwraps_a_canonical_gateway_readiness_failure(self) -> None:
        class Response:
            status_code = 401

            @staticmethod
            def json() -> dict[str, object]:
                return {"ok": False, "error": {"code": "AUTH", "message": "credential unavailable", "details": None}, "exit_code": 1}

        class Client:
            def __enter__(self) -> "Client":
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def get(self, _: str) -> Response:
                return Response()

        with patch("aiweekend_target.__main__.httpx.Client", return_value=Client()):
            with self.assertRaisesRegex(TargetError, "gateway readiness contract failed") as captured:
                _health("hf-gateway")
        self.assertEqual(captured.exception.code, ErrorCode.AUTH)
