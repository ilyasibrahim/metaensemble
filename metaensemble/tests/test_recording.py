"""Unit tests for the recording helpers (`metaensemble.lib.recording`).

These are pure-function tests covering the small bits of logic that
hooks rely on: marker parsing, outcome classification, deliverable-path
extraction, and Role / Executor materialization against a Ledger.
"""
from __future__ import annotations

from pathlib import Path

from metaensemble.lib.ledger import Ledger
from metaensemble.lib.recording import (
    classify_failure_reason,
    classify_outcome,
    ensure_executor,
    ensure_role,
    ensure_task,
    estimate_tokens,
    extract_deliverable_path,
    manifest_path_for,
    parse_markers,
)


# --- estimate_tokens -----------------------------------------------------

def test_estimate_tokens_returns_zero_for_empty():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0


def test_estimate_tokens_scales_with_length():
    short = estimate_tokens("hello world")
    long = estimate_tokens("hello world " * 100)
    assert long > short * 50


# --- parse_markers -------------------------------------------------------

def test_parse_markers_no_markers():
    assert parse_markers("just a plain prompt") == {}
    assert parse_markers("") == {}
    assert parse_markers(None) == {}


def test_parse_markers_manifest():
    result = parse_markers("[manifest: hm-01967ca0-5a00-7b4f] do the thing")
    assert result["manifest_id"] == "hm-01967ca0-5a00-7b4f"


def test_parse_markers_continuing():
    result = parse_markers("[continuing: be-7b3] keep going")
    assert result["continuing_alias"] == "be-7b3"


def test_parse_markers_task():
    result = parse_markers("[task: task-abc123def456] do it")
    assert result["task_id"] == "task-abc123def456"


def test_parse_markers_task_accepts_semantic_ids():
    """Live-test regression (2026-07-03): a hex-only charset silently
    dropped Coordinator-written semantic ids like `task-livetest-020`,
    losing the shared-Task grouping the marker exists to create."""
    result = parse_markers("[task: task-livetest-020] run the check")
    assert result["task_id"] == "task-livetest-020"


def test_parse_markers_all_three():
    prompt = "[manifest: hm-abc123] [continuing: arch-7b3] [task: task-deadbeef0001] go"
    result = parse_markers(prompt)
    assert result["manifest_id"] == "hm-abc123"
    assert result["continuing_alias"] == "arch-7b3"
    assert result["task_id"] == "task-deadbeef0001"


# --- classify_outcome ----------------------------------------------------

def test_classify_outcome_ok_for_normal_response():
    assert classify_outcome("Done. Wrote the file.") == "ok"


def test_classify_outcome_failed_on_error_hint():
    assert classify_outcome("Error: command failed with exit 1") == "failed"
    assert classify_outcome("Traceback (most recent call last)") == "failed"
    assert classify_outcome("Exception: permission denied") == "failed"


def test_classify_outcome_ignores_benign_failure_vocabulary():
    assert classify_outcome("wici weyday = 'failed to call'") == "ok"
    assert classify_outcome("That could not have happened in this branch.") == "ok"
    assert classify_outcome("With the exception of one edge case, this is done.") == "ok"
    assert classify_outcome("The command returned error code 0.") == "ok"


def test_classify_outcome_failed_on_is_error_field():
    assert classify_outcome({"is_error": True, "content": "anything"}) == "failed"


def test_classify_outcome_failed_on_none():
    assert classify_outcome(None) == "failed"


def test_classify_outcome_partial():
    assert classify_outcome("Partial result; remaining work outstanding") == "partial"


# --- classify_failure_reason -------------------------------------------

def test_classify_failure_reason_cost_gate_block():
    assert classify_failure_reason("MetaEnsemble cost gate blocked the dispatch: reason=large run") == "cost_gate_block"


def test_classify_failure_reason_manifest_invalid():
    assert classify_failure_reason("Manifest validation failed: schema mismatch at expected_deliverables") == "manifest_invalid"


def test_classify_failure_reason_timeout():
    assert classify_failure_reason("Tool execution timed out after 300s") == "timeout"


def test_classify_failure_reason_exception():
    assert classify_failure_reason("Traceback (most recent call last): ...") == "exception"


def test_classify_failure_reason_other_for_unknown():
    # An empty failure with no diagnostic signal lands in 'other'.
    assert classify_failure_reason("the run did not complete") == "other"


def test_classify_failure_reason_other_for_none_input():
    assert classify_failure_reason(None) == "other"


def test_classify_failure_reason_dict_payload():
    assert classify_failure_reason({"is_error": True, "content": "manifest schema validation failed"}) == "manifest_invalid"


def test_classify_failure_reason_branch_precedence_cost_gate_wins():
    """When a response carries both cost-gate-block and manifest-invalid signals,
    cost_gate_block wins per the documented branch order."""
    assert classify_failure_reason(
        "MetaEnsemble cost gate blocked the dispatch; manifest schema also failed validation"
    ) == "cost_gate_block"


def test_classify_failure_reason_branch_precedence_manifest_over_timeout():
    """Manifest-invalid beats timeout when both signals appear."""
    assert classify_failure_reason("manifest validation timed out") == "manifest_invalid"


# --- extract_deliverable_path --------------------------------------------

def test_extract_deliverable_path_finds_reports_md():
    response = "I wrote the result to reports/review/auth-20260514.md for you to read."
    assert extract_deliverable_path(response) == "reports/review/auth-20260514.md"


def test_extract_deliverable_path_returns_none_when_absent():
    assert extract_deliverable_path("Just text, no path") is None


def test_extract_deliverable_path_handles_nested_paths():
    response = "Output: src/dashboard/reports/2026/may/index.md"
    assert extract_deliverable_path(response) == "src/dashboard/reports/2026/may/index.md"


def test_extract_deliverable_path_preserves_absolute_paths_with_spaces():
    path = "/Users/example/Projects/sample app/reports/review/output.md"
    response = f"Deliverable written at {path}"
    assert extract_deliverable_path(response) == path


def test_extract_deliverable_path_preserves_quoted_paths_with_spaces():
    path = "reports/review/sample report.md"
    response = f"Deliverable: `{path}`"
    assert extract_deliverable_path(response) == path


def test_extract_deliverable_path_finds_metaensemble_reports_md():
    response = "Wrote `.metaensemble/reports/audit/synthesis-20260620.md`."
    assert (
        extract_deliverable_path(response)
        == ".metaensemble/reports/audit/synthesis-20260620.md"
    )


# --- ensure_role / ensure_executor / ensure_task -------------------------

def test_ensure_role_is_idempotent(tmp_ledger: Ledger):
    ensure_role(tmp_ledger, "backend")
    ensure_role(tmp_ledger, "backend")
    rows = tmp_ledger._conn.execute(
        "SELECT * FROM roles WHERE role_id = 'backend'"
    ).fetchall()
    assert len(rows) == 1


def test_ensure_executor_creates_new(tmp_ledger: Ledger):
    ensure_role(tmp_ledger, "backend")
    result = ensure_executor(tmp_ledger, role_id="backend")
    assert result.created is True
    assert result.executor.alias.startswith("back-")


def test_ensure_executor_reuses_active(tmp_ledger: Ledger):
    ensure_role(tmp_ledger, "backend")
    a = ensure_executor(tmp_ledger, role_id="backend")
    b = ensure_executor(tmp_ledger, role_id="backend")
    assert a.executor.executor_id == b.executor.executor_id
    assert b.created is False


def test_ensure_executor_force_fresh_always_creates(tmp_ledger: Ledger):
    """`force_fresh=True` mints a new Executor even when an active one exists."""
    ensure_role(tmp_ledger, "backend")
    first = ensure_executor(tmp_ledger, role_id="backend")
    second = ensure_executor(tmp_ledger, role_id="backend", force_fresh=True)
    assert second.created is True
    assert second.executor.executor_id != first.executor.executor_id


def test_parse_markers_fresh():
    assert parse_markers("[fresh] do thing").get("fresh") == "1"
    assert "fresh" not in parse_markers("plain prompt")


def test_ensure_executor_continuation_overrides_default(tmp_ledger: Ledger):
    ensure_role(tmp_ledger, "backend")
    first = ensure_executor(tmp_ledger, role_id="backend")
    # Force-create a second one out of band so the default would prefer it.
    ensure_role(tmp_ledger, "other")
    ensure_executor(tmp_ledger, role_id="other")
    # Now continuing back to the first by alias.
    cont = ensure_executor(
        tmp_ledger,
        role_id="other",
        continuing_alias=first.executor.alias,
    )
    assert cont.executor.executor_id == first.executor.executor_id


def test_ensure_task_idempotent_and_records_manifest(tmp_ledger: Ledger):
    ensure_task(tmp_ledger, "task-001", "auth", manifest_path="/tmp/hm-x.yaml")
    ensure_task(tmp_ledger, "task-001", "auth", manifest_path="/tmp/hm-x.yaml")
    rows = tmp_ledger._conn.execute(
        "SELECT * FROM tasks WHERE task_id = 'task-001'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["manifest_path"] == "/tmp/hm-x.yaml"


# --- manifest_path_for ---------------------------------------------------

def test_manifest_path_for_resolves_existing(tmp_path: Path):
    state = tmp_path / ".metaensemble" / "state"
    manifests = tmp_path / ".metaensemble" / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "hm-abc.yaml").write_text("manifest_id: hm-abc\n")
    assert manifest_path_for(state, "hm-abc") == manifests / "hm-abc.yaml"


def test_manifest_path_for_returns_none_when_missing(tmp_path: Path):
    state = tmp_path / ".metaensemble" / "state"
    (tmp_path / ".metaensemble" / "manifests").mkdir(parents=True)
    assert manifest_path_for(state, "hm-missing") is None
