"""Tests for Ledger CRUD and replay (metaensemble/lib/ledger.py)."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from metaensemble.lib.ids import make_alias, uuid7
from metaensemble.lib.ledger import Executor, Ledger, Run


MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "state" / "migrations" / "001_init.sql"
)


def _make_run(executor_id: str, task_id: str, **overrides) -> Run:
    now = datetime.now().isoformat()
    base = dict(
        run_id=str(uuid7()),
        executor_id=executor_id,
        task_id=task_id,
        model="sonnet",
        tokens_in=1000,
        tokens_out=500,
        window_id="2026-05-09T00",
        started_ts=now,
        ended_ts=now,
        outcome="ok",
    )
    base.update(overrides)
    return Run(**base)


def test_append_run_persists_to_sqlite(tmp_ledger, sample_executor, sample_task):
    run = _make_run(sample_executor.executor_id, sample_task)
    tmp_ledger.append_run(run)

    runs = tmp_ledger.get_recent_runs(limit=10)
    assert len(runs) == 1
    assert runs[0].run_id == run.run_id


def test_append_run_appends_to_jsonl(tmp_ledger, sample_executor, sample_task):
    run = _make_run(sample_executor.executor_id, sample_task)
    tmp_ledger.append_run(run)

    lines = tmp_ledger.jsonl_path.read_text().strip().splitlines()
    assert len(lines) == 1
    assert run.run_id in lines[0]


def test_append_run_is_idempotent_by_run_id(tmp_ledger, sample_executor, sample_task):
    """A second append of the same run_id must not raise and must report no
    insert (returns False), leaving exactly one row. Guards the
    'UNIQUE constraint failed: runs.run_id' crash class."""
    run = _make_run(sample_executor.executor_id, sample_task)
    assert tmp_ledger.append_run(run) is True
    assert tmp_ledger.append_run(run) is False
    rows = [r for r in tmp_ledger.get_recent_runs(limit=10) if r.run_id == run.run_id]
    assert len(rows) == 1


def test_append_run_duplicate_does_not_duplicate_jsonl(
    tmp_ledger, sample_executor, sample_task
):
    """The JSONL mirror is appended only on a real insert, so a duplicate
    append must not add a second mirror line."""
    run = _make_run(sample_executor.executor_id, sample_task)
    tmp_ledger.append_run(run)
    tmp_ledger.append_run(run)
    lines = tmp_ledger.jsonl_path.read_text().strip().splitlines()
    assert sum(1 for ln in lines if run.run_id in ln) == 1


def test_run_exists_reflects_persistence(tmp_ledger, sample_executor, sample_task):
    run = _make_run(sample_executor.executor_id, sample_task)
    assert tmp_ledger.run_exists(run.run_id) is False
    tmp_ledger.append_run(run)
    assert tmp_ledger.run_exists(run.run_id) is True


def test_get_recent_runs_orders_descending(tmp_ledger, sample_executor, sample_task):
    base = datetime.now()
    for i in range(5):
        run = _make_run(
            sample_executor.executor_id, sample_task,
            ended_ts=(base + timedelta(seconds=i)).isoformat(),
        )
        tmp_ledger.append_run(run)

    runs = tmp_ledger.get_recent_runs(limit=10)
    timestamps = [r.ended_ts for r in runs]
    assert timestamps == sorted(timestamps, reverse=True)


def test_get_recent_runs_respects_limit(tmp_ledger, sample_executor, sample_task):
    for _ in range(10):
        tmp_ledger.append_run(_make_run(sample_executor.executor_id, sample_task))
    runs = tmp_ledger.get_recent_runs(limit=3)
    assert len(runs) == 3


def test_get_runs_by_executor_filters_correctly(
    tmp_ledger, sample_executor, sample_task, sample_role
):
    # Add a second executor under the same role.
    other = Executor(
        executor_id=str(uuid7()),
        alias=make_alias("xx", uuid7()),
        role_id=sample_role,
        parent_executor_id=None,
        created_ts=datetime.now().isoformat(),
        last_seen_ts=datetime.now().isoformat(),
        status="active",
    )
    tmp_ledger.upsert_executor(other)

    for _ in range(3):
        tmp_ledger.append_run(_make_run(sample_executor.executor_id, sample_task))
    for _ in range(2):
        tmp_ledger.append_run(_make_run(other.executor_id, sample_task))

    own = tmp_ledger.get_runs_by_executor(sample_executor.executor_id)
    other_runs = tmp_ledger.get_runs_by_executor(other.executor_id)
    assert len(own) == 3
    assert len(other_runs) == 2


def test_get_window_burn_aggregates_correctly(
    tmp_ledger, sample_executor, sample_task
):
    for _ in range(3):
        tmp_ledger.append_run(_make_run(
            sample_executor.executor_id, sample_task,
            window_id="2026-05-09T00",
            tokens_in=100, tokens_out=50,
        ))
    for _ in range(2):
        tmp_ledger.append_run(_make_run(
            sample_executor.executor_id, sample_task,
            window_id="2026-05-09T01",
            tokens_in=200, tokens_out=100,
        ))

    s1 = tmp_ledger.get_window_burn("2026-05-09T00")
    assert s1.total_runs == 3
    assert s1.total_tokens_in == 300
    assert s1.total_tokens_out == 150

    s2 = tmp_ledger.get_window_burn("2026-05-09T01")
    assert s2.total_runs == 2
    assert s2.total_tokens_in == 400


def test_get_executor_by_alias_finds_match(tmp_ledger, sample_executor):
    found = tmp_ledger.get_executor_by_alias(sample_executor.alias)
    assert found is not None
    assert found.executor_id == sample_executor.executor_id


def test_get_executor_by_alias_returns_none_for_missing(tmp_ledger):
    assert tmp_ledger.get_executor_by_alias("nope-aaa") is None


def test_get_active_executors_filters_by_recency(tmp_ledger, sample_role):
    base = datetime.now()
    fresh_id = str(uuid7())
    stale_id = str(uuid7())
    tmp_ledger.upsert_executor(Executor(
        executor_id=fresh_id, alias="fresh-aaa", role_id=sample_role,
        parent_executor_id=None,
        created_ts=base.isoformat(),
        last_seen_ts=base.isoformat(),
        status="active",
    ))
    tmp_ledger.upsert_executor(Executor(
        executor_id=stale_id, alias="stale-bbb", role_id=sample_role,
        parent_executor_id=None,
        created_ts=(base - timedelta(days=10)).isoformat(),
        last_seen_ts=(base - timedelta(days=10)).isoformat(),
        status="idle",
    ))
    cutoff = base - timedelta(days=1)
    active = tmp_ledger.get_active_executors(since=cutoff)
    aliases = {e.alias for e in active}
    assert "fresh-aaa" in aliases
    assert "stale-bbb" not in aliases


def test_replay_from_jsonl_is_idempotent(
    tmp_ledger, sample_executor, sample_task, tmp_path
):
    # Populate the original ledger.
    for _ in range(5):
        tmp_ledger.append_run(_make_run(
            sample_executor.executor_id,
            sample_task,
            role_version="1.0.0",
            requested_model_tier="sonnet",
            files_touched_json='["src/app.py"]',
            tool_use_json='[{"name":"Edit","count":1}]',
            cache_read_tokens=7,
            cache_create_tokens=3,
        ))
    original_jsonl = tmp_ledger.jsonl_path.read_text()

    # Build a fresh DB pointing at the same JSONL.
    new_ledger = Ledger(
        db_path=tmp_path / "replay.db",
        jsonl_path=tmp_ledger.jsonl_path,
    )
    new_ledger.initialize(MIGRATION_PATH.read_text())

    # Restore role + executor + task before replay so FKs hold.
    new_ledger._conn.execute(
        "INSERT INTO roles (role_id, version, spec_path, model_tier, created_ts) "
        "VALUES (?, ?, ?, ?, ?)",
        (sample_executor.role_id, "1.0.0", "roles/backend.md", "sonnet",
         datetime.now().isoformat()),
    )
    new_ledger.upsert_executor(sample_executor)
    new_ledger._conn.execute(
        "INSERT INTO tasks (task_id, task_type, status, created_ts) "
        "VALUES (?, ?, ?, ?)",
        (sample_task, "test", "open", datetime.now().isoformat()),
    )

    count1 = new_ledger.replay_from_jsonl()
    count2 = new_ledger.replay_from_jsonl()
    assert count1 == 5
    assert count2 == 5  # All lines processed; INSERT OR IGNORE prevents dupes.

    runs = new_ledger.get_recent_runs(limit=20)
    assert len(runs) == 5
    assert runs[0].role_version == "1.0.0"
    assert runs[0].requested_model_tier == "sonnet"
    assert runs[0].files_touched_json == '["src/app.py"]'
    assert runs[0].cache_read_tokens == 7

    new_ledger.close()
    # JSONL must be unchanged by replay.
    assert tmp_ledger.jsonl_path.read_text() == original_jsonl


def test_failure_reason_round_trips_through_ledger(
    tmp_ledger, sample_executor, sample_task
):
    """A Run with a non-None failure_reason persists and reads back correctly.

    Locks both write paths (`append_run`) and read paths (`Run(**dict(row))`
    in `get_recent_runs`) against the new column. Without this test, a
    regression that silently drops failure_reason from the INSERT or the
    SELECT projection would not be caught.
    """
    run = _make_run(
        sample_executor.executor_id,
        sample_task,
        outcome="failed",
        failure_reason="timeout",
    )
    tmp_ledger.append_run(run)

    runs = tmp_ledger.get_recent_runs(limit=5)
    assert len(runs) == 1
    assert runs[0].failure_reason == "timeout"
    assert runs[0].outcome == "failed"


def test_append_run_rejects_non_scalar_model(tmp_ledger, sample_executor, sample_task):
    """Runtime objects must fail before sqlite binding, not inside sqlite3."""
    run = _make_run(
        sample_executor.executor_id,
        sample_task,
        model={"id": "claude-opus-4-7"},  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="model"):
        tmp_ledger.append_run(run)

    assert tmp_ledger.get_recent_runs(limit=10) == []


def test_recording_failed_outcome_round_trips(
    tmp_ledger, sample_executor, sample_task
):
    run = _make_run(
        sample_executor.executor_id,
        sample_task,
        outcome="recording_failed",
        failure_reason="post-task recording failed: bind error",
    )

    tmp_ledger.append_run(run)

    stored = tmp_ledger.get_recent_runs(limit=1)[0]
    assert stored.outcome == "recording_failed"
    assert stored.failure_reason == "post-task recording failed: bind error"


def test_initialize_reclassifies_mislabeled_recording_salvage(tmp_path):
    """Upgrade path: interrupted salvage + matching hook log becomes recording_failed."""
    project = tmp_path / "project"
    state = project / ".metaensemble" / "state"
    hooks = project / ".metaensemble" / "hooks"
    state.mkdir(parents=True)
    hooks.mkdir(parents=True)
    ledger = Ledger(db_path=state / "department.db", jsonl_path=state / "runs.jsonl")
    ledger.initialize(MIGRATION_PATH.read_text())
    ledger.ensure_role(
        role_id="backend",
        version="1.0.0",
        spec_path="roles/backend.md",
        model_tier="sonnet",
    )
    executor = Executor(
        executor_id="exec-1",
        alias="be-001",
        role_id="backend",
        parent_executor_id=None,
        created_ts=datetime.now().isoformat(),
        last_seen_ts=datetime.now().isoformat(),
        status="active",
    )
    ledger.upsert_executor(executor)
    ledger.ensure_task(task_id="task-1", task_type="test", status="open")
    ledger.append_run(_make_run(
        executor.executor_id,
        "task-1",
        run_id="run-recording-bug",
        outcome="interrupted",
        failure_reason="session ended before PostToolUse",
    ))
    ledger.close()
    (hooks / "log.jsonl").write_text(
        '{"ts":"2026-05-27T06:04:24+00:00","kind":"post-task-failed",'
        '"message":"Error binding parameter 4: type \'dict\' is not supported",'
        '"context":{"run_id":"run-recording-bug"}}\n'
    )

    upgraded = Ledger(db_path=state / "department.db", jsonl_path=state / "runs.jsonl")
    upgraded.initialize(MIGRATION_PATH.read_text())

    stored = upgraded.get_recent_runs(limit=1)[0]
    assert stored.outcome == "recording_failed"
    assert "Error binding parameter 4" in (stored.failure_reason or "")
    upgraded.close()


def test_initialize_backfills_failure_reason_on_pre_existing_ledger(tmp_path):
    """A Ledger created without the failure_reason column gains it on next init.

    Simulates a v0.1-shape database that pre-dates the failure_reason
    column. Re-running `initialize` should add the column via the
    PRAGMA-table_info-driven backfill path, with no error and no
    re-issued ALTER on the second run.
    """
    import sqlite3

    db_path = tmp_path / "old.db"
    jsonl_path = tmp_path / "old.jsonl"

    # Pre-migration shape: runs table without failure_reason.
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE roles (
          role_id TEXT PRIMARY KEY, version TEXT, spec_path TEXT,
          model_tier TEXT, created_ts TEXT
        );
        CREATE TABLE executors (
          executor_id TEXT PRIMARY KEY, alias TEXT, role_id TEXT,
          parent_executor_id TEXT, created_ts TEXT, last_seen_ts TEXT, status TEXT
        );
        CREATE TABLE tasks (
          task_id TEXT PRIMARY KEY, task_type TEXT, status TEXT,
          manifest_path TEXT, parent_task_id TEXT, created_ts TEXT
        );
        CREATE TABLE runs (
          run_id TEXT PRIMARY KEY, executor_id TEXT, task_id TEXT, model TEXT,
          tokens_in INTEGER, tokens_out INTEGER, window_id TEXT,
          started_ts TEXT, ended_ts TEXT, outcome TEXT,
          brief_in_path TEXT, brief_out_path TEXT, deliverable_path TEXT
        );
        """
    )
    conn.close()

    ledger = Ledger(db_path=db_path, jsonl_path=jsonl_path)
    ledger.initialize(MIGRATION_PATH.read_text())

    cols = {
        row[1]
        for row in ledger._conn.execute("PRAGMA table_info(runs)").fetchall()
    }
    assert "failure_reason" in cols
    assert "role_version" in cols
    assert "tool_use_json" in cols

    # Re-initializing must be a no-op (the column already exists).
    ledger.initialize(MIGRATION_PATH.read_text())
    cols_again = {
        row[1]
        for row in ledger._conn.execute("PRAGMA table_info(runs)").fetchall()
    }
    assert cols == cols_again

    ledger.close()
