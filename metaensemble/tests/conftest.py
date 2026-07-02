"""Shared pytest fixtures for MetaEnsemble core tests."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from metaensemble.hooks import _common as hooks_common
from metaensemble.lib.ids import make_alias, uuid7
from metaensemble.lib.ledger import Executor, Ledger


MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "state" / "migrations" / "001_init.sql"
)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    """Quarantine every test from the developer's real user-level state.

    Two leak paths exist without this fixture:

      1. `lib/file_events.py` helpers default `home` to `Path.home()`, so
         any code path that writes an active-dispatch marker without an
         explicit `home=` lands in the REAL `~/.metaensemble/state/`
         (observed: the perf-hook subprocess benchmarks stranding an
         `unknown-session.json` marker there). Pointing HOME (and
         USERPROFILE, for Windows) at a per-test tmp dir isolates
         `Path.home()` for in-process code AND for subprocesses, which
         inherit os.environ.

      2. `hooks/_common.py:log_error` resolves `hooks_log_path()` at call
         time from the ambient project (cwd), so in-process error logging
         — e.g. reconcile's per-sidecar guard — appends fixture run ids to
         the REAL repo's `.metaensemble/hooks/log.jsonl` when pytest runs
         from the repo root. Patching `hooks_log_path` in its defining
         module redirects `log_error`, which looks the symbol up in its
         own module namespace on every call.

    Subprocess-based hook tests are unaffected by (2): they re-import
    `_common` and resolve the log from METAENSEMBLE_STATE_DIR, which
    those tests already set. Tests that build their own fake home simply
    override HOME again on top of this fixture.
    """
    home = tmp_path_factory.mktemp("isolated-home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    hook_log = home / ".metaensemble" / "hooks" / "log.jsonl"
    monkeypatch.setattr(hooks_common, "hooks_log_path", lambda: hook_log)
    return home


@pytest.fixture
def tmp_ledger(tmp_path):
    """Fresh Ledger with the initial migration applied. Cleans up after the test."""
    ledger = Ledger(
        db_path=tmp_path / "test.db",
        jsonl_path=tmp_path / "runs.jsonl",
    )
    ledger.initialize(MIGRATION_PATH.read_text())
    yield ledger
    ledger.close()


@pytest.fixture
def sample_role(tmp_ledger):
    """Insert a sample Role and return its role_id."""
    tmp_ledger._conn.execute(
        "INSERT INTO roles (role_id, version, spec_path, model_tier, created_ts) "
        "VALUES (?, ?, ?, ?, ?)",
        ("backend", "1.0.0", "roles/backend.md", "sonnet", datetime.now().isoformat()),
    )
    return "backend"


@pytest.fixture
def sample_executor(tmp_ledger, sample_role):
    """Insert a sample Executor and return it."""
    eid = str(uuid7())
    alias = make_alias("be", uuid7())
    now = datetime.now().isoformat()
    executor = Executor(
        executor_id=eid,
        alias=alias,
        role_id=sample_role,
        parent_executor_id=None,
        created_ts=now,
        last_seen_ts=now,
        status="active",
    )
    tmp_ledger.upsert_executor(executor)
    return executor


@pytest.fixture
def sample_task(tmp_ledger):
    """Insert a sample Task and return its task_id."""
    tid = "test-task-1"
    tmp_ledger._conn.execute(
        "INSERT INTO tasks (task_id, task_type, status, created_ts) VALUES (?, ?, ?, ?)",
        (tid, "test", "open", datetime.now().isoformat()),
    )
    return tid
