"""Query-latency benchmark on a 10k-row Ledger.

PERFORMANCE.md §4 Benchmark 3. CI gate: every named query function in
metaensemble/lib/ledger.py has p95 latency below the budget on a populated ledger.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from metaensemble.lib.ids import make_alias, uuid7
from metaensemble.lib.ledger import Executor, Ledger, Run


P95_BUDGET_MS = 10.0
N_ROWS = 10_000
N_ITERATIONS = 100

MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "state" / "migrations" / "001_init.sql"
)


def _populate(ledger: Ledger, n: int) -> tuple[str, str, str]:
    """Populate `n` runs across a single Executor and Task. Multiple windows."""
    role_id = "backend"
    ledger._conn.execute(
        "INSERT INTO roles (role_id, version, spec_path, model_tier, created_ts) "
        "VALUES (?, ?, ?, ?, ?)",
        (role_id, "1.0.0", "roles/backend.md", "sonnet", datetime.now().isoformat()),
    )

    executor_id = str(uuid7())
    alias = make_alias("be", uuid7())
    now = datetime.now()
    ledger.upsert_executor(Executor(
        executor_id=executor_id,
        alias=alias,
        role_id=role_id,
        parent_executor_id=None,
        created_ts=now.isoformat(),
        last_seen_ts=now.isoformat(),
        status="active",
    ))

    task_id = "perf-task"
    ledger._conn.execute(
        "INSERT INTO tasks (task_id, task_type, status, created_ts) VALUES (?, ?, ?, ?)",
        (task_id, "perf", "open", now.isoformat()),
    )

    for i in range(n):
        ts = (now - timedelta(seconds=i)).isoformat()
        ledger.append_run(Run(
            run_id=str(uuid7()),
            executor_id=executor_id,
            task_id=task_id,
            model="sonnet",
            tokens_in=100,
            tokens_out=50,
            window_id=f"window-{i // 100}",  # ~100 windows over 10k rows
            started_ts=ts,
            ended_ts=ts,
            outcome="ok",
        ))

    return executor_id, task_id, alias


def _p95_ms(callable_fn, iterations: int = N_ITERATIONS) -> float:
    timings: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        callable_fn()
        timings.append((time.perf_counter() - t0) * 1000.0)
    timings.sort()
    return timings[int(0.95 * len(timings))]


@pytest.fixture(scope="module")
def populated(tmp_path_factory):
    """A 10k-row Ledger; one-time setup for this module."""
    tmp = tmp_path_factory.mktemp("perf_ledger")
    ledger = Ledger(db_path=tmp / "perf.db", jsonl_path=tmp / "perf.jsonl")
    ledger.initialize(MIGRATION_PATH.read_text())
    executor_id, task_id, alias = _populate(ledger, N_ROWS)
    yield ledger, executor_id, task_id, alias
    ledger.close()


def test_get_recent_runs_meets_p95(populated):
    ledger, *_ = populated
    p95 = _p95_ms(lambda: ledger.get_recent_runs(limit=50))
    assert p95 < P95_BUDGET_MS, (
        f"get_recent_runs p95={p95:.2f}ms exceeds budget {P95_BUDGET_MS}ms"
    )


def test_get_runs_by_executor_meets_p95(populated):
    ledger, executor_id, _, _ = populated
    p95 = _p95_ms(lambda: ledger.get_runs_by_executor(executor_id, limit=50))
    assert p95 < P95_BUDGET_MS, (
        f"get_runs_by_executor p95={p95:.2f}ms exceeds budget {P95_BUDGET_MS}ms"
    )


def test_get_runs_by_task_meets_p95(populated):
    ledger, _, task_id, _ = populated
    p95 = _p95_ms(lambda: ledger.get_runs_by_task(task_id, limit=50))
    assert p95 < P95_BUDGET_MS, (
        f"get_runs_by_task p95={p95:.2f}ms exceeds budget {P95_BUDGET_MS}ms"
    )


def test_get_window_burn_meets_p95(populated):
    ledger, *_ = populated
    p95 = _p95_ms(lambda: ledger.get_window_burn("window-50"))
    assert p95 < P95_BUDGET_MS, (
        f"get_window_burn p95={p95:.2f}ms exceeds budget {P95_BUDGET_MS}ms"
    )


def test_get_executor_by_alias_meets_p95(populated):
    ledger, _, _, alias = populated
    p95 = _p95_ms(lambda: ledger.get_executor_by_alias(alias))
    assert p95 < P95_BUDGET_MS, (
        f"get_executor_by_alias p95={p95:.2f}ms exceeds budget {P95_BUDGET_MS}ms"
    )


def test_indices_used_for_runs_queries(populated):
    """Verify EXPLAIN QUERY PLAN selects the expected index for each query.

    This catches regressions where a query starts doing a full table scan
    because an index was dropped or a WHERE clause was changed without
    updating the index list (PERFORMANCE.md §3 R2).
    """
    ledger, executor_id, task_id, alias = populated
    def _plan_details(query: str, params: tuple) -> list[str]:
        rows = ledger._conn.execute(query, params).fetchall()
        # EXPLAIN QUERY PLAN rows expose the plan text in the `detail` column.
        return [row["detail"] for row in rows]

    by_executor = _plan_details(
        "EXPLAIN QUERY PLAN SELECT * FROM runs WHERE executor_id = ? "
        "ORDER BY ended_ts DESC LIMIT 50",
        (executor_id,),
    )
    by_task = _plan_details(
        "EXPLAIN QUERY PLAN SELECT * FROM runs WHERE task_id = ? "
        "ORDER BY ended_ts DESC LIMIT 50",
        (task_id,),
    )
    by_window = _plan_details(
        "EXPLAIN QUERY PLAN SELECT * FROM runs WHERE window_id = ?",
        ("window-50",),
    )

    assert any("idx_runs_executor" in d for d in by_executor), \
        f"by_executor plan does not use idx_runs_executor: {by_executor}"
    assert any("idx_runs_task" in d for d in by_task), \
        f"by_task plan does not use idx_runs_task: {by_task}"
    assert any("idx_runs_window" in d for d in by_window), \
        f"by_window plan does not use idx_runs_window: {by_window}"
