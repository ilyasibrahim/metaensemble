"""C11 — Catalog hygiene check.

Detects macOS Finder / iCloud sync duplicate files (stem ` N`) leaking into
MetaEnsemble's catalog directories. MetaEnsemble filters them out at
enumeration time so they do not corrupt inspect output, but they consume
disk/iCloud quota and confuse third-party tooling, so the doctor surfaces
them as a WARN with explicit remediation.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from metaensemble.lib import doctor


def _make_clean_layers(root: Path) -> Path:
    """Stage a fake source tree + runtime + user-level layout under `root/home`."""
    home = root / "home"
    # Source tree (CORE_DIR) — points at root/source/metaensemble
    source = root / "source" / "metaensemble"
    (source / "roles").mkdir(parents=True)
    (source / "commands").mkdir()
    (source / "skills" / "metaensemble-protocol").mkdir(parents=True)
    (source / "output-styles").mkdir()
    (source / "roles" / "architect.md").write_text("# role")
    (source / "roles" / "backend.md").write_text("# role")
    (source / "commands" / "dispatch.md").write_text("# cmd")
    (source / "output-styles" / "wire.md").write_text("# style")
    # Vendored runtime under HOME/.metaensemble/runtime
    runtime = home / ".metaensemble" / "runtime"
    for sub in ("roles", "commands", "skills", "output-styles"):
        (runtime / sub).mkdir(parents=True)
    # User-level under HOME/.claude
    user_runtime = home / ".claude"
    for sub in ("commands", "skills", "output-styles"):
        (user_runtime / sub).mkdir(parents=True)
    return source


def _point_core_dir_at(monkeypatch, source_dir: Path) -> None:
    """Redirect installer.CORE_DIR so the catalog scan walks the fake tree."""
    from metaensemble.lib import installer
    monkeypatch.setattr(installer, "CORE_DIR", source_dir)


def test_c11_ok_when_no_duplicates(tmp_path, monkeypatch):
    """Zero duplicate files across all three layers → OK."""
    source = _make_clean_layers(tmp_path)
    _point_core_dir_at(monkeypatch, source)
    home = tmp_path / "home"

    result = doctor.check_catalog_hygiene(home=home)

    assert result.check_id == "C11"
    assert result.status == "OK"
    assert "Zero duplicate" in result.detail
    assert result.remediation is None


def test_c11_warn_on_source_tree_duplicates(tmp_path, monkeypatch):
    """Duplicates in the source tree are surfaced with a count and examples."""
    source = _make_clean_layers(tmp_path)
    _point_core_dir_at(monkeypatch, source)
    home = tmp_path / "home"
    # Plant macOS-Finder-style duplicates in the source tree's roles dir
    for name in ("architect 2.md", "architect 3.md", "backend 2.md", "backend 3.md"):
        (source / "roles" / name).write_text("# stray")

    result = doctor.check_catalog_hygiene(home=home)

    assert result.check_id == "C11"
    assert result.status == "WARN"
    assert "Detected 4 duplicate file(s)" in result.detail
    assert result.remediation is not None
    assert "iCloud" in result.remediation
    assert "exclude `.venv/`" in result.remediation
    # All 4 hits fit under the 5-example cap, so all should be shown.
    for name in ("architect 2.md", "architect 3.md", "backend 2.md", "backend 3.md"):
        assert name in result.detail


def test_c11_warn_truncates_example_list_to_five(tmp_path, monkeypatch):
    """When more than 5 duplicates exist, only the first 5 are shown
    explicitly; the remainder are counted in a trailing summary."""
    source = _make_clean_layers(tmp_path)
    _point_core_dir_at(monkeypatch, source)
    home = tmp_path / "home"
    for i in range(2, 12):  # 10 duplicates
        (source / "roles" / f"architect {i}.md").write_text("# stray")

    result = doctor.check_catalog_hygiene(home=home)

    assert result.status == "WARN"
    assert "Detected 10 duplicate file(s)" in result.detail
    assert "5 more" in result.detail  # `(N more)` trailing count


def test_c11_warn_on_vendored_runtime_duplicates(tmp_path, monkeypatch):
    """Duplicates in the vendored runtime layer are caught too."""
    source = _make_clean_layers(tmp_path)
    _point_core_dir_at(monkeypatch, source)
    home = tmp_path / "home"
    runtime_cmds = home / ".metaensemble" / "runtime" / "commands"
    (runtime_cmds / "dispatch 2.md").write_text("# stray")

    result = doctor.check_catalog_hygiene(home=home)

    assert result.status == "WARN"
    assert "dispatch 2.md" in result.detail


def test_c11_warn_on_user_level_duplicates(tmp_path, monkeypatch):
    """Duplicates in ~/.claude/commands etc. are caught."""
    source = _make_clean_layers(tmp_path)
    _point_core_dir_at(monkeypatch, source)
    home = tmp_path / "home"
    (home / ".claude" / "commands" / "limits 2.md").write_text("# stray")

    result = doctor.check_catalog_hygiene(home=home)

    assert result.status == "WARN"
    assert "limits 2.md" in result.detail


def test_c11_legitimate_names_with_hyphen_underscore_digits_not_flagged(
    tmp_path, monkeypatch
):
    """`backend_2.md`, `agent-2.md`, `v2-experiment.md` are NOT duplicates.

    The pattern is strictly ` N` (space then digits), not `-N` or `_N`.
    """
    source = _make_clean_layers(tmp_path)
    _point_core_dir_at(monkeypatch, source)
    home = tmp_path / "home"
    for name in ("backend_2.md", "agent-2.md", "v2-experiment.md"):
        (source / "roles" / name).write_text("# legit")

    result = doctor.check_catalog_hygiene(home=home)

    assert result.status == "OK", (
        f"hyphen/underscore-N names were wrongly flagged: {result.detail}"
    )


def test_c11_is_registered_in_run_doctor(tmp_path, monkeypatch):
    """C11 must appear in the doctor's full check sequence."""
    report = doctor.run_doctor()
    check_ids = [r.check_id for r in report.results]
    assert "C11" in check_ids, f"C11 missing from doctor report: {check_ids}"
