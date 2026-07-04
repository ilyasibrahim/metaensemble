"""Tests for the Suite-A acceptance checkers (`evals/runners/acceptance.py`).

One focused test per criterion kind — each exercising a pass path and a
fail path against a throwaway workspace — plus an integration test that
runs a full criteria list mirroring task `a1_bugfix_off_by_one` against
a synthetic fixed workspace. No Claude calls; every subprocess is local
(pytest, ruff, git, python).
"""
from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path

import pytest

from evals.runners import acceptance
from evals.runners.acceptance import (
    AcceptanceReport,
    BaselineStats,
    check_acceptance,
    collect_baseline_stats,
)

NO_BASELINE = BaselineStats(test_count=0, public_api=None)

PASSING_TEST = "def test_ok():\n    assert True\n"
FAILING_TEST = "def test_broken():\n    assert False\n"
TWO_TESTS = (
    "def test_one():\n    assert True\n\n\n"
    "def test_two():\n    assert True\n"
)


def _ws(tmp_path: Path, files: dict[str, str]) -> Path:
    """Plain (non-git) workspace with the given relative files."""
    ws = tmp_path / "ws"
    for rel, content in files.items():
        path = ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _git(ws: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c", "user.name=Eval Tests",
            "-c", "user.email=eval-tests@metaensemble.invalid",
            *args,
        ],
        cwd=str(ws),
        check=True,
        capture_output=True,
        text=True,
    )


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """Git workspace with one commit containing the given files."""
    ws = _ws(tmp_path, files)
    _git(ws, "init", "-q")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "baseline")
    return ws


def _grade(ws: Path, criteria: list[dict], baseline: BaselineStats = NO_BASELINE):
    return check_acceptance(ws, criteria, baseline=baseline)


# ---------------------------------------------------------------------------
# Report shape and fail-closed dispatch
# ---------------------------------------------------------------------------

def test_report_dataclasses_are_frozen():
    report = AcceptanceReport(passed=True, score=1.0, details=[])
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.passed = False  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        NO_BASELINE.test_count = 9  # type: ignore[misc]


def test_unknown_kind_fails_closed_and_scores_fraction(tmp_path):
    ws = _ws(tmp_path, {"report.md": "hello"})
    report = _grade(ws, [
        {"kind": "file_exists", "glob": "report.md"},
        {"kind": "frobnicate_the_widgets"},
    ])
    assert not report.passed
    assert report.score == 0.5
    assert report.details[0].startswith("PASS file_exists — ")
    assert report.details[1].startswith("FAIL frobnicate_the_widgets — ")
    assert "unknown criterion" in report.details[1]


def test_checker_exception_becomes_fail_line(tmp_path, monkeypatch):
    ws = _ws(tmp_path, {})

    def _boom(*args, **kwargs):
        raise RuntimeError("subprocess exploded")

    monkeypatch.setattr(acceptance.subprocess, "run", _boom)
    report = _grade(ws, [{"kind": "build_passes"}])
    assert not report.passed
    assert report.details[0].startswith("FAIL build_passes — ")
    assert "RuntimeError" in report.details[0]
    assert "subprocess exploded" in report.details[0]


def test_nonexistent_workspace_never_raises(tmp_path):
    ws = tmp_path / "does-not-exist"
    report = _grade(ws, [
        {"kind": "build_passes"},
        {"kind": "file_modified", "path": "x.py"},
        {"kind": "file_exists", "glob": "*.md"},
    ])
    assert not report.passed
    assert report.score == 0.0
    assert all(line.startswith("FAIL ") for line in report.details)


# ---------------------------------------------------------------------------
# One focused test per checker kind
# ---------------------------------------------------------------------------

def test_build_passes(tmp_path):
    ok_ws = _ws(tmp_path / "ok", {"test_smoke.py": PASSING_TEST})
    report = _grade(ok_ws, [{"kind": "build_passes"}])
    assert report.passed and report.score == 1.0
    assert report.details[0].startswith("PASS build_passes — ")

    bad_ws = _ws(tmp_path / "bad", {"test_smoke.py": FAILING_TEST})
    report = _grade(bad_ws, [{"kind": "build_passes"}])
    assert not report.passed
    assert report.details[0].startswith("FAIL build_passes — ")


def test_test_count_at_least(tmp_path):
    ws = _ws(tmp_path, {"test_counts.py": TWO_TESTS})
    ok = _grade(ws, [{"kind": "test_count_at_least", "value": 2}])
    assert ok.passed
    assert "2 tests collected" in ok.details[0]

    fail = _grade(ws, [{"kind": "test_count_at_least", "value": 3}])
    assert not fail.passed
    assert "2 tests collected < 3" in fail.details[0]


def test_test_count_delta_at_least(tmp_path):
    ws = _ws(tmp_path, {"test_counts.py": TWO_TESTS})
    baseline = BaselineStats(test_count=1, public_api=None)
    ok = _grade(ws, [{"kind": "test_count_delta_at_least", "value": 1}], baseline)
    assert ok.passed
    assert "baseline 1" in ok.details[0]

    fail = _grade(ws, [{"kind": "test_count_delta_at_least", "value": 2}], baseline)
    assert not fail.passed


def test_lint_clean(tmp_path):
    ok_ws = _ws(tmp_path / "ok", {"clean.py": "X = 1\n"})
    assert _grade(ok_ws, [{"kind": "lint_clean"}]).passed

    bad_ws = _ws(tmp_path / "bad", {"dirty.py": "import os\n"})  # F401
    report = _grade(bad_ws, [{"kind": "lint_clean"}])
    assert not report.passed
    assert report.details[0].startswith("FAIL lint_clean — ")


def test_file_modified(tmp_path):
    ws = _repo(tmp_path, {"pagination.py": "PAGE = 1\n"})
    (ws / "pagination.py").write_text("PAGE = 2\n", encoding="utf-8")
    (ws / "test_pagination.py").write_text(PASSING_TEST, encoding="utf-8")

    tracked = _grade(ws, [{"kind": "file_modified", "path": "pagination.py"}])
    assert tracked.passed, "unstaged edit to a tracked file counts as modified"

    untracked = _grade(ws, [{"kind": "file_modified", "path": "test_pagination.py"}])
    assert untracked.passed, "a brand-new untracked file counts as modified"

    miss = _grade(ws, [{"kind": "file_modified", "path": "other.py"}])
    assert not miss.passed
    assert "other.py not among" in miss.details[0]


def test_file_modified_inside_new_directory(tmp_path):
    ws = _repo(tmp_path, {"README.md": "hi\n"})
    (ws / "reports").mkdir()
    (ws / "reports" / "review.md").write_text("body\n", encoding="utf-8")
    report = _grade(ws, [{"kind": "file_modified", "path": "reports/review.md"}])
    assert report.passed, "untracked files inside new directories are listed"


def test_api_surface_preserved(tmp_path):
    files = {
        "mymod.py": "def foo():\n    return 1\n\n\nBAR = 2\n",
        "api_manifest.json": json.dumps({"mymod": ["foo", "BAR"]}),
    }
    ws = _repo(tmp_path, files)
    baseline = collect_baseline_stats(ws)
    assert baseline.public_api == {"mymod": ["foo", "BAR"]}

    ok = _grade(ws, [{"kind": "api_surface_preserved"}], baseline)
    assert ok.passed
    assert "2 public names" in ok.details[0]

    # Agent deletes a public name: the baseline snapshot catches it even
    # if the agent also rewrote the manifest.
    (ws / "mymod.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    broken = _grade(ws, [{"kind": "api_surface_preserved"}], baseline)
    assert not broken.passed
    assert "BAR" in broken.details[0]


def test_api_surface_preserved_missing_manifest_fails(tmp_path):
    ws = _ws(tmp_path, {"mymod.py": "def foo():\n    return 1\n"})
    report = _grade(ws, [{"kind": "api_surface_preserved"}], NO_BASELINE)
    assert not report.passed
    assert "api_manifest.json missing" in report.details[0]


def test_api_surface_preserved_import_error_fails(tmp_path):
    ws = _ws(tmp_path, {
        "mymod.py": "raise RuntimeError('import-time boom')\n",
        "api_manifest.json": json.dumps({"mymod": ["foo"]}),
    })
    report = _grade(ws, [{"kind": "api_surface_preserved"}], NO_BASELINE)
    assert not report.passed
    assert "failed to import" in report.details[0]


def test_markdown_links_resolve(tmp_path):
    ws = _repo(tmp_path, {
        "USER-GUIDE.md": "# Guide\n",
        "docs/DEPLOYMENT.md": "# Deploy\n",
    })
    (ws / "USER-GUIDE.md").write_text(
        "# Guide\n\nSee [deploy](docs/DEPLOYMENT.md), "
        "[section](#rollback), and [site](https://example.com/x.md).\n",
        encoding="utf-8",
    )
    ok = _grade(ws, [{"kind": "markdown_links_resolve"}])
    assert ok.passed, ok.details
    assert "1 relative links resolve" in ok.details[0]

    (ws / "USER-GUIDE.md").write_text(
        "# Guide\n\nSee [ghost](docs/MISSING.md).\n", encoding="utf-8"
    )
    broken = _grade(ws, [{"kind": "markdown_links_resolve"}])
    assert not broken.passed
    assert "docs/MISSING.md" in broken.details[0]


def test_markdown_links_resolve_relative_to_md_file(tmp_path):
    ws = _repo(tmp_path, {
        "docs/guide.md": "# G\n",
        "docs/other.md": "# O\n",
    })
    (ws / "docs" / "guide.md").write_text(
        "[sibling](other.md)\n", encoding="utf-8"
    )
    assert _grade(ws, [{"kind": "markdown_links_resolve"}]).passed


def test_file_exists(tmp_path):
    ws = _ws(tmp_path, {"reports/2026-07-04-uninstall-review.md": "words\n"})
    ok = _grade(ws, [{"kind": "file_exists", "glob": "reports/*-uninstall-review.md"}])
    assert ok.passed

    miss = _grade(ws, [{"kind": "file_exists", "glob": "reports/*-security.md"}])
    assert not miss.passed
    assert "no files match" in miss.details[0]


def test_word_count_at_least(tmp_path):
    ws = _ws(tmp_path, {"reports/review.md": "alpha beta gamma delta epsilon\n"})
    ok = _grade(ws, [
        {"kind": "word_count_at_least", "value": 5, "glob": "reports/*.md"},
    ])
    assert ok.passed
    assert "5 words >= 5" in ok.details[0]

    fail = _grade(ws, [
        {"kind": "word_count_at_least", "value": 300, "glob": "reports/*.md"},
    ])
    assert not fail.passed
    assert "max word count 5" in fail.details[0]


def test_word_count_falls_back_to_file_exists_glob(tmp_path):
    ws = _ws(tmp_path, {"reports/review.md": "one two three four five six\n"})
    criteria = [
        {"kind": "file_exists", "glob": "reports/*.md"},
        {"kind": "word_count_at_least", "value": 6},
    ]
    report = _grade(ws, criteria)
    assert report.passed, report.details

    no_glob = _grade(ws, [{"kind": "word_count_at_least", "value": 6}])
    assert not no_glob.passed
    assert "no glob" in no_glob.details[0]


def test_perf_benchmark_passes(tmp_path):
    ws = _ws(tmp_path, {
        "test_perf.py": (
            "def test_get_window_burn_meets_p95():\n    assert True\n\n\n"
            "def test_unrelated_failing():\n    assert False\n"
        ),
    })
    ok = _grade(ws, [
        {"kind": "perf_benchmark_passes", "benchmark": "test_get_window_burn_meets_p95"},
    ])
    assert ok.passed, ok.details
    assert "1 passed" in ok.details[0]

    none = _grade(ws, [
        {"kind": "perf_benchmark_passes", "benchmark": "test_not_a_real_benchmark"},
    ])
    assert not none.passed
    assert "no tests matched" in none.details[0]

    failing = _grade(ws, [
        {"kind": "perf_benchmark_passes", "benchmark": "test_unrelated_failing"},
    ])
    assert not failing.passed


def test_quiet_addopts_workspace_still_counts_and_benchmarks(tmp_path):
    """Every Suite-A starting repo sets `addopts = "-q"` in pyproject.toml,
    stacking with the checkers' own `-q` into `-qq`. At that verbosity
    pytest prints per-file `path: N` collection lines and suppresses the
    "N passed" summary; both must still parse."""
    ws = _ws(tmp_path, {
        "pyproject.toml": (
            "[project]\n"
            'name = "quiet-ws"\n'
            'version = "0.0.0"\n'
            "\n"
            "[tool.pytest.ini_options]\n"
            'addopts = "-q"\n'
        ),
        "test_quiet.py": TWO_TESTS,
    })
    assert collect_baseline_stats(ws).test_count == 2

    counted = _grade(ws, [{"kind": "test_count_at_least", "value": 2}])
    assert counted.passed, counted.details
    assert "2 tests collected" in counted.details[0]

    bench = _grade(ws, [{"kind": "perf_benchmark_passes", "benchmark": "test_one"}])
    assert bench.passed, bench.details
    assert "1 passed" in bench.details[0]


def test_ci_yaml_has_matrix_axis(tmp_path):
    ci = (
        "name: CI\n"
        "on: [push]\n"
        "jobs:\n"
        "  test:\n"
        "    strategy:\n"
        "      matrix:\n"
        "        quality: [full, no-quality]\n"
        "        python-version: ['3.11']\n"
        "    runs-on: ubuntu-latest\n"
    )
    ws = _ws(tmp_path, {".github/workflows/ci.yml": ci})
    ok = _grade(ws, [{"kind": "ci_yaml_has_matrix_axis", "axis": "no-quality"}])
    assert ok.passed
    assert "'quality'" in ok.details[0]

    miss = _grade(ws, [{"kind": "ci_yaml_has_matrix_axis", "axis": "gpu"}])
    assert not miss.passed

    empty = _ws(tmp_path / "empty", {})
    absent = _grade(empty, [{"kind": "ci_yaml_has_matrix_axis", "axis": "no-quality"}])
    assert not absent.passed
    assert "missing" in absent.details[0]


# ---------------------------------------------------------------------------
# Baseline collection
# ---------------------------------------------------------------------------

def test_collect_baseline_stats(tmp_path):
    ws = _ws(tmp_path, {
        "test_counts.py": TWO_TESTS,
        "api_manifest.json": json.dumps({"mod": ["name"]}),
    })
    baseline = collect_baseline_stats(ws)
    assert baseline.test_count == 2
    assert baseline.public_api == {"mod": ["name"]}


def test_collect_baseline_stats_never_raises(tmp_path):
    baseline = collect_baseline_stats(tmp_path / "does-not-exist")
    assert baseline == BaselineStats(test_count=0, public_api=None)

    bad_manifest = _ws(tmp_path, {"api_manifest.json": "{not json"})
    assert collect_baseline_stats(bad_manifest).public_api is None


# ---------------------------------------------------------------------------
# Integration: full criteria list mirroring task a1_bugfix_off_by_one
# ---------------------------------------------------------------------------

PAGINATION_FIXED = '''"""Tiny paginator fixture."""


def paginate(items, page, size):
    start = page * size
    return items[start:start + size]
'''

PAGINATION_TESTS = '''"""Paginator regression tests."""

from pagination import paginate

ITEMS = list(range(10))


def test_first_page():
    assert paginate(ITEMS, 0, 3) == [0, 1, 2]


def test_second_page():
    assert paginate(ITEMS, 1, 3) == [3, 4, 5]


def test_last_partial_page():
    assert paginate(ITEMS, 3, 3) == [9]


def test_empty_beyond_end():
    assert paginate(ITEMS, 5, 3) == []


def test_page_size_one():
    assert paginate(ITEMS, 4, 1) == [4]
'''

REGRESSION_TEST = '''

def test_boundary_regression():
    assert paginate(ITEMS, 2, 3) == [6, 7, 8]
'''


def test_integration_task_a1_full_criteria_list(tmp_path):
    # Baseline: buggy paginator committed at the starting SHA.
    ws = _repo(tmp_path, {
        "pagination.py": PAGINATION_FIXED.replace(
            "items[start:start + size]", "items[start:start + size - 1]"
        ),
        "test_pagination.py": PAGINATION_TESTS,
    })
    baseline = collect_baseline_stats(ws)
    assert baseline.test_count == 5

    # Simulated agent run: fix the off-by-one, add a regression test.
    (ws / "pagination.py").write_text(PAGINATION_FIXED, encoding="utf-8")
    (ws / "test_pagination.py").write_text(
        PAGINATION_TESTS + REGRESSION_TEST, encoding="utf-8"
    )

    criteria = [
        {"kind": "build_passes"},
        {"kind": "test_count_at_least", "value": 5},
        {"kind": "lint_clean"},
        {"kind": "file_modified", "path": "pagination.py"},
        {"kind": "file_modified", "path": "test_pagination.py"},
    ]
    report = check_acceptance(ws, criteria, baseline=baseline)
    assert report.passed, report.details
    assert report.score == 1.0
    assert len(report.details) == 5
    assert all(line.startswith("PASS ") for line in report.details)
    assert all(" — " in line for line in report.details)


def test_integration_task_a1_unfixed_workspace_fails_build(tmp_path):
    # Agent "ran" but never fixed the bug: build_passes and file_modified fail.
    ws = _repo(tmp_path, {
        "pagination.py": PAGINATION_FIXED.replace(
            "items[start:start + size]", "items[start:start + size - 1]"
        ),
        "test_pagination.py": PAGINATION_TESTS,
    })
    baseline = collect_baseline_stats(ws)
    criteria = [
        {"kind": "build_passes"},
        {"kind": "test_count_at_least", "value": 5},
        {"kind": "lint_clean"},
        {"kind": "file_modified", "path": "pagination.py"},
    ]
    report = check_acceptance(ws, criteria, baseline=baseline)
    assert not report.passed
    assert report.score == pytest.approx(2 / 4)
    assert report.details[0].startswith("FAIL build_passes — ")
    assert report.details[3].startswith("FAIL file_modified — ")
