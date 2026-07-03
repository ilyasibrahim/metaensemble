"""Functional tests for the `metaensemble stats` digest (tools/stats.py)."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
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


def _seed_two_executors(state_root: Path) -> tuple[str, str]:
    """Seed 3 ok Runs for one Executor and 1 failed Run for a second.

    Returns (busy_alias, quiet_alias) so tests can assert the top-Executor
    ordering without re-deriving the aliases.
    """
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
    ledger._conn.execute(
        "INSERT INTO tasks (task_id, task_type, status, created_ts) VALUES (?, ?, ?, ?)",
        ("test-task", "test", "open", now),
    )

    busy_alias = make_alias("be", uuid7())
    quiet_alias = make_alias("qa", uuid7())
    executors = {
        busy_alias: str(uuid7()),
        quiet_alias: str(uuid7()),
    }
    for alias, executor_id in executors.items():
        ledger.upsert_executor(Executor(
            executor_id=executor_id, alias=alias, role_id="backend",
            parent_executor_id=None, created_ts=now, last_seen_ts=now,
            status="active",
        ))

    from metaensemble.hooks._common import current_window_id
    window_id = current_window_id()

    def _run(executor_id: str, outcome: str) -> Run:
        return Run(
            run_id=str(uuid7()),
            executor_id=executor_id, task_id="test-task",
            model="sonnet", tokens_in=200, tokens_out=100,
            window_id=window_id,
            started_ts=now, ended_ts=now, outcome=outcome,
        )

    for _ in range(3):
        ledger.append_run(_run(executors[busy_alias], "ok"))
    ledger.append_run(_run(executors[quiet_alias], "failed"))
    ledger.close()
    return busy_alias, quiet_alias


@pytest.fixture
def state_root(tmp_path):
    return tmp_path / "state"


def test_stats_tool_reports_growth_and_run_mix(state_root):
    busy_alias, quiet_alias = _seed_two_executors(state_root)
    code, out, _ = _invoke_module("metaensemble.tools.stats", state_root)
    assert code == 0
    assert "Ledger stats" in out
    assert "Runs recorded: 4" in out
    assert "ok 3 (75.0%)" in out
    assert "failed 1 (25.0%)" in out
    # Footprint rows report on-disk sizes plus the derived growth constant
    # anchored to the PERFORMANCE.md §5.1 measurement.
    assert "department.db" in out
    assert "runs.jsonl" in out
    assert "KiB/Run" in out
    assert "PERFORMANCE.md §5.1" in out
    # Top-Executor table joins alias + Role, busiest first.
    assert f"| `{busy_alias}` | backend | 3 |" in out
    assert f"| `{quiet_alias}` | backend | 1 |" in out
    assert out.index(busy_alias) < out.index(quiet_alias)


def test_stats_tool_notes_current_window(state_root):
    _seed_two_executors(state_root)
    code, out, _ = _invoke_module("metaensemble.tools.stats", state_root)
    from metaensemble.hooks._common import current_window_id
    assert code == 0
    # The seeded Runs land in the current window, and the note stays
    # Ledger-scoped ("dispatched Runs only") — never plan-wide.
    assert f"Current window `{current_window_id()}`" in out
    assert "4 Run(s)" in out
    assert "dispatched Runs only" in out


def test_stats_tool_handles_empty_ledger(state_root):
    state_root.mkdir(parents=True, exist_ok=True)
    code, out, _ = _invoke_module("metaensemble.tools.stats", state_root)
    assert code == 0
    assert "no Runs recorded yet" in out


def test_stats_cli_subcommand_delegates_to_tool(state_root):
    _seed_two_executors(state_root)
    code, out, _ = _invoke_module("metaensemble.cli", state_root, "stats")
    assert code == 0
    assert "Ledger stats" in out
    assert "Runs recorded: 4" in out
