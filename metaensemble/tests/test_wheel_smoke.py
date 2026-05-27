"""Wheel-build smoke test.

Builds the wheel via `python -m build` and verifies:

- Both the `metaensemble` and `evals` packages ship.
- All asset directories are present (commands, skills, output-styles, roles,
  schemas, state/migrations, config, statusline for the metaensemble package;
  cassettes (with README.md and nested JSON/JSONL), datasets, configs for
  evals).
- A clean-venv `pip install <wheel>` gives a working `metaensemble --help`
  and importable `evals.runners.api`.
- Data lookups for `evals/cassettes/` go through `importlib.resources` —
  never `from evals.cassettes import *` (cassettes/ is a data directory,
  not a Python package).

This keeps wheel packaging honest across both importable code and data assets.
"""
from __future__ import annotations

import subprocess
import shutil
import sys
import venv
import zipfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _find_wheel(dist_dir: Path) -> Path:
    """Pick the newest wheel under `dist/`."""
    wheels = sorted(dist_dir.glob("metaensemble-*.whl"))
    if not wheels:
        pytest.skip(
            f"no wheel in {dist_dir}; run `python -m build --wheel` from "
            f"{REPO_ROOT} first or let this test build one."
        )
    return wheels[-1]


def _build_wheel_in(tmp_path: Path) -> Path:
    """Build a fresh wheel into a tmp dist dir using `python -m build`."""
    out = tmp_path / "dist"
    out.mkdir()
    shutil.rmtree(REPO_ROOT / "build", ignore_errors=True)
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(out), str(REPO_ROOT)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        f"python -m build failed (rc={proc.returncode})\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return _find_wheel(out)


# Every directory we expect to see in the wheel. Buckets matter, not exact
# file counts (the file count changes as we add commands/roles).
REQUIRED_BUCKETS = (
    "metaensemble/commands",
    "metaensemble/skills/metaensemble-protocol",
    "metaensemble/output-styles",
    "metaensemble/roles",
    "metaensemble/schemas",
    "metaensemble/state/migrations",
    "metaensemble/config",
    "metaensemble/statusline",
    "metaensemble/lib",
    "metaensemble/hooks",
    "metaensemble/tools",
    "evals/cassettes",
    "evals/datasets",
    "evals/configs",
    "evals/runners",
)


# Specific reviewer-flagged files that v3.2 sign-off (§11) calls out.
REQUIRED_FILES = (
    "metaensemble/commands/dispatch.md",
    "metaensemble/skills/metaensemble-protocol/SKILL.md",
    "metaensemble/output-styles/wire.md",
    "metaensemble/output-styles/deliverable.md",
    "metaensemble/roles/architect.md",
    "metaensemble/schemas/manifest.schema.json",
    "metaensemble/schemas/brief.schema.json",
    "metaensemble/state/migrations/001_init.sql",
    "metaensemble/statusline/me_status.py",
    # bin/me-run.template is not shipped; the runner is generated at install time.
    "evals/cassettes/README.md",
    "evals/cassettes/bootstrap.jsonl",
)


def test_wheel_builds_with_all_asset_buckets(tmp_path):
    """`python -m build --wheel` produces a wheel containing every asset
    directory the runtime relies on."""
    wheel = _build_wheel_in(tmp_path)
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
    missing = [b for b in REQUIRED_BUCKETS if not any(n.startswith(b + "/") for n in names)]
    assert not missing, f"wheel missing required buckets: {missing}"


def test_wheel_contains_required_files(tmp_path):
    """Required runtime files ship."""
    wheel = _build_wheel_in(tmp_path)
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
    missing = [f for f in REQUIRED_FILES if f not in names]
    assert not missing, f"wheel missing required files: {missing}"


def test_wheel_does_not_ship_tests(tmp_path):
    """Runtime wheels must not include the source test suite or fixtures."""
    wheel = _build_wheel_in(tmp_path)
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
    leaked = [n for n in names if n.startswith("metaensemble/tests/")]
    assert not leaked, f"wheel should not ship tests/fixtures: {leaked[:10]}"


def test_wheel_includes_console_script_entry_point(tmp_path):
    """The `metaensemble` console script entry point survives packaging."""
    wheel = _build_wheel_in(tmp_path)
    with zipfile.ZipFile(wheel) as zf:
        eps = next((n for n in zf.namelist() if n.endswith("entry_points.txt")), None)
        assert eps is not None, "wheel has no entry_points.txt"
        text = zf.read(eps).decode("utf-8")
    assert "metaensemble" in text and "metaensemble.cli:main" in text, (
        f"entry_points.txt missing the metaensemble console script:\n{text}"
    )


@pytest.mark.slow
def test_wheel_installs_into_clean_venv_and_cli_runs(tmp_path):
    """Build the wheel, install into a fresh venv, verify CLI + evals access.

    This is the slowest test (creates a venv, installs the wheel). Marked
    `slow` so CI can run it explicitly. Local devs running `pytest -q`
    skip it via `-m "not slow"` if their default config does that.

    Per v3.2 #1: evals/cassettes/ is NOT a Python package — use
    `importlib.resources` for data lookup, never `from evals.cassettes
    import *` (which would fail and mask real packaging bugs).
    """
    wheel = _build_wheel_in(tmp_path)

    # Build a clean venv.
    venv_dir = tmp_path / "test-venv"
    venv.create(venv_dir, with_pip=True, clear=True)
    venv_python = venv_dir / "bin" / "python"
    assert venv_python.exists(), "venv python not created"

    # Install the wheel.
    proc = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--quiet", str(wheel)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        f"pip install failed:\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )

    venv_metaensemble = venv_dir / "bin" / "metaensemble"
    assert venv_metaensemble.exists(), "console script not created"

    # 1. metaensemble console script works from the installed wheel.
    proc = subprocess.run(
        [str(venv_metaensemble), "--help"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"CLI --help failed: {proc.stderr}"
    assert "metaensemble" in proc.stdout.lower()
    assert "feedback-first" in proc.stdout

    # 2. evals.runners.api importable (it IS a real Python module).
    proc = subprocess.run(
        [str(venv_python), "-c", "import evals.runners.api"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"evals.runners.api import failed: {proc.stderr}"

    # 3. Data lookups via importlib.resources — never `import evals.cassettes`.
    #    cassettes/ is a data directory (no __init__.py), so `import` would fail.
    probe = (
        "import importlib.resources as r; "
        "cassettes = r.files('evals').joinpath('cassettes'); "
        "assert cassettes.joinpath('README.md').is_file(), 'cassettes/README.md missing'; "
        "assert any(p.name.endswith('.jsonl') for p in cassettes.iterdir()), 'no jsonl cassette'; "
        "datasets = r.files('evals').joinpath('datasets'); "
        "assert any(datasets.iterdir()), 'datasets/ empty'"
    )
    proc = subprocess.run(
        [str(venv_python), "-c", probe],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        f"importlib.resources probe failed: {proc.stderr}"
    )

    # 4. Artifact/source parity for the stale-console-script bug class:
    #    generated eval reports must land under the caller cwd, never
    #    under site-packages where package data lives.
    eval_cwd = tmp_path / "eval-cwd"
    eval_cwd.mkdir()
    proc = subprocess.run(
        [
            str(venv_metaensemble), "eval", "--tier", "replay",
            "--cells", "B4_best_prompt", "--seeds", "1",
        ],
        capture_output=True, text=True, cwd=str(eval_cwd),
    )
    assert proc.returncode == 0, (
        f"wheel console eval failed:\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    prefix = "Eval report written to "
    lines = [line for line in proc.stdout.splitlines() if line.startswith(prefix)]
    assert lines, proc.stdout
    report_path = Path(lines[-1][len(prefix):])
    assert report_path.parent == eval_cwd / "evals" / "reports"
    assert report_path.exists()
