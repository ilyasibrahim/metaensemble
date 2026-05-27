"""Quality gate — verification companion to the cost gate.

The cost gate (`cost_gate.py`) decides whether a *proposed* dispatch is
affordable given window pressure and run size. The quality gate decides
whether the Deliverable an Executor *produced* clears five quality axes
the productive-engineering literature has converged on:

    Axis 1 — correctness        (pytest exit code)
    Axis 2 — security           (bandit + pip-audit)
    Axis 3 — maintainability    (ruff)
    Axis 4 — complexity         (radon cyclomatic)
    Axis 5 — coverage delta     (coverage.py)

Each axis returns AUTO, NOTIFY, or BLOCK against configurable thresholds
that default to industry-anchored values (SonarQube *Sonar Way*, Snyk
medium-severity default, NISTIR 8397's 80% coverage floor, McCabe's
10-and-15 complexity bands, DORA's elite CFR under 15%). The final gate
state is the worst of the available axes. Irreversibility and novelty
are not part of the quality gate; those remain cost-gate concerns.

The gate fires as a PostToolUse hook on the Agent tool. Quality is a
property of *output*, so the gate cannot evaluate before the Run
completes; that is the inversion of the cost gate's pre-flight check.

For tool-absent axes (no bandit installed, no ruff installed, etc.) the
runner returns None and the aggregator simply skips that axis. The gate
state is still computed from the remaining axes. This keeps the gate
useful for users who have not installed the full toolchain.

See ARCHITECTURE.md §10 for the design choices below; the defaults
encoded in `metaensemble/lib/config.py` anchor on SonarQube, Snyk, NISTIR
8397, McCabe, and DORA.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from metaensemble.lib.cost_gate import GateState


# --- Decision objects ---------------------------------------------------

@dataclass(frozen=True)
class QualityAxis:
    """One axis's contribution to the gate decision.

    `state` is its individual verdict; `findings` are short human-readable
    strings the BLOCK surface can show; `raw` carries the numeric value
    (defect count, complexity number, coverage percentage) for telemetry.
    """

    name: str
    state: GateState
    findings: tuple[str, ...] = ()
    raw: float | None = None


@dataclass(frozen=True)
class QualityGateDecision:
    """Aggregated outcome across the configured axes.

    `state` is the worst of the available axes; `axes` records each axis's
    individual contribution (including the skipped ones, marked AUTO with a
    `tool not installed` finding); `options` is the four-option surface
    presented to the Principal on NOTIFY or BLOCK; `summary` is the
    one-paragraph English block the hook writes back to the Coordinator.
    """

    state: GateState
    axes: tuple[QualityAxis, ...]
    options: tuple[dict, ...] = ()
    summary: str = ""


# --- Aggregation -------------------------------------------------------

_STATE_RANK = {
    GateState.AUTO: 0,
    GateState.NOTIFY: 1,
    GateState.BLOCK: 2,
}


def worst_state(axes: Iterable[QualityAxis]) -> GateState:
    """Pick the worst state across axes. Empty iterable returns AUTO."""
    worst = GateState.AUTO
    for axis in axes:
        if _STATE_RANK[axis.state] > _STATE_RANK[worst]:
            worst = axis.state
    return worst


# Standard option set the gate surfaces to the Principal. The cost gate
# offers cost-shaped options (drop tier, split, pause); the quality gate
# offers quality-shaped options (accept-and-log, peer review, re-dispatch
# with stricter constraints, split).
_QUALITY_OPTIONS = (
    {
        "id": 1,
        "label": "Accept the Deliverable as-is — log the override in the Ledger",
    },
    {
        "id": 2,
        "label": "Send to peer review — a second Executor of a different Role checks the findings",
    },
    {
        "id": 3,
        "label": "Re-dispatch with the findings folded into the Manifest's acceptance criteria",
    },
    {
        "id": 4,
        "label": "Split — dispatch the remediation as a separate Task",
    },
)


def _format_summary(state: GateState, axes: tuple[QualityAxis, ...]) -> str:
    """Build a tight English summary for the Coordinator to read out."""
    if state == GateState.AUTO:
        return "Quality gate: all configured axes clear."

    failed = [a for a in axes if a.state != GateState.AUTO]
    pieces = []
    for a in failed:
        if a.findings:
            head = a.findings[0]
            extra = f" (+{len(a.findings) - 1} more)" if len(a.findings) > 1 else ""
            pieces.append(f"{a.name} → {a.state.value}: {head}{extra}")
        else:
            pieces.append(f"{a.name} → {a.state.value}")
    body = "; ".join(pieces)

    if state == GateState.BLOCK:
        return f"Quality gate would BLOCK this Deliverable. Failures: {body}."
    return f"Quality gate would NOTIFY. Concerns: {body}."


def build_decision(axes: tuple[QualityAxis, ...]) -> QualityGateDecision:
    """Aggregate per-axis results into the final decision.

    Worst-of-axes for state. Options are surfaced whenever state is NOTIFY
    or BLOCK — this is the grammar fix the Principal requested: NOTIFY no
    longer hides the choice surface, it just defaults to "proceed" rather
    than pausing.
    """
    state = worst_state(axes)
    options = _QUALITY_OPTIONS if state != GateState.AUTO else ()
    summary = _format_summary(state, axes)
    return QualityGateDecision(
        state=state,
        axes=axes,
        options=options,
        summary=summary,
    )
