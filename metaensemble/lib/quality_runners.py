"""Tool wrappers for the five quality-gate axes.

Each runner takes the list of Python files to inspect and the axis's
config slice. It returns a `QualityAxis` (state + findings + raw score)
or `None` when the underlying tool is not installed — the gate skips
absent axes rather than failing closed, so a user with a minimal install
still gets the cost gate without being forced to install bandit, radon,
and pip-audit just to dispatch a single Run.

Each runner targets fast static analysis on the changed files only.
The exception is correctness (pytest), which runs the full suite by
default when Python deliverables are in scope. Projects with especially
expensive suites can disable it with `correctness.enabled: false` in
quality.yaml.

Wherever possible the runner shells out to the tool's JSON output mode,
parses the structured result, and maps it to `GateState` against the
thresholds in `AxisConfig`. Heuristic counts are used where JSON output
is impractical.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path

from metaensemble.lib.config import AxisConfig
from metaensemble.lib.cost_gate import GateState
from metaensemble.lib.quality_gate import QualityAxis


# --- Helpers ------------------------------------------------------------

def _tool_available(name: str) -> str | None:
    """Return the full path to the tool, or None if not installed.

    Looks in three places, in order: (1) the bin directory of the
    currently-running Python interpreter (which is the project's venv
    when the hook fires under `.venv/bin/python`), (2) the system PATH,
    (3) absent. This makes the runner robust to the common case where
    the user invokes MetaEnsemble through the resilient launcher and
    the activated PATH does not yet contain `.venv/bin/`.
    """
    venv_candidate = Path(sys.executable).parent / name
    if venv_candidate.exists() and os.access(venv_candidate, os.X_OK):
        return str(venv_candidate)
    return shutil.which(name)


def _python_files(files: list[Path]) -> list[Path]:
    """Filter the changed-file list down to .py files that exist."""
    return [f for f in files if f.exists() and f.suffix == ".py"]


def _run(cmd: list[str], cwd: Path, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a subprocess and capture text output. Never raises on non-zero exit;
    the caller inspects returncode and parses stdout/stderr."""
    return subprocess.run(  # nosec B603
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _skipped(name: str, reason: str) -> QualityAxis:
    """Build an AUTO-state axis recording why a runner did not execute.

    Skipped axes do not influence the gate's worst-of-axes calculation,
    so the gate still completes on partial toolchain installs.
    """
    return QualityAxis(name=name, state=GateState.AUTO, findings=(reason,), raw=None)


# --- Axis 1: Correctness (pytest) -------------------------------------

def run_correctness(
    files: list[Path], config: AxisConfig, project_root: Path
) -> QualityAxis:
    """Run the project's pytest suite and classify by failure count.

    Enabled by default because correctness is one of the five quality
    axes. Projects with expensive suites can opt out with
    `correctness.enabled: false` in `quality.yaml`.
    """
    if not config.enabled:
        return _skipped("correctness", "correctness axis disabled in quality.yaml")
    if not _python_files(files):
        return _skipped("correctness", "no Python files in scope")

    pytest = _tool_available("pytest") or _tool_available("py.test")
    if pytest is None:
        return _skipped("correctness", "pytest not installed")

    # Run with quiet mode and no traceback. Timeout is generous since
    # full suites can be slow on cold caches.
    result = _run([pytest, "--tb=no", "-q", "--no-header"], cwd=project_root, timeout=120)
    # The summary line typically reads "X failed, Y passed in Z.Ss".
    failures = 0
    for line in result.stdout.splitlines()[::-1]:
        if " failed" in line and " passed" in line:
            try:
                failures = int(line.strip().split()[0])
            except ValueError:
                pass
            break

    notify_at = int(config.get("notify_failures", 1))
    block_at = int(config.get("block_failures", 3))
    if failures >= block_at:
        state = GateState.BLOCK
    elif failures >= notify_at:
        state = GateState.NOTIFY
    else:
        state = GateState.AUTO

    findings = (
        f"{failures} test failure(s)" if failures else "all tests pass",
    )
    return QualityAxis(name="correctness", state=state, findings=findings, raw=float(failures))


# --- Axis 2: Security (bandit + pip-audit) ----------------------------

_BANDIT_SEVERITY = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
_SEVERITY_TO_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def run_security(
    files: list[Path], config: AxisConfig, project_root: Path
) -> QualityAxis:
    """Run bandit on changed Python files. Classify by highest severity."""
    if not config.enabled:
        return _skipped("security", "security axis disabled in quality.yaml")
    bandit = _tool_available("bandit")
    if bandit is None:
        return _skipped("security", "bandit not installed")
    py_files = _python_files(files)
    if not py_files:
        return _skipped("security", "no Python files in scope")

    cmd = [bandit, "-q", "-f", "json", *[str(p) for p in py_files]]
    result = _run(cmd, cwd=project_root, timeout=60)
    if not result.stdout.strip():
        return _skipped("security", "bandit produced no output")

    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        return _skipped("security", "bandit output not parseable")

    results = report.get("results", [])
    highest_rank = -1
    findings_list: list[str] = []
    for item in results:
        sev = (item.get("issue_severity") or "").upper()
        rank = _BANDIT_SEVERITY.get(sev, -1)
        highest_rank = max(highest_rank, rank)
        if len(findings_list) < 3:
            findings_list.append(
                f"{sev.lower()}: {item.get('test_name','?')} "
                f"({Path(item.get('filename','?')).name}:{item.get('line_number','?')})"
            )

    notify_rank = _SEVERITY_TO_RANK.get(str(config.get("notify_severity", "medium")), 1)
    block_rank = _SEVERITY_TO_RANK.get(str(config.get("block_severity", "high")), 2)

    if highest_rank >= block_rank:
        state = GateState.BLOCK
    elif highest_rank >= notify_rank:
        state = GateState.NOTIFY
    else:
        state = GateState.AUTO

    if not findings_list:
        findings_list = [f"{len(results)} bandit finding(s) below NOTIFY threshold"]
    return QualityAxis(
        name="security",
        state=state,
        findings=tuple(findings_list),
        raw=float(len(results)),
    )


# --- Axis 3: Maintainability (ruff) ------------------------------------

def run_maintainability(
    files: list[Path], config: AxisConfig, project_root: Path
) -> QualityAxis:
    """Run ruff on changed Python files. Classify by issue count.

    Mapped to SonarQube-style A/B/C/D/E grades using the issue-count
    thresholds in `quality.yaml`. The grade is informational; the
    state is what the gate aggregator consumes.
    """
    if not config.enabled:
        return _skipped("maintainability", "maintainability axis disabled in quality.yaml")
    ruff = _tool_available("ruff")
    if ruff is None:
        return _skipped("maintainability", "ruff not installed")
    py_files = _python_files(files)
    if not py_files:
        return _skipped("maintainability", "no Python files in scope")

    cmd = [ruff, "check", "--output-format=json", *[str(p) for p in py_files]]
    result = _run(cmd, cwd=project_root, timeout=30)
    try:
        issues = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError:
        return _skipped("maintainability", "ruff output not parseable")

    count = len(issues)
    notify_at = int(config.get("notify_issues", 6))
    block_at = int(config.get("block_issues", 16))
    if count >= block_at:
        state = GateState.BLOCK
    elif count >= notify_at:
        state = GateState.NOTIFY
    else:
        state = GateState.AUTO

    findings_list: list[str] = []
    for it in issues[:3]:
        loc = it.get("location") or {}
        findings_list.append(
            f"{it.get('code','?')}: {it.get('message','?')} "
            f"({Path(it.get('filename','?')).name}:{loc.get('row','?')})"
        )
    if not findings_list:
        findings_list = ["no linter issues on changed files"]

    return QualityAxis(
        name="maintainability",
        state=state,
        findings=tuple(findings_list),
        raw=float(count),
    )


# --- Axis 4: Complexity (radon) ---------------------------------------

def run_complexity(
    files: list[Path], config: AxisConfig, project_root: Path
) -> QualityAxis:
    """Run radon's cyclomatic-complexity check on changed Python files."""
    if not config.enabled:
        return _skipped("complexity", "complexity axis disabled in quality.yaml")
    radon = _tool_available("radon")
    if radon is None:
        return _skipped("complexity", "radon not installed")
    py_files = _python_files(files)
    if not py_files:
        return _skipped("complexity", "no Python files in scope")

    cmd = [radon, "cc", "-j", "-nc", *[str(p) for p in py_files]]
    result = _run(cmd, cwd=project_root, timeout=30)
    try:
        report = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError:
        return _skipped("complexity", "radon output not parseable")

    notify_above = int(config.get("notify_above", 10))
    block_above = int(config.get("block_above", 15))
    max_complexity = 0
    findings_list: list[str] = []
    for path, entries in report.items():
        for entry in entries:
            complexity = int(entry.get("complexity", 0))
            if complexity > max_complexity:
                max_complexity = complexity
            if complexity > notify_above and len(findings_list) < 3:
                findings_list.append(
                    f"{entry.get('name','?')} cc={complexity} "
                    f"({Path(path).name}:{entry.get('lineno','?')})"
                )

    if max_complexity > block_above:
        state = GateState.BLOCK
    elif max_complexity > notify_above:
        state = GateState.NOTIFY
    else:
        state = GateState.AUTO

    if not findings_list:
        findings_list = [f"max cyclomatic complexity on changed code = {max_complexity}"]
    return QualityAxis(
        name="complexity",
        state=state,
        findings=tuple(findings_list),
        raw=float(max_complexity),
    )


# --- Axis 5: Coverage delta (coverage.py) -----------------------------

def run_coverage(
    files: list[Path], config: AxisConfig, project_root: Path
) -> QualityAxis:
    """Read coverage.py's last report and classify by absolute coverage.

    The delta calculation (drop vs. baseline) requires a stored baseline;
    v0.1 implements the simpler absolute-floor check. v0.2 can extend
    to compare against a baseline file the project commits.
    """
    if not config.enabled:
        return _skipped("coverage", "coverage axis disabled in quality.yaml")
    coverage_bin = _tool_available("coverage")
    if coverage_bin is None:
        return _skipped("coverage", "coverage.py not installed")
    coverage_file = project_root / ".coverage"
    if not coverage_file.exists():
        return _skipped("coverage", ".coverage file not found; run tests with coverage first")

    cmd = [coverage_bin, "json", "--quiet", "-o", "-"]
    result = _run(cmd, cwd=project_root, timeout=30)
    try:
        report = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError:
        return _skipped("coverage", "coverage json output not parseable")

    totals = report.get("totals", {})
    percent = float(totals.get("percent_covered", 0.0))
    block_below = float(config.get("block_absolute_below", 80.0))

    if percent < block_below:
        state = GateState.BLOCK
        findings = (f"line coverage {percent:.1f}% below floor {block_below:.0f}%",)
    elif percent < block_below + 5:
        state = GateState.NOTIFY
        findings = (f"line coverage {percent:.1f}% within 5pp of the floor",)
    else:
        state = GateState.AUTO
        findings = (f"line coverage {percent:.1f}%",)

    return QualityAxis(name="coverage", state=state, findings=findings, raw=percent)


# --- Driver -----------------------------------------------------------

def run_all_axes(
    files: list[Path], config, project_root: Path
) -> tuple[QualityAxis, ...]:
    """Run every configured axis and return the per-axis results in order.

    `config` is a `QualityConfig`. The function exists so tests and the
    hook can drive the gate without duplicating the per-axis dispatch.
    """
    return (
        run_correctness(files, config.correctness, project_root),
        run_security(files, config.security, project_root),
        run_maintainability(files, config.maintainability, project_root),
        run_complexity(files, config.complexity, project_root),
        run_coverage(files, config.coverage, project_root),
    )
