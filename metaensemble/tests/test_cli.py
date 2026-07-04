"""Functional tests for the `metaensemble` CLI entry point."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest



REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_package_version_matches_pyproject():
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:
        import tomli as tomllib  # Python 3.10: tomli backport from [test] extras
    import metaensemble

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert metaensemble.__version__ == pyproject["project"]["version"]


def _invoke_cli(
    cwd: Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, "-m", "metaensemble.cli", *args],
        capture_output=True, text=True, env=env, cwd=str(cwd),
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_init_creates_state_directory(tmp_path):
    code, out, err = _invoke_cli(tmp_path, "init")
    assert code == 0, err
    project_dir = tmp_path / ".metaensemble"
    assert project_dir.is_dir()
    assert (project_dir / "state" / "department.db").exists()
    assert (project_dir / "state" / "runs.jsonl").parent.exists()
    assert (project_dir / "manifests").is_dir()
    assert (project_dir / "briefs").is_dir()
    assert (project_dir / "budgets.yaml").exists()


def test_init_refuses_to_clobber_existing(tmp_path):
    _invoke_cli(tmp_path, "init")
    code, _, err = _invoke_cli(tmp_path, "init")
    assert code != 0
    assert "already exists" in err


def test_init_force_reinitializes(tmp_path):
    _invoke_cli(tmp_path, "init")
    code, _, _ = _invoke_cli(tmp_path, "init", "--force")
    assert code == 0


def test_init_with_pack_flag_explains_deferral(tmp_path):
    """--pack is accepted but explains the deferral. The old assertion
    checked for a version string that the init notice also contained,
    so it kept passing after the pack message stopped naming a version."""
    code, out, _ = _invoke_cli(tmp_path, "init", "--pack", "ml")
    assert code == 0
    assert "reserved for a future release" in out


def test_limits_subcommand(tmp_path):
    _invoke_cli(tmp_path, "init")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["METAENSEMBLE_STATE_DIR"] = str(tmp_path / ".metaensemble" / "state")
    proc = subprocess.run(
        [sys.executable, "-m", "metaensemble.cli", "limits"],
        capture_output=True, text=True, env=env, cwd=str(tmp_path),
    )
    assert proc.returncode == 0
    assert "Window" in proc.stdout


def test_inspect_points_at_adopt(tmp_path):
    """After inspecting, the CLI must point Principals at the new
    `adopt` / `setup` commands, not the removed `install`."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()

    code, out, err = _invoke_cli(project, "inspect", extra_env={"HOME": str(home)})

    assert code == 0, err
    assert "metaensemble install" not in out
    assert "metaensemble adopt" in out or "metaensemble setup" in out


@pytest.mark.parametrize("retired", ["window", "survey"])
def test_retired_cli_commands_fail(tmp_path, retired):
    code, _out, err = _invoke_cli(tmp_path, retired)
    assert code != 0
    assert "invalid choice" in err


def test_user_setup_help_uses_layout_vocabulary(tmp_path):
    code, out, err = _invoke_cli(tmp_path, "user-setup", "--help")
    assert code == 0, err
    assert "--layout" in out
    assert "--mode" not in out
    assert "parallel" not in out
    assert "incorporate" not in out


# --- Manifest id generation and scaffold --------------------------------


def test_manifest_new_id_prints_valid_uuidv7(tmp_path):
    """`metaensemble manifest new-id` must print one line matching the
    `hm-<UUIDv7>` pattern the schema requires for `manifest_id`."""
    import re
    code, out, err = _invoke_cli(tmp_path, "manifest", "new-id")
    assert code == 0, err
    line = out.strip()
    pattern = (
        r"^hm-[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    assert re.match(pattern, line), f"output does not match hm-<UUIDv7>: {line!r}"


def test_manifest_scaffold_writes_valid_yaml(tmp_path):
    """`manifest scaffold <task> -o <path>` must write a parseable YAML
    file with every required top-level Manifest field present."""
    import yaml
    target = tmp_path / "draft.yaml"
    code, out, err = _invoke_cli(
        tmp_path, "manifest", "scaffold", "implement-auth", "-o", str(target),
    )
    assert code == 0, err
    assert target.exists()
    data = yaml.safe_load(target.read_text())
    for key in (
        "manifest_id", "version", "task",
        "context", "expected_deliverables", "constraints",
    ):
        assert key in data, f"scaffold missing required field {key!r}"
    assert data["task"] == "implement-auth"
    assert data["version"] == 1
    assert data["manifest_id"].startswith("hm-")


def test_manifest_scaffold_output_fails_validation_until_todos_filled(tmp_path):
    """The scaffold ships with TODO markers in every required-but-author-
    supplied field. As written it must FAIL validation (forcing the
    author to fill the TODOs); after the author replaces the markers
    with valid values, the same file must PASS validation."""
    import yaml
    from metaensemble.lib.manifest import validate_manifest
    from jsonschema.exceptions import ValidationError

    target = tmp_path / "draft.yaml"
    code, _, err = _invoke_cli(
        tmp_path, "manifest", "scaffold", "ship-feature", "-o", str(target),
    )
    assert code == 0, err
    data = yaml.safe_load(target.read_text())
    with pytest.raises(ValidationError):
        validate_manifest(data)

    # Fill the TODOs with valid content.
    data["context"]["files"] = [{"path": "src/feature.py"}]
    data["expected_deliverables"] = [{"path": "src/feature.py"}]
    data["constraints"] = {"model_tier": "sonnet", "window_budget": 4000}
    data["acceptance"] = ["tests pass", "no regressions"]
    validate_manifest(data)


def test_manifest_scaffold_handles_task_with_yaml_metacharacters(tmp_path):
    """Regression: a task containing a colon (or any YAML metacharacter)
    must be serialized as a quoted scalar. Raw interpolation `task: {task}`
    would emit `task: ship: feature`, which PyYAML rejects with
    `mapping values are not allowed here`. The output must round-trip
    through yaml.safe_load with the task string preserved verbatim."""
    import yaml
    target = tmp_path / "draft.yaml"
    weird = "ship: feature # with hash & quote 'x'"
    code, _, err = _invoke_cli(
        tmp_path, "manifest", "scaffold", weird, "-o", str(target),
    )
    assert code == 0, err
    data = yaml.safe_load(target.read_text())  # must not raise
    assert data["task"] == weird, (
        f"task round-trip lost data: expected {weird!r}, got {data['task']!r}"
    )


def test_manifest_scaffold_creates_missing_parent_dirs(tmp_path):
    """Regression: `-o` into a path whose parent does not exist must
    create the parent chain (SKILL.md advertises
    `.metaensemble/manifests/...` as the canonical location, which the
    author may not have created yet)."""
    import yaml
    nested = tmp_path / "does" / "not" / "yet" / "exist" / "draft.yaml"
    assert not nested.parent.exists()
    code, _, err = _invoke_cli(
        tmp_path, "manifest", "scaffold", "ship-feature", "-o", str(nested),
    )
    assert code == 0, err
    assert nested.exists(), "scaffold did not create the output file"
    data = yaml.safe_load(nested.read_text())
    assert data["task"] == "ship-feature"


def test_eval_replay_writes_report_under_caller_cwd(tmp_path):
    """Wheel installs must not write eval reports into site-packages.

    Package data is read from the installed `evals/` package, but generated
    reports belong to the Principal's current working directory.
    """
    code, out, err = _invoke_cli(
        tmp_path,
        "eval",
        "--tier",
        "replay",
        "--cells",
        "B4_best_prompt",
        "--seeds",
        "1",
    )
    assert code == 0, err
    prefix = "Eval report written to "
    report_lines = [line for line in out.splitlines() if line.startswith(prefix)]
    assert report_lines, out
    report_path = Path(report_lines[-1][len(prefix):])
    assert report_path.parent == tmp_path / "evals" / "reports"
    assert report_path.exists()
    assert "Evaluation report (replay)" in report_path.read_text()
