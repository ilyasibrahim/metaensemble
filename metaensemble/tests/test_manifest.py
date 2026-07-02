"""Tests for Manifest, Brief, and Role schema validation (metaensemble/lib/manifest.py)."""
from __future__ import annotations


import pytest
import yaml
from jsonschema.exceptions import ValidationError

from metaensemble.lib.ids import uuid7
from metaensemble.lib.manifest import (
    scaffold_manifest,
    validate_brief,
    validate_manifest,
    validate_role_frontmatter,
)


def _u() -> str:
    return str(uuid7())


def _hm() -> str:
    return f"hm-{_u()}"


# --- Manifest --------------------------------------------------------------


def test_valid_manifest_passes():
    manifest = {
        "manifest_id": _hm(),
        "version": 1,
        "from": "arch-7b3",
        "to": "be-9c1",
        "task": "implement-auth",
        "context": {"files": [{"path": "src/auth.py", "lines": "1-100"}]},
        "expected_deliverables": [{"path": "src/handlers.py"}],
        "constraints": {"model_tier": "sonnet", "window_budget": 8000},
    }
    validate_manifest(manifest)


def test_manifest_missing_required_field_raises():
    manifest = {"manifest_id": _hm()}
    with pytest.raises(ValidationError):
        validate_manifest(manifest)


def test_manifest_with_peer_review_block():
    manifest = {
        "manifest_id": _hm(),
        "version": 1,
        "task": "deploy",
        "context": {"files": [{"path": "out"}]},
        "expected_deliverables": [{"path": "out"}],
        "constraints": {"model_tier": "sonnet", "window_budget": 1000},
        "peer_review": {
            "mandatory_for_reversibility": True,
            "min_reviewers": 2,
            "dissent_handling": "surface_minority",
        },
    }
    validate_manifest(manifest)


def test_manifest_rejects_invalid_dissent_handling():
    manifest = {
        "manifest_id": _hm(),
        "version": 1,
        "task": "deploy",
        "context": {"files": [{"path": "out"}]},
        "expected_deliverables": [{"path": "out"}],
        "constraints": {"model_tier": "sonnet", "window_budget": 1000},
        "peer_review": {"dissent_handling": "average_them"},
    }
    with pytest.raises(ValidationError):
        validate_manifest(manifest)


def test_manifest_rejects_extra_property():
    manifest = {
        "manifest_id": _hm(),
        "version": 1,
        "task": "deploy",
        "context": {"files": [{"path": "out"}]},
        "expected_deliverables": [{"path": "out"}],
        "constraints": {"model_tier": "sonnet", "window_budget": 1000},
        "extraneous_field": "should fail",
    }
    with pytest.raises(ValidationError):
        validate_manifest(manifest)


def test_manifest_extras_block_is_accepted():
    """The `extras` top-level field accepts open-shape reference material."""
    manifest = {
        "manifest_id": _hm(),
        "version": 1,
        "task": "css-token-migration",
        "context": {"files": [{"path": "src/style.css", "lines": "1-50"}]},
        "expected_deliverables": [{"path": "src/style.css"}],
        "constraints": {"model_tier": "sonnet", "window_budget": 8000},
        "extras": {
            "rationale": "Centralize brand colors as the single source of truth.",
            "source_color_corrections": {
                "--sprakbanken: #f59e0b": "--source-sprakbanken: #B45309",
            },
            "kpis": [
                {"id": "total-records", "label": "Total records collected"},
            ],
        },
    }
    validate_manifest(manifest)


def test_manifest_extras_rejects_non_object():
    """Extras must be an object, not a string or list."""
    manifest = {
        "manifest_id": _hm(),
        "version": 1,
        "task": "deploy",
        "context": {"files": [{"path": "out"}]},
        "expected_deliverables": [{"path": "out"}],
        "constraints": {"model_tier": "sonnet", "window_budget": 1000},
        "extras": "this should be an object, not a string",
    }
    with pytest.raises(ValidationError):
        validate_manifest(manifest)


# --- Brief -----------------------------------------------------------------


def test_valid_brief_passes():
    brief = {
        "v": 1,
        "brief_id": _u(),
        "from": "arch-7b3",
        "to": "be-9c1",
        "task_id": "task-1",
        "tier": "sonnet",
    }
    validate_brief(brief)


def test_brief_wrong_version_raises():
    brief = {
        "v": 2,
        "brief_id": _u(),
        "from": "arch-7b3",
        "to": "be-9c1",
        "task_id": "task-1",
        "tier": "sonnet",
    }
    with pytest.raises(ValidationError):
        validate_brief(brief)


def test_brief_invalid_alias_format_raises():
    brief = {
        "v": 1,
        "brief_id": _u(),
        "from": "arch_no_hyphen",
        "to": "be-9c1",
        "task_id": "task-1",
        "tier": "sonnet",
    }
    with pytest.raises(ValidationError):
        validate_brief(brief)


# --- Role ------------------------------------------------------------------


def test_valid_role_frontmatter():
    role = {
        "name": "backend",
        "version": "1.0.0",
        "description": "Backend implementation specialist for API endpoints.",
        "model_tier": "sonnet",
    }
    validate_role_frontmatter(role)


def test_role_with_onboarding_block():
    role = {
        "name": "security",
        "version": "1.0.0",
        "description": "Security review specialist for code and configuration.",
        "model_tier": "sonnet",
        "onboarding": {
            "read_first": ["reports/arch/system.md"],
            "coordinate_with": ["backend", "devops"],
            "conventions": ["metaensemble/conventions/security-review.md"],
            "mentor_role": "code-quality",
        },
    }
    validate_role_frontmatter(role)


def test_role_invalid_model_tier_raises():
    role = {
        "name": "backend",
        "version": "1.0.0",
        "description": "Backend implementation specialist for API endpoints.",
        "model_tier": "frontier",
    }
    with pytest.raises(ValidationError):
        validate_role_frontmatter(role)


def test_role_invalid_version_format_raises():
    role = {
        "name": "backend",
        "version": "v1",
        "description": "Backend implementation specialist for API endpoints.",
        "model_tier": "sonnet",
    }
    with pytest.raises(ValidationError):
        validate_role_frontmatter(role)


# --- Manifest schema invariants -----------------------------------------


def test_manifest_schema_requires_minimum_one_context_file():
    """`context.files: []` must fail validation.

    The Manifest is the typed handoff contract; a dispatch with zero file
    pointers is almost always a bug because the receiving Executor would have
    no anchor to work from. Forcing one file at the schema level catches it
    before the PreToolUse hook fires.
    """
    manifest = {
        "manifest_id": _hm(),
        "version": 1,
        "task": "implement-auth",
        "context": {"files": []},
        "expected_deliverables": [{"path": "src/handlers.py"}],
        "constraints": {"model_tier": "sonnet", "window_budget": 8000},
    }
    with pytest.raises(ValidationError) as exc:
        validate_manifest(manifest)
    assert "files" in str(exc.value) or "[]" in str(exc.value), (
        f"error should reference the offending field/value: {exc.value}"
    )


def test_manifest_schema_accepts_memory_role_in_context_files():
    """`context.files[].role` is a free-form string; `memory` marks entries
    that point at the runtime's own memory files (CLAUDE.md and friends)."""
    manifest = {
        "manifest_id": _hm(),
        "version": 1,
        "task": "implement-auth",
        "context": {"files": [{"path": "CLAUDE.md", "role": "memory"}]},
        "expected_deliverables": [{"path": "src/handlers.py"}],
        "constraints": {"model_tier": "sonnet", "window_budget": 8000},
    }
    validate_manifest(manifest)


def test_manifest_schema_accepts_delegates_to():
    """v3.1 schema patch: `delegates_to` is part of the top-level set.
    The Coordinator uses it to dispatch sibling sub-Roles in parallel
    (M-form pattern). Schema validation must accept the block with a
    non-empty `delegates_to`."""
    manifest = {
        "manifest_id": _hm(),
        "version": 1,
        "task": "implement-auth",
        "context": {"files": [{"path": "src/auth.py"}]},
        "expected_deliverables": [{"path": "src/handlers.py"}],
        "constraints": {"model_tier": "sonnet", "window_budget": 8000},
        "delegates_to": [
            {"role": "backend", "purpose": "REST handler implementation",
             "budget_pct_of_head": 60},
            {"role": "test-engineer", "purpose": "Integration tests",
             "budget_pct_of_head": 40},
        ],
    }
    validate_manifest(manifest)


# --- Manifest scaffold ------------------------------------------------------


def test_scaffold_prefills_memory_surfaces_from_install_decisions(tmp_path):
    """An adopted project's recorded memory surfaces become `role: memory`
    context entries, in the recorded order."""
    project = tmp_path / "proj"
    (project / ".metaensemble").mkdir(parents=True)
    (project / ".metaensemble" / "install-decisions.yaml").write_text(
        "memory_surfaces:\n"
        '  - path: "CLAUDE.md"\n'
        "    scope: project\n"
        '  - path: ".claude/CLAUDE.md"\n'
        "    scope: project\n"
    )

    data = yaml.safe_load(scaffold_manifest("ship-feature", project=project))

    assert data["context"]["files"] == [
        {"path": "CLAUDE.md", "role": "memory"},
        {"path": ".claude/CLAUDE.md", "role": "memory"},
    ]
    assert data["task"] == "ship-feature"


def test_scaffold_prefills_claude_md_when_never_adopted(tmp_path):
    """Without install-decisions.yaml, a bare `CLAUDE.md` at the project
    root is enough to pre-fill the memory context entry."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "CLAUDE.md").write_text("# project memory\n")

    data = yaml.safe_load(scaffold_manifest("ship-feature", project=project))

    assert data["context"]["files"] == [{"path": "CLAUDE.md", "role": "memory"}]


def test_scaffold_context_files_stay_todo_without_memory_surfaces(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()

    data = yaml.safe_load(scaffold_manifest("ship-feature", project=project))

    assert data["context"]["files"] == []


def test_scaffold_respects_explicitly_empty_memory_surfaces(tmp_path):
    """A decisions file that records zero surfaces is authoritative — the
    scaffold must not second-guess it with filesystem probes."""
    project = tmp_path / "proj"
    (project / ".metaensemble").mkdir(parents=True)
    (project / ".metaensemble" / "install-decisions.yaml").write_text(
        "memory_surfaces:\n  []\n"
    )
    (project / "CLAUDE.md").write_text("# project memory\n")

    data = yaml.safe_load(scaffold_manifest("ship-feature", project=project))

    assert data["context"]["files"] == []


def test_scaffold_with_memory_prefill_fails_only_on_intended_todos(tmp_path):
    """The memory pre-fill satisfies `context.files` minItems; the scaffold
    must still fail validation on its TODO fields, and pass once those —
    and only those — are filled in."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "CLAUDE.md").write_text("# project memory\n")

    data = yaml.safe_load(scaffold_manifest("ship-feature", project=project))
    with pytest.raises(ValidationError):
        validate_manifest(data)

    # Fill the TODOs, leaving the pre-filled memory context untouched.
    data["expected_deliverables"] = [{"path": "src/feature.py"}]
    data["constraints"] = {"model_tier": "sonnet", "window_budget": 4000}
    data["acceptance"] = ["tests pass", "no regressions"]
    validate_manifest(data)


def test_scaffold_handles_task_with_yaml_metacharacters(tmp_path):
    """The task scalar must round-trip through yaml.safe_load verbatim even
    with colons, hashes, and quotes."""
    project = tmp_path / "proj"
    project.mkdir()
    weird = "ship: feature # with hash & quote 'x'"

    data = yaml.safe_load(scaffold_manifest(weird, project=project))

    assert data["task"] == weird
