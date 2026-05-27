"""Tests for the curated Role specifications shipped in metaensemble/roles/.

Every Role spec must validate against `metaensemble/schemas/role.schema.json`. This
test loads the frontmatter from each `.md` file in `metaensemble/roles/` and runs
it through the same validator the runtime uses.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from metaensemble.lib.manifest import validate_role_frontmatter


ROLES_DIR = Path(__file__).resolve().parent.parent / "roles"


def _split_frontmatter(text: str) -> dict:
    """Parse the YAML frontmatter block from a markdown file."""
    if not text.startswith("---\n"):
        raise ValueError("missing frontmatter header")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise ValueError("unterminated frontmatter block")
    yaml_text = text[4:end]
    return yaml.safe_load(yaml_text)


def _role_files() -> list[Path]:
    return sorted(p for p in ROLES_DIR.glob("*.md") if p.name != "README.md")


@pytest.mark.parametrize("role_path", _role_files(), ids=lambda p: p.stem)
def test_role_frontmatter_validates(role_path: Path):
    """Every Role spec's frontmatter must conform to role.schema.json."""
    text = role_path.read_text()
    frontmatter = _split_frontmatter(text)
    validate_role_frontmatter(frontmatter)


def test_at_least_five_curated_roles_ship():
    """v0.1.0 ships at least five curated Roles in Core (architect, backend,
    frontend, code-quality, test-engineer at minimum)."""
    role_files = _role_files()
    assert len(role_files) >= 5, f"expected >=5 curated Roles, found {len(role_files)}"
    names = {p.stem for p in role_files}
    expected_minimum = {"architect", "backend", "frontend", "code-quality", "test-engineer"}
    missing = expected_minimum - names
    assert not missing, f"missing curated Roles: {missing}"


def test_role_alias_prefixes_are_unique():
    """Two Roles should not share an alias prefix; collisions break Executor naming."""
    seen: dict[str, str] = {}
    for path in _role_files():
        frontmatter = _split_frontmatter(path.read_text())
        prefix = frontmatter.get("alias_prefix") or frontmatter["name"][:4]
        assert prefix not in seen, (
            f"alias prefix `{prefix}` collides between {seen[prefix]} and {path.stem}"
        )
        seen[prefix] = path.stem
