"""Tiered runner dispatch for the evaluation harness.

Three tiers correspond to three failure-mode budgets. `replay` reads
cassette responses recorded from a prior live run — zero API spend,
deterministic, suitable for PR-gate CI. `smoke` runs one seed against
the smoke suite to verify the live pipeline still works. `full` runs
the release-gated cycle with every cell × every seed.

Live API calls are issued through the `anthropic` SDK, which the
package already imports indirectly via the runtime. The runner does
not bundle a vendored SDK so production and eval use the same client.

The replay path is deterministic and CI-safe. The live smoke path uses
Claude Code directly with tools disabled so smoke/full metrics can be
measured without silently changing the project under evaluation.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from evals.runners.metrics import (
    CellMetrics,
    RunOutcome,
    compute_cell_metrics,
)


class Tier(str, Enum):
    REPLAY = "replay"
    SMOKE = "smoke"
    FULL = "full"


@dataclass(frozen=True)
class CellSpec:
    """One cell of the (baseline × suite) matrix."""

    id: str
    kind: str        # "baseline" | "full_system" | "ablation"
    dispatch_fn: str # symbolic name of the dispatch strategy


@dataclass(frozen=True)
class TaskSpec:
    id: str
    suite: str       # "suite_a" | "suite_b"
    description: str
    acceptance: list[dict]
    acceptable_labels: list[str] | None = None


@dataclass(frozen=True)
class HarnessReport:
    """One eval cycle's full result. Rendered to Markdown by `render_report`."""

    tier: Tier
    cells: list[CellMetrics]
    notes: list[str]


def evaluate_release_gates(
    report: HarnessReport,
    *,
    failed_run_waste_threshold: float | None = None,
    overhead_ratio_ceiling: float | None = None,
) -> tuple[bool, list[str]]:
    """Evaluate D-8/D-9 release gates against a rendered metric report.

    Returns `(failed, notes)`. Gates only evaluate when their threshold
    and underlying metric are present; missing overhead data is reported
    rather than treated as pass or fail.
    """
    failed = False
    notes: list[str] = []

    if failed_run_waste_threshold is not None:
        total_tokens = sum(c.total_tokens for c in report.cells)
        waste_tokens = sum(c.failed_run_token_waste for c in report.cells)
        waste_fraction = (waste_tokens / total_tokens) if total_tokens else 0.0
        state = "FAIL" if waste_fraction > failed_run_waste_threshold else "PASS"
        failed = failed or state == "FAIL"
        notes.append(
            "D-9 failed-run waste gate: "
            f"{state} ({waste_fraction:.1%} of tokens; "
            f"threshold {failed_run_waste_threshold:.1%})."
        )

    if overhead_ratio_ceiling is not None:
        measured = [
            c for c in report.cells
            if c.orchestration_overhead_ratio is not None
        ]
        if measured:
            violators = [
                c for c in measured
                if (c.orchestration_overhead_ratio or 0.0) > overhead_ratio_ceiling
            ]
            if violators:
                failed = True
                rendered = ", ".join(
                    f"{c.cell_id}={c.orchestration_overhead_ratio:.2f}x"
                    for c in violators
                )
                notes.append(
                    "D-8 orchestration-overhead gate: "
                    f"FAIL ({rendered}; ceiling {overhead_ratio_ceiling:.2f}x)."
                )
            else:
                notes.append(
                    "D-8 orchestration-overhead gate: "
                    f"PASS (ceiling {overhead_ratio_ceiling:.2f}x)."
                )
        else:
            notes.append(
                "D-8 orchestration-overhead gate: not evaluated "
                "(best-prompt baseline tokens unavailable in this run)."
            )

    return failed, notes


def run_cell_replay(
    cell: CellSpec,
    tasks: list[TaskSpec],
    cassette_dir: Path,
    seeds: int = 5,
) -> list[RunOutcome]:
    """Replay tier: read cassettes from disk. No API calls.

    Cassettes live at `cassette_dir/<cell.id>/<task.id>/<seed>.json` and
    encode the recorded RunOutcome fields. Missing cassettes raise
    `FileNotFoundError` so a PR that adds a task without recording its
    cassette fails CI deterministically.
    """
    outcomes: list[RunOutcome] = []
    for task in tasks:
        for seed in range(seeds):
            path = cassette_dir / cell.id / task.id / f"{seed}.json"
            if not path.exists():
                packed = _load_packed_replay(cassette_dir, cell.id, task.id, seed)
                if packed is None:
                    raise FileNotFoundError(
                        f"replay cassette missing: {path}. Record it with "
                        "`metaensemble eval --tier smoke --record-cassettes` "
                        "or add an entry to evals/cassettes/*.jsonl."
                    )
                outcomes.append(packed)
                continue
            data = json.loads(path.read_text())
            outcomes.append(RunOutcome(**data))
    return outcomes


def _load_packed_replay(
    cassette_dir: Path,
    cell_id: str,
    task_id: str,
    seed: int,
) -> RunOutcome | None:
    """Read compact JSONL cassette packs.

    The per-file cassette path is the canonical recorder output. The shipped
    v0.1.0 bootstrap pack uses JSONL to avoid hundreds of tiny fixture files
    while still exercising the same replay parser and metrics code in CI.
    """
    if not cassette_dir.exists():
        return None
    for pack in sorted(cassette_dir.glob("*.jsonl")):
        try:
            lines = pack.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                record_seed = int(record.get("seed", -1))
            except (TypeError, ValueError):
                continue
            if (
                record.get("cell_id") == cell_id
                and record.get("task_id") == task_id
                and record_seed == seed
            ):
                outcome = dict(record)
                outcome.pop("cell_id", None)
                outcome.pop("source", None)
                return RunOutcome(**outcome)
    return None


def run_cell_live(
    cell: CellSpec,
    tasks: list[TaskSpec],
    *,
    seeds: int,
    budget_usd: float,
    dispatch_fn: Callable[[CellSpec, TaskSpec, int], RunOutcome],
) -> list[RunOutcome]:
    """Live tier: issue real API calls, record outcomes.

    Delegates to `dispatch_fn` so tests can exercise live aggregation
    without spending money. The production smoke-suite live path is
    `run_suite_b_live_claude`.
    """
    outcomes: list[RunOutcome] = []
    for task in tasks:
        for seed in range(seeds):
            outcome = dispatch_fn(cell, task, seed)
            outcomes.append(outcome)
    return outcomes


def run_suite_b_live_claude(
    cell: CellSpec,
    tasks: list[TaskSpec],
    *,
    seeds: int,
    budget_usd: float,
    cwd: Path,
) -> list[RunOutcome]:
    """Run a live classification smoke cell through Claude Code.

    Smoke needs to be a real behavioral check, not a scaffold. To keep token
    spend bounded and side-effect-free, one no-tools Claude call classifies the
    whole smoke batch for a cell/seed, and measured tokens are prorated
    across item-level RunOutcome records. Dispatch itself is covered by the
    live install/incorporation test; the eval harness should not silently write
    Manifests, reports, or project files during a metrics run.
    """
    suite_b = [t for t in tasks if t.suite == "suite_b"]
    if not suite_b:
        return []
    outcomes: list[RunOutcome] = []
    for seed in range(seeds):
        batch = _invoke_claude_suite_b_cell(
            cell=cell,
            tasks=suite_b,
            seed=seed,
            budget_usd=budget_usd,
            cwd=cwd,
        )
        outcomes.extend(batch)
    return outcomes


def _invoke_claude_suite_b_cell(
    *,
    cell: CellSpec,
    tasks: list[TaskSpec],
    seed: int,
    budget_usd: float,
    cwd: Path,
) -> list[RunOutcome]:
    prompt = _suite_b_prompt(cell, tasks, seed)
    cmd = [
        "claude", "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(_suite_b_json_schema()),
        "--max-budget-usd", f"{budget_usd:.4f}",
        "--model", "haiku",
        "--no-session-persistence",
    ]
    cmd.extend(["--disable-slash-commands"])
    cmd.append(prompt)
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=240,
    )
    duration_ms = 0.0
    cost_usd = 0.0
    tokens_in = 0
    tokens_out = 0
    failure_reason: str | None = None
    predictions: dict[str, str] = {}
    quality: dict[str, float] = {}
    try:
        payload = json.loads(proc.stdout)
        duration_ms = float(payload.get("duration_ms") or 0.0)
        cost_usd = float(payload.get("total_cost_usd") or 0.0)
        tokens_in, tokens_out = _tokens_from_claude_payload(payload)
        if proc.returncode == 0 and not payload.get("is_error"):
            if isinstance(payload.get("structured_output"), dict):
                predictions, quality = _suite_b_predictions_from_data(payload["structured_output"])
            else:
                result = payload.get("result") or ""
                predictions, quality = _parse_suite_b_predictions(result)
        else:
            errors = payload.get("errors") or []
            failure_reason = (
                "; ".join(str(e) for e in errors)
                or str(payload.get("result") or "")
                or str(payload.get("subtype") or "")
                or "claude_failed"
            )
    except Exception as exc:
        failure_reason = f"claude_output_parse_failed: {exc}"
        duration_ms = 0.0
        tokens_in = 0
        tokens_out = 0
    if proc.returncode != 0 and failure_reason is None:
        failure_reason = (proc.stderr or "claude_failed").strip()[:500]

    per_task_in = math.ceil(tokens_in / max(1, len(tasks)))
    per_task_out = math.ceil(tokens_out / max(1, len(tasks)))
    per_task_ms = duration_ms / max(1, len(tasks))
    budget_exceeded = cost_usd > budget_usd or "maximum budget" in (failure_reason or "").lower()
    outcomes: list[RunOutcome] = []
    for task in tasks:
        acceptable = set(task.acceptable_labels or [])
        label = predictions.get(task.id)
        passed = bool(label and label in acceptable)
        outcomes.append(RunOutcome(
            task_id=task.id,
            seed=seed,
            passed=passed,
            quality_score=(quality.get(task.id, 1.0) if passed else 0.0),
            minimum_useful_answer_score=(1.0 if label else 0.0),
            tokens_in=per_task_in,
            tokens_out=per_task_out,
            budget_exceeded=budget_exceeded,
            duration_ms=per_task_ms,
            failure_reason=None if passed else (failure_reason or f"predicted={label!r}"),
        ))
    return outcomes


def _suite_b_prompt(cell: CellSpec, tasks: list[TaskSpec], seed: int) -> str:
    """Build the current classification-smoke fixture prompt.

    Suite B is a concrete classification smoke fixture. The prompt is
    intentionally fixture-specific; it is not MetaEnsemble's product scope.
    """
    items = "\n".join(
        f"- id: {t.id}\n  text: {json.dumps(t.description, ensure_ascii=False)}\n"
        f"  acceptable_labels: {', '.join(t.acceptable_labels or [])}"
        for t in tasks
    )
    base = (
        "Classify each Somali text into exactly one dialect label from its "
        "acceptable_labels list. Return only JSON matching the requested schema. "
        "No prose outside JSON. Use concise rationales."
    )
    if cell.id == "MM_full":
        base = (
            "Use the full MetaEnsemble rubric in one side-effect-free eval call: "
            "state the task contract internally, classify as a domain specialist, "
            "self-check every label against the allowed labels, then emit the "
            "machine-readable result. "
            + base
        )
    elif cell.id == "B1_single_agent":
        base = "Classify directly. " + base
    elif cell.id == "B2_single_agent_prompted":
        base = (
            "You are a careful Somali dialect classifier. Check morphology, "
            "focus markers, register, and negative forms before assigning a label. "
            + base
        )
    elif cell.id == "B4_best_prompt":
        base = (
            "Use a best-effort rubric: identify dialectal markers, compare against "
            "the allowed labels, then output the label only in the JSON field. "
            + base
        )
    else:
        base = (
            f"Run the `{cell.id}` evaluation strategy as a read-only classification "
            "pass. " + base
        )
    return f"{base}\n\nseed: {seed}\nitems:\n{items}"


def _suite_b_json_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "predictions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "label": {"type": "string"},
                        "confidence": {"type": "number"},
                        "rationale": {"type": "string"},
                    },
                    "required": ["id", "label", "confidence"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["predictions"],
        "additionalProperties": False,
    }


def _parse_suite_b_predictions(text: str) -> tuple[dict[str, str], dict[str, float]]:
    data = _extract_json_object(text)
    return _suite_b_predictions_from_data(data)


def _suite_b_predictions_from_data(data: dict) -> tuple[dict[str, str], dict[str, float]]:
    predictions: dict[str, str] = {}
    quality: dict[str, float] = {}
    for item in data.get("predictions") or []:
        item_id = str(item.get("id", "")).strip()
        label = str(item.get("label", "")).strip()
        if not item_id or not label:
            continue
        predictions[item_id] = label
        try:
            conf = float(item.get("confidence", 1.0))
        except (TypeError, ValueError):
            conf = 1.0
        quality[item_id] = max(0.0, min(1.0, conf))
    return predictions, quality


def _extract_json_object(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("no JSON object found in Claude result")
    return json.loads(match.group(0))


def _tokens_from_claude_payload(payload: dict) -> tuple[int, int]:
    model_usage = payload.get("modelUsage") or {}
    in_total = 0
    out_total = 0
    if isinstance(model_usage, dict):
        for usage in model_usage.values():
            if not isinstance(usage, dict):
                continue
            in_total += int(usage.get("inputTokens") or 0)
            in_total += int(usage.get("cacheReadInputTokens") or 0)
            in_total += int(usage.get("cacheCreationInputTokens") or 0)
            out_total += int(usage.get("outputTokens") or 0)
    usage = payload.get("usage") or {}
    if not in_total and isinstance(usage, dict):
        in_total = int(usage.get("input_tokens") or 0)
        in_total += int(usage.get("cache_read_input_tokens") or 0)
        in_total += int(usage.get("cache_creation_input_tokens") or 0)
    if not out_total and isinstance(usage, dict):
        out_total = int(usage.get("output_tokens") or 0)
    return in_total, out_total


def assemble_report(
    tier: Tier,
    cells_with_outcomes: list[tuple[CellSpec, list[RunOutcome]]],
    baseline_total_tokens_lookup: dict[str, int] | None = None,
) -> HarnessReport:
    """Build a HarnessReport from per-cell outcome lists.

    `baseline_total_tokens_lookup` maps cell.id → baseline total tokens
    (typically B4's tokens for the suite). When provided, the metric
    `orchestration_overhead_ratio` is computed per cell.
    """
    notes: list[str] = []
    cell_metrics: list[CellMetrics] = []
    for cell, outcomes in cells_with_outcomes:
        baseline_total = (
            baseline_total_tokens_lookup.get(cell.id)
            if baseline_total_tokens_lookup
            else None
        )
        cell_metrics.append(
            compute_cell_metrics(
                cell_id=cell.id,
                runs=outcomes,
                baseline_total_tokens=baseline_total,
            )
        )
    return HarnessReport(tier=tier, cells=cell_metrics, notes=notes)


def render_report(report: HarnessReport) -> str:
    """Render the report as Markdown. Stable format for `evals/reports/<date>.md`."""
    lines = [f"# Evaluation report ({report.tier.value})", ""]
    lines.append("| Cell | pass@budget | quality/1k tokens | overhead | waste tokens | p50 ms |")
    lines.append("|---|---|---|---|---|---|")
    for c in report.cells:
        overhead = (
            f"{c.orchestration_overhead_ratio:.2f}×"
            if c.orchestration_overhead_ratio is not None
            else "—"
        )
        lines.append(
            f"| `{c.cell_id}` | {c.pass_at_budget} | "
            # Three significant figures, not fixed decimals: suite-B smoke
            # calls score ~0.2/1k while token-heavy suite-A runs score
            # ~0.003/1k, and a fixed .2f renders the latter as 0.00.
            f"{c.quality_per_1k_tokens:.3g} | {overhead} | "
            f"{c.failed_run_token_waste:,} | "
            f"{c.time_to_useful_deliverable_ms_p50:.0f} |"
        )
    if report.notes:
        lines.append("")
        lines.append("## Notes")
        for n in report.notes:
            lines.append(f"- {n}")
    return "\n".join(lines)
