"""Tests for the read-only Ledger MCP server wiring.

The whole module skips cleanly when the optional `mcp` SDK is absent, so the
core suite stays green without it; CI installs `metaensemble[test]` (which pins
`mcp`) and runs these for real. No test opens a real stdio transport — the
server object is inspected in-process instead.
"""
from __future__ import annotations

import sys

import pytest

pytest.importorskip("mcp")  # skip the module when the SDK is not installed

from metaensemble.mcp import queries, server  # noqa: E402


# The exact tool surface the server must expose — one per frozen query.
EXPECTED_TOOL_NAMES = {
    "recent_runs",
    "runs_by_executor",
    "runs_by_task",
    "active_executors",
    "executor_detail",
    "outcome_counts",
    "top_executors",
    "window_burn",
    "ledger_stats",
}

# Minimal keyword arguments needed to call each tool (some take a required
# positional). Everything else defaults.
MINIMAL_TOOL_ARGS: dict[str, dict] = {
    "recent_runs": {},
    "runs_by_executor": {"alias_or_id": "arch-7b3"},
    "runs_by_task": {"task_id": "task-1"},
    "active_executors": {},
    "executor_detail": {"alias_or_id": "arch-7b3"},
    "outcome_counts": {},
    "top_executors": {},
    "window_burn": {},
    "ledger_stats": {},
}


def _tool_map(srv) -> dict:
    """Map tool name -> underlying callable for the tools on `srv`.

    Reaches into FastMCP's tool manager; isolated in one place so an SDK
    internal-layout change is a single-line fix rather than a test rewrite.
    """
    return {tool.name: tool.fn for tool in srv._tool_manager.list_tools()}


def test_build_server_registers_expected_tools():
    srv = server.build_server()
    assert set(_tool_map(srv)) == EXPECTED_TOOL_NAMES


def test_each_registered_tool_delegates_to_queries(monkeypatch):
    """Every registered tool must return exactly what its queries.* function
    returns — proving it delegates and never computes its own read."""
    srv = server.build_server()
    tools = _tool_map(srv)

    sentinel = object()
    for name in EXPECTED_TOOL_NAMES:
        monkeypatch.setattr(queries, name, lambda *a, **k: sentinel, raising=True)

    for name in EXPECTED_TOOL_NAMES:
        result = tools[name](**MINIMAL_TOOL_ARGS[name])
        assert result is sentinel, f"tool {name!r} did not delegate to queries.{name}"


def test_recent_runs_tool_forwards_arguments(monkeypatch):
    srv = server.build_server()
    tools = _tool_map(srv)
    captured: dict = {}

    def _spy(limit=20, since_iso=None):
        captured.update(limit=limit, since_iso=since_iso)
        return []

    monkeypatch.setattr(queries, "recent_runs", _spy, raising=True)
    tools["recent_runs"](limit=7, since_iso="2026-01-01T00:00:00+00:00")
    assert captured == {"limit": 7, "since_iso": "2026-01-01T00:00:00+00:00"}


def test_window_burn_tool_forwards_window_id(monkeypatch):
    srv = server.build_server()
    tools = _tool_map(srv)
    captured: dict = {}

    def _spy(window_id=None):
        captured["window_id"] = window_id
        return {"scope": "dispatched-Run tokens recorded in this project's Ledger"}

    monkeypatch.setattr(queries, "window_burn", _spy, raising=True)
    tools["window_burn"](window_id="2026-07-01T00")
    assert captured["window_id"] == "2026-07-01T00"


def test_ledger_stats_resource_registered():
    """The optional read-only resource is exposed at a ledger:// URI without
    over-coupling to the SDK's resource object shape."""
    srv = server.build_server()
    uris = [str(resource.uri) for resource in srv._resource_manager.list_resources()]
    assert any("ledger" in uri and "stats" in uri for uri in uris)


def _fake_absent_mcp(monkeypatch) -> None:
    """Make `from mcp.server.fastmcp import FastMCP` raise ImportError.

    Setting the leaf module to None in sys.modules forces the import machinery
    to raise even when the real SDK is installed and cached, so the crisp-error
    guard can be exercised in an environment that has `mcp`.
    """
    for name in ("mcp", "mcp.server", "mcp.server.fastmcp"):
        monkeypatch.setitem(sys.modules, name, None)


def test_build_server_raises_actionable_error_without_sdk(monkeypatch):
    _fake_absent_mcp(monkeypatch)
    with pytest.raises(ImportError) as excinfo:
        server.build_server()
    assert "metaensemble[mcp]" in str(excinfo.value)


def test_run_stdio_returns_nonzero_and_prints_hint_without_sdk(monkeypatch, capsys):
    _fake_absent_mcp(monkeypatch)
    rc = server.run_stdio()
    assert rc == 1
    assert "metaensemble[mcp]" in capsys.readouterr().err
