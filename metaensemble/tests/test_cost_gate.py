"""Tests for the two-axis cost gate (metaensemble/lib/cost_gate.py)."""
from __future__ import annotations

from metaensemble.lib.config import BudgetConfig, load_budget_config
from metaensemble.lib.cost_gate import (
    GateState,
    evaluate,
    is_action_irreversible,
)


def _config(
    *,
    run_soft: float = 5.0,
    run_hard: float = 15.0,
    window_warn: float = 30.0,
    window_block: float = 10.0,
    capacity: int = 88_000,
) -> BudgetConfig:
    """Build a BudgetConfig with the capacity-relative defaults."""
    return BudgetConfig(
        run_soft_pct_of_capacity=run_soft,
        run_hard_pct_of_capacity=run_hard,
        window_warn_pct_remaining=window_warn,
        window_block_pct_remaining=window_block,
        window_capacity_tokens=capacity,
        irreversible_actions=[
            "Write to existing files",
            "Bash matching git push|rm |DROP ",
        ],
    )


# --- Axis 1: run size vs capacity --------------------------------------


def test_small_run_with_full_window_auto():
    """A 1% dispatch with a full window should auto-proceed."""
    d = evaluate(
        estimated_tokens=880,  # 1% of 88k capacity
        remaining_window_tokens=88_000,
        is_irreversible=False,
        is_novel_pattern=False,
        config=_config(),
    )
    assert d.state == GateState.AUTO
    assert d.run_pct_of_capacity == 1.0


def test_run_between_soft_and_hard_notifies():
    """A 10% dispatch (between 5% soft and 15% hard) notifies."""
    d = evaluate(
        estimated_tokens=8_800,  # 10% of 88k
        remaining_window_tokens=88_000,
        is_irreversible=False,
        is_novel_pattern=False,
        config=_config(),
    )
    assert d.state == GateState.NOTIFY
    assert "10.0%" in d.reason
    assert "soft limit" in d.reason


def test_run_above_hard_blocks():
    """A 20% dispatch (above 15% hard) blocks."""
    d = evaluate(
        estimated_tokens=17_600,  # 20% of 88k
        remaining_window_tokens=88_000,
        is_irreversible=False,
        is_novel_pattern=False,
        config=_config(),
    )
    assert d.state == GateState.BLOCK
    assert "hard limit" in d.reason


# --- Axis 2: window headroom -------------------------------------------


def test_low_window_warns_even_for_small_run():
    """When only 20% of the window remains, even a 1% run notifies.

    As the window depletes, the system warns regardless of single-run
    size — that is what axis 2 is for.
    """
    d = evaluate(
        estimated_tokens=880,  # tiny dispatch
        remaining_window_tokens=17_600,  # 20% of capacity remaining
        is_irreversible=False,
        is_novel_pattern=False,
        config=_config(),
    )
    assert d.state == GateState.NOTIFY
    assert "20.0% of the window remains" in d.reason


def test_very_low_window_blocks_even_for_small_run():
    """When <10% of the window remains, every dispatch blocks."""
    d = evaluate(
        estimated_tokens=100,
        remaining_window_tokens=4_400,  # 5% of capacity remaining
        is_irreversible=False,
        is_novel_pattern=False,
        config=_config(),
    )
    assert d.state == GateState.BLOCK
    assert "5.0% of the window remains" in d.reason


def test_window_fully_remaining_passes_for_normal_run():
    """A normal 2% dispatch with 100% window remaining is AUTO on both axes."""
    d = evaluate(
        estimated_tokens=1_760,  # 2% of 88k
        remaining_window_tokens=88_000,
        is_irreversible=False,
        is_novel_pattern=False,
        config=_config(),
    )
    assert d.state == GateState.AUTO


# --- Tie-breaking + worst-of semantics ---------------------------------


def test_block_when_both_axes_disagree_picks_worse():
    """One axis NOTIFY and the other BLOCK → final state BLOCK."""
    d = evaluate(
        estimated_tokens=8_800,  # 10% — NOTIFY on axis 1
        remaining_window_tokens=4_400,  # 5% — BLOCK on axis 2
        is_irreversible=False,
        is_novel_pattern=False,
        config=_config(),
    )
    assert d.state == GateState.BLOCK
    # Window reason wins because BLOCK is the worse state.
    assert "window remains" in d.reason


def test_combined_reason_when_both_axes_warn():
    """Both axes NOTIFY → final state NOTIFY, reason combines both."""
    d = evaluate(
        estimated_tokens=7_040,  # 8% — NOTIFY on axis 1
        remaining_window_tokens=17_600,  # 20% remaining — NOTIFY on axis 2
        is_irreversible=False,
        is_novel_pattern=False,
        config=_config(),
    )
    assert d.state == GateState.NOTIFY
    # Combined reason mentions both axes.
    assert "window remains" in d.reason
    assert "window capacity" in d.reason


# --- Hard-block overrides (unchanged semantics) -------------------------


def test_irreversibility_always_blocks_even_when_cheap():
    d = evaluate(
        estimated_tokens=50,
        remaining_window_tokens=88_000,
        is_irreversible=True,
        is_novel_pattern=False,
        config=_config(),
    )
    assert d.state == GateState.BLOCK
    assert "irreversible" in d.reason


def test_novelty_blocks_when_configured():
    d = evaluate(
        estimated_tokens=50,
        remaining_window_tokens=88_000,
        is_irreversible=False,
        is_novel_pattern=True,
        config=_config(),
    )
    assert d.state == GateState.BLOCK
    assert "novel" in d.reason


def test_exhausted_window_blocks():
    """remaining=0 → axis 2 is 0% remaining, which is below block threshold."""
    d = evaluate(
        estimated_tokens=1,
        remaining_window_tokens=0,
        is_irreversible=False,
        is_novel_pattern=False,
        config=_config(),
    )
    assert d.state == GateState.BLOCK


# --- Reversibility classifier ------------------------------------------


def test_is_action_irreversible_for_git_push():
    assert is_action_irreversible(
        tool_name="Bash",
        tool_input={"command": "git push origin main"},
        irreversible_patterns=["Bash matching git push|rm "],
    )


def test_is_action_irreversible_for_web_fetch():
    assert is_action_irreversible(
        tool_name="WebFetch",
        tool_input={"url": "https://example.com"},
        irreversible_patterns=["any non-localhost network call"],
    )


def test_is_action_reversible_for_local_read():
    assert not is_action_irreversible(
        tool_name="Read",
        tool_input={"file_path": "/tmp/file"},
        irreversible_patterns=["Bash matching git push", "any non-localhost network call"],
    )


def test_write_to_existing_files_pattern_blocks_existing_file(tmp_path):
    target = tmp_path / "existing.py"
    target.write_text("print('old')\n")
    assert is_action_irreversible(
        tool_name="Write",
        tool_input={"file_path": str(target)},
        irreversible_patterns=["Write to existing files"],
    )


def test_write_to_existing_files_pattern_allows_new_file(tmp_path):
    target = tmp_path / "new.py"
    assert not is_action_irreversible(
        tool_name="Write",
        tool_input={"file_path": str(target)},
        irreversible_patterns=["Write to existing files"],
    )


# --- Config loader ------------------------------------------------------


def test_load_budget_config_defaults_to_known_values(tmp_path):
    cfg = load_budget_config(
        user_path=tmp_path / "missing-user.yaml",
        project_path=tmp_path / "missing-project.yaml",
    )
    assert cfg.run_soft_pct_of_capacity == 20.0
    assert cfg.run_hard_pct_of_capacity == 40.0
    assert cfg.window_warn_pct_remaining == 30.0
    assert cfg.window_block_pct_remaining == 10.0
    assert cfg.window_capacity_tokens == 88_000
    assert "Write to existing files" in cfg.irreversible_actions


def test_load_budget_config_project_overrides_user(tmp_path):
    user = tmp_path / "user.yaml"
    user.write_text("thresholds:\n  run_soft_pct_of_capacity: 3\n")
    project = tmp_path / "project.yaml"
    project.write_text("thresholds:\n  run_soft_pct_of_capacity: 7\n")
    cfg = load_budget_config(user_path=user, project_path=project)
    assert cfg.run_soft_pct_of_capacity == 7.0
    # User-level value passes through where Project does not override; this
    # falls through to the built-in default for the hard threshold.
    assert cfg.run_hard_pct_of_capacity == 40.0
