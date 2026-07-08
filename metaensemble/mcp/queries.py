"""Pure, MCP-free read queries over the Ledger for the read-only MCP server.

This module is the serialization boundary between MetaEnsemble's frozen
Ledger dataclasses and any external MCP client (an IDE, Gemini CLI,
ChatGPT, Claude Desktop). It exposes reads only: every function opens the
Ledger through `open_readonly_ledger`, which attaches a SQLite `?mode=ro`
connection (the pattern lib/doctor.py uses), so no client can mutate the
institutional memory it reads.

Two invariants shape the output:

  - Telemetry scope. Window-burn numbers are PROJECT-SCOPED dispatched-Run
    tokens from this project's Ledger — never plan-wide, never a
    percentage. `window_burn` always carries an explicit `scope` string.
  - Dispatched-Runs-only. The Ledger stamps one row per dispatched Run;
    work continued inside a resumed session stamps none. These counts are
    dispatched Runs, not total activity.

All access rides the R1 named-query API in lib/ledger.py — no raw SQL
lives here. Every result set is bounded (R5): `limit` clamps to
`_MAX_LIMIT`. Every function is fail-soft: a missing or uninitialized
Ledger yields an empty result (or an {"error": ...} dict for a dict-shaped
return), never an exception that could crash a long-lived server.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from metaensemble.hooks import _common as hooks_common
from metaensemble.lib.ledger import (
    Executor,
    ExecutorRunCount,
    Ledger,
    Role,
    Run,
)


# Upper bound on every result set (PERFORMANCE.md §3 R5). No query returns
# more rows than this even when a client asks for more.
_MAX_LIMIT = 200


# --- Read-only Ledger access ---------------------------------------------


class _ReadOnlyLedger(Ledger):
    """A Ledger whose SQLite connection is opened read-only.

    Bypasses Ledger.__init__ — which opens a read-write connection and
    switches the database into WAL mode — and attaches a `?mode=ro` URI
    connection instead. Every named query is inherited unchanged, so reads
    still ride the R1 named-query API while writes are refused by SQLite.
    """

    def __init__(self, db_path: Path | str, jsonl_path: Path | str):
        self.db_path = Path(db_path)
        self.jsonl_path = Path(jsonl_path)
        uri = f"{self.db_path.resolve(strict=False).as_uri()}?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True)
        self._conn.row_factory = sqlite3.Row


def open_readonly_ledger(state_dir: Path | None = None) -> Ledger | None:
    """Open this project's Ledger for reading only, or None when absent.

    Resolves the state directory the way the CLI tools do — the
    METAENSEMBLE_STATE_DIR override, else `<cwd>/.metaensemble/state` —
    unless an explicit directory is passed. Returns None when the Ledger DB
    file does not exist yet, so callers degrade to an empty result on an
    uninitialized project rather than raising. The caller owns the returned
    connection and must close it.
    """
    resolved = Path(state_dir) if state_dir is not None else hooks_common.state_dir()
    db = resolved / "department.db"
    if not db.exists():
        return None
    return _ReadOnlyLedger(db, resolved / "runs.jsonl")


@contextmanager
def _ledger_session(state_dir: Path | None = None) -> Iterator[Ledger | None]:
    """Open the read-only Ledger and guarantee its connection is closed."""
    ledger = open_readonly_ledger(state_dir)
    try:
        yield ledger
    finally:
        if ledger is not None:
            ledger.close()


# --- Bounding + parsing helpers ------------------------------------------


def _clamp_limit(limit: object) -> int:
    """Clamp a caller-supplied limit into [1, _MAX_LIMIT] (R5).

    Guards the SQL LIMIT clause: a negative value would become SQLite's
    unbounded `LIMIT -1` and read the whole table, and a non-integer would
    raise at bind time. Both collapse to a safe bounded value here.
    """
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return 1
    if n < 1:
        return 1
    return min(n, _MAX_LIMIT)


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string to a datetime, or None if unparseable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


# --- Serialization: frozen dataclass -> JSON-able dict -------------------


def _run_to_dict(run: Run) -> dict:
    """Project a Run onto the fields a read-only consumer needs."""
    return {
        "run_id": run.run_id,
        "executor_id": run.executor_id,
        "task_id": run.task_id,
        "model": run.model,
        "tokens_in": run.tokens_in,
        "tokens_out": run.tokens_out,
        "window_id": run.window_id,
        "started_ts": run.started_ts,
        "ended_ts": run.ended_ts,
        "outcome": run.outcome,
        "failure_reason": run.failure_reason,
        "brief_in_path": run.brief_in_path,
        "brief_out_path": run.brief_out_path,
        "deliverable_path": run.deliverable_path,
    }


def _executor_to_dict(executor: Executor) -> dict:
    return {
        "executor_id": executor.executor_id,
        "alias": executor.alias,
        "role_id": executor.role_id,
        "parent_executor_id": executor.parent_executor_id,
        "created_ts": executor.created_ts,
        "last_seen_ts": executor.last_seen_ts,
        "status": executor.status,
    }


def _role_to_dict(role: Role) -> dict:
    return {
        "role_id": role.role_id,
        "version": role.version,
        "model_tier": role.model_tier,
        "spec_path": role.spec_path,
        "created_ts": role.created_ts,
    }


def _executor_run_count_to_dict(entry: ExecutorRunCount) -> dict:
    return {
        "alias": entry.alias,
        "role_id": entry.role_id,
        "run_count": entry.run_count,
    }


def _resolve_executor(ledger: Ledger, alias_or_id: str) -> Executor | None:
    """Resolve an alias first, then fall back to a raw executor_id.

    The alias is the human-facing handle (`/relaunch <alias>`); the id is
    the stable internal key. Trying the alias index first mirrors how the
    Coordinator addresses Executors.
    """
    by_alias = ledger.get_executor_by_alias(alias_or_id)
    if by_alias is not None:
        return by_alias
    return ledger.get_executor(alias_or_id)


# --- Public query functions ----------------------------------------------


def recent_runs(limit: int = 20, since_iso: str | None = None) -> list[dict]:
    """Most-recent dispatched Runs, newest first, bounded by `limit`."""
    bounded = _clamp_limit(limit)
    since = _parse_iso(since_iso)
    try:
        with _ledger_session() as ledger:
            if ledger is None:
                return []
            runs = ledger.get_recent_runs(limit=bounded, since=since)
            return [_run_to_dict(r) for r in runs]
    except Exception:
        return []


def runs_by_executor(alias_or_id: str, limit: int = 20) -> list[dict]:
    """Runs by one Executor, resolved by alias or id, newest first."""
    bounded = _clamp_limit(limit)
    try:
        with _ledger_session() as ledger:
            if ledger is None:
                return []
            executor = _resolve_executor(ledger, alias_or_id)
            executor_id = executor.executor_id if executor is not None else alias_or_id
            runs = ledger.get_runs_by_executor(executor_id, limit=bounded)
            return [_run_to_dict(r) for r in runs]
    except Exception:
        return []


def runs_by_task(task_id: str, limit: int = 20) -> list[dict]:
    """Runs recorded against one Task, newest first."""
    bounded = _clamp_limit(limit)
    try:
        with _ledger_session() as ledger:
            if ledger is None:
                return []
            runs = ledger.get_runs_by_task(task_id, limit=bounded)
            return [_run_to_dict(r) for r in runs]
    except Exception:
        return []


def active_executors(days: int = 30, limit: int = 50) -> list[dict]:
    """Executors seen within the last `days`, most-recently-seen first."""
    bounded = _clamp_limit(limit)
    try:
        window_days = max(int(days), 0)
    except (TypeError, ValueError):
        window_days = 30
    since = datetime.now(timezone.utc) - timedelta(days=window_days)
    try:
        with _ledger_session() as ledger:
            if ledger is None:
                return []
            executors = ledger.get_active_executors(since, limit=bounded)
            return [_executor_to_dict(e) for e in executors]
    except Exception:
        return []


def executor_detail(alias_or_id: str) -> dict | None:
    """Identity, Role, and lifetime Run count for one Executor.

    Returns None when the alias/id resolves to no Executor (or the Ledger
    is absent), so a client can tell "unknown Executor" apart from an
    Executor with zero Runs.
    """
    try:
        with _ledger_session() as ledger:
            if ledger is None:
                return None
            executor = _resolve_executor(ledger, alias_or_id)
            if executor is None:
                return None
            role = ledger.get_role(executor.role_id)
            run_count = ledger.count_runs_by_executor(executor.executor_id)
            return {
                "executor": _executor_to_dict(executor),
                "role": _role_to_dict(role) if role is not None else None,
                "run_count": run_count,
            }
    except Exception:
        return None


def outcome_counts() -> dict[str, int]:
    """Run tally per outcome literal across the whole Ledger."""
    try:
        with _ledger_session() as ledger:
            if ledger is None:
                return {}
            return ledger.get_outcome_counts()
    except Exception:
        return {}


def top_executors(limit: int = 5) -> list[dict]:
    """Executors ranked by lifetime Run count, highest first."""
    bounded = _clamp_limit(limit)
    try:
        with _ledger_session() as ledger:
            if ledger is None:
                return []
            entries = ledger.get_executor_run_counts(limit=bounded)
            return [_executor_run_count_to_dict(e) for e in entries]
    except Exception:
        return []


def window_burn(window_id: str | None = None) -> dict:
    """Token burn for one 5-hour window from this project's Ledger.

    The numbers are PROJECT-SCOPED dispatched-Run tokens recorded in this
    project's Ledger — never plan-wide and never a percentage (the
    telemetry-scope invariant). The `scope` string states this explicitly
    so no downstream surface can relabel the figure as "% of plan". When
    `window_id` is omitted the current 5-hour window is used.
    """
    resolved_window = window_id or hooks_common.current_window_id()
    scope = (
        "dispatched-Run tokens recorded in this project's Ledger "
        f"for window {resolved_window}"
    )
    try:
        with _ledger_session() as ledger:
            if ledger is None:
                return {
                    "window_id": resolved_window,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "run_count": 0,
                    "scope": scope,
                }
            summary = ledger.get_window_burn(resolved_window)
            return {
                "window_id": summary.window_id,
                "tokens_in": summary.total_tokens_in,
                "tokens_out": summary.total_tokens_out,
                "run_count": summary.total_runs,
                "scope": scope,
            }
    except Exception:
        return {
            "window_id": resolved_window,
            "tokens_in": 0,
            "tokens_out": 0,
            "run_count": 0,
            "scope": scope,
            "error": "ledger read failed",
        }


def ledger_stats() -> dict:
    """Structured Ledger digest: totals, outcome mix, top Executors, window.

    Composes the smaller query functions and returns structured data only.
    The Markdown formatting lives in tools/stats.py and is deliberately not
    duplicated here — an MCP client renders its own view. The counts are
    dispatched Runs only (see the module docstring), and the `window` block
    carries the telemetry-scope note from `window_burn`.
    """
    outcomes = outcome_counts()
    return {
        "total_runs": sum(outcomes.values()),
        "outcome_counts": outcomes,
        "top_executors": top_executors(limit=5),
        "window": window_burn(),
    }
