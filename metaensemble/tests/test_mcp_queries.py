"""Tests for the read-only MCP query/serialization layer.

The public functions resolve the Ledger through METAENSEMBLE_STATE_DIR, so
every test points that override at a controlled state directory: the
seeded one built here, or an empty one for the fail-soft cases. Nothing
here may touch the real repo Ledger.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from metaensemble.hooks import _common as hooks_common
from metaensemble.lib.ledger import Executor, Ledger, Run
from metaensemble.mcp import queries

MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "state" / "migrations" / "001_init.sql"
)


@dataclass(frozen=True)
class _Seeded:
    """Handles the tests need to address the seeded fixtures."""

    state_dir: Path
    executor_a_id: str
    executor_b_id: str
    alias_a: str
    alias_b: str
    task_id: str
    current_window: str


def _run(**overrides: object) -> Run:
    """Build a Run with sensible defaults; override what a case cares about."""
    base = dict(
        run_id="r",
        executor_id="exec-a",
        task_id="task-1",
        model="claude-sonnet",
        tokens_in=100,
        tokens_out=50,
        window_id="2026-01-01T00",
        started_ts="2026-06-01T00:00:00+00:00",
        ended_ts="2026-06-01T00:00:00+00:00",
        outcome="ok",
    )
    base.update(overrides)
    return Run(**base)  # type: ignore[arg-type]


@pytest.fixture
def seeded(tmp_path, monkeypatch) -> _Seeded:
    """Seed a real department.db and point METAENSEMBLE_STATE_DIR at it.

    Executor A gets three Runs (ok, failed, ok), Executor B one (partial),
    so outcome mix, ranking, window aggregation, and since-filtering are
    all exercisable. Three Runs land in the current window so the default
    `window_burn()` has a non-zero count to assert on.
    """
    state_dir = tmp_path / ".metaensemble" / "state"
    state_dir.mkdir(parents=True)
    current = hooks_common.current_window_id()
    now = datetime.now(timezone.utc).isoformat()

    ledger = Ledger(
        db_path=state_dir / "department.db",
        jsonl_path=state_dir / "runs.jsonl",
    )
    ledger.initialize(MIGRATION_PATH.read_text())
    ledger.ensure_role(
        role_id="backend", version="1.0.0",
        spec_path="roles/backend.md", model_tier="sonnet",
    )
    ledger.upsert_executor(Executor(
        executor_id="exec-a", alias="be-alpha", role_id="backend",
        parent_executor_id=None, created_ts=now, last_seen_ts=now, status="active",
    ))
    ledger.upsert_executor(Executor(
        executor_id="exec-b", alias="be-beta", role_id="backend",
        parent_executor_id=None, created_ts=now, last_seen_ts=now, status="active",
    ))
    ledger.ensure_task(task_id="task-1", task_type="test", status="open")

    ledger.append_run(_run(
        run_id="rA1", executor_id="exec-a", outcome="ok",
        window_id="2026-06-01T00", ended_ts="2026-06-01T00:00:00+00:00",
        tokens_in=100, tokens_out=50,
    ))
    ledger.append_run(_run(
        run_id="rA2", executor_id="exec-a", outcome="failed",
        window_id=current, ended_ts="2026-06-15T00:00:00+00:00",
        tokens_in=200, tokens_out=80, failure_reason="boom",
    ))
    ledger.append_run(_run(
        run_id="rA3", executor_id="exec-a", outcome="ok",
        window_id=current, ended_ts="2026-07-01T00:00:00+00:00",
        tokens_in=300, tokens_out=120,
    ))
    ledger.append_run(_run(
        run_id="rB1", executor_id="exec-b", outcome="partial",
        window_id=current, ended_ts="2026-06-20T00:00:00+00:00",
        tokens_in=10, tokens_out=5,
    ))
    ledger.close()

    monkeypatch.setenv("METAENSEMBLE_STATE_DIR", str(state_dir))
    return _Seeded(
        state_dir=state_dir,
        executor_a_id="exec-a", executor_b_id="exec-b",
        alias_a="be-alpha", alias_b="be-beta",
        task_id="task-1", current_window=current,
    )


@pytest.fixture
def empty_state(tmp_path, monkeypatch) -> Path:
    """Point METAENSEMBLE_STATE_DIR at a dir with no department.db."""
    state_dir = tmp_path / ".metaensemble" / "state"
    state_dir.mkdir(parents=True)
    monkeypatch.setenv("METAENSEMBLE_STATE_DIR", str(state_dir))
    return state_dir


# --- open_readonly_ledger: read-only invariant ---------------------------


def test_open_readonly_ledger_refuses_writes(seeded):
    ledger = queries.open_readonly_ledger()
    assert ledger is not None
    try:
        with pytest.raises(sqlite3.OperationalError):
            ledger._conn.execute("UPDATE runs SET outcome = 'ok' WHERE run_id = 'rA2'")
    finally:
        ledger.close()


def test_open_readonly_ledger_absent_returns_none(empty_state):
    assert queries.open_readonly_ledger() is None


# --- recent_runs ----------------------------------------------------------


def test_recent_runs_shape_and_order(seeded):
    rows = queries.recent_runs()
    assert [r["run_id"] for r in rows] == ["rA3", "rB1", "rA2", "rA1"]
    first = rows[0]
    assert set(first) == {
        "run_id", "executor_id", "task_id", "model",
        "tokens_in", "tokens_out", "window_id",
        "started_ts", "ended_ts", "outcome", "failure_reason",
        "brief_in_path", "brief_out_path", "deliverable_path",
    }
    assert first["outcome"] == "ok"


def test_recent_runs_respects_limit(seeded):
    rows = queries.recent_runs(limit=2)
    assert len(rows) == 2
    assert [r["run_id"] for r in rows] == ["rA3", "rB1"]


def test_recent_runs_since_filter(seeded):
    rows = queries.recent_runs(since_iso="2026-06-18T00:00:00+00:00")
    assert [r["run_id"] for r in rows] == ["rA3", "rB1"]


def test_recent_runs_absent_ledger_is_empty(empty_state):
    assert queries.recent_runs() == []


# --- runs_by_executor / runs_by_task -------------------------------------


def test_runs_by_executor_alias(seeded):
    rows = queries.runs_by_executor(seeded.alias_a)
    assert {r["run_id"] for r in rows} == {"rA1", "rA2", "rA3"}


def test_runs_by_executor_id(seeded):
    rows = queries.runs_by_executor(seeded.executor_a_id)
    assert {r["run_id"] for r in rows} == {"rA1", "rA2", "rA3"}


def test_runs_by_executor_unknown_is_empty(seeded):
    assert queries.runs_by_executor("nobody") == []


def test_runs_by_task(seeded):
    rows = queries.runs_by_task(seeded.task_id)
    assert {r["run_id"] for r in rows} == {"rA1", "rA2", "rA3", "rB1"}


def test_runs_by_task_unknown_is_empty(seeded):
    assert queries.runs_by_task("no-such-task") == []


# --- active_executors -----------------------------------------------------


def test_active_executors(seeded):
    rows = queries.active_executors(days=30)
    aliases = {e["alias"] for e in rows}
    assert aliases == {"be-alpha", "be-beta"}
    assert set(rows[0]) == {
        "executor_id", "alias", "role_id", "parent_executor_id",
        "created_ts", "last_seen_ts", "status",
    }


def test_active_executors_absent_ledger_is_empty(empty_state):
    assert queries.active_executors() == []


# --- executor_detail ------------------------------------------------------


def test_executor_detail_by_alias(seeded):
    detail = queries.executor_detail(seeded.alias_a)
    assert detail is not None
    assert detail["executor"]["alias"] == "be-alpha"
    assert detail["role"]["role_id"] == "backend"
    assert detail["run_count"] == 3


def test_executor_detail_by_id(seeded):
    detail = queries.executor_detail(seeded.executor_b_id)
    assert detail is not None
    assert detail["executor"]["executor_id"] == "exec-b"
    assert detail["run_count"] == 1


def test_executor_detail_unknown_is_none(seeded):
    assert queries.executor_detail("ghost") is None


def test_executor_detail_absent_ledger_is_none(empty_state):
    assert queries.executor_detail("be-alpha") is None


# --- outcome_counts / top_executors --------------------------------------


def test_outcome_counts(seeded):
    counts = queries.outcome_counts()
    assert counts == {"ok": 2, "failed": 1, "partial": 1}


def test_outcome_counts_absent_ledger_is_empty(empty_state):
    assert queries.outcome_counts() == {}


def test_top_executors_ranked(seeded):
    top = queries.top_executors()
    assert top[0]["alias"] == "be-alpha"
    assert top[0]["run_count"] == 3
    assert set(top[0]) == {"alias", "role_id", "run_count"}


def test_top_executors_absent_ledger_is_empty(empty_state):
    assert queries.top_executors() == []


# --- window_burn: telemetry-scope invariant ------------------------------


def test_window_burn_explicit_window(seeded):
    burn = queries.window_burn(seeded.current_window)
    assert burn["run_count"] == 3
    assert burn["tokens_in"] == 510
    assert burn["tokens_out"] == 205
    assert burn["window_id"] == seeded.current_window


def test_window_burn_carries_scope_and_no_percentage(seeded):
    burn = queries.window_burn()
    assert "scope" in burn
    assert seeded.current_window in burn["scope"]
    assert "%" not in json.dumps(burn)


def test_window_burn_absent_ledger_still_scoped(empty_state):
    burn = queries.window_burn()
    assert burn["run_count"] == 0
    assert "scope" in burn
    assert "%" not in burn["scope"]


# --- ledger_stats ---------------------------------------------------------


def test_ledger_stats_composition(seeded):
    stats = queries.ledger_stats()
    assert stats["total_runs"] == 4
    assert stats["outcome_counts"] == {"ok": 2, "failed": 1, "partial": 1}
    assert stats["top_executors"][0]["alias"] == "be-alpha"
    assert "scope" in stats["window"]


def test_ledger_stats_absent_ledger_is_zero(empty_state):
    stats = queries.ledger_stats()
    assert stats["total_runs"] == 0
    assert stats["outcome_counts"] == {}
    assert stats["top_executors"] == []


# --- bounding helper ------------------------------------------------------


def test_clamp_limit_bounds():
    assert queries._clamp_limit(9999) == queries._MAX_LIMIT
    assert queries._clamp_limit(-5) == 1
    assert queries._clamp_limit(0) == 1
    assert queries._clamp_limit(50) == 50
    assert queries._clamp_limit("not-an-int") == 1
