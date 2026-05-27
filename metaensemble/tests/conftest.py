"""Shared pytest fixtures for MetaEnsemble core tests."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from metaensemble.lib.ids import make_alias, uuid7
from metaensemble.lib.ledger import Executor, Ledger


MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "state" / "migrations" / "001_init.sql"
)


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
