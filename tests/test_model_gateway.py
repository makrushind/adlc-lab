import asyncio
import json
import unittest
from pathlib import Path

import httpx

from aiweekend_target.errors import ErrorCode, TargetError
from aiweekend_target.gateway.app import create_app
from aiweekend_target.gateway.policy import validate_chat_body
from aiweekend_target.gateway.transport import (
    MAX_CHAT_RESPONSE_BYTES,
    UpstreamResponse,
    UpstreamStream,
    discover_model,
    send_chat,
)
from aiweekend_target.lab.config import (
    BASE_MODEL,
    MODEL_PAIR,
    PROVIDER,
    ROUTER_URL,
    ModelProfile,
    load_model_profile,
)


TARGET_MODEL = "gemma-4-e4b-uncensored-hauhaucs-aggressive"


def _lmstudio_profile(*, require_tools: bool = True) -> ModelProfile:
    return ModelProfile(
        backend="lmstudio",
        model_id=TARGET_MODEL,
        base_url="http://127.0.0.1:1234/v1/",
        owner="lmstudio",
        auth_mode="none",
        require_tools=require_tools,
    )


def _advertised_models(*ids: str) -> dict[str, object]:
    return {"object": "list", "data": [{"id": model_id, "object": "model"} for model_id in ids]}


def _native_models(*, tools: bool = True) -> dict[str, object]:
    return {
        "models": [{
            "type": "llm",
            "publisher": "HauhauCS",
            "key": TARGET_MODEL,
            "loaded_instances": [{"id": TARGET_MODEL, "config": {}}],
            "max_context_length": 131072,
            "capabilities": {"vision": True, "trained_for_tool_use": tools},
        }]
    }


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.delivered = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.delivered += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class ModelProfileTests(unittest.TestCase):
    def test_empty_environment_preserves_the_hugging_face_profile(self) -> None:
        profile = load_model_profile({})
        self.assertEqual(profile.backend, "huggingface")
        self.assertEqual(profile.model_id, MODEL_PAIR)
        self.assertEqual(profile.discovery_model_id, BASE_MODEL)
        self.assertEqual(profile.provider, PROVIDER)
        self.assertEqual(profile.base_url, ROUTER_URL)
        self.assertEqual(profile.secret_path, Path("/run/secrets/hf_token"))
        self.assertTrue(profile.require_tools)

    def test_lmstudio_profile_uses_exact_operator_selected_model(self) -> None:
        profile = load_model_profile({
            "ADLC_MODEL_BACKEND": "lmstudio",
            "ADLC_MODEL_ID": TARGET_MODEL,
        })
        self.assertEqual(profile.model_id, TARGET_MODEL)
        self.assertEqual(profile.base_url, "http://host.docker.internal:1234/v1")
        self.assertEqual(profile.auth_mode, "none")
        self.assertIsNone(profile.secret_path)

    def test_lmstudio_requires_an_explicit_model_id(self) -> None:
        with self.assertRaises(TargetError) as captured:
            load_model_profile({"ADLC_MODEL_BACKEND": "lmstudio"})
        self.assertEqual(captured.exception.code, ErrorCode.CONFIG)

    def test_profile_rejects_url_credentials_and_non_v1_routes(self) -> None:
        for url in (
            "http://token@127.0.0.1:1234/v1",
            "http://127.0.0.1:1234/v1?target=other",
            "http://127.0.0.1:1234/chat/completions",
        ):
            with self.subTest(url=url):
                with self.assertRaises(TargetError) as captured:
                    load_model_profile({
                        "ADLC_MODEL_BACKEND": "lmstudio",
                        "ADLC_MODEL_ID": TARGET_MODEL,
                        "ADLC_MODEL_BASE_URL": url,
                    })
                self.assertEqual(captured.exception.code, ErrorCode.CONFIG)

    def test_hugging_face_pair_cannot_disagree_with_pinned_provider(self) -> None:
        with self.assertRaises(TargetError) as captured:
            load_model_profile({
                "ADLC_MODEL_BACKEND": "huggingface",
                "ADLC_MODEL_ID": "openai/gpt-oss-20b:other",
            })
        self.assertEqual(captured.exception.code, ErrorCode.CONFIG)

    def test_hugging_face_route_cannot_be_redirected_by_environment(self) -> None:
        for url in ("https://attacker.example/v1", f"{ROUTER_URL}/"):
            with self.subTest(url=url):
                with self.assertRaises(TargetError) as captured:
                    load_model_profile({
                        "ADLC_MODEL_BACKEND": "huggingface",
                        "ADLC_MODEL_BASE_URL": url,
                    })
                self.assertEqual(captured.exception.code, ErrorCode.CONFIG)

    def test_hugging_face_profile_accepts_only_the_exact_router_url(self) -> None:
        with self.assertRaises(TargetError) as captured:
            ModelProfile(
                backend="huggingface",
                model_id=MODEL_PAIR,
                base_url="https://hf-proxy.example/v1",
                owner=PROVIDER,
                auth_mode="required",
                secret_path=Path("/run/secrets/hf_token"),
                discovery_model_id=BASE_MODEL,
                provider=PROVIDER,
            )
        self.assertEqual(captured.exception.code, ErrorCode.CONFIG)

    def test_chat_body_cannot_override_the_profile_route(self) -> None:
        accepted = validate_chat_body({"model": TARGET_MODEL, "messages": []}, TARGET_MODEL)
        self.assertEqual(accepted["model"], TARGET_MODEL)
        for field in ("base_url", "url", "headers", "api_key", "provider", "fallbacks"):
            with self.subTest(field=field):
                with self.assertRaises(TargetError) as captured:
                    validate_chat_body({"model": TARGET_MODEL, "messages": [], field: "attacker"}, TARGET_MODEL)
                self.assertEqual(captured.exception.code, ErrorCode.POLICY)
        with self.assertRaises(TargetError) as captured:
            validate_chat_body(
                {"model": TARGET_MODEL, "messages": [], "stream": True},
                TARGET_MODEL,
            )
        self.assertEqual(captured.exception.code, ErrorCode.POLICY)


class LmStudioTransportTests(unittest.TestCase):
    def test_discovery_matches_exact_model_and_reports_native_capabilities(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/v1/models":
                return httpx.Response(200, json=_advertised_models(TARGET_MODEL))
            if request.url.path == "/api/v1/models":
                return httpx.Response(200, json=_native_models())
            return httpx.Response(404, json={"error": "unexpected route"})

        capabilities = asyncio.run(discover_model(None, httpx.MockTransport(handler), _lmstudio_profile()))

        self.assertEqual([request.url.path for request in requests], ["/v1/models", "/api/v1/models"])
        self.assertTrue(capabilities.chat_completions)
        self.assertIs(capabilities.tool_calls, True)
        self.assertIs(capabilities.vision, True)
        self.assertEqual(capabilities.max_context_length, 131072)
        self.assertIs(capabilities.loaded, True)
        self.assertEqual(capabilities.source, "lmstudio-native-metadata")
        for request in requests:
            self.assertNotIn("authorization", request.headers)

    def test_discovery_does_not_accept_a_similar_model_id(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_advertised_models(f"{TARGET_MODEL}-other"))

        with self.assertRaises(TargetError) as captured:
            asyncio.run(discover_model(None, httpx.MockTransport(handler), _lmstudio_profile()))
        self.assertEqual(captured.exception.code, ErrorCode.MODEL_UNAVAILABLE)

    def test_required_tool_capability_is_enforced_from_metadata(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = _advertised_models(TARGET_MODEL) if request.url.path == "/v1/models" else _native_models(tools=False)
            return httpx.Response(200, json=payload)

        with self.assertRaises(TargetError) as captured:
            asyncio.run(discover_model(None, httpx.MockTransport(handler), _lmstudio_profile()))
        self.assertEqual(captured.exception.code, ErrorCode.MODEL_UNAVAILABLE)
        self.assertIn("not declared tool-capable", captured.exception.message)

    def test_non_tool_run_can_use_openai_compatible_discovery_without_native_metadata(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/models":
                return httpx.Response(200, json=_advertised_models(TARGET_MODEL))
            return httpx.Response(404, json={"error": "native endpoint unavailable"})

        capabilities = asyncio.run(
            discover_model(None, httpx.MockTransport(handler), _lmstudio_profile(require_tools=False))
        )
        self.assertIsNone(capabilities.tool_calls)
        self.assertEqual(capabilities.source, "openai-compatible-model-list")

    def test_chat_is_forwarded_only_to_the_profile_url_without_auth(self) -> None:
        requests: list[httpx.Request] = []
        payload = {"model": TARGET_MODEL, "messages": [{"role": "user", "content": "ping"}]}

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "model": TARGET_MODEL,
                    "choices": [{"message": {"role": "assistant", "content": "pong"}}],
                },
            )

        response = asyncio.run(send_chat(payload, None, httpx.MockTransport(handler), _lmstudio_profile()))

        self.assertIsInstance(response, UpstreamResponse)
        self.assertEqual(len(requests), 1)
        self.assertEqual(str(requests[0].url), "http://127.0.0.1:1234/v1/chat/completions")
        self.assertNotIn("authorization", requests[0].headers)
        self.assertEqual(json.loads(requests[0].content), payload)

    def test_chat_rejects_missing_or_different_response_model(self) -> None:
        for name, response_body in (
            ("missing", {"choices": []}),
            ("different", {"model": f"{TARGET_MODEL}-other", "choices": []}),
        ):
            with self.subTest(name=name):
                def handler(_: httpx.Request, body: dict[str, object] = response_body) -> httpx.Response:
                    return httpx.Response(200, json=body)

                with self.assertRaises(TargetError) as captured:
                    asyncio.run(
                        send_chat(
                            {"model": TARGET_MODEL, "messages": []},
                            None,
                            httpx.MockTransport(handler),
                            _lmstudio_profile(),
                        )
                    )
                self.assertEqual(captured.exception.code, ErrorCode.MODEL_UNAVAILABLE)

    def test_chat_requires_a_strict_json_object(self) -> None:
        invalid_responses = (
            b"[]",
            b"{",
            b"\xff",
            f'{{"model":"{TARGET_MODEL}","model":"{TARGET_MODEL}"}}'.encode(),
            f'{{"model":"{TARGET_MODEL}","value":NaN}}'.encode(),
        )
        for response_body in invalid_responses:
            with self.subTest(response_body=response_body[:40]):
                def handler(_: httpx.Request, body: bytes = response_body) -> httpx.Response:
                    return httpx.Response(200, content=body)

                with self.assertRaises(TargetError) as captured:
                    asyncio.run(
                        send_chat(
                            {"model": TARGET_MODEL, "messages": []},
                            None,
                            httpx.MockTransport(handler),
                            _lmstudio_profile(),
                        )
                    )
                self.assertEqual(captured.exception.code, ErrorCode.PROVIDER)

    def test_non_stream_chat_response_is_bounded(self) -> None:
        prefix = f'{{"model":"{TARGET_MODEL}","padding":"'.encode()
        suffix = b'"}'
        exact = prefix + (b"x" * (MAX_CHAT_RESPONSE_BYTES - len(prefix) - len(suffix))) + suffix
        self.assertEqual(len(exact), MAX_CHAT_RESPONSE_BYTES)

        def accepted_handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=exact)

        accepted = asyncio.run(
            send_chat(
                {"model": TARGET_MODEL, "messages": []},
                None,
                httpx.MockTransport(accepted_handler),
                _lmstudio_profile(),
            )
        )
        self.assertIsInstance(accepted, UpstreamResponse)

        oversized_stream = _ChunkStream(prefix, b"x" * MAX_CHAT_RESPONSE_BYTES)

        def rejected_handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=oversized_stream)

        with self.assertRaises(TargetError) as captured:
            asyncio.run(
                send_chat(
                    {"model": TARGET_MODEL, "messages": []},
                    None,
                    httpx.MockTransport(rejected_handler),
                    _lmstudio_profile(),
                )
            )
        self.assertEqual(captured.exception.code, ErrorCode.PROVIDER)
        self.assertTrue(oversized_stream.closed)

    def test_streaming_chat_response_has_a_cumulative_bound(self) -> None:
        accepted_stream = _ChunkStream(
            b"x" * (MAX_CHAT_RESPONSE_BYTES // 2),
            b"y" * (MAX_CHAT_RESPONSE_BYTES - MAX_CHAT_RESPONSE_BYTES // 2),
        )

        async def consume(stream: _ChunkStream) -> bytes:
            def handler(_: httpx.Request) -> httpx.Response:
                return httpx.Response(200, stream=stream)

            response = await send_chat(
                {"model": TARGET_MODEL, "messages": [], "stream": True},
                None,
                httpx.MockTransport(handler),
                _lmstudio_profile(),
            )
            self.assertIsInstance(response, UpstreamStream)
            result = bytearray()
            async for chunk in response.body:
                result.extend(chunk)
            return bytes(result)

        self.assertEqual(len(asyncio.run(consume(accepted_stream))), MAX_CHAT_RESPONSE_BYTES)
        self.assertTrue(accepted_stream.closed)

        rejected_stream = _ChunkStream(b"x" * MAX_CHAT_RESPONSE_BYTES, b"y")
        with self.assertRaises(TargetError) as captured:
            asyncio.run(consume(rejected_stream))
        self.assertEqual(captured.exception.code, ErrorCode.PROVIDER)
        self.assertTrue(rejected_stream.closed)


class HuggingFaceTransportTests(unittest.TestCase):
    def test_chat_accepts_profile_or_discovery_model_identity_only(self) -> None:
        profile = load_model_profile({})
        for response_model in (profile.model_id, profile.discovery_model_id):
            with self.subTest(response_model=response_model):
                def handler(_: httpx.Request, model: str | None = response_model) -> httpx.Response:
                    return httpx.Response(200, json={"model": model, "choices": []})

                response = asyncio.run(
                    send_chat(
                        {"model": profile.model_id, "messages": []},
                        "secret",
                        httpx.MockTransport(handler),
                        profile,
                    )
                )
                self.assertIsInstance(response, UpstreamResponse)

        for response_body in ({"choices": []}, {"model": "other/model", "choices": []}):
            with self.subTest(response_body=response_body):
                def handler(_: httpx.Request, body: dict[str, object] = response_body) -> httpx.Response:
                    return httpx.Response(200, json=body)

                with self.assertRaises(TargetError) as captured:
                    asyncio.run(
                        send_chat(
                            {"model": profile.model_id, "messages": []},
                            "secret",
                            httpx.MockTransport(handler),
                            profile,
                        )
                    )
                self.assertEqual(captured.exception.code, ErrorCode.MODEL_UNAVAILABLE)


class LmStudioGatewayAsgiTests(unittest.TestCase):
    def test_models_endpoint_exposes_only_the_selected_model_and_capabilities(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/models":
                return httpx.Response(200, json=_advertised_models(TARGET_MODEL, "some-other-model"))
            return httpx.Response(200, json=_native_models())

        async def exercise() -> httpx.Response:
            app = create_app(profile=_lmstudio_profile(), transport=httpx.MockTransport(handler))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
                return await client.get("/v1/models")

        response = asyncio.run(exercise())
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["data"]), 1)
        self.assertEqual(payload["data"][0]["id"], TARGET_MODEL)
        self.assertEqual(payload["data"][0]["backend"], "lmstudio")
        self.assertIs(payload["data"][0]["capabilities"]["tool_calls"], True)

    def test_chat_rejects_routing_injection_before_contacting_lmstudio(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={})

        async def exercise() -> httpx.Response:
            app = create_app(profile=_lmstudio_profile(), transport=httpx.MockTransport(handler))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
                return await client.post(
                    "/v1/chat/completions",
                    json={"model": TARGET_MODEL, "messages": [], "base_url": "http://attacker.invalid/v1"},
                )

        response = asyncio.run(exercise())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "POLICY")
        self.assertEqual(requests, [])


if __name__ == "__main__":
    unittest.main()
