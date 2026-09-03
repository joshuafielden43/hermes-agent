# Caller-owned tool inference (fork only)

Status: local candidate; not enabled or deployed. Work: Vikunja 2082.

## Contract

The existing `/v1/chat/completions` and `/v1/responses` remain server-agent
endpoints. The separate `/v1/inference/chat/completions` performs one provider
inference request. Hermes returns function calls and never executes them.
The caller owns tool execution and sends the assistant tool calls plus
`role: tool` results on its next request. No session is created for this lane.

Both inference routes require the profile's API-server bearer key, even when
the listener is manually constructed without a key. Provider credentials stay
on Hermes. Caller-supplied providers, URLs, headers, credentials, SDK escape
hatches, built-in tools, and structured-output options are rejected.
No fallback, provider retry, or HTTP redirect is allowed. Existing shared
admission/drain accounting bounds concurrent inference requests.

## Server configuration

Configure `gateway.api_server.caller_inference` in the profile's `config.yaml`:

```yaml
gateway:
  api_server:
    caller_inference:
      enabled: true
      provider: openai-codex  # exact provider ID, or custom:<configured-name>
      model: YOUR_VERIFIED_MODEL
      context_length: 32000  # example only: set the verified provider limit
```

Disabled by default. Empty/malformed settings fail closed. Provider credentials
are resolved through Hermes' existing server-side configuration. Automatic
provider selection, bare `custom`, and virtual agent providers are not supported.
The context limit is an operator declaration, not a claim that this endpoint
token-counts requests. It must be verified before deployment.

Clients use base URL `http://HOST:8642/v1/inference`, model `hermes-inference`,
and the API-server key. Authenticated `GET /v1/inference/models` advertises that
alias, the declared context length, and `tool_execution: caller`. Discovery
does not contact the provider and is not a credential-health check. The old
`/v1/models` is unchanged.

## Supported surface

- Text messages with system, developer, user, assistant, and tool roles.
- Function schemas, tool choice, assistant tool calls and tool results.
- Nonstreaming and SSE responses, including split function arguments.
- Chat Completions and Responses provider transports. Responses requests use
  `store: false`; transport events are converted to Chat Completions chunks.
- Only declared functions are sent; no Hermes tools or memories are injected.
- Errors are generic. A failed/truncated stream emits an error, not `[DONE]`.

This is not a full OpenAI proxy. Multimodal content, arbitrary extra fields,
structured output, and provider-native tools are unsupported. Responses-mode
sampling/token-limit options are rejected because Codex does not accept them;
they are not silently discarded. Caller cancellation closes the upstream stream
when observed; provider reads have a 120-second timeout. The caller should also
set a total job deadline. No provider request/response bodies are logged here.

## Verification boundary

`tests/gateway/test_caller_inference.py` uses real HTTP listeners and temporary
Hermes configuration, with a loopback provider standing in for the external
network. It verifies auth/config gates, model discovery, tool/result replay,
streamed call IDs/names/arguments, forbidden overrides, provider errors,
redirect refusal, and concurrency. Existing API/structured-output tests remain
the regression gate. No private collaborator mocks or live provider keys.

Live acceptance still requires an exact-SHA build/deployment, selected model
and context-limit verification, authenticated text/tool/stream probes against
the deployed service, then one bounded CRM task. Local tests are not live proof.

### Local validation, 2026-09-03

The bounded 10-module collar gate passed 498 tests with one skip, including
35 caller-inference HTTP cases. Ruff passed all four touched Python files;
`ty check` passed the new module and its tests. The canonical test wrapper's
collection-only run exits nonzero because it expects executed tests; the
actual test execution above passed. No repository-wide suite was run.

### Standards review

Zero open findings after adding keyless-listener and broken-stream HTTP tests.

### Spec review

Zero unresolved findings. Review caught provider-private metadata in Chat
responses breaking tool-result replay. Output projection now removes those
extras from messages and deltas; regression tests cover removal and replay.
