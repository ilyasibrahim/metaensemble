"""Tests for `metaensemble.lib.reconcile`.

The reconciler is the deterministic fallback for sidecars stranded when
the PostToolUse hook does not fire (`kill -9`, runtime crash, budget
exhaustion in `claude --max-budget-usd`). Both layers — session-end
reconcile and stale-sidecar reconcile — must record a failed Run row,
delete the sidecar, and update the Executor's `last_seen_ts`.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from metaensemble.lib.file_events import FileToolEvent, append_file_event
from metaensemble.lib.reconcile import (
    REASON_BUDGET_EXCEEDED,
    REASON_SESSION_END,
    REASON_STALE,
    reconcile_session_pending,
    reconcile_stale_pending,
)
from metaensemble.lib.sidecar import PendingRun, pending_dir, write_pending


def _make_pending(
    *,
    run_id: str = "run-001",
    session_id: str = "sess-A",
    executor_id: str = "exec-1",
    task_id: str = "task-001",
    started_ts: str | None = None,
    extra: dict | None = None,
) -> PendingRun:
    """Build a PendingRun fixture with sane defaults.

    Defaults match the executor and task the conftest fixtures set up,
    so `_record_failed_run` can find the Executor row to bump.
    """
    return PendingRun(
        run_id=run_id,
        session_id=session_id,
        executor_id=executor_id,
        task_id=task_id,
        role_id="backend",
        model_tier="sonnet",
        started_ts=started_ts or datetime.now(timezone.utc).isoformat(),
        window_id="2026-05-19T07",
        estimated_tokens_in=500,
        extra=extra or {},
    )


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    """A `<tmp>/state/` directory matching the runtime layout."""
    s = tmp_path / "state"
    s.mkdir()
    return s


@pytest.fixture
def seeded_task(tmp_ledger):
    """Insert a Task row keyed `task-001` to satisfy the FK on `runs.task_id`."""
    tmp_ledger._conn.execute(
        "INSERT INTO tasks (task_id, task_type, status, created_ts) VALUES (?, ?, ?, ?)",
        ("task-001", "test", "in_progress", datetime.now(timezone.utc).isoformat()),
    )
    return "task-001"


def test_session_reconcile_records_failed_run(
    tmp_ledger, sample_executor, seeded_task, state_root
):
    """A sidecar whose session_id matches becomes a failed Run."""
    pending = _make_pending(
        executor_id=sample_executor.executor_id,
        task_id=seeded_task,
    )
    write_pending(state_root, pending)

    reconciled = reconcile_session_pending(tmp_ledger, state_root, "sess-A")

    assert len(reconciled) == 1
    assert reconciled[0].reason == REASON_SESSION_END
    assert reconciled[0].run_id == "run-001"

    runs = tmp_ledger.get_recent_runs(limit=10)
    assert len(runs) == 1
    assert runs[0].outcome == "interrupted"
    assert runs[0].failure_reason == REASON_SESSION_END
    assert runs[0].tokens_out == 0

    # Sidecar is gone.
    assert not (pending_dir(state_root) / "run-001.json").exists()


def test_session_reconcile_ignores_other_sessions(
    tmp_ledger, sample_executor, seeded_task, state_root
):
    pending = _make_pending(
        run_id="run-001",
        session_id="sess-A",
        executor_id=sample_executor.executor_id,
        task_id=seeded_task,
    )
    write_pending(state_root, pending)

    reconciled = reconcile_session_pending(tmp_ledger, state_root, "sess-B")

    assert reconciled == []
    # Sidecar untouched.
    assert (pending_dir(state_root) / "run-001.json").exists()
    assert len(tmp_ledger.get_recent_runs(limit=10)) == 0


def test_stale_reconcile_picks_old_sidecars(
    tmp_ledger, sample_executor, seeded_task, state_root
):
    """A sidecar older than max_age is reconciled with REASON_STALE."""
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    pending = _make_pending(
        executor_id=sample_executor.executor_id,
        task_id=seeded_task,
        started_ts=old_ts,
    )
    write_pending(state_root, pending)

    reconciled = reconcile_stale_pending(
        tmp_ledger, state_root, max_age=timedelta(hours=1)
    )

    assert len(reconciled) == 1
    assert reconciled[0].reason == REASON_STALE
    runs = tmp_ledger.get_recent_runs(limit=10)
    assert runs[0].outcome == "interrupted"
    assert runs[0].failure_reason == REASON_STALE


def test_stale_reconcile_marks_budget_exceeded_from_transcript(
    tmp_ledger, sample_executor, seeded_task, state_root, tmp_path
):
    """Budget-killed parent sessions should not be recorded as generic interruptions."""
    transcript = tmp_path / "parent.jsonl"
    transcript.write_text(
        '{"type":"result","subtype":"error_max_budget_usd",'
        '"errors":["Reached maximum budget ($0.30)"]}\n'
    )
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    pending = _make_pending(
        executor_id=sample_executor.executor_id,
        task_id=seeded_task,
        started_ts=old_ts,
        extra={"transcript_path": str(transcript)},
    )
    write_pending(state_root, pending)

    reconciled = reconcile_stale_pending(
        tmp_ledger, state_root, max_age=timedelta(hours=1)
    )

    assert len(reconciled) == 1
    assert reconciled[0].reason == REASON_BUDGET_EXCEEDED
    run = tmp_ledger.get_recent_runs(limit=1)[0]
    assert run.outcome == "budget_exceeded"
    assert run.failure_reason == REASON_BUDGET_EXCEEDED


def test_reconcile_marks_recording_failed_from_hook_log(
    tmp_ledger, sample_executor, seeded_task, state_root
):
    """If PostToolUse ran and logged failure, recovery must not blame the user."""
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    pending = _make_pending(
        run_id="run-post-task-failed",
        executor_id=sample_executor.executor_id,
        task_id=seeded_task,
        started_ts=old_ts,
    )
    write_pending(state_root, pending)
    hook_log = state_root.parent / "hooks" / "log.jsonl"
    hook_log.parent.mkdir(parents=True)
    hook_log.write_text(
        '{"ts":"2026-05-27T06:04:24+00:00","kind":"post-task-failed",'
        '"message":"Error binding parameter 4: type \'dict\' is not supported",'
        '"context":{"run_id":"run-post-task-failed"}}\n'
    )

    reconciled = reconcile_stale_pending(
        tmp_ledger, state_root, max_age=timedelta(hours=1)
    )

    assert len(reconciled) == 1
    run = tmp_ledger.get_recent_runs(limit=1)[0]
    assert run.outcome == "recording_failed"
    assert "Error binding parameter 4" in (run.failure_reason or "")


def test_reconcile_promotes_prefix_sidecar_with_structured_model(
    tmp_ledger, sample_executor, seeded_task, state_root
):
    """Upgrade path: unreconciled pre-fix sidecar becomes a normalized Run row."""
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    pending_dir(state_root).mkdir(parents=True)
    (pending_dir(state_root) / "run-prefix-recording-failed.json").write_text(
        json.dumps(
            {
                "run_id": "run-prefix-recording-failed",
                "session_id": "sess-prefix",
                "executor_id": sample_executor.executor_id,
                "task_id": seeded_task,
                "role_id": "backend",
                "model_tier": {"id": "claude-sonnet-4-5", "display_name": "Sonnet 4.5"},
                "started_ts": old_ts,
                "window_id": "2026-05-27T10",
                "estimated_tokens_in": 777,
            }
        )
    )
    hook_log = state_root.parent / "hooks" / "log.jsonl"
    hook_log.parent.mkdir(parents=True)
    hook_log.write_text(
        '{"ts":"2026-05-27T06:04:24+00:00","kind":"post-task-failed",'
        '"message":"Error binding parameter 4: type \'dict\' is not supported",'
        '"context":{"run_id":"run-prefix-recording-failed"}}\n'
    )

    reconciled = reconcile_stale_pending(
        tmp_ledger, state_root, max_age=timedelta(hours=1)
    )

    assert [r.run_id for r in reconciled] == ["run-prefix-recording-failed"]
    assert not (pending_dir(state_root) / "run-prefix-recording-failed.json").exists()
    run = tmp_ledger.get_recent_runs(limit=1)[0]
    assert run.outcome == "recording_failed"
    assert run.model == "claude-sonnet-4-5"
    assert run.requested_model_tier == "claude-sonnet-4-5"
    assert "Error binding parameter 4" in (run.failure_reason or "")


def test_stale_reconcile_skips_fresh_sidecars(
    tmp_ledger, sample_executor, seeded_task, state_root
):
    """Sidecars newer than max_age are left in place."""
    fresh_ts = datetime.now(timezone.utc).isoformat()
    pending = _make_pending(
        executor_id=sample_executor.executor_id,
        task_id=seeded_task,
        started_ts=fresh_ts,
    )
    write_pending(state_root, pending)

    reconciled = reconcile_stale_pending(
        tmp_ledger, state_root, max_age=timedelta(hours=1)
    )

    assert reconciled == []
    assert (pending_dir(state_root) / "run-001.json").exists()


def test_reconcile_updates_executor_last_seen(
    tmp_ledger, sample_executor, seeded_task, state_root
):
    """The Executor's `last_seen_ts` advances when its sidecar reconciles."""
    original_last_seen = sample_executor.last_seen_ts
    pending = _make_pending(
        executor_id=sample_executor.executor_id,
        task_id=seeded_task,
        started_ts=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
    )
    write_pending(state_root, pending)

    reconcile_stale_pending(tmp_ledger, state_root, max_age=timedelta(hours=1))

    row = tmp_ledger._conn.execute(
        "SELECT last_seen_ts FROM executors WHERE executor_id = ?",
        (sample_executor.executor_id,),
    ).fetchone()
    assert row["last_seen_ts"] != original_last_seen


def test_reconcile_persists_file_events_from_incomplete_run(
    tmp_ledger, sample_executor, seeded_task, state_root
):
    """If PostToolUse is skipped, reconcile still records file provenance."""
    started_ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    pending = _make_pending(
        executor_id=sample_executor.executor_id,
        task_id=seeded_task,
        started_ts=started_ts,
    )
    write_pending(state_root, pending)
    append_file_event(
        state_root,
        FileToolEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            session_id=pending.session_id,
            run_id=pending.run_id,
            tool_name="Edit",
            path="/tmp/project/index.html",
            rel_path="index.html",
            cwd="/tmp/project",
        ),
    )

    reconcile_session_pending(tmp_ledger, state_root, pending.session_id)

    run = tmp_ledger.get_recent_runs(limit=1)[0]
    assert run.outcome == "interrupted"
    assert run.files_touched_json == '["index.html"]'
    assert run.tool_use_json == '[{"name": "Edit", "count": 1, "input_tokens": 0}]'
    assert run.deliverable_ref_json
    assert not list((state_root / "file-events").glob("*.jsonl"))


def test_reconcile_idempotent_on_repeat(
    tmp_ledger, sample_executor, seeded_task, state_root
):
    """Running reconcile twice in a row records exactly one Run."""
    pending = _make_pending(
        executor_id=sample_executor.executor_id,
        task_id=seeded_task,
        started_ts=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
    )
    write_pending(state_root, pending)

    first = reconcile_stale_pending(tmp_ledger, state_root, max_age=timedelta(hours=1))
    second = reconcile_stale_pending(tmp_ledger, state_root, max_age=timedelta(hours=1))

    assert len(first) == 1
    assert len(second) == 0
    assert len(tmp_ledger.get_recent_runs(limit=10)) == 1


def test_migration_002_recognizes_new_outcomes(tmp_ledger):
    """The 002_outcome_extended migration adds `interrupted` + `budget_exceeded`.

    Pinning that the CHECK constraint accepts both new literals so a
    future migration cannot regress the contract by accident.
    """
    runs_ddl = tmp_ledger._conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='runs'"
    ).fetchone()[0]
    assert "'interrupted'" in runs_ddl
    assert "'budget_exceeded'" in runs_ddl


def test_reconcile_empty_state_returns_empty(tmp_ledger, state_root):
    """No sidecars → empty reconciliation list, no Ledger writes, no error."""
    assert reconcile_session_pending(tmp_ledger, state_root, "any") == []
    assert reconcile_stale_pending(tmp_ledger, state_root) == []
    assert len(tmp_ledger.get_recent_runs(limit=10)) == 0


def test_reconcile_handles_missing_executor(tmp_ledger, seeded_task, state_root):
    """Sidecar references an Executor that is not in the Ledger.

    The reconciler must still record the Run (the FK on `executor_id`
    means the Run write will fail unless the Executor exists). Today the
    behavior is that the Executor must exist; this test pins that
    contract so a future regression is caught.
    """
    pending = _make_pending(
        executor_id="ghost-executor",
        task_id=seeded_task,
        started_ts=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
    )
    write_pending(state_root, pending)

    # FK violation expected — current contract.
    with pytest.raises(Exception):
        reconcile_stale_pending(tmp_ledger, state_root, max_age=timedelta(hours=1))
