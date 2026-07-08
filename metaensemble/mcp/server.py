"""Read-only MCP server exposing the MetaEnsemble Ledger (GitHub issue #4).

Any Model Context Protocol client (Gemini CLI, an IDE, ChatGPT, Claude
Desktop, ...) can attach to this server and read the institutional memory
recorded for the current project: dispatched Runs, Executors, outcome mix,
and 5-hour-window token burn. Every tool is read-only; there is no write
surface, by design.

All query logic lives in `metaensemble.mcp.queries`, which opens the Ledger
read-only and returns JSON-able dicts/lists. This module only wires those
pure functions into MCP tools — it never touches SQLite and never
re-implements a query. Each tool forwards to `queries.<name>` at call time
(attribute lookup, not a bound reference) so the query layer stays the
single source of truth.

The `mcp` SDK is an optional dependency. Importing this module is cheap and
SDK-free; the crisp "install metaensemble[mcp]" hint is raised only when a
server is actually built or run, so the import guard degrades gracefully
instead of dumping an ImportError traceback.

Confirmed against the official MCP Python SDK (`mcp` on PyPI, latest 1.28.1,
Python >=3.10): the high-level API is `from mcp.server.fastmcp import
FastMCP`, tools register via `add_tool`/`@tool`, resources via `@resource`,
and the stdio transport starts with `run(transport="stdio")`.
"""
from __future__ import annotations

import json
import sys
from typing import Callable

from metaensemble.mcp import queries


# Raised in place of a bare ImportError traceback when the optional SDK is
# missing. Names the exact extra so any entry point (the CLI subcommand or the
# console script) can surface a one-line remediation.
_INSTALL_HINT = (
    "The MetaEnsemble MCP server needs the optional `mcp` SDK, which is not "
    "installed.\n\n    pip install 'metaensemble[mcp]'\n"
)

_SERVER_NAME = "MetaEnsemble Ledger"

# Shown to the client on connect. Carries both load-bearing caveats so the
# scope of every number is legible before any tool is called.
_SERVER_INSTRUCTIONS = (
    "Read-only access to the MetaEnsemble Ledger for the current project — the "
    "institutional memory of dispatched Runs, Executors, and token burn. Every "
    "tool is read-only; there is no write surface. Counts reflect one row per "
    "dispatched Run recorded in this project's Ledger; work continued inside a "
    "resumed session is not recorded. Window burn is project-scoped Ledger "
    "tokens for a 5-hour bucket, never a share of a plan or subscription."
)


# --- Tool functions: one per frozen query, delegating to queries.<name> ---
#
# These are module-level (not closures) so tests can address them directly and
# so `add_tool` derives each tool's schema from the real signature and its
# description from the docstring the client is shown. Every body forwards to
# `queries.<name>` by attribute lookup, so the query layer remains the only
# implementation of the read.


def _recent_runs(limit: int = 20, since_iso: str | None = None) -> list[dict]:
    """List the most recent dispatched Runs in this project's Ledger, newest first.

    `limit` is clamped to a safe maximum server-side. `since_iso` optionally
    restricts to Runs that ended at or after an ISO-8601 timestamp (e.g.
    "2026-07-01T00:00:00+00:00"). The Ledger records one row per dispatched
    Run; work continued inside a resumed session is not counted.
    """
    return queries.recent_runs(limit=limit, since_iso=since_iso)


def _runs_by_executor(alias_or_id: str, limit: int = 20) -> list[dict]:
    """List recent Runs for one Executor, newest first.

    The Executor is resolved by alias first, then by executor id. `limit` is
    clamped to a safe maximum. Dispatched Runs only — work continued inside a
    resumed session is not counted.
    """
    return queries.runs_by_executor(alias_or_id, limit=limit)


def _runs_by_task(task_id: str, limit: int = 20) -> list[dict]:
    """List recent Runs recorded against one Task id, newest first.

    `limit` is clamped to a safe maximum. Dispatched Runs only — work
    continued inside a resumed session is not counted.
    """
    return queries.runs_by_task(task_id, limit=limit)


def _active_executors(days: int = 30, limit: int = 50) -> list[dict]:
    """List Executors seen within the last `days` days, most recently seen first.

    `limit` is clamped to a safe maximum.
    """
    return queries.active_executors(days=days, limit=limit)


def _executor_detail(alias_or_id: str) -> dict | None:
    """Return one Executor's identity, Role, and lifetime Run count.

    The Executor is resolved by alias first, then by executor id. Returns null
    when no Executor matches, so an unknown handle is distinguishable from an
    Executor with zero Runs. The Run count is dispatched Runs only.
    """
    return queries.executor_detail(alias_or_id)


def _outcome_counts() -> dict:
    """Return the count of dispatched Runs by outcome (ok, failed, partial, ...).

    Dispatched Runs only — work continued inside a resumed session is not
    counted.
    """
    return queries.outcome_counts()


def _top_executors(limit: int = 5) -> list[dict]:
    """Return the Executors with the most recorded Runs, highest first.

    `limit` is clamped to a safe maximum. Run counts are dispatched Runs only.
    """
    return queries.top_executors(limit=limit)


def _window_burn(window_id: str | None = None) -> dict:
    """Return token burn for one 5-hour window (defaults to the current one).

    The numbers are dispatched-Run tokens recorded in this project's Ledger for
    the window — NOT a share of any plan or subscription limit. The returned
    `scope` field states this explicitly.
    """
    return queries.window_burn(window_id=window_id)


def _ledger_stats() -> dict:
    """Return a structured Ledger summary: total Runs, outcome mix, top Executors.

    Structured data only (the Markdown view lives in the `metaensemble stats`
    tool). Counts are dispatched Runs only, and the `window` block carries the
    project-scoped telemetry-scope note.
    """
    return queries.ledger_stats()


# Tool name (shown to the client) -> delegating function. The name is the
# stable public handle; the function's signature and docstring supply the
# schema and description.
_TOOLS: tuple[tuple[str, Callable], ...] = (
    ("recent_runs", _recent_runs),
    ("runs_by_executor", _runs_by_executor),
    ("runs_by_task", _runs_by_task),
    ("active_executors", _active_executors),
    ("executor_detail", _executor_detail),
    ("outcome_counts", _outcome_counts),
    ("top_executors", _top_executors),
    ("window_burn", _window_burn),
    ("ledger_stats", _ledger_stats),
)


def _require_fastmcp():
    """Import and return the FastMCP class, or raise a crisp install hint.

    Kept out of module import so `import metaensemble.mcp.server` stays cheap
    and SDK-free; the actionable error surfaces only when a server is actually
    built, never as an import-time traceback.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise ImportError(_INSTALL_HINT) from exc
    return FastMCP


def build_server():
    """Construct the read-only Ledger MCP server with every query tool registered.

    Registers one tool per frozen query function and one read-only resource
    (`ledger://stats`). Raises ImportError with an install hint when the
    optional `mcp` SDK is absent.
    """
    FastMCP = _require_fastmcp()
    server = FastMCP(_SERVER_NAME, instructions=_SERVER_INSTRUCTIONS)
    for name, fn in _TOOLS:
        server.add_tool(fn, name=name)

    @server.resource("ledger://stats", name="ledger_stats", mime_type="application/json")
    def _ledger_stats_resource() -> str:
        """Structured Ledger summary as JSON: total Runs, outcome mix, top Executors.

        Dispatched Runs only — work continued inside a resumed session is not
        counted.
        """
        return json.dumps(queries.ledger_stats())

    return server


def run_stdio() -> int | None:
    """Run the read-only Ledger MCP server over stdio; blocks until disconnect.

    Returns a non-zero code (after printing the install hint) when the optional
    `mcp` SDK is missing, so both `metaensemble mcp-serve` and the
    `metaensemble-mcp` console script fail cleanly rather than dumping a
    traceback. Returns None on a normal server shutdown.
    """
    try:
        server = build_server()
    except ImportError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    server.run(transport="stdio")
    return None
