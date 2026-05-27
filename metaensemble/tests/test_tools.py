"""Functional tests for the Principal-facing CLI tools."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from metaensemble.lib.ids import make_alias, uuid7
from metaensemble.lib.ledger import Executor, Ledger, Run


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MIGRATION_PATH = REPO_ROOT / "metaensemble" / "state" / "migrations" / "001_init.sql"


def _invoke_module(module: str, state_root: Path, *args: str) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["METAENSEMBLE_STATE_DIR"] = str(state_root)
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-m", module, *args],
        capture_output=True, text=True, env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _seed(state_root: Path, runs: int = 3) -> tuple[str, str]:
    state_root.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(
        db_path=state_root / "department.db",
        jsonl_path=state_root / "runs.jsonl",
    )
    ledger.initialize(MIGRATION_PATH.read_text())
    now = datetime.now(timezone.utc).isoformat()
    ledger._conn.execute(
        "INSERT INTO roles (role_id, version, spec_path, model_tier, created_ts) "
        "VALUES (?, ?, ?, ?, ?)",
        ("backend", "1.0.0", "roles/backend.md", "sonnet", now),
    )
    executor_id = str(uuid7())
    alias = make_alias("be", uuid7())
    ledger.upsert_executor(Executor(
        executor_id=executor_id, alias=alias, role_id="backend",
        parent_executor_id=None, created_ts=now, last_seen_ts=now,
        status="active",
    ))
    ledger._conn.execute(
        "INSERT INTO tasks (task_id, task_type, status, created_ts) VALUES (?, ?, ?, ?)",
        ("test-task", "test", "open", now),
    )
    from metaensemble.hooks._common import current_window_id
    window_id = current_window_id()
    for _ in range(runs):
        ledger.append_run(Run(
            run_id=str(uuid7()),
            executor_id=executor_id, task_id="test-task",
            model="sonnet", tokens_in=200, tokens_out=100,
            window_id=window_id,
            started_ts=now, ended_ts=now, outcome="ok",
        ))
    ledger.close()
    return executor_id, alias


@pytest.fixture
def state_root(tmp_path):
    return tmp_path / "state"


def test_limits_tool_reports_current_burn(state_root):
    """The limits display separates plan-wide %, project-scope tokens,
    Ledger Runs, and cache tokens into distinct rows so the Principal
    cannot confuse plan-scope numbers with project-scope ones.
    """
    _seed(state_root, runs=2)
    code, out, _ = _invoke_module("metaensemble.tools.limits", state_root)
    assert code == 0
    assert "Window" in out
    assert "Ledger" in out
    assert "Plan 5h" in out
    assert "Project" in out
    assert "Cache" in out


def test_standup_tool_reports_recent_activity(state_root):
    _seed(state_root, runs=5)
    code, out, _ = _invoke_module("metaensemble.tools.standup", state_root)
    assert code == 0
    assert "Standup" in out
    assert "Last 24 hours" in out


def test_executors_tool_lists_active_executors(state_root):
    _, alias = _seed(state_root, runs=1)
    code, out, _ = _invoke_module("metaensemble.tools.executors", state_root)
    assert code == 0
    assert alias in out
    assert "backend" in out


def test_executors_tool_handles_empty_ledger(state_root):
    state_root.mkdir(parents=True, exist_ok=True)
    code, out, _ = _invoke_module("metaensemble.tools.executors", state_root)
    assert code == 0
    assert "no Executors" in out


def test_ledger_recent_subcommand(state_root):
    _seed(state_root, runs=3)
    code, out, _ = _invoke_module("metaensemble.tools.ledger", state_root, "recent", "--limit", "10")
    assert code == 0
    assert "Recent runs" in out


def test_ledger_by_executor_with_alias(state_root):
    _, alias = _seed(state_root, runs=2)
    code, out, _ = _invoke_module("metaensemble.tools.ledger", state_root, "by-executor", alias)
    assert code == 0
    assert alias in out


def test_ledger_by_task_subcommand(state_root):
    _seed(state_root, runs=2)
    code, out, _ = _invoke_module("metaensemble.tools.ledger", state_root, "by-task", "test-task")
    assert code == 0
    assert "test-task" in out


def test_perf_tool_with_no_runs(state_root):
    state_root.mkdir(parents=True, exist_ok=True)
    code, out, _ = _invoke_module("metaensemble.tools.perf", state_root)
    assert code == 0
    assert "Performance" in out


def test_perf_excludes_stale_reconciled_sidecars_from_latency(state_root):
    state_root.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(
        db_path=state_root / "department.db",
        jsonl_path=state_root / "runs.jsonl",
    )
    ledger.initialize(MIGRATION_PATH.read_text())
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    ledger._conn.execute(
        "INSERT INTO roles (role_id, version, spec_path, model_tier, created_ts) "
        "VALUES (?, ?, ?, ?, ?)",
        ("backend", "1.0.0", "roles/backend.md", "sonnet", now),
    )
    executor_id = str(uuid7())
    ledger.upsert_executor(Executor(
        executor_id=executor_id,
        alias=make_alias("be", uuid7()),
        role_id="backend",
        parent_executor_id=None,
        created_ts=now,
        last_seen_ts=now,
        status="active",
    ))
    ledger._conn.execute(
        "INSERT INTO tasks (task_id, task_type, status, created_ts) VALUES (?, ?, ?, ?)",
        ("test-task", "test", "open", now),
    )
    from metaensemble.hooks._common import current_window_id
    ledger.append_run(Run(
        run_id=str(uuid7()),
        executor_id=executor_id,
        task_id="test-task",
        model="sonnet",
        tokens_in=100,
        tokens_out=50,
        window_id=current_window_id(),
        started_ts=(now_dt - timedelta(seconds=30)).isoformat(),
        ended_ts=now,
        outcome="ok",
    ))
    ledger.append_run(Run(
        run_id=str(uuid7()),
        executor_id=executor_id,
        task_id="test-task",
        model="sonnet",
        tokens_in=100,
        tokens_out=0,
        window_id=current_window_id(),
        started_ts=(now_dt - timedelta(days=2)).isoformat(),
        ended_ts=now,
        outcome="failed",
        failure_reason="interrupted: stale sidecar reconciled by metaensemble",
    ))
    ledger.close()

    code, out, _ = _invoke_module("metaensemble.tools.perf", state_root)
    assert code == 0
    assert "p50 30000ms" in out
    assert "stale reconciled sidecar" in out
    assert "172800" not in out
