"""Gates for the Suite-A fixture builder and the pinned tasks.yaml SHAs.

Four contracts:

1. `evals.fixtures.build.build_fixture` is deterministic — two builds
   on this machine produce the same single-commit SHA, and that SHA is
   the hard-coded `FIXTURE_SHAS` value tasks.yaml pins.
2. The paginator fixture's own tests pass while the seeded page-boundary
   bug still reproduces, so task a1 has real work to do.
3. The legacy fixture's 12 tests pass and `api_manifest.json` matches
   the module's real public surface, so `api_surface_preserved` has a
   truthful baseline.
4. `evals/datasets/suite_a/tasks.yaml` carries no deferred placeholders
   and every `metaensemble` task pins the v0.2.0 release commit.
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from evals.fixtures.build import FIXTURE_SHAS, build_fixture

REPO_ROOT = Path(__file__).resolve().parents[2]
TASKS_YAML = REPO_ROOT / "evals" / "datasets" / "suite_a" / "tasks.yaml"

# v0.2.0 release commit (`git rev-parse v0.2.0^{commit}`), pinned as
# `starting_sha` by every `starting_repo: metaensemble` task.
V020_SHA = "27ac404d80312028eff49a5dca3a04338ff8f8ed"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="fixture builder requires git"
)


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> dict[str, Path]:
    """Each fixture built once, keyed by name, after SHA verification."""
    workspaces: dict[str, Path] = {}
    for name, expected_sha in FIXTURE_SHAS.items():
        dest = tmp_path_factory.mktemp("fixture") / name
        sha = build_fixture(name, dest)
        assert sha == expected_sha, (
            f"{name} built {sha}, but FIXTURE_SHAS pins {expected_sha}; "
            "re-run `python -m evals.fixtures.build --print-shas` and "
            "re-pin FIXTURE_SHAS plus tasks.yaml if the fixture changed "
            "intentionally."
        )
        workspaces[name] = dest
    return workspaces


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations via sys.modules, so the
    # module must be registered while its body executes.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def _run_pytest(workspace: Path) -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider"],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"fixture test suite failed in {workspace}:\n"
        f"{proc.stdout}\n{proc.stderr}"
    )
    return proc.stdout


def test_build_fixture_is_deterministic(built, tmp_path):
    for name, expected_sha in FIXTURE_SHAS.items():
        again = build_fixture(name, tmp_path / f"again-{name}")
        assert again == expected_sha, f"{name} rebuild produced a new SHA"


def test_build_fixture_rejects_unknown_name(tmp_path):
    with pytest.raises(ValueError, match="unknown fixture"):
        build_fixture("oss-fixture-nonexistent", tmp_path / "x")


def test_paginator_fixture_tests_pass(built):
    stdout = _run_pytest(built["oss-fixture-paginator"])
    assert re.search(r"\b4 passed\b", stdout), stdout


def test_paginator_boundary_bug_reproduces(built):
    pagination = _load_module(
        built["oss-fixture-paginator"] / "pagination.py",
        "_fixture_pagination",
    )
    # Intended behavior per the module docstring is [3, 4, 5] / [9];
    # the frozen fixture must still exhibit the off-by-one so task a1
    # has a real bug to fix.
    assert pagination.paginate(list(range(6)), 1, 3) == [3, 4]
    assert pagination.paginate(list(range(10)), 3, 3) == []
    # Interior pages stay correct — the fixture's own tests cover these.
    assert pagination.paginate(list(range(10)), 0, 3) == [0, 1, 2]


def test_legacy_fixture_tests_pass(built):
    stdout = _run_pytest(built["oss-fixture-legacy"])
    assert re.search(r"\b12 passed\b", stdout), stdout


def test_legacy_api_manifest_matches_public_surface(built):
    workspace = built["oss-fixture-legacy"]
    manifest = json.loads((workspace / "api_manifest.json").read_text())
    assert list(manifest) == ["legacy.big_module"]
    module = _load_module(
        workspace / "legacy" / "big_module.py", "_fixture_big_module"
    )
    assert manifest["legacy.big_module"] == sorted(module.__all__)
    for name in manifest["legacy.big_module"]:
        assert hasattr(module, name), f"manifest names missing symbol {name}"


def test_tasks_yaml_has_no_deferred_shas():
    raw = TASKS_YAML.read_text()
    assert "__DEFERRED__" not in raw
    data = yaml.safe_load(raw)
    for task in data["tasks"]:
        sha = task["starting_sha"]
        assert re.fullmatch(r"[0-9a-f]{40}", sha), (
            f"{task['id']} starting_sha is not a full 40-char SHA: {sha!r}"
        )


def test_tasks_yaml_pins_expected_shas():
    data = yaml.safe_load(TASKS_YAML.read_text())
    for task in data["tasks"]:
        repo = task["starting_repo"]
        sha = task["starting_sha"]
        if repo == "metaensemble":
            assert sha == V020_SHA, (
                f"{task['id']} must pin the v0.2.0 commit {V020_SHA}, got {sha}"
            )
        else:
            assert repo in FIXTURE_SHAS, f"{task['id']} names unknown repo {repo!r}"
            assert sha == FIXTURE_SHAS[repo], (
                f"{task['id']} must pin FIXTURE_SHAS[{repo!r}]"
            )


def test_v020_constant_matches_tag_when_resolvable():
    """Cross-check V020_SHA against the local tag; skip on shallow clones."""
    proc = subprocess.run(
        ["git", "rev-parse", "v0.2.0^{commit}"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.skip("v0.2.0 tag not resolvable in this checkout")
    assert proc.stdout.strip() == V020_SHA
