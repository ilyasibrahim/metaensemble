"""Project-surface overlap ownership.

MetaEnsemble can automatically record some project facts that teams often
maintain manually in Markdown files: deliverable indexes, work registries,
status ledgers, and similar documentation. The inspect step writes those
overlaps into `.metaensemble/install-decisions.yaml`; runtime code consumes
that file as data instead of hard-coding any one filename.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


ACTION_METAENSEMBLE_OWNED = "metaensemble_owned"
ACTION_PROJECT_OWNED = "project_owned"
ACTION_DUAL = "dual"
WRITE_POLICY_BLOCK_WHEN_METAENSEMBLE_OWNED = "block_when_metaensemble_owned"


@dataclass(frozen=True)
class OverlapSurface:
    category: str
    project_surface: str
    metaensemble_surface: str
    action: str
    write_policy: str = WRITE_POLICY_BLOCK_WHEN_METAENSEMBLE_OWNED
    rationale: str = ""


def _decisions_path(project_root: Path) -> Path:
    return project_root / ".metaensemble" / "install-decisions.yaml"


def _surface_paths(entry: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    single = entry.get("project_surface")
    if isinstance(single, str) and single.strip():
        paths.append(single.strip())

    many = entry.get("project_surfaces")
    if isinstance(many, list):
        for item in many:
            if isinstance(item, str) and item.strip():
                paths.append(item.strip())
            elif isinstance(item, dict):
                raw = item.get("path") or item.get("project_surface")
                if isinstance(raw, str) and raw.strip():
                    paths.append(raw.strip())

    out: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def load_overlap_surfaces(project_root: Path) -> tuple[OverlapSurface, ...]:
    """Read overlap ownership records from a project's decisions file."""
    path = _decisions_path(project_root)
    if not path.is_file():
        return ()
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return ()
    overlaps = data.get("overlaps") or {}
    if not isinstance(overlaps, dict):
        return ()

    surfaces: list[OverlapSurface] = []
    for category, entry in overlaps.items():
        if not isinstance(entry, dict):
            continue
        action = str(entry.get("action", "")).strip()
        metaensemble_surface = str(entry.get("metaensemble_surface", "")).strip()
        write_policy = str(
            entry.get("write_policy") or WRITE_POLICY_BLOCK_WHEN_METAENSEMBLE_OWNED
        ).strip()
        rationale = str(entry.get("rationale", "")).strip()
        for project_surface in _surface_paths(entry):
            surfaces.append(
                OverlapSurface(
                    category=str(category).strip(),
                    project_surface=project_surface,
                    metaensemble_surface=metaensemble_surface,
                    action=action,
                    write_policy=write_policy,
                    rationale=rationale,
                )
            )
    return tuple(surfaces)


def _resolve_surface_path(project_root: Path, surface: str) -> Path:
    path = Path(surface).expanduser()
    if not path.is_absolute():
        path = project_root / path
    return path.resolve(strict=False)


def protected_overlap_for_path(
    project_root: Path,
    path: Path,
) -> OverlapSurface | None:
    """Return the metaensemble-owned overlap surface matching `path`, if any."""
    root = project_root.resolve(strict=False)
    target = path.resolve(strict=False)
    for surface in load_overlap_surfaces(root):
        if surface.action != ACTION_METAENSEMBLE_OWNED:
            continue
        if surface.write_policy != WRITE_POLICY_BLOCK_WHEN_METAENSEMBLE_OWNED:
            continue
        if target == _resolve_surface_path(root, surface.project_surface):
            return surface
    return None
