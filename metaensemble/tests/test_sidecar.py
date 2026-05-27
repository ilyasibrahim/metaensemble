"""Unit tests for the pending-Run sidecar (`metaensemble.lib.sidecar`)."""
from __future__ import annotations

from pathlib import Path

from metaensemble.lib.sidecar import (
    PendingRun,
    delete_pending,
    latest_pending_for_session,
    pending_dir,
    read_pending,
    write_pending,
)


def _sample(**overrides) -> PendingRun:
    base = dict(
        run_id="run-001",
        session_id="sess-A",
        executor_id="exec-1",
        task_id="task-001",
        role_id="backend",
        model_tier="sonnet",
        started_ts="2026-05-14T10:00:00",
        window_id="2026-05-14T10",
        estimated_tokens_in=500,
    )
    base.update(overrides)
    return PendingRun(**base)


def test_write_then_read_roundtrips(tmp_path: Path):
    state = tmp_path / "state"
    pending = _sample()
    write_pending(state, pending)
    loaded = read_pending(state, pending.run_id)
    assert loaded is not None
    assert loaded.run_id == pending.run_id
    assert loaded.session_id == pending.session_id
    assert loaded.estimated_tokens_in == 500


def test_read_pending_returns_none_when_absent(tmp_path: Path):
    assert read_pending(tmp_path / "state", "nonexistent") is None


def test_latest_pending_for_session_picks_newest(tmp_path: Path):
    state = tmp_path / "state"
    write_pending(state, _sample(run_id="run-001", session_id="sess-A"))
    write_pending(state, _sample(run_id="run-002", session_id="sess-A"))
    write_pending(state, _sample(run_id="run-003", session_id="sess-B"))
    latest = latest_pending_for_session(state, "sess-A")
    assert latest is not None
    assert latest.run_id == "run-002"


def test_latest_pending_for_session_ignores_other_sessions(tmp_path: Path):
    state = tmp_path / "state"
    write_pending(state, _sample(run_id="run-001", session_id="sess-A"))
    assert latest_pending_for_session(state, "sess-other") is None


def test_delete_pending_removes_file(tmp_path: Path):
    state = tmp_path / "state"
    write_pending(state, _sample())
    assert delete_pending(state, "run-001") is True
    assert delete_pending(state, "run-001") is False
    assert not (pending_dir(state) / "run-001.json").exists()


def test_pending_dir_is_under_state(tmp_path: Path):
    assert pending_dir(tmp_path) == tmp_path / "pending"
