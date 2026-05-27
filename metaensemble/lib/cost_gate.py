"""Cost-gate evaluation — two-axis decision for MetaEnsemble dispatches.

The cost gate answers one question on every dispatch: *should this Run
auto-proceed, notify the Principal in passing, or block for explicit
approval?* The answer is the worst of two axes, with irreversibility
and novelty as independent hard-blocks on top.

Both axes are anchored to the WINDOW CAPACITY — a fixed reference the
Principal can reason about — rather than to a moving denominator like
"percent of remaining window."

Axis 1 — run size (vs capacity):
    AUTO    when run_pct <= run_soft
    NOTIFY  when run_soft < run_pct <= run_hard
    BLOCK   when run_pct > run_hard

Axis 2 — window headroom (vs capacity):
    AUTO    when remaining_pct >= window_warn
    NOTIFY  when window_block <= remaining_pct < window_warn
    BLOCK   when remaining_pct < window_block

Final = worst(Axis 1, Axis 2). Irreversibility and novelty are hard-block
overrides on top.

Defaults (configurable via budgets.yaml):
    run_soft_pct_of_capacity   = 20   (20% of capacity = a substantive dispatch)
    run_hard_pct_of_capacity   = 40   (40% = an outsized dispatch)
    window_warn_pct_remaining  = 30   (warn when < 30% of window remains)
    window_block_pct_remaining = 10   (block all when < 10% remains)
    window_capacity_tokens     = 88000 (Max-5 cap; override per plan)

Capacity is normally derived live from the runtime's `rate_limits`
feed (see metaensemble.lib.native_state); the configured number is the
fallback when no fresh native data is available.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from metaensemble.lib.config import BudgetConfig


class GateState(str, Enum):
    AUTO = "auto"
    NOTIFY = "notify"
    BLOCK = "block"

    def worse_than(self, other: "GateState") -> bool:
        order = {GateState.AUTO: 0, GateState.NOTIFY: 1, GateState.BLOCK: 2}
        return order[self] > order[other]


@dataclass(frozen=True)
class CostGateDecision:
    """Outcome of cost-gate evaluation.

    `run_pct_of_capacity` and `remaining_pct_of_capacity` are the values
    the gate decides on. `estimated_pct_of_window` carries the same
    number as `run_pct_of_capacity` and exists for callers that want
    a single percentage to render without inspecting both axes.
    """

    state: GateState
    reason: str
    estimated_pct_of_window: float
    run_pct_of_capacity: float = 0.0
    remaining_pct_of_capacity: float = 0.0


def _evaluate_run_axis(
    estimated_tokens: int,
    capacity: int,
    config: BudgetConfig,
) -> tuple[GateState, str, float]:
    """Score the run-size axis against capacity."""
    pct = (estimated_tokens / max(capacity, 1)) * 100.0
    if pct > config.run_hard_pct_of_capacity:
        return (
            GateState.BLOCK,
            f"this dispatch is {pct:.1f}% of window capacity "
            f"(hard limit {config.run_hard_pct_of_capacity:.0f}%)",
            pct,
        )
    if pct > config.run_soft_pct_of_capacity:
        return (
            GateState.NOTIFY,
            f"this dispatch is {pct:.1f}% of window capacity "
            f"(soft limit {config.run_soft_pct_of_capacity:.0f}%)",
            pct,
        )
    return GateState.AUTO, f"this dispatch is {pct:.1f}% of window capacity", pct


def _evaluate_window_axis(
    remaining_window_tokens: int,
    capacity: int,
    used_tokens: int,
    config: BudgetConfig,
) -> tuple[GateState, str, float]:
    """Score the window-headroom axis against capacity.

    When observed usage already exceeds the configured capacity, the
    capacity setting is almost certainly wrong for the user's plan. In
    that case the window axis cannot give a trustworthy signal, so we
    return AUTO and let the run-size axis (which is anchored to the same
    capacity but at least compares apples to apples for the dispatch
    being evaluated) carry the gate alone.
    """
    if used_tokens > capacity:
        return (
            GateState.AUTO,
            "configured capacity is below observed usage; treating window "
            "as unconstrained — fix `window_capacity_tokens` in budgets.yaml",
            0.0,
        )
    remaining_pct = (max(remaining_window_tokens, 0) / max(capacity, 1)) * 100.0
    if remaining_pct < config.window_block_pct_remaining:
        return (
            GateState.BLOCK,
            f"only {remaining_pct:.1f}% of the window remains "
            f"(block threshold {config.window_block_pct_remaining:.0f}%)",
            remaining_pct,
        )
    if remaining_pct < config.window_warn_pct_remaining:
        return (
            GateState.NOTIFY,
            f"only {remaining_pct:.1f}% of the window remains "
            f"(warn threshold {config.window_warn_pct_remaining:.0f}%)",
            remaining_pct,
        )
    return GateState.AUTO, f"{remaining_pct:.1f}% of window remains", remaining_pct


def evaluate(
    *,
    estimated_tokens: int,
    remaining_window_tokens: int,
    is_irreversible: bool,
    is_novel_pattern: bool,
    config: BudgetConfig,
    window_capacity_tokens: int | None = None,
    used_window_tokens: int | None = None,
) -> CostGateDecision:
    """Two-axis cost-gate evaluation. Pure function; no I/O.

    The final state is the worst of (run-size, window-headroom).
    Irreversibility and novelty are independent hard-block overrides
    on top so they show up with their own reason strings rather than
    being averaged into a percentage.

    `used_window_tokens` is optional; when supplied it allows the gate
    to detect the misconfigured-capacity case (observed usage exceeds
    configured capacity) and degrade the window axis to AUTO rather
    than block the Principal on a wrong cap. When not supplied, the
    gate infers used = capacity - remaining, which is correct as long
    as the caller hasn't independently observed over-capacity usage.
    """
    capacity = (
        window_capacity_tokens
        if window_capacity_tokens is not None
        else config.window_capacity_tokens
    )
    if capacity <= 0:
        capacity = 1

    # Hard-block overrides come first so they own the surfaced reason.
    if is_irreversible:
        run_pct = (estimated_tokens / capacity) * 100.0
        rem_pct = (max(remaining_window_tokens, 0) / capacity) * 100.0
        return CostGateDecision(
            state=GateState.BLOCK,
            reason="action is irreversible; mandatory peer review and Principal approval",
            estimated_pct_of_window=run_pct,
            run_pct_of_capacity=run_pct,
            remaining_pct_of_capacity=rem_pct,
        )
    if is_novel_pattern and config.novelty_block_first_run:
        run_pct = (estimated_tokens / capacity) * 100.0
        rem_pct = (max(remaining_window_tokens, 0) / capacity) * 100.0
        return CostGateDecision(
            state=GateState.BLOCK,
            reason="novel Manifest pattern; first occurrence blocks for Principal review",
            estimated_pct_of_window=run_pct,
            run_pct_of_capacity=run_pct,
            remaining_pct_of_capacity=rem_pct,
        )

    run_state, run_reason, run_pct = _evaluate_run_axis(
        estimated_tokens, capacity, config
    )
    used_tokens = (
        used_window_tokens
        if used_window_tokens is not None
        else max(0, capacity - max(0, remaining_window_tokens))
    )
    win_state, win_reason, win_pct = _evaluate_window_axis(
        remaining_window_tokens, capacity, used_tokens, config
    )

    # Final state is the worst of the two axes.
    if win_state.worse_than(run_state):
        return CostGateDecision(
            state=win_state, reason=win_reason,
            estimated_pct_of_window=run_pct,
            run_pct_of_capacity=run_pct,
            remaining_pct_of_capacity=win_pct,
        )
    if run_state.worse_than(win_state):
        return CostGateDecision(
            state=run_state, reason=run_reason,
            estimated_pct_of_window=run_pct,
            run_pct_of_capacity=run_pct,
            remaining_pct_of_capacity=win_pct,
        )
    # Tie — both AUTO, both NOTIFY, or both BLOCK. Pick the run reason for
    # AUTO (informational), the window reason for NOTIFY/BLOCK (the more
    # actionable framing — the Principal can fix window pressure by waiting
    # for the next bucket, while run size requires a Manifest change).
    if run_state is GateState.AUTO:
        return CostGateDecision(
            state=GateState.AUTO, reason=run_reason,
            estimated_pct_of_window=run_pct,
            run_pct_of_capacity=run_pct,
            remaining_pct_of_capacity=win_pct,
        )
    combined = f"{win_reason}; also {run_reason}"
    return CostGateDecision(
        state=run_state, reason=combined,
        estimated_pct_of_window=run_pct,
        run_pct_of_capacity=run_pct,
        remaining_pct_of_capacity=win_pct,
    )


# --- Reversibility classifier -------------------------------------------

_FILE_MUTATION_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def _targets_existing_file(tool_name: str, tool_input: dict) -> bool:
    """True when a file-mutating tool targets a file already on disk."""
    if tool_name not in _FILE_MUTATION_TOOLS or not isinstance(tool_input, dict):
        return False
    for key in ("file_path", "path", "notebook_path"):
        raw = tool_input.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        try:
            if candidate.exists() and candidate.is_file():
                return True
        except OSError:
            continue
    return False


def is_action_irreversible(
    tool_name: str,
    tool_input: dict,
    irreversible_patterns: list[str],
) -> bool:
    """Match a tool invocation against the configured irreversible-action list.

    The default patterns are written as human-readable hints in `budgets.yaml`
    and matched here against the actual tool name and parameters. This is a
    conservative classifier: when in doubt, return True so the cost gate
    blocks for explicit Principal review.
    """
    haystacks = [tool_name]
    if isinstance(tool_input, dict):
        for v in tool_input.values():
            if isinstance(v, str):
                haystacks.append(v)
    joined = " ".join(haystacks)

    for pattern in irreversible_patterns:
        if pattern.startswith("Bash matching "):
            if tool_name == "Bash":
                regex = pattern[len("Bash matching ") :]
                if re.search(regex, joined):
                    return True
            continue
        if pattern == "Write to existing files":
            if _targets_existing_file(tool_name, tool_input):
                return True
            continue
        if pattern == "any non-localhost network call":
            if tool_name in {"WebFetch", "WebSearch"}:
                return True
            continue
        if pattern.lower() in joined.lower():
            return True

    return False
