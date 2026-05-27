"""Multi-instance dispatch planning and synthesis.

This module exposes the multi-instance patterns from ARCHITECTURE §12 as
pure planning functions plus a single materializer that writes the plan
into the Ledger. Synthesis helpers combine the Deliverables that come
back from multi-Executor dispatches into the form the Principal sees.

The planners are pure (no I/O), so they remain cheap to call from
hooks and easy to unit-test. Materialization is the only function that
touches state.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from metaensemble.lib.ids import make_alias, uuid7
from metaensemble.lib.ledger import Executor, Ledger


# --- Plan data classes ----------------------------------------------------


@dataclass(frozen=True)
class ExecutorAssignment:
    """One Executor's role in a dispatch plan, prior to materialization.

    `divergent_focus` is set on fan-out assignments to capture the
    differentiating prompt each parallel Executor receives.
    """

    role_id: str
    alias_prefix: str
    model_tier: str
    role_in_plan: str  # "executor" | "reviewer"
    divergent_focus: str | None = None


@dataclass(frozen=True)
class DispatchPlan:
    pattern: str  # "solo" | "fanout" | "consensus" | "shadow" | "peer-review"
    task_type: str
    assignments: list[ExecutorAssignment] = field(default_factory=list)


# --- Planners (pure) -----------------------------------------------------


def plan_solo(
    role_id: str, alias_prefix: str, model_tier: str, task_type: str = "task"
) -> DispatchPlan:
    """Plan a single Executor for one Task."""
    return DispatchPlan(
        pattern="solo",
        task_type=task_type,
        assignments=[
            ExecutorAssignment(
                role_id=role_id,
                alias_prefix=alias_prefix,
                model_tier=model_tier,
                role_in_plan="executor",
            )
        ],
    )


def plan_fanout(
    role_id: str,
    alias_prefix: str,
    model_tier: str,
    n: int,
    foci: list[str] | None = None,
    task_type: str = "task",
) -> DispatchPlan:
    """Plan N Executors of one Role with divergent Briefs.

    `foci` is an optional list of differentiating prompts, one per Executor.
    When omitted, each assignment carries a None focus and the Coordinator
    is expected to compose differentiating Briefs from context.
    """
    if n < 2:
        raise ValueError(f"fanout requires n >= 2; got {n}")
    if foci is not None and len(foci) != n:
        raise ValueError(f"foci length ({len(foci)}) must match n ({n})")
    assignments = [
        ExecutorAssignment(
            role_id=role_id,
            alias_prefix=alias_prefix,
            model_tier=model_tier,
            role_in_plan="executor",
            divergent_focus=foci[i] if foci else None,
        )
        for i in range(n)
    ]
    return DispatchPlan(pattern="fanout", task_type=task_type, assignments=assignments)


def plan_consensus(
    role_id: str, alias_prefix: str, model_tier: str, n: int, task_type: str = "task"
) -> DispatchPlan:
    """Plan N Executors of one Role voting on the same Task."""
    if n < 2:
        raise ValueError(f"consensus requires n >= 2; got {n}")
    assignments = [
        ExecutorAssignment(
            role_id=role_id,
            alias_prefix=alias_prefix,
            model_tier=model_tier,
            role_in_plan="executor",
        )
        for _ in range(n)
    ]
    return DispatchPlan(pattern="consensus", task_type=task_type, assignments=assignments)


def plan_shadow(
    role_id: str, alias_prefix: str, tiers: list[str], task_type: str = "task"
) -> DispatchPlan:
    """Plan two Executors of the same Role at different model tiers."""
    if len(tiers) != 2:
        raise ValueError(f"shadow requires exactly 2 tiers; got {len(tiers)}")
    assignments = [
        ExecutorAssignment(
            role_id=role_id,
            alias_prefix=alias_prefix,
            model_tier=tier,
            role_in_plan="executor",
        )
        for tier in tiers
    ]
    return DispatchPlan(pattern="shadow", task_type=task_type, assignments=assignments)


def plan_peer_review(
    executor_role: str,
    executor_prefix: str,
    executor_tier: str,
    reviewer_specs: list[tuple[str, str, str]],
    task_type: str = "task",
) -> DispatchPlan:
    """Plan one executor + one or more reviewer Executors of different Roles.

    `reviewer_specs` is a list of (role_id, alias_prefix, model_tier) tuples,
    one per reviewer. Reviewer Roles must differ from the executor Role per
    ARCHITECTURE §12 (cross-Role review).
    """
    if not reviewer_specs:
        raise ValueError("peer-review requires at least one reviewer")
    for reviewer_role, _, _ in reviewer_specs:
        if reviewer_role == executor_role:
            raise ValueError(
                f"peer-review reviewer Role '{reviewer_role}' must differ from "
                f"executor Role '{executor_role}'"
            )
    assignments = [
        ExecutorAssignment(
            role_id=executor_role,
            alias_prefix=executor_prefix,
            model_tier=executor_tier,
            role_in_plan="executor",
        )
    ]
    for role_id, prefix, tier in reviewer_specs:
        assignments.append(
            ExecutorAssignment(
                role_id=role_id,
                alias_prefix=prefix,
                model_tier=tier,
                role_in_plan="reviewer",
            )
        )
    return DispatchPlan(pattern="peer-review", task_type=task_type, assignments=assignments)


# --- Materialization (writes to Ledger) ----------------------------------


def materialize_plan(
    ledger: Ledger,
    plan: DispatchPlan,
    task_id: str | None = None,
    parent_executor_id: str | None = None,
) -> tuple[str, list[Executor]]:
    """Create Task + Executor records in the Ledger from a DispatchPlan.

    Returns (task_id, list[Executor]). The Executors carry stable UUIDv7
    identities and aliases that survive across sessions.

    Lineage rules:
    - All Executors created from the same plan share a `parent_task_id`
      (the Task they jointly handle).
    - When `parent_executor_id` is provided, every Executor in the plan
      points at it as its parent in the `executors` table; this captures
      the case where this plan is a sub-dispatch from another Executor.
    """
    if task_id is None:
        task_id = f"task-{uuid7().hex[:12]}"

    now = datetime.now(timezone.utc).isoformat()
    ledger.ensure_task(
        task_id=task_id,
        task_type=plan.task_type,
        status="open",
        created_ts=now,
    )

    created: list[Executor] = []
    for assignment in plan.assignments:
        executor_uuid = uuid7()
        # Try a few times for alias uniqueness; collisions are rare.
        alias: str | None = None
        for _ in range(8):
            candidate = make_alias(assignment.alias_prefix, uuid7())
            if ledger.get_executor_by_alias(candidate) is None:
                alias = candidate
                break
        if alias is None:
            raise RuntimeError(
                f"alias collision after 8 attempts for prefix {assignment.alias_prefix!r}"
            )

        executor = Executor(
            executor_id=str(executor_uuid),
            alias=alias,
            role_id=assignment.role_id,
            parent_executor_id=parent_executor_id,
            created_ts=now,
            last_seen_ts=now,
            status="active",
        )
        ledger.upsert_executor(executor)
        created.append(executor)

    return task_id, created


# --- Synthesis (pure functions over Deliverable content) ------------------


SUMMARY_HEADING = re.compile(
    r"^##\s+(summary|what was done)\b", re.IGNORECASE | re.MULTILINE
)


@dataclass(frozen=True)
class SynthesisResult:
    """Combined output of a multi-Executor dispatch, formatted for the Principal."""

    pattern: str
    majority_position: str | None
    minority_positions: list[str]
    combined_summary: str


def _extract_summary(deliverable_text: str) -> str:
    """Extract the leading summary section of a Deliverable, or a truncated head if absent.

    Looks for the first heading matching "## Summary" or "## What was done".
    Returns the content from that heading to the next "##" boundary, or the
    first 300 characters if no summary heading is present.
    """
    match = SUMMARY_HEADING.search(deliverable_text)
    if not match:
        head = deliverable_text.strip()[:300]
        return head + ("..." if len(deliverable_text) > 300 else "")
    start = match.start()
    # Find the next "## " heading after the summary
    next_heading = re.search(r"^##\s+", deliverable_text[match.end():], re.MULTILINE)
    end = match.end() + next_heading.start() if next_heading else len(deliverable_text)
    return deliverable_text[start:end].strip()


def synthesize_fanout(deliverable_texts: list[str]) -> SynthesisResult:
    """Combine N divergent Deliverables. Surface each Executor's distinct position."""
    if not deliverable_texts:
        raise ValueError("fanout synthesis requires at least one Deliverable")
    summaries = [_extract_summary(t) for t in deliverable_texts]
    sections = [
        f"### Perspective {i + 1}\n\n{summary}"
        for i, summary in enumerate(summaries)
    ]
    body = "\n\n".join(sections)
    return SynthesisResult(
        pattern="fanout",
        majority_position=None,
        minority_positions=summaries,
        combined_summary=f"## Fan-out synthesis ({len(deliverable_texts)} perspectives)\n\n{body}",
    )


def synthesize_consensus(deliverable_texts: list[str]) -> SynthesisResult:
    """Combine N same-Role Deliverables into a majority/dissent report.

    The simple heuristic: when all summaries are non-trivially similar by
    string-overlap, treat the first as majority and mark none as dissent;
    when they diverge, present each as a separate position. A real
    consensus synthesizer would use a model call; this function avoids
    one to keep the lib free of model dependencies.
    """
    if not deliverable_texts:
        raise ValueError("consensus synthesis requires at least one Deliverable")
    summaries = [_extract_summary(t) for t in deliverable_texts]

    sections = [
        f"### Reviewer {i + 1}\n\n{s}" for i, s in enumerate(summaries)
    ]
    body = "\n\n".join(sections)
    return SynthesisResult(
        pattern="consensus",
        majority_position=summaries[0],
        minority_positions=summaries[1:],
        combined_summary=(
            f"## Consensus synthesis ({len(deliverable_texts)} reviewers)\n\n"
            f"The Principal should read all positions before deciding; dissent is "
            f"surfaced rather than averaged.\n\n{body}"
        ),
    )


def synthesize_peer_review(
    executor_deliverable: str, reviewer_deliverables: list[str]
) -> SynthesisResult:
    """The executor's work alongside the reviewer findings. Dissent surfaces explicitly."""
    if not reviewer_deliverables:
        raise ValueError("peer-review synthesis requires at least one reviewer")
    executor_summary = _extract_summary(executor_deliverable)
    reviewer_summaries = [_extract_summary(t) for t in reviewer_deliverables]
    review_sections = [
        f"### Review {i + 1}\n\n{s}" for i, s in enumerate(reviewer_summaries)
    ]
    review_body = "\n\n".join(review_sections)
    return SynthesisResult(
        pattern="peer-review",
        majority_position=executor_summary,
        minority_positions=reviewer_summaries,
        combined_summary=(
            f"## Peer-review synthesis ({len(reviewer_deliverables)} reviewers)\n\n"
            f"### Executor's work\n\n{executor_summary}\n\n{review_body}"
        ),
    )
