import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    _ProviderAuthResolutionError,
    StructuredOutputRequestError,
    StructuredOutputRunError,
    StructuredOutputValidationError,
    _structured_output_contract,
    _validated_structured_output,
)


def _adapter(api_key: str = "") -> APIServerAdapter:
    return APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": api_key} if api_key else {})
    )


def _app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    app.router.add_post("/v1/responses", adapter._handle_responses)
    app.router.add_get("/v1/responses/{response_id}", adapter._handle_get_response)
    return app


def _payload(surface: str, *, stream: bool = False) -> dict:
    if surface == "chat":
        return {
            "model": "hermes-agent",
            "messages": [{"role": "user", "content": "answer"}],
            "response_format": {"type": "json_object"},
            "stream": stream,
        }
    return {
        "model": "hermes-agent",
        "input": "answer",
        "text": {"format": {"type": "json_object"}},
        "stream": stream,
    }


def _result(text: str, **extra) -> dict:
    return {
        "final_response": text,
        "messages": [
            {"role": "user", "content": "answer"},
            {"role": "assistant", "content": text},
        ],
        "api_calls": 1,
        **extra,
    }


def _usage() -> dict:
    return {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}


def test_contract_parser_validates_without_schema_retrieval():
    contract = _structured_output_contract(
        {
            "type": "json_schema",
            "name": "answer",
            "schema": {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        responses=True,
    )

    assert _validated_structured_output(
        contract, '```json\n{"ok": true}\n```'
    ) == '{"ok": true}'
    with pytest.raises(StructuredOutputValidationError):
        _validated_structured_output(contract, '{"ok": "yes"}')
    with pytest.raises(ValueError, match="external references"):
        _structured_output_contract(
            {
                "type": "json_schema",
                "name": "answer",
                "schema": {"$ref": "https://example.test/schema"},
            },
            responses=True,
        )


@pytest.mark.parametrize(("value", "responses"), (
    ({"type": "text", "unexpected": True}, False),
    ({"type": "json_object", "unexpected": True}, True),
    ({"type": "json_schema", "json_schema": {
        "name": "answer", "schema": {"type": "object"}, "unexpected": True,
    }}, False),
    ({
        "type": "json_schema", "name": "answer",
        "schema": {"type": "object"}, "unexpected": True,
    }, True),
))
def test_contract_parser_rejects_unknown_fields(value, responses):
    with pytest.raises(ValueError, match="unsupported fields"):
        _structured_output_contract(value, responses=responses)


def test_contract_validator_rejects_numeric_overflow():
    contract = _structured_output_contract(
        {"type": "json_object"}, responses=False
    )

    with pytest.raises(StructuredOutputValidationError, match="finite"):
        _validated_structured_output(contract, '{"value": 1e400}')


@pytest.mark.asyncio
@pytest.mark.parametrize(("surface", "path"), (
    ("chat", "/v1/chat/completions"),
    ("responses", "/v1/responses"),
))
async def test_nonstream_valid_contract_reaches_gateway_boundary(surface, path):
    adapter = _adapter()
    async with TestClient(TestServer(_app(adapter))) as client:
        with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as run:
            run.return_value = (_result('```json\n{"answer": 42}\n```'), _usage())
            response = await client.post(path, json=_payload(surface))
            body = await response.json()

    assert response.status == 200
    rendered = (
        body["choices"][0]["message"]["content"]
        if surface == "chat"
        else body["output"][-1]["content"][0]["text"]
    )
    assert rendered == '{"answer": 42}'
    assert run.call_args.kwargs["output_contract"]["type"] == "json_object"
    assert body["hermes"]["output_contract"]["validated"] is True


@pytest.mark.asyncio
async def test_responses_nonstream_emits_complete_output_message_shape():
    adapter = _adapter()
    async with TestClient(TestServer(_app(adapter))) as client:
        with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as run:
            run.return_value = (_result('{"answer": 42}'), _usage())
            response = await client.post(
                "/v1/responses", json=_payload("responses")
            )
            body = await response.json()

    assert response.status == 200
    message = body["output"][-1]
    assert message["id"].startswith("msg_")
    assert message["status"] == "completed"
    assert message["role"] == "assistant"
    assert message["content"] == [{
        "type": "output_text",
        "text": '{"answer": 42}',
        "annotations": [],
    }]


@pytest.mark.asyncio
async def test_responses_stream_reuses_complete_message_shape_in_terminal_events():
    adapter = _adapter()

    async def run(**kwargs):
        kwargs["stream_delta_callback"]('{"answer": 42}')
        return _result('{"answer": 42}'), _usage()

    async with TestClient(TestServer(_app(adapter))) as client:
        with patch.object(adapter, "_run_agent", side_effect=run):
            response = await client.post(
                "/v1/responses", json=_payload("responses", stream=True)
            )
            stream_body = await response.text()

    events = [
        json.loads(line.removeprefix("data: "))
        for line in stream_body.splitlines()
        if line.startswith("data: ")
    ]
    done_message = next(
        event["item"]
        for event in events
        if event.get("type") == "response.output_item.done"
        and event.get("item", {}).get("type") == "message"
    )
    completed_message = next(
        item
        for event in events
        if event.get("type") == "response.completed"
        for item in event["response"]["output"]
        if item.get("type") == "message"
    )

    assert done_message == completed_message
    assert completed_message["id"].startswith("msg_")
    assert completed_message["status"] == "completed"
    assert completed_message["content"] == [{
        "type": "output_text",
        "text": '{"answer": 42}',
        "annotations": [],
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize(("surface", "path"), (
    ("chat", "/v1/chat/completions"),
    ("responses", "/v1/responses"),
))
async def test_nonstream_repairs_once_and_commits_canonical(surface, path):
    adapter = _adapter()
    agent = MagicMock()
    agent._persist_disabled = True
    calls = 0

    async def run(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            kwargs["agent_ref"][0] = agent
            return _result("not json"), _usage()
        assert kwargs["format_only"] is True
        return _result('{"answer": 42}'), _usage()

    async with TestClient(TestServer(_app(adapter))) as client:
        with patch.object(adapter, "_run_agent", side_effect=run):
            response = await client.post(path, json=_payload(surface))
            body = await response.json()

    assert response.status == 200
    assert calls == 2
    assert "not json" not in json.dumps(body)
    agent._flush_messages_to_session_db.assert_called_once()
    committed = agent._flush_messages_to_session_db.call_args.args[0]
    assert committed[-1]["content"] == '{"answer": 42}'


@pytest.mark.asyncio
@pytest.mark.parametrize(("surface", "path"), (
    ("chat", "/v1/chat/completions"),
    ("responses", "/v1/responses"),
))
async def test_nonstream_provider_auth_failure_never_repairs(surface, path):
    adapter = _adapter()
    raw_error = "provider credential unavailable: api_key=raw-test-secret"

    with patch.object(
        adapter,
        "_create_agent",
        side_effect=_ProviderAuthResolutionError(raw_error),
    ) as create_agent:
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(path, json=_payload(surface))
            body = await response.json()

    assert response.status == 502
    assert body["error"]["code"] == "agent_error"
    assert raw_error not in json.dumps(body)
    assert create_agent.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("surface", "path"), (
    ("chat", "/v1/chat/completions"),
    ("responses", "/v1/responses"),
))
@pytest.mark.parametrize("failed_result", (
    {"completed": False},
    {"failed": True, "error": "private provider failure"},
))
async def test_nonstream_run_failure_is_agent_error(surface, path, failed_result):
    adapter = _adapter()

    async with TestClient(TestServer(_app(adapter))) as client:
        with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as run:
            run.return_value = (_result("private assistant body", **failed_result), _usage())
            response = await client.post(path, json=_payload(surface))
            body = await response.json()

    assert response.status == 502
    assert body["error"]["code"] == "agent_error"
    assert "private assistant body" not in json.dumps(body)
    assert run.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("surface", "path"), (
    ("chat", "/v1/chat/completions"),
    ("responses", "/v1/responses"),
))
async def test_nonstream_failure_keeps_effective_capability_without_assistant_history(
    surface, path
):
    adapter = _adapter()
    failed = _result(
        "private assistant body",
        failed=True,
        error="provider exploded",
        _structured_output_capable=True,
    )

    async with TestClient(TestServer(_app(adapter))) as client:
        with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as run:
            run.return_value = (failed, _usage())
            response = await client.post(path, json=_payload(surface))
            body = await response.json()

    assert response.status == 502
    assert body["hermes"]["output_contract"]["route_capable"] is True
    if surface == "responses":
        stored = adapter._response_store.get(body["id"])
        assert stored is not None
        assert [item["role"] for item in stored["conversation_history"]] == ["user"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("surface", "path"), (
    ("chat", "/v1/chat/completions"),
    ("responses", "/v1/responses"),
))
@pytest.mark.parametrize("repair_failure", (
    {"completed": False},
    {"_provider_auth_error": "Provider authentication failed"},
))
async def test_nonstream_repair_failure_is_agent_error(surface, path, repair_failure):
    adapter = _adapter()

    async def run(**kwargs):
        if kwargs.get("format_only"):
            return _result("private repair body", **repair_failure), _usage()
        return _result("not json"), _usage()

    async with TestClient(TestServer(_app(adapter))) as client:
        with patch.object(adapter, "_run_agent", side_effect=run) as run_mock:
            response = await client.post(path, json=_payload(surface))
            body = await response.json()

    assert response.status == 502
    assert body["error"]["code"] == "agent_error"
    assert "private repair body" not in json.dumps(body)
    assert run_mock.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("surface", "path"), (
    ("chat", "/v1/chat/completions"),
    ("responses", "/v1/responses"),
))
async def test_terminal_invalid_output_neither_leaks_nor_persists(surface, path):
    adapter = _adapter()
    agent = MagicMock()
    agent._persist_disabled = True

    async def run(**kwargs):
        if not kwargs.get("format_only"):
            kwargs["agent_ref"][0] = agent
        return _result("private invalid payload"), _usage()

    async with TestClient(TestServer(_app(adapter))) as client:
        with patch.object(adapter, "_run_agent", side_effect=run):
            response = await client.post(path, json=_payload(surface))
            body = await response.json()

    assert response.status == 502
    assert body["error"]["code"] == "structured_output_validation_failed"
    assert "private invalid payload" not in json.dumps(body)
    assert agent._persist_disabled is True
    agent._flush_messages_to_session_db.assert_not_called()
    agent._sync_external_memory_for_turn.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(("surface", "path"), (
    ("chat", "/v1/chat/completions"),
    ("responses", "/v1/responses"),
))
async def test_contracted_stream_releases_valid_text_and_tool_lifecycle(surface, path):
    adapter = _adapter()
    agent = MagicMock()
    agent._persist_disabled = True

    async def run(**kwargs):
        kwargs["agent_ref"][0] = agent
        kwargs["tool_start_callback"](
            "call_terminal_1", "terminal", {"command": "pwd"}
        )
        kwargs["tool_complete_callback"](
            "call_terminal_1", "terminal", {"command": "pwd"}, "ok"
        )
        await asyncio.sleep(0.05)
        kwargs["stream_delta_callback"]('```json\n{"answer": 42}\n```')
        return _result('```json\n{"answer": 42}\n```'), _usage()

    async with TestClient(TestServer(_app(adapter))) as client:
        with patch.object(adapter, "_run_agent", side_effect=run):
            # Chat-completions lifecycle events are opt-in (strict OpenAI
            # stream by default); the Responses surface emits spec-native
            # function_call items unconditionally and ignores the header.
            response = await client.post(
                path,
                json=_payload(surface, stream=True),
                headers={"X-Hermes-Tool-Progress": "1"},
            )
            body = await response.text()

    assert response.status == 200
    assert "call_terminal_1" in body
    assert ("running" if surface == "chat" else "in_progress") in body
    assert "completed" in body
    assert "not json" not in body
    assert "answer" in body and "42" in body
    assert agent._persist_disabled is False
    agent._flush_messages_to_session_db.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(("surface", "path"), (
    ("chat", "/v1/chat/completions"),
    ("responses", "/v1/responses"),
))
async def test_contracted_stream_withholds_invalid_text_and_persistence(surface, path):
    adapter = _adapter()
    agent = MagicMock()
    agent._persist_disabled = True

    async def run(**kwargs):
        kwargs["agent_ref"][0] = agent
        kwargs["stream_delta_callback"]("private invalid delta")
        return _result("private invalid delta"), _usage()

    async with TestClient(TestServer(_app(adapter))) as client:
        with patch.object(adapter, "_run_agent", side_effect=run):
            response = await client.post(path, json=_payload(surface, stream=True))
            body = await response.text()

    assert response.status == 200
    assert "private invalid delta" not in body
    assert "structured_output_validation_failed" in body
    assert agent._persist_disabled is True
    agent._flush_messages_to_session_db.assert_not_called()
    agent._sync_external_memory_for_turn.assert_not_called()


@pytest.mark.asyncio
async def test_responses_stream_provider_failure_is_not_schema_failure():
    adapter = _adapter()

    async def run(**kwargs):
        return _result(
            "private assistant body",
            failed=True,
            completed=False,
            error="provider exploded",
            _structured_output_capable=True,
        ), _usage()

    async with TestClient(TestServer(_app(adapter))) as client:
        with patch.object(adapter, "_run_agent", side_effect=run):
            response = await client.post(
                "/v1/responses", json=_payload("responses", stream=True)
            )
            body = await response.text()

    assert "event: response.failed" in body
    assert '"code": "agent_error"' in body
    assert "structured_output_validation_failed" not in body
    assert "private assistant body" not in body
    failed_event = next(
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ") and '"type": "response.failed"' in line
    )
    failed_response = failed_event["response"]
    assert failed_response["hermes"]["output_contract"]["route_capable"] is True
    stored = adapter._response_store.get(failed_response["id"])
    assert stored is not None
    assert [item["role"] for item in stored["conversation_history"]] == ["user"]


@pytest.mark.asyncio
async def test_responses_stream_unexpected_failure_persists_terminal_sidecar():
    adapter = _adapter()
    original_write = web.StreamResponse.write
    failed_once = False

    async def fail_one_terminal_item(response, data):
        nonlocal failed_once
        if not failed_once and b"event: response.output_text.done" in data:
            failed_once = True
            raise RuntimeError("event write failed")
        return await original_write(response, data)

    async def run(**kwargs):
        kwargs["stream_delta_callback"]("partial")
        return _result('{"answer": 42}'), _usage()

    async with TestClient(TestServer(_app(adapter))) as client:
        with (
            patch.object(adapter, "_run_agent", side_effect=run),
            patch.object(web.StreamResponse, "write", new=fail_one_terminal_item),
        ):
            response = await client.post(
                "/v1/responses", json=_payload("responses", stream=True)
            )
            body = await response.text()

    assert "event: hermes.sidecar" in body
    assert "event: response.failed" in body
    failed_event = next(
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ") and '"type": "response.failed"' in line
    )
    failed_response = failed_event["response"]
    assert failed_response["hermes"]["output_contract"]["mode"] == "json_object"
    stored = adapter._response_store.get(failed_response["id"])
    assert stored is not None
    assert stored["response"]["status"] == "failed"
    assert stored["response"]["hermes"] == failed_response["hermes"]
    assert [item["role"] for item in stored["conversation_history"]] == ["user"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("surface", "path"), (
    ("chat", "/v1/chat/completions"),
    ("responses", "/v1/responses"),
))
async def test_stream_provider_auth_failure_is_agent_error(surface, path):
    adapter = _adapter()

    with patch.object(
        adapter,
        "_create_agent",
        side_effect=_ProviderAuthResolutionError("provider credential unavailable"),
    ):
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(
                path, json=_payload(surface, stream=True)
            )
            body = await response.text()

    assert ('"error_code": "agent_error"' if surface == "chat" else '"code": "agent_error"') in body
    assert "structured_output_validation_failed" not in body
    assert "provider credential unavailable" not in body
    if surface == "responses":
        assert "event: response.failed" in body
        assert "event: response.output_text.delta" not in body


@pytest.mark.asyncio
async def test_sidecar_continuation_vocabulary_at_both_boundaries():
    adapter = _adapter(api_key="secret")
    auth = {"Authorization": "Bearer secret"}
    async with TestClient(TestServer(_app(adapter))) as client:
        with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as run:
            run.return_value = (_result('{"answer": 42}', session_id="session-1"), _usage())
            chat = await client.post(
                "/v1/chat/completions",
                json=_payload("chat"),
                headers={**auth, "X-Hermes-Session-Id": "session-1"},
            )
            chat_body = await chat.json()
            first = await client.post(
                "/v1/responses", json=_payload("responses"), headers=auth
            )
            first_body = await first.json()
            follow_payload = _payload("responses")
            follow_payload["previous_response_id"] = first_body["id"]
            follow = await client.post(
                "/v1/responses", json=follow_payload, headers=auth
            )
            follow_body = await follow.json()

    assert chat_body["hermes"]["context"]["continuation"] == "session"
    assert first_body["hermes"]["context"]["continuation"] == "new"
    assert follow_body["hermes"]["context"]["continuation"] == "previous_response_id"


def _patch_runtime(monkeypatch, *, api_mode: str):
    class FakeAgent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.api_mode = kwargs.get("api_mode")
            self.request_overrides = {}

    monkeypatch.setattr("run_agent.AIAgent", FakeAgent)
    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "global-provider",
            "api_key": "global-key",
            "base_url": "https://global.example/v1",
            "api_mode": api_mode,
        },
    )
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "global/model")
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})
    monkeypatch.setattr(
        "gateway.run.GatewayRunner._load_reasoning_config",
        staticmethod(lambda model="": {}),
    )
    monkeypatch.setattr(
        "gateway.run.GatewayRunner._load_fallback_model", staticmethod(lambda: None)
    )
    monkeypatch.setattr("gateway.run._current_max_iterations", lambda: 90)
    monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", lambda *_: set())
    monkeypatch.setattr(
        "gateway.platforms.api_server._resolve_request_runtime_agent_kwargs",
        lambda provider, target_model=None: {
            "provider": provider,
            "api_key": "session-key",
            "base_url": "https://session.example/v1",
            "api_mode": api_mode,
        },
    )
    monkeypatch.setattr(
        "agent.models_dev.get_model_info",
        lambda *args, **kwargs: SimpleNamespace(structured_output=True),
    )


@pytest.mark.parametrize(("api_mode", "wire_key"), (
    ("chat_completions", "response_format"),
    ("codex_responses", "text"),
))
def test_session_override_controls_effective_provider_wire_shape(
    monkeypatch, api_mode, wire_key
):
    _patch_runtime(monkeypatch, api_mode=api_mode)
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_ensure_session_db", lambda: None)
    monkeypatch.setattr(
        adapter,
        "_session_model_override_for",
        lambda key: {
            "model": "session/model",
            "provider": "session-provider",
            "api_mode": api_mode,
        },
    )
    contract = _structured_output_contract(
        {"type": "json_object"}, responses=False
    )

    agent = adapter._create_agent(
        session_id="session-1",
        route={"model": "ignored/route", "provider": "ignored-provider"},
        output_contract=contract,
        defer_persistence=True,
    )

    assert agent.model == "session/model"
    assert agent.provider == "session-provider"
    assert agent._hermes_api_runtime["route_source"] == "session_model_override"
    assert wire_key in agent.request_overrides
    assert agent._persist_disabled is True


def test_unsupported_effective_mode_fails_closed(monkeypatch):
    _patch_runtime(monkeypatch, api_mode="unsupported_mode")
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_ensure_session_db", lambda: None)
    contract = _structured_output_contract(
        {"type": "json_object"}, responses=False
    )

    with pytest.raises(StructuredOutputRequestError, match="not supported"):
        adapter._create_agent(output_contract=contract)


def test_real_runtime_config_propagates_contract_to_agent(
    tmp_path, monkeypatch
):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "model:\n  provider: openrouter\n  default: openai/gpt-4o-mini\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    from gateway import run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    adapter = _adapter()
    contract = _structured_output_contract(
        {"type": "json_object"}, responses=False
    )

    agent = adapter._create_agent(
        output_contract=contract,
        defer_persistence=True,
    )

    assert agent.provider == "openrouter"
    assert agent.model == "openai/gpt-4o-mini"
    assert agent.request_overrides["response_format"] == {"type": "json_object"}
    assert agent._persist_disabled is True


@pytest.mark.asyncio
@pytest.mark.parametrize(("surface", "path"), (
    ("chat", "/v1/chat/completions"),
    ("responses", "/v1/responses"),
))
async def test_nonstream_provider_auth_from_isolated_home_never_repairs(
    surface, path, tmp_path, monkeypatch, caplog
):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "model:\n  provider: openrouter\n  default: test-model\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    adapter = _adapter()

    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(path, json=_payload(surface))
        body = await response.json()

    assert response.status == 502
    assert body["error"]["code"] == "agent_error"
    assert "structured_output_validation_failed" not in json.dumps(body)
    assert sum(
        "Provider authentication failed for session=" in record.getMessage()
        for record in caplog.records
    ) == 1
