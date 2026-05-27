#!/usr/bin/env python3
"""Reverse-convert MetaEnsemble Role files back into Claude Code agent files.

Used to recover from top-level installs where the installer's
backup directory is missing (the documented `<project>/.metaensemble/
backups/<ts>/agents/` was not created — most often because the install
predates the current installer, or was a manual setup that bypassed
`apply_install()`).

The Role frontmatter is a strict superset of the agent frontmatter, and
the body is preserved verbatim through `convert_agent_to_role`. So the
reverse mapping is mechanical and lossless for everything except the
description-padding the forward converter adds when an agent's
description is fewer than ten characters. We strip the canonical
"(imported from Claude Code agent)" suffix when we see it so descriptions
match the original where possible.

Reverse mapping:
  name        -> name
  description -> description (suffix stripped if it matches the canonical pad)
  allowed_tools -> tools (joined as comma-separated string, the agent convention)
  model_tier  -> model

Dropped fields (MetaEnsemble-only):
  version, alias_prefix, output_styles, onboarding

Usage:
  scripts/role-to-agent.py <role-file> <agent-dir>
  scripts/role-to-agent.py --all <roles-dir> <agent-dir>

This script is a one-shot recovery tool. It is intentionally separate
from the CLI surface because the same recovery should not happen
silently as part of `uninstall`; it is the user's deliberate decision
to leave the MetaEnsemble paradigm.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# yaml is in the metaensemble venv; the script assumes it is available.
import yaml  # noqa: E402


CANONICAL_PAD = " (imported from Claude Code agent)"


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise ValueError("file lacks YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise ValueError("frontmatter not terminated")
    fm_text = text[4:end]
    body = text[end + 5:]
    loaded = yaml.safe_load(fm_text) or {}
    if not isinstance(loaded, dict):
        raise ValueError("frontmatter did not parse as a mapping")
    return loaded, body


def role_to_agent_text(role_text: str) -> str:
    """Render the agent-format equivalent of a Role file's content."""
    fm, body = _parse_frontmatter(role_text)

    name = str(fm.get("name", "imported")).strip()
    description = str(fm.get("description", "")).strip()
    if description.endswith(CANONICAL_PAD):
        description = description[: -len(CANONICAL_PAD)].strip()

    tools_list = fm.get("allowed_tools") or fm.get("tools") or []
    if isinstance(tools_list, list):
        tools_value = ", ".join(str(t).strip() for t in tools_list if str(t).strip())
    else:
        tools_value = str(tools_list).strip()

    model = str(fm.get("model_tier") or fm.get("model") or "").strip()

    # Build the agent-format frontmatter. We emit it manually rather than via
    # yaml.dump so the field order matches Claude Code's conventional layout
    # (name, description, tools, model, color) and so unquoted descriptions
    # with colons survive the round trip without forced quoting.
    lines = ["---"]
    lines.append(f"name: {name}")
    if description:
        # Description may contain a colon; emit single-quoted to be safe and
        # idiomatic for the agent format. Single quotes around the literal
        # are the most forgiving for human reading.
        escaped = description.replace("'", "''")
        lines.append(f"description: '{escaped}'")
    if tools_value:
        lines.append(f"tools: {tools_value}")
    if model:
        lines.append(f"model: {model}")
    lines.append("---")
    # Body retains its original leading newline / structure verbatim.
    if not body.startswith("\n"):
        return "\n".join(lines) + "\n" + body
    return "\n".join(lines) + body


def convert_one(role_path: Path, agent_dir: Path) -> Path:
    role_text = role_path.read_text()
    agent_text = role_to_agent_text(role_text)
    agent_dir.mkdir(parents=True, exist_ok=True)
    target = agent_dir / role_path.name
    target.write_text(agent_text)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reverse-convert MetaEnsemble Role files into Claude Code agents.",
    )
    parser.add_argument("--all", action="store_true",
                        help="Convert every *.md in <source> (otherwise <source> must be one file)")
    parser.add_argument("source", help="Role file or roles directory")
    parser.add_argument("target", help="Target agents/ directory")
    args = parser.parse_args(argv)

    source = Path(args.source).resolve()
    target_dir = Path(args.target).resolve()

    if args.all:
        if not source.is_dir():
            parser.error(f"--all expects a directory; got {source}")
        files = sorted(source.glob("*.md"))
        if not files:
            print(f"No .md files under {source}", file=sys.stderr)
            return 1
        for f in files:
            written = convert_one(f, target_dir)
            print(f"{f}  ->  {written}")
        return 0

    if not source.is_file():
        parser.error(f"source not found: {source}")
    written = convert_one(source, target_dir)
    print(f"{source}  ->  {written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
