"""Per-fixture project-signal activation tests.

Each fixture under `metaensemble/tests/fixtures/projects/<name>/` ships with
an `expected_activations.json` declaring exactly which curated Roles must
match and which must not. The detector is run against each fixture and the
result is asserted to match precisely — false positives are as much a
defect as false negatives.

These fixtures pin the v0.1.0 curated Role detector contract.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from metaensemble.lib.installer import detect_role_relevance


FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures" / "projects"


def _fixture_dirs() -> list[Path]:
    if not FIXTURES_ROOT.is_dir():
        return []
    return sorted(
        d for d in FIXTURES_ROOT.iterdir()
        if d.is_dir() and (d / "expected_activations.json").is_file()
    )


@pytest.mark.parametrize("fixture", _fixture_dirs(), ids=lambda p: p.name)
def test_fixture_activations_match_expected(fixture: Path):
    """For each fixture, the detector activates exactly the expected Roles.

    No extras (false positives) and no misses (false negatives). The
    expected_activations.json file is the contract; the detector's output
    is asserted against it as an exact set match.
    """
    expected = json.loads((fixture / "expected_activations.json").read_text())
    expected_active = set(expected["active"])
    expected_inactive = set(expected["inactive"])
    all_expected_roles = expected_active | expected_inactive

    relevance = {r.role_id: r for r in detect_role_relevance(fixture)}
    actual_active = {role_id for role_id, r in relevance.items() if r.relevant}
    actual_inactive = {role_id for role_id, r in relevance.items() if not r.relevant}
    all_detected_roles = actual_active | actual_inactive

    # Sanity: the contract covers exactly the Roles the detector reports.
    # If the curated set grows or shrinks, the fixtures must be updated to
    # match — silent drift is a defect.
    assert all_expected_roles == all_detected_roles, (
        f"fixture {fixture.name}: expected_activations.json covers "
        f"{sorted(all_expected_roles)} but detector reports "
        f"{sorted(all_detected_roles)}"
    )

    unexpected_active = actual_active - expected_active
    unexpected_inactive = expected_active - actual_active
    if unexpected_active or unexpected_inactive:
        msg = [f"fixture {fixture.name} activation mismatch:"]
        if unexpected_active:
            msg.append(f"  Roles activated but expected inactive: {sorted(unexpected_active)}")
        if unexpected_inactive:
            msg.append(f"  Roles expected active but inactive: {sorted(unexpected_inactive)}")
        # Show evidence for debugging
        msg.append("  Evidence by Role:")
        for role_id in sorted(unexpected_active | unexpected_inactive):
            r = relevance[role_id]
            msg.append(f"    {role_id}: relevant={r.relevant}, evidence={r.evidence}")
        pytest.fail("\n".join(msg))


def test_empty_project_activates_nothing():
    """A bare project root with only `.git/` must activate zero Roles.

    This is the false-positive backstop: detectors that fire on minimal
    structure (the previous behavior) would activate `architect` and
    `code-quality` for any project with any Python file. The v0.1.0 catalog
    requires explicit signals.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "empty"
        project.mkdir()
        (project / ".git").mkdir()  # Git but nothing else
        relevance = detect_role_relevance(project)
        active = [r.role_id for r in relevance if r.relevant]
        assert active == [], f"empty project must not activate any Role; got {active}"


def test_all_fixtures_have_expected_activations_file():
    """Every fixture directory must include `expected_activations.json`.

    Forces test authors to declare expected behavior; prevents shipping a
    fixture corpus without its activation contract.
    """
    dirs = [d for d in FIXTURES_ROOT.iterdir() if d.is_dir()]
    missing = [d.name for d in dirs if not (d / "expected_activations.json").is_file()]
    assert not missing, f"fixtures without expected_activations.json: {missing}"


def test_fixture_count_matches_curated_detector_scope():
    """The v0.1.0 detector ships five archetypal fixtures."""
    expected_names = {
        "somali-mini",
        "web-app-django",
        "data-pipeline-dbt",
        "python-library",
        "infra-terraform",
    }
    actual_names = {d.name for d in _fixture_dirs()}
    assert actual_names == expected_names, (
        f"fixture set drifted from the curated detector scope. "
        f"Expected {expected_names}, got {actual_names}."
    )
