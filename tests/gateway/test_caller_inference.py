"""Caller inference contracts at real authenticated HTTP/provider boundaries."""

import json
import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


AUTH = {"Authorization": "Bearer gateway-test-key"}
TOOL = {
    "type": "function",
    "function": {
        "name": "crm_probe_echo",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
}
CALL = {
    "id": "call_probe_1",
    "type": "function",
    "function": {
        "name": "crm_probe_echo",
        "arguments": '{"text":"sentinel"}',
    },
}


@asynccontextmanager
async def configured_provider(handler, *, mode="chat_completions"):
    provider = web.Application()
    provider.router.add_post("/v1/chat/completions", handler)
    provider.router.add_post("/v1/responses", handler)
    async with TestServer(provider) as server:
        config = {
            "model": {"provider": "custom:fixture", "default": "fixture-model"},
            "providers": {
                "fixture": {
                    "base_url": str(server.make_url("/v1")),
                    "api_key": "provider-only-secret",
                    "api_mode": mode,
                }
            },
            "gateway": {
                "api_server": {
                    "max_concurrent_runs": 1,
                    "caller_inference": {
                        "enabled": True,
                        "provider": "custom:fixture",
                        "model": "fixture-model",
                        "context_length": 32000,
                    },
                }
            },
        }
        Path(os.environ["HERMES_HOME"], "config.yaml").write_text(json.dumps(config))
        async with TestClient(TestServer(app_for())) as client:
            yield client


def app_for(key="gateway-test-key"):
    adapter = APIServerAdapter(PlatformConfig(extra={"key": key}))
    app = web.Application()
    for method, path, handler in adapter._http_route_table():
        app.router.add_route(method, path, handler)
    return app


@pytest.mark.asyncio
async def test_inference_is_authenticated_and_disabled_by_default():
    async with TestClient(TestServer(app_for())) as client:
        response = await client.post("/v1/inference/chat/completions", json={})
        assert response.status == 401
        response = await client.post(
            "/v1/inference/chat/completions",
            json={},
            headers={"Authorization": "Bearer gateway-test-key"},
        )
        assert response.status == 503
        assert (await response.json())["error"]["code"] == "inference_unconfigured"


@pytest.mark.asyncio
async def test_inference_discovery_is_separate_from_server_agent_models():
    async def provider(request):
        raise AssertionError("Discovery must not call the provider")

    async with configured_provider(provider) as client:
        assert (await client.get("/v1/inference/models")).status == 401
        response = await client.get("/v1/inference/models", headers=AUTH)
        assert response.status == 200
        assert (await response.json())["data"] == [
            {
                "id": "hermes-inference",
                "object": "model",
                "owned_by": "hermes",
                "context_length": 32000,
                "tool_execution": "caller",
            }
        ]
        response = await client.get("/v1/models", headers=AUTH)
        assert "hermes-inference" not in {
            model["id"] for model in (await response.json())["data"]
        }


@pytest.mark.asyncio
async def test_caller_tools_and_results_round_trip_without_server_execution():
    received = []

    async def provider(request):
        assert request.headers["Authorization"] == "Bearer provider-only-secret"
        body = await request.json()
        received.append(body)
        message = (
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [CALL],
                "reasoning_content": "provider-private-reasoning",
            }
            if len(received) == 1
            else {"role": "assistant", "content": "done"}
        )
        return web.json_response({
            "id": "fixture",
            "object": "chat.completion",
            "created": 1,
            "model": "fixture-model",
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "tool_calls" if len(received) == 1 else "stop",
                }
            ],
        })

    messages = [
        {"role": "system", "content": "Caller instructions"},
        {"role": "user", "content": "Echo sentinel"},
    ]
    choice = {"type": "function", "function": {"name": "crm_probe_echo"}}
    async with configured_provider(provider) as client:
        response = await client.post(
            "/v1/inference/chat/completions",
            headers=AUTH,
            json={
                "model": "hermes-inference",
                "messages": messages,
                "tools": [TOOL],
                "tool_choice": choice,
            },
        )
        assert response.status == 200, await response.text()
        first = await response.json()
        assert "provider-private-reasoning" not in json.dumps(first)
        assert first["choices"][0]["message"]["tool_calls"] == [CALL]
        assert len(received) == 1
        assert received[0]["messages"] == messages
        assert received[0]["tools"] == [TOOL]
        assert received[0]["tool_choice"] == choice
        messages += [
            first["choices"][0]["message"],
            {"role": "tool", "tool_call_id": "call_probe_1", "content": "sentinel"},
        ]
        response = await client.post(
            "/v1/inference/chat/completions",
            headers=AUTH,
            json={
                "model": "hermes-inference",
                "messages": messages,
                "tools": [TOOL],
            },
        )
        assert response.status == 200
        assert (await response.json())["choices"][0]["message"]["content"] == "done"
        assert received[1]["messages"] == messages
        assert len(received) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_config",
    [
        None,
        [],
        {"enabled": True},
        {"enabled": True, "provider": " AUTO ", "model": "x"},
        {"enabled": True, "provider": "unknown-provider", "model": "x"},
        {"enabled": True, "provider": ["custom"], "model": "x"},
    ],
)
async def test_missing_or_malformed_config_never_falls_back(bad_config):
    Path(os.environ["HERMES_HOME"], "config.yaml").write_text(
        json.dumps({
            "gateway": {"api_server": {"caller_inference": bad_config}},
        })
    )
    async with TestClient(TestServer(app_for())) as client:
        response = await client.post(
            "/v1/inference/chat/completions",
            headers=AUTH,
            json={
                "model": "hermes-inference",
                "messages": [{"role": "user", "content": "x"}],
            },
        )
        assert response.status == 503
        assert (await response.json())["error"]["code"] == "inference_unconfigured"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 401, 402, 429, 500])
async def test_provider_errors_and_redirects_are_not_retried_or_disclosed(status):
    received = []

    async def provider(request):
        received.append(request.path)
        return web.Response(
            status=status,
            text="provider-only-secret private-error",
            headers={"Location": "/v1/responses"},
        )

    async with configured_provider(provider) as client:
        response = await client.post(
            "/v1/inference/chat/completions",
            headers=AUTH,
            json={
                "model": "hermes-inference",
                "messages": [{"role": "user", "content": "x"}],
            },
        )
        assert response.status == 502
        text = await response.text()
        assert "provider-only-secret" not in text
        assert "private-error" not in text
        assert received == ["/v1/chat/completions"]


@pytest.mark.asyncio
async def test_inference_obeys_shared_concurrency_limit():
    entered, release = asyncio.Event(), asyncio.Event()

    async def provider(request):
        entered.set()
        await release.wait()
        return web.json_response({
            "id": "fixture",
            "object": "chat.completion",
            "created": 1,
            "model": "fixture-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "done"},
                    "finish_reason": "stop",
                }
            ],
        })

    async with configured_provider(provider) as client:
        body = {
            "model": "hermes-inference",
            "messages": [{"role": "user", "content": "x"}],
        }
        first = asyncio.create_task(
            client.post("/v1/inference/chat/completions", headers=AUTH, json=body)
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            second = await asyncio.wait_for(
                client.post("/v1/inference/chat/completions", headers=AUTH, json=body),
                timeout=5,
            )
            assert second.status == 429
        finally:
            release.set()
            await first


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"extra_body": {"model": "other-model"}},
        {"extra_headers": {"Authorization": "Bearer caller-secret"}},
        {"base_url": "https://invalid.example"},
        {"provider": "auto"},
        {"tools": [{"type": "web_search"}]},
        {"model": "other-model"},
        {"messages": []},
        {"messages": [{"role": "user", "content": "x", "codex_message_items": []}]},
        {"stream": "true"},
        {"response_format": {"type": "json_object"}},
    ],
)
async def test_caller_cannot_override_routing_or_request_server_tools(override):
    received = []

    async def provider(request):
        received.append(await request.json())
        return web.json_response({})

    async with configured_provider(provider) as client:
        response = await client.post(
            "/v1/inference/chat/completions",
            headers=AUTH,
            json={
                "model": "hermes-inference",
                "messages": [{"role": "user", "content": "hello"}],
                **override,
            },
        )
        assert response.status == 400
        assert received == []


@pytest.mark.asyncio
async def test_streamed_tool_identity_and_split_arguments_reach_caller():
    async def provider(request):
        body = await request.json()
        assert body["stream"] is True
        assert body["tools"] == [TOOL]
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for delta, finish in [
            (
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_probe_1",
                            "type": "function",
                            "function": {"name": "crm_probe_echo", "arguments": ""},
                        }
                    ]
                },
                None,
            ),
            (
                {"tool_calls": [{"index": 0, "function": {"arguments": '{"text":'}}]},
                None,
            ),
            (
                {
                    "tool_calls": [
                        {"index": 0, "function": {"arguments": '"sentinel"}'}}
                    ]
                },
                None,
            ),
            ({}, "tool_calls"),
        ]:
            provider_delta = {**delta, "reasoning_content": "provider-private-reasoning"}
            chunk = {
                "id": "fixture",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "fixture-model",
                "choices": [{"index": 0, "delta": provider_delta, "finish_reason": finish}],
            }
            await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        return response

    async with configured_provider(provider) as client:
        response = await client.post(
            "/v1/inference/chat/completions",
            headers=AUTH,
            json={
                "model": "hermes-inference",
                "messages": [{"role": "user", "content": "Echo"}],
                "tools": [TOOL],
                "stream": True,
            },
        )
        assert response.status == 200
        text = await response.text()
        assert "provider-private-reasoning" not in text
        assert text.endswith("data: [DONE]\n\n")
        chunks = [
            json.loads(line[6:])
            for line in text.splitlines()
            if line.startswith("data: {")
        ]
        assert all(chunk["model"] == "hermes-inference" for chunk in chunks)
        calls = [
            call
            for chunk in chunks
            for call in chunk["choices"][0]["delta"].get("tool_calls", [])
        ]
        assert calls[0]["id"] == "call_probe_1"
        assert calls[0]["function"]["name"] == "crm_probe_echo"
        assert (
            "".join(call["function"]["arguments"] for call in calls)
            == '{"text":"sentinel"}'
        )
        assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_responses_provider_preserves_tools_and_tool_result_continuation(stream):
    received = []

    async def provider(request):
        body = await request.json()
        received.append(body)
        assert request.path == "/v1/responses"
        assert request.headers["Authorization"] == "Bearer provider-only-secret"
        assert body["store"] is False
        assert body["tools"][0]["name"] == "crm_probe_echo"
        assert body["tool_choice"] == {"type": "function", "name": "crm_probe_echo"}
        assert body["input"][0] == {
            "role": "system",
            "content": "Keep this instruction",
        }
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        events = [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": "fc_fixture",
                    "type": "function_call",
                    "call_id": "call_probe_1",
                    "name": "crm_probe_echo",
                    "arguments": "",
                    "status": "in_progress",
                },
            },
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "item_id": "fc_fixture",
                "delta": '{"text":"sentinel"}',
            },
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_fixture",
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            },
        ]
        for event in events:
            await response.write(
                f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
            )
        return response

    async with configured_provider(provider, mode="codex_responses") as client:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "Keep this instruction"},
            {"role": "user", "content": "Echo"},
        ]
        body = {
            "model": "hermes-inference",
            "messages": messages,
            "tools": [TOOL],
            "stream": stream,
            "tool_choice": {"type": "function", "function": {"name": "crm_probe_echo"}},
        }
        response = await client.post(
            "/v1/inference/chat/completions", headers=AUTH, json=body
        )
        assert response.status == 200, await response.text()
        if stream:
            text = await response.text()
            chunks = [
                json.loads(line[6:])
                for line in text.splitlines()
                if line.startswith("data: {")
            ]
            calls = [
                call
                for chunk in chunks
                for choice in chunk["choices"]
                for call in choice["delta"].get("tool_calls", [])
            ]
            assert calls[0]["id"] == "call_probe_1"
            assert calls[0]["function"]["name"] == "crm_probe_echo"
            assert (
                "".join(call["function"]["arguments"] for call in calls)
                == '{"text":"sentinel"}'
            )
            assert text.endswith("data: [DONE]\n\n")
        else:
            payload = await response.json()
            assert payload["choices"][0]["message"]["tool_calls"] == [CALL]
            assert payload["choices"][0]["finish_reason"] == "tool_calls"
        messages += [
            {"role": "assistant", "content": None, "tool_calls": [CALL]},
            {"role": "tool", "tool_call_id": "call_probe_1", "content": "sentinel"},
        ]
        response = await client.post(
            "/v1/inference/chat/completions", headers=AUTH, json=body
        )
        await response.read()
        assert response.status == 200
        assert received[1]["input"][-1] == {
            "type": "function_call_output",
            "call_id": "call_probe_1",
            "output": "sentinel",
        }
        assert len(received) == 2


@pytest.mark.asyncio
async def test_manual_listener_without_key_cannot_enable_inference():
    async def provider(request):
        raise AssertionError("Unauthenticated requests must never reach the provider")

    async with configured_provider(provider):
        async with TestClient(TestServer(app_for(key=""))) as client:
            response = await client.post(
                "/v1/inference/chat/completions",
                json={
                    "model": "hermes-inference",
                    "messages": [{"role": "user", "content": "x"}],
                },
            )
            assert response.status == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,stream",
    [("chat_completions", True), ("codex_responses", True), ("codex_responses", False)],
)
@pytest.mark.parametrize("failure", ["truncated", "error"])
async def test_failed_provider_stream_never_reports_success(mode, stream, failure):
    async def provider(request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        if mode == "chat_completions":
            event = {
                "id": "fixture",
                "created": 1,
                "model": "fixture-model",
                "object": "chat.completion.chunk",
                "choices": [
                    {"index": 0, "delta": {"content": "partial"}, "finish_reason": None}
                ],
            }
        else:
            event = {
                "type": "response.output_text.delta",
                "delta": "partial",
                "output_index": 0,
                "item_id": "msg_fixture",
                "content_index": 0,
            }
        await response.write(f"data: {json.dumps(event)}\n\n".encode())
        if failure == "error":
            event = (
                {"error": {"message": "provider-only-secret private-error"}}
                if mode == "chat_completions"
                else {
                    "type": "response.failed",
                    "response": {
                        "status": "failed",
                        "error": {"message": "provider-only-secret private-error"},
                    },
                }
            )
            await response.write(f"data: {json.dumps(event)}\n\n".encode())
        return response

    async with configured_provider(provider, mode=mode) as client:
        response = await client.post(
            "/v1/inference/chat/completions",
            headers=AUTH,
            json={
                "model": "hermes-inference",
                "messages": [{"role": "user", "content": "x"}],
                "stream": stream,
            },
        )
        text = await response.text()
        assert "inference_provider_failed" in text
        assert "[DONE]" not in text
        assert "provider-only-secret" not in text
        assert "private-error" not in text
        assert response.status == (200 if stream else 502)
