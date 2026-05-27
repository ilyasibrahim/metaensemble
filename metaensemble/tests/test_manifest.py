"""Tests for Manifest, Brief, and Role schema validation (metaensemble/lib/manifest.py)."""
from __future__ import annotations


import pytest
from jsonschema.exceptions import ValidationError

from metaensemble.lib.ids import uuid7
from metaensemble.lib.manifest import (
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
