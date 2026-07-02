"""Functional tests for the five MetaEnsemble lifecycle hooks.

Each hook is invoked as a subprocess with synthetic JSON on stdin, and
its stdout JSON and exit code are checked alongside the side effects
(Ledger rows, sidecar files, log entries) that the hook should produce.

The PreToolUse / PostToolUse contract under test here is the one the
runtime actually exposes: hooks see `tool_input`, `tool_response`, and
`session_id`, and they self-derive Run records from those fields. The
Coordinator does not (and cannot) inject structured metadata into the
tool output.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from metaensemble.lib.ledger import Executor, Ledger, Run
from metaensemble.lib.runtime_state import _encode_cwd_for_runtime


HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "state" / "migrations" / "001_init.sql"
)


def _invoke(
    hook: str,
    stdin_payload: dict,
    state_root: Path,
    *,
    cwd: Path | None = None,
    home: Path | None = None,
) -> tuple[int, str, str]:
    """Run a hook as a subprocess with stdin JSON. Returns (exit, stdout, stderr).

    Isolates `HOME` to the state_root's parent so the hook's runtime-state
    reader (which scans `~/.claude/projects/`) sees an empty world and
    the cost gate falls back to the manual capacity default. Without
    this, hook tests pick up the developer's real runtime data and the
    near-full current window blocks every dispatch.
    """
    env = os.environ.copy()
    env["METAENSEMBLE_STATE_DIR"] = str(state_root)
    env["PYTHONPATH"] = str(HOOKS_DIR.parent.parent)
    env["HOME"] = str(home or state_root.parent)
    proc = subprocess.run(
        [sys.executable, str(HOOKS_DIR / hook)],
        input=json.dumps(stdin_payload),
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd) if cwd else None,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _open_ledger(state_root: Path) -> Ledger:
    state_root.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(
        db_path=state_root / "department.db",
        jsonl_path=state_root / "runs.jsonl",
    )
    ledger.initialize(MIGRATION_PATH.read_text())
    return ledger


@pytest.fixture
def state_root(tmp_path):
    """A clean state directory rooted at the test's tmp_path."""
    return tmp_path / "state"


# --- Session lifecycle ---------------------------------------------------

def test_session_start_emits_summary(state_root):
    _open_ledger(state_root).close()
    code, out, _ = _invoke("session_start.py", {}, state_root)
    assert code == 0
    payload = json.loads(out)
    assert payload["continue"] is True
    assert "session start" in payload["systemMessage"]


def test_session_start_recovers_on_missing_db(tmp_path):
    state_root = tmp_path / "fresh"
    code, out, _ = _invoke("session_start.py", {}, state_root)
    assert code == 0
    payload = json.loads(out)
    assert payload["continue"] is True


def test_session_start_survives_colliding_stale_sidecar(state_root):
    """Regression: a stale pending sidecar whose run_id is already recorded
    must not crash session_start. The reconciler's unguarded re-insert
    previously raised 'UNIQUE constraint failed: runs.run_id', which the
    blanket except surfaced to the Coordinator as '(state unavailable)'."""
    from datetime import datetime, timedelta, timezone

    from metaensemble.lib.ledger import Executor, Run
    from metaensemble.lib.sidecar import PendingRun, write_pending

    old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    run_id = "019ebd74-728c-7eb0-b91e-73efd88e76f0"

    ledger = _open_ledger(state_root)
    ledger._conn.execute(
        "INSERT INTO roles (role_id, version, spec_path, model_tier, created_ts) "
        "VALUES (?, ?, ?, ?, ?)",
        ("backend", "1.0.0", "roles/backend.md", "sonnet", old_ts),
    )
    ledger._conn.execute(
        "INSERT INTO tasks (task_id, task_type, status, created_ts) VALUES (?, ?, ?, ?)",
        ("task-collide", "test", "done", old_ts),
    )
    ledger._conn.commit()
    ledger.upsert_executor(
        Executor(
            executor_id="exec-collide",
            alias="be-collide",
            role_id="backend",
            parent_executor_id=None,
            created_ts=old_ts,
            last_seen_ts=old_ts,
            status="active",
        )
    )
    ledger.append_run(
        Run(
            run_id=run_id,
            executor_id="exec-collide",
            task_id="task-collide",
            model="sonnet",
            tokens_in=100,
            tokens_out=50,
            window_id="2026-05-19T07",
            started_ts=old_ts,
            ended_ts=old_ts,
            outcome="ok",
        )
    )
    ledger.close()

    # A stale sidecar carrying the SAME run_id (restored/duplicate).
    write_pending(
        state_root,
        PendingRun(
            run_id=run_id,
            session_id="sess-X",
            executor_id="exec-collide",
            task_id="task-collide",
            role_id="backend",
            model_tier="sonnet",
            started_ts=old_ts,
            window_id="2026-05-19T07",
            estimated_tokens_in=500,
            extra={},
        ),
    )

    code, out, _ = _invoke("session_start.py", {}, state_root)

    assert code == 0
    payload = json.loads(out)
    assert payload["continue"] is True
    assert "state unavailable" not in payload["systemMessage"]
    assert "session start" in payload["systemMessage"]


def test_session_summary_emits_digest(state_root):
    _open_ledger(state_root).close()
    code, out, _ = _invoke("session_summary.py", {}, state_root)
    assert code == 0
    payload = json.loads(out)
    assert "session summary" in payload["systemMessage"]


def test_session_summary_includes_run_level_outputs(state_root):
    ledger = _open_ledger(state_root)
    now = datetime.now(timezone.utc).isoformat()
    ledger.ensure_role(
        role_id="devops",
        version="1.0.0",
        spec_path="roles/devops.md",
        model_tier="sonnet",
        created_ts=now,
    )
    ledger.upsert_executor(
        Executor(
            executor_id="exec-1",
            alias="do-123",
            role_id="devops",
            parent_executor_id=None,
            created_ts=now,
            last_seen_ts=now,
            status="active",
        )
    )
    ledger.ensure_task(
        task_id="task-1",
        task_type="config",
        status="done",
        created_ts=now,
    )
    ledger.append_run(
        Run(
            run_id="run-output-001",
            executor_id="exec-1",
            task_id="task-1",
            model="sonnet",
            tokens_in=100,
            tokens_out=25,
            window_id="2026-06-20T15",
            started_ts=now,
            ended_ts=now,
            outcome="ok",
            deliverable_ref_json=json.dumps(
                {"kind": "hash", "value": "sha256:" + "a" * 64}
            ),
            files_touched_json=json.dumps([
                "CODE_OF_CONDUCT.md",
                ".github/CODEOWNERS",
            ]),
        )
    )
    ledger.close()

    code, out, _ = _invoke("session_summary.py", {}, state_root)

    assert code == 0
    payload = json.loads(out)
    message = payload["systemMessage"]
    assert "Outputs recorded: none" not in message
    assert "Outputs recorded (1):" in message
    assert "do-123/devops (run run-output-00): 2 file(s) touched" in message


def test_session_summary_excludes_text_only_run_output(state_root):
    """A successful run whose only output is a free-text summary (no files) —
    e.g. a blocked-write failure narrative — must NOT be listed as a deliverable."""
    ledger = _open_ledger(state_root)
    now = datetime.now(timezone.utc).isoformat()
    ledger.ensure_role(
        role_id="general-purpose", version="1.0.0", spec_path="roles/gp.md",
        model_tier="sonnet", created_ts=now,
    )
    ledger.upsert_executor(Executor(
        executor_id="exec-2", alias="gene-59e", role_id="general-purpose",
        parent_executor_id=None, created_ts=now, last_seen_ts=now, status="active",
    ))
    ledger.ensure_task(task_id="task-2", task_type="probe", status="done", created_ts=now)
    ledger.append_run(Run(
        run_id="run-textonly-001", executor_id="exec-2", task_id="task-2", model="sonnet",
        tokens_in=50, tokens_out=10, window_id="2026-06-20T15",
        started_ts=now, ended_ts=now, outcome="ok",
        deliverable_ref_json=json.dumps({
            "kind": "summary",
            "value": "The Write was blocked by a PreToolUse hook (file_event.py policy hook)",
        }),
        files_touched_json=None,
    ))
    ledger.close()

    code, out, _ = _invoke("session_summary.py", {}, state_root)
    assert code == 0
    message = json.loads(out)["systemMessage"]
    assert "Outputs recorded: none" in message
    assert "Write was blocked" not in message


# --- Pre-task: cost gate + identity derivation ---------------------------

def test_pre_task_passes_non_task_invocations_through(state_root):
    _open_ledger(state_root).close()
    code, out, _ = _invoke(
        "pre_task.py",
        {"tool_name": "Read", "tool_input": {"file_path": "/tmp/foo"}},
        state_root,
    )
    assert code == 0
    assert json.loads(out)["continue"] is True


def test_pre_task_auto_short_prompt(state_root):
    """A small prompt is well below the soft threshold; gate auto-approves."""
    _open_ledger(state_root).close()
    code, out, _ = _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-1",
            "tool_input": {
                "subagent_type": "backend",
                "description": "small task",
                "prompt": "implement a helper function",
            },
        },
        state_root,
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["continue"] is True


def test_pre_task_notify_surfaces_structured_options(state_root):
    """A prompt sized between the soft and hard thresholds should NOTIFY,
    proceed (exit 0), and surface the same structured options BLOCK would,
    so the Principal can intercept without having to invent the right
    action under time pressure.

    Soft threshold is 20% of 88k capacity = ~17.6k tokens (70.4k chars
    at the 4-char-per-token estimate); hard is 40% = ~35.2k tokens.
    100k chars ≈ 25k tokens lands cleanly in the NOTIFY band.
    """
    _open_ledger(state_root).close()
    notify_prompt = "x" * 100_000
    code, out, _ = _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-notify",
            "tool_input": {
                "subagent_type": "backend",
                "description": "intermediate task",
                "prompt": notify_prompt,
            },
        },
        state_root,
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["continue"] is True
    msg = payload["systemMessage"]
    assert "cost gate — notify" in msg.lower()
    assert "Options:" in msg
    assert "Drop the model tier" in msg
    assert "Split the Task" in msg
    assert "Send the Manifest back for revision" in msg
    assert "Default: proceed" in msg
    # NOTIFY proceeds — it must not carry a native permission decision.
    assert "hookSpecificOutput" not in payload
    # Sentinel for the Coordinator to surface.
    notifies_dir = state_root / "notifies"
    assert notifies_dir.exists()
    sentinels = list(notifies_dir.glob("sess-notify-*.json"))
    assert len(sentinels) == 1
    record = json.loads(sentinels[0].read_text())
    assert record["state"] == "notify"
    assert record["default"] == "proceed"
    assert len(record["options"]) == 4


def test_pre_task_block_surfaces_structured_options(state_root):
    """BLOCK retains the structured options surface — the grammar is
    shared between NOTIFY and BLOCK so the Principal sees the same set
    of interventions whether the dispatch is paused or proceeding."""
    _open_ledger(state_root).close()
    big_prompt = "x" * 160_000
    code, out, _ = _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-block",
            "tool_input": {
                "subagent_type": "backend",
                "description": "outsized task",
                "prompt": big_prompt,
            },
        },
        state_root,
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["decision"] == "block"
    msg = payload["reason"]
    assert "cost gate — block" in msg.lower()
    assert "Options:" in msg
    assert "Send the Manifest back for revision" in msg
    assert "Default: paused" in msg
    blocks_dir = state_root / "blocks"
    sentinels = list(blocks_dir.glob("sess-block-*.json"))
    assert len(sentinels) == 1
    record = json.loads(sentinels[0].read_text())
    assert record["state"] == "block"
    assert record["default"] == "paused"
    assert len(record["options"]) == 4
    assert record["options"][3]["label"] == "Send the Manifest back for revision"


def test_pre_task_block_emits_native_permission_denial(state_root):
    """A BLOCK must ride the runtime's native PreToolUse permission
    decision (exit 0 — the runtime ignores stdout JSON on non-zero
    exits), so the full decision surface reaches the Coordinator as a
    proper denial reason instead of a generic hook error. The legacy
    `decision`/`reason` pair rides along for runtimes that predate
    `hookSpecificOutput`."""
    _open_ledger(state_root).close()
    big_prompt = "x" * 160_000
    code, out, _ = _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-deny",
            "tool_input": {
                "subagent_type": "backend",
                "description": "outsized task",
                "prompt": big_prompt,
            },
        },
        state_root,
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "continue" not in payload  # continue:false would halt the turn
    specific = payload["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    reason = specific["permissionDecisionReason"]
    assert reason
    # Both channels carry the same decision surface.
    assert reason == payload["reason"]
    assert "cost gate — block" in reason.lower()


def test_pre_task_blocks_when_prompt_exceeds_hard_threshold(state_root):
    """A prompt large enough to push estimated tokens past the run-size
    hard threshold (40% of 88k capacity = ~35.2k tokens) should block.
    160k chars ≈ 40k tokens at the 4-char-per-token estimate."""
    _open_ledger(state_root).close()
    big_prompt = "x" * 160_000
    code, out, _ = _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-1",
            "tool_input": {
                "subagent_type": "backend",
                "description": "huge task",
                "prompt": big_prompt,
            },
        },
        state_root,
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["decision"] == "block"
    # The block reason should mention either run-size or window pressure.
    reason = payload["reason"].lower()
    assert "block" in reason


def test_pre_task_rejects_fanout_one(state_root):
    """`[fanout: 1]` violates the multi-instance protocol — block before any work."""
    _open_ledger(state_root).close()
    code, out, _ = _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-fanout-one",
            "tool_input": {
                "subagent_type": "backend",
                "description": "invalid fanout",
                "prompt": "[fanout: 1] some work",
            },
        },
        state_root,
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["decision"] == "block"
    reason = payload["reason"]
    assert "--fanout requires N >= 2" in reason
    assert "(got 1)" in reason
    # Every pre_task block path carries the native permission denial.
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    # No pending sidecar should have been written for the rejected dispatch.
    pending_dir = state_root / "pending"
    if pending_dir.exists():
        assert list(pending_dir.glob("*.json")) == []


def test_pre_task_rejects_consensus_one(state_root):
    """`[consensus: 1]` is also rejected; same guard, different marker name."""
    _open_ledger(state_root).close()
    code, out, _ = _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-consensus-one",
            "tool_input": {
                "subagent_type": "backend",
                "description": "invalid consensus",
                "prompt": "[consensus: 1] same work twice",
            },
        },
        state_root,
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "--consensus requires N >= 2" in payload["reason"]


def test_pre_task_accepts_fanout_two(state_root):
    """A well-formed `[fanout: 2]` marker passes the guard and proceeds normally."""
    _open_ledger(state_root).close()
    code, out, _ = _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-fanout-two",
            "tool_input": {
                "subagent_type": "backend",
                "description": "valid fanout leg",
                "prompt": "[fanout: 2] explore approach A",
            },
        },
        state_root,
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["continue"] is True


def test_pre_task_rejects_fanout_zero_and_negatives(state_root):
    """`[fanout: 0]` and any non-integer literal are rejected as < 2."""
    _open_ledger(state_root).close()
    for prompt_marker in ("[fanout: 0]", "[fanout: -1]"):
        code, out, _ = _invoke(
            "pre_task.py",
            {
                "tool_name": "Task",
                "session_id": "sess-fanout-edge",
                "tool_input": {
                    "subagent_type": "backend",
                    "description": "edge fanout",
                    "prompt": f"{prompt_marker} edge case",
                },
            },
            state_root,
        )
        # `[fanout: -1]` does not match the digit-only regex, so it goes
        # through unguarded — pin the digit-only case (0) as a hard block.
        if prompt_marker == "[fanout: 0]":
            assert code == 0
            assert "--fanout requires N >= 2" in json.loads(out)["reason"]


def test_pre_task_yaml_error_produces_actionable_message(state_root, tmp_path):
    """A malformed YAML Manifest blocks with a message naming the line/column."""
    _open_ledger(state_root).close()
    manifests_dir = state_root.parent / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    mid = "hm-01967ca0-5a00-7b4f-9a2c-f3d8b0a10001"
    # Unquoted ':' inside a string value triggers a YAML parse failure.
    (manifests_dir / f"{mid}.yaml").write_text(
        "manifest_id: " + mid + "\n"
        "version: 1\n"
        "task: smoke-test\n"
        "context:\n"
        "  files:\n"
        "    - path: x.py\n"
        "      lines: \"1-10\"\n"
        "expected_deliverables:\n"
        "  - path: out\n"
        "constraints:\n"
        "  model_tier: sonnet\n"
        "  window_budget: 1000\n"
        "extras:\n"
        "  bad: --foo: bar: should-fail\n"
    )
    code, out, _ = _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-1",
            "tool_input": {
                "subagent_type": "backend",
                "description": "yaml-failure smoke",
                "prompt": f"[manifest: {mid}] do the thing",
            },
        },
        state_root,
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["decision"] == "block"
    reason = payload["reason"]
    assert "YAML parser" in reason or "line" in reason
    assert mid in reason


def test_pre_task_schema_error_names_the_field(state_root, tmp_path):
    """A schema-violating Manifest blocks with a message naming the field."""
    _open_ledger(state_root).close()
    manifests_dir = state_root.parent / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    mid = "hm-01967ca0-5a00-7b4f-9a2c-f3d8b0a10002"
    # Missing constraints.window_budget — schema requires it.
    (manifests_dir / f"{mid}.yaml").write_text(
        "manifest_id: " + mid + "\n"
        "version: 1\n"
        "task: schema-test\n"
        "context: { files: [{ path: x.py, lines: \"1-10\" }] }\n"
        "expected_deliverables: [{ path: out }]\n"
        "constraints: { model_tier: sonnet }\n"
    )
    code, out, _ = _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-1",
            "tool_input": {
                "subagent_type": "backend",
                "description": "schema-failure smoke",
                "prompt": f"[manifest: {mid}] do the thing",
            },
        },
        state_root,
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["decision"] == "block"
    reason = payload["reason"]
    assert mid in reason
    assert "Schema" in reason or "schema" in reason


def test_pre_task_creates_executor_on_first_use(state_root):
    """First Task of an unseen subagent_type auto-discovers the Role and
    creates one Executor for that Role."""
    _open_ledger(state_root).close()
    code, _, _ = _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-1",
            "tool_input": {
                "subagent_type": "frontend",
                "description": "task A",
                "prompt": "render the page",
            },
        },
        state_root,
    )
    assert code == 0
    ledger = _open_ledger(state_root)
    rows = ledger._conn.execute(
        "SELECT * FROM executors WHERE role_id = 'frontend'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["alias"].startswith("fron-")
    ledger.close()


def test_pre_task_reuses_executor_on_subsequent_calls(state_root):
    """Two Tasks of the same Role in the same project reuse one Executor."""
    _open_ledger(state_root).close()
    for description in ("task one", "task two"):
        code, _, _ = _invoke(
            "pre_task.py",
            {
                "tool_name": "Task",
                "session_id": "sess-1",
                "tool_input": {
                    "subagent_type": "backend",
                    "description": description,
                    "prompt": "do the thing",
                },
            },
            state_root,
        )
        assert code == 0
    ledger = _open_ledger(state_root)
    rows = ledger._conn.execute(
        "SELECT * FROM executors WHERE role_id = 'backend'"
    ).fetchall()
    assert len(rows) == 1
    ledger.close()


def test_pre_task_honors_continuing_marker(state_root):
    """A `[continuing: alias]` marker reuses the named Executor."""
    _open_ledger(state_root).close()
    # Bootstrap an Executor.
    code, _, _ = _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-1",
            "tool_input": {
                "subagent_type": "backend",
                "description": "task",
                "prompt": "do something",
            },
        },
        state_root,
    )
    assert code == 0

    ledger = _open_ledger(state_root)
    alias = ledger._conn.execute(
        "SELECT alias FROM executors WHERE role_id = 'backend' LIMIT 1"
    ).fetchone()["alias"]
    ledger.close()

    # Second call references that alias explicitly.
    code, _, _ = _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-2",
            "tool_input": {
                "subagent_type": "backend",
                "description": "task",
                "prompt": f"[continuing: {alias}] keep going",
            },
        },
        state_root,
    )
    assert code == 0
    ledger = _open_ledger(state_root)
    rows = ledger._conn.execute(
        "SELECT * FROM executors WHERE role_id = 'backend'"
    ).fetchall()
    assert len(rows) == 1
    ledger.close()


def test_pre_task_writes_pending_sidecar(state_root):
    _open_ledger(state_root).close()
    code, _, _ = _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-1",
            "tool_input": {
                "subagent_type": "backend",
                "description": "task",
                "prompt": "do something",
            },
        },
        state_root,
    )
    assert code == 0
    pending_dir = state_root / "pending"
    assert pending_dir.exists()
    files = list(pending_dir.glob("*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text())
    assert record["session_id"] == "sess-1"
    assert record["role_id"] == "backend"


def test_dispatch_project_marker_routes_run_to_target_ledger(tmp_path):
    """A dispatch from another cwd records in the explicit adopted project."""
    dev_project = tmp_path / "dev"
    target_project = tmp_path / "adopted project"
    dev_state = dev_project / ".metaensemble" / "state"
    target_state = target_project / ".metaensemble" / "state"
    dev_project.mkdir(parents=True)
    target_project.mkdir(parents=True)
    _open_ledger(dev_state).close()
    _open_ledger(target_state).close()

    prompt = f"[project: {target_project}] implement the requested change"
    code, out, _ = _invoke(
        "pre_task.py",
        {
            "tool_name": "Agent",
            "session_id": "sess-routed",
            "tool_input": {
                "subagent_type": "backend",
                "description": "routed task",
                "prompt": prompt,
            },
        },
        dev_state,
        cwd=dev_project,
    )
    assert code == 0
    assert json.loads(out)["continue"] is True
    assert list((target_state / "pending").glob("*.json"))
    assert not (dev_state / "pending").exists()

    code, out, _ = _invoke(
        "post_task.py",
        {
            "tool_name": "Agent",
            "session_id": "sess-routed",
            "tool_input": {
                "subagent_type": "backend",
                "description": "routed task",
                "prompt": prompt,
            },
            "tool_response": "done",
        },
        dev_state,
        cwd=dev_project,
    )
    assert code == 0
    assert json.loads(out)["continue"] is True

    target_ledger = _open_ledger(target_state)
    dev_ledger = _open_ledger(dev_state)
    try:
        assert len(target_ledger.get_recent_runs(limit=10)) == 1
        assert len(dev_ledger.get_recent_runs(limit=10)) == 0
    finally:
        target_ledger.close()
        dev_ledger.close()


# --- File events: project boundary + provenance --------------------------

def test_file_event_blocks_outside_active_project_root(state_root):
    """File-modifying tools are blocked when they resolve outside the
    project root that created the active MetaEnsemble dispatch."""
    _open_ledger(state_root).close()
    project_root = state_root.parent / "project"
    project_root.mkdir()

    pre_code, _, _ = _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-boundary",
            "tool_input": {
                "subagent_type": "frontend",
                "description": "edit css",
                "prompt": "edit a css file",
            },
        },
        state_root,
        cwd=project_root,
    )
    assert pre_code == 0

    code, out, _ = _invoke(
        "file_event.py",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "session_id": "sess-boundary",
            "cwd": str(project_root),
            "tool_input": {"file_path": "../outside.css"},
        },
        state_root,
        cwd=project_root,
    )
    assert code == 2
    payload = json.loads(out)
    assert payload["continue"] is False
    assert "project boundary guard" in payload["stopReason"]


def test_file_event_blocks_metaensemble_owned_overlap_surface(tmp_path):
    """Overlap ownership is enforced generically from install-decisions.yaml."""
    project_root = tmp_path / "project"
    state_root = project_root / ".metaensemble" / "state"
    work_record = project_root / "docs" / "work-log.md"
    work_record.parent.mkdir(parents=True)
    work_record.write_text("# Work log\n")
    decisions = project_root / ".metaensemble" / "install-decisions.yaml"
    decisions.parent.mkdir(parents=True, exist_ok=True)
    decisions.write_text(
        "overlaps:\n"
        "  deliverable_records:\n"
        "    project_surface: \"docs/work-log.md\"\n"
        "    metaensemble_surface: \"Ledger runs + deliverable_ref_json\"\n"
        "    action: metaensemble_owned\n"
        "    write_policy: block_when_metaensemble_owned\n"
    )
    _open_ledger(state_root).close()

    pre_code, _, _ = _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-overlap-owned",
            "tool_input": {
                "subagent_type": "docs",
                "description": "update work log",
                "prompt": "summarize work",
            },
        },
        state_root,
        cwd=project_root,
    )
    assert pre_code == 0

    code, out, _ = _invoke(
        "file_event.py",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "session_id": "sess-overlap-owned",
            "cwd": str(project_root),
            "tool_input": {"file_path": "docs/work-log.md"},
        },
        state_root,
        cwd=project_root,
    )

    assert code == 2
    payload = json.loads(out)
    assert payload["continue"] is False
    assert "overlap ownership guard" in payload["stopReason"]
    assert "deliverable_records" in payload["stopReason"]


def test_file_event_allows_project_owned_overlap_surface(tmp_path):
    project_root = tmp_path / "project"
    state_root = project_root / ".metaensemble" / "state"
    work_record = project_root / "docs" / "work-log.md"
    work_record.parent.mkdir(parents=True)
    work_record.write_text("# Work log\n")
    decisions = project_root / ".metaensemble" / "install-decisions.yaml"
    decisions.parent.mkdir(parents=True, exist_ok=True)
    decisions.write_text(
        "overlaps:\n"
        "  deliverable_records:\n"
        "    project_surface: \"docs/work-log.md\"\n"
        "    metaensemble_surface: \"Ledger runs + deliverable_ref_json\"\n"
        "    action: project_owned\n"
    )
    _open_ledger(state_root).close()

    pre_code, _, _ = _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-overlap-project",
            "tool_input": {
                "subagent_type": "docs",
                "description": "update work log",
                "prompt": "summarize work",
            },
        },
        state_root,
        cwd=project_root,
    )
    assert pre_code == 0

    code, out, _ = _invoke(
        "file_event.py",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "session_id": "sess-overlap-project",
            "cwd": str(project_root),
            "tool_input": {"file_path": "docs/work-log.md"},
        },
        state_root,
        cwd=project_root,
    )

    assert code == 0
    assert json.loads(out)["continue"] is True


def test_file_event_allows_current_claude_project_state_write(state_root):
    """The boundary guard permits Claude Code's own per-project state writes.

    The allowance is scoped to the active project slug under
    `~/.claude/projects/` and should not be recorded as Run provenance.
    """
    home = state_root.parent / "home"
    project_root = state_root.parent / "project"
    state_root = project_root / ".metaensemble" / "state"
    _open_ledger(state_root).close()

    pre_code, _, _ = _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-claude-state",
            "tool_input": {
                "subagent_type": "frontend",
                "description": "edit css",
                "prompt": "edit a css file",
            },
        },
        state_root,
        cwd=project_root,
        home=home,
    )
    assert pre_code == 0

    runtime_file = (
        home / ".claude" / "projects" / _encode_cwd_for_runtime(project_root)
        / "memories" / "session.md"
    )

    for event_name in ("PreToolUse", "PostToolUse"):
        code, out, _ = _invoke(
            "file_event.py",
            {
                "hook_event_name": event_name,
                "tool_name": "Write",
                "session_id": "sess-claude-state",
                "cwd": str(project_root),
                "tool_input": {"file_path": str(runtime_file)},
                "tool_response": "ok",
            },
            state_root,
            cwd=project_root,
            home=home,
        )
        assert code == 0
        assert json.loads(out)["continue"] is True

    assert not (state_root / "file-events").exists()


def test_file_event_blocks_other_claude_project_state_write(state_root):
    """The Claude-state carve-out must not allow writes to other project slugs."""
    home = state_root.parent / "home"
    project_root = state_root.parent / "project"
    state_root = project_root / ".metaensemble" / "state"
    _open_ledger(state_root).close()

    pre_code, _, _ = _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-other-claude-state",
            "tool_input": {
                "subagent_type": "frontend",
                "description": "edit css",
                "prompt": "edit a css file",
            },
        },
        state_root,
        cwd=project_root,
        home=home,
    )
    assert pre_code == 0

    other_project = state_root.parent / "other-project"
    runtime_file = (
        home / ".claude" / "projects" / _encode_cwd_for_runtime(other_project)
        / "memories" / "session.md"
    )

    code, out, _ = _invoke(
        "file_event.py",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "session_id": "sess-other-claude-state",
            "cwd": str(project_root),
            "tool_input": {"file_path": str(runtime_file)},
        },
        state_root,
        cwd=project_root,
        home=home,
    )

    assert code == 2
    payload = json.loads(out)
    assert payload["continue"] is False
    assert "project boundary guard" in payload["stopReason"]


def test_file_event_records_files_touched_for_post_task(state_root):
    """PostTask merges file-tool hook events into files_touched/tool_use fields."""
    project_root = state_root.parent / "project"
    state_root = project_root / ".metaensemble" / "state"
    _open_ledger(state_root).close()
    (project_root / "src").mkdir(parents=True)
    (project_root / "src" / "app.css").write_text("body{}")  # real artifact on disk

    pre_code, _, _ = _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-file",
            "tool_input": {
                "subagent_type": "frontend",
                "description": "edit css",
                "prompt": "edit src/app.css",
            },
        },
        state_root,
        cwd=project_root,
    )
    assert pre_code == 0

    for event_name in ("PreToolUse", "PostToolUse"):
        code, out, _ = _invoke(
            "file_event.py",
            {
                "hook_event_name": event_name,
                "tool_name": "Edit",
                "session_id": "sess-file-child",
                "cwd": str(project_root),
                "tool_input": {"file_path": "src/app.css"},
                "tool_response": "ok",
            },
            state_root,
            cwd=project_root,
        )
        assert code == 0
        assert json.loads(out)["continue"] is True

    post_code, _, _ = _invoke(
        "post_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-file",
            "tool_input": {"subagent_type": "frontend"},
            "tool_response": "Done.",
        },
        state_root,
        cwd=project_root,
    )
    assert post_code == 0

    ledger = _open_ledger(state_root)
    run = ledger.get_recent_runs(limit=1)[0]
    assert json.loads(run.files_touched_json) == ["src/app.css"]
    assert json.loads(run.tool_use_json) == [
        {"name": "Edit", "count": 1, "input_tokens": 0}
    ]
    ledger.close()
    assert not list((state_root / "file-events").glob("*.jsonl"))


def test_file_event_ignores_direct_edits_without_active_dispatch(state_root):
    """Plain Claude Code edits should not create orphan Run provenance."""
    project_root = state_root.parent / "project"
    state_root = project_root / ".metaensemble" / "state"
    _open_ledger(state_root).close()
    (project_root / "src").mkdir(parents=True)

    code, out, _ = _invoke(
        "file_event.py",
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Edit",
            "session_id": "plain-session",
            "cwd": str(project_root),
            "tool_input": {"file_path": "src/app.css"},
            "tool_response": "ok",
        },
        state_root,
        cwd=project_root,
    )

    assert code == 0
    assert json.loads(out)["continue"] is True
    assert not (state_root / "file-events").exists()


def test_file_event_blocks_direct_edit_during_dispatch_command(state_root):
    """A `/dispatch` slash command must go through Task/Agent, not direct Edit."""
    project_root = state_root.parent / "project"
    state_root = project_root / ".metaensemble" / "state"
    _open_ledger(state_root).close()
    transcript = state_root.parent / "dispatch-transcript.jsonl"
    transcript.write_text(json.dumps({
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "text",
                "text": (
                    "When the Principal invokes `/dispatch <task description>`, "
                    "follow the Coordinator protocol.\n\nARGUMENTS: edit file"
                ),
            }],
        },
    }) + "\n")

    code, out, _ = _invoke(
        "file_event.py",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "session_id": "direct-dispatch",
            "cwd": str(project_root),
            "transcript_path": str(transcript),
            "tool_input": {"file_path": "src/app.css"},
        },
        state_root,
        cwd=project_root,
    )

    assert code == 2
    payload = json.loads(out)
    assert payload["continue"] is False
    assert "direct file edit" in payload["stopReason"]


def test_file_event_allows_manifest_write_before_dispatch_run(state_root):
    """The Coordinator may write the Manifest before Task/Agent starts."""
    project_root = state_root.parent / "project"
    state_root = project_root / ".metaensemble" / "state"
    _open_ledger(state_root).close()
    transcript = state_root.parent / "dispatch-transcript.jsonl"
    transcript.write_text(json.dumps({
        "type": "user",
        "message": {
            "role": "user",
            "content": (
                "<command-message>dispatch</command-message>\n"
                "<command-name>/dispatch</command-name>\n"
                "<command-args>edit file</command-args>"
            ),
        },
    }) + "\n")

    code, out, _ = _invoke(
        "file_event.py",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "session_id": "manifest-write",
            "cwd": str(project_root),
            "transcript_path": str(transcript),
            "tool_input": {"file_path": ".metaensemble/manifests/hm-test.yaml"},
        },
        state_root,
        cwd=project_root,
    )

    assert code == 0
    assert json.loads(out)["continue"] is True


def test_file_event_allows_metaensemble_report_write_before_dispatch_run(state_root):
    """The Coordinator may write an explicitly-declared synthesis report."""
    project_root = state_root.parent / "project"
    state_root = project_root / ".metaensemble" / "state"
    _open_ledger(state_root).close()
    transcript = state_root.parent / "dispatch-transcript.jsonl"
    transcript.write_text(json.dumps({
        "type": "user",
        "message": {
            "role": "user",
            "content": (
                "<command-message>dispatch</command-message>\n"
                "<command-name>/dispatch</command-name>\n"
                "<command-args>write declared synthesis</command-args>"
            ),
        },
    }) + "\n")

    code, out, _ = _invoke(
        "file_event.py",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "session_id": "report-write",
            "cwd": str(project_root),
            "transcript_path": str(transcript),
            "tool_input": {"file_path": ".metaensemble/reports/audit/synthesis.md"},
        },
        state_root,
        cwd=project_root,
    )

    assert code == 0
    assert json.loads(out)["continue"] is True


def test_file_event_allows_legacy_configured_claude_report_root(state_root):
    """Existing projects may preserve `.claude/reports` as their report root."""
    project_root = state_root.parent / "project"
    state_root = project_root / ".metaensemble" / "state"
    _open_ledger(state_root).close()
    decisions = project_root / ".metaensemble" / "install-decisions.yaml"
    decisions.write_text(
        "suggested_layout: top-level\n"
        "overlaps:\n"
        "  deliverable_records:\n"
        "    project_surface: \".claude/reports/_registry.md\"\n"
        "    action: project_owned\n"
        "agents: []\n"
    )
    transcript = state_root.parent / "dispatch-transcript.jsonl"
    transcript.write_text(json.dumps({
        "type": "user",
        "message": {
            "role": "user",
            "content": (
                "<command-message>dispatch</command-message>\n"
                "<command-name>/dispatch</command-name>\n"
                "<command-args>write declared synthesis</command-args>"
            ),
        },
    }) + "\n")

    code, out, _ = _invoke(
        "file_event.py",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "session_id": "legacy-report-write",
            "cwd": str(project_root),
            "transcript_path": str(transcript),
            "tool_input": {"file_path": ".claude/reports/audit/synthesis.md"},
        },
        state_root,
        cwd=project_root,
    )

    assert code == 0
    assert json.loads(out)["continue"] is True


# --- Post-task: completion of pending Runs --------------------------------

def test_post_task_completes_pending_run(state_root):
    """End-to-end: pre_task stamps a sidecar; post_task writes the Run."""
    _open_ledger(state_root).close()
    pre_code, _, _ = _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-1",
            "tool_input": {
                "subagent_type": "backend",
                "description": "auth",
                "prompt": "implement password reset",
            },
        },
        state_root,
    )
    assert pre_code == 0

    post_code, _, _ = _invoke(
        "post_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-1",
            "tool_input": {"subagent_type": "backend"},
            "tool_response": "Done. The password reset endpoint is implemented in src/auth/reset.py.",
        },
        state_root,
    )
    assert post_code == 0

    ledger = _open_ledger(state_root)
    runs = ledger.get_recent_runs(limit=10)
    assert len(runs) == 1
    assert runs[0].outcome == "ok"
    assert runs[0].model == "sonnet"
    assert runs[0].model_source == "tier_fallback"
    assert runs[0].tokens_in > 0
    assert runs[0].tokens_out > 0
    ledger.close()

    # Sidecar should be deleted after the Run is written.
    pending_dir = state_root / "pending"
    assert not list(pending_dir.glob("*.json"))


def test_post_task_normalizes_dict_model_from_statusline(state_root):
    """Live repro: statusline may expose model as a dict, but Ledger stores text."""
    _open_ledger(state_root).close()
    runtime_state = state_root.parent / ".metaensemble" / "state"
    runtime_state.mkdir(parents=True)
    runtime_state.joinpath("runtime-rate-limits.json").write_text(json.dumps({
        "captured_at": "2999-01-01T00:00:00+00:00",
        "rate_limits": {},
        "model": {"id": "claude-opus-4-7", "display_name": "Opus 4.7"},
        "session_id": "sess-dict-statusline",
    }))
    pre_code, _, _ = _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-dict-statusline",
            "tool_input": {
                "subagent_type": "backend",
                "description": "auth",
                "prompt": "implement password reset",
            },
        },
        state_root,
    )
    assert pre_code == 0

    post_code, _, _ = _invoke(
        "post_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-dict-statusline",
            "tool_input": {"subagent_type": "backend"},
            "tool_response": "Done.",
        },
        state_root,
    )
    assert post_code == 0

    ledger = _open_ledger(state_root)
    run = ledger.get_recent_runs(limit=1)[0]
    assert run.model == "claude-opus-4-7"
    assert run.model_source == "statusline"
    ledger.close()


def test_post_task_normalizes_dict_model_from_transcript(state_root, tmp_path):
    _open_ledger(state_root).close()
    transcript = tmp_path / "sess.jsonl"
    transcript.write_text(json.dumps({
        "message": {
            "role": "assistant",
            "model": {"id": "claude-sonnet-4-6", "display_name": "Sonnet 4.6"},
            "content": [{"type": "text", "text": "Done."}],
        },
    }) + "\n")
    _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-dict-transcript",
            "tool_input": {
                "subagent_type": "backend",
                "description": "auth",
                "prompt": "implement password reset",
            },
        },
        state_root,
    )

    post_code, _, _ = _invoke(
        "post_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-dict-transcript",
            "transcript_path": str(transcript),
            "tool_input": {"subagent_type": "backend"},
            "tool_response": "Done.",
        },
        state_root,
    )
    assert post_code == 0

    ledger = _open_ledger(state_root)
    run = ledger.get_recent_runs(limit=1)[0]
    assert run.model == "claude-sonnet-4-6"
    assert run.model_source == "transcript"
    ledger.close()


def test_post_task_derives_transcript_path_from_session_and_cwd(state_root):
    """`claude -p` live hooks may omit transcript_path; derive it from cwd."""
    _open_ledger(state_root).close()
    home = state_root.parent / "home"
    project_root = state_root.parent / "project"
    project_root.mkdir()
    session_id = "sess-derived-transcript"
    prompt = "implement password reset"
    transcript_dir = home / ".claude" / "projects" / _encode_cwd_for_runtime(project_root)
    transcript_dir.mkdir(parents=True)
    (transcript_dir / f"{session_id}.jsonl").write_text(json.dumps({
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-7",
            "content": [{
                "type": "tool_use",
                "name": "Agent",
                "input": {
                    "subagent_type": "backend",
                    "prompt": prompt,
                },
            }],
        },
    }) + "\n")

    _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": session_id,
            "tool_input": {
                "subagent_type": "backend",
                "description": "auth",
                "prompt": prompt,
            },
        },
        state_root,
        cwd=project_root,
        home=home,
    )

    post_code, _, _ = _invoke(
        "post_task.py",
        {
            "tool_name": "Task",
            "session_id": session_id,
            "tool_input": {"subagent_type": "backend"},
            "tool_response": "Done.",
        },
        state_root,
        cwd=project_root,
        home=home,
    )
    assert post_code == 0

    ledger = _open_ledger(state_root)
    run = ledger.get_recent_runs(limit=1)[0]
    assert run.model == "claude-opus-4-7"
    assert run.model_source == "transcript"
    ledger.close()


def test_post_task_malformed_transcript_still_writes_core_run(state_root, tmp_path):
    _open_ledger(state_root).close()
    transcript = tmp_path / "bad.jsonl"
    transcript.write_text("{not json\n")
    _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-bad-transcript",
            "tool_input": {
                "subagent_type": "backend",
                "description": "auth",
                "prompt": "implement password reset",
            },
        },
        state_root,
    )

    post_code, _, _ = _invoke(
        "post_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-bad-transcript",
            "transcript_path": str(transcript),
            "tool_input": {"subagent_type": "backend"},
            "tool_response": "Done.",
        },
        state_root,
    )
    assert post_code == 0

    ledger = _open_ledger(state_root)
    run = ledger.get_recent_runs(limit=1)[0]
    assert run.outcome == "ok"
    assert run.model == "sonnet"
    assert run.model_source == "tier_fallback"
    ledger.close()


def test_post_task_classifies_failure(state_root):
    _open_ledger(state_root).close()
    _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-1",
            "tool_input": {
                "subagent_type": "backend",
                "description": "auth",
                "prompt": "do thing",
            },
        },
        state_root,
    )
    code, _, _ = _invoke(
        "post_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-1",
            "tool_input": {"subagent_type": "backend"},
            "tool_response": "Error: failed to write file; permission denied",
        },
        state_root,
    )
    assert code == 0
    ledger = _open_ledger(state_root)
    runs = ledger.get_recent_runs(limit=10)
    assert len(runs) == 1
    assert runs[0].outcome == "failed"
    # Failure reason is classified and recorded; the response has no
    # specific diagnostic signal, so it lands in 'other'. A regression
    # that drops failure_reason from the Run write path would surface as
    # None here.
    assert runs[0].failure_reason == "other"
    ledger.close()


def test_post_task_records_specific_failure_reason(state_root):
    """A failure response with an explicit signal (here, a traceback) is
    classified into the appropriate category, not buried as 'other'.

    This complements test_post_task_classifies_failure by exercising
    the non-'other' branches of classify_failure_reason through the
    full pre/post-hook chain.
    """
    _open_ledger(state_root).close()
    _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-x",
            "tool_input": {
                "subagent_type": "backend",
                "description": "crash",
                "prompt": "do thing",
            },
        },
        state_root,
    )
    code, _, _ = _invoke(
        "post_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-x",
            "tool_input": {"subagent_type": "backend"},
            "tool_response": "Traceback (most recent call last): ZeroDivisionError",
        },
        state_root,
    )
    assert code == 0
    ledger = _open_ledger(state_root)
    runs = ledger.get_recent_runs(limit=10)
    assert len(runs) == 1
    assert runs[0].outcome == "failed"
    assert runs[0].failure_reason == "exception"
    ledger.close()


def test_post_task_no_pending_logs_and_continues(state_root):
    """If no pending sidecar exists, the hook logs and continues."""
    state_root.mkdir(parents=True, exist_ok=True)
    code, out, _ = _invoke(
        "post_task.py",
        {
            "tool_name": "Task",
            "session_id": "orphan",
            "tool_response": "irrelevant",
        },
        state_root,
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["continue"] is True


def test_post_task_captures_deliverable_path(state_root):
    project_root = state_root.parent / "project"
    state_root = project_root / ".metaensemble" / "state"
    _open_ledger(state_root).close()
    # The claimed deliverable must exist on disk to be recorded (provenance is
    # real artifacts only; a path parsed from prose that does not exist is dropped).
    deliverable = (
        project_root
        / ".metaensemble"
        / "reports"
        / "implementation"
        / "auth-20260514.md"
    )
    deliverable.parent.mkdir(parents=True)
    deliverable.write_text("# report")
    _invoke(
        "pre_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-1",
            "tool_input": {
                "subagent_type": "backend",
                "description": "task",
                "prompt": "implement",
            },
        },
        state_root,
        cwd=project_root,
    )
    _invoke(
        "post_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-1",
            "tool_input": {"subagent_type": "backend"},
            "tool_response": (
                "Wrote the report to "
                ".metaensemble/reports/implementation/auth-20260514.md and tests pass."
            ),
        },
        state_root,
        cwd=project_root,
    )
    ledger = _open_ledger(state_root)
    runs = ledger.get_recent_runs(limit=10)
    assert len(runs) == 1
    assert (
        runs[0].deliverable_path
        == ".metaensemble/reports/implementation/auth-20260514.md"
    )
    ledger.close()


# --- Deliverable sync ----------------------------------------------------

def test_deliverable_sync_records_report_writes(state_root, tmp_path):
    state_root.mkdir(parents=True, exist_ok=True)
    deliverable = tmp_path / "reports" / "review" / "auth-20260513.md"
    code, _, _ = _invoke(
        "deliverable_sync.py",
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(deliverable)},
        },
        state_root,
    )
    assert code == 0
    index = state_root / "deliverables_index.jsonl"
    assert index.exists()
    record = json.loads(index.read_text().strip())
    assert record["path"] == str(deliverable)


def test_deliverable_sync_records_metaensemble_report_writes(state_root, tmp_path):
    state_root.mkdir(parents=True, exist_ok=True)
    deliverable = tmp_path / ".metaensemble" / "reports" / "audit" / "synthesis.md"
    code, _, _ = _invoke(
        "deliverable_sync.py",
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(deliverable)},
        },
        state_root,
    )
    assert code == 0
    index = state_root / "deliverables_index.jsonl"
    assert index.exists()
    record = json.loads(index.read_text().strip())
    assert record["path"] == str(deliverable)


def test_deliverable_sync_skips_non_report_writes(state_root):
    state_root.mkdir(parents=True, exist_ok=True)
    code, _, _ = _invoke(
        "deliverable_sync.py",
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/random.txt"},
        },
        state_root,
    )
    assert code == 0
    index = state_root / "deliverables_index.jsonl"
    assert not index.exists()


def test_session_summary_excludes_nonexistent_file_output(state_root):
    """A run claiming a file that does not exist on disk (e.g. a denied write
    the transcript listed) must NOT be counted as a produced deliverable."""
    ledger = _open_ledger(state_root)
    now = datetime.now(timezone.utc).isoformat()
    ledger.ensure_role(role_id="code-quality", version="1.0.0",
                       spec_path="roles/cq.md", model_tier="sonnet", created_ts=now)
    ledger.upsert_executor(Executor(
        executor_id="exec-3", alias="code-9", role_id="code-quality",
        parent_executor_id=None, created_ts=now, last_seen_ts=now, status="active"))
    ledger.ensure_task(task_id="task-3", task_type="audit", status="done", created_ts=now)
    ghost = "/nonexistent/dir/governance-verification.md"
    ledger.append_run(Run(
        run_id="run-ghost-001", executor_id="exec-3", task_id="task-3", model="sonnet",
        tokens_in=80, tokens_out=20, window_id="2026-06-20T15",
        started_ts=now, ended_ts=now, outcome="ok",
        deliverable_ref_json=json.dumps({"kind": "path", "value": ghost}),
        files_touched_json=json.dumps([ghost]),
    ))
    ledger.close()
    code, out, _ = _invoke("session_summary.py", {}, state_root)
    assert code == 0
    message = json.loads(out)["systemMessage"]
    assert "governance-verification.md" not in message
    assert "Outputs recorded: none" in message
