"""Configuration loader for MetaEnsemble.

Reads `~/.metaensemble/budgets.yaml` and project-level overrides at
`<project>/.metaensemble/budgets.yaml`, merging per the Core / User / Project
layering rules in ARCHITECTURE.md §4. Falls back to hard-coded defaults when
no config exists, so the system works on a fresh install without setup.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# --- Defaults (encoded once; PERFORMANCE.md §3 R5: bounded by design) -----

_DEFAULT_THRESHOLDS = {
    # Run-size axis. Expressed as a percentage of the WINDOW CAPACITY, not
    # of what is left. Capacity is a fixed reference; "block any single
    # dispatch that exceeds 40% of my whole window" is a sentence the
    # Principal can reason about.
    "run_soft_pct_of_capacity": 20,   # one substantive dispatch
    "run_hard_pct_of_capacity": 40,   # outsized dispatch — needs explicit OK
    # Window-headroom axis. Expressed as the percentage of capacity STILL
    # REMAINING. "Warn when only 30% of the window is left" / "Block when
    # only 10% is left" — direct, easy to tune.
    "window_warn_pct_remaining": 30,
    "window_block_pct_remaining": 10,
    # Capacity, in tokens, for the user's plan's 5-hour window. The cost
    # gate normally derives this live from the runtime's `rate_limits`
    # feed (see metaensemble.lib.native_state); the configured number is the
    # fallback when no fresh native data is available.
    "window_capacity_tokens": 88_000,
}

_DEFAULT_IRREVERSIBLE_ACTIONS = [
    "Write to existing files",
    r"Bash matching git push|rm |DROP |DELETE ",
    "any non-localhost network call",
]

_DEFAULT_NOVELTY = {
    "treat_first_run_of_pattern_as_block": True,
    "drop_to_notify_after_n_runs": 2,
    "drop_to_auto_after_n_runs": 3,
}

_DEFAULT_CAPACITY_CALIBRATION = {
    # When true, MetaEnsemble derives the window capacity from the
    # user's observed historical peak (with 10% headroom) instead of
    # the manual `window_capacity_tokens` value. The manual value is
    # used as a FLOOR — if the user has never run a large session, we
    # use the manual default. When false, only the manual value is
    # used (useful for testing / pinned reproducibility).
    "auto_calibrate_capacity": True,
}


@dataclass(frozen=True)
class BudgetConfig:
    """Cost-gate configuration. See ARCHITECTURE.md §9.

    Two-axis design. A dispatch is evaluated against:
      Axis 1 (run size): how large is this single dispatch relative to
        the window capacity?
      Axis 2 (window headroom): how much of the window is still available?

    Each axis independently can escalate the gate state to NOTIFY or BLOCK.
    The final state is the worst of the two. Irreversibility and novelty
    are independent hard-blocks on top.
    """

    run_soft_pct_of_capacity: float
    run_hard_pct_of_capacity: float
    window_warn_pct_remaining: float
    window_block_pct_remaining: float
    window_capacity_tokens: int

    irreversible_actions: list[str] = field(default_factory=list)
    novelty_block_first_run: bool = True
    novelty_drop_to_notify_after: int = 2
    novelty_drop_to_auto_after: int = 3
    auto_calibrate_capacity: bool = True


def _load_yaml_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open() as f:
        return yaml.safe_load(f) or {}


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Shallow merge per layer; overlay wins. Lists replace rather than concat."""
    result = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _merge(result[k], v)
        else:
            result[k] = v
    return result


def load_budget_config(
    user_path: Path | None = None,
    project_path: Path | None = None,
) -> BudgetConfig:
    """Load and merge budget config. Core defaults <- user <- project.

    Args:
        user_path: usually `~/.metaensemble/budgets.yaml`. Defaults to that path.
        project_path: usually `<cwd>/.metaensemble/budgets.yaml`. Defaults to that.

    Returns:
        BudgetConfig with merged values; never raises on missing files.
    """
    if user_path is None:
        user_path = Path.home() / ".metaensemble" / "budgets.yaml"
    if project_path is None:
        project_path = Path.cwd() / ".metaensemble" / "budgets.yaml"

    core_defaults = {
        "thresholds": dict(_DEFAULT_THRESHOLDS),
        "irreversible_actions": list(_DEFAULT_IRREVERSIBLE_ACTIONS),
        "novelty": dict(_DEFAULT_NOVELTY),
        "capacity_calibration": dict(_DEFAULT_CAPACITY_CALIBRATION),
    }

    merged = _merge(core_defaults, _load_yaml_if_exists(user_path))
    merged = _merge(merged, _load_yaml_if_exists(project_path))

    thresholds = merged.get("thresholds", {})
    novelty = merged.get("novelty", {})
    calibration = merged.get("capacity_calibration", {})

    return BudgetConfig(
        run_soft_pct_of_capacity=float(
            thresholds.get("run_soft_pct_of_capacity",
                           _DEFAULT_THRESHOLDS["run_soft_pct_of_capacity"])
        ),
        run_hard_pct_of_capacity=float(
            thresholds.get("run_hard_pct_of_capacity",
                           _DEFAULT_THRESHOLDS["run_hard_pct_of_capacity"])
        ),
        window_warn_pct_remaining=float(
            thresholds.get("window_warn_pct_remaining",
                           _DEFAULT_THRESHOLDS["window_warn_pct_remaining"])
        ),
        window_block_pct_remaining=float(
            thresholds.get("window_block_pct_remaining",
                           _DEFAULT_THRESHOLDS["window_block_pct_remaining"])
        ),
        window_capacity_tokens=int(
            thresholds.get("window_capacity_tokens",
                           _DEFAULT_THRESHOLDS["window_capacity_tokens"])
        ),
        irreversible_actions=list(
            merged.get("irreversible_actions", _DEFAULT_IRREVERSIBLE_ACTIONS)
        ),
        novelty_block_first_run=bool(
            novelty.get("treat_first_run_of_pattern_as_block",
                        _DEFAULT_NOVELTY["treat_first_run_of_pattern_as_block"])
        ),
        novelty_drop_to_notify_after=int(
            novelty.get("drop_to_notify_after_n_runs",
                        _DEFAULT_NOVELTY["drop_to_notify_after_n_runs"])
        ),
        novelty_drop_to_auto_after=int(
            novelty.get("drop_to_auto_after_n_runs",
                        _DEFAULT_NOVELTY["drop_to_auto_after_n_runs"])
        ),
        auto_calibrate_capacity=bool(
            calibration.get("auto_calibrate_capacity",
                            _DEFAULT_CAPACITY_CALIBRATION["auto_calibrate_capacity"])
        ),
    )


def effective_capacity_tokens(
    config: BudgetConfig,
    home: Path | None = None,
) -> int:
    """Return the capacity the cost gate should use right now.

    Priority order:
      1. Native rate-limit data captured by the statusline script. The
         runtime exposes `used_percentage` for the 5-hour window
         directly. Combined with the metered burn we observe in the
         runtime jsonls (the absolute token count), we derive
         capacity = burn / (used_percentage / 100). This is the only
         trustworthy source — the runtime knows the user's actual
         plan cap.
      2. `config.window_capacity_tokens` as a fixed fallback when no
         fresh native data is available.

    Critically, we no longer derive capacity from observed historical
    peaks. That heuristic was unreliable across plan changes — the
    user's plan can shift between yesterday and today, and the only
    source that updates with the plan is the runtime itself.
    """
    if not config.auto_calibrate_capacity:
        return config.window_capacity_tokens

    from metaensemble.lib.native_state import load_native_rate_limits
    from metaensemble.lib.runtime_state import get_window_burn, window_id_for

    native = load_native_rate_limits()
    if (
        native is not None
        and native.is_fresh
        and native.five_hour is not None
        and native.five_hour.used_percentage > 1.0
    ):
        # Derive capacity from authoritative percentage + observed burn.
        burn = get_window_burn(window_id=window_id_for())
        observed_used = burn.input_tokens + burn.output_tokens
        if observed_used > 0:
            derived = int(observed_used / (native.five_hour.used_percentage / 100.0))
            # Cap absurdly large derivations (>20× the floor) to avoid
            # nonsense when burn and percentage disagree pathologically.
            ceiling = config.window_capacity_tokens * 20
            return min(max(derived, config.window_capacity_tokens), ceiling)

    return config.window_capacity_tokens


# --- Quality gate configuration ----------------------------------------
#
# Defaults anchor on industry standards: SonarQube *Sonar Way* (zero
# new issues, A/B/C maintainability grades), Snyk's medium-severity
# default block, NISTIR 8397's 80% line-coverage floor, McCabe's stable
# 10-and-15 complexity bands, DORA's elite change-failure rate ≤ 15%.

_DEFAULT_QUALITY = {
    "correctness": {
        "enabled": True,
        "notify_failures": 1,
        "block_failures": 3,
    },
    "security": {
        "enabled": True,
        # Snyk default: medium → NOTIFY, high/critical → BLOCK.
        "notify_severity": "medium",
        "block_severity": "high",
    },
    "maintainability": {
        "enabled": True,
        # Ruff issue counts mapped to SonarQube-style grades:
        # 0–5 issues = A/B (AUTO), 6–15 = C (NOTIFY), 16+ = D/E (BLOCK).
        "notify_issues": 6,
        "block_issues": 16,
    },
    "complexity": {
        "enabled": True,
        # McCabe cyclomatic complexity on changed functions.
        "notify_above": 10,
        "block_above": 15,
    },
    "coverage": {
        "enabled": True,
        # 5pp drop → NOTIFY/BLOCK; absolute floor 80% mirrors NISTIR 8397.
        "notify_drop_pp": 5.0,
        "block_drop_pp": 5.0,
        "block_absolute_below": 80.0,
    },
}


@dataclass(frozen=True)
class AxisConfig:
    """Per-axis thresholds for the quality gate."""

    enabled: bool = True
    # The remaining fields are axis-specific, populated from the YAML map.
    options: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.options.get(key, default)


@dataclass(frozen=True)
class QualityConfig:
    """Quality-gate configuration. See `metaensemble/lib/quality_gate.py`.

    Mirrors the BudgetConfig pattern: five axes, each independently
    configurable. A user-level `~/.metaensemble/quality.yaml` is merged
    with a project-level `<project>/.metaensemble/quality.yaml`. Anything
    not set falls through to the industry-anchored defaults above.
    """

    correctness: AxisConfig
    security: AxisConfig
    maintainability: AxisConfig
    complexity: AxisConfig
    coverage: AxisConfig


def load_quality_config(
    user_path: Path | None = None,
    project_path: Path | None = None,
) -> QualityConfig:
    """Load and merge quality config. Core defaults <- user <- project."""
    if user_path is None:
        user_path = Path.home() / ".metaensemble" / "quality.yaml"
    if project_path is None:
        project_path = Path.cwd() / ".metaensemble" / "quality.yaml"

    core_defaults = {axis: dict(values) for axis, values in _DEFAULT_QUALITY.items()}
    merged = _merge(core_defaults, _load_yaml_if_exists(user_path))
    merged = _merge(merged, _load_yaml_if_exists(project_path))

    def _axis(name: str) -> AxisConfig:
        values = merged.get(name, _DEFAULT_QUALITY[name])
        return AxisConfig(
            enabled=bool(values.get("enabled", True)),
            options={k: v for k, v in values.items() if k != "enabled"},
        )

    return QualityConfig(
        correctness=_axis("correctness"),
        security=_axis("security"),
        maintainability=_axis("maintainability"),
        complexity=_axis("complexity"),
        coverage=_axis("coverage"),
    )
