"""Regression gate: every documented Ledger field must populate as a
real column on a successful Run row.

The Ledger contract is:

    "The Ledger records the Executor, Role version, model, tool use,
    token cost, files touched, output, gate state, review findings, and
    final outcome."

This test asserts the columns exist *and* that the Run dataclass carries
them. Future drift between docs and code is caught here before it ships.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from metaensemble.lib.ledger import Run


CLAIMED_FIELDS = {
    # documented phrase                → Run dataclass attribute
    "executor":          "executor_id",
    "role_version":      "role_version",
    "model":             "model",
    "model_source":      "model_source",
    "requested_model":   "requested_model_tier",
    "tool_use":          "tool_use_json",
    "token_cost_in":     "tokens_in",
    "token_cost_out":    "tokens_out",
    "cache_read":        "cache_read_tokens",
    "cache_create":      "cache_create_tokens",
    "orchestration":     "orchestration_tokens",
    "files_touched":     "files_touched_json",
    "output":            "deliverable_ref_json",
    "gate_state":        "quality_state",
    "review_findings":   "review_findings_json",
    "outcome":           "outcome",
}


def test_run_dataclass_carries_every_claimed_field():
    """Every documented field exists on the Run dataclass."""
    fields = set(Run.__dataclass_fields__.keys())
    for label, attr in CLAIMED_FIELDS.items():
        assert attr in fields, f"missing Run.{attr} (documented as: {label})"


def test_runs_table_carries_every_claimed_column(tmp_ledger):
    """Every documented column exists on the runs table after init."""
    columns = {
        row[1]
        for row in tmp_ledger._conn.execute("PRAGMA table_info(runs)").fetchall()
    }
    for label, attr in CLAIMED_FIELDS.items():
        assert attr in columns, f"missing runs.{attr} (documented as: {label})"


def test_successful_run_persists_every_claimed_field(
    tmp_ledger, sample_executor, sample_task
):
    """Append a Run with every field populated. Read back and verify each is non-null."""
    run = Run(
        run_id="run-schema-1",
        executor_id=sample_executor.executor_id,
        task_id=sample_task,
        model="claude-sonnet-4-6",
        tokens_in=100,
        tokens_out=200,
        window_id="2026-05-19T10",
        started_ts=datetime.now(timezone.utc).isoformat(),
        ended_ts=datetime.now(timezone.utc).isoformat(),
        outcome="ok",
        brief_in_path=".metaensemble/briefs/in.json",
        brief_out_path=".metaensemble/briefs/out.json",
        deliverable_path="reports/x.md",
        failure_reason=None,
        quality_state="auto",
        quality_findings_json=json.dumps({"axes": []}),
        role_version="1.0.0",
        requested_model_tier="sonnet",
        model_source="transcript",
        deliverable_ref_json=json.dumps({"kind": "path", "value": "reports/x.md"}),
        files_touched_json=json.dumps(["src/foo.py"]),
        tool_use_json=json.dumps([{"name": "Edit", "count": 1}]),
        review_findings_json=json.dumps({"axes": []}),
        cache_read_tokens=500,
        cache_create_tokens=50,
        orchestration_tokens=120,
    )
    tmp_ledger.append_run(run)
    rows = tmp_ledger.get_recent_runs(limit=5)
    assert len(rows) == 1
    persisted = rows[0]
    assert persisted.model == "claude-sonnet-4-6"
    assert persisted.role_version == "1.0.0"
    assert persisted.requested_model_tier == "sonnet"
    assert persisted.model_source == "transcript"
    assert persisted.brief_out_path == ".metaensemble/briefs/out.json"
    assert persisted.deliverable_ref_json
    assert json.loads(persisted.deliverable_ref_json)["kind"] == "path"
    assert json.loads(persisted.files_touched_json) == ["src/foo.py"]
    assert json.loads(persisted.tool_use_json)[0]["count"] == 1
    assert persisted.cache_read_tokens == 500
    assert persisted.cache_create_tokens == 50
    assert persisted.orchestration_tokens == 120


def test_build_deliverable_ref_handles_all_three_kinds():
    from metaensemble.lib.recording import build_deliverable_ref

    # 1. Markdown report path wins when present.
    ref = build_deliverable_ref(
        "result",
        deliverable_path="reports/x.md",
    )
    assert ref == {"kind": "path", "value": "reports/x.md", "inferred": True}

    # 2. Short text answer becomes a summary ref.
    ref = build_deliverable_ref("Yes — the migration is safe under concurrent writes.")
    assert ref["kind"] == "summary"
    assert ref["value"].startswith("Yes")

    # 3. Empty response with file edits → hash ref.
    ref = build_deliverable_ref("", files_touched=("src/a.py", "src/b.py"))
    assert ref["kind"] == "hash"
    assert ref["value"].startswith("sha256:")

    # 4. Nothing → None.
    assert build_deliverable_ref("", files_touched=()) is None
