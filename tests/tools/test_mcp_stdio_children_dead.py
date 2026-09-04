"""Regression tests for MCP stdio aggregate liveness (#94335 / #94637).

The #81995 fast-fail gate consumes ``_stdio_children_dead`` as a boolean
state machine: True means every tracked child is gone; False means at least
one child is alive or liveness is unknown. The live-child branch was inverted,
so healthy stdio RPCs were cancelled while their subprocesses were still alive.

Watcher-consumer cases are distilled from #94521. Dependency/probe fail-open
cases are distilled from #94661 into the canonical #94339 carrier. The
leaked-probe cases at the bottom cover #95938: the consumer in
``_make_tool_handler`` must create the watcher coroutine exactly once.
"""

import asyncio
import builtins
import gc
import json
import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tools.mcp_tool as mcp
from tools.mcp_tool import MCPServerTask


@pytest.fixture(autouse=True)
def _reset_mcp_state():
    old_servers = dict(mcp._servers)
    old_counts = dict(mcp._server_error_counts)
    old_opened = dict(mcp._server_breaker_opened_at)
    yield
    mcp._servers.clear()
    mcp._servers.update(old_servers)
    mcp._server_error_counts.clear()
    mcp._server_error_counts.update(old_counts)
    mcp._server_breaker_opened_at.clear()
    mcp._server_breaker_opened_at.update(old_opened)


def _task_with_pids(pids, *, http=False):
    task = object.__new__(MCPServerTask)
    task._stdio_child_pids = pids
    task._config = {"url": "http://example.invalid"} if http else {"command": "x"}
    return task


def test_live_child_reports_not_dead():
    """The reported bug: an alive tracked pid must NOT report all-dead."""
    with patch("psutil.pid_exists", return_value=True):
        assert _task_with_pids([60634])._stdio_children_dead() is False


def test_all_children_dead_reports_dead():
    with patch("psutil.pid_exists", return_value=False):
        assert _task_with_pids([111, 222])._stdio_children_dead() is True


def test_mixed_liveness_reports_not_dead():
    """One live sibling is enough — dead others must not flip the verdict."""
    with patch("psutil.pid_exists", side_effect=lambda pid: pid != 111):
        assert _task_with_pids([111, 222])._stdio_children_dead() is False


def test_no_captured_pids_stays_fail_open():
    """Unknown (no tracked pids / HTTP transport) must not fail fast."""
    assert _task_with_pids([])._stdio_children_dead() is False
    assert _task_with_pids([1], http=True)._stdio_children_dead() is False


def test_psutil_unavailable_stays_fail_open():
    """Missing probe support is unknown, never proof of child death."""
    real_import = builtins.__import__

    def _without_psutil(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("psutil unavailable")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=_without_psutil):
        assert _task_with_pids([1])._stdio_children_dead() is False


def test_pid_probe_error_stays_fail_open():
    """A failed probe cannot authorize the destructive fast-fail."""
    with patch("psutil.pid_exists", side_effect=OSError("probe failed")):
        assert _task_with_pids([1])._stdio_children_dead() is False


def test_watcher_does_not_resolve_while_a_child_is_alive():
    """The watcher must not cancel an RPC while any child is still live."""

    async def _run():
        with patch("psutil.pid_exists", return_value=True):
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    _task_with_pids([60634])._watch_stdio_children(),
                    timeout=0.05,
                )

    asyncio.run(_run())


def test_watcher_resolves_when_all_children_are_dead():
    """The watcher completes only when the aggregate verdict is all-dead."""

    async def _run():
        with patch("psutil.pid_exists", return_value=False):
            await asyncio.wait_for(
                _task_with_pids([111, 222])._watch_stdio_children(),
                timeout=0.1,
            )

    asyncio.run(_run())


def _connected_server_with_watcher(pids, *, async_session=True):
    """A connected-server double carrying the real #81995 watcher methods."""
    task = _task_with_pids(pids)
    result = SimpleNamespace(isError=False, content=[], structuredContent=None)
    mock_session = MagicMock()
    if async_session:
        mock_session.call_tool = AsyncMock(return_value=result)
    else:
        mock_session.call_tool = MagicMock(return_value=result)
    server = SimpleNamespace(
        session=mock_session,
        _rpc_lock=MagicMock(),
        _pending_call_context=None,
        _stdio_children_dead=task._stdio_children_dead,
        _watch_stdio_children=task._watch_stdio_children,
    )
    server._rpc_lock.__aenter__ = AsyncMock(return_value=None)
    server._rpc_lock.__aexit__ = AsyncMock(return_value=None)
    return server


def _run_on_loop(coro_or_factory, timeout=120):
    coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _unawaited_coroutine_warnings(caught):
    return [
        w for w in caught
        if issubclass(w.category, RuntimeWarning) and "never awaited" in str(w.message)
    ]


def test_tool_call_racing_watcher_leaks_no_coroutine():
    """#95938: the consumer probed ``_watch_stdio_children()`` to test
    awaitability, discarded that coroutine, then created a second one for the
    race — the probe leaked as "coroutine was never awaited" on every real
    MCP tool call. The watcher awaitable must be created exactly once."""
    mcp._servers["playwright"] = _connected_server_with_watcher([60634])
    handler = mcp._make_tool_handler("playwright", "browser_navigate", 5)
    with patch("psutil.pid_exists", return_value=True), \
         patch.object(mcp, "_run_on_mcp_loop", side_effect=_run_on_loop), \
         warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = handler({}, task_id="t1")
        gc.collect()

    payload = json.loads(out)
    assert "error" not in payload
    assert payload.get("result") == ""
    assert _unawaited_coroutine_warnings(caught) == []


def test_tool_call_with_stub_session_closes_watcher():
    """#95938 follow-through: when ``call_tool`` returns a non-coroutine stub
    the race branch is skipped — the already-created watcher coroutine must be
    closed, not dropped."""
    mcp._servers["playwright"] = _connected_server_with_watcher(
        [60634], async_session=False
    )
    handler = mcp._make_tool_handler("playwright", "browser_navigate", 5)
    with patch("psutil.pid_exists", return_value=True), \
         patch.object(mcp, "_run_on_mcp_loop", side_effect=_run_on_loop), \
         warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = handler({}, task_id="t1")
        gc.collect()

    payload = json.loads(out)
    assert "error" not in payload
    assert payload.get("result") == ""
    assert _unawaited_coroutine_warnings(caught) == []
