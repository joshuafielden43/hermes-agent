"""Opt-in caller-owned tool inference. Never invokes the Hermes agent loop."""

import asyncio
import json
import re
import time
import uuid
from typing import Any

from aiohttp import web


def valid_request(body):
    """A deliberately text/function-only wire contract; no SDK escape hatches."""
    fields = {
        "model",
        "messages",
        "tools",
        "tool_choice",
        "stream",
        "stream_options",
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "stop",
        "parallel_tool_calls",
    }
    if (
        not isinstance(body, dict)
        or set(body) - fields
        or body.get("model") != "hermes-inference"
    ):
        return False
    if type(body.get("stream", False)) is not bool:
        return False
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    for msg in messages:
        if not isinstance(msg, dict) or set(msg) - {
            "role",
            "content",
            "tool_calls",
            "tool_call_id",
            "name",
        }:
            return False
        if msg.get("role") not in {"system", "developer", "user", "assistant", "tool"}:
            return False
        if not isinstance(msg.get("content"), str) and not (
            msg.get("role") == "assistant"
            and msg.get("tool_calls")
            and msg.get("content") is None
        ):
            return False
        if "tool_calls" in msg:
            if msg["role"] != "assistant" or not isinstance(msg["tool_calls"], list):
                return False
            for call in msg["tool_calls"]:
                if not isinstance(call, dict) or set(call) != {
                    "id",
                    "type",
                    "function",
                }:
                    return False
                fn = call.get("function")
                if (
                    call["type"] != "function"
                    or not isinstance(call["id"], str)
                    or not isinstance(fn, dict)
                    or set(fn) != {"name", "arguments"}
                    or not all(isinstance(fn[k], str) for k in fn)
                ):
                    return False
        if msg["role"] == "tool" and not isinstance(msg.get("tool_call_id"), str):
            return False
    tools = body.get("tools", [])
    if not isinstance(tools, list):
        return False
    names = set()
    for tool in tools:
        if (
            not isinstance(tool, dict)
            or set(tool) != {"type", "function"}
            or tool["type"] != "function"
        ):
            return False
        fn = tool["function"]
        if not isinstance(fn, dict) or set(fn) - {
            "name",
            "description",
            "parameters",
            "strict",
        }:
            return False
        name = fn.get("name")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
            or name in names
        ):
            return False
        names.add(name)
    choice = body.get("tool_choice", "auto")
    if isinstance(choice, str):
        return choice in {"none", "auto", "required"} and (
            choice != "required" or bool(tools)
        )
    return (
        isinstance(choice, dict)
        and set(choice) == {"type", "function"}
        and choice["type"] == "function"
        and isinstance(choice["function"], dict)
        and set(choice["function"]) == {"name"}
        and choice["function"]["name"] in names
    )


def error(code, status):
    # Do not return provider exception strings, URLs, credentials or headers.
    return web.json_response({"error": {"code": code, "message": code}}, status=status)


async def stream_result(request, chunks):
    response = web.StreamResponse(
        headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store"}
    )
    await response.prepare(request)
    try:
        finished = False
        async for payload in chunks:
            finished |= any(
                choice.get("finish_reason") for choice in payload.get("choices", [])
            )
            await response.write(f"data: {json.dumps(payload)}\n\n".encode())
        if not finished:
            raise ValueError("Incomplete provider stream")
        await response.write(b"data: [DONE]\n\n")
    except (ConnectionError, asyncio.CancelledError):
        raise
    except Exception:
        await response.write(
            b'data: {"error":{"code":"inference_provider_failed"}}\n\n'
        )
    return response


async def chat_chunks(result):
    async for chunk in result:
        yield public_chat_payload(chunk, streaming=True)


def public_chat_payload(result, *, streaming=False):
    """Expose only the round-trippable caller contract, never provider extras."""
    raw = result.model_dump(exclude_none=True)
    payload = {key: raw[key] for key in ("id", "created", "object")}
    payload["model"] = "hermes-inference"
    field = "delta" if streaming else "message"
    choices = []
    for choice in raw["choices"]:
        source = choice[field]
        message = {key: source[key] for key in ("role", "content") if key in source}
        if "tool_calls" in source:
            message["tool_calls"] = [
                {
                    **{
                        key: call[key] for key in ("index", "id", "type") if key in call
                    },
                    "function": {
                        key: call["function"][key]
                        for key in ("name", "arguments")
                        if key in call.get("function", {})
                    },
                }
                for call in source["tool_calls"]
            ]
        choices.append({
            "index": choice["index"],
            field: message,
            "finish_reason": choice.get("finish_reason"),
        })
    payload["choices"] = choices
    if raw.get("usage"):
        payload["usage"] = {
            key: raw["usage"][key]
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if key in raw["usage"]
        }
    return payload


def responses_request(body, model):
    from agent.codex_responses_adapter import _chat_messages_to_responses_input

    items = []
    for message in body["messages"]:
        if message["role"] in {"system", "developer"}:
            items.append({"role": message["role"], "content": message["content"]})
        else:
            items.extend(_chat_messages_to_responses_input([message]))
    payload = {
        "model": model,
        "input": items,
        "instructions": "",
        "store": False,
        "stream": True,
    }
    if "tools" in body:
        payload["tools"] = [
            {"type": "function", **tool["function"]} for tool in body["tools"]
        ]
    if "tool_choice" in body:
        choice = body["tool_choice"]
        payload["tool_choice"] = (
            {"type": "function", "name": choice["function"]["name"]}
            if isinstance(choice, dict)
            else choice
        )
    if "parallel_tool_calls" in body:
        payload["parallel_tool_calls"] = body["parallel_tool_calls"]
    return payload


async def responses_chunks(result):
    """Translate only caller function/text events, never provider built-in tools."""
    envelope = {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "created": int(time.time()),
        "object": "chat.completion.chunk",
        "model": "hermes-inference",
    }
    calls = {}
    completed = False
    async for event in result:
        event = event.model_dump(exclude_none=True)
        kind = event.get("type")
        delta, finish, usage = {}, None, None
        if kind == "response.output_item.added":
            item = event["item"]
            if item["type"] == "function_call":
                index = len(calls)
                calls[event["output_index"]] = index
                delta = {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": item["call_id"],
                            "type": "function",
                            "function": {
                                "name": item["name"],
                                "arguments": item.get("arguments", ""),
                            },
                        }
                    ]
                }
            elif item["type"] not in {"message", "reasoning"}:
                raise ValueError("Provider-executed tools are not supported")
            else:
                continue
        elif kind == "response.function_call_arguments.delta":
            delta = {
                "tool_calls": [
                    {
                        "index": calls[event["output_index"]],
                        "function": {"arguments": event["delta"]},
                    }
                ]
            }
        elif kind == "response.output_text.delta":
            delta = {"content": event["delta"]}
        elif kind in {"error", "response.failed"}:
            raise ValueError("Provider failed")
        elif kind in {"response.completed", "response.incomplete"}:
            response = event["response"]
            if response.get("status") not in {"completed", "incomplete"}:
                raise ValueError("Provider failed")
            finish = (
                "length"
                if kind == "response.incomplete"
                else "tool_calls"
                if calls
                else "stop"
            )
            raw_usage = response.get("usage")
            if raw_usage:
                usage = {
                    "prompt_tokens": raw_usage["input_tokens"],
                    "completion_tokens": raw_usage["output_tokens"],
                    "total_tokens": raw_usage["total_tokens"],
                }
            completed = True
        else:
            continue
        chunk: dict[str, Any] = {
            **envelope,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if usage:
            chunk["usage"] = usage
        yield chunk
        if completed:
            return
    if not completed:
        raise ValueError("Incomplete provider stream")


async def collect_response(chunks):
    text, calls, usage, last = [], {}, None, None
    async for chunk in chunks:
        last = chunk
        usage = chunk.get("usage", usage)
        for choice in chunk["choices"]:
            delta = choice["delta"]
            if delta.get("content"):
                text.append(delta["content"])
            for call in delta.get("tool_calls", []):
                index = call["index"]
                if index not in calls:
                    calls[index] = {
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["function"]["name"], "arguments": ""},
                    }
                calls[index]["function"]["arguments"] += call["function"].get(
                    "arguments", ""
                )
    if last is None:
        raise ValueError("Empty provider response")
    message = {"role": "assistant", "content": "".join(text) or None}
    if calls:
        message["tool_calls"] = list(calls.values())
    result = {
        **last,
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": last["choices"][0]["finish_reason"],
            }
        ],
    }
    if usage:
        result["usage"] = usage
    return result


async def handle_inference(request):
    from hermes_cli.config import load_config
    from hermes_cli.runtime_provider import (
        is_routable_provider,
        resolve_runtime_provider,
    )
    from openai import AsyncOpenAI
    import httpx

    try:
        config = load_config()["gateway"]["api_server"].get("caller_inference", {})
        provider, model = config.get("provider"), config.get("model")
        if (
            config.get("enabled") is not True
            or not isinstance(provider, str)
            or provider.strip().lower() in {"", "auto", "custom", "moa"}
            or not isinstance(model, str)
            or not model.strip()
            or type(config.get("context_length")) is not int
            or config["context_length"] <= 0
            or not is_routable_provider(provider)
        ):
            return error("inference_unconfigured", 503)
    except (AttributeError, KeyError, TypeError, ValueError):
        return error("inference_unconfigured", 503)
    if request.method == "GET":
        return web.json_response({
            "object": "list",
            "data": [
                {
                    "id": "hermes-inference",
                    "object": "model",
                    "owned_by": "hermes",
                    "context_length": config["context_length"],
                    "tool_execution": "caller",
                }
            ],
        })
    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        return error("invalid_request", 400)
    try:
        valid = valid_request(body)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        return error("invalid_request", 400)
    try:
        runtime = await asyncio.to_thread(
            resolve_runtime_provider, requested=provider, target_model=model
        )
        if not runtime.get("api_key") or not runtime.get("base_url"):
            return error("inference_unconfigured", 503)
        # The existing resolver has legacy alias/fallback behavior. This lane
        # accepts only exact provider IDs (or explicitly named custom routes).
        expected = (
            "custom"
            if provider.strip().lower().startswith("custom:")
            else provider.strip().lower()
        )
        if runtime.get("provider") != expected:
            return error("inference_unconfigured", 503)
    except Exception:
        return error("inference_unconfigured", 503)
    mode = runtime.get("api_mode")
    if mode not in {"chat_completions", "codex_responses"}:
        return error("unsupported_inference_transport", 503)
    # Codex does not accept these options. Reject instead of silently ignoring them.
    if mode == "codex_responses" and set(body) & {
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "stop",
    }:
        return error("unsupported_inference_option", 400)
    from agent.codex_headers import apply_required_codex_headers

    client_options = {}
    apply_required_codex_headers(
        client_options, access_token=runtime["api_key"], base_url=runtime["base_url"]
    )
    try:
        async with AsyncOpenAI(
            api_key=runtime["api_key"],
            base_url=runtime["base_url"],
            max_retries=0,
            timeout=120,
            http_client=httpx.AsyncClient(trust_env=False, follow_redirects=False),
            **client_options,
        ) as client:
            if mode == "codex_responses":
                result = await client.responses.create(**responses_request(body, model))
                try:
                    chunks = responses_chunks(result)
                    if body.get("stream"):
                        return await stream_result(request, chunks)
                    return web.json_response(await collect_response(chunks))
                finally:
                    await result.close()
            result = await client.chat.completions.create(**{**body, "model": model})
            if body.get("stream"):
                try:
                    return await stream_result(request, chat_chunks(result))
                finally:
                    await result.close()
            return web.json_response(public_chat_payload(result))
    except Exception:
        return error("inference_provider_failed", 502)
