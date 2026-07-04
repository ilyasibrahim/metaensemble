"""Acceptance checkers for Suite-A live evaluation.

Grades a post-agent workspace (the task's git repo after the Executor
ran) against the task's declared acceptance criteria from
`evals/datasets/suite_a/tasks.yaml`. Two entry points:

- `collect_baseline_stats(workspace)` runs BEFORE the agent, at the
  task's starting SHA, and snapshots the numbers delta-style criteria
  compare against (collected test count, declared public API).
- `check_acceptance(workspace, criteria, baseline=...)` runs AFTER the
  agent and grades every criterion, producing one detail line each in
  the form ``PASS/FAIL <kind> — <detail>``.

Fail-closed contract: unknown criterion kinds FAIL with "unknown
criterion", any checker exception becomes a FAIL detail line, and
neither entry point ever raises. Checker subprocesses run with
``cwd=workspace`` and a 300-second default timeout.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

DEFAULT_TIMEOUT_S = 300

_MANIFEST_NAME = "api_manifest.json"
_CI_WORKFLOW_PATH = Path(".github") / "workflows" / "ci.yml"

# `[text](target)` / `![alt](target)`; captures the target up to the
# first whitespace or closing paren so `(path "title")` still parses.
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(\s*<?([^)>\s]+)>?[^)]*\)")
_COLLECTED_RE = re.compile(r"(\d+)\s+tests?\s+collected")
_PASSED_RE = re.compile(r"(\d+)\s+passed")
# Suite-A workspaces set `addopts = "-q"` in pyproject.toml, which stacks
# with our own `-q` into `-qq`; at that verbosity `--collect-only` prints
# one `<path>: <count>` line per file instead of node ids or the
# "N tests collected" summary.
_QQ_COLLECT_RE = re.compile(r"(?m)^(\S+):\s+(\d+)\s*$")


@dataclass(frozen=True)
class BaselineStats:
    """Workspace stats captured at the starting SHA, before the agent runs."""

    test_count: int
    public_api: dict[str, list[str]] | None


@dataclass(frozen=True)
class AcceptanceReport:
    """Graded acceptance result for one run of one task."""

    passed: bool        # every criterion passed
    score: float        # fraction of criteria passed, 0.0 - 1.0
    details: list[str]  # one line per criterion: "PASS/FAIL kind — detail"


def collect_baseline_stats(workspace: Path) -> BaselineStats:
    """Snapshot pre-agent stats at the starting SHA. Never raises."""
    try:
        count, _ = _collect_test_count(workspace)
        test_count = count if count is not None else 0
    except Exception:
        test_count = 0
    try:
        public_api, _ = _read_api_manifest(workspace)
    except Exception:
        public_api = None
    return BaselineStats(test_count=test_count, public_api=public_api)


def check_acceptance(
    workspace: Path,
    criteria: list[dict],
    *,
    baseline: BaselineStats,
) -> AcceptanceReport:
    """Grade every criterion against the post-agent workspace. Never raises."""
    details: list[str] = []
    passed_count = 0
    for criterion in criteria:
        if isinstance(criterion, dict):
            kind = str(criterion.get("kind") or "")
        else:
            kind, criterion = "", {}
        checker = _CHECKERS.get(kind)
        if checker is None:
            ok, detail = False, f"unknown criterion kind {kind!r}"
        else:
            try:
                ok, detail = checker(workspace, criterion, criteria, baseline)
            except Exception as exc:  # fail-closed: exceptions grade as FAIL
                ok, detail = False, f"checker raised {type(exc).__name__}: {exc}"
        status = "PASS" if ok else "FAIL"
        details.append(f"{status} {kind or '<missing kind>'} — {detail}")
        if ok:
            passed_count += 1
    total = len(criteria)
    return AcceptanceReport(
        passed=(passed_count == total),
        score=(passed_count / total) if total else 1.0,
        details=details,
    )


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------

def _subprocess_env() -> dict[str, str]:
    """Inherited env minus PYTHONPATH so workspace imports stay isolated."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run(
    cmd: list[str],
    workspace: Path,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(workspace),
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _snip(text: str, limit: int = 240) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _last_line(proc: subprocess.CompletedProcess) -> str:
    for stream in (proc.stdout, proc.stderr):
        lines = [ln for ln in (stream or "").splitlines() if ln.strip()]
        if lines:
            return _snip(lines[-1])
    return f"exit {proc.returncode}, no output"


def _pytest_cmd(*extra: str) -> list[str]:
    return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *extra]


def _collect_test_count(workspace: Path) -> tuple[int | None, str]:
    """Parse `pytest --collect-only -q` collected-test count.

    Returns `(count, detail)`; count is None when the count could not be
    determined (pytest missing, collection error, unparseable output).
    """
    proc = _run(_pytest_cmd("--collect-only"), workspace)
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if "No module named pytest" in output:
        return None, "pytest unavailable in the eval interpreter"
    match = _COLLECTED_RE.search(output)
    if match:
        count = int(match.group(1))
    elif "no tests collected" in output or "no tests ran" in output:
        count = 0
    else:
        qq_counts = [
            int(n) for _, n in _QQ_COLLECT_RE.findall(proc.stdout or "")
        ]
        node_lines = [
            ln for ln in (proc.stdout or "").splitlines() if "::" in ln
        ]
        if qq_counts:
            count = sum(qq_counts)  # `-qq` per-file collection summary
        elif proc.returncode == 5:
            count = 0  # pytest exit 5: no tests collected (silent at -qq)
        elif not node_lines and proc.returncode != 0:
            return None, f"collection failed: {_last_line(proc)}"
        else:
            count = len(node_lines)
    if "error" in output and proc.returncode not in (0, 5):
        return None, f"collection errored: {_last_line(proc)}"
    return count, f"{count} tests collected"


def _modified_files(workspace: Path) -> tuple[set[str], str | None]:
    """Staged + unstaged + untracked paths, per git. `(paths, error)`."""
    paths: set[str] = set()
    diff = _run(["git", "diff", "--name-only", "HEAD"], workspace)
    if diff.returncode != 0:
        return set(), f"git diff failed: {_last_line(diff)}"
    paths.update(ln.strip() for ln in diff.stdout.splitlines() if ln.strip())
    status = _run(
        ["git", "status", "--porcelain", "--untracked-files=all"], workspace
    )
    if status.returncode != 0:
        return set(), f"git status failed: {_last_line(status)}"
    for line in status.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:  # rename: report the new path
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if path:
            paths.add(path)
    return paths, None


def _read_api_manifest(
    workspace: Path,
) -> tuple[dict[str, list[str]] | None, str | None]:
    """Read `api_manifest.json` (module → public names). `(mapping, error)`."""
    manifest_path = workspace / _MANIFEST_NAME
    if not manifest_path.is_file():
        return None, f"{_MANIFEST_NAME} missing at workspace root"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{_MANIFEST_NAME} unreadable: {_snip(str(exc))}"
    if not isinstance(data, dict) or not data:
        return None, f"{_MANIFEST_NAME} malformed: expected non-empty object"
    mapping: dict[str, list[str]] = {}
    for module, names in data.items():
        if not isinstance(names, list):
            return None, f"{_MANIFEST_NAME} malformed: {module!r} is not a list"
        mapping[str(module)] = [str(n) for n in names]
    return mapping, None


def _int_value(criterion: dict, key: str = "value") -> tuple[int | None, str]:
    raw = criterion.get(key)
    try:
        return int(raw), ""
    except (TypeError, ValueError):
        return None, f"criterion {key} {raw!r} is not an integer"


# ---------------------------------------------------------------------------
# Checkers — each returns (ok, detail) and may raise; the dispatcher
# converts exceptions into FAIL lines.
# ---------------------------------------------------------------------------

def _check_build_passes(
    workspace: Path, criterion: dict, criteria: list[dict], baseline: BaselineStats
) -> tuple[bool, str]:
    proc = _run(_pytest_cmd(), workspace)
    output = (proc.stdout or "") + (proc.stderr or "")
    if "No module named pytest" in output:
        return False, "pytest unavailable in the eval interpreter"
    if proc.returncode == 0:
        return True, f"pytest exit 0 ({_last_line(proc)})"
    return False, f"pytest exit {proc.returncode}: {_last_line(proc)}"


def _check_test_count_at_least(
    workspace: Path, criterion: dict, criteria: list[dict], baseline: BaselineStats
) -> tuple[bool, str]:
    value, err = _int_value(criterion)
    if value is None:
        return False, err
    count, detail = _collect_test_count(workspace)
    if count is None:
        return False, detail
    if count >= value:
        return True, f"{count} tests collected >= {value}"
    return False, f"{count} tests collected < {value}"


def _check_test_count_delta_at_least(
    workspace: Path, criterion: dict, criteria: list[dict], baseline: BaselineStats
) -> tuple[bool, str]:
    value, err = _int_value(criterion)
    if value is None:
        return False, err
    count, detail = _collect_test_count(workspace)
    if count is None:
        return False, detail
    delta = count - baseline.test_count
    comparison = (
        f"delta {delta:+d} (baseline {baseline.test_count}, now {count})"
    )
    if delta >= value:
        return True, f"{comparison} >= {value}"
    return False, f"{comparison} < {value}"


def _check_lint_clean(
    workspace: Path, criterion: dict, criteria: list[dict], baseline: BaselineStats
) -> tuple[bool, str]:
    proc = _run([sys.executable, "-m", "ruff", "check", "."], workspace)
    output = (proc.stdout or "") + (proc.stderr or "")
    if "No module named" in output and "ruff" in output:
        return False, "ruff unavailable in the eval interpreter"
    if proc.returncode == 0:
        return True, "ruff check . exit 0"
    return False, f"ruff exit {proc.returncode}: {_last_line(proc)}"


def _check_file_modified(
    workspace: Path, criterion: dict, criteria: list[dict], baseline: BaselineStats
) -> tuple[bool, str]:
    path = criterion.get("path")
    if not path:
        return False, "criterion has no path"
    target = str(path).replace(os.sep, "/")
    modified, err = _modified_files(workspace)
    if err:
        return False, err
    if target in modified:
        return True, f"{target} modified"
    return False, f"{target} not among {len(modified)} modified files"


def _check_api_surface_preserved(
    workspace: Path, criterion: dict, criteria: list[dict], baseline: BaselineStats
) -> tuple[bool, str]:
    api = baseline.public_api
    if api is None:
        # No pre-agent snapshot: fall back to the post-agent manifest at
        # the workspace root; a missing manifest fails closed.
        api, err = _read_api_manifest(workspace)
        if api is None:
            return False, err or f"{_MANIFEST_NAME} missing at workspace root"
    probe = (
        "import importlib, json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "mod = importlib.import_module(sys.argv[2])\n"
        "names = json.loads(sys.argv[3])\n"
        "missing = [n for n in names if not hasattr(mod, n)]\n"
        "print(json.dumps(missing))\n"
    )
    checked = 0
    for module, names in api.items():
        proc = _run(
            [
                sys.executable, "-I", "-B", "-c", probe,
                str(workspace), module, json.dumps(names),
            ],
            workspace,
        )
        if proc.returncode != 0:
            return False, f"module {module!r} failed to import: {_last_line(proc)}"
        try:
            missing = json.loads(proc.stdout.strip() or "[]")
        except json.JSONDecodeError:
            return False, f"module {module!r} probe output unparseable"
        if missing:
            return (
                False,
                f"module {module!r} lost public names: {', '.join(map(str, missing))}",
            )
        checked += len(names)
    return True, f"{checked} public names across {len(api)} modules importable"


def _check_markdown_links_resolve(
    workspace: Path, criterion: dict, criteria: list[dict], baseline: BaselineStats
) -> tuple[bool, str]:
    modified, err = _modified_files(workspace)
    if err:
        return False, err
    md_files = sorted(p for p in modified if p.lower().endswith(".md"))
    broken: list[str] = []
    checked = 0
    for rel in md_files:
        md_path = workspace / rel
        if not md_path.is_file():  # deleted markdown has no links to check
            continue
        try:
            text = md_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            broken.append(f"{rel} unreadable: {exc}")
            continue
        for match in _MD_LINK_RE.finditer(text):
            target = match.group(1).strip()
            if (
                not target
                or target.startswith("#")
                or target.startswith("mailto:")
                or "://" in target
            ):
                continue
            target = target.split("#", 1)[0]
            if not target:
                continue
            if target.startswith("/"):
                resolved = workspace / target.lstrip("/")
            else:
                resolved = md_path.parent / target
            checked += 1
            if not resolved.exists():
                broken.append(f"{rel}: broken link -> {target}")
    if broken:
        return False, "; ".join(broken[:5])
    return True, f"{checked} relative links resolve across {len(md_files)} modified .md files"


def _check_file_exists(
    workspace: Path, criterion: dict, criteria: list[dict], baseline: BaselineStats
) -> tuple[bool, str]:
    pattern = criterion.get("glob")
    if not pattern:
        return False, "criterion has no glob"
    matches = sorted(str(p.relative_to(workspace)) for p in workspace.glob(str(pattern)))
    if matches:
        return True, f"{pattern} matches {', '.join(matches[:3])}"
    return False, f"no files match {pattern}"


def _check_word_count_at_least(
    workspace: Path, criterion: dict, criteria: list[dict], baseline: BaselineStats
) -> tuple[bool, str]:
    value, err = _int_value(criterion)
    if value is None:
        return False, err
    pattern = criterion.get("glob")
    if not pattern:
        for other in criteria:
            if isinstance(other, dict) and other.get("kind") == "file_exists":
                pattern = other.get("glob")
                if pattern:
                    break
    if not pattern:
        return False, "no glob on criterion and no file_exists glob in criteria list"
    files = [p for p in workspace.glob(str(pattern)) if p.is_file()]
    if not files:
        return False, f"no files match {pattern}"
    best = 0
    best_file = files[0]
    for path in files:
        words = len(path.read_text(encoding="utf-8", errors="replace").split())
        if words > best:
            best, best_file = words, path
    rel = best_file.relative_to(workspace)
    if best >= value:
        return True, f"{rel} has {best} words >= {value}"
    return False, f"max word count {best} ({rel}) < {value}"


def _check_perf_benchmark_passes(
    workspace: Path, criterion: dict, criteria: list[dict], baseline: BaselineStats
) -> tuple[bool, str]:
    benchmark = criterion.get("benchmark")
    if not benchmark:
        return False, "criterion has no benchmark"
    # `-v` cancels the workspace's own `addopts = "-q"` stacking with our
    # `-q` into `-qq`, which would suppress the "N passed" summary this
    # checker parses.
    proc = _run(_pytest_cmd("-v", "-k", str(benchmark)), workspace)
    output = (proc.stdout or "") + (proc.stderr or "")
    if "No module named pytest" in output:
        return False, "pytest unavailable in the eval interpreter"
    if proc.returncode == 5:
        return False, f"no tests matched benchmark {benchmark!r}"
    if proc.returncode != 0:
        return False, f"pytest exit {proc.returncode}: {_last_line(proc)}"
    match = _PASSED_RE.search(output)
    passed = int(match.group(1)) if match else 0
    if passed < 1:
        return False, f"benchmark {benchmark!r} collected no passing tests"
    return True, f"benchmark {benchmark!r}: {passed} passed"


def _check_ci_yaml_has_matrix_axis(
    workspace: Path, criterion: dict, criteria: list[dict], baseline: BaselineStats
) -> tuple[bool, str]:
    axis = criterion.get("axis")
    if not axis:
        return False, "criterion has no axis"
    ci_path = workspace / _CI_WORKFLOW_PATH
    if not ci_path.is_file():
        return False, f"{_CI_WORKFLOW_PATH} missing"
    try:
        doc = yaml.safe_load(ci_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return False, f"{_CI_WORKFLOW_PATH} unparseable: {_snip(str(exc))}"
    hit = _find_matrix_axis(doc, str(axis))
    if hit:
        return True, f"matrix dimension {hit!r} contains {axis!r}"
    return False, f"no matrix dimension lists {axis!r} in {_CI_WORKFLOW_PATH}"


def _find_matrix_axis(node: object, axis: str) -> str | None:
    """Depth-first search for a `matrix` dict with a dimension listing `axis`."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "matrix" and isinstance(value, dict):
                for dim, entries in value.items():
                    if dim in ("include", "exclude"):
                        continue
                    if isinstance(entries, list) and any(
                        str(e) == axis for e in entries
                    ):
                        return str(dim)
            found = _find_matrix_axis(value, axis)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_matrix_axis(item, axis)
            if found:
                return found
    return None


_Checker = Callable[[Path, dict, list, BaselineStats], "tuple[bool, str]"]

_CHECKERS: dict[str, _Checker] = {
    "build_passes": _check_build_passes,
    "test_count_at_least": _check_test_count_at_least,
    "test_count_delta_at_least": _check_test_count_delta_at_least,
    "lint_clean": _check_lint_clean,
    "file_modified": _check_file_modified,
    "api_surface_preserved": _check_api_surface_preserved,
    "markdown_links_resolve": _check_markdown_links_resolve,
    "file_exists": _check_file_exists,
    "word_count_at_least": _check_word_count_at_least,
    "perf_benchmark_passes": _check_perf_benchmark_passes,
    "ci_yaml_has_matrix_axis": _check_ci_yaml_has_matrix_axis,
}
