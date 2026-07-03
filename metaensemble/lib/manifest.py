"""Manifest, Brief, and Role schema validation — and Manifest scaffolding.

Manifests are YAML on disk; Briefs and Role frontmatter are JSON-shaped
even when written in YAML. All three are validated against the schemas in
`metaensemble/schemas/` using a cached Draft 2020-12 validator (PERFORMANCE.md R3).

Scaffolding lives here rather than in the CLI so the starter Manifest can
consume the project's recorded memory surfaces (installer inspection state)
without the CLI knowing the file formats involved.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from metaensemble.lib.ids import uuid7


SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"


@lru_cache(maxsize=8)
def _validator(schema_name: str) -> Draft202012Validator:
    """Cached validator instance per schema (PERFORMANCE.md R3)."""
    schema_path = SCHEMA_DIR / schema_name
    with schema_path.open() as f:
        schema = json.load(f)
    return Draft202012Validator(schema)


def load_manifest(path: Path | str) -> dict[str, Any]:
    """Load and validate a Manifest YAML file.

    Raises:
        jsonschema.exceptions.ValidationError: on schema mismatch.
        yaml.YAMLError: on parse failure.
    """
    with Path(path).open() as f:
        data = yaml.safe_load(f)
    validate_manifest(data)
    return data


def validate_manifest(data: dict[str, Any]) -> None:
    """Validate a Manifest dict against manifest.schema.json."""
    _validator("manifest.schema.json").validate(data)


def validate_brief(data: dict[str, Any]) -> None:
    """Validate a Brief dict against brief.schema.json."""
    _validator("brief.schema.json").validate(data)


def validate_role_frontmatter(data: dict[str, Any]) -> None:
    """Validate Role spec frontmatter against role.schema.json."""
    _validator("role.schema.json").validate(data)


def _memory_surface_paths(project_root: Path) -> list[str]:
    """Memory-surface paths the scaffold should pre-fill, project-relative.

    Adopted projects record their memory surfaces in
    `.metaensemble/install-decisions.yaml` (written by the installer's
    inspection); that record is authoritative when present — including an
    explicitly empty list. Projects that were never adopted — or whose
    decisions file predates the `memory_surfaces` key (adopt never
    rewrites an existing decisions file) — fall back to re-detecting the
    same surfaces the installer would record, so a pre-key project's
    scaffold matches what a fresh adoption sees. The YAML is read
    directly, and the installer import is deferred into the fallback
    branch, so this module stays light for the hook import path.
    """
    decisions_path = project_root / ".metaensemble" / "install-decisions.yaml"
    if decisions_path.is_file():
        try:
            data = yaml.safe_load(decisions_path.read_text()) or {}
        except (OSError, yaml.YAMLError):
            data = {}
        if isinstance(data, dict) and "memory_surfaces" in data:
            raw = data.get("memory_surfaces") or []
            paths: list[str] = []
            if isinstance(raw, list):
                for entry in raw:
                    if not isinstance(entry, dict):
                        continue
                    surface_path = str(entry.get("path", "")).strip()
                    if surface_path:
                        paths.append(surface_path)
            return paths
    from metaensemble.lib.installer import detect_memory_surfaces

    return [surface.path for surface in detect_memory_surfaces(project_root)]


def scaffold_manifest(task: str, *, project: Path | str | None = None) -> str:
    """Render the starter Manifest YAML for `metaensemble manifest scaffold`.

    The output deliberately fails schema validation until the author
    replaces the `TODO:` markers. When the project's memory surfaces are
    known (see `_memory_surface_paths`), `context.files` is pre-filled
    with one `role: memory` entry per surface so the dispatch consumes
    the memory files the runtime already loads instead of a rebuilt
    context store; only the author-supplied TODO fields then keep the
    scaffold from validating.
    """
    project_root = Path(project) if project is not None else Path.cwd()
    # JSON is a YAML 1.2 subset for scalars, so `json.dumps` produces
    # a fully-escaped double-quoted YAML scalar — safe for tasks with
    # colons, hashes, quotes, or any other YAML metacharacter.
    # Raw f-string interpolation would emit `task: ship: feature` for
    # a task of `ship: feature`, which PyYAML rejects.
    task_yaml = json.dumps(task)
    memory_paths = _memory_surface_paths(project_root)
    if memory_paths:
        files_lines = [
            "  files:  # pre-filled memory surfaces; add task-specific {path, lines?, role?} entries"
        ]
        for surface_path in memory_paths:
            files_lines.append(f"    - path: {json.dumps(surface_path)}")
            files_lines.append("      role: memory")
        files_block = "\n".join(files_lines) + "\n"
    else:
        files_block = "  files: []  # TODO: at least one {path, lines?, role?} entry\n"
    return (
        f"manifest_id: hm-{uuid7()}\n"
        f"version: 1\n"
        f"task: {task_yaml}\n"
        f"context:\n"
        f"{files_block}"
        f"expected_deliverables: []  # TODO: at least one {{path, must_export?, coverage?, schema?}}\n"
        f"constraints:\n"
        f"  model_tier: TODO  # opus | sonnet | haiku\n"
        f"  window_budget: 0  # TODO: positive integer token budget\n"
        f"acceptance:\n"
        f"  - TODO: one acceptance criterion per line\n"
    )
