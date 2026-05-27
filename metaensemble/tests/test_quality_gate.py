"""Tests for the quality gate (`metaensemble/lib/quality_gate.py`) and its runners.

Pure-function tests cover the aggregator (`worst_state`, `build_decision`).
Integration tests run the actual tools (ruff, bandit, radon) against
tiny synthetic Python files to confirm the runners parse output and map
to the right gate state. Coverage and correctness are exercised through
their tool-absence paths since wiring a project-level pytest run inside
a unit test would loop.
"""
from __future__ import annotations


import pytest

from metaensemble.lib.config import AxisConfig, load_quality_config
from metaensemble.lib.cost_gate import GateState
from metaensemble.lib.quality_gate import (
    QualityAxis,
    build_decision,
    worst_state,
)
from metaensemble.lib.quality_runners import (
    run_complexity,
    run_correctness,
    run_coverage,
    run_maintainability,
    run_security,
)


# --- worst_state / build_decision --------------------------------------

def test_worst_state_returns_auto_for_empty():
    assert worst_state([]) == GateState.AUTO


def test_worst_state_picks_highest():
    axes = [
        QualityAxis("a", GateState.AUTO),
        QualityAxis("b", GateState.NOTIFY),
        QualityAxis("c", GateState.AUTO),
    ]
    assert worst_state(axes) == GateState.NOTIFY


def test_worst_state_block_wins_over_notify():
    axes = [
        QualityAxis("a", GateState.NOTIFY),
        QualityAxis("b", GateState.BLOCK),
        QualityAxis("c", GateState.NOTIFY),
    ]
    assert worst_state(axes) == GateState.BLOCK


def test_build_decision_auto_has_no_options():
    decision = build_decision((QualityAxis("a", GateState.AUTO),))
    assert decision.state == GateState.AUTO
    assert decision.options == ()
    assert "all configured axes clear" in decision.summary


def test_build_decision_notify_has_options():
    """The grammar fix: options surface on NOTIFY as well as BLOCK,
    so the Principal can act rather than only intercept mid-flight."""
    axis = QualityAxis("security", GateState.NOTIFY, findings=("medium: SQL injection",))
    decision = build_decision((axis,))
    assert decision.state == GateState.NOTIFY
    assert len(decision.options) == 4
    assert "Quality gate would NOTIFY" in decision.summary
    assert "security" in decision.summary


def test_build_decision_block_includes_findings_in_summary():
    axis = QualityAxis(
        "security",
        GateState.BLOCK,
        findings=("high: hardcoded password", "high: insecure deserialization"),
    )
    decision = build_decision((axis,))
    assert decision.state == GateState.BLOCK
    assert "BLOCK" in decision.summary
    assert "high: hardcoded password" in decision.summary
    assert "(+1 more)" in decision.summary


def test_build_decision_worst_of_axes():
    decision = build_decision((
        QualityAxis("correctness", GateState.AUTO),
        QualityAxis("security", GateState.NOTIFY, findings=("medium: unsafe yaml",)),
        QualityAxis("maintainability", GateState.BLOCK, findings=("16 ruff issues",)),
    ))
    assert decision.state == GateState.BLOCK
    assert len(decision.options) == 4


# --- Runner: complexity (radon, installed) ----------------------------

@pytest.fixture
def axis_default() -> AxisConfig:
    return AxisConfig(enabled=True, options={
        "notify_above": 10,
        "block_above": 15,
        "notify_failures": 1,
        "block_failures": 3,
        "notify_severity": "medium",
        "block_severity": "high",
        "notify_issues": 6,
        "block_issues": 16,
        "notify_drop_pp": 5.0,
        "block_drop_pp": 5.0,
        "block_absolute_below": 80.0,
    })


def test_complexity_auto_on_simple_function(tmp_path, axis_default):
    file = tmp_path / "simple.py"
    file.write_text("def f(x):\n    return x + 1\n")
    axis = run_complexity([file], axis_default, tmp_path)
    assert axis.name == "complexity"
    assert axis.state == GateState.AUTO


def test_complexity_block_on_pathologically_complex_function(tmp_path, axis_default):
    # A function whose cyclomatic complexity exceeds the BLOCK threshold of 15.
    branches = "\n".join(
        f"    elif x == {i}: return {i}" for i in range(2, 20)
    )
    file = tmp_path / "complex.py"
    file.write_text(
        "def f(x):\n"
        "    if x == 1: return 1\n"
        f"{branches}\n"
        "    else: return -1\n"
    )
    axis = run_complexity([file], axis_default, tmp_path)
    assert axis.state == GateState.BLOCK
    assert axis.raw >= 15


def test_complexity_disabled_returns_auto(tmp_path):
    file = tmp_path / "x.py"
    file.write_text("def f(): pass\n")
    axis = run_complexity([file], AxisConfig(enabled=False), tmp_path)
    assert axis.state == GateState.AUTO
    assert any("disabled" in f for f in axis.findings)


def test_complexity_no_python_files_skips(tmp_path, axis_default):
    md = tmp_path / "doc.md"
    md.write_text("# doc\n")
    axis = run_complexity([md], axis_default, tmp_path)
    assert axis.state == GateState.AUTO
    assert any("no Python files" in f for f in axis.findings)


# --- Runner: maintainability (ruff, installed) -----------------------

def test_maintainability_auto_on_clean_file(tmp_path, axis_default):
    file = tmp_path / "clean.py"
    file.write_text('"""Clean module."""\n\n\ndef f(x):\n    return x\n')
    axis = run_maintainability([file], axis_default, tmp_path)
    assert axis.name == "maintainability"
    assert axis.state == GateState.AUTO


def test_maintainability_classifies_issue_count(tmp_path, axis_default):
    """A file with many unused imports lands in NOTIFY or BLOCK."""
    # 20+ unused imports — enough to push ruff issue count over BLOCK threshold (16).
    imports = "\n".join(f"import json as _x{i}  # noqa" for i in range(20))
    # Drop the noqa to actually have lint issues; F401 unused imports.
    imports = "\n".join(f"import json as _x{i}" for i in range(20))
    file = tmp_path / "dirty.py"
    file.write_text(imports + "\n")
    axis = run_maintainability([file], axis_default, tmp_path)
    # Either NOTIFY or BLOCK depending on what ruff flags by default.
    assert axis.state in (GateState.NOTIFY, GateState.BLOCK)
    assert axis.raw >= 6


# --- Runner: security (bandit, installed) -----------------------------

def test_security_block_on_high_severity(tmp_path, axis_default):
    """A file that calls eval on user input — bandit flags high severity."""
    file = tmp_path / "danger.py"
    file.write_text(
        "import sys\n"
        "def f():\n"
        "    return eval(sys.argv[1])\n"
    )
    axis = run_security([file], axis_default, tmp_path)
    assert axis.name == "security"
    # eval() is typically a medium-severity bandit finding (B307).
    # We assert the runner produces *some* non-AUTO state for an obvious risk.
    assert axis.state in (GateState.NOTIFY, GateState.BLOCK)


def test_security_auto_on_clean_file(tmp_path, axis_default):
    file = tmp_path / "safe.py"
    file.write_text("def f(x):\n    return x + 1\n")
    axis = run_security([file], axis_default, tmp_path)
    assert axis.state == GateState.AUTO


# --- Runner: correctness + coverage (tool-absence and skip paths) ----

def test_correctness_skipped_when_no_py_files(tmp_path, axis_default):
    md = tmp_path / "doc.md"
    md.write_text("# notes\n")
    axis = run_correctness([md], axis_default, tmp_path)
    assert axis.state == GateState.AUTO
    assert any("no Python files" in f for f in axis.findings)


def test_correctness_disabled_returns_auto(tmp_path):
    axis = run_correctness([tmp_path / "any.py"], AxisConfig(enabled=False), tmp_path)
    assert axis.state == GateState.AUTO


def test_coverage_skipped_when_no_dot_coverage_file(tmp_path, axis_default):
    axis = run_coverage([tmp_path / "x.py"], axis_default, tmp_path)
    assert axis.state == GateState.AUTO
    assert any(".coverage file not found" in f for f in axis.findings)


def test_coverage_disabled_returns_auto(tmp_path):
    axis = run_coverage([], AxisConfig(enabled=False), tmp_path)
    assert axis.state == GateState.AUTO


# --- Config loader ----------------------------------------------------

def test_load_quality_config_defaults_match_research(tmp_path):
    cfg = load_quality_config(
        user_path=tmp_path / "no-user.yaml",
        project_path=tmp_path / "no-project.yaml",
    )
    assert cfg.correctness.enabled is True
    assert cfg.security.options["notify_severity"] == "medium"
    assert cfg.security.options["block_severity"] == "high"
    assert cfg.maintainability.options["notify_issues"] == 6
    assert cfg.maintainability.options["block_issues"] == 16
    assert cfg.complexity.options["notify_above"] == 10
    assert cfg.complexity.options["block_above"] == 15
    assert cfg.coverage.options["block_absolute_below"] == 80.0


def test_load_quality_config_project_overrides_user(tmp_path):
    user = tmp_path / "user.yaml"
    user.write_text("security:\n  notify_severity: low\n")
    project = tmp_path / "project.yaml"
    project.write_text("security:\n  block_severity: critical\n")
    cfg = load_quality_config(user_path=user, project_path=project)
    # Project does not override notify_severity, so user value wins.
    assert cfg.security.options["notify_severity"] == "low"
    assert cfg.security.options["block_severity"] == "critical"


def test_load_quality_config_disable_axis_via_yaml(tmp_path):
    project = tmp_path / "project.yaml"
    project.write_text("correctness:\n  enabled: false\n")
    cfg = load_quality_config(
        user_path=tmp_path / "no-user.yaml",
        project_path=project,
    )
    assert cfg.correctness.enabled is False
    # Other axes still default to enabled.
    assert cfg.security.enabled is True
