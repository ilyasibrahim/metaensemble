"""Tests for the configured axis commands that generalize the quality
gate beyond Python (`axis_commands` in quality.yaml).

The command runner is exercised with the running interpreter itself
(`sys.executable -c "import sys; sys.exit(N)"`) so no external toolchain
is required. Config tests cover the core <- user <- project merge, the
never-crash handling of unknown axis names, and the mixed-deliverable
driver behavior: a .py + .ts dispatch records both the Python runner's
axis and the command's `<axis>:cmd` twin.
"""
from __future__ import annotations

import shlex
import sys
from pathlib import Path

import yaml

from metaensemble.hooks.post_task import _quality_files_from_manifest
from metaensemble.lib.config import AxisCommand, load_quality_config
from metaensemble.lib.cost_gate import GateState
from metaensemble.lib.ids import uuid7
from metaensemble.lib.quality_runners import run_all_axes, run_configured_command


PY = shlex.quote(sys.executable)


def _exit_cmd(code: int) -> str:
    return f'{PY} -c "import sys; sys.exit({code})"'


# --- run_configured_command --------------------------------------------

def test_configured_command_auto_on_exit_zero(tmp_path):
    axis = run_configured_command(
        "correctness", AxisCommand(cmd=_exit_cmd(0)), tmp_path
    )
    assert axis.name == "correctness:cmd"
    assert axis.state == GateState.AUTO
    assert axis.raw == 0.0
    assert any("exited 0" in f for f in axis.findings)


def test_configured_command_nonzero_defaults_to_notify(tmp_path):
    axis = run_configured_command(
        "maintainability", AxisCommand(cmd=_exit_cmd(1)), tmp_path
    )
    assert axis.state == GateState.NOTIFY
    assert axis.raw == 1.0


def test_configured_command_nonzero_maps_to_block_when_configured(tmp_path):
    axis = run_configured_command(
        "correctness",
        AxisCommand(cmd=_exit_cmd(1), state_on_fail="block"),
        tmp_path,
    )
    assert axis.state == GateState.BLOCK


def test_configured_command_findings_carry_output_tail(tmp_path):
    script = "import sys; [print('line', i) for i in range(10)]; sys.exit(2)"
    cmd = f'{PY} -c "{script}"'
    axis = run_configured_command("security", AxisCommand(cmd=cmd), tmp_path)
    assert axis.state == GateState.NOTIFY
    # First finding names the command; the tail is capped at ~5 lines.
    assert "exited 2" in axis.findings[0]
    assert len(axis.findings) <= 6
    assert "line 9" in axis.findings[-1]


def test_configured_command_timeout_skips_axis(tmp_path):
    cmd = f'{PY} -c "import time; time.sleep(10)"'
    axis = run_configured_command(
        "coverage", AxisCommand(cmd=cmd, timeout=1), tmp_path
    )
    assert axis.state == GateState.AUTO
    assert any("timed out" in f for f in axis.findings)


def test_configured_command_absent_tool_skips_axis(tmp_path):
    axis = run_configured_command(
        "complexity",
        AxisCommand(cmd="definitely-not-a-real-tool-xyz --check ."),
        tmp_path,
    )
    assert axis.state == GateState.AUTO
    assert any("command not found" in f for f in axis.findings)


def test_configured_command_unparseable_cmd_skips_axis(tmp_path):
    axis = run_configured_command(
        "security", AxisCommand(cmd='echo "unbalanced'), tmp_path
    )
    assert axis.state == GateState.AUTO
    assert any("could not parse" in f for f in axis.findings)


# --- Config loading and merge ------------------------------------------

def test_load_quality_config_parses_axis_commands(tmp_path):
    project = tmp_path / "project.yaml"
    project.write_text(yaml.safe_dump({
        "axis_commands": {
            "correctness": {
                "cmd": "npm test --silent",
                "timeout": 180,
                "state_on_fail": "block",
            },
            "maintainability": {"cmd": "npx eslint ."},
        }
    }))
    cfg = load_quality_config(
        user_path=tmp_path / "no-user.yaml", project_path=project
    )
    assert cfg.axis_commands["correctness"] == AxisCommand(
        cmd="npm test --silent", timeout=180, state_on_fail="block"
    )
    # Defaults fill in: 120s timeout, notify on fail.
    assert cfg.axis_commands["maintainability"] == AxisCommand(
        cmd="npx eslint .", timeout=120, state_on_fail="notify"
    )
    assert cfg.ignored_axis_commands == ()


def test_load_quality_config_no_axis_commands_by_default(tmp_path):
    cfg = load_quality_config(
        user_path=tmp_path / "no-user.yaml",
        project_path=tmp_path / "no-project.yaml",
    )
    assert cfg.axis_commands == {}
    assert cfg.ignored_axis_commands == ()


def test_axis_commands_project_overrides_user(tmp_path):
    user = tmp_path / "user.yaml"
    user.write_text(yaml.safe_dump({
        "axis_commands": {
            "correctness": {"cmd": "npm test", "state_on_fail": "block"},
        }
    }))
    project = tmp_path / "project.yaml"
    project.write_text(yaml.safe_dump({
        "axis_commands": {
            "correctness": {"cmd": "yarn test"},
        }
    }))
    cfg = load_quality_config(user_path=user, project_path=project)
    # Project cmd wins; the user's state_on_fail survives the deep merge,
    # matching how per-axis threshold keys already layer.
    assert cfg.axis_commands["correctness"].cmd == "yarn test"
    assert cfg.axis_commands["correctness"].state_on_fail == "block"


def test_axis_commands_string_shorthand(tmp_path):
    project = tmp_path / "project.yaml"
    project.write_text(yaml.safe_dump({
        "axis_commands": {"security": "npm audit --audit-level=high"}
    }))
    cfg = load_quality_config(
        user_path=tmp_path / "no-user.yaml", project_path=project
    )
    assert cfg.axis_commands["security"] == AxisCommand(
        cmd="npm audit --audit-level=high"
    )


def test_axis_commands_unknown_axis_ignored_never_crashes(tmp_path):
    project = tmp_path / "project.yaml"
    project.write_text(yaml.safe_dump({
        "axis_commands": {
            "typoaxis": {"cmd": "npm test"},
            "correctness": {"cmd": "npm test"},
        }
    }))
    cfg = load_quality_config(
        user_path=tmp_path / "no-user.yaml", project_path=project
    )
    assert "typoaxis" not in cfg.axis_commands
    assert "correctness" in cfg.axis_commands
    assert any(
        name == "typoaxis" and "unknown axis" in reason
        for name, reason in cfg.ignored_axis_commands
    )


def test_axis_commands_invalid_values_degrade_to_defaults(tmp_path):
    project = tmp_path / "project.yaml"
    project.write_text(yaml.safe_dump({
        "axis_commands": {
            "correctness": {
                "cmd": "npm test",
                "state_on_fail": "explode",  # not notify|block
                "timeout": "soon",           # not an int
            },
            "security": {"timeout": 30},     # no cmd — unusable
        }
    }))
    cfg = load_quality_config(
        user_path=tmp_path / "no-user.yaml", project_path=project
    )
    assert cfg.axis_commands["correctness"].state_on_fail == "notify"
    assert cfg.axis_commands["correctness"].timeout == 120
    assert "security" not in cfg.axis_commands
    assert any(
        name == "security" and "no usable cmd" in reason
        for name, reason in cfg.ignored_axis_commands
    )


def test_axis_commands_non_mapping_block_ignored(tmp_path):
    project = tmp_path / "project.yaml"
    project.write_text("axis_commands: just-a-string\n")
    cfg = load_quality_config(
        user_path=tmp_path / "no-user.yaml", project_path=project
    )
    assert cfg.axis_commands == {}
    assert any("must be a mapping" in reason for _, reason in cfg.ignored_axis_commands)


# --- run_all_axes driver -------------------------------------------------

def _config_with_commands(tmp_path: Path, axis_commands: dict) -> object:
    """Build a QualityConfig with the Python axes disabled (so the driver
    tests never shell out to pytest/bandit/ruff/radon) and the given
    axis_commands block active."""
    project = tmp_path / "quality-project.yaml"
    doc: dict = {
        axis: {"enabled": False}
        for axis in ("correctness", "security", "maintainability", "complexity", "coverage")
    }
    doc["axis_commands"] = axis_commands
    project.write_text(yaml.safe_dump(doc))
    return load_quality_config(
        user_path=tmp_path / "quality-no-user.yaml", project_path=project
    )


def test_run_all_axes_mixed_py_and_ts_records_both_axes(tmp_path):
    py = tmp_path / "mod.py"
    py.write_text("def f():\n    return 1\n")
    ts = tmp_path / "mod.ts"
    ts.write_text("export const f = () => 1;\n")
    config = _config_with_commands(
        tmp_path, {"correctness": {"cmd": _exit_cmd(0)}}
    )
    axes = run_all_axes([py, ts], config, tmp_path)
    names = [a.name for a in axes]
    assert "correctness" in names
    assert "correctness:cmd" in names
    cmd_axis = next(a for a in axes if a.name == "correctness:cmd")
    assert cmd_axis.state == GateState.AUTO


def test_run_all_axes_py_only_does_not_run_commands(tmp_path):
    py = tmp_path / "mod.py"
    py.write_text("def f():\n    return 1\n")
    # The command would BLOCK if it ran; a pure-Python deliverable set
    # must not trigger it.
    config = _config_with_commands(
        tmp_path, {"correctness": {"cmd": _exit_cmd(1), "state_on_fail": "block"}}
    )
    axes = run_all_axes([py], config, tmp_path)
    assert [a.name for a in axes] == [
        "correctness", "security", "maintainability", "complexity", "coverage",
    ]
    assert all(a.state == GateState.AUTO for a in axes)


def test_run_all_axes_no_py_no_commands_is_all_skips(tmp_path):
    md = tmp_path / "notes.md"
    md.write_text("# notes\n")
    config = _config_with_commands(tmp_path, {})
    axes = run_all_axes([md], config, tmp_path)
    assert len(axes) == 5
    assert all(a.state == GateState.AUTO for a in axes)


def test_run_all_axes_command_failure_escalates_worst_of(tmp_path):
    ts = tmp_path / "mod.ts"
    ts.write_text("export const f = () => 1;\n")
    config = _config_with_commands(
        tmp_path,
        {
            "correctness": {"cmd": _exit_cmd(1), "state_on_fail": "block"},
            "maintainability": {"cmd": _exit_cmd(1)},
        },
    )
    axes = run_all_axes([ts], config, tmp_path)
    by_name = {a.name: a for a in axes}
    assert by_name["correctness:cmd"].state == GateState.BLOCK
    assert by_name["maintainability:cmd"].state == GateState.NOTIFY


def test_run_all_axes_surfaces_ignored_axis_command_notes(tmp_path):
    ts = tmp_path / "mod.ts"
    ts.write_text("export const f = () => 1;\n")
    config = _config_with_commands(tmp_path, {"typoaxis": {"cmd": _exit_cmd(0)}})
    axes = run_all_axes([ts], config, tmp_path)
    note = next(a for a in axes if a.name == "typoaxis:cmd")
    assert note.state == GateState.AUTO
    assert any("unknown axis" in f for f in note.findings)


# --- Hook wiring: manifest deliverables reach the gate unfiltered -------

def test_quality_files_from_manifest_includes_non_python(tmp_path):
    manifest = {
        "manifest_id": f"hm-{uuid7()}",
        "version": 1,
        "task": "implement-widget",
        "context": {"files": [{"path": "src/widget.ts", "lines": "1-100"}]},
        "expected_deliverables": [
            {"path": "src/widget.ts"},
            {"path": "src/glue.py"},
            {"path": "docs/widget.md"},
        ],
        "constraints": {"model_tier": "sonnet", "window_budget": 8000},
    }
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(manifest))
    files = _quality_files_from_manifest(str(path))
    assert [f.as_posix() for f in files] == [
        "src/widget.ts", "src/glue.py", "docs/widget.md",
    ]
