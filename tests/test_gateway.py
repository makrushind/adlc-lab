import asyncio
import unittest
from unittest.mock import patch

import httpx

from aiweekend_target.__main__ import _health
from aiweekend_target.errors import ErrorCode, TargetError
from aiweekend_target.gateway.app import create_app
from aiweekend_target.gateway.transport import _upstream_error
from aiweekend_target.lab.config import MODEL_PAIR


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

    def test_health_command_rejects_non_readiness_gateway_documents(self) -> None:
        class Response:
            status_code = 400

            @staticmethod
            def json() -> dict[str, object]:
                return {"ok": False, "error": {"code": "CONFIG", "message": "fabricated", "details": None}, "exit_code": 1}

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
        self.assertEqual(captured.exception.code, ErrorCode.PROVIDER)


class GatewayChatAsgiTests(unittest.TestCase):
    def _post(self, content: bytes) -> httpx.Response:
        async def exercise() -> httpx.Response:
            app = create_app()
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            with patch("aiweekend_target.gateway.app.read_secret", return_value="placeholder"):
                async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
                    return await client.post("/v1/chat/completions", content=content)

        return asyncio.run(exercise())

    def test_malformed_chat_json_returns_canonical_policy_document(self) -> None:
        response = self._post(b"{")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {"ok": False, "error": {"code": "POLICY", "message": "request body must be a JSON object", "details": None}, "exit_code": 1},
        )

    def test_invalid_chat_transport_return_returns_canonical_provider_document(self) -> None:
        async def invalid_send_chat(*_: object) -> object:
            return object()

        async def exercise() -> httpx.Response:
            app = create_app()
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            with patch("aiweekend_target.gateway.app.read_secret", return_value="placeholder"):
                with patch("aiweekend_target.gateway.app.send_chat", invalid_send_chat):
                    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
                        return await client.post("/v1/chat/completions", content=('{"model":"' + MODEL_PAIR + '","messages":[]}').encode("utf-8"))

        response = asyncio.run(exercise())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {"ok": False, "error": {"code": "PROVIDER", "message": "Hugging Face Router returned an invalid response", "details": None}, "exit_code": 1},
        )
