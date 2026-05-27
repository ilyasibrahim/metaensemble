"""Tests for the multi-instance dispatch planner, materializer, and synthesizers."""
from __future__ import annotations

import pytest

from metaensemble.lib.dispatch import (
    materialize_plan,
    plan_consensus,
    plan_fanout,
    plan_peer_review,
    plan_shadow,
    plan_solo,
    synthesize_consensus,
    synthesize_fanout,
    synthesize_peer_review,
)


# --- Planners (pure) ------------------------------------------------------


def test_plan_solo_emits_single_assignment():
    plan = plan_solo("backend", "be", "sonnet")
    assert plan.pattern == "solo"
    assert len(plan.assignments) == 1
    assert plan.assignments[0].role_id == "backend"
    assert plan.assignments[0].model_tier == "sonnet"


def test_plan_fanout_emits_n_assignments():
    plan = plan_fanout("backend", "be", "sonnet", n=3)
    assert plan.pattern == "fanout"
    assert len(plan.assignments) == 3
    assert all(a.role_id == "backend" for a in plan.assignments)


def test_plan_fanout_rejects_n_below_two():
    with pytest.raises(ValueError):
        plan_fanout("backend", "be", "sonnet", n=1)


def test_plan_fanout_attaches_foci_when_provided():
    plan = plan_fanout(
        "backend", "be", "sonnet", n=3,
        foci=["sql", "graphql", "rest"],
    )
    foci_seen = {a.divergent_focus for a in plan.assignments}
    assert foci_seen == {"sql", "graphql", "rest"}


def test_plan_fanout_rejects_mismatched_foci_length():
    with pytest.raises(ValueError):
        plan_fanout("backend", "be", "sonnet", n=3, foci=["one", "two"])


def test_plan_consensus_emits_n_assignments():
    plan = plan_consensus("code-quality", "cq", "sonnet", n=3)
    assert plan.pattern == "consensus"
    assert len(plan.assignments) == 3


def test_plan_shadow_requires_exactly_two_tiers():
    plan = plan_shadow("test-engineer", "te", ["sonnet", "haiku"])
    assert plan.pattern == "shadow"
    assert [a.model_tier for a in plan.assignments] == ["sonnet", "haiku"]


def test_plan_shadow_rejects_wrong_tier_count():
    with pytest.raises(ValueError):
        plan_shadow("test-engineer", "te", ["sonnet"])
    with pytest.raises(ValueError):
        plan_shadow("test-engineer", "te", ["sonnet", "haiku", "opus"])


def test_plan_peer_review_requires_different_reviewer_role():
    with pytest.raises(ValueError):
        plan_peer_review(
            executor_role="backend",
            executor_prefix="be",
            executor_tier="sonnet",
            reviewer_specs=[("backend", "be", "sonnet")],
        )


def test_plan_peer_review_marks_roles_correctly():
    plan = plan_peer_review(
        executor_role="backend",
        executor_prefix="be",
        executor_tier="sonnet",
        reviewer_specs=[
            ("code-quality", "cq", "sonnet"),
            ("security", "sec", "sonnet"),
        ],
    )
    assert plan.pattern == "peer-review"
    assert plan.assignments[0].role_in_plan == "executor"
    assert plan.assignments[1].role_in_plan == "reviewer"
    assert plan.assignments[2].role_in_plan == "reviewer"


# --- Materialization (writes to Ledger) ----------------------------------


def test_materialize_plan_creates_executors(tmp_ledger):
    tmp_ledger._conn.execute(
        "INSERT INTO roles (role_id, version, spec_path, model_tier, created_ts) "
        "VALUES (?, ?, ?, ?, ?)",
        ("backend", "1.0.0", "roles/backend.md", "sonnet", "2026-05-13T00:00:00"),
    )
    plan = plan_fanout("backend", "be", "sonnet", n=3)
    task_id, executors = materialize_plan(tmp_ledger, plan)
    assert task_id.startswith("task-")
    assert len(executors) == 3
    aliases = {e.alias for e in executors}
    assert len(aliases) == 3  # uniqueness honored


def test_materialize_plan_uses_provided_task_id(tmp_ledger):
    tmp_ledger._conn.execute(
        "INSERT INTO roles (role_id, version, spec_path, model_tier, created_ts) "
        "VALUES (?, ?, ?, ?, ?)",
        ("backend", "1.0.0", "roles/backend.md", "sonnet", "2026-05-13T00:00:00"),
    )
    plan = plan_solo("backend", "be", "sonnet", task_type="implement")
    task_id, executors = materialize_plan(tmp_ledger, plan, task_id="my-task")
    assert task_id == "my-task"
    # Task type was honored on the row
    row = tmp_ledger._conn.execute(
        "SELECT task_type FROM tasks WHERE task_id = ?", ("my-task",)
    ).fetchone()
    assert row["task_type"] == "implement"


def test_materialize_plan_records_parent_executor(tmp_ledger):
    tmp_ledger._conn.execute(
        "INSERT INTO roles (role_id, version, spec_path, model_tier, created_ts) "
        "VALUES (?, ?, ?, ?, ?)",
        ("backend", "1.0.0", "roles/backend.md", "sonnet", "2026-05-13T00:00:00"),
    )
    parent_plan = plan_solo("backend", "be", "sonnet")
    _, parent_execs = materialize_plan(tmp_ledger, parent_plan)
    parent_id = parent_execs[0].executor_id

    child_plan = plan_consensus("backend", "be", "sonnet", n=2)
    _, child_execs = materialize_plan(tmp_ledger, child_plan, parent_executor_id=parent_id)
    for child in child_execs:
        assert child.parent_executor_id == parent_id


# --- Synthesizers (pure) -------------------------------------------------


def _deliverable(summary_body: str, extra_section: str = "") -> str:
    return f"# Title\n\n## Summary\n\n{summary_body}\n\n{extra_section}".strip()


def test_synthesize_fanout_extracts_each_summary():
    d1 = _deliverable("Chose SQL because of consistency requirements.")
    d2 = _deliverable("Chose GraphQL because the frontend needs flexible queries.")
    d3 = _deliverable("Chose REST because client diversity demands a low-bar contract.")
    result = synthesize_fanout([d1, d2, d3])
    assert result.pattern == "fanout"
    assert "SQL" in result.combined_summary
    assert "GraphQL" in result.combined_summary
    assert "REST" in result.combined_summary
    assert len(result.minority_positions) == 3


def test_synthesize_fanout_rejects_empty():
    with pytest.raises(ValueError):
        synthesize_fanout([])


def test_synthesize_consensus_surfaces_dissent():
    d1 = _deliverable("Approve: no security issues.")
    d2 = _deliverable("Reject: SQL injection in line 14.")
    result = synthesize_consensus([d1, d2])
    assert result.majority_position is not None
    assert len(result.minority_positions) == 1
    assert "SQL injection" in result.combined_summary
    assert "Approve" in result.combined_summary


def test_synthesize_peer_review_surfaces_reviewer_findings():
    executor = _deliverable("Implemented the auth endpoints per the design spec.")
    r1 = _deliverable("Reviewed: missing rate-limit on /login.")
    result = synthesize_peer_review(executor, [r1])
    assert result.pattern == "peer-review"
    assert "Implemented the auth endpoints" in result.combined_summary
    assert "rate-limit" in result.combined_summary


def test_synthesize_peer_review_requires_a_reviewer():
    with pytest.raises(ValueError):
        synthesize_peer_review(_deliverable("done"), [])


def test_extract_summary_falls_back_to_head_when_no_summary_heading():
    from metaensemble.lib.dispatch import _extract_summary
    body = "# Title\n\nSome opening paragraph without a summary heading.\n\nMore content."
    out = _extract_summary(body)
    assert "Some opening" in out
