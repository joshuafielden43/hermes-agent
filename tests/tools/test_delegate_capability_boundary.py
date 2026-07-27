"""One hostile-leaf regression for #1612 capability boundaries."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools.delegate_tool import _build_child_agent
from tools.file_tools import patch_tool, write_file_tool
from tools.path_security import clear_task_write_roots, set_task_write_roots


def _parent(tmp_path: Path) -> MagicMock:
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "test-key"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "test-model"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent.provider_require_parameters = False
    parent.provider_data_collection = ""
    parent.openrouter_min_coding_score = None
    parent.request_overrides = {}
    parent.reasoning_config = None
    parent.prefill_messages = None
    parent.max_tokens = None
    parent._session_db = None
    parent.session_id = "parent-session"
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    parent._fallback_chain = []
    parent._client_kwargs = {"api_key": "test-key"}
    parent.enabled_toolsets = ["hermes-cli"]
    parent.disabled_toolsets = None
    parent.valid_tool_names = {
        "web_search",
        "web_extract",
        "x_search",
        "read_file",
        "search_files",
        "write_file",
        "patch",
        "terminal",
        "process",
        "execute_code",
        "send_message",
        "delegate_task",
        "tool_call",
    }
    parent.cwd = str(tmp_path)
    parent.terminal_cwd = str(tmp_path)
    parent._write_roots = None
    parent._subdirectory_hints = None
    return parent


def test_hostile_leaf_gets_only_selected_tools_and_staging_writes(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    escape = tmp_path / "escape.txt"
    escape.write_text("safe\n", encoding="utf-8")
    parent = _parent(tmp_path)
    parent_tools_before = set(parent.valid_tool_names)

    requested = [
        "web_search",
        "web_extract",
        "x_search",
        "read_file",
        "search_files",
        "write_file",
        "patch",
        "terminal",
        "process",
        "execute_code",
        "send_message",
        "delegate_task",
        "tool_call",
    ]

    def make_child(**kwargs):
        child = MagicMock()
        child.session_id = "child-session"
        child.enabled_toolsets = kwargs.get("enabled_toolsets")
        child.disabled_toolsets = kwargs.get("disabled_toolsets")
        # Start with the full parent surface so _apply_exact_tool_filter can narrow.
        names = sorted(parent.valid_tool_names)
        child.tools = [{"type": "function", "function": {"name": n}} for n in names]
        child.valid_tool_names = set(names)
        child._delegate_depth = 1
        child._subagent_id = "sa-0-hostile"
        child.interrupt = MagicMock()
        child.get_activity_summary = MagicMock(return_value={"current_tool": None})
        return child

    with patch("run_agent.AIAgent", side_effect=lambda **kw: make_child(**kw)):
        with patch("tools.delegate_tool._load_config", return_value={}):
            child = _build_child_agent(
                task_index=0,
                goal="stage three files then escape",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=5,
                task_count=1,
                parent_agent=parent,
                role="leaf",
                enabled_tools=requested,
                write_roots=[str(staging)],
            )

    enabled = set(child.valid_tool_names)
    assert "write_file" in enabled
    assert "patch" in enabled
    assert "web_search" in enabled
    assert "read_file" in enabled
    assert "terminal" not in enabled
    assert "process" not in enabled
    assert "execute_code" not in enabled
    assert "send_message" not in enabled
    assert "delegate_task" not in enabled
    assert "tool_call" not in enabled
    assert child._write_roots == [str(staging.resolve())]
    assert parent.valid_tool_names == parent_tools_before

    task_id = "hostile-leaf"
    set_task_write_roots(task_id, child._write_roots)
    try:
        for name, body in (
            ("report.md", "# ok\n"),
            ("sources.json", "{}\n"),
            ("leaf-done.json", '{"ok": true}\n'),
        ):
            out = json.loads(write_file_tool(str(staging / name), body, task_id=task_id))
            assert "error" not in out, out

        denied_write = json.loads(
            write_file_tool(str(escape), "owned\n", task_id=task_id)
        )
        denied_patch = json.loads(
            patch_tool(
                mode="replace",
                path=str(escape),
                old_string="safe",
                new_string="owned",
                task_id=task_id,
            )
        )
        assert "error" in denied_write
        assert "write root" in denied_write["error"].lower() or "outside" in denied_write["error"].lower()
        assert "error" in denied_patch
        assert escape.read_text(encoding="utf-8") == "safe\n"
    finally:
        clear_task_write_roots(task_id)

    parent_write = json.loads(
        write_file_tool(str(escape), "parent-ok\n", task_id="parent-task")
    )
    assert "error" not in parent_write
    assert escape.read_text(encoding="utf-8") == "parent-ok\n"
