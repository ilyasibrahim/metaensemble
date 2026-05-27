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
from pathlib import Path

import pytest

from metaensemble.lib.ledger import Ledger
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


def test_session_summary_emits_digest(state_root):
    _open_ledger(state_root).close()
    code, out, _ = _invoke("session_summary.py", {}, state_root)
    assert code == 0
    payload = json.loads(out)
    assert "session summary" in payload["systemMessage"]


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
    assert "Default: proceed" in msg
    # Sentinel for the Coordinator to surface.
    notifies_dir = state_root / "notifies"
    assert notifies_dir.exists()
    sentinels = list(notifies_dir.glob("sess-notify-*.json"))
    assert len(sentinels) == 1
    record = json.loads(sentinels[0].read_text())
    assert record["state"] == "notify"
    assert record["default"] == "proceed"
    assert len(record["options"]) == 3


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
    assert code == 2
    payload = json.loads(out)
    assert payload["continue"] is False
    msg = payload["stopReason"]
    assert "cost gate — block" in msg.lower()
    assert "Options:" in msg
    assert "Default: paused" in msg
    blocks_dir = state_root / "blocks"
    sentinels = list(blocks_dir.glob("sess-block-*.json"))
    assert len(sentinels) == 1
    record = json.loads(sentinels[0].read_text())
    assert record["state"] == "block"
    assert record["default"] == "paused"
    assert len(record["options"]) == 3


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
    assert code == 2
    payload = json.loads(out)
    assert payload["continue"] is False
    # The block reason should mention either run-size or window pressure.
    reason = payload["stopReason"].lower()
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
    assert code == 2
    payload = json.loads(out)
    assert payload["continue"] is False
    reason = payload["stopReason"]
    assert "--fanout requires N >= 2" in reason
    assert "(got 1)" in reason
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
    assert code == 2
    payload = json.loads(out)
    assert "--consensus requires N >= 2" in payload["stopReason"]


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
            assert code == 2
            assert "--fanout requires N >= 2" in json.loads(out)["stopReason"]


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
    assert code == 2
    payload = json.loads(out)
    assert payload["continue"] is False
    reason = payload["stopReason"]
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
    assert code == 2
    payload = json.loads(out)
    assert payload["continue"] is False
    reason = payload["stopReason"]
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
    _open_ledger(state_root).close()
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
    )
    _invoke(
        "post_task.py",
        {
            "tool_name": "Task",
            "session_id": "sess-1",
            "tool_input": {"subagent_type": "backend"},
            "tool_response": "Wrote the report to reports/implementation/auth-20260514.md and tests pass.",
        },
        state_root,
    )
    ledger = _open_ledger(state_root)
    runs = ledger.get_recent_runs(limit=10)
    assert len(runs) == 1
    assert runs[0].deliverable_path == "reports/implementation/auth-20260514.md"
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
