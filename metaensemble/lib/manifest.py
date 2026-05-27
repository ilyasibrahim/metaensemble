"""Manifest, Brief, and Role schema validation for MetaEnsemble.

Manifests are YAML on disk; Briefs and Role frontmatter are JSON-shaped
even when written in YAML. All three are validated against the schemas in
`metaensemble/schemas/` using a cached Draft 2020-12 validator (PERFORMANCE.md R3).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator


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
