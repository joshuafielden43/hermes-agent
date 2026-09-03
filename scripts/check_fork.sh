#!/usr/bin/env bash
# House Python CI adapted to the fork's bounded collar surface. No deployment.
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/ruff check \
  gateway/caller_inference.py gateway/platforms/api_server.py \
  hermes_cli/config_defaults.py tests/gateway/test_caller_inference.py \
  scripts/fork_publish.py tests/scripts/test_fork_publish.py
.venv/bin/ty check gateway/caller_inference.py tests/gateway/test_caller_inference.py \
  scripts/fork_publish.py tests/scripts/test_fork_publish.py
scripts/run_tests.sh \
  tests/scripts/test_fork_publish.py \
  tests/gateway/test_api_server.py \
  tests/gateway/test_structured_output_contract.py \
  tests/gateway/test_caller_inference.py \
  tests/agent/test_chat_completion_helpers_provider_sort.py \
  tests/agent/test_models_dev.py \
  tests/agent/test_prompt_builder.py \
  tests/hermes_cli/test_models_dev_preferred_merge.py \
  tests/run_agent/test_anthropic_prompt_cache_policy.py \
  tests/run_agent/test_run_agent_codex_responses.py \
  tests/run_agent/test_session_activity_persist.py \
  -q --disable-warnings -j 2
