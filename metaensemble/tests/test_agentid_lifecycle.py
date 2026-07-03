"""agentId-keyed background dispatch lifecycle.

Covers the case session-keying provably cannot handle: fan-out / concurrent
same-session dispatches. Correlation is by the per-dispatch agentId
(reconciled from the pre_task stamp via tool_use_id), never by session.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


from metaensemble.lib.file_events import (
    ActiveDispatch,
    agent_active_dispatch_path,
    read_active_dispatch_by_agent,
    write_active_dispatch_by_agent,
)
from metaensemble.lib.ledger import Executor, Ledger
from metaensemble.lib.sidecar import (
    PendingRun,
    pending_by_tool_use_id,
    pending_dir,
    write_pending,
)

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
MIGRATION = Path(__file__).resolve().parent.parent / "state" / "migrations" / "001_init.sql"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_ledger(state_root: Path) -> None:
    state_root.mkdir(parents=True, exist_ok=True)
    L = Ledger(db_path=state_root / "department.db", jsonl_path=state_root / "runs.jsonl")
    L.initialize(MIGRATION.read_text())
    L._conn.execute(
        "INSERT INTO roles (role_id, version, spec_path, model_tier, created_ts) VALUES (?,?,?,?,?)",
        ("backend", "1.0.0", "roles/backend.md", "sonnet", _now()),
    )
    L._conn.execute(
        "INSERT INTO tasks (task_id, task_type, status, created_ts) VALUES (?,?,?,?)",
        ("task-1", "test", "in_progress", _now()),
    )
    L._conn.commit()
    L.upsert_executor(Executor("exec-1", "be-1", "backend", None, _now(), _now(), "active"))
    L.close()


def _pending(run_id: str, tool_use_id: str, session: str = "sess-A") -> PendingRun:
    return PendingRun(
        run_id=run_id, session_id=session, executor_id="exec-1", task_id="task-1",
        role_id="backend", model_tier="sonnet", started_ts=_now(),
        window_id="2026-05-19T07", estimated_tokens_in=100,
        extra={"tool_use_id": tool_use_id},
    )


def _invoke(hook: str, payload: dict, state_root: Path, home: Path):
    env = os.environ.copy()
    env["METAENSEMBLE_STATE_DIR"] = str(state_root)
    env["PYTHONPATH"] = str(HOOKS.parent.parent)
    env["HOME"] = str(home)
    p = subprocess.run(
        [sys.executable, str(HOOKS / hook)],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
        cwd=str(state_root.parent),
    )
    return p.returncode, p.stdout, p.stderr


# --- lib-level: correlation by tool_use_id distinguishes fan-out -----------

def test_pending_by_tool_use_id_distinguishes_fanout(tmp_path):
    state_root = tmp_path / "state"
    (state_root).mkdir()
    write_pending(state_root, _pending("r1", "T1"))
    write_pending(state_root, _pending("r2", "T2"))  # same session, newer
    assert pending_by_tool_use_id(state_root, "T1").run_id == "r1"
    assert pending_by_tool_use_id(state_root, "T2").run_id == "r2"
    assert pending_by_tool_use_id(state_root, "nope") is None


def test_agent_marker_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    a = ActiveDispatch("sess-A", "r1", "/proj", "/proj/.metaensemble/state", _now(),
                       agent_id="AG1", tool_use_id="T1")
    write_active_dispatch_by_agent(a)
    got = read_active_dispatch_by_agent("AG1")
    assert got is not None and got.run_id == "r1" and got.agent_id == "AG1"
    assert read_active_dispatch_by_agent("AG2") is None


# --- hook-level: defer, finalize, and fan-out correctness ------------------

def test_post_task_agent_defers_and_indexes_by_agentid(tmp_path):
    state_root = tmp_path / "state"
    _seed_ledger(state_root)
    write_pending(state_root, _pending("r1", "T1"))

    code, _, _ = _invoke("post_task.py", {
        "tool_name": "Agent",
        "tool_response": {"agentId": "AG1", "isAsync": True},
        "tool_use_id": "T1", "session_id": "sess-A", "cwd": str(state_root.parent),
    }, state_root, home=tmp_path)
    assert code == 0

    # agentId-keyed marker written, pointing at the right run; NOT finalized.
    marker = agent_active_dispatch_path("AG1", home=tmp_path)
    assert marker.exists()
    assert json.loads(marker.read_text())["run_id"] == "r1"
    assert (pending_dir(state_root) / "r1.json").exists()          # deferred
    L = Ledger(db_path=state_root / "department.db", jsonl_path=state_root / "runs.jsonl")
    assert L.get_recent_runs(limit=10) == []                       # no run yet
    L.close()


def test_subagent_stop_finalizes_by_agentid(tmp_path):
    state_root = tmp_path / "state"
    _seed_ledger(state_root)
    write_pending(state_root, _pending("r1", "T1"))
    write_active_dispatch_by_agent(ActiveDispatch(
        "sess-A", "r1", str(state_root.parent), str(state_root), _now(),
        agent_id="AG1", tool_use_id="T1"), home=tmp_path)

    code, _, _ = _invoke("subagent_stop.py", {
        "agent_id": "AG1", "session_id": "sess-A",
        "last_assistant_message": "done", "cwd": str(state_root.parent),
    }, state_root, home=tmp_path)
    assert code == 0

    L = Ledger(db_path=state_root / "department.db", jsonl_path=state_root / "runs.jsonl")
    runs = L.get_recent_runs(limit=10)
    L.close()
    assert len(runs) == 1 and runs[0].run_id == "r1" and runs[0].outcome == "ok"
    assert not (pending_dir(state_root) / "r1.json").exists()       # sidecar gone
    assert not agent_active_dispatch_path("AG1", home=tmp_path).exists()  # marker cleared


def test_fanout_two_same_session_dispatches_do_not_cross_correlate(tmp_path):
    """THE case session-keying gets wrong: two concurrent dispatches in one
    session. Each must finalize its OWN run, correlated by agentId/tool_use_id."""
    state_root = tmp_path / "state"
    _seed_ledger(state_root)
    write_pending(state_root, _pending("r1", "T1", session="sess-A"))
    write_pending(state_root, _pending("r2", "T2", session="sess-A"))  # SAME session

    # Two launches, different agentIds + tool_use_ids.
    _invoke("post_task.py", {"tool_name": "Agent", "tool_response": {"agentId": "AG1"},
            "tool_use_id": "T1", "session_id": "sess-A", "cwd": str(state_root.parent)},
            state_root, home=tmp_path)
    _invoke("post_task.py", {"tool_name": "Agent", "tool_response": {"agentId": "AG2"},
            "tool_use_id": "T2", "session_id": "sess-A", "cwd": str(state_root.parent)},
            state_root, home=tmp_path)

    # Each marker points at the CORRECT run (no swap, no "newest wins").
    assert json.loads(agent_active_dispatch_path("AG1", home=tmp_path).read_text())["run_id"] == "r1"
    assert json.loads(agent_active_dispatch_path("AG2", home=tmp_path).read_text())["run_id"] == "r2"

    # Each subagent stop finalizes its OWN run.
    _invoke("subagent_stop.py", {"agent_id": "AG1", "session_id": "sess-A",
            "last_assistant_message": "one", "cwd": str(state_root.parent)}, state_root, home=tmp_path)
    _invoke("subagent_stop.py", {"agent_id": "AG2", "session_id": "sess-A",
            "last_assistant_message": "two", "cwd": str(state_root.parent)}, state_root, home=tmp_path)

    L = Ledger(db_path=state_root / "department.db", jsonl_path=state_root / "runs.jsonl")
    ids = sorted(r.run_id for r in L.get_recent_runs(limit=10))
    L.close()
    assert ids == ["r1", "r2"]   # both finalized, each exactly once
    assert not any(pending_dir(state_root).glob("*"))


def test_file_event_authorizes_subagent_write_by_agentid(tmp_path):
    """A subagent Write carrying agent_id is authorized against the agentId
    marker's project root."""
    state_root = tmp_path / "state"
    proj = tmp_path / "proj"
    (proj / ".metaensemble").mkdir(parents=True)
    write_active_dispatch_by_agent(ActiveDispatch(
        "sess-A", "r1", str(proj), str(state_root), _now(),
        agent_id="AG1", tool_use_id="T1"), home=tmp_path)

    code, out, _ = _invoke("file_event.py", {
        "tool_name": "Write", "hook_event_name": "PreToolUse",
        "agent_id": "AG1", "session_id": "sess-A", "cwd": str(proj),
        "tool_input": {"file_path": str(proj / ".metaensemble/reports/x.md")},
    }, state_root, home=tmp_path)
    assert code == 0
    assert json.loads(out).get("continue") is True   # authorized, not blocked


# --- marker/state-leak regressions -----------------------------------------

def test_project_fallback_ignores_agent_markers(tmp_path, monkeypatch):
    """An agentId-keyed marker must not satisfy the legacy project fallback."""
    from metaensemble.lib.file_events import read_active_dispatch_for_project
    monkeypatch.setenv("HOME", str(tmp_path))
    write_active_dispatch_by_agent(ActiveDispatch(
        "sess-A", "r1", str(tmp_path / "proj"), str(tmp_path / "state"), _now(),
        agent_id="AG1", tool_use_id="T1"))
    assert read_active_dispatch_for_project(tmp_path / "proj") is None


def test_clear_for_run_clears_both_indexes_guarded(tmp_path, monkeypatch):
    """Run cleanup clears matching session + agent markers, guarded by run_id."""
    from metaensemble.lib.file_events import (
        write_active_dispatch, read_active_dispatch, clear_active_dispatch_for_run,
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    write_active_dispatch(ActiveDispatch("sess-A", "r1", "/p", "/p/s", _now()))
    write_active_dispatch_by_agent(ActiveDispatch(
        "sess-A", "r1", "/p", "/p/s", _now(), agent_id="AG1", tool_use_id="T1"))
    assert clear_active_dispatch_for_run("r1", session_id="sess-A") == 2
    assert read_active_dispatch("sess-A") is None
    assert read_active_dispatch_by_agent("AG1") is None
    # guard: a marker for a DIFFERENT run is preserved
    write_active_dispatch(ActiveDispatch("sess-B", "OTHER", "/p", "/p/s", _now()))
    assert clear_active_dispatch_for_run("r1", session_id="sess-B") == 0
    assert read_active_dispatch("sess-B") is not None


def test_subagent_stop_clears_both_session_and_agent_markers(tmp_path):
    """Background finalization clears both session and agent markers."""
    from metaensemble.lib.file_events import write_active_dispatch, read_active_dispatch
    state_root = tmp_path / "state"
    _seed_ledger(state_root)
    write_pending(state_root, _pending("r1", "T1"))
    write_active_dispatch(ActiveDispatch(
        "sess-A", "r1", str(state_root.parent), str(state_root), _now()), home=tmp_path)
    write_active_dispatch_by_agent(ActiveDispatch(
        "sess-A", "r1", str(state_root.parent), str(state_root), _now(),
        agent_id="AG1", tool_use_id="T1"), home=tmp_path)

    _invoke("subagent_stop.py", {"agent_id": "AG1", "session_id": "sess-A",
            "last_assistant_message": "done", "cwd": str(state_root.parent)},
            state_root, home=tmp_path)

    assert read_active_dispatch("sess-A", home=tmp_path) is None        # session cleared
    assert read_active_dispatch_by_agent("AG1", home=tmp_path) is None  # agent cleared


def test_reconcile_clears_agent_marker_for_recorded_run(tmp_path, monkeypatch):
    """Duplicate-run cleanup clears agentId markers as well as sidecars."""
    from datetime import timedelta
    from metaensemble.lib.reconcile import reconcile_stale_pending
    from metaensemble.lib.ledger import Ledger, Run
    monkeypatch.setenv("HOME", str(tmp_path))
    state_root = tmp_path / "state"
    _seed_ledger(state_root)
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    write_pending(state_root, PendingRun(
        run_id="r1", session_id="sess-A", executor_id="exec-1", task_id="task-1",
        role_id="backend", model_tier="sonnet", started_ts=old,
        window_id="2026-05-19T07", estimated_tokens_in=1, extra={"tool_use_id": "T1"}))
    L = Ledger(db_path=state_root / "department.db", jsonl_path=state_root / "runs.jsonl")
    L.append_run(Run(run_id="r1", executor_id="exec-1", task_id="task-1", model="sonnet",
                     tokens_in=1, tokens_out=1, window_id="2026-05-19T07",
                     started_ts=old, ended_ts=old, outcome="ok"))
    write_active_dispatch_by_agent(ActiveDispatch(
        "sess-A", "r1", str(state_root.parent), str(state_root), old,
        agent_id="AG1", tool_use_id="T1"), home=tmp_path)

    reconcile_stale_pending(L, state_root, max_age=timedelta(hours=1))
    L.close()
    assert read_active_dispatch_by_agent("AG1", home=tmp_path) is None  # agent marker cleared


def test_direct_write_blocked_when_only_agent_marker_present(tmp_path):
    """Direct coordinator edits cannot ride an unrelated live agent marker."""
    state_root = tmp_path / "state"
    proj = tmp_path / "proj"
    (proj / ".metaensemble").mkdir(parents=True)
    write_active_dispatch_by_agent(ActiveDispatch(
        "sess-A", "r1", str(proj), str(state_root), _now(),
        agent_id="AG1", tool_use_id="T1"), home=tmp_path)
    tdir = tmp_path / ".claude" / "projects" / "enc"
    tdir.mkdir(parents=True)
    tfile = tdir / "sess.jsonl"
    tfile.write_text(json.dumps({"type": "user", "message": {
        "role": "user", "content": "<command-name>/dispatch</command-name>"}}) + "\n")

    code, out, _ = _invoke("file_event.py", {
        "tool_name": "Edit", "hook_event_name": "PreToolUse", "session_id": "sess-A",
        "cwd": str(proj), "transcript_path": str(tfile),
        "tool_input": {"file_path": str(proj / "x.py")},
    }, state_root, home=tmp_path)
    assert code == 2 and json.loads(out).get("continue") is False
