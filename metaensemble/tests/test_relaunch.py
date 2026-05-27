"""Tests for cross-session relaunch context preparation."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone


from metaensemble.lib.dispatch import materialize_plan, plan_solo
from metaensemble.lib.ids import uuid7
from metaensemble.lib.ledger import Run
from metaensemble.lib.relaunch import prepare_relaunch, render_relaunch_context


def _seed_role(ledger):
    ledger._conn.execute(
        "INSERT INTO roles (role_id, version, spec_path, model_tier, created_ts) "
        "VALUES (?, ?, ?, ?, ?)",
        ("backend", "1.0.0", "roles/backend.md", "sonnet", "2026-05-13T00:00:00"),
    )


def _seed_task(ledger, task_id: str = "test-task"):
    ledger._conn.execute(
        "INSERT INTO tasks (task_id, task_type, status, created_ts) VALUES (?, ?, ?, ?)",
        (task_id, "test", "open", "2026-05-13T00:00:00"),
    )


def _spawn_executor(ledger):
    """Materialize a solo plan and return the created Executor."""
    plan = plan_solo("backend", "be", "sonnet")
    _, executors = materialize_plan(ledger, plan)
    return executors[0]


def test_prepare_relaunch_returns_none_for_unknown_alias(tmp_ledger):
    _seed_role(tmp_ledger)
    assert prepare_relaunch(tmp_ledger, "missing-aaa") is None


def test_prepare_relaunch_with_no_prior_runs(tmp_ledger):
    _seed_role(tmp_ledger)
    executor = _spawn_executor(tmp_ledger)

    ctx = prepare_relaunch(tmp_ledger, executor.alias)
    assert ctx is not None
    assert ctx.executor.alias == executor.alias
    assert ctx.last_run is None
    assert ctx.history_length == 0


def test_prepare_relaunch_loads_brief_and_deliverable_summary(tmp_ledger, tmp_path):
    _seed_role(tmp_ledger)
    _seed_task(tmp_ledger)
    executor = _spawn_executor(tmp_ledger)

    # Write a Brief and a Deliverable to disk so the relaunch can pick them up.
    brief_in = tmp_path / "brief_in.json"
    brief_in.write_text(json.dumps({"v": 1, "task_id": "test-task"}))
    deliverable = tmp_path / "deliverable.md"
    deliverable.write_text(
        "# Result\n\n## Summary\n\nImplemented login and logout endpoints.\n\n"
        "## Details\n\nMore details here."
    )

    now = datetime.now(timezone.utc).isoformat()
    tmp_ledger.append_run(Run(
        run_id=str(uuid7()),
        executor_id=executor.executor_id,
        task_id="test-task",
        model="sonnet", tokens_in=200, tokens_out=100,
        window_id="2026-05-13T00",
        started_ts=now, ended_ts=now,
        outcome="ok",
        brief_in_path=str(brief_in),
        deliverable_path=str(deliverable),
    ))

    ctx = prepare_relaunch(tmp_ledger, executor.alias)
    assert ctx is not None
    assert ctx.last_brief_in == {"v": 1, "task_id": "test-task"}
    assert ctx.last_deliverable_summary is not None
    assert "login and logout" in ctx.last_deliverable_summary
    assert ctx.last_deliverable_full is None  # cheap mode


def test_prepare_relaunch_full_mode_loads_complete_deliverable(tmp_ledger, tmp_path):
    _seed_role(tmp_ledger)
    _seed_task(tmp_ledger)
    executor = _spawn_executor(tmp_ledger)

    deliverable = tmp_path / "deliverable.md"
    deliverable.write_text(
        "# Result\n\n## Summary\n\nShort summary.\n\n## Details\n\nLong detailed body that the cheap mode would not load."
    )

    now = datetime.now(timezone.utc).isoformat()
    tmp_ledger.append_run(Run(
        run_id=str(uuid7()),
        executor_id=executor.executor_id,
        task_id="test-task",
        model="sonnet", tokens_in=200, tokens_out=100,
        window_id="2026-05-13T00",
        started_ts=now, ended_ts=now,
        outcome="ok",
        deliverable_path=str(deliverable),
    ))

    cheap = prepare_relaunch(tmp_ledger, executor.alias)
    full = prepare_relaunch(tmp_ledger, executor.alias, full=True)

    assert cheap.last_deliverable_full is None
    assert full.last_deliverable_full is not None
    assert "Long detailed body" in full.last_deliverable_full


def test_prepare_relaunch_cheap_mode_reports_total_history(tmp_ledger, tmp_path):
    """Cheap mode loads one Run but still reports the Executor's full history."""
    _seed_role(tmp_ledger)
    _seed_task(tmp_ledger)
    executor = _spawn_executor(tmp_ledger)

    base = datetime.now(timezone.utc)
    for i in range(3):
        deliverable = tmp_path / f"d-{i}.md"
        deliverable.write_text(f"# Result\n\n## Summary\n\nRun {i}.")
        ts = (base + timedelta(seconds=i)).isoformat()
        tmp_ledger.append_run(Run(
            run_id=str(uuid7()),
            executor_id=executor.executor_id,
            task_id="test-task",
            model="sonnet", tokens_in=100 + i, tokens_out=50,
            window_id="2026-05-13T00",
            started_ts=ts, ended_ts=ts,
            outcome="ok",
            deliverable_path=str(deliverable),
        ))

    ctx = prepare_relaunch(tmp_ledger, executor.alias)

    assert ctx.history_length == 3
    assert ctx.last_run is not None


def test_render_relaunch_context_emits_markdown(tmp_ledger, tmp_path):
    _seed_role(tmp_ledger)
    _seed_task(tmp_ledger)
    executor = _spawn_executor(tmp_ledger)

    deliverable = tmp_path / "d.md"
    deliverable.write_text("# X\n\n## Summary\n\nDid the work.")
    now = datetime.now(timezone.utc).isoformat()
    tmp_ledger.append_run(Run(
        run_id=str(uuid7()),
        executor_id=executor.executor_id,
        task_id="test-task",
        model="sonnet", tokens_in=100, tokens_out=50,
        window_id="2026-05-13T00",
        started_ts=now, ended_ts=now,
        outcome="ok",
        deliverable_path=str(deliverable),
    ))

    ctx = prepare_relaunch(tmp_ledger, executor.alias)
    rendered = render_relaunch_context(ctx)
    assert "Relaunch context" in rendered
    assert executor.alias in rendered
    assert "Did the work" in rendered
