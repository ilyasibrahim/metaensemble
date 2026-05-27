"""Headline metrics for the evaluation harness.

`pass@budget` is the primary correctness metric (no overspending wins);
`quality_per_1k_tokens` and
`orchestration_overhead_ratio` are the efficiency primaries. The
supporting metrics expose reliability and concision so a "pass" that
came from a 20-page report carries less weight than a one-line answer.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class WilsonInterval:
    """Wilson score interval at a given confidence level."""

    point: float
    lo: float
    hi: float
    n: int

    def __str__(self) -> str:
        return f"{self.point:.3f} (95% CI: {self.lo:.3f}–{self.hi:.3f}, n={self.n})"


def wilson_95(successes: int, n: int) -> WilsonInterval:
    """Wilson score confidence interval at 95%.

    Standard recipe (z = 1.96). Returns (point estimate, lo, hi, n).
    On n = 0, returns (0.0, 0.0, 1.0, 0) so the cell still produces
    a number rather than a NaN that breaks markdown rendering.
    """
    if n <= 0:
        return WilsonInterval(point=0.0, lo=0.0, hi=1.0, n=0)
    z = 1.96
    p = successes / n
    denom = 1.0 + (z * z) / n
    center = (p + (z * z) / (2 * n)) / denom
    halfwidth = (z * math.sqrt((p * (1 - p) + (z * z) / (4 * n)) / n)) / denom
    return WilsonInterval(
        point=p,
        lo=max(0.0, center - halfwidth),
        hi=min(1.0, center + halfwidth),
        n=n,
    )


@dataclass(frozen=True)
class CellMetrics:
    """Per-cell aggregate over `n` seeds."""

    cell_id: str
    pass_at_budget: WilsonInterval
    quality_per_1k_tokens: float
    orchestration_overhead_ratio: float | None
    failed_run_token_waste: int
    time_to_useful_deliverable_ms_p50: float
    minimum_useful_answer_score: float
    total_tokens: int


def compute_cell_metrics(
    *,
    cell_id: str,
    runs: list["RunOutcome"],
    baseline_total_tokens: int | None = None,
) -> CellMetrics:
    """Aggregate one cell's seeded runs into a CellMetrics row.

    `runs` is a list of `RunOutcome` records. `baseline_total_tokens`
    is the best-single-agent baseline's token total for the same
    suite, used to derive `orchestration_overhead_ratio`. When None
    (e.g. when the cell IS the baseline), the ratio is None.
    """
    n = len(runs)
    passes = sum(1 for r in runs if r.passed and not r.budget_exceeded)
    pass_at_budget = wilson_95(passes, n)

    passed_runs = [r for r in runs if r.passed]
    pass_total_tokens = sum(r.tokens_in + r.tokens_out for r in passed_runs)
    if passed_runs and pass_total_tokens > 0:
        score_sum = sum(r.quality_score for r in passed_runs)
        quality_per_1k = (score_sum / (pass_total_tokens / 1000.0))
    else:
        quality_per_1k = 0.0

    total_tokens = sum(r.tokens_in + r.tokens_out for r in runs)
    overhead = (
        total_tokens / baseline_total_tokens
        if baseline_total_tokens
        else None
    )

    failed = [r for r in runs if not r.passed or r.budget_exceeded]
    failed_waste = sum(r.tokens_in + r.tokens_out for r in failed)

    if passed_runs:
        latencies = sorted(r.duration_ms for r in passed_runs)
        p50 = latencies[len(latencies) // 2]
        muas = sum(r.minimum_useful_answer_score for r in passed_runs) / len(passed_runs)
    else:
        p50 = 0.0
        muas = 0.0

    return CellMetrics(
        cell_id=cell_id,
        pass_at_budget=pass_at_budget,
        quality_per_1k_tokens=quality_per_1k,
        orchestration_overhead_ratio=overhead,
        failed_run_token_waste=failed_waste,
        time_to_useful_deliverable_ms_p50=p50,
        minimum_useful_answer_score=muas,
        total_tokens=total_tokens,
    )


@dataclass(frozen=True)
class RunOutcome:
    """One seed × one task outcome that feeds into `compute_cell_metrics`."""

    task_id: str
    seed: int
    passed: bool
    quality_score: float            # 0.0 - 1.0, per the task's acceptance grading
    minimum_useful_answer_score: float  # 0.0 - 1.0, concision/brevity reward
    tokens_in: int
    tokens_out: int
    budget_exceeded: bool
    duration_ms: float
    failure_reason: str | None = None
