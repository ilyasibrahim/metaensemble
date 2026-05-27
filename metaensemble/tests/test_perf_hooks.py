"""Hook-latency benchmark against a 10k-row Ledger.

PERFORMANCE.md §4 Benchmark 2. CI gate: every hook script completes in
under the configured p95 budget when invoked against a populated Ledger.
The budget is generous to absorb Python interpreter startup; the hook's
own work must stay well below.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from metaensemble.lib.ids import make_alias, uuid7
from metaensemble.lib.ledger import Executor, Ledger, Run


P95_BUDGET_MS = 400.0  # generous; absorbs Python startup. Hook's own work <100ms.
N_ROWS = 10_000
N_ITERATIONS = 25  # smaller than the lib benchmarks because subprocess startup is expensive

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "state" / "migrations" / "001_init.sql"
)


def _populate(state_root: Path, n: int) -> Ledger:
    state_root.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(
        db_path=state_root / "department.db",
        jsonl_path=state_root / "runs.jsonl",
    )
    ledger.initialize(MIGRATION_PATH.read_text())
    now = datetime.now()
    ledger._conn.execute(
        "INSERT INTO roles (role_id, version, spec_path, model_tier, created_ts) "
        "VALUES (?, ?, ?, ?, ?)",
        ("backend", "1.0.0", "roles/backend.md", "sonnet", now.isoformat()),
    )
    eid = str(uuid7())
    alias = make_alias("be", uuid7())
    ledger.upsert_executor(Executor(
        executor_id=eid, alias=alias, role_id="backend",
        parent_executor_id=None,
        created_ts=now.isoformat(), last_seen_ts=now.isoformat(),
        status="active",
    ))
    ledger._conn.execute(
        "INSERT INTO tasks (task_id, task_type, status, created_ts) VALUES (?, ?, ?, ?)",
        ("perf-task", "perf", "open", now.isoformat()),
    )

    for i in range(n):
        ts = (now - timedelta(seconds=i)).isoformat()
        ledger.append_run(Run(
            run_id=str(uuid7()),
            executor_id=eid,
            task_id="perf-task",
            model="sonnet",
            tokens_in=100, tokens_out=50,
            window_id=f"window-{i // 100}",
            started_ts=ts, ended_ts=ts,
            outcome="ok",
        ))
    return ledger


def _measure_hook_p95(
    hook: str,
    stdin_payload: dict,
    state_root: Path,
    iterations: int = N_ITERATIONS,
) -> float:
    env = os.environ.copy()
    env["METAENSEMBLE_STATE_DIR"] = str(state_root)
    env["PYTHONPATH"] = str(HOOKS_DIR.parent.parent)
    cmd = [sys.executable, str(HOOKS_DIR / hook)]
    input_str = json.dumps(stdin_payload)
    timings: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        subprocess.run(
            cmd, input=input_str, capture_output=True, text=True, env=env,
        )
        timings.append((time.perf_counter() - t0) * 1000.0)
    timings.sort()
    return timings[int(0.95 * len(timings))]


@pytest.fixture(scope="module")
def populated(tmp_path_factory):
    state_root = tmp_path_factory.mktemp("perf_hooks_state")
    ledger = _populate(state_root, N_ROWS)
    ledger.close()
    yield state_root


def test_session_start_meets_p95(populated):
    p95 = _measure_hook_p95("session_start.py", {}, populated)
    assert p95 < P95_BUDGET_MS, f"session_start p95={p95:.1f}ms exceeds {P95_BUDGET_MS}ms"


def test_pre_task_meets_p95(populated):
    payload = {"tool_name": "Task", "tool_input": {"budget": 100}}
    p95 = _measure_hook_p95("pre_task.py", payload, populated)
    assert p95 < P95_BUDGET_MS, f"pre_task p95={p95:.1f}ms exceeds {P95_BUDGET_MS}ms"


def test_post_task_meets_p95(populated):
    # Use a non-Task tool name so the hook short-circuits without DB writes;
    # the measurement we want is the dispatch-tag short-circuit latency.
    payload = {"tool_name": "Other"}
    p95 = _measure_hook_p95("post_task.py", payload, populated)
    assert p95 < P95_BUDGET_MS, f"post_task p95={p95:.1f}ms exceeds {P95_BUDGET_MS}ms"


def test_deliverable_sync_meets_p95(populated):
    payload = {"tool_name": "Other"}
    p95 = _measure_hook_p95("deliverable_sync.py", payload, populated)
    assert p95 < P95_BUDGET_MS, f"deliverable_sync p95={p95:.1f}ms exceeds {P95_BUDGET_MS}ms"


def test_session_summary_meets_p95(populated):
    p95 = _measure_hook_p95("session_summary.py", {}, populated)
    assert p95 < P95_BUDGET_MS, f"session_summary p95={p95:.1f}ms exceeds {P95_BUDGET_MS}ms"
