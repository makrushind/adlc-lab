import asyncio
import hashlib
import json
import unittest
from unittest.mock import patch

import httpx

from aiweekend_target.__main__ import _health
from aiweekend_target.errors import ErrorCode, TargetError
from aiweekend_target.gateway.app import create_app
from aiweekend_target.gateway.transport import _bounded_error_diagnostics, _upstream_error, probe_available
from aiweekend_target.lab.config import MODEL_PAIR


class GatewayErrorTests(unittest.TestCase):
    def test_forbidden_upstream_response_is_authentication_failure(self) -> None:
        self.assertEqual(_upstream_error(403).code, ErrorCode.AUTH)

    def test_rate_limited_upstream_response_is_quota_failure(self) -> None:
        self.assertEqual(_upstream_error(429).code, ErrorCode.QUOTA)

    def test_probe_handles_an_unknown_length_upstream_failure_without_reading_its_body(self) -> None:
        class ClosingTransport(httpx.AsyncBaseTransport):
            def __init__(self) -> None:
                self.closed = False
                self.iterated = False

            async def handle_async_request(self, _: httpx.Request) -> httpx.Response:
                transport = self

                class ResponseStream(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        transport.iterated = True
                        yield b'{}'

                    async def aclose(self) -> None:
                        return None

                return httpx.Response(400, stream=ResponseStream())

            async def aclose(self) -> None:
                self.closed = True

        transport = ClosingTransport()
        with self.assertRaises(TargetError) as captured:
            asyncio.run(probe_available("secret", transport))
        self.assertEqual(captured.exception.code, ErrorCode.PROVIDER)
        self.assertFalse(transport.iterated)

    def test_declared_oversized_error_body_is_not_requested_from_the_source(self) -> None:
        delivered: list[int] = []

        class ResponseStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                delivered.append(16 * 1024 + 1)
                yield b"x" * (16 * 1024 + 1)

            async def aclose(self) -> None:
                return None

        async def exercise() -> dict[str, object]:
            response = httpx.Response(400, headers={"content-length": str(16 * 1024 + 1)}, stream=ResponseStream())
            try:
                return await _bounded_error_diagnostics(response)
            finally:
                await response.aclose()

        diagnostics = asyncio.run(exercise())
        self.assertEqual(delivered, [])
        self.assertEqual(diagnostics, {
            "status": 400,
            "error_type": None,
            "error_code": None,
            "error_param": None,
            "has_failed_generation": False,
            "sample_bytes": 0,
            "sample_sha256": hashlib.sha256(b"").hexdigest(),
            "truncated": True,
        })

    def test_unknown_or_invalid_declared_error_body_lengths_are_never_read(self) -> None:
        for name, headers in (
            ("missing", {}),
            ("invalid", {"content-length": "not-a-number"}),
            ("signed", {"content-length": "+1"}),
            ("negative", {"content-length": "-1"}),
        ):
            with self.subTest(name=name):
                delivered: list[int] = []

                class ResponseStream(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        delivered.append(1)
                        yield b"x"

                    async def aclose(self) -> None:
                        return None

                async def exercise() -> dict[str, object]:
                    response = httpx.Response(400, headers=headers, stream=ResponseStream())
                    try:
                        return await _bounded_error_diagnostics(response)
                    finally:
                        await response.aclose()

                diagnostics = asyncio.run(exercise())
                self.assertEqual(delivered, [])
                self.assertEqual(diagnostics["sample_bytes"], 0)
                self.assertEqual(diagnostics["sample_sha256"], hashlib.sha256(b"").hexdigest())
                self.assertTrue(diagnostics["truncated"])

    def test_error_diagnostics_expose_an_oversized_source_chunk_without_retaining_it(self) -> None:
        delivered: list[int] = []

        class ResponseStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                delivered.append(16 * 1024 + 1)
                yield b"x" * (16 * 1024 + 1)

            async def aclose(self) -> None:
                return None

        async def exercise() -> dict[str, object]:
            response = httpx.Response(400, headers={"content-length": str(16 * 1024)}, stream=ResponseStream())
            try:
                return await _bounded_error_diagnostics(response)
            finally:
                await response.aclose()

        diagnostics = asyncio.run(exercise())
        self.assertEqual(delivered, [16 * 1024 + 1])
        self.assertEqual(diagnostics["sample_bytes"], 16 * 1024)
        self.assertEqual(diagnostics["truncated"], True)

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

    def test_chat_response_stream_failure_returns_the_existing_provider_document(self) -> None:
        class FailingResponseStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                raise httpx.ReadError("stream interrupted")
                yield b""

            async def aclose(self) -> None:
                return None

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(400, headers={"content-length": "2"}, stream=FailingResponseStream())

        async def exercise() -> httpx.Response:
            app = create_app(transport=httpx.MockTransport(handler))
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            with patch("aiweekend_target.gateway.app.read_secret", return_value="placeholder"):
                async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
                    return await client.post("/v1/chat/completions", json={"model": MODEL_PAIR, "messages": []})

        response = asyncio.run(exercise())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {"ok": False, "error": {"code": "PROVIDER", "message": "Hugging Face Router is unavailable", "details": None}, "exit_code": 1},
        )

    def test_pathological_digit_only_content_length_returns_canonical_error_without_reading_body(self) -> None:
        delivered: list[int] = []

        class ResponseStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                delivered.append(1)
                yield b"x"

            async def aclose(self) -> None:
                return None

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                headers={"content-length": "9" * 5_000},
                stream=ResponseStream(),
            )

        async def exercise() -> httpx.Response:
            app = create_app(transport=httpx.MockTransport(handler))
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            with patch("aiweekend_target.gateway.app.read_secret", return_value="placeholder"):
                async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
                    return await client.post(
                        "/v1/chat/completions",
                        json={"model": MODEL_PAIR, "messages": []},
                    )

        response = asyncio.run(exercise())

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {
                "ok": False,
                "error": {
                    "code": "PROVIDER",
                    "message": "Hugging Face Router request failed",
                    "details": {"upstream_status": 400},
                },
                "exit_code": 1,
            },
        )
        self.assertEqual(delivered, [])

    def test_chat_upstream_failures_log_only_bounded_redacted_transport_metadata(self) -> None:
        canary = "CANARY_HF_GATEWAY_TRANSPORT_MUST_NOT_APPEAR"
        credential = "hf_transport_credential_must_not_appear"
        short_provider_values = ("ADLC_CANARY_RAG_7A91C4", "hf_shortcredential", "token1234567890")
        secret = "hf_gateway_secret_must_not_appear"
        bodies = [
            (
                "valid JSON",
                json.dumps(
                    {
                        "error": {
                            "type": "invalid_request_error",
                            "code": "tool_validation_failed",
                            "param": "tools",
                            "message": f"{canary} {credential}",
                        }
                    },
                    separators=(",", ":"),
                ).encode("utf-8"),
                "invalid_request_error",
                "tool_validation_failed",
                "tools",
                False,
                False,
            ),
            (
                "invalid JSON",
                f'{{"error":{{"message":"{canary} {credential}"}}'.encode("utf-8"),
                None,
                None,
                None,
                False,
                False,
            ),
            (
                "failed generation",
                json.dumps(
                    {
                        "error": {"type": "generation_error", "code": "generation_failed", "param": None},
                        "failed_generation": {"arguments": f"{canary} {credential}"},
                    },
                    separators=(",", ":"),
                ).encode("utf-8"),
                "generation_error",
                "generation_failed",
                None,
                True,
                False,
            ),
            (
                "unsafe error fields",
                json.dumps(
                    {
                        "error": {"type": canary, "code": canary, "param": canary, "message": credential},
                    },
                    separators=(",", ":"),
                ).encode("utf-8"),
                None,
                None,
                None,
                False,
                False,
            ),
            *(
                (
                    f"short unsafe error fields {value}",
                    json.dumps(
                        {"error": {"type": value, "code": value, "param": value}},
                        separators=(",", ":"),
                    ).encode("utf-8"),
                    None,
                    None,
                    None,
                    False,
                    False,
                )
                for value in short_provider_values
            ),
            (
                "oversized body",
                (f'{canary} {credential} '.encode("utf-8") * 1_000),
                None,
                None,
                None,
                False,
                True,
            ),
        ]
        requests: list[httpx.Request] = []
        logs: list[dict[str, object]] = []

        class ResponseStream(httpx.AsyncByteStream):
            def __init__(self, content: bytes) -> None:
                self.content = content

            async def __aiter__(self):
                yield self.content

            async def aclose(self) -> None:
                return None

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            _, response_body, *_ = bodies[len(requests) - 1]
            return httpx.Response(
                400,
                headers={"content-type": "application/json", "content-length": str(len(response_body))},
                stream=ResponseStream(response_body),
            )

        async def exercise() -> list[httpx.Response]:
            app = create_app(transport=httpx.MockTransport(handler), trace_sink=logs.append)
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            payload = {
                "model": MODEL_PAIR,
                "messages": [
                    {"role": "user", "content": f"Review {canary} {credential}"},
                    {"role": "tool", "content": json.dumps({"arguments": f"{canary} {credential}"})},
                ],
                "tools": [
                    {"type": "function", "function": {"name": "search_repo", "description": f"{canary} {credential}"}}
                ],
                "tool_choice": {"type": "function", "function": {"name": "search_repo"}},
                "max_tokens": 73,
                "max_completion_tokens": 47,
                "stream": False,
            }
            with patch("aiweekend_target.gateway.app.read_secret", return_value=secret):
                async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
                    return [await client.post("/v1/chat/completions", json=payload) for _ in bodies]

        responses = asyncio.run(exercise())

        expected_payload = {
            "model": MODEL_PAIR,
            "messages": [
                {"role": "user", "content": f"Review {canary} {credential}"},
                {"role": "tool", "content": json.dumps({"arguments": f"{canary} {credential}"})},
            ],
            "tools": [{"type": "function", "function": {"name": "search_repo", "description": f"{canary} {credential}"}}],
            "tool_choice": {"type": "function", "function": {"name": "search_repo"}},
            "max_tokens": 73,
            "max_completion_tokens": 47,
            "stream": False,
        }
        expected_request = json.dumps(expected_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        expected_tool_hash = hashlib.sha256(
            json.dumps(expected_payload["tools"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        expected_message_bytes = [
            len(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            for message in expected_payload["messages"]
        ]

        self.assertEqual(len(requests), len(bodies))
        for request in requests:
            self.assertEqual(str(request.url), "https://router.huggingface.co/v1/chat/completions")
            self.assertEqual(request.headers["content-type"], "application/json")
            self.assertEqual(request.headers["accept"], "application/json")
            self.assertEqual(request.headers["authorization"], f"Bearer {secret}")
            self.assertEqual(request.content, expected_request)
        for response in responses:
            self.assertEqual(response.status_code, 400)
            self.assertEqual(
                response.json(),
                {
                    "ok": False,
                    "error": {
                        "code": "PROVIDER",
                        "message": "Hugging Face Router request failed",
                        "details": {"upstream_status": 400},
                    },
                    "exit_code": 1,
                },
            )

        request_logs = [entry for entry in logs if entry["type"] == "llm_request"]
        self.assertEqual(len(request_logs), len(bodies))
        for entry in request_logs:
            self.assertEqual(entry["message_bytes"], expected_message_bytes)
            self.assertEqual(entry["tool_choice_kind"], "named")
            self.assertEqual(entry["tool_schema_sha256"], expected_tool_hash)
            self.assertEqual(entry["output_token_cap"], 47)

        error_logs = [entry for entry in logs if entry["type"] == "gateway_error"]
        self.assertEqual(len(error_logs), len(bodies))
        for entry, expected in zip(error_logs, bodies, strict=True):
            _, response_body, error_type, error_code, error_param, has_failed_generation, truncated = expected
            retained_sample = b"" if len(response_body) > 16 * 1024 else response_body
            self.assertEqual(entry["upstream"], {
                "status": 400,
                "error_type": error_type,
                "error_code": error_code,
                "error_param": error_param,
                "has_failed_generation": has_failed_generation,
                "sample_bytes": len(retained_sample),
                "sample_sha256": hashlib.sha256(retained_sample).hexdigest(),
                "truncated": truncated,
            })
        rendered_logs = json.dumps(logs, separators=(",", ":"))
        for forbidden in (canary, credential, *short_provider_values, secret, "Authorization", "Bearer"):
            self.assertNotIn(forbidden, rendered_logs)
