"""Regression: the curated-name catalog ignores filesystem duplicates.

macOS Finder and some pip-install upgrade paths leave behind
" N"-suffixed copies (`architect 2.md`, `backend 3.md`) in the installed
package directory. These are not real curated Roles or commands. The
catalog must ignore them so the inspect renderer does not surface them
as legitimate "retired" Roles to the Principal.

Reproduces the regression where `metaensemble inspect` on the Somali
project listed 23 spurious "architect 2"-style Roles in the optional
section.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from metaensemble.lib.installer import (
    _is_canonical_curated_name,
    _metaensemble_curated_names,
)


def test_canonical_filter_rejects_macos_duplicates():
    assert _is_canonical_curated_name("architect")
    assert _is_canonical_curated_name("backend")
    assert _is_canonical_curated_name("ml-engineer")
    assert _is_canonical_curated_name("metaensemble-protocol")

    # macOS-style "X N" duplicates must be filtered out.
    assert not _is_canonical_curated_name("architect 2")
    assert not _is_canonical_curated_name("backend 3")
    assert not _is_canonical_curated_name("docs 10")

    # Names with a hyphen-N or underscore-N suffix are legitimate; only
    # the SPACE-then-digits pattern is the duplicate marker.
    assert _is_canonical_curated_name("v2-experiment")
    assert _is_canonical_curated_name("agent-2")
    assert _is_canonical_curated_name("backend_2")

    # The package's README file is also excluded by convention.
    assert not _is_canonical_curated_name("README")


def test_curated_catalog_ignores_duplicate_files_on_disk(tmp_path, monkeypatch):
    """Stage a fake CORE_DIR with both canonical Role files and duplicates.

    The catalog must return only the canonical names — neither the
    duplicates nor any spurious entries.
    """
    fake_core = tmp_path / "metaensemble"
    (fake_core / "roles").mkdir(parents=True)
    (fake_core / "commands").mkdir()
    (fake_core / "skills").mkdir()
    (fake_core / "output-styles").mkdir()

    # Canonical files
    for name in ("architect.md", "backend.md", "ml-engineer.md"):
        (fake_core / "roles" / name).write_text("# role")
    (fake_core / "commands" / "dispatch.md").write_text("# command")
    (fake_core / "output-styles" / "wire.md").write_text("# style")
    (fake_core / "skills" / "metaensemble-protocol").mkdir()

    # Stray duplicates (macOS Finder pattern)
    for name in (
        "architect 2.md", "architect 3.md", "backend 2.md",
        "ml-engineer 2.md", "ml-engineer 3.md", "ml-engineer 4.md",
    ):
        (fake_core / "roles" / name).write_text("# stray")
    (fake_core / "commands" / "dispatch 2.md").write_text("# stray")
    (fake_core / "output-styles" / "wire 2.md").write_text("# stray")
    (fake_core / "skills" / "metaensemble-protocol 2").mkdir()

    # Point CORE_DIR at the fake tree
    from metaensemble.lib import installer
    monkeypatch.setattr(installer, "CORE_DIR", fake_core)

    catalog = _metaensemble_curated_names()
    assert catalog["agent"] == {"architect", "backend", "ml-engineer"}
    assert catalog["command"] == {"dispatch"}
    assert catalog["output-style"] == {"wire"}
    assert catalog["skill"] == {"metaensemble-protocol"}
