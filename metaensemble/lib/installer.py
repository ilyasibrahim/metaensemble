"""MetaEnsemble installer.

Two phases, four operations:
1. `inspect`: read-only inventory of the user's and project's existing
   Claude Code setup, returned as a structured object and written as a
   Markdown report.
2. `plan_install`: pure function that turns an inspection + layout choice into a
   list of actions the installer would take.
3. `apply_install`: applies user-scope or project-scope actions.
4. `uninstall`: reverses user-scope or project-scope actions.

Reversibility is the contract: every change `apply_install` makes is
reversed by the corresponding `unadopt` or `user-teardown` path. Project
backups live in `<project>/.metaensemble/backups/<timestamp>/`; user
backups live in `~/.metaensemble/installs/<timestamp>/`.

The installer's responsibility is mechanical: detect, convert,
symlink, configure hooks, and back up the originals. It does not
semantically rewrite user content; it copies and maps fields.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


# --- Constants ------------------------------------------------------------

RUNTIME_DIR_NAME = ".claude"  # the agent runtime's config directory name

# Inside a runtime config dir, these subdirectories carry user content.
AGENTS_SUBDIR = "agents"
COMMANDS_SUBDIR = "commands"
SKILLS_SUBDIR = "skills"
OUTPUT_STYLES_SUBDIR = "output-styles"
SETTINGS_FILE = "settings.json"
INSPECTION_SNAPSHOT_KEEP = 5

# Memory files the runtime auto-loads for a project, relative to the
# project root, in the runtime's load order. Recorded at inspection time
# so downstream surfaces (Manifest scaffolding, dispatch context) point
# at the runtime's own memory rather than rebuilding a parallel store.
PROJECT_MEMORY_SURFACES = ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md")

CORE_DIR = Path(__file__).resolve().parent.parent  # metaensemble/


class Layout(str, Enum):
    NAMESPACED = "namespaced"
    TOP_LEVEL = "top-level"


def _normalize_layout_value(value: str) -> str:
    """Normalize serialized layout values to one of the canonical Layout values.

    Legacy `parallel`/`incorporate` keys are migrated on-disk by
    `migrate_vocabulary_state()` before any layout-aware read path runs, so
    this function no longer tolerates them. If a caller somehow encounters
    a legacy value here, the migration has failed silently — return the raw
    value and let the Layout enum constructor raise a clear error upstream.
    """
    return value.strip().lower().replace("_", "-")


# --- Vocabulary migration (v0.1.0) -----------------------------------------

import re as _re  # noqa: E402


def _rewrite_legacy_values_in_obj(obj: Any) -> bool:
    """Rewrite the install plan's top-level `mode` field to `layout`.

    Scoped narrowly: this rewrites ONLY the top-level `mode` key on the
    plan JSON object. Recursive rewriting would corrupt action payloads
    that happen to use `mode` for unrelated purposes (e.g. a future
    chmod-like action with `{"mode": "0644"}`). Install plans carry the
    layout choice as a single top-level field; that is the only field
    this migrator touches.

    Returns True if any change was made.
    """
    if not isinstance(obj, dict):
        return False
    changed = False
    # Top-level "mode" -> "layout" rename. If both keys exist, drop the
    # legacy one without overwriting the canonical value.
    if "mode" in obj:
        legacy_value = obj.pop("mode")
        if "layout" not in obj:
            obj["layout"] = legacy_value
        changed = True
    # Normalize the top-level "layout" value when it carries a legacy enum.
    value = obj.get("layout")
    if isinstance(value, str):
        if value == "parallel":
            obj["layout"] = Layout.NAMESPACED.value
            changed = True
        elif value == "incorporate":
            obj["layout"] = Layout.TOP_LEVEL.value
            changed = True
    return changed


def _migrate_plan_json(path: Path) -> dict | None:
    """Rewrite legacy `mode`/`parallel`/`incorporate` in a plan.json file.

    Returns an action record dict if the file was changed, None if the file
    was already canonical (idempotent no-op).
    """
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not _rewrite_legacy_values_in_obj(data):
        return None
    try:
        path.write_text(json.dumps(data, indent=2, default=str) + "\n")
    except OSError:
        return None
    return {"kind": "rewrite-json", "path": str(path)}


def _migrate_decisions_yaml(path: Path) -> dict | None:
    """Rewrite legacy `suggested_mode`/`parallel`/`incorporate` in a YAML file.

    Comment-preserving: operates on the raw text via line-anchored regex so
    the user's edits and the file's commentary survive. Returns an action
    record dict if the file was changed, None otherwise.
    """
    try:
        original = path.read_text()
    except OSError:
        return None
    new_text = original
    # Key rename: `suggested_mode:` -> `suggested_layout:`. Top-level only.
    new_text = _re.sub(
        r"(?m)^(\s*)suggested_mode(\s*:)", r"\1suggested_layout\2", new_text
    )
    # Value rewrite on the same line as a layout-ish key.
    def _value_sub(match: "_re.Match[str]") -> str:
        prefix, value = match.group(1), match.group(2).strip()
        if value in ("parallel", '"parallel"', "'parallel'"):
            return f"{prefix}{Layout.NAMESPACED.value}"
        if value in ("incorporate", '"incorporate"', "'incorporate'"):
            return f"{prefix}{Layout.TOP_LEVEL.value}"
        return match.group(0)
    new_text = _re.sub(
        r"(?m)^(\s*(?:suggested_layout|layout)\s*:\s*)(\S+)",
        _value_sub,
        new_text,
    )
    # Cosmetic comment rewrite ("# Recommended mode: X" -> "# Recommended layout: Y").
    def _comment_sub(match: "_re.Match[str]") -> str:
        value = match.group(2).strip()
        if value == "parallel":
            return f"{match.group(1)}{Layout.NAMESPACED.value}"
        if value == "incorporate":
            return f"{match.group(1)}{Layout.TOP_LEVEL.value}"
        return match.group(0)
    new_text = _re.sub(
        r"(?m)^(\s*#\s*Recommended\s+)mode(\s*:\s*)(\S+)",
        lambda m: f"{m.group(1)}layout{m.group(2)}" + (
            Layout.NAMESPACED.value if m.group(3).strip() == "parallel"
            else Layout.TOP_LEVEL.value if m.group(3).strip() == "incorporate"
            else m.group(3)
        ),
        new_text,
    )
    if new_text == original:
        return None
    try:
        path.write_text(new_text)
    except OSError:
        return None
    return {"kind": "rewrite-yaml", "path": str(path)}


def _log_vocabulary_migration(me_dir: Path, actions: list[dict]) -> None:
    """Append migration actions to <project>/.metaensemble/hooks/log.jsonl."""
    if not actions:
        return
    hooks_dir = me_dir / "hooks"
    try:
        hooks_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    log_path = hooks_dir / "log.jsonl"
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        with log_path.open("a") as fh:
            for action in actions:
                record = {
                    "kind": "vocabulary-migration",
                    "ts": ts,
                    "action_kind": action.get("kind"),
                    **{k: v for k, v in action.items() if k != "kind"},
                }
                fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


_MIGRATION_DONE: set[str] = set()


def migrate_vocabulary_state(home: Path | None = None) -> list[dict]:
    """Migrate legacy `parallel`/`incorporate` vocabulary in on-disk state.

    Idempotent: running twice produces zero actions the second time. Reads
    the projects registry (`discover_projects`) as the source of truth for
    which project directories to scan — never filesystem-walks looking for
    `.metaensemble/` directories (that would visit stale registrations).

    Returns the list of action records performed, primarily for tests and
    logging. Each action is a dict with keys `kind` and `path`, and for
    file renames `from` and `to`.

    Process-cached: the second call within the same Python process is a
    no-op so CLI commands that share a process don't re-walk the FS.
    """
    home = home or Path.home()
    cache_key = str(home)
    if cache_key in _MIGRATION_DONE:
        return []

    actions: list[dict] = []

    # 1. User-level install records under ~/.metaensemble/installs/<ts>/plan.json.
    installs_root = home / ".metaensemble" / "installs"
    if installs_root.is_dir():
        try:
            install_dirs = sorted(p for p in installs_root.iterdir() if p.is_dir())
        except OSError:
            install_dirs = []
        for install_dir in install_dirs:
            plan = install_dir / "plan.json"
            if plan.is_file():
                action = _migrate_plan_json(plan)
                if action:
                    actions.append(action)

    # 2. Per-project state, scoped to the projects registry.
    try:
        projects = discover_projects(home)
    except Exception:  # nosec B110 — migration must not break on registry errors
        projects = []
    for proj in projects:
        if not proj.has_metaensemble_dir:
            continue
        if not proj.path.exists():
            continue
        me_dir = proj.path / ".metaensemble"
        project_actions: list[dict] = []

        # 2a. Rename the pre-rename decisions file to install-decisions.yaml
        # when only the legacy name is present. If both exist, leave the
        # legacy file alone (user already migrated; we don't want to destroy
        # their work).
        legacy_yaml = me_dir / "survey-decisions.yaml"  # vocab-migration: legacy-name
        new_yaml = me_dir / "install-decisions.yaml"
        if legacy_yaml.is_file() and not new_yaml.exists():
            try:
                legacy_yaml.rename(new_yaml)
                project_actions.append({
                    "kind": "rename",
                    "from": str(legacy_yaml),
                    "to": str(new_yaml),
                    "path": str(new_yaml),
                })
            except OSError:
                pass

        # 2b. Rewrite keys/values in install-decisions.yaml.
        if new_yaml.is_file():
            action = _migrate_decisions_yaml(new_yaml)
            if action:
                project_actions.append(action)

        # 2c. Rewrite plan.json files under <project>/.metaensemble/backups/.
        backups_root = me_dir / "backups"
        if backups_root.is_dir():
            try:
                backup_dirs = sorted(p for p in backups_root.iterdir() if p.is_dir())
            except OSError:
                backup_dirs = []
            for backup_dir in backup_dirs:
                plan = backup_dir / "plan.json"
                if plan.is_file():
                    action = _migrate_plan_json(plan)
                    if action:
                        project_actions.append(action)

        if project_actions:
            _log_vocabulary_migration(me_dir, project_actions)
            actions.extend(project_actions)

    _MIGRATION_DONE.add(cache_key)
    return actions


# --- Data classes ---------------------------------------------------------


@dataclass(frozen=True)
class DiscoveredArtifact:
    """A user-authored file the inspection found in the runtime config."""

    kind: str           # "agent" | "command" | "skill" | "output-style"
    name: str           # the basename without extension, e.g. "backend"
    path: Path          # absolute path to the file
    layer: str          # "user" | "project"


@dataclass(frozen=True)
class Collision:
    """A user-authored artifact whose name matches a MetaEnsemble-shipped one."""

    discovered: DiscoveredArtifact
    metaensemble_counterpart: str  # the matching MetaEnsemble item


@dataclass(frozen=True)
class RoleRelevance:
    """Per-Role assessment of whether a curated Role looks relevant for this project."""

    role_id: str
    relevant: bool
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AgentComparison:
    """Per-agent comparison of a user agent vs. its MetaEnsemble curated counterpart.

    Used to drive the per-agent choice surface the inspection exposes:
    the user reads this and decides whether to keep their version, take
    MetaEnsemble's, or keep both side-by-side under different names.
    """

    name: str
    user_path: Path
    user_layer: str                       # "user" | "project"
    curated_path: Path                    # path inside metaensemble/roles/
    user_tools: list[str] = field(default_factory=list)
    curated_tools: list[str] = field(default_factory=list)
    user_model: str = ""
    curated_model_tier: str = ""
    user_body_size: int = 0               # rough length, for "richer" hint
    curated_body_size: int = 0
    user_description: str = ""
    curated_description: str = ""


@dataclass(frozen=True)
class AgentDecision:
    """The user's choice for one agent surfaced by the inspection.

    `kind` values:
      - `collision`        — the agent name exists in both the user's setup
                             and MetaEnsemble's curated set. The user must
                             choose between three handling modes.
      - `user_unique`      — only in the user's setup. Default: preserve.
      - `curated_relevant` — only in MetaEnsemble; project signals suggest
                             it would be useful. Default: activate.
      - `curated_optional` — only in MetaEnsemble; no project signals
                             suggest it is needed. Default: retire.

    `action` values, scoped by kind:
      - For `collision`:
          - "keep_yours"  — the user's agent stays; MetaEnsemble's Role is not installed.
          - "take_ours"   — the user's agent is backed up and replaced with MetaEnsemble's Role.
          - "keep_both"   — the user's agent stays at its original path; MetaEnsemble's
                            Role installs under a namespaced suffix (e.g. `<name>-me`).
      - For `user_unique`: "preserve" (default) or "convert".
      - For `curated_relevant`: "activate" (default) or "retire".
      - For `curated_optional`: "retire" (default) or "activate".

    `recommendation` is a short human sentence the inspection renders so the
    user understands why this default was chosen.
    """

    name: str
    kind: str
    action: str
    recommendation: str = ""
    comparison: AgentComparison | None = None
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OverlapDecision:
    """A project-native surface that overlaps with a MetaEnsemble surface."""

    category: str
    project_surface: str
    metaensemble_surface: str
    action: str
    recommendation: str
    rationale: str
    write_policy: str = "block_when_metaensemble_owned"
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MemorySurface:
    """A memory file the runtime already loads for this project.

    Recorded at inspection time so downstream consumers (Manifest
    scaffolding, dispatch context) can route work through the runtime's
    own memory files. MetaEnsemble only points at these surfaces; it
    never writes them.
    """

    path: str            # relative to the project root, POSIX-style
    scope: str = "project"


@dataclass(frozen=True)
class SurveyDecisions:
    """The full per-agent / per-curated-Role decision set the user can edit.

    Persisted as `<project>/.metaensemble/install-decisions.yaml` so the user
    edits it once and the installer reads it on every subsequent install.
    """

    agents: list[AgentDecision] = field(default_factory=list)
    timestamp: str = ""
    suggested_layout: str = Layout.NAMESPACED.value
    layout_rationale: str = ""
    report_root: str = ".metaensemble/reports"
    overlaps: list[OverlapDecision] = field(default_factory=list)
    memory_surfaces: list[MemorySurface] = field(default_factory=list)


@dataclass(frozen=True)
class SurveyResult:
    """Output of the read-only inspection phase."""

    discovered: list[DiscoveredArtifact] = field(default_factory=list)
    collisions: list[Collision] = field(default_factory=list)
    role_relevance: list[RoleRelevance] = field(default_factory=list)
    decisions: SurveyDecisions | None = None
    user_runtime_exists: bool = False
    project_runtime_exists: bool = False
    report_path: Path | None = None
    decisions_path: Path | None = None


@dataclass(frozen=True)
class Action:
    """A single change the installer plans to make."""

    kind: str           # "symlink" | "convert-agent" | "copy" | "backup" | "merge-settings" | "skip" | "render-launcher"
    source: Path | None
    target: Path | None
    description: str
    backup_path: Path | None = None
    skip_if_exists: bool = False  # for per-file symlinks; refuse-to-overwrite semantics


_USER_SCOPE_ACTION_KINDS = frozenset({
    "symlink",          # ~/.claude/commands/, skills/, output-styles/
    "vendor-runtime",   # ~/.metaensemble/runtime/ (atomic: assets + runner)
    "merge-settings",   # ~/.claude/settings.json
})

_PROJECT_SCOPE_ACTION_KINDS = frozenset({
    "convert-agent",         # triggered by per-project install-decisions.yaml
    "install-curated-role",  # triggered by keep_both collision decisions
    "copy", "backup",        # project-relative backups produced by the above
})


def is_user_scope_action(action: "Action") -> bool:
    """True for actions that belong to user-level integration (layout-shaped).

    Used by the CLI to split a single install plan into the subset that
    `metaensemble user-setup` should apply versus the subset that
    `metaensemble adopt` should apply. The classification is by action
    kind, not by target path, because some actions write to user-level
    locations (`~/.metaensemble/roles/`) while being triggered by
    per-project decisions — those count as project-scope work.
    """
    return action.kind in _USER_SCOPE_ACTION_KINDS


def is_project_scope_action(action: "Action") -> bool:
    """True for actions tied to a specific project's install decisions."""
    return action.kind in _PROJECT_SCOPE_ACTION_KINDS


@dataclass(frozen=True)
class InstallPlan:
    """The full set of actions for a given install layout."""

    layout: Layout
    actions: list[Action] = field(default_factory=list)

    def user_actions(self) -> list["Action"]:
        """Subset applied by `metaensemble user-setup`."""
        return [a for a in self.actions if is_user_scope_action(a)]

    def project_actions(self) -> list["Action"]:
        """Subset applied by `metaensemble adopt`."""
        return [a for a in self.actions if is_project_scope_action(a)]
    timestamp: str = ""
    active_roles: list[str] = field(default_factory=list)
    inactive_roles: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class InstallReport:
    """What `apply_install` actually did.

    `applied` and `skipped` cover the actions that ran or were refused.
    `noop` lists actions whose desired post-state already held — re-running
    `install` after a successful first run lands every Action in `noop`,
    so the CLI can report `Unchanged.` rather than "Applied N action(s)"
    plus a fresh backup directory.
    """

    applied: list[Action] = field(default_factory=list)
    skipped: list[Action] = field(default_factory=list)
    noop: list[Action] = field(default_factory=list)
    errors: list[tuple[Action, str]] = field(default_factory=list)
    backup_root: Path | None = None


# --- Path helpers --------------------------------------------------------


def _user_runtime_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / RUNTIME_DIR_NAME


def _project_runtime_dir(project: Path) -> Path:
    return project / RUNTIME_DIR_NAME


def _project_metaensemble_dir(project: Path) -> Path:
    return project / ".metaensemble"


def _user_metaensemble_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".metaensemble"


def _backup_root(project: Path, timestamp: str) -> Path:
    return _project_metaensemble_dir(project) / "backups" / timestamp


def _runtime_root(home: Path | None = None) -> Path:
    """Path of the `~/.metaensemble/runtime` symlink.

    Resolves through the symlink to `~/.metaensemble/runtime-versions/<id>/`
    at OS level. Stable across vendor refreshes — symlinks under
    `~/.claude/` point here, not at any specific versioned dir.
    """
    return _user_metaensemble_dir(home) / "runtime"


def _runtime_versions_dir(home: Path | None = None) -> Path:
    """Container for atomic-replaceable runtime versions."""
    return _user_metaensemble_dir(home) / "runtime-versions"


def _runner_path(home: Path | None = None) -> Path:
    """Path of the runner script through the runtime symlink.

    Used by settings.json hook commands. Stable across vendor refreshes
    because it resolves through `~/.metaensemble/runtime`, which is
    atomically swapped to the new versioned dir on each user-setup.
    """
    return _runtime_root(home) / "bin" / "me-run"


def _user_backup_root(home: Path | None, timestamp: str) -> Path:
    """Backup root for user-scope installs.

    User-setup writes its plan manifest under
    `~/.metaensemble/installs/<timestamp>/` so that `user-teardown` can
    walk it later to reverse the integration. Mirrors the project-level
    `<project>/.metaensemble/backups/<timestamp>/` layout, but lives
    where the user-level state already does.
    """
    return _user_metaensemble_dir(home) / "installs" / timestamp


def _archive_slug(path: Path) -> str:
    """Filesystem-safe slug for user-level project archives."""
    return "".join(c if c.isalnum() or c in "._-" else "-" for c in str(path))


def remap_user_scope_backup_paths(
    plan: InstallPlan,
    *,
    project: Path | None = None,
    home: Path | None = None,
) -> InstallPlan:
    """Return a user-scope plan whose backup paths match real apply.

    `plan_install` computes backup paths against the project-level backup
    root because the full plan may include project actions. `user-setup`
    applies only user-scope actions, whose backups live under
    `~/.metaensemble/installs/<timestamp>/`. Dry-run rendering and real
    apply must use the same remapping or the preview lies about where
    `settings.json` will be backed up.
    """
    project = project or Path.cwd()
    target_backup_root = _user_backup_root(home, plan.timestamp)
    project_root = _backup_root(project, plan.timestamp)
    remapped: list[Action] = []
    for action in plan.actions:
        if action.backup_path:
            try:
                rel = action.backup_path.relative_to(project_root)
            except ValueError:
                pass
            else:
                action = Action(
                    kind=action.kind,
                    source=action.source,
                    target=action.target,
                    description=action.description,
                    backup_path=target_backup_root / rel,
                    skip_if_exists=action.skip_if_exists,
                )
        remapped.append(action)
    return InstallPlan(
        layout=plan.layout,
        actions=remapped,
        active_roles=plan.active_roles,
        inactive_roles=plan.inactive_roles,
        timestamp=plan.timestamp,
    )


def _rotate_inspection_snapshots(report_dir: Path, *, keep: int = INSPECTION_SNAPSHOT_KEEP) -> None:
    """Keep inspection artifacts bounded without touching the editable decisions file."""
    for pattern in ("inspection-*.md", "install-decisions.*.yaml"):
        snapshots = sorted(report_dir.glob(pattern), key=lambda p: p.name)
        for old in snapshots[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass


def _archive_project_inspection_artifacts(
    project: Path,
    *,
    home: Path | None = None,
) -> Path | None:
    """Copy inspection artifacts before a project purge removes `.metaensemble/`."""
    source_dir = _project_metaensemble_dir(project)
    if not source_dir.is_dir():
        return None
    artifacts = sorted(
        p for p in source_dir.iterdir()
        if p.is_file()
        and (
            p.name.startswith("inspection-")
            or p.name == "install-decisions.yaml"
            # Preserve pre-contract artifacts during upgrade purges.
            or p.name.startswith("survey-")  # vocab-migration: legacy-name
            or p.name == "survey-decisions.yaml"  # vocab-migration: legacy-name
        )
    )
    if not artifacts:
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_dir = (
        _user_metaensemble_dir(home)
        / "archives"
        / "project-inspections"
        / _archive_slug(project.resolve(strict=False))
        / timestamp
    )
    archive_dir.mkdir(parents=True, exist_ok=True)
    for artifact in artifacts:
        try:
            shutil.copy2(artifact, archive_dir / artifact.name)
        except OSError:
            pass
    return archive_dir


# --- Survey (read-only) ---------------------------------------------------


def _is_metaensemble_managed(path: Path) -> bool:
    """True when `path` is a symlink whose target lives inside the MetaEnsemble repo.

    Used by inspection to skip MetaEnsemble's own installed symlinks
    (top-level slash commands under `~/.claude/commands/`, the
    metaensemble-protocol skill, output styles) so they are not
    double-counted as user artifacts. Without this filter, inspection
    after `user-setup --layout=top-level` reports every symlink as a
    user command colliding with MetaEnsemble's curated set, inflating
    the collision count by ~10 and confusing the Principal.

    `path` may point at a file (slash command symlink, output style
    symlink) or a SKILL.md inside a symlinked skill directory.
    """
    try:
        # Walk up the chain looking for any symlinked ancestor — a
        # SKILL.md inside `~/.claude/skills/metaensemble-protocol/` is
        # not itself a symlink, but its parent directory is.
        check = path
        while True:
            if check.is_symlink():
                try:
                    target = check.resolve(strict=False)
                except (OSError, RuntimeError):
                    return False
                # Recognized as managed when the symlink resolves to:
                # - inside the installed package directory (CORE_DIR), OR
                # - inside the user-level vendored runtime
                #   (`~/.metaensemble/runtime-versions/...` — the
                #    final resolution of `~/.metaensemble/runtime`).
                # The runtime-versions check covers vendored-runtime installs
                # where symlinks point through ~/.metaensemble/runtime/.
                if _is_inside(target, CORE_DIR):
                    return True
                # Recognize vendored runtime targets by the
                # signature `.metaensemble/runtime-versions/<id>/...` in
                # the resolved path. Using a path-component check rather
                # than computing _runtime_versions_dir() lets this work
                # with monkeypatched test homes where Path.home() is
                # rewired.
                parts = target.parts
                for i in range(len(parts) - 1):
                    if parts[i] == ".metaensemble" and parts[i + 1] == "runtime-versions":
                        return True
                return False
            parent = check.parent
            if parent == check:
                return False
            check = parent
    except OSError:
        return False


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _scan_layer(root: Path, layer: str) -> list[DiscoveredArtifact]:
    """Walk one runtime-config root and emit DiscoveredArtifacts.

    Skips entries that resolve into the MetaEnsemble repo via symlink —
    those are MetaEnsemble's own installed pieces, not user artifacts,
    and including them inflates collision counts after user-setup.
    """
    out: list[DiscoveredArtifact] = []
    if not root.exists() or not root.is_dir():
        return out

    agents = root / AGENTS_SUBDIR
    if agents.is_dir():
        for path in sorted(agents.glob("*.md")):
            if _is_metaensemble_managed(path):
                continue
            out.append(DiscoveredArtifact(
                kind="agent", name=path.stem, path=path, layer=layer,
            ))

    commands = root / COMMANDS_SUBDIR
    if commands.is_dir():
        for path in sorted(commands.glob("*.md")):
            if _is_metaensemble_managed(path):
                continue
            out.append(DiscoveredArtifact(
                kind="command", name=path.stem, path=path, layer=layer,
            ))

    skills = root / SKILLS_SUBDIR
    if skills.is_dir():
        for skill_dir in sorted(skills.iterdir()):
            skill_md = skill_dir / "SKILL.md"
            if skill_dir.is_dir() and skill_md.exists():
                if _is_metaensemble_managed(skill_md):
                    continue
                out.append(DiscoveredArtifact(
                    kind="skill", name=skill_dir.name,
                    path=skill_md, layer=layer,
                ))

    output_styles = root / OUTPUT_STYLES_SUBDIR
    if output_styles.is_dir():
        for path in sorted(output_styles.glob("*.md")):
            if _is_metaensemble_managed(path):
                continue
            out.append(DiscoveredArtifact(
                kind="output-style", name=path.stem, path=path, layer=layer,
            ))

    return out


_DUPLICATE_SUFFIX_PATTERN = _re.compile(r" \d+$")


def _is_canonical_curated_name(stem: str) -> bool:
    """Reject filesystem duplicates like `architect 2`, `backend 3`, etc.

    macOS Finder and some pip install paths leave behind " N"-suffixed
    copies of installed files; these are not real curated Roles or
    commands. The catalog must ignore them so the inspect renderer does
    not surface them as legitimate options to the Principal.
    """
    if stem == "README":
        return False
    return _DUPLICATE_SUFFIX_PATTERN.search(stem) is None


def _metaensemble_curated_names() -> dict[str, set[str]]:
    """The names MetaEnsemble ships, grouped by kind, used for collision detection."""
    out: dict[str, set[str]] = {
        "agent": set(),  # the curated Roles are MetaEnsemble's "agents"
        "command": set(),
        "skill": set(),
        "output-style": set(),
    }
    roles_dir = CORE_DIR / "roles"
    if roles_dir.is_dir():
        out["agent"] = {
            p.stem for p in roles_dir.glob("*.md")
            if _is_canonical_curated_name(p.stem)
        }
    commands_dir = CORE_DIR / "commands"
    if commands_dir.is_dir():
        out["command"] = {
            p.stem for p in commands_dir.glob("*.md")
            if _is_canonical_curated_name(p.stem)
        }
    skills_dir = CORE_DIR / "skills"
    if skills_dir.is_dir():
        out["skill"] = {
            p.name for p in skills_dir.iterdir()
            if p.is_dir() and _is_canonical_curated_name(p.name)
        }
    styles_dir = CORE_DIR / "output-styles"
    if styles_dir.is_dir():
        out["output-style"] = {
            p.stem for p in styles_dir.glob("*.md")
            if _is_canonical_curated_name(p.stem)
        }
    return out


def survey(
    home: Path | None = None,
    project: Path | None = None,
    write_report: bool = True,
) -> SurveyResult:
    """Inspect the user's and project's runtime configs. No changes made.

    Args:
        home: override for the user's home directory (testing).
        project: project root; defaults to cwd.
        write_report: when True, writes a Markdown report under
            `<project>/.metaensemble/inspection-<timestamp>.md` AND writes a
            companion `<project>/.metaensemble/install-decisions.yaml`
            populated with sensible per-agent defaults. The user edits
            the decisions file before running install; that is the
            single user-choice surface the installer reads.
    """
    project = project or Path.cwd()
    user_root = _user_runtime_dir(home)
    project_root = _project_runtime_dir(project)

    discovered = _scan_layer(user_root, "user") + _scan_layer(project_root, "project")
    curated = _metaensemble_curated_names()

    collisions: list[Collision] = []
    for artifact in discovered:
        kind_set = curated.get(artifact.kind, set())
        if artifact.name in kind_set:
            collisions.append(Collision(
                discovered=artifact, metaensemble_counterpart=artifact.name,
            ))

    role_relevance = detect_role_relevance(project)

    interim_result = SurveyResult(
        discovered=discovered,
        collisions=collisions,
        role_relevance=role_relevance,
        user_runtime_exists=user_root.exists(),
        project_runtime_exists=project_root.exists(),
    )
    decisions = build_default_decisions(interim_result, home=home, project=project)

    report_path: Path | None = None
    decisions_path: Path | None = None
    if write_report:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        report_dir = _project_metaensemble_dir(project)
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"inspection-{timestamp}.md"
        report_path.write_text(_render_survey_v2(
            discovered=discovered,
            decisions=decisions,
            user_exists=user_root.exists(),
            project_exists=project_root.exists(),
        ))
        # The decisions file is the user's editable surface. If a prior
        # decisions file already exists we do not overwrite it — the user
        # may have already made choices — but we DO write a sibling
        # `install-decisions.<timestamp>.yaml` so they can diff and adopt.
        decisions_path = report_dir / "install-decisions.yaml"
        if not decisions_path.exists():
            decisions_path.write_text(_decisions_to_yaml_doc(decisions))
        else:
            (report_dir / f"install-decisions.{timestamp}.yaml").write_text(
                _decisions_to_yaml_doc(decisions)
            )
        _rotate_inspection_snapshots(report_dir)

    return SurveyResult(
        discovered=discovered,
        collisions=collisions,
        role_relevance=role_relevance,
        decisions=decisions,
        user_runtime_exists=user_root.exists(),
        project_runtime_exists=project_root.exists(),
        report_path=report_path,
        decisions_path=decisions_path,
    )


# --- Project-signal detection for curated Role relevance ----------------


def detect_role_relevance(project_root: Path) -> list[RoleRelevance]:
    """Walk the project for signals each curated Role would care about.

    Pure file-system inspection: directory existence and glob matching.
    No model calls, no semantic analysis. The output is a list of
    RoleRelevance entries, one per curated Role, populated with the
    evidence found.
    """
    def dir_exists(name: str) -> bool:
        return (project_root / name).is_dir()

    def file_exists(name: str) -> bool:
        return (project_root / name).is_file()

    def has_any(pattern: str) -> bool:
        try:
            return any(True for _ in project_root.glob(pattern))
        except (OSError, ValueError):
            return False

    def path_contains(*needles: str) -> bool:
        lowered = tuple(n.lower() for n in needles)
        try:
            for path in project_root.rglob("*"):
                rel = str(path.relative_to(project_root)).lower()
                if any(n in rel for n in lowered):
                    return True
        except (OSError, ValueError):
            return False
        return False

    def file_contains(name: str, *needles: str) -> bool:
        """True if `<project>/<name>` exists and contains any needle.

        Cost-bounded: reads at most the first 4 KB of the file. Used to
        check whether a config file references a specific tool/framework
        (e.g. `pyproject.toml` contains `[tool.mypy]`).
        """
        candidate = project_root / name
        if not candidate.is_file():
            return False
        try:
            text = candidate.read_text(errors="replace")[:4096]
        except OSError:
            return False
        return any(n in text for n in needles)

    code_evidence: list[str] = []
    for pattern in (
        "**/*.py", "**/*.js", "**/*.ts", "**/*.tsx", "**/*.jsx",
        "**/*.go", "**/*.rs", "**/*.java",
    ):
        if has_any(pattern):
            code_evidence.append(f"code files matching `{pattern}`")
            break

    # `architect`: ADR/architecture documentation, not generic code presence.
    architect_evidence: list[str] = []
    for f in ("docs/ARCHITECTURE.md", "ARCHITECTURE.md"):
        if file_exists(f):
            architect_evidence.append(f"`{f}`")
    for d in ("docs/adr", "docs/adrs", "docs/decisions", "adr", "adrs"):
        if dir_exists(d):
            architect_evidence.append(f"`{d}/` directory")
    for pattern in ("ADR-*.md", "docs/ADR-*.md", "RFC-*.md", "docs/RFC-*.md"):
        if has_any(pattern):
            architect_evidence.append(f"files matching `{pattern}`")
            break

    backend_evidence: list[str] = []
    # Backend is project-shape sensitive: a generic `pyproject.toml` does
    # not imply backend work. The minima are a backend-named directory OR
    # an explicit web framework declaration OR Django's `manage.py`.
    for d in ("backend", "api", "server", "services", "routes"):
        if dir_exists(d):
            backend_evidence.append(f"`{d}/` directory")
    if file_exists("manage.py"):
        backend_evidence.append("`manage.py` (Django)")
    if file_contains(
        "requirements.txt",
        "flask", "fastapi", "django", "starlette", "uvicorn",
    ) or file_contains(
        "pyproject.toml",
        "flask", "fastapi", "django", "starlette", "uvicorn",
    ):
        backend_evidence.append("Python web framework declared in requirements")
    if file_contains("package.json", "express", "koa", "nestjs", "fastify"):
        backend_evidence.append("Node web framework declared in package.json")

    frontend_evidence: list[str] = []
    for d in (
        "frontend", "web", "client", "ui",
        "src/components", "src/pages",
        "public",
    ):
        if dir_exists(d):
            frontend_evidence.append(f"`{d}/` directory")
    for pattern in ("**/*.tsx", "**/*.jsx", "**/*.vue", "**/*.svelte"):
        if has_any(pattern):
            frontend_evidence.append(f"frontend-framework files (`{pattern}`)")
    if file_contains(
        "package.json",
        "react", "vue", "svelte", "angular", "next", "nuxt", "remix",
    ):
        frontend_evidence.append("frontend framework declared in package.json")
    for f in (
        "tailwind.config.js", "tailwind.config.ts",
        "vite.config.js", "vite.config.ts",
        "webpack.config.js",
    ):
        if file_exists(f):
            frontend_evidence.append(f"`{f}`")
    if has_any("**/*.html") and not has_any("docs/**/*.html"):
        frontend_evidence.append("HTML files")

    # `code-quality`: linter / type-checker / pre-commit signals. Distinct
    # from generic `code_evidence` so a project without configured tooling
    # does not falsely activate code-quality.
    code_quality_evidence: list[str] = []
    for f in (
        ".pre-commit-config.yaml", ".pre-commit-config.yml",
        ".flake8", ".ruff.toml", ".eslintrc", ".eslintrc.json",
        ".eslintrc.js", "eslint.config.js", "eslint.config.mjs",
        "tslint.json",
    ):
        if file_exists(f):
            code_quality_evidence.append(f"`{f}`")
    for d in (".mypy_cache", ".ruff_cache"):
        if dir_exists(d):
            code_quality_evidence.append(f"`{d}/` directory")
    if file_contains(
        "pyproject.toml",
        "[tool.mypy]", "[tool.ruff]", "[tool.black]", "[tool.pyright]",
    ):
        code_quality_evidence.append("`pyproject.toml` declares linter/type-checker config")
    if file_contains("setup.cfg", "[mypy]", "[flake8]"):
        code_quality_evidence.append("`setup.cfg` declares linter config")

    test_evidence: list[str] = []
    for d in ("tests", "test", "__tests__", "spec", ".pytest_cache"):
        if dir_exists(d):
            test_evidence.append(f"`{d}/` directory")
    for pattern in ("**/test_*.py", "**/*_test.py", "**/*_test.go", "**/*.test.ts", "**/*.spec.ts"):
        if has_any(pattern):
            test_evidence.append(f"test files (`{pattern}`)")
            break
    for f in (
        "pytest.ini", "jest.config.js", "jest.config.ts",
        "vitest.config.js", "vitest.config.ts",
        "playwright.config.js", "playwright.config.ts",
        "cypress.config.js", "cypress.config.ts",
    ):
        if file_exists(f):
            test_evidence.append(f"`{f}`")
    if file_contains("pyproject.toml", "[tool.pytest", "[tool.coverage"):
        test_evidence.append("`pyproject.toml` declares pytest/coverage config")

    devops_evidence: list[str] = []
    for d in (
        ".github/workflows", ".circleci",
        "infra", "terraform", "helm", "k8s", "kubernetes",
        "ansible", "docker",
    ):
        if dir_exists(d):
            devops_evidence.append(f"`{d}/` directory")
    for f in (
        ".gitlab-ci.yml", "Dockerfile", "docker-compose.yml",
        "Jenkinsfile",
    ):
        if file_exists(f):
            devops_evidence.append(f"`{f}`")
    if has_any("**/*.tf"):
        devops_evidence.append("Terraform files (`*.tf`)")
    if file_exists("Makefile") and file_contains(
        "Makefile", "deploy", "build", "release", "docker"
    ):
        devops_evidence.append("`Makefile` with deploy/build targets")

    docs_evidence: list[str] = []
    for d in ("docs", "documentation"):
        if dir_exists(d):
            docs_evidence.append(f"`{d}/` directory")
    for f in ("mkdocs.yml", "sphinx/conf.py", "docs/conf.py"):
        if file_exists(f):
            docs_evidence.append(f"`{f}`")
    docs_dir = project_root / "docs"
    if docs_dir.is_dir():
        try:
            md_in_docs = len(list(docs_dir.rglob("*.md")))
            if md_in_docs >= 3:
                docs_evidence.append(f"{md_in_docs} markdown files under `docs/`")
        except OSError:
            pass
    md_root = list(project_root.glob("*.md"))
    if len(md_root) >= 3:
        docs_evidence.append(f"{len(md_root)} markdown files at project root")
    readme = project_root / "README.md"
    if readme.is_file():
        try:
            non_blank = sum(
                1 for ln in readme.read_text(errors="replace").splitlines()
                if ln.strip()
            )
            if non_blank > 200:
                docs_evidence.append(f"`README.md` with {non_blank} non-blank lines")
        except OSError:
            pass

    data_evidence: list[str] = []
    for d in (
        "data", "data/raw", "data/staging", "data/processed",
        "data/silver", "data/gold",
        "datasets", "dataset", "corpus", "corpora", "features",
        "migrations", "airflow",
    ):
        if dir_exists(d):
            data_evidence.append(f"`{d}/` directory")
    for f in ("dbt_project.yml", "dagster.yaml", "Snakefile"):
        if file_exists(f):
            data_evidence.append(f"`{f}`")
    for pattern in ("**/*.csv", "**/*.parquet", "**/*.jsonl", "**/*.ndjson"):
        if has_any(pattern):
            data_evidence.append(f"data files (`{pattern}`)")
            break

    ml_evidence: list[str] = []
    project_name = project_root.name.lower()
    for token in ("classifier", "model", "training", "ml", "machine-learning"):
        if token in project_name:
            ml_evidence.append(f"project name contains `{token}`")
            break
    # `models/` collides with dbt projects (where it holds SQL files, not ML
    # model artifacts). When `dbt_project.yml` is present, route `models/`
    # to data-engineer only and skip it here.
    is_dbt = file_exists("dbt_project.yml")
    ml_dirs = (
        ("notebooks", "experiments", "checkpoints", "mlruns", "mlflow", "wandb")
        if is_dbt
        else ("models", "model", "notebooks", "experiments", "checkpoints",
              "mlruns", "mlflow", "wandb")
    )
    for d in ml_dirs:
        if dir_exists(d):
            ml_evidence.append(f"`{d}/` directory")
    for f in ("train.py", "training.py", "evaluate.py"):
        if file_exists(f):
            ml_evidence.append(f"`{f}`")
    for pattern in (
        "**/*train*.py", "**/*model*.py", "**/*classifier*.py",
        "**/*.ipynb",
    ):
        if has_any(pattern):
            ml_evidence.append(f"ML-adjacent files (`{pattern}`)")
            break
    if file_contains(
        "requirements.txt",
        "torch", "tensorflow", "transformers", "scikit-learn", "xgboost",
    ) or file_contains(
        "pyproject.toml",
        "torch", "tensorflow", "transformers", "scikit-learn", "xgboost",
    ):
        ml_evidence.append("ML framework declared in requirements")
    # No data->ML cascade. Having a data pipeline does not imply ML work;
    # the data-engineer Role exists to own pipelines that are not ML.

    return [
        RoleRelevance(
            role_id="architect",
            relevant=bool(architect_evidence),
            evidence=architect_evidence or ["no architecture/ADR documentation detected"],
        ),
        RoleRelevance(
            role_id="backend",
            relevant=bool(backend_evidence),
            evidence=backend_evidence or ["no backend signals detected"],
        ),
        RoleRelevance(
            role_id="frontend",
            relevant=bool(frontend_evidence),
            evidence=frontend_evidence or ["no frontend signals detected"],
        ),
        RoleRelevance(
            role_id="code-quality",
            relevant=bool(code_quality_evidence),
            evidence=code_quality_evidence or ["no linter/type-checker config detected"],
        ),
        RoleRelevance(
            role_id="test-engineer",
            relevant=bool(test_evidence),
            evidence=test_evidence or ["no test directories or files detected"],
        ),
        RoleRelevance(
            role_id="devops",
            relevant=bool(devops_evidence),
            evidence=devops_evidence or ["no CI/CD or infra signals detected"],
        ),
        RoleRelevance(
            role_id="docs",
            relevant=bool(docs_evidence),
            evidence=docs_evidence or ["minimal documentation surface detected"],
        ),
        RoleRelevance(
            role_id="data-engineer",
            relevant=bool(data_evidence),
            evidence=data_evidence or ["no data pipeline signals detected"],
        ),
        RoleRelevance(
            role_id="ml-engineer",
            relevant=bool(ml_evidence),
            evidence=ml_evidence or ["no ML/modeling signals detected"],
        ),
    ]


def _role_signal_probes() -> dict[str, list[str]]:
    """The catalog of probe descriptions per curated Role.

    Used by the inspect renderer to surface, for every Role, the signals
    the detector probed — independent of which ones fired. This is the
    transparency layer for inspect reports: even when a Role does not
    activate, the Principal can read what would have activated it.

    Keep these strings in sync with the rules in `detect_role_relevance`.
    Order is deterministic so report output is stable across runs.
    """
    return {
        "architect": [
            "`docs/ARCHITECTURE.md` or `ARCHITECTURE.md` exists",
            "`docs/adr/`, `docs/adrs/`, `docs/decisions/`, `adr/`, `adrs/` directory exists",
            "files matching `ADR-*.md` or `RFC-*.md` exist in the project root or `docs/`",
        ],
        "backend": [
            "`backend/`, `api/`, `server/`, `services/`, `routes/` directory exists",
            "`manage.py` (Django) exists",
            "`requirements.txt` or `pyproject.toml` declares a Python web framework "
            "(flask/fastapi/django/starlette/uvicorn)",
            "`package.json` declares a Node web framework (express/koa/nestjs/fastify)",
        ],
        "frontend": [
            "`frontend/`, `web/`, `client/`, `ui/`, `src/components/`, `src/pages/`, "
            "`public/` directory exists",
            "any `*.tsx`/`*.jsx`/`*.vue`/`*.svelte` file exists",
            "`package.json` declares a frontend framework "
            "(react/vue/svelte/angular/next/nuxt/remix)",
            "`tailwind.config.*`, `vite.config.*`, or `webpack.config.js` exists",
            "any `*.html` file exists outside `docs/`",
        ],
        "code-quality": [
            "`.pre-commit-config.yaml`, `.flake8`, `.ruff.toml`, `.eslintrc*`, "
            "`eslint.config.*`, or `tslint.json` exists",
            "`.mypy_cache/` or `.ruff_cache/` directory exists",
            "`pyproject.toml` declares `[tool.mypy]`, `[tool.ruff]`, `[tool.black]`, "
            "or `[tool.pyright]`",
            "`setup.cfg` declares `[mypy]` or `[flake8]`",
        ],
        "test-engineer": [
            "`tests/`, `test/`, `__tests__/`, `spec/`, or `.pytest_cache/` directory exists",
            "any `test_*.py`/`*_test.py`/`*.test.ts`/`*.spec.ts` file exists",
            "`pytest.ini`, `jest.config.*`, `vitest.config.*`, `playwright.config.*`, "
            "or `cypress.config.*` exists",
            "`pyproject.toml` declares `[tool.pytest.ini_options]` or `[tool.coverage`",
        ],
        "devops": [
            "`.github/workflows/`, `.circleci/`, `terraform/`, `helm/`, `k8s/`, "
            "`kubernetes/`, `ansible/`, `docker/`, or `infra/` directory exists",
            "`Dockerfile`, `docker-compose.yml`, `.gitlab-ci.yml`, or `Jenkinsfile` exists",
            "any `*.tf` (Terraform) file exists",
            "`Makefile` contains deploy/build/release/docker targets",
        ],
        "docs": [
            "`docs/` or `documentation/` directory exists",
            "`mkdocs.yml`, `sphinx/conf.py`, or `docs/conf.py` exists",
            "`docs/` contains at least 3 markdown files",
            "project root has at least 3 markdown files",
            "`README.md` has more than 200 non-blank lines",
        ],
        "data-engineer": [
            "`data/`, `data/raw/`, `data/staging/`, `data/processed/`, `data/silver/`, "
            "`data/gold/`, `migrations/`, or `airflow/` directory exists",
            "`dbt_project.yml`, `dagster.yaml`, or `Snakefile` exists",
            "any `*.csv`/`*.parquet`/`*.jsonl`/`*.ndjson` data file exists",
        ],
        "ml-engineer": [
            "project name contains `classifier`, `model`, `training`, `ml`, "
            "or `machine-learning`",
            "`models/`, `notebooks/`, `experiments/`, `checkpoints/`, `mlruns/`, "
            "`mlflow/`, or `wandb/` directory exists (note: `models/` routes to "
            "data-engineer when `dbt_project.yml` is present)",
            "`train.py`, `training.py`, or `evaluate.py` exists",
            "any `*train*.py`, `*model*.py`, `*classifier*.py`, or `*.ipynb` file exists",
            "`requirements.txt` or `pyproject.toml` declares an ML framework "
            "(torch/tensorflow/transformers/scikit-learn/xgboost)",
        ],
    }


def _work_record_documentation_candidates(project_root: Path) -> list[Path]:
    """Return manual deliverable/work-record docs in deterministic order."""
    direct = [
        project_root / ".claude" / "reports" / "_registry.md",
        project_root / ".claude" / "_registry.md",
        project_root / "reports" / "_registry.md",
        project_root / "docs" / "_registry.md",
        project_root / "_registry.md",
    ]
    found: list[Path] = []
    for path in direct:
        if path.is_file() and path not in found:
            found.append(path)
    try:
        for path in sorted(project_root.rglob("_registry.md")):
            if ".metaensemble" in path.parts:
                continue
            if path.is_file() and path not in found:
                found.append(path)
    except (OSError, ValueError):
        pass
    return found


def detect_overlaps(project_root: Path) -> list[OverlapDecision]:
    """Detect project-native surfaces that overlap with MetaEnsemble surfaces."""
    overlaps: list[OverlapDecision] = []
    registry_candidates = _work_record_documentation_candidates(project_root)
    if registry_candidates:
        registry = registry_candidates[0]
        try:
            rel = registry.relative_to(project_root).as_posix()
        except ValueError:
            rel = str(registry)
        overlaps.append(OverlapDecision(
            category="deliverable_records",
            project_surface=rel,
            metaensemble_surface="Ledger runs + deliverable_ref_json + deliverables index",
            action="project_owned",
            recommendation="metaensemble_owned",
            rationale=(
                "The Ledger automatically records structural fields (report path, "
                "date, status, model, tokens, deliverable_ref) for deliverables and "
                "Runs at zero model-token cost. Manual project work-record docs add "
                "a curated narrative summary that the Ledger cannot generate without "
                "model tokens. Choose `project_owned` only if that curated narrative "
                "is load-bearing for this project's reading culture; otherwise "
                "`metaensemble_owned` saves documentation-maintenance tokens on every "
                "dispatch."
            ),
            write_policy="block_when_metaensemble_owned",
            evidence=[f"found work-record documentation file `{rel}`"],
        ))
    return overlaps


def detect_report_root(project_root: Path) -> str:
    """Return the report root MetaEnsemble should use for this project.

    Existing projects can have a load-bearing convention such as
    `.claude/reports/_registry.md`; keep that convention. Greenfield projects
    with no detected work-record surface use MetaEnsemble's private ignored
    area so reports do not pollute the committable tree.
    """
    registry_candidates = _work_record_documentation_candidates(project_root)
    if registry_candidates:
        registry = registry_candidates[0]
        try:
            return registry.parent.relative_to(project_root).as_posix()
        except ValueError:
            return str(registry.parent)
    return ".metaensemble/reports"


def detect_memory_surfaces(project_root: Path) -> list[MemorySurface]:
    """Detect the memory files the runtime already loads for this project.

    Pure existence checks against the runtime's documented memory
    locations (`PROJECT_MEMORY_SURFACES`), in load order. The result is
    recorded in `install-decisions.yaml` so Manifest scaffolding and
    dispatch context consume the runtime's memory surfaces directly.
    Absent files are simply not listed, and detection is regenerative
    rather than accumulative, so re-running adopt never duplicates
    entries.
    """
    return [
        MemorySurface(path=rel, scope="project")
        for rel in PROJECT_MEMORY_SURFACES
        if (project_root / rel).is_file()
    ]


# --- Agent comparison + decisions ----------------------------------------


def _read_agent_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    """Read frontmatter + body, falling back to lenient parsing.

    Re-implements the lenient parse used by `_parse_frontmatter` so this
    helper does not depend on the agent-conversion code path.
    """
    text = path.read_text(errors="replace")
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    fm_text = text[4:end]
    body = text[end + 5:]
    try:
        loaded = yaml.safe_load(fm_text)
        if isinstance(loaded, dict):
            return loaded, body
    except yaml.YAMLError:
        pass
    fm: dict[str, Any] = {}
    for line in fm_text.split("\n"):
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        if key.strip():
            fm[key.strip()] = value.strip()
    return fm, body


def _agent_tools(fm: dict[str, Any]) -> list[str]:
    raw = fm.get("tools") or fm.get("allowed_tools") or ""
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    return [t.strip() for t in str(raw).split(",") if t.strip()]


def _agent_model(fm: dict[str, Any]) -> str:
    return str(fm.get("model") or fm.get("model_tier") or "").strip()


def compare_agent_to_role(user_path: Path, curated_path: Path, layer: str) -> AgentComparison:
    """Build an AgentComparison for one user agent vs. one curated Role.

    Both files are read once; field shapes are normalized so the inspection
    can render a side-by-side table without re-parsing.
    """
    user_fm, user_body = _read_agent_frontmatter(user_path)
    curated_fm, curated_body = _read_agent_frontmatter(curated_path)
    return AgentComparison(
        name=user_path.stem,
        user_path=user_path,
        user_layer=layer,
        curated_path=curated_path,
        user_tools=_agent_tools(user_fm),
        curated_tools=_agent_tools(curated_fm),
        user_model=_agent_model(user_fm),
        curated_model_tier=_agent_model(curated_fm),
        user_body_size=len(user_body),
        curated_body_size=len(curated_body),
        user_description=str(user_fm.get("description", "")).strip(),
        curated_description=str(curated_fm.get("description", "")).strip(),
    )


def _recommend_collision(cmp: AgentComparison) -> tuple[str, str]:
    """Choose a default action + recommendation sentence for a collision.

    Heuristic priority:
      1. If the user's body is meaningfully larger than MetaEnsemble's,
         their version is probably richer and we recommend `keep_yours`.
      2. If the user's frontmatter is missing model or tools but
         MetaEnsemble's has them, recommend `take_ours`.
      3. Otherwise default to `keep_yours` — the conservative choice that
         never silently overwrites the user's careful prompt engineering.
    """
    if cmp.user_body_size >= cmp.curated_body_size + 200:
        return (
            "keep_yours",
            f"Your `{cmp.name}` agent appears richer ({cmp.user_body_size} chars "
            f"vs. {cmp.curated_body_size}). Keeping it is the safe default.",
        )
    if not cmp.user_tools and cmp.curated_tools:
        return (
            "take_ours",
            f"Your `{cmp.name}` agent declares no tools; MetaEnsemble's Role "
            f"declares {len(cmp.curated_tools)}. Taking ours adds typed defaults.",
        )
    if not cmp.user_model and cmp.curated_model_tier:
        return (
            "take_ours",
            f"Your `{cmp.name}` agent has no `model:` set; MetaEnsemble's Role "
            f"specifies `{cmp.curated_model_tier}`. Taking ours fixes that.",
        )
    return (
        "keep_yours",
        f"Your `{cmp.name}` agent and MetaEnsemble's Role are roughly equivalent. "
        "Keeping yours is the safe default; switch to `take_ours` if you prefer.",
    )


def build_default_decisions(
    survey_result: "SurveyResult",
    *,
    home: Path | None = None,
    project: Path | None = None,
) -> SurveyDecisions:
    """Compose a default SurveyDecisions from a freshly completed inspection.

    The function is opinionated by design: it produces a recommendation
    per agent and per curated Role so the user can read once, edit only
    what they disagree with, and run install.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    curated_dir = CORE_DIR / "roles"
    curated_names = _metaensemble_curated_names()["agent"]
    user_agents_by_name: dict[str, DiscoveredArtifact] = {
        a.name: a for a in survey_result.discovered if a.kind == "agent"
    }
    relevance_by_id: dict[str, RoleRelevance] = {
        r.role_id: r for r in survey_result.role_relevance
    }

    decisions: list[AgentDecision] = []

    # 1. Collisions — name exists in both the user's setup and curated.
    for name in sorted(set(user_agents_by_name) & curated_names):
        user_art = user_agents_by_name[name]
        curated_path = curated_dir / f"{name}.md"
        cmp = compare_agent_to_role(user_art.path, curated_path, user_art.layer)
        action, recommendation = _recommend_collision(cmp)
        # Carry the same signal evidence the detector produced so the
        # rendered report can show *why* a collision Role looks relevant
        # to this project — not just that the names happen to match.
        relevance = relevance_by_id.get(name)
        evidence = list(relevance.evidence) if relevance and relevance.relevant else []
        decisions.append(AgentDecision(
            name=name, kind="collision", action=action,
            recommendation=recommendation, comparison=cmp,
            evidence=evidence,
        ))

    # 2. User-unique — preserved by default, never converted unless asked.
    for name in sorted(set(user_agents_by_name) - curated_names):
        user_art = user_agents_by_name[name]
        decisions.append(AgentDecision(
            name=name, kind="user_unique", action="preserve",
            recommendation=(
                f"`{name}` is your own agent and does not exist in MetaEnsemble's "
                "curated set. Preserved as-is by default."
            ),
            comparison=None,
        ))

    # 3 + 4. Curated-only — bucketed by project relevance.
    for name in sorted(curated_names - {n.lower() for n in user_agents_by_name}):
        relevance = relevance_by_id.get(name)
        is_relevant = bool(relevance and relevance.relevant)
        evidence = list(relevance.evidence) if relevance else []
        if is_relevant:
            decisions.append(AgentDecision(
                name=name, kind="curated_relevant", action="activate",
                recommendation=(
                    f"`{name}` matches signals in your project — "
                    f"{evidence[0] if evidence else 'general code'}. Activating by default."
                ),
                evidence=evidence,
            ))
        else:
            ev_msg = evidence[0] if evidence else "no signal detected"
            decisions.append(AgentDecision(
                name=name, kind="curated_optional", action="retire",
                recommendation=(
                    f"`{name}` has no clear use in this project ({ev_msg}). "
                    "Retiring by default; flip to `activate` if you want it on the roster."
                ),
                evidence=evidence,
            ))

    # Suggested layout: top-level when at least one collision exists or the user
    # has any agents at all, namespaced otherwise. The actual handling of agents
    # is driven by the per-agent action, not the layout, so the recommendation is
    # mainly about command placement rather than agent migration.
    has_collisions = any(d.kind == "collision" for d in decisions)
    has_user_agents = bool(user_agents_by_name)
    if has_collisions:
        suggested = Layout.TOP_LEVEL.value
        rationale = (
            "You have agents whose names match MetaEnsemble's curated set. "
            "Top-level layout keeps the common slash-command names available "
            "while the per-agent decisions below govern every agent collision."
        )
    elif has_user_agents:
        suggested = Layout.NAMESPACED.value
        rationale = (
            "You have unique agents that do not collide with MetaEnsemble's. "
            "Namespaced layout keeps MetaEnsemble slash commands under "
            "`/metaensemble:*` so your existing command surface stays separate."
        )
    else:
        suggested = Layout.TOP_LEVEL.value
        rationale = (
            "You have no existing agents. Top-level layout gives the shortest "
            "slash-command names without implying any project-surface ownership."
        )

    return SurveyDecisions(
        agents=decisions,
        timestamp=timestamp,
        suggested_layout=suggested,
        layout_rationale=rationale,
        report_root=detect_report_root(project or Path.cwd()),
        overlaps=detect_overlaps(project or Path.cwd()),
        memory_surfaces=detect_memory_surfaces(project or Path.cwd()),
    )


def _decisions_to_yaml_doc(decisions: SurveyDecisions) -> str:
    """Serialize SurveyDecisions to a human-friendly YAML the user can edit."""
    lines: list[str] = [
        "# MetaEnsemble install decisions",
        f"# Generated: {decisions.timestamp}",
        "#",
        "# Edit the `action` for any decision you want to change, then run",
        "#   metaensemble adopt",
        "# The installer reads this file and respects every choice you record here.",
        "#",
        "# Recommended layout: " + decisions.suggested_layout,
        "# Reason: " + decisions.layout_rationale,
        "",
        f"suggested_layout: {decisions.suggested_layout}",
        f"report_root: {json.dumps(decisions.report_root)}",
        "",
        "# Memory files the runtime already loads for this project. MetaEnsemble",
        "# consumes these as dispatch context (scaffolded Manifest `context.files`",
        "# entries with `role: memory`); it never rewrites them.",
        "memory_surfaces:",
    ]
    if decisions.memory_surfaces:
        for surface in decisions.memory_surfaces:
            lines.append(f"  - path: {json.dumps(surface.path)}")
            lines.append(f"    scope: {surface.scope}")
    else:
        lines.append("  []")
    lines.extend([
        "",
        "overlaps:",
    ])
    if decisions.overlaps:
        for overlap in decisions.overlaps:
            lines.append(f"  {overlap.category}:")
            lines.append(f"    project_surface: {json.dumps(overlap.project_surface)}")
            lines.append(f"    metaensemble_surface: {json.dumps(overlap.metaensemble_surface)}")
            lines.append(f"    action: {overlap.action}      # project_owned | metaensemble_owned | dual")
            lines.append(f"    recommendation: {overlap.recommendation}")
            lines.append(f"    write_policy: {overlap.write_policy}")
            lines.append("    rationale: >")
            for r_line in overlap.rationale.splitlines() or [overlap.rationale]:
                lines.append(f"      {r_line}")
            if overlap.evidence:
                lines.append("    evidence:")
                for item in overlap.evidence:
                    lines.append(f"      - {item}")
    else:
        lines.append("  {}")
    lines.extend([
        "",
        "agents:",
    ])
    for d in decisions.agents:
        lines.append(f"  - name: {d.name}")
        lines.append(f"    kind: {d.kind}")
        lines.append(f"    action: {d.action}      # default; edit to override")
        if d.kind == "collision":
            lines.append("    # collision options: keep_yours | take_ours | keep_both")
        elif d.kind == "user_unique":
            lines.append("    # user_unique options: preserve | convert")
        elif d.kind == "curated_relevant":
            lines.append("    # curated_relevant options: activate | retire")
        elif d.kind == "curated_optional":
            lines.append("    # curated_optional options: retire | activate")
        if d.recommendation:
            lines.append("    recommendation: |")
            for r_line in d.recommendation.splitlines() or [d.recommendation]:
                lines.append(f"      {r_line}")
    lines.append("")
    return "\n".join(lines)


def load_decisions(path: Path) -> SurveyDecisions:
    """Parse an install-decisions.yaml back into a SurveyDecisions.

    Recommendations and the comparison/evidence fields are intentionally
    dropped — they exist only to inform the user's choice; the installer
    consumes only the `action` per agent and overlap.
    """
    data = yaml.safe_load(path.read_text()) or {}
    agents_raw = data.get("agents") or []
    agents: list[AgentDecision] = []
    for entry in agents_raw:
        if not isinstance(entry, dict):
            continue
        agents.append(AgentDecision(
            name=str(entry.get("name", "")).strip(),
            kind=str(entry.get("kind", "")).strip(),
            action=str(entry.get("action", "")).strip(),
            recommendation="",
        ))
    overlaps: list[OverlapDecision] = []
    overlaps_raw = data.get("overlaps") or {}
    if isinstance(overlaps_raw, dict):
        for category, entry in overlaps_raw.items():
            if not isinstance(entry, dict):
                continue
            evidence_raw = entry.get("evidence") or []
            evidence = [str(item) for item in evidence_raw] if isinstance(evidence_raw, list) else []
            overlaps.append(OverlapDecision(
                category=str(category),
                project_surface=str(entry.get("project_surface", "")),
                metaensemble_surface=str(entry.get("metaensemble_surface", "")),
                action=str(entry.get("action", "")).strip(),
                recommendation=str(entry.get("recommendation", "")).strip(),
                rationale=str(entry.get("rationale", "")).strip(),
                write_policy=str(entry.get("write_policy") or "block_when_metaensemble_owned").strip(),
                evidence=evidence,
            ))
    report_root = str(data.get("report_root") or "").strip()
    if not report_root:
        # Older install-decisions.yaml files predate `report_root`. Infer the
        # convention from the project so established `.claude/reports` projects
        # do not get silently treated as greenfield on the next adopt.
        report_root = detect_report_root(path.parent.parent)
    memory_surfaces: list[MemorySurface] = []
    if "memory_surfaces" in data:
        memory_raw = data.get("memory_surfaces") or []
        if isinstance(memory_raw, list):
            for entry in memory_raw:
                if not isinstance(entry, dict):
                    continue
                surface_path = str(entry.get("path", "")).strip()
                if not surface_path:
                    continue
                memory_surfaces.append(MemorySurface(
                    path=surface_path,
                    scope=str(entry.get("scope") or "project").strip(),
                ))
    else:
        # Older install-decisions.yaml files predate `memory_surfaces`.
        # Re-detect from the project so established adopts gain the memory
        # pointers without waiting for a fresh inspection.
        memory_surfaces = detect_memory_surfaces(path.parent.parent)

    return SurveyDecisions(
        agents=agents,
        timestamp=str(data.get("timestamp", "")),
        suggested_layout=_normalize_layout_value(
            str(data.get("suggested_layout") or data.get("suggested_mode") or Layout.NAMESPACED.value)
        ),
        layout_rationale="",
        report_root=report_root,
        overlaps=overlaps,
        memory_surfaces=memory_surfaces,
    )


def _render_survey_v2(
    discovered: list[DiscoveredArtifact],
    decisions: SurveyDecisions,
    user_exists: bool,
    project_exists: bool,
) -> str:
    """Render a friendly, opinionated inspection report.

    The report is short by design: every decision has a default with a
    one-sentence rationale, so the reader scans rather than studies. The
    full editable surface lives in the companion `install-decisions.yaml`.
    """
    has_user_setup = user_exists or project_exists

    lines: list[str] = [
        "# MetaEnsemble — inspection report",
        "",
        f"Generated: {decisions.timestamp}",
        "",
        "## Verdict",
        "",
        f"Recommended install layout: **`{decisions.suggested_layout}`**",
        "",
        decisions.layout_rationale,
        "",
        "Every decision below has a default. Read once, edit only what you disagree with in",
        "`.metaensemble/install-decisions.yaml`, then run:",
        "",
        "```",
        f"metaensemble user-setup --layout={decisions.suggested_layout}  # once per machine",
        "metaensemble adopt                                # registers this project",
        "```",
        "",
    ]

    # Collisions section.
    collision_decisions = [d for d in decisions.agents if d.kind == "collision"]
    lines.append("## Agents that exist in both your setup and MetaEnsemble")
    lines.append("")
    if not collision_decisions:
        lines.append("None — no name collisions detected.")
    else:
        lines.append(
            f"{len(collision_decisions)} of your agents share a name with one of "
            "MetaEnsemble's curated Roles. For each, choose `keep_yours` (default for "
            "agents that look richer than ours), `take_ours`, or `keep_both`."
        )
        lines.append("")
        lines.append("| Agent | Your tools | Ours | Your model | Ours | Default |")
        lines.append("|---|---|---|---|---|---|")
        for d in collision_decisions:
            cmp = d.comparison
            yt = ", ".join(cmp.user_tools[:4]) if cmp else ""
            ct = ", ".join(cmp.curated_tools[:4]) if cmp else ""
            if cmp and len(cmp.user_tools) > 4:
                yt += f", +{len(cmp.user_tools) - 4}"
            if cmp and len(cmp.curated_tools) > 4:
                ct += f", +{len(cmp.curated_tools) - 4}"
            ym = cmp.user_model if cmp else ""
            cm = cmp.curated_model_tier if cmp else ""
            lines.append(
                f"| `{d.name}` | {yt or '—'} | {ct or '—'} | {ym or '—'} | {cm or '—'} | `{d.action}` |"
            )
        lines.append("")
        for d in collision_decisions:
            if d.evidence:
                evidence_str = "; ".join(d.evidence)
                lines.append(
                    f"- **`{d.name}`** — {d.recommendation} "
                    f"(project evidence: {evidence_str})"
                )
            else:
                lines.append(f"- **`{d.name}`** — {d.recommendation}")
        lines.append("")

    # User-unique section.
    user_unique = [d for d in decisions.agents if d.kind == "user_unique"]
    lines.append("## Agents unique to you")
    lines.append("")
    if not user_unique:
        lines.append("None.")
    else:
        lines.append(
            f"{len(user_unique)} of your agents do not exist in MetaEnsemble's curated set. "
            "These are preserved as native agents by default. Switch any to `convert` in "
            "the decisions file if you want them to become Roles instead."
        )
        lines.append("")
        for d in user_unique:
            lines.append(f"- `{d.name}` — default: **{d.action}**")
        lines.append("")

    # Curated relevant.
    relevant = [d for d in decisions.agents if d.kind == "curated_relevant"]
    collision_count = len(collision_decisions)
    lines.append("## Curated Roles that match this project")
    lines.append("")
    if not relevant:
        if collision_count > 0:
            # All curated Roles that match the project are already represented
            # as collision agents in the user's setup — the per-agent decisions
            # for those Roles live in the collision section above.
            lines.append(
                f"All curated Roles relevant to this project are already represented as "
                f"agents in your setup ({collision_count} collision(s) above). The "
                "per-agent decisions in the collision section govern how each is "
                "handled; this section lists curated Roles you do not yet have."
            )
        else:
            lines.append("None — no project signals matched any curated Role.")
    else:
        lines.append(
            f"{len(relevant)} curated Roles are recommended for activation based on what "
            "we found in your project tree:"
        )
        lines.append("")
        for d in relevant:
            evidence_str = "; ".join(d.evidence) if d.evidence else "general code"
            lines.append(f"- `{d.name}` — default: **{d.action}** — evidence: {evidence_str}")
        lines.append("")

    # Curated optional.
    optional = [d for d in decisions.agents if d.kind == "curated_optional"]
    lines.append("## Curated Roles that look optional")
    lines.append("")
    if not optional:
        if collision_count > 0:
            lines.append(
                "None — every curated Role is either already present in your setup as a "
                "collision agent (see the collision section above) or has supporting "
                "evidence in this project."
            )
        else:
            lines.append("None — every curated Role found supporting evidence in this project.")
    else:
        lines.append(
            f"{len(optional)} curated Roles do not match obvious signals in this project. "
            "They are retired by default. Flip any to `activate` in the decisions file if "
            "you want them on the roster anyway."
        )
        lines.append("")
        for d in optional:
            ev = d.evidence[0] if d.evidence else "no signals"
            lines.append(f"- `{d.name}` — default: **{d.action}** — {ev}")
        lines.append("")

    # Signal probe summary — full transparency on what the detector
    # looked for and what it found. Deterministic across runs.
    role_probes = _role_signal_probes()
    if role_probes:
        lines.append("## Signal probe summary")
        lines.append("")
        lines.append(
            "For every curated Role, the table below lists the project signals "
            "the detector probed and whether each fired. Use it to understand "
            "why a Role activated (or didn't), and what would change the verdict."
        )
        lines.append("")
        # Build a quick lookup of evidence per role id so the summary can
        # report both the firing signals (from the decision) and the full
        # probe surface (from the catalog).
        evidence_by_role: dict[str, list[str]] = {}
        relevant_by_role: dict[str, bool] = {}
        for d in decisions.agents:
            if d.kind in ("collision", "curated_relevant", "curated_optional"):
                # `collision` and `curated_relevant` decisions are populated
                # with the firing-evidence list. `curated_optional` carries
                # the "no signals" message; treat as empty.
                if d.kind == "curated_optional":
                    evidence_by_role[d.name] = []
                    relevant_by_role[d.name] = False
                else:
                    evidence_by_role[d.name] = list(d.evidence)
                    relevant_by_role[d.name] = bool(d.evidence)
        for role_id, probes in role_probes.items():
            status = "MATCHED" if relevant_by_role.get(role_id) else "no match"
            firing = evidence_by_role.get(role_id) or []
            lines.append(f"### `{role_id}` — {status}")
            if firing:
                lines.append("- Signals that fired:")
                for ev in firing:
                    lines.append(f"  - {ev}")
            lines.append("- Signals probed:")
            for hint in probes:
                lines.append(f"  - {hint}")
            lines.append("")

    # Non-agent artifacts (skills, commands, output styles) — surfaced briefly.
    non_agents = [a for a in discovered if a.kind != "agent"]
    if non_agents:
        lines.append("## Other artifacts in your setup")
        lines.append("")
        by_kind: dict[str, list[DiscoveredArtifact]] = {}
        for a in non_agents:
            by_kind.setdefault(a.kind, []).append(a)
        for kind, items in by_kind.items():
            names = ", ".join(f"`{a.name}`" for a in sorted(items, key=lambda x: x.name))
            lines.append(f"- **{kind}s ({len(items)})**: {names}")
        lines.append("")
        lines.append(
            "MetaEnsemble's commands install under a namespace in namespaced layout and "
            "at top level (with collision refusal) in top-level layout. Skills and "
            "the protocol skill are additive. Output styles install as "
            "`metaensemble-wire`/`metaensemble-deliverable` in namespaced layout and "
            "`wire`/`deliverable` in top-level layout."
        )
        lines.append("")

    if decisions.overlaps:
        lines.append("## Overlap ownership decisions")
        lines.append("")
        lines.append(
            "These project surfaces duplicate work MetaEnsemble can already record. "
            "Edit `.metaensemble/install-decisions.yaml` if the default ownership "
            "does not match how this project should spend tokens."
        )
        lines.append("")
        for overlap in decisions.overlaps:
            lines.append(
                f"- **{overlap.category}** — `{overlap.project_surface}` overlaps "
                f"with {overlap.metaensemble_surface}; recommended owner: "
                f"`{overlap.recommendation}`."
            )
        lines.append("")

    lines.append("## Report location")
    lines.append("")
    lines.append(
        f"Default report root for new MetaEnsemble-authored reports: "
        f"`{decisions.report_root}`."
    )
    lines.append(
        "Use existing project report conventions only when the inspection detected "
        "one in `.metaensemble/install-decisions.yaml`; otherwise keep report "
        "artifacts under this ignored MetaEnsemble area."
    )
    lines.append(
        "Do not write both Executor reports and a Coordinator synthesis by default; "
        "write a Coordinator synthesis file only when the Manifest explicitly "
        "declares it as a Deliverable."
    )
    lines.append("")

    # Next steps.
    lines.append("## What happens next")
    lines.append("")
    if not has_user_setup:
        lines.append(
            "Your `~/.claude/` config is empty. MetaEnsemble will install its curated set "
            f"in `{decisions.suggested_layout}` layout without touching anything because there "
            "is nothing to touch."
        )
    else:
        lines.append(
            "1. Open `.metaensemble/install-decisions.yaml`. Read the defaults. Edit any "
            "you disagree with."
        )
        lines.append(
            f"2. If you have not yet run `metaensemble user-setup "
            f"--layout={decisions.suggested_layout}`, run it now (once per machine)."
        )
        lines.append(
            "3. Run `metaensemble adopt`. The installer will honor your decisions: "
            "every agent action is recorded in the install plan and reversible by "
            "`metaensemble unadopt`."
        )
        lines.append(
            "4. Verify health with `metaensemble doctor` and your first session with "
            "`metaensemble standup`."
        )
    lines.append("")
    return "\n".join(lines)


def _render_survey(
    discovered: list[DiscoveredArtifact],
    collisions: list[Collision],
    role_relevance: list[RoleRelevance],
    user_exists: bool,
    project_exists: bool,
) -> str:
    lines = [
        "# MetaEnsemble inspection",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Detected runtime configs",
        "",
        f"- User runtime ({RUNTIME_DIR_NAME}/) exists: {user_exists}",
        f"- Project runtime ({RUNTIME_DIR_NAME}/) exists: {project_exists}",
        "",
    ]

    by_layer: dict[str, list[DiscoveredArtifact]] = {"user": [], "project": []}
    for art in discovered:
        by_layer.setdefault(art.layer, []).append(art)

    for layer in ("user", "project"):
        arts = by_layer.get(layer, [])
        lines.append(f"## {layer.capitalize()}-layer artifacts ({len(arts)})")
        lines.append("")
        if not arts:
            lines.append("(none)")
            lines.append("")
            continue
        by_kind: dict[str, list[DiscoveredArtifact]] = {}
        for art in arts:
            by_kind.setdefault(art.kind, []).append(art)
        for kind in ("agent", "command", "skill", "output-style"):
            items = by_kind.get(kind, [])
            if not items:
                continue
            lines.append(f"### {kind.capitalize()}s ({len(items)})")
            lines.append("")
            for art in items:
                lines.append(f"- `{art.name}` — {art.path}")
            lines.append("")

    lines.append(f"## Collisions with MetaEnsemble-shipped items ({len(collisions)})")
    lines.append("")
    if not collisions:
        lines.append("(none — no name conflicts detected)")
    else:
        for c in collisions:
            lines.append(
                f"- `{c.discovered.kind}` named `{c.discovered.name}` "
                f"({c.discovered.layer} layer) collides with MetaEnsemble's "
                f"`{c.metaensemble_counterpart}`. Resolution depends on install decisions."
            )
    lines.append("")

    relevant = [r for r in role_relevance if r.relevant]
    irrelevant = [r for r in role_relevance if not r.relevant]

    lines.append("## Curated Roles relevant to this project")
    lines.append("")
    if not relevant:
        lines.append("(none — no project signals detected)")
    else:
        for r in relevant:
            lines.append(f"- **`{r.role_id}`** — evidence:")
            for ev in r.evidence:
                lines.append(f"  - {ev}")
    lines.append("")

    lines.append("## Curated Roles that look less relevant")
    lines.append("")
    if not irrelevant:
        lines.append("(none — every curated Role found supporting evidence)")
    else:
        for r in irrelevant:
            lines.append(f"- **`{r.role_id}`** — {r.evidence[0] if r.evidence else 'no signals detected'}")
        lines.append("")
        lines.append("These Roles stay available unless you mark them `retire` in "
                     "`install-decisions.yaml` before running `metaensemble adopt`.")
    lines.append("")

    lines.append("## Recommended install layouts")
    lines.append("")
    lines.append("- **`namespaced`**: MetaEnsemble installs in namespaced subdirectories. "
                 "Your existing artifacts are not touched. Both paradigms coexist.")
    lines.append("- **`top-level`**: Existing agents are converted to MetaEnsemble Roles "
                 "(originals backed up). Commands install at top level with collision "
                 "resolution. Skills and hooks extend additively.")
    lines.append("")
    suggested_relevant = ", ".join(r.role_id for r in relevant) or "(none detected)"
    lines.append(
        f"Curated Roles the signals above point at: {suggested_relevant}."
    )
    lines.append(
        "These activate by default. To override, edit the per-Role `action` "
        "in `install-decisions.yaml` (flip `activate` ↔ `retire`) before "
        "running `metaensemble adopt`."
    )
    lines.append("")
    return "\n".join(lines)


# --- Agent → Role conversion ---------------------------------------------


def _derive_alias_prefix(name: str) -> str:
    """First four lowercase alphanumeric chars of the name."""
    cleaned = "".join(c for c in name.lower() if c.isalnum())
    return cleaned[:4] if cleaned else "exec"


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter, falling back to lenient line-by-line parsing.

    Claude Code's agent files allow unquoted descriptions containing colons
    and other YAML special characters; strict yaml.safe_load chokes on these.
    The lenient fallback treats each line as `key: rest-of-line` and stores
    the raw string value, which is correct for the small flat schemas the
    agent format uses (name, description, tools, model, color).
    """
    if not text.startswith("---\n"):
        raise ValueError("file lacks YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise ValueError("frontmatter not terminated")
    fm_text = text[4:end]
    body = text[end + 5 :]
    try:
        loaded = yaml.safe_load(fm_text)
        if isinstance(loaded, dict):
            return loaded, body
    except yaml.YAMLError:
        pass

    # Lenient fallback: parse line-by-line, treating values as raw strings.
    fm: dict[str, Any] = {}
    for line in fm_text.split("\n"):
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key:
            fm[key] = value
    return fm, body


def _normalize_tools(tools_raw: Any) -> list[str]:
    if isinstance(tools_raw, list):
        return [str(t).strip() for t in tools_raw if str(t).strip()]
    if isinstance(tools_raw, str):
        return [t.strip() for t in tools_raw.split(",") if t.strip()]
    return []


def _normalize_model_tier(model_raw: Any) -> str:
    if not model_raw:
        return "sonnet"
    s = str(model_raw).lower()
    for tier in ("opus", "sonnet", "haiku"):
        if tier in s:
            return tier
    return "sonnet"


# The eight values Claude Code accepts for sub-agent `color` frontmatter
# (per the official sub-agents docs). Anything outside this set is dropped
# silently on conversion, since the runtime would reject it anyway.
_ALLOWED_AGENT_COLORS: frozenset[str] = frozenset({
    "red", "orange", "yellow", "green", "cyan", "blue", "purple", "pink",
})


def _normalize_color(color_raw: Any) -> str | None:
    """Return a valid agent color (one of the eight Claude Code accepts) or None."""
    if not color_raw:
        return None
    s = str(color_raw).strip().lower()
    return s if s in _ALLOWED_AGENT_COLORS else None


def convert_agent_to_role(agent_text: str) -> str:
    """Convert a Claude Code agent file into a MetaEnsemble Role spec.

    Mechanical field mapping. The body is preserved unchanged. Fields the
    Role schema requires that the source lacks (version, alias_prefix,
    output_styles, onboarding) get sensible defaults.

    Raises:
        ValueError: if the source file lacks well-formed frontmatter.
    """
    fm, body = _parse_frontmatter(agent_text)
    name = str(fm.get("name", "imported")).strip() or "imported"
    description = str(fm.get("description", "")).strip()
    if len(description) < 10:
        description = f"{description} (imported from Claude Code agent)".strip()
        if len(description) < 10:
            description = "Imported from Claude Code agent specification"

    tools = _normalize_tools(fm.get("tools") or fm.get("allowed_tools"))
    model_tier = _normalize_model_tier(fm.get("model") or fm.get("model_tier"))
    alias_prefix = _derive_alias_prefix(name)
    color = _normalize_color(fm.get("color"))

    new_fm: dict[str, Any] = {
        "name": name,
        "version": "1.0.0",
        "description": description,
        "model_tier": model_tier,
        "alias_prefix": alias_prefix,
    }
    if tools:
        new_fm["allowed_tools"] = tools
    if color:
        new_fm["color"] = color
    new_fm["output_styles"] = {
        "default": "deliverable",
        "wire": "wire",
        "deliverable": "deliverable",
    }
    new_fm["onboarding"] = {
        "read_first": [],
        "coordinate_with": [],
        "conventions": [],
        "mentor_role": None,
    }

    fm_text = yaml.dump(new_fm, default_flow_style=False, sort_keys=False)
    return f"---\n{fm_text}---\n{body}"


# --- Plan ----------------------------------------------------------------


def plan_install(
    survey_result: SurveyResult,
    layout: Layout,
    project: Path | None = None,
    home: Path | None = None,
    selected_roles: list[str] | None = None,
    decisions: SurveyDecisions | None = None,
) -> InstallPlan:
    """Produce the install plan, honoring user choices when present.

    Decision priority (highest first):
      1. `decisions` argument — explicit per-agent + per-curated-Role choices,
         normally loaded from `<project>/.metaensemble/install-decisions.yaml`.
         This is the user's primary choice surface and the only one the v0.1
         CLI exposes.
      2. `selected_roles` — programmatic narrowing filter. Treated as a
         filter on top of decisions: any name not present is marked retired.
         No CLI flag wires to this in v0.1; it stays as a hook for tests
         and downstream callers that drive `plan_install` directly.
      3. Default — when neither is supplied, the inspection is re-run with the
         current home/project to compute defaults. This keeps `plan_install`
         usable from tests and from a CLI that does not pass decisions.

    Per-agent action handling:
      - `keep_yours`  (collision) — no convert action; user's agent stays.
      - `take_ours`   (collision) — convert action; user's agent backed up
                                     and replaced by MetaEnsemble's Role.
      - `keep_both`   (collision) — preserve user's agent AND install
                                     MetaEnsemble's Role under a `-me`
                                     suffix (e.g. `backend-me`).
      - `preserve`    (user_unique) — no action; agent kept as native.
      - `convert`     (user_unique) — convert action.
      - `activate`    (curated_*) — name added to active_roles list.
      - `retire`      (curated_*) — name added to inactive_roles list.

    Top-level layout applies the convert actions; namespaced layout only
    installs MetaEnsemble's own surface in namespaced subdirs and leaves
    every agent untouched regardless of decision (the decision still
    affects active-roles.yaml so the Coordinator honors the user's
    choice at dispatch time).
    """
    project = project or Path.cwd()
    layout = Layout(layout)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = _backup_root(project, timestamp)
    actions: list[Action] = []

    all_curated_roles = sorted(_metaensemble_curated_names()["agent"])

    # Resolve the effective decisions for this install.
    if decisions is None:
        decisions = build_default_decisions(survey_result, home=home, project=project)

    # Per-agent decision lookup by name (normalized lowercase to match the
    # filesystem-ish casing the curated set uses).
    decisions_by_name: dict[str, AgentDecision] = {
        d.name: d for d in decisions.agents
    }

    # Active / inactive Roles fall out of the per-agent actions for curated
    # entries, plus user-unique preserves/converts that the Coordinator can
    # still dispatch to. The `selected_roles` argument acts as an additional
    # narrowing filter when supplied (programmatic callers only — no v0.1
    # CLI flag wires to it).
    selected_filter: set[str] | None = None
    if selected_roles is not None:
        selected_filter = {r.strip() for r in selected_roles if r.strip()}

    # The active_roles / inactive_roles lists answer the question the
    # Coordinator asks at dispatch time: "is this name dispatchable in
    # this project?" A name is dispatchable when EITHER the user's
    # native agent or a MetaEnsemble Role is available for it. For a
    # `keep_yours` collision the user's native agent is the answer; we
    # still list the name in active_roles so the Coordinator does not
    # mistakenly refuse a dispatch the user can actually make.
    active_roles: list[str] = []
    inactive_roles: list[str] = []
    for d in decisions.agents:
        if d.kind == "collision":
            if d.action == "keep_yours":
                # User's native agent IS dispatchable for this name.
                active_roles.append(d.name)
            elif d.action == "take_ours":
                active_roles.append(d.name)
            elif d.action == "keep_both":
                # Both names available — user's at the original name,
                # MetaEnsemble's at the `-me` suffix.
                active_roles.append(d.name)
                active_roles.append(f"{d.name}-me")
        elif d.kind == "user_unique":
            if d.action in ("preserve", "convert"):
                active_roles.append(d.name)
        elif d.kind == "curated_relevant":
            if d.action == "activate":
                active_roles.append(d.name)
            else:
                inactive_roles.append(d.name)
        elif d.kind == "curated_optional":
            if d.action == "activate":
                active_roles.append(d.name)
            else:
                inactive_roles.append(d.name)

    # Make sure every curated Role appears in exactly one of the two
    # buckets, even if no decision touched it (defensive).
    seen = set(active_roles) | set(inactive_roles)
    for name in all_curated_roles:
        if name not in seen:
            inactive_roles.append(name)

    if selected_filter is not None:
        # selected_roles is authoritative for which CURATED Roles are
        # active. User-unique Roles surfaced by decisions are preserved
        # in active_roles independently of this filter.
        user_unique_active = [n for n in active_roles if n not in all_curated_roles]
        active_curated = [n for n in all_curated_roles if n in selected_filter]
        inactive_curated = [n for n in all_curated_roles if n not in selected_filter]
        active_roles = active_curated + user_unique_active
        inactive_roles = [n for n in inactive_roles if n not in all_curated_roles] + inactive_curated

    # Deduplicate while preserving order for readability.
    def _uniq(seq: list[str]) -> list[str]:
        out: list[str] = []
        seen_inner: set[str] = set()
        for x in seq:
            if x not in seen_inner:
                seen_inner.add(x)
                out.append(x)
        return out
    active_roles = _uniq(active_roles)
    inactive_roles = _uniq([n for n in inactive_roles if n not in active_roles])

    # --- MetaEnsemble's own pieces install in both modes -------------------
    user_root = _user_runtime_dir(home)
    runtime_root = _runtime_root(home)

    # vendor-runtime is FIRST in the user-scope action list. It atomically
    # populates `~/.metaensemble/runtime/` with assets + runner. The slash
    # command / skill / output-style symlinks below then point at paths inside
    # `runtime/`, so the targets must exist before those symlinks are applied.
    actions.append(Action(
        kind="vendor-runtime",
        source=None,
        target=runtime_root,
        description=(
            "Vendor MetaEnsemble runtime atomically into "
            f"{runtime_root} (versioned + symlink swap)"
        ),
    ))

    if layout is Layout.NAMESPACED:
        commands_target = user_root / COMMANDS_SUBDIR / "metaensemble"
        output_style_prefix = "metaensemble-"
        actions.append(Action(
            kind="symlink",
            source=runtime_root / "commands",
            target=commands_target,
            description=f"Install MetaEnsemble slash commands at {commands_target}",
        ))
    else:  # TOP_LEVEL
        output_style_prefix = ""
        commands_dir = user_root / COMMANDS_SUBDIR
        for command_md in sorted((CORE_DIR / "commands").glob("*.md")):
            if not _is_canonical_curated_name(command_md.stem):
                continue
            target_path = commands_dir / command_md.name
            actions.append(Action(
                kind="symlink",
                source=runtime_root / "commands" / command_md.name,
                target=target_path,
                description=f"Install command `{command_md.stem}` at {target_path}",
                skip_if_exists=True,
            ))

    actions.append(Action(
        kind="symlink",
        source=runtime_root / "skills" / "metaensemble-protocol",
        target=user_root / SKILLS_SUBDIR / "metaensemble-protocol",
        description="Install the metaensemble-protocol skill (additive in both modes)",
    ))

    for style in ("wire", "deliverable"):
        target_name = f"{output_style_prefix}{style}.md"
        actions.append(Action(
            kind="symlink",
            source=runtime_root / "output-styles" / f"{style}.md",
            target=user_root / OUTPUT_STYLES_SUBDIR / target_name,
            description=f"Install output style: {target_name}",
        ))

    actions.append(Action(
        kind="merge-settings",
        source=None,
        target=user_root / SETTINGS_FILE,
        description="Configure MetaEnsemble lifecycle hooks + statusline in settings.json",
        backup_path=backup_root / "settings.json.bak",
    ))

    # --- Per-agent actions driven by the decisions surface ----------------
    project_metaensemble = _project_metaensemble_dir(project)
    user_metaensemble = _user_metaensemble_dir(home)

    for art in survey_result.discovered:
        if art.kind != "agent":
            continue
        decision = decisions_by_name.get(art.name)
        # If the decisions file lacks this agent (e.g. it was added after
        # the inspection), default to preserve so we never silently convert.
        if decision is None:
            continue

        target_dir = (user_metaensemble if art.layer == "user"
                      else project_metaensemble) / "roles"
        target_path = target_dir / f"{art.name}.md"
        backup_path = backup_root / "agents" / art.layer / f"{art.name}.md"

        if layout is Layout.TOP_LEVEL:
            if decision.kind == "collision" and decision.action == "take_ours":
                actions.append(Action(
                    kind="convert-agent",
                    source=art.path,
                    target=target_path,
                    description=(
                        f"Take ours: replace {art.layer}-layer agent `{art.name}` "
                        "with MetaEnsemble's Role"
                    ),
                    backup_path=backup_path,
                ))
            elif decision.kind == "collision" and decision.action == "keep_both":
                # Install MetaEnsemble's Role under a `-me` suffix; leave user's
                # agent in place. We do NOT use convert-agent here because the
                # source is MetaEnsemble's curated spec, not the user's agent.
                suffixed_target = target_dir / f"{art.name}-me.md"
                actions.append(Action(
                    kind="install-curated-role",
                    source=CORE_DIR / "roles" / f"{art.name}.md",
                    target=suffixed_target,
                    description=(
                        f"Keep both: also install MetaEnsemble's `{art.name}` "
                        f"as `{art.name}-me` Role"
                    ),
                ))
            elif decision.kind == "user_unique" and decision.action == "convert":
                actions.append(Action(
                    kind="convert-agent",
                    source=art.path,
                    target=target_path,
                    description=f"Convert user agent `{art.name}` to MetaEnsemble Role",
                    backup_path=backup_path,
                ))
            # keep_yours, preserve, and the default no-decision case: emit
            # no convert action; the agent stays at its original path.

    return InstallPlan(
        layout=layout, actions=actions, timestamp=timestamp,
        active_roles=active_roles, inactive_roles=inactive_roles,
    )


# --- Apply ---------------------------------------------------------------


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _do_symlink(action: Action) -> None:
    _ensure_parent(action.target)
    if action.target.exists() or action.target.is_symlink():
        # Idempotent: if it already points at the right place, no-op.
        if action.target.is_symlink() and Path(os.readlink(action.target)) == action.source:
            return
        if action.skip_if_exists:
            # User has a same-named artifact; refuse to overwrite (per the
            # collision-resolution policy in DEPLOYMENT.md).
            return
        raise FileExistsError(
            f"target exists and is not the expected symlink: {action.target}"
        )
    action.target.symlink_to(action.source)


def _do_convert_agent(action: Action) -> None:
    _ensure_parent(action.target)
    if action.backup_path:
        _ensure_parent(action.backup_path)
        shutil.copy2(action.source, action.backup_path)
    role_text = convert_agent_to_role(action.source.read_text())
    action.target.write_text(role_text)
    # Leave a shim behind so the agent runtime can still resolve the name.
    # The shim's frontmatter mirrors the original agent (name/description/
    # tools/model) so `Agent(subagent_type="X")` continues to work; the
    # body declares the file is now managed by a MetaEnsemble Role and
    # delegates to the Coordinator. Without this shim, the user loses
    # native dispatch by name after every top-level install.
    _write_agent_shim(action.source, action.target)


def _do_install_curated_role(action: Action) -> None:
    """Copy a curated Role spec from `metaensemble/roles/` to a user-layer target.

    Used for the `keep_both` decision where the user's agent stays in place
    and MetaEnsemble's Role is installed under a `-me` suffix.
    """
    _ensure_parent(action.target)
    if action.source is None or action.target is None:
        raise ValueError("install-curated-role requires source and target")
    shutil.copy2(action.source, action.target)


def _ensure_project_state(project: Path) -> None:
    """Idempotently create `<project>/.metaensemble/` subdirs + the Ledger.

    Made callable from apply_install so a fresh `metaensemble user-setup` no
    longer leaves the doctor's C4 check in WARN. Re-runs are safe; missing
    pieces are filled in, existing pieces are left alone.
    """
    base = project / ".metaensemble"
    state = base / "state"
    state.mkdir(parents=True, exist_ok=True)
    (base / "manifests").mkdir(parents=True, exist_ok=True)
    (base / "briefs").mkdir(parents=True, exist_ok=True)
    (base / "hooks").mkdir(parents=True, exist_ok=True)
    # Drop in an example budgets.yaml if one is not present, so the
    # cost gate has a config to read.
    budgets_target = base / "budgets.yaml"
    if not budgets_target.exists():
        example = CORE_DIR / "config" / "budgets.example.yaml"
        if example.exists():
            shutil.copy(example, budgets_target)
    # Add `.metaensemble/` to the project's root `.gitignore` so the
    # whole per-project directory stays out of git, following the same
    # convention every other tool uses for its per-project working
    # directories (`.venv`, `node_modules`, `dist`, `__pycache__`).
    # If the project has no `.gitignore`, one is created. If it has
    # one, an idempotent managed block is appended only when
    # `.metaensemble/` is not already listed.
    _ensure_project_gitignore(project)
    # Legacy cleanup: earlier versions wrote a `.gitignore` inside
    # `.metaensemble/` itself; that file is moot once the parent
    # directory is ignored. Remove only the file we wrote ourselves
    # (identified by our unique header line); leave any user-edited
    # file untouched.
    _remove_legacy_inner_gitignore(base)
    # Initialize the Ledger DB. The Ledger class auto-creates parent
    # dirs, so this is a no-op on subsequent runs.
    try:
        from metaensemble.lib.ledger import Ledger  # local import to avoid cycle
        migration = (CORE_DIR / "state" / "migrations" / "001_init.sql").read_text()
        ledger = Ledger(db_path=state / "department.db",
                        jsonl_path=state / "runs.jsonl")
        ledger.initialize(migration)
        ledger.close()
    except Exception:  # nosec B110
        # Doctor will catch a real Ledger problem on its next pass; we
        # never block install on Ledger init.
        pass


_GITIGNORE_MANAGED_BLOCK = (
    "\n"
    "# MetaEnsemble: per-project working directory (Ledger, manifests, backups,\n"
    "# inspection output). Re-derivable per machine — do not commit.\n"
    ".metaensemble/\n"
)

_GITIGNORE_NEW_FILE = (
    "# MetaEnsemble: per-project working directory (Ledger, manifests, backups,\n"
    "# inspection output). Re-derivable per machine — do not commit.\n"
    ".metaensemble/\n"
)

_LEGACY_INNER_GITIGNORE_MARKER = (
    "MetaEnsemble: ignore transient state, keep curated declarations committable."
)

_LEGACY_GITIGNORE_ENTRIES = {
    ".metaensemble/state/department.db",
    ".metaensemble/state/runs.jsonl",
    ".metaensemble/hooks/log.jsonl",
    ".metaensemble/state/pending",
    ".metaensemble/state/pending/",
}


def _gitignore_lists_metaensemble(text: str) -> bool:
    """True iff `.metaensemble/` is already listed as an ignore rule.

    Treats `.metaensemble`, `.metaensemble/`, `/.metaensemble`, and
    `/.metaensemble/` as equivalent. Negations (`!.metaensemble`) and
    comment lines do not count.
    """
    accepted = {".metaensemble", ".metaensemble/", "/.metaensemble", "/.metaensemble/"}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        if line in accepted:
            return True
    return False


def _ensure_project_gitignore(project: Path) -> None:
    """Make sure `<project>/.gitignore` ignores `.metaensemble/`.

    - No `.gitignore` at the project root → create one with our managed
      block as the entire file.
    - `.gitignore` exists and already lists `.metaensemble/` → no-op.
    - `.gitignore` exists without `.metaensemble/` → append the managed
      block (with a leading blank line for readability). The user's
      existing content is preserved verbatim.
    """
    gitignore = project / ".gitignore"
    try:
        if not gitignore.exists():
            gitignore.write_text(_GITIGNORE_NEW_FILE)
            return
        existing = gitignore.read_text()
        if _gitignore_lists_metaensemble(existing):
            return
        # Append, preserving exactly one blank line between the existing
        # content and our block regardless of trailing-newline state.
        separator = "" if existing.endswith("\n\n") else (
            "\n" if existing.endswith("\n") else "\n\n"
        )
        gitignore.write_text(existing + separator + _GITIGNORE_MANAGED_BLOCK.lstrip("\n"))
    except OSError:
        # Never block install on .gitignore — it is a convenience, not a
        # correctness requirement.
        pass


def _remove_legacy_inner_gitignore(metaensemble_dir: Path) -> None:
    """Delete `<project>/.metaensemble/.gitignore` if we wrote it.

    Earlier versions wrote a self-contained `.gitignore` inside
    `.metaensemble/` itself; that file is meaningless once the parent
    directory is ignored from the project root. We only remove a file
    that carries our own header marker — a hand-edited file with that
    name is left alone.
    """
    inner = metaensemble_dir / ".gitignore"
    if not inner.exists():
        return
    try:
        text = inner.read_text()
    except OSError:
        return
    if _LEGACY_INNER_GITIGNORE_MARKER in text:
        try:
            inner.unlink()
        except OSError:
            pass


def _write_agent_shim(agent_path: Path, role_path: Path) -> None:
    """Replace the converted agent file with a thin shim that delegates.

    The shim preserves enough of the original frontmatter to keep the
    agent runtime's `Agent(subagent_type="X")` working, then redirects
    the body to a one-paragraph note explaining that this agent is
    managed by MetaEnsemble. The original body lives in the Role file
    at `role_path`; the shim notes its location so a curious user can
    find it.
    """
    try:
        agent_path.read_text()
    except FileNotFoundError:
        # Caller already unlinked the original. Reconstruct a minimal
        # frontmatter from the Role at role_path so we still produce a
        # working shim — better than no agent file at all.
        try:
            role_fm, _ = _read_agent_frontmatter(role_path)
        except Exception:
            return
        name = str(role_fm.get("name", agent_path.stem))
        desc = str(role_fm.get("description", "")).strip() or "Managed by MetaEnsemble Role."
        tools_list = role_fm.get("allowed_tools") or role_fm.get("tools") or []
        if isinstance(tools_list, list):
            tools = ", ".join(str(t) for t in tools_list if str(t).strip())
        else:
            tools = str(tools_list)
        model = role_fm.get("model_tier") or role_fm.get("model") or ""
        agent_path.parent.mkdir(parents=True, exist_ok=True)
        agent_path.write_text(_compose_shim_body(name, desc, tools, str(model), role_path))
        return

    try:
        fm, _body = _read_agent_frontmatter(agent_path)
    except Exception:
        return
    name = str(fm.get("name", agent_path.stem))
    desc = str(fm.get("description", "")).strip()
    tools_list = fm.get("tools") or fm.get("allowed_tools") or []
    if isinstance(tools_list, list):
        tools = ", ".join(str(t) for t in tools_list if str(t).strip())
    else:
        tools = str(tools_list)
    model = fm.get("model") or fm.get("model_tier") or ""
    agent_path.parent.mkdir(parents=True, exist_ok=True)
    agent_path.write_text(_compose_shim_body(name, desc, tools, str(model), role_path))


CANONICAL_PAD = " (imported from Claude Code agent)"


def role_to_agent_text(role_text: str) -> str:
    """Render the agent-format equivalent of a Role file's content.

    The reverse of `convert_agent_to_role`. Mechanical mapping:
        name        -> name
        description -> description (canonical pad suffix stripped)
        allowed_tools -> tools (comma-joined, the agent-format convention)
        model_tier  -> model
    Dropped fields (MetaEnsemble-only): version, alias_prefix, output_styles, onboarding.
    Body preserved verbatim.

    This is the function that powers `metaensemble export-agents`, the
    documented launch-time escape hatch for users who want their original
    Claude Code agent files back when project adoption backups are missing.
    Recovery via this command is the user-controlled mirror image of the
    forward conversion the installer performs at install time.
    """
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
    color = _normalize_color(fm.get("color"))

    lines = ["---", f"name: {name}"]
    if description:
        escaped = description.replace("'", "''")
        lines.append(f"description: '{escaped}'")
    if tools_value:
        lines.append(f"tools: {tools_value}")
    if model:
        lines.append(f"model: {model}")
    if color:
        lines.append(f"color: {color}")
    lines.append("---")
    if not body.startswith("\n"):
        return "\n".join(lines) + "\n" + body
    return "\n".join(lines) + body


def export_agents(
    *,
    home: Path | None = None,
    project: Path | None = None,
    target_dir: Path | None = None,
    include_user: bool = True,
    include_project: bool = True,
    overwrite: bool = False,
) -> list[Path]:
    """Reverse-convert MetaEnsemble Role files into Claude Code agents.

    Recovery escape hatch. Walks the user-layer and (optionally) project-layer
    Role directories, reverse-converts each `.md` file, and writes the result
    into `target_dir`. When `target_dir` is None we write to the default
    `~/.claude/agents/`.

    The function is conservative by design: if the target already exists and
    `overwrite=False`, that file is skipped and the rest continue. The caller
    receives the list of paths actually written so the CLI can report exactly
    what changed.
    """
    home = home or Path.home()
    project = project or Path.cwd()
    target_dir = target_dir or (home / RUNTIME_DIR_NAME / AGENTS_SUBDIR)
    target_dir.mkdir(parents=True, exist_ok=True)

    sources: list[Path] = []
    if include_user:
        user_roles = home / ".metaensemble" / "roles"
        if user_roles.is_dir():
            sources.extend(sorted(user_roles.glob("*.md")))
    if include_project:
        project_roles = project / ".metaensemble" / "roles"
        if project_roles.is_dir():
            sources.extend(sorted(project_roles.glob("*.md")))

    written: list[Path] = []
    for role_path in sources:
        target = target_dir / role_path.name
        if target.exists() and not overwrite:
            continue
        text = role_to_agent_text(role_path.read_text())
        target.write_text(text)
        written.append(target)
    return written


def _compose_shim_body(name: str, description: str, tools: str, model: str, role_path: Path) -> str:
    """Render the shim agent file content."""
    lines = ["---", f"name: {name}"]
    if description:
        escaped = description.replace("'", "''")
        lines.append(f"description: '{escaped}'")
    if tools:
        lines.append(f"tools: {tools}")
    if model:
        lines.append(f"model: {model}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {name} (MetaEnsemble Role)")
    lines.append("")
    lines.append(
        "This agent has been adopted into MetaEnsemble. Its full spec "
        f"lives at `{role_path}`. When the runtime dispatches "
        f"`Agent(subagent_type=\"{name}\")` directly, that invocation is fine; "
        "for richer coordination (Manifests, Ledger recording, peer review), "
        "dispatch through `/dispatch <intent>` so the Coordinator engages the "
        "MetaEnsemble protocol."
    )
    lines.append("")
    lines.append(
        "To revert this agent to its pre-MetaEnsemble form, run "
        "`metaensemble unadopt` from this project, or copy the "
        "backup at `<project>/.metaensemble/backups/<ts>/agents/...` back to "
        "this path manually."
    )
    lines.append("")
    return "\n".join(lines)


# --- Atomic runtime vendoring -------------------------------------------

# Directories of package assets to vendor into ~/.metaensemble/runtime/.
# These mirror the package-data globs in pyproject.toml. Source files come
# from the installed package via `importlib.resources` (works for both
# wheel and editable installs); destination is the new versioned dir.
_VENDOR_ASSET_DIRS = (
    "commands",
    "skills",
    "output-styles",
    "roles",
    "schemas",
    "state",
    "config",
)

# Subset of vendored files the MANIFEST must contain post-vendor. If any
# is missing the manifest verification fails and the atomic swap aborts.
# Kept small and stable; expanding it on every new file would create churn.
_VENDOR_REQUIRED_FILES = (
    "bin/me-run",
    "commands/dispatch.md",
    "skills/metaensemble-protocol/SKILL.md",
    "schemas/manifest.schema.json",
    "state/migrations/001_init.sql",
)


def _new_runtime_version_id() -> str:
    """Collision-proof version id (v3.2 #4).

    Format: `<UTC-second-timestamp>-<12 hex of UUIDv7 random tail>`. The
    timestamp keeps the id human-sortable; the random suffix comes from
    the LAST 12 hex chars of the UUID (= 48 bits of `os.urandom`) so
    sub-second back-to-back calls cannot collide. The UUID's leading
    hex chars encode the millisecond timestamp and would NOT be unique
    across rapid back-to-back calls within one ms — using the tail is
    what gives the collision guarantee.
    """
    from metaensemble.lib.ids import uuid7
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{uuid7().hex[-12:]}"


def _runner_text(python_executable: str) -> str:
    """Content of `~/.metaensemble/runtime/bin/me-run`.

    `shlex.quote` on the Python path is MANDATORY (v3.2 #2) — paths with
    spaces (e.g. `/Users/Jane Doe/anaconda3/bin/python`) would break
    exec under `/bin/sh` without quoting.
    """
    return (
        "#!/bin/sh\n"
        "# MetaEnsemble runner — generated by `metaensemble user-setup`.\n"
        "# Pinned to one Python interpreter. Re-run user-setup after switching\n"
        "# Python envs to regenerate against the new interpreter.\n"
        f"exec {shlex.quote(python_executable)} -m metaensemble.cli \"$@\"\n"
    )


def _package_resources_root():
    """Return the installed package resource root.

    Split out so tests can stage a polluted package tree without relying on
    the real site-packages directory.
    """
    import importlib.resources as resources
    return resources.files("metaensemble")


def _copy_resource_tree_filtered(src, dst: Path) -> list[str]:
    """Copy one package resource tree, skipping iCloud/Finder duplicates.

    macOS/iCloud conflict copies use a stable `name N.ext` pattern. If those
    files are copied into the vendored runtime, Claude Code discovers them as
    real slash commands or skills through the runtime symlinks. Filtering here
    prevents that propagation; C11 remains the diagnostic surface for the
    source pollution.
    """
    skipped: list[str] = []

    def _copy_node(node, target: Path, rel_parts: tuple[str, ...]) -> None:
        name = getattr(node, "name", "")
        if name and not _is_canonical_curated_name(Path(name).stem):
            skipped.append("/".join((*rel_parts, name)))
            return
        try:
            is_dir = node.is_dir()
        except (AttributeError, OSError):
            is_dir = False
        if is_dir:
            target.mkdir(parents=True, exist_ok=True)
            try:
                children = sorted(node.iterdir(), key=lambda child: child.name)
            except (AttributeError, OSError):
                children = []
            for child in children:
                _copy_node(child, target / child.name, (*rel_parts, name))
            return
        try:
            is_file = node.is_file()
        except (AttributeError, OSError):
            is_file = False
        if not is_file:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        source_path = Path(str(node))
        if source_path.is_file():
            shutil.copy2(source_path, target)
        else:
            target.write_bytes(node.read_bytes())

    dst.mkdir(parents=True, exist_ok=True)
    try:
        children = sorted(src.iterdir(), key=lambda child: child.name)
    except (AttributeError, OSError):
        children = []
    for child in children:
        _copy_node(child, dst / child.name, ())
    return skipped


def _log_vendor_runtime_duplicate_skips(home: Path, skipped: list[str]) -> None:
    if not skipped:
        return
    state = _user_metaensemble_dir(home) / "state"
    try:
        state.mkdir(parents=True, exist_ok=True)
        record = {
            "kind": "vendor-runtime-skipped-duplicates",
            "ts": datetime.now(timezone.utc).isoformat(),
            "count": len(skipped),
            "message": (
                f"Skipped {len(skipped)} duplicate files during runtime vendor; "
                "common cause is iCloud Desktop sync. Run `metaensemble doctor` "
                "(C11) for remediation guidance."
            ),
            "examples": skipped[:10],
        }
        with (state / "vendor-runtime.log.jsonl").open("a") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


def _copy_package_assets_into(version_dir: Path, *, home: Path | None = None) -> list[str]:
    """Copy every asset directory from the installed package into version_dir.

    Uses `importlib.resources.files("metaensemble")` so this works whether
    the package was installed via wheel (assets in site-packages) or
    editable (assets in the source tree). Either way, the destination is
    a hard copy — independent of the source going forward.

    Returns any source-relative duplicate paths skipped during the copy.
    """
    pkg = _package_resources_root()
    skipped: list[str] = []
    for asset_dir in _VENDOR_ASSET_DIRS:
        src = pkg.joinpath(asset_dir)
        # `Traversable.is_dir()` returns False for zip-imported packages
        # but we don't ship those for v0.1.0 — wheel-extracted into a
        # real dir is the supported install.
        try:
            if not src.is_dir():
                continue
        except (AttributeError, OSError):
            continue
        dst = version_dir / asset_dir
        skipped.extend(
            f"{asset_dir}/{rel}"
            for rel in _copy_resource_tree_filtered(src, dst)
        )
    if home is not None:
        _log_vendor_runtime_duplicate_skips(home, skipped)
    return skipped


def _write_runtime_manifest(version_dir: Path) -> None:
    """Write a MANIFEST file: one line per file, `<sha256>  <relpath>`.

    Used by `_verify_runtime_manifest` to detect incomplete copies before
    the atomic swap, and by doctor C9 to detect post-install corruption.
    """
    lines: list[str] = []
    for path in sorted(version_dir.rglob("*")):
        if not path.is_file() or path.name == "MANIFEST":
            continue
        h = hashlib.sha256(path.read_bytes()).hexdigest()
        rel = path.relative_to(version_dir).as_posix()
        lines.append(f"{h}  {rel}")
    (version_dir / "MANIFEST").write_text("\n".join(lines) + "\n")


def _verify_runtime_manifest(version_dir: Path) -> None:
    """Raise if MANIFEST is missing, malformed, or any listed file has
    changed hash. Plus: assert every `_VENDOR_REQUIRED_FILES` entry is
    present in the manifest — a copy that failed partway would lack
    these even if the surviving files hash correctly.
    """
    manifest = version_dir / "MANIFEST"
    if not manifest.exists():
        raise RuntimeError(f"MANIFEST missing from {version_dir}")
    listed: set[str] = set()
    for line in manifest.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            sha, rel = line.split("  ", 1)
        except ValueError:
            raise RuntimeError(f"Malformed MANIFEST line in {version_dir}: {line!r}")
        path = version_dir / rel
        if not path.is_file():
            raise RuntimeError(f"Missing file referenced in MANIFEST: {rel}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise RuntimeError(f"Hash mismatch for {rel}")
        listed.add(rel)
    missing_required = [r for r in _VENDOR_REQUIRED_FILES if r not in listed]
    if missing_required:
        raise RuntimeError(
            f"MANIFEST missing required files: {missing_required}"
        )


def _verify_runtime_manifest_safe(version_dir: Path) -> bool:
    """Boolean wrapper for use in the recovery sweep (no exceptions)."""
    try:
        _verify_runtime_manifest(version_dir)
        return True
    except Exception:
        return False


def _cleanup_stale_vendor_artifacts(home: Path | None = None) -> None:
    """Remove ONLY invalid/incomplete runtime artifacts.

    What this removes:
      - `runtime.tmp-*` symlinks/dirs (orphans from interrupted vendor)
      - `runtime.bak-*` directories (legacy from pre-v3.1 designs)
      - `~/.metaensemble/bin/me-run` and its parent dir (legacy launcher)
      - `versions/<id>/` whose MANIFEST is missing OR fails verification

    What this does NOT touch:
      - Any version dir with a valid MANIFEST — even if it's not currently
        pointed at by `runtime`. Those are previous valid versions that
        `_gc_runtime_versions` is responsible for retaining or pruning.
    """
    base = _user_metaensemble_dir(home)
    if not base.exists():
        return

    # Stale tmp symlinks/dirs from interrupted vendor calls.
    for entry in base.glob("runtime.tmp-*"):
        try:
            if entry.is_symlink():
                entry.unlink()
            elif entry.is_dir():
                shutil.rmtree(entry)
        except OSError:
            pass

    # Legacy bak directories (from pre-v3.1 designs).
    for entry in base.glob("runtime.bak-*"):
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
        except OSError:
            pass

    # Legacy launcher residue: older installs lived at
    # ~/.metaensemble/bin/me-run. The current runner lives inside
    # ~/.metaensemble/runtime/bin/me-run, so the old `bin/` dir is orphaned.
    legacy_bin = base / "bin"
    if legacy_bin.is_dir():
        try:
            shutil.rmtree(legacy_bin)
        except OSError:
            pass

    # Invalid version dirs (missing/failed MANIFEST). Recovery never
    # deletes a VALID version dir — that's GC's responsibility.
    versions_dir = _runtime_versions_dir(home)
    if versions_dir.is_dir():
        for version in versions_dir.iterdir():
            if not version.is_dir():
                continue
            if not _verify_runtime_manifest_safe(version):
                try:
                    shutil.rmtree(version)
                except OSError:
                    pass


def _gc_runtime_versions(home: Path | None = None, keep: int = 2) -> None:
    """Retain the last N valid version dirs; delete older ones.

    Always preserves the version currently pointed at by `~/.metaensemble/
    runtime`, even if it falls outside the `keep` window (e.g. a forced
    rollback). Only acts on VALID versions (MANIFEST verified); invalid
    versions are recovery's job.
    """
    versions_dir = _runtime_versions_dir(home)
    if not versions_dir.is_dir():
        return
    runtime_link = _runtime_root(home)
    current: Path | None = None
    if runtime_link.is_symlink():
        try:
            current = runtime_link.resolve(strict=False)
        except OSError:
            current = None

    valid = sorted(
        (v for v in versions_dir.iterdir()
         if v.is_dir() and (v / "MANIFEST").exists()),
        key=lambda p: p.name,
    )
    to_keep = set(valid[-keep:])
    if current is not None:
        to_keep.add(current)
    for v in valid:
        if v not in to_keep:
            try:
                shutil.rmtree(v)
            except OSError:
                pass


def _vendor_runtime_atomically(home: Path | None = None,
                                python_executable: str | None = None) -> Path:
    """Atomic-swap ~/.metaensemble/runtime to a freshly vendored version.

    Algorithm:

      1. Recovery sweep removes stale tmp/bak/invalid artifacts.
      2. Allocate a collision-proof version dir
         `~/.metaensemble/runtime-versions/<id>/`.
      3. Copy all package asset dirs into the version dir.
      4. Generate `bin/me-run` runner inside the version dir.
      5. Write MANIFEST (sha256 per file).
      6. Verify MANIFEST — every required file present + every hash matches.
         A failure here aborts BEFORE the atomic swap, leaving the
         previous `runtime` symlink intact.
      7. Atomic symlink swap via `os.replace(tmp_link, runtime_link)`.
         POSIX guarantees this is one syscall.
      8. GC valid previous versions (keep last 2 + currently-linked).

    Returns the path of the new version dir (the symlink's target after swap).
    """
    home = home or Path.home()
    python_executable = python_executable or sys.executable

    base = _user_metaensemble_dir(home)
    base.mkdir(parents=True, exist_ok=True)

    # 1. Recovery sweep.
    _cleanup_stale_vendor_artifacts(home)

    # 2. Allocate version dir.
    versions_dir = _runtime_versions_dir(home)
    versions_dir.mkdir(parents=True, exist_ok=True)
    version_id = _new_runtime_version_id()
    new_version = versions_dir / version_id
    new_version.mkdir()

    try:
        # 3. Copy asset dirs.
        _copy_package_assets_into(new_version, home=home)

        # 4. Generate runner inside the version dir.
        runner = new_version / "bin" / "me-run"
        runner.parent.mkdir(parents=True, exist_ok=True)
        runner.write_text(_runner_text(python_executable))
        runner.chmod(0o755)

        # 5. Write MANIFEST.
        _write_runtime_manifest(new_version)

        # 6. Verify (raises on failure — aborts BEFORE the swap).
        _verify_runtime_manifest(new_version)

        # 7. Atomic symlink swap.
        runtime_link = _runtime_root(home)
        tmp_link = base / f"runtime.tmp-{version_id}"
        if tmp_link.is_symlink() or tmp_link.exists():
            tmp_link.unlink()
        tmp_link.symlink_to(new_version, target_is_directory=True)
        os.replace(tmp_link, runtime_link)
    except Exception:
        # Roll back the in-progress version dir; runtime symlink (if any)
        # is untouched because we never got to step 7.
        shutil.rmtree(new_version, ignore_errors=True)
        raise

    # 8. GC.
    _gc_runtime_versions(home, keep=2)
    return new_version


def _do_vendor_runtime(action: Action, home: Path | None = None) -> None:
    """Apply a vendor-runtime action.

    The action's `target` is the runtime symlink path (informational); the
    actual swap happens via `_vendor_runtime_atomically`. `source` is
    unused — the vendor function locates assets via importlib.resources.
    """
    _vendor_runtime_atomically(home=home)


# --- Legacy launcher cleanup -------------------------------------------
# Kept as a no-op shim to ease any in-flight rollback. The render-launcher
# action kind is no longer emitted by plan_install; if it appears in an
# old backup plan being reversed, the reversal path handles it as a
# best-effort unlink.


def _do_render_launcher(action: Action) -> None:
    """No-op shim for legacy render-launcher actions.

    `plan_install` no longer emits this action; the runner is generated
    atomically inside `~/.metaensemble/runtime/bin/me-run` by the
    `vendor-runtime` action. This shim exists so the reversal path in
    `uninstall` (which walks backup plan.json files) does not crash on
    legacy `render-launcher` entries left by older installs.
    """
    return


def _do_merge_settings(action: Action) -> None:
    settings_path = action.target
    backup_path = action.backup_path
    existing: dict[str, Any] = {}
    if settings_path.exists():
        if backup_path:
            _ensure_parent(backup_path)
            shutil.copy2(settings_path, backup_path)
        try:
            existing = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            existing = {}
    hooks = existing.setdefault("hooks", {})
    # The merge-settings action targets `<home>/.claude/settings.json`, so
    # we can recover the install's home directory from the target path
    # (`<home>/.claude/settings.json` → `<home>`). This lets the launcher
    # lookup find the right `~/.metaensemble/runtime/bin/me-run` for the
    # install without threading `home` through every Action call site.
    install_home = settings_path.parent.parent
    metaensemble_hooks = _metaensemble_hook_entries(home=install_home)
    # Strip any existing MetaEnsemble entries (recognized via the same
    # rule the uninstaller uses) before adding the current ones. This
    # turns merge into "replace ours, leave others alone" instead of
    # "append ours and dedupe by exact match", which would leave legacy
    # direct-Python entries alongside the launcher entries and cause
    # every dispatch to fire the hooks twice.
    for event, groups in list(hooks.items()):
        if not isinstance(groups, list):
            continue
        new_groups: list[dict[str, Any]] = []
        for group in groups:
            inner = group.get("hooks") or []
            kept = [h for h in inner if not _is_metaensemble_hook_command(h.get("command", ""))]
            if kept != inner:
                # Drop the group entirely if every hook in it was ours.
                if kept:
                    group = dict(group)
                    group["hooks"] = kept
                    new_groups.append(group)
            else:
                new_groups.append(group)
        if new_groups:
            hooks[event] = new_groups
        else:
            del hooks[event]
    for event, entries in metaensemble_hooks.items():
        event_list = hooks.setdefault(event, [])
        for entry in entries:
            if entry not in event_list:
                event_list.append(entry)
    # Register MetaEnsemble's statusline script unless the user already
    # has a non-MetaEnsemble statusline configured (refuse-to-overwrite
    # policy mirrors the slash-command behaviour in top-level layout).
    statusline_entry = _metaensemble_statusline_entry(home=install_home)
    existing_status = existing.get("statusLine")
    if (
        not isinstance(existing_status, dict)
        or "metaensemble" in str(existing_status.get("command", "")).lower()
    ):
        existing["statusLine"] = statusline_entry
    _ensure_parent(settings_path)
    settings_path.write_text(json.dumps(existing, indent=2) + "\n")


def _statusline_command(home: Path | None = None) -> str:
    """Build the statusline command using the runtime runner.

    The runner lives inside the vendored runtime at
    `~/.metaensemble/runtime/bin/me-run`, generated atomically by
    `vendor-runtime` and pinned to the install's Python interpreter.
    The path goes through the runtime symlink so it stays stable across
    re-vendors. shlex.join handles spaces/metacharacters in the path.

    Fallback (no runtime vendored yet): use the package's statusline
    script directly. This keeps the test harness and first-run
    pre-vendor doctor checks working.
    """
    runner = _runner_path(home)
    if runner.exists():
        return shlex.join([str(runner), "statusline"])
    statusline_script = CORE_DIR / "statusline" / "me_status.py"
    return shlex.join([sys.executable, str(statusline_script)])


def _metaensemble_statusline_entry(home: Path | None = None) -> dict[str, Any]:
    """Statusline configuration MetaEnsemble installs in settings.json.

    Claude Code v2.1.80+ pipes a JSON payload (including the runtime's
    `rate_limits` field) to the configured statusline command on each
    refresh. Our script captures the rate-limit data to a state file
    that hooks and tools read; the rendered output also gives the user
    a small visible indicator that MetaEnsemble is active.
    """
    return {
        "type": "command",
        "command": _statusline_command(home=home),
    }


def _hook_command(hook_filename: str, home: Path | None = None) -> str:
    """Build a shell-safe command string for a hook script.

    When `<home>/.metaensemble/runtime/bin/me-run` exists, the command is
    rendered as `me-run hook <hook_filename>` so the launcher resolves
    the Python interpreter at execution time. This is the portability
    benefit: moving the project or upgrading Python no longer requires
    settings.json surgery — `metaensemble user-setup` re-renders the
    runner inside a freshly vendored version directory and atomically
    swaps the `runtime` symlink to it.

    When the launcher is not installed (a first-run install that has
    not yet run `user-setup`, or the test harness), the command falls
    back to `sys.executable` plus the absolute hook path. The fallback
    is correctness-preserving but loses portability; the doctor will
    warn when settings.json carries an absolute Python path.

    `home` lets callers (notably tests) point the launcher lookup at a
    fixture directory rather than the real `~/.metaensemble/`. When
    `home` is None the real user's home is used.

    The command string is parsed by the agent runtime's shell, so any
    path containing whitespace or shell metacharacters must be quoted.
    `shlex.join` produces a properly escaped command that survives
    arbitrary install locations (paths with spaces, parentheses, etc.).
    """
    runner = _runner_path(home)
    if runner.exists():
        return shlex.join([str(runner), "hook", hook_filename])
    hooks_dir = CORE_DIR / "hooks"
    return shlex.join([sys.executable, str(hooks_dir / hook_filename)])


def _metaensemble_hook_entries(home: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    """The hook command entries MetaEnsemble registers in settings.json.

    The agent-runtime tool that spawns subagents goes by two names
    across runtime versions: `Task` and `Agent`. The matcher in
    settings.json must match the active runtime's tool name or the
    hook never fires, which produces a silent failure mode where
    dispatches succeed but the Ledger stays empty. Registering both
    names ensures the wiring works regardless of which runtime
    version is running.

    `home` controls where `_hook_command` looks for the resilient
    launcher; tests pass a tmp_path home so their assertions are
    independent of the developer's real `~/.metaensemble/`.
    """
    pre_task_cmd = _hook_command("pre_task.py", home=home)
    post_task_cmd = _hook_command("post_task.py", home=home)
    file_event_cmd = _hook_command("file_event.py", home=home)
    session_start_cmd = _hook_command("session_start.py", home=home)
    deliverable_sync_cmd = _hook_command("deliverable_sync.py", home=home)
    session_summary_cmd = _hook_command("session_summary.py", home=home)
    subagent_stop_cmd = _hook_command("subagent_stop.py", home=home)
    return {
        "SessionStart": [{
            "matcher": "*",
            "hooks": [{"type": "command", "command": session_start_cmd}],
        }],
        "PreToolUse": [
            {"matcher": "Task",  "hooks": [{"type": "command", "command": pre_task_cmd}]},
            {"matcher": "Agent", "hooks": [{"type": "command", "command": pre_task_cmd}]},
            {"matcher": "Write", "hooks": [{"type": "command", "command": file_event_cmd}]},
            {"matcher": "Edit", "hooks": [{"type": "command", "command": file_event_cmd}]},
            {"matcher": "MultiEdit", "hooks": [{"type": "command", "command": file_event_cmd}]},
            {"matcher": "NotebookEdit", "hooks": [{"type": "command", "command": file_event_cmd}]},
        ],
        "PostToolUse": [
            {"matcher": "Task",  "hooks": [{"type": "command", "command": post_task_cmd}]},
            {"matcher": "Agent", "hooks": [{"type": "command", "command": post_task_cmd}]},
            {"matcher": "Write", "hooks": [{"type": "command", "command": file_event_cmd}]},
            {"matcher": "Edit", "hooks": [{"type": "command", "command": file_event_cmd}]},
            {"matcher": "MultiEdit", "hooks": [{"type": "command", "command": file_event_cmd}]},
            {"matcher": "NotebookEdit", "hooks": [{"type": "command", "command": file_event_cmd}]},
            {"matcher": "Write", "hooks": [{"type": "command", "command": deliverable_sync_cmd}]},
        ],
        "Stop": [{
            "matcher": "*",
            "hooks": [{"type": "command", "command": session_summary_cmd}],
        }],
        # SubagentStop finalizes background-dispatched Runs by agentId.
        "SubagentStop": [{
            "matcher": "*",
            "hooks": [{"type": "command", "command": subagent_stop_cmd}],
        }],
    }


def _action_already_applied(action: Action) -> bool:
    """Return True when the action's desired post-state already holds.

    Used by `apply_install` to classify actions as no-ops on a repeat
    install rather than re-applying them and creating a fresh backup
    directory. The check is conservative: any uncertainty (file content
    mismatch, missing source, exception) returns False so the action
    runs through its normal handler.
    """
    try:
        if action.kind == "symlink":
            if not action.target or not action.source:
                return False
            return (
                action.target.is_symlink()
                and Path(os.readlink(action.target)) == action.source
            )
        if action.kind == "render-launcher":
            # Legacy install action. The action is a no-op shim; treat as
            # already applied so the install loop skips it cleanly.
            return True
        if action.kind == "vendor-runtime":
            # Never short-circuit. The vendored runtime is a SNAPSHOT of the
            # installed package's assets; a valid MANIFEST only proves that
            # snapshot is intact, NOT that it matches the currently installed
            # package. After `pip install --upgrade metaensemble`, the old
            # snapshot still has a valid MANIFEST but ships pre-upgrade
            # assets, so silently skipping here would leave users on the old
            # runtime. Re-vendor on every user-setup; the GC keeps version
            # churn bounded.
            return False
        if action.kind == "convert-agent":
            if not action.target or not action.source or not action.source.exists():
                return False
            if not action.target.exists():
                return False
            expected = convert_agent_to_role(action.source.read_text())
            return action.target.read_text() == expected
        if action.kind == "install-curated-role":
            if not action.target or not action.source:
                return False
            if not action.target.exists() or not action.source.exists():
                return False
            return action.target.read_bytes() == action.source.read_bytes()
        if action.kind == "merge-settings":
            if not action.target or not action.target.exists():
                return False
            try:
                existing = json.loads(action.target.read_text())
            except json.JSONDecodeError:
                return False
            existing_hooks = existing.get("hooks") or {}
            install_home = action.target.parent.parent
            wanted_entries = _metaensemble_hook_entries(home=install_home)
            wanted_statusline = _metaensemble_statusline_entry(home=install_home)
            # Every wanted entry must already be present...
            for event, entries in wanted_entries.items():
                event_list = existing_hooks.get(event) or []
                for entry in entries:
                    if entry not in event_list:
                        return False
            if existing.get("statusLine") != wanted_statusline:
                return False
            # ...and there must be no stale MetaEnsemble entries from a
            # previous install format. Without this, an upgrade can
            # leave old direct-Python entries alongside launcher entries
            # because the idempotency check sees the new entries present
            # and reports noop.
            for event, groups in existing_hooks.items():
                if not isinstance(groups, list):
                    continue
                wanted_for_event = wanted_entries.get(event, [])
                for group in groups:
                    for hook in (group.get("hooks") or []):
                        cmd = hook.get("command", "")
                        if not _is_metaensemble_hook_command(cmd):
                            continue
                        # This entry is ours; is it one of the entries
                        # we WANT to install now? If not, it is stale
                        # and the action needs to re-fire to clean up.
                        matches_wanted = any(
                            group.get("matcher") == w.get("matcher")
                            and hook in (w.get("hooks") or [])
                            for w in wanted_for_event
                        )
                        if not matches_wanted:
                            return False
            return True
    except Exception:
        # Any uncertainty about the comparison means "treat as not-yet-applied"
        # so the handler runs and we never silently miss a needed change.
        return False
    return False


def apply_install(
    plan: InstallPlan,
    project: Path | None = None,
    dry_run: bool = False,
    user_scope_only: bool = False,
    home: Path | None = None,
) -> InstallReport:
    """Execute the actions in a plan. Returns a report.

    With `dry_run=True`, no filesystem changes happen; the returned report
    lists what would have been applied.

    Idempotency contract: when every action's desired post-state already
    holds (a re-run of a successful install), no backup directory is
    created and the report's `applied` list is empty; the `noop` list
    enumerates the actions that were skipped because they were already
    in effect. This lets the CLI print `Unchanged.` instead of a
    misleading `Applied N action(s)`.
    """
    project = project or Path.cwd()

    # `plan_install` bakes the backup_path into each Action at plan time
    # using the project-level backup root. For `user_scope_only`
    # invocations the real backup root is the user-level one, so we
    # remap each backup_path BEFORE the idempotency check / apply loop —
    # the on-disk plan.json, per-action backup writes, and `user-teardown`
    # all need the corrected location.
    if user_scope_only:
        plan = remap_user_scope_backup_paths(plan, project=project, home=home)

    # Pre-classify actions so we know whether to create a backup directory
    # at all. The classification is read-only and cheap (file stat + small
    # reads); skipping a backup when nothing will change is the user-
    # visible idempotency improvement.
    if not dry_run:
        already_applied = [a for a in plan.actions if _action_already_applied(a)]
        needs_apply = [a for a in plan.actions if a not in already_applied]
    else:
        already_applied = []
        needs_apply = list(plan.actions)

    backup_root: Path | None = None
    # When user_scope_only=False (the project-level apply), `vendor-runtime`
    # is the one action that always lives in needs_apply. Never short-circuit
    # it, so pip-upgrade flows cannot leave stale assets in the runtime.
    # Its rollback is the previous version dir under runtime-versions/ (GC
    # owns retention), so it does NOT need an entry in the project's
    # backups/ tree. Decide on backup-dir creation against the actions that
    # actually need a backup, not just `needs_apply`.
    needs_backup = (
        needs_apply
        if user_scope_only
        else [a for a in needs_apply if a.kind != "vendor-runtime"]
    )
    if not dry_run and needs_backup:
        # Only mint a backup directory when something actually needs to be
        # written. A repeat install where every non-vendor-runtime action
        # is a noop leaves no new directory behind. `user_scope_only=True`
        # writes the backup under `~/.metaensemble/installs/<timestamp>/`
        # so that `user-teardown` can later reverse the integration
        # without depending on any one project's `.metaensemble/backups/`.
        backup_root = (
            _user_backup_root(home, plan.timestamp)
            if user_scope_only
            else _backup_root(project, plan.timestamp)
        )
        backup_root.mkdir(parents=True, exist_ok=True)
        manifest_path = backup_root / "plan.json"
        manifest_path.write_text(json.dumps(_serializable_plan(plan), indent=2))
        # Initialize the project state directory idempotently. Without
        # this, the doctor's C4 check fires WARN immediately after a
        # fresh install, and the very first hook invocation has to
        # create the state directory under unclear circumstances. Doing
        # it here makes install the canonical "ready to dispatch" point.
        # User-scope-only invocations skip this — `user-setup` must not
        # leave a footprint in whatever cwd it ran from.
        if not user_scope_only:
            _ensure_project_state(project)
    elif not dry_run and not user_scope_only:
        # Even on a pure-noop run, make sure the project state directory
        # exists. This is the "doctor stays green after every install"
        # invariant; the call is idempotent and cheap.
        _ensure_project_state(project)

    applied: list[Action] = []
    noop: list[Action] = list(already_applied)
    errors: list[tuple[Action, str]] = []
    for action in needs_apply:
        if dry_run:
            applied.append(action)
            continue
        try:
            if action.kind == "symlink":
                _do_symlink(action)
            elif action.kind == "vendor-runtime":
                # Apply against the install's home (recoverable from the
                # action's target path: ~/.metaensemble/runtime → ~).
                vendor_home = (
                    action.target.parent.parent
                    if action.target else None
                )
                _do_vendor_runtime(action, home=vendor_home)
            elif action.kind == "render-launcher":
                # Legacy no-op shim — see _do_render_launcher.
                _do_render_launcher(action)
            elif action.kind == "convert-agent":
                _do_convert_agent(action)
            elif action.kind == "install-curated-role":
                _do_install_curated_role(action)
            elif action.kind == "merge-settings":
                _do_merge_settings(action)
            else:
                errors.append((action, f"unknown action kind: {action.kind}"))
                continue
            applied.append(action)
        except Exception as exc:
            errors.append((action, str(exc)))

    # Write active-roles.yaml capturing which curated Roles are active.
    # Skip the rewrite when the file already matches what we would write,
    # so a repeat install doesn't churn the mtime of an unchanged file.
    # User-scope-only invocations skip this entirely — active-roles.yaml
    # is a per-project artifact and user-setup must not write to cwd.
    if not dry_run and not user_scope_only:
        active_roles_path = _project_metaensemble_dir(project) / "active-roles.yaml"
        active_roles_path.parent.mkdir(parents=True, exist_ok=True)
        active_roles_content = {
            "active_roles": list(plan.active_roles),
            "inactive_roles": list(plan.inactive_roles),
            "set_at": plan.timestamp,
        }
        new_text = yaml.dump(
            active_roles_content, default_flow_style=False, sort_keys=False
        )
        existing_text = (
            active_roles_path.read_text() if active_roles_path.exists() else None
        )
        # The `set_at` timestamp changes on every run; compare on the
        # role-list payload (excluding `set_at`) to decide whether a real
        # change happened. The file is rewritten only when the payload
        # itself moved, preserving the install's idempotency promise.
        if existing_text is not None:
            try:
                existing_payload = yaml.safe_load(existing_text) or {}
                payload_unchanged = (
                    list(existing_payload.get("active_roles", []))
                        == active_roles_content["active_roles"]
                    and list(existing_payload.get("inactive_roles", []))
                        == active_roles_content["inactive_roles"]
                )
            except yaml.YAMLError:
                payload_unchanged = False
            if not payload_unchanged:
                active_roles_path.write_text(new_text)
        else:
            active_roles_path.write_text(new_text)

    return InstallReport(
        applied=applied,
        skipped=[],
        noop=noop,
        errors=errors,
        backup_root=backup_root,
    )


def _serializable_plan(plan: InstallPlan) -> dict[str, Any]:
    return {
        "layout": plan.layout.value,
        "timestamp": plan.timestamp,
        "active_roles": list(plan.active_roles),
        "inactive_roles": list(plan.inactive_roles),
        "actions": [
            {
                "kind": a.kind,
                "source": str(a.source) if a.source else None,
                "target": str(a.target) if a.target else None,
                "description": a.description,
                "backup_path": str(a.backup_path) if a.backup_path else None,
            }
            for a in plan.actions
        ],
    }


# --- Cross-project discovery --------------------------------------------


@dataclass(frozen=True)
class DiscoveredProject:
    """One project on the user's machine with a `.metaensemble/` directory.

    Discovery walks the Claude Code runtime's project registry
    (`~/.claude/projects/<encoded-cwd>/`) to find every project the
    user has opened a session in, then checks each one for an
    accompanying `.metaensemble/` to determine install status.
    """

    path: Path
    has_metaensemble_dir: bool
    has_ledger_db: bool
    run_count: int = 0
    last_run_ts: str | None = None


def _cwd_from_runtime_project_dir(proj_dir: Path) -> Path | None:
    """Read the project's cwd directly from any session jsonl in the dir.

    Avoids inverting the runtime's lossy `/`→`-` encoding (which also
    collapses dots and spaces and so cannot be inverted in general).
    Every session event the runtime writes carries a top-level `cwd`
    field; we pull that out of the first usable line we find. Returns
    None if no jsonl can be read or none contains a cwd.
    """
    try:
        for jsonl in proj_dir.glob("*.jsonl"):
            try:
                with jsonl.open() as fp:
                    for line in fp:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(event, dict) and isinstance(event.get("cwd"), str):
                            return Path(event["cwd"])
            except OSError:
                continue
    except OSError:
        pass
    return None


def discover_projects(home: Path | None = None) -> list[DiscoveredProject]:
    """List every project the runtime knows about, with install status.

    The result is sorted by most-recent run first (when known), so a
    `metaensemble projects` listing surfaces the active projects at
    the top.
    """
    home = home or Path.home()
    projects_root = home / ".claude" / "projects"
    if not projects_root.is_dir():
        return []

    out: list[DiscoveredProject] = []
    seen_paths: set[Path] = set()
    for proj_dir in projects_root.iterdir():
        if not proj_dir.is_dir():
            continue
        path = _cwd_from_runtime_project_dir(proj_dir)
        if path is None:
            continue
        # Skip the user's home dir itself — `~/.metaensemble/` is the
        # user-level layer the launcher and roles live in, not a
        # MetaEnsemble-installed project. Showing it as a "project"
        # would mislead.
        if path == home:
            continue
        if path in seen_paths:
            continue
        seen_paths.add(path)
        me_dir = path / ".metaensemble"
        has_me = me_dir.is_dir()
        ledger = me_dir / "state" / "department.db"
        has_ledger = ledger.is_file()
        run_count = 0
        last_run_ts: str | None = None
        if has_ledger:
            try:
                import sqlite3
                conn = sqlite3.connect(str(ledger))
                row = conn.execute(
                    "SELECT COUNT(*), MAX(ended_ts) FROM runs"
                ).fetchone()
                conn.close()
                run_count = int(row[0]) if row and row[0] is not None else 0
                last_run_ts = row[1] if row and row[1] else None
            except Exception:  # nosec B110
                # Project discovery is best-effort. A corrupt Ledger should
                # hide that project's run count, not break the setup wizard.
                pass
        out.append(DiscoveredProject(
            path=path,
            has_metaensemble_dir=has_me,
            has_ledger_db=has_ledger,
            run_count=run_count,
            last_run_ts=last_run_ts,
        ))

    out.sort(key=lambda p: (p.last_run_ts or "", p.run_count), reverse=True)
    return out


def prune_missing_projects(home: Path | None = None) -> list[Path]:
    """Remove Claude Code project registry entries whose cwd no longer exists."""
    home = home or Path.home()
    projects_root = home / ".claude" / "projects"
    if not projects_root.is_dir():
        return []
    removed: list[Path] = []
    for proj_dir in projects_root.iterdir():
        if not proj_dir.is_dir():
            continue
        path = _cwd_from_runtime_project_dir(proj_dir)
        if path is None or path.exists():
            continue
        shutil.rmtree(proj_dir)
        removed.append(path)
    return removed


def render_projects(projects: list[DiscoveredProject]) -> str:
    """Markdown rendering for the `metaensemble projects` CLI."""
    if not projects:
        return (
            "## MetaEnsemble projects\n\n"
            "No Claude Code projects found under `~/.claude/projects/`. "
            "Open a session in a project to register it; then run "
            "`metaensemble setup` to choose a project and adopt it."
        )
    lines = [
        "## MetaEnsemble projects",
        "",
        "Every project Claude Code has seen, with MetaEnsemble install status. "
        "Run `metaensemble setup` for the interactive wizard, or `cd <path>` "
        "then `metaensemble adopt` to register a specific project; per-project "
        "state stays isolated under `<path>/.metaensemble/`.",
        "",
        "| Status | Runs | Last run | Path |",
        "|---|---|---|---|",
    ]
    for p in projects:
        if p.has_ledger_db:
            status = "installed"
        elif p.has_metaensemble_dir:
            status = "init-only"
        else:
            status = "not installed"
        last = p.last_run_ts[:19] if p.last_run_ts else "—"
        runs = str(p.run_count) if p.has_ledger_db else "—"
        lines.append(f"| {status} | {runs} | {last} | `{p.path}` |")
    return "\n".join(lines)


# --- Uninstall -----------------------------------------------------------


def _all_backup_roots(project: Path) -> list[Path]:
    """Return every install's backup root for this project, OLDEST first.

    Each `metaensemble adopt` run writes a fresh `<project>/.metaensemble/
    backups/<timestamp>/` directory with that install's plan.json. To
    reverse a project's MetaEnsemble adoption to a truly pre-install state
    we walk every backup, not just the most recent one (the most recent
    one only captures the delta from the previous install, which is not
    necessarily clean).
    """
    backups_dir = _project_metaensemble_dir(project) / "backups"
    if not backups_dir.is_dir():
        return []
    return sorted([p for p in backups_dir.iterdir() if p.is_dir()])


def _all_user_backup_roots(home: Path | None = None) -> list[Path]:
    """Return every user-setup's backup root, OLDEST first.

    `user-setup` writes `~/.metaensemble/installs/<timestamp>/plan.json`;
    `user-teardown` walks these in reverse to reverse the user-level
    integration. Mirrors `_all_backup_roots` for the user scope.
    """
    installs_dir = _user_metaensemble_dir(home) / "installs"
    if not installs_dir.is_dir():
        return []
    return sorted([p for p in installs_dir.iterdir() if p.is_dir()])


def _reverse_effect_key(
    *,
    kind: str,
    target: Path | None,
    source: Path | None,
    scope: str,
) -> tuple[str, str, str, str]:
    """Identity for teardown effects across stacked install records.

    Re-running setup can leave multiple plan.json records with the same
    user-visible reversal. Teardown should reverse that effect once, keyed
    by scope, action kind, canonical target, and managed ownership.
    """
    canonical_target = ""
    if target is not None:
        canonical_target = str(target.expanduser().resolve(strict=False))

    ownership = "managed"
    if kind == "symlink" and target is not None and source is not None:
        if target.exists() or target.is_symlink():
            ownership = (
                "managed-symlink"
                if _is_managed_symlink(target, source)
                else "unmanaged-symlink"
            )
        else:
            ownership = "managed-symlink"
    elif kind == "merge-settings":
        ownership = "managed-settings"
    elif kind == "vendor-runtime":
        ownership = "managed-runtime"
    elif kind in _PROJECT_SCOPE_ACTION_KINDS:
        ownership = "managed-project"

    return (scope, kind, canonical_target, ownership)


def _latest_backup_root(project: Path) -> Path | None:
    """Most recent install's backup root. Kept for back-compat callers."""
    candidates = _all_backup_roots(project)
    return candidates[-1] if candidates else None


def detect_user_layout(home: Path | None = None) -> Layout | None:
    """Infer which `--layout` user-setup ran in, or None when it hasn't.

    Reads filesystem state under `~/.claude/`:
      - `~/.claude/commands/metaensemble/` exists → namespaced layout
        slash commands).
      - `~/.claude/commands/dispatch.md` exists as a symlink whose target
        lives under the MetaEnsemble repo → top-level layout
        slash commands).
      - Neither → user-setup has not been run.

    This is the predicate `cmd_adopt` uses to refuse-with-hint when the
    user calls adopt before user-setup, and to choose the layout for the
    project plan.
    """
    home_dir = home or Path.home()
    commands = home_dir / ".claude" / "commands"
    if (commands / "metaensemble").exists():
        return Layout.NAMESPACED
    top_level = commands / "dispatch.md"
    if top_level.exists() and top_level.is_symlink():
        # Verify the symlink points at MetaEnsemble's repo to avoid
        # mistaking a user-authored /dispatch command for our install.
        try:
            target = top_level.resolve(strict=False)
            if "metaensemble/commands" in str(target) or "metaensemble" in str(target).lower():
                return Layout.TOP_LEVEL
        except OSError:
            pass
    return None


detect_user_mode = detect_user_layout


def project_has_install_actions(project: Path | None = None) -> bool:
    """True when the project has at least one applied install on record.

    Used by the dry-run uninstall path to distinguish "this project will
    actually reverse user-runtime integration" from "the residue scanner
    found integration installed by some other project, which this
    uninstall will not touch". An install records its plan under
    `<project>/.metaensemble/backups/<timestamp>/plan.json`; the presence
    of at least one such backup root is the signal that an uninstall
    invocation here has something to reverse.
    """
    return bool(_all_backup_roots(project or Path.cwd()))


def _strip_metaensemble_statusline(data: dict[str, Any]) -> bool:
    """Remove MetaEnsemble's statusline entry from settings.json if present.

    Identifies our entry by the metaensemble repo path or the resilient
    launcher path in the command string. Other (non-MetaEnsemble)
    statusline entries are preserved.
    Returns True if a change was made.
    """
    status = data.get("statusLine")
    if isinstance(status, dict) and _is_metaensemble_hook_command(str(status.get("command", ""))):
        data.pop("statusLine", None)
        return True
    return False


def _is_metaensemble_hook_command(command: str) -> bool:
    """Recognize commands MetaEnsemble installed in settings.json.

    Three patterns ship across versions:

    - **Runtime runner** (current):
      `.metaensemble/runtime/bin/me-run hook <script>`
    - **Legacy launcher**:
      `.metaensemble/bin/me-run hook <script>`
    - **Direct form** (legacy fallback when no launcher existed):
      `<sys.executable> <package>/hooks/<script>.py` — matched by the
      installed package's parent path being a substring of the command.

    Any of the three counts so legacy-cleanup during user-setup removes
    every form. Other tools' hooks contain none of these markers.
    """
    lowered = command.lower()
    me_marker = str(CORE_DIR.parent).lower()
    if me_marker in lowered:
        return True
    # Current runtime runner path.
    if "/.metaensemble/runtime/bin/me-run" in lowered:
        return True
    # Legacy launcher path.
    if "/.metaensemble/bin/me-run" in lowered:
        return True
    return False


def _strip_metaensemble_hooks(target: Path) -> bool:
    """Remove MetaEnsemble hook entries from a settings.json file in place.

    Identifies our hooks by the launcher path or the metaensemble repo
    path that the installer embedded in each command string. Returns
    True if the file changed. The function is conservative: it only
    removes hook groups whose every command matches MetaEnsemble's
    pattern, leaving any user-authored hooks (or hooks from other
    tools) untouched.
    """
    if not target.exists():
        return False
    try:
        data = json.loads(target.read_text())
    except json.JSONDecodeError:
        return False
    hooks = data.get("hooks")
    changed = False
    if isinstance(hooks, dict):
        for event, groups in list(hooks.items()):
            if not isinstance(groups, list):
                continue
            new_groups: list[dict[str, Any]] = []
            for group in groups:
                inner = group.get("hooks") or []
                kept = [h for h in inner if not _is_metaensemble_hook_command(h.get("command", ""))]
                if not kept:
                    changed = True
                    continue
                if len(kept) != len(inner):
                    changed = True
                group["hooks"] = kept
                new_groups.append(group)
            if new_groups:
                hooks[event] = new_groups
            else:
                del hooks[event]
                changed = True
        if not hooks:
            data.pop("hooks", None)
            changed = True
    if _strip_metaensemble_statusline(data):
        changed = True
    if changed:
        target.write_text(json.dumps(data, indent=2) + "\n")
    return changed


def _managed_symlink_target(path: Path) -> Path | None:
    """Return the resolved target for a symlink without requiring it to exist."""
    if not path.is_symlink():
        return None
    raw = Path(os.readlink(path))
    target = raw if raw.is_absolute() else path.parent / raw
    return target.resolve(strict=False)


def _is_managed_symlink(path: Path, expected_target: Path) -> bool:
    """True when `path` is a symlink installed by MetaEnsemble.

    Three patterns accepted (current + two legacy):
      - Resolved target equals `expected_target.resolve(strict=False)`.
        This is the current path (expected_target inside the
        vendored runtime).
      - Resolved target lives inside the installed package directory
        (CORE_DIR). Older installs created symlinks straight into
        the source tree; the residue scanner must still recognize them
        so legacy installs can be cleaned up.
      - Resolved target lives inside `~/.metaensemble/runtime-versions/`.
        Same as the inspection filter's signature check — recognizes the
        symlink as managed even when the runtime is mid-vendor.
    """
    actual = _managed_symlink_target(path)
    if actual is None:
        return False
    if actual == expected_target.resolve(strict=False):
        return True
    # Legacy: target inside the installed package directory.
    try:
        actual.relative_to(CORE_DIR.resolve())
        return True
    except ValueError:
        pass
    # Vendored runtime signature.
    parts = actual.parts
    for i in range(len(parts) - 1):
        if parts[i] == ".metaensemble" and parts[i + 1] == "runtime-versions":
            return True
    return False


def _managed_user_runtime_symlink_candidates(home: Path | None = None) -> list[tuple[Path, Path]]:
    """Every user-runtime symlink name MetaEnsemble may have installed.

    Namespaced layout installs a namespaced command directory and prefixed output
    styles. Top-level layout installs per-command and unprefixed output style
    symlinks. A user can move between layouts or uninstall from a different
    project than the one that originally created a user-level symlink, so the
    purge path has to know about all managed names.

    Current expected targets resolve through `~/.metaensemble/runtime/`
    (the vendored runtime). _is_managed_symlink calls `resolve(strict=False)`
    on both sides, so they match whether or not the runtime symlink is
    populated.
    """
    runtime = _user_runtime_dir(home)
    runtime_root = _runtime_root(home)
    candidates: list[tuple[Path, Path]] = [
        (
            runtime / COMMANDS_SUBDIR / "metaensemble",
            runtime_root / "commands",
        ),
        (
            runtime / SKILLS_SUBDIR / "metaensemble-protocol",
            runtime_root / "skills" / "metaensemble-protocol",
        ),
    ]
    commands_dir = CORE_DIR / "commands"
    if commands_dir.exists():
        for command_md in sorted(commands_dir.glob("*.md")):
            if not _is_canonical_curated_name(command_md.stem):
                continue
            candidates.append((
                runtime / COMMANDS_SUBDIR / command_md.name,
                runtime_root / "commands" / command_md.name,
            ))
    for style in ("wire", "deliverable"):
        source = runtime_root / "output-styles" / f"{style}.md"
        candidates.append((
            runtime / OUTPUT_STYLES_SUBDIR / f"metaensemble-{style}.md",
            source,
        ))
        candidates.append((
            runtime / OUTPUT_STYLES_SUBDIR / f"{style}.md",
            source,
        ))
    return candidates


def remediate_stale_managed_symlinks(home: Path | None = None) -> list[Path]:
    """Remove managed dangling symlinks left by retired runtime artifacts.

    When a previously-installed runtime artifact (a slash-command file, an
    output style, the namespaced commands directory) is renamed or removed
    in a later release, the old symlink in `~/.claude/` remains pointing at
    a now-deleted target. The current install plan doesn't reference the old
    name and so never touches it, but a `metaensemble doctor` run with that
    symlink dangling causes confusion. This function proactively scans the
    user-runtime layout for managed symlinks whose targets no longer exist
    and removes them.

    `Managed` here means the same predicate `_is_managed_symlink` accepts:
    the symlink resolves into the vendored runtime, a vendored runtime
    version, or the installed package directory. Anything else is treated
    as user-authored and left alone.

    Returns the list of removed symlink paths so callers can surface them
    in install reports.
    """
    home = home or Path.home()
    runtime = home / RUNTIME_DIR_NAME
    if not runtime.is_dir():
        return []

    removed: list[Path] = []
    scan_dirs = [runtime / COMMANDS_SUBDIR, runtime / OUTPUT_STYLES_SUBDIR]
    namespaced = runtime / COMMANDS_SUBDIR / "metaensemble"
    if namespaced.is_dir():
        scan_dirs.append(namespaced)
    skills_dir = runtime / SKILLS_SUBDIR / "metaensemble-protocol"

    for scan in scan_dirs:
        if not scan.is_dir():
            continue
        try:
            entries = list(scan.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_symlink():
                continue
            target = _managed_symlink_target(entry)
            if target is None:
                continue
            # The symlink is managed (target resolves into our managed
            # tree) AND its target does not exist on disk: that is the
            # stale-managed pattern this function targets.
            looks_managed = False
            try:
                target.relative_to(CORE_DIR.resolve())
                looks_managed = True
            except ValueError:
                pass
            if not looks_managed:
                parts = target.parts
                for i in range(len(parts) - 1):
                    if parts[i] == ".metaensemble" and parts[i + 1] in (
                        "runtime", "runtime-versions"
                    ):
                        looks_managed = True
                        break
            if not looks_managed:
                continue
            if target.exists():
                continue
            try:
                entry.unlink()
                removed.append(entry)
            except OSError:
                continue

    # The metaensemble-protocol skill is a symlink to a directory; if that
    # target is gone, drop the symlink so user-setup's reinstall does not
    # collide with a dangling entry.
    if skills_dir.is_symlink():
        target = _managed_symlink_target(skills_dir)
        if target is not None and not target.exists():
            try:
                skills_dir.unlink()
                removed.append(skills_dir)
            except OSError:
                pass

    return removed


def _purge_user_runtime_integration(home: Path | None = None) -> list[Path]:
    """Remove managed `~/.claude` artifacts left by any install layout.

    This is deliberately symlink-only for commands, skills, and output styles:
    if a user-authored file occupies one of those names, purge reports it as
    residue rather than deleting it. Settings are handled surgically by
    `_strip_metaensemble_hooks`, preserving non-MetaEnsemble hooks and keys.
    """
    removed: list[Path] = []
    for path, source in _managed_user_runtime_symlink_candidates(home):
        try:
            if _is_managed_symlink(path, source):
                path.unlink()
                removed.append(path)
        except OSError:
            continue
    settings_path = _user_runtime_dir(home) / SETTINGS_FILE
    if _strip_metaensemble_hooks(settings_path):
        removed.append(settings_path)
    return removed


def _user_runtime_integration_residue(home: Path | None = None) -> list[Path]:
    """Managed `~/.claude` integration still present after uninstall."""
    remaining: list[Path] = []
    for path, source in _managed_user_runtime_symlink_candidates(home):
        if _is_managed_symlink(path, source):
            remaining.append(path)
    settings_path = _user_runtime_dir(home) / SETTINGS_FILE
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            data = {}
        if _settings_has_metaensemble_integration(data):
            remaining.append(settings_path)
    return sorted(remaining)


def _settings_has_metaensemble_integration(data: dict[str, Any]) -> bool:
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for groups in hooks.values():
            if not isinstance(groups, list):
                continue
            for group in groups:
                for hook in (group.get("hooks") or []):
                    if _is_metaensemble_hook_command(hook.get("command", "")):
                        return True
    status = data.get("statusLine")
    if isinstance(status, dict):
        command = str(status.get("command", ""))
        if _is_metaensemble_hook_command(command):
            return True
    return False


def _strip_metaensemble_gitignore_block(project: Path) -> bool:
    """Remove the managed `.metaensemble/` block from the project's `.gitignore`.

    Returns True if a change was made. The managed block is identified by
    the exact wording in `_GITIGNORE_NEW_FILE` / `_GITIGNORE_MANAGED_BLOCK`
    (comment + the `.metaensemble/` line). User-edited lines are preserved.
    If after removal the file would be empty (only whitespace), it is
    deleted so we don't leave an empty `.gitignore` behind that we
    ourselves created.
    """
    gitignore = project / ".gitignore"
    if not gitignore.exists():
        return False
    try:
        original = gitignore.read_text()
    except OSError:
        return False
    lines = original.splitlines(keepends=True)
    out: list[str] = []
    skip_next_metaensemble_line = False
    me_comment_seen = False
    for line in lines:
        stripped = line.strip()
        # Drop both lines of the managed comment, then the `.metaensemble/`
        # rule that follows it.
        if "MetaEnsemble: per-project working directory" in stripped:
            me_comment_seen = True
            skip_next_metaensemble_line = True
            continue
        if me_comment_seen and stripped.startswith("# survey output"):
            continue
        if me_comment_seen and stripped.startswith("# inspection output"):
            continue
        if me_comment_seen and stripped.startswith("# Re-derivable"):
            continue
        if skip_next_metaensemble_line and stripped in (
            ".metaensemble", ".metaensemble/", "/.metaensemble", "/.metaensemble/",
        ):
            skip_next_metaensemble_line = False
            me_comment_seen = False
            continue
        if stripped in _LEGACY_GITIGNORE_ENTRIES:
            continue
        # Defensive: if no comment was seen, still strip a bare
        # `.metaensemble/` rule that obviously belongs to us.
        if not me_comment_seen and stripped in (
            ".metaensemble", ".metaensemble/", "/.metaensemble", "/.metaensemble/",
        ):
            continue
        out.append(line)
    new = "".join(out).rstrip("\n")
    if not new.strip():
        # The file existed only because of MetaEnsemble; clear the file out.
        try:
            gitignore.unlink()
            return True
        except OSError:
            return False
    if new + "\n" != original:
        gitignore.write_text(new + "\n")
        return True
    return False


@dataclass(frozen=True)
class ResidueReport:
    """What remains after `uninstall` completes — guides Principal next steps."""

    project_state_remaining: list[Path] = field(default_factory=list)
    user_state_remaining: list[Path] = field(default_factory=list)
    user_runtime_remaining: list[Path] = field(default_factory=list)
    package_install_command: str | None = None
    notes: list[str] = field(default_factory=list)


def _project_state_residue(project: Path) -> list[Path]:
    base = project / ".metaensemble"
    if not base.exists():
        return []
    return sorted(p for p in base.iterdir())


def _user_state_residue(home: Path | None = None) -> list[Path]:
    base = (home or Path.home()) / ".metaensemble"
    if not base.exists():
        return []
    return sorted(p for p in base.iterdir())


def purge_project_state(
    project: Path | None = None,
    *,
    home: Path | None = None,
) -> list[Path]:
    """Delete `<project>/.metaensemble/` entirely. Returns the paths removed.

    Used by `metaensemble unadopt --purge-state`. Idempotent:
    if the directory is already absent, returns an empty list.
    """
    project = project or Path.cwd()
    base = project / ".metaensemble"
    if not base.exists():
        return []
    removed = _project_state_residue(project)
    _archive_project_inspection_artifacts(project, home=home)
    shutil.rmtree(base, ignore_errors=False)
    return removed


def purge_user_state(home: Path | None = None) -> list[Path]:
    """Delete `~/.metaensemble/` entirely. Returns the paths removed.

    Used by `metaensemble user-teardown --purge-state`. Idempotent.
    """
    base = (home or Path.home()) / ".metaensemble"
    if not base.exists():
        return []
    removed = _user_state_residue(home)
    shutil.rmtree(base, ignore_errors=False)
    return removed


def uninstall(
    project: Path | None = None,
    restore: bool = False,
    home: Path | None = None,
    purge_project_state_flag: bool = False,
    purge_user_state_flag: bool = False,
    scope: str = "both",
    dry_run: bool = False,
) -> InstallReport:
    """Remove MetaEnsemble's installed pieces; optionally restore from backups.

    Reverses every install of MetaEnsemble in this project, not just the
    most recent. Each `metaensemble adopt` run wrote a backup root with
    its own plan.json; we walk every plan in reverse chronological order
    and reverse each action. With `restore=True`:
      - `merge-settings` is reversed using the OLDEST settings.json.bak
        on disk, since that is the snapshot taken before MetaEnsemble
        ever touched the file. Restoring from a later backup would carry
        forward MetaEnsemble hooks that this uninstall is meant to remove.
      - `convert-agent` actions restore each backed-up agent file to its
        original location. Only the first install's plan has these; later
        installs were no-ops for the agent set because the agents had
        already been moved.
    Without `restore=True`:
      - `merge-settings` is reversed by surgically stripping
        MetaEnsemble's hook entries from the live settings.json,
        leaving any other hooks the user has added in place.

    Project gitignore block is always reversed (project-tree pollution).

    The `purge_*` flags optionally extend the cleanup. They are off by
    default to preserve the existing `--restore-integration` semantics:
    a default uninstall reverses the integration but leaves the Ledger,
    manifests, briefs, inspection outputs, and user-layer files alone so the
    user can re-adopt later without losing data.
      - `purge_project_state_flag=True` — also delete `<project>/.metaensemble/`.
      - `purge_user_state_flag=True` — also delete `~/.metaensemble/`.

    `dry_run=True` walks the same logic but never mutates the filesystem.
    The returned `applied` list still enumerates the actions that WOULD
    have been reversed, letting the CLI preview the work.
    """
    project = project or Path.cwd()
    if scope not in ("both", "project", "user"):
        raise ValueError(f"scope must be one of 'both'/'project'/'user', got {scope!r}")

    # `scope` filters which action kinds get reversed and which side
    # effects (settings.json restore, gitignore strip, purge flags)
    # fire. When scope='project' we leave every user-level artifact in
    # place; when scope='user' we leave every project-level artifact in
    # place. The default 'both' preserves the legacy do-everything path.
    project_in_scope = scope in ("both", "project")
    user_in_scope = scope in ("both", "user")

    backup_roots = _all_backup_roots(project)
    if user_in_scope:
        # User-setup writes to ~/.metaensemble/installs/; include those
        # backup roots so user-scope actions from a clean user-setup are
        # reachable when project-level adoption was never done.
        backup_roots = backup_roots + _all_user_backup_roots(home)
        backup_roots = sorted(set(backup_roots), key=lambda p: p.name)
    if not backup_roots:
        # Edge case: a fully-idempotent install, where every Action was already in effect when the
        # install ran — produces no backup_root, but `_ensure_project_state`
        # still creates the project state subtree, writes budgets.yaml,
        # and appends the managed block to the project's `.gitignore`.
        # Uninstall must reverse the gitignore block even in this branch,
        # otherwise the project tree carries a `.gitignore` that lists
        # `.metaensemble/` long after the install state is gone. Also
        # honor the purge flags here so teardown on a noop-installed
        # project still cleans both state dirs.
        applied_no_backup: list[Action] = []
        if project_in_scope:
            try:
                if dry_run:
                    gitignore = project / ".gitignore"
                    if gitignore.exists() and "metaensemble" in gitignore.read_text().lower():
                        applied_no_backup.append(Action(
                            kind="reverse-seed-gitignore-block",
                            source=None,
                            target=gitignore,
                            description="Remove `.metaensemble/` from project .gitignore",
                        ))
                elif _strip_metaensemble_gitignore_block(project):
                    applied_no_backup.append(Action(
                        kind="reverse-seed-gitignore-block",
                        source=None,
                        target=project / ".gitignore",
                        description="Remove `.metaensemble/` from project .gitignore",
                    ))
            except Exception:  # nosec B110
                # Dry-run uninstall is a preview surface. If the existing
                # .gitignore cannot be inspected, later doctor/uninstall paths
                # report the residue rather than blocking teardown planning.
                pass
        if purge_project_state_flag and project_in_scope:
            paths = (
                _project_state_residue(project) if dry_run
                else purge_project_state(project, home=home)
            )
            for path in paths:
                applied_no_backup.append(Action(
                    kind="purge-project-state",
                    source=None, target=path,
                    description=f"Purge `{path}`",
                ))
        if purge_user_state_flag and user_in_scope:
            runtime_paths = (
                _user_runtime_integration_residue(home) if dry_run
                else _purge_user_runtime_integration(home)
            )
            for path in runtime_paths:
                applied_no_backup.append(Action(
                    kind="purge-user-runtime-integration",
                    source=None, target=path,
                    description=f"Purge managed user runtime artifact `{path}`",
                ))
            user_paths = (
                _user_state_residue(home) if dry_run
                else purge_user_state(home)
            )
            for path in user_paths:
                applied_no_backup.append(Action(
                    kind="purge-user-state",
                    source=None, target=path,
                    description=f"Purge `{path}`",
                ))
        return InstallReport(
            applied=applied_no_backup, skipped=[], errors=[], backup_root=None,
        )

    applied: list[Action] = []
    errors: list[tuple[Action, str]] = []

    # Settings restore is special: every merge-settings action backed up the
    # settings.json that existed at install time. The OLDEST backup that
    # contains no MetaEnsemble references is the only one that predates
    # MetaEnsemble entirely; any later backup carries forward our hooks.
    # If no clean backup exists (first install had no prior settings.json
    # to back up, so no bak file was created), we fall back to surgical
    # removal of MetaEnsemble's hook entries from the live file.
    # A backup file is "clean" if it predates MetaEnsemble entirely — no
    # repo-path or launcher-path substring anywhere in the file. The
    # OLDEST clean backup is the one we restore from.
    me_marker = str(CORE_DIR.parent).lower()
    legacy_launcher_marker = "/.metaensemble/bin/me-run"
    runtime_launcher_marker = "/.metaensemble/runtime/bin/me-run"
    oldest_clean_settings_backup: Path | None = None
    if restore:
        for root in backup_roots:
            candidate = root / "settings.json.bak"
            if not candidate.exists():
                continue
            try:
                contents = candidate.read_text().lower()
                if (me_marker not in contents
                        and legacy_launcher_marker not in contents
                        and runtime_launcher_marker not in contents):
                    oldest_clean_settings_backup = candidate
                    break  # backup_roots is sorted oldest-first
            except OSError:
                continue

    settings_target_path: Path | None = None  # set on first merge-settings seen
    seen_reverse_effects: set[tuple[str, str, str, str]] = set()

    # Walk every install in reverse so the latest install's effects unwind
    # before any earlier install's effects.
    for backup_root in reversed(backup_roots):
        plan_path = backup_root / "plan.json"
        if not plan_path.exists():
            continue
        plan_data = json.loads(plan_path.read_text())
        for action_dict in plan_data["actions"]:
            kind = action_dict["kind"]
            # Filter by scope: only reverse kinds in the requested scope.
            if kind in _USER_SCOPE_ACTION_KINDS and not user_in_scope:
                continue
            if kind in _PROJECT_SCOPE_ACTION_KINDS and not project_in_scope:
                continue
            target = Path(action_dict["target"]) if action_dict.get("target") else None
            source = Path(action_dict["source"]) if action_dict.get("source") else None
            backup_path = (Path(action_dict["backup_path"])
                           if action_dict.get("backup_path") else None)
            action_scope = "user" if kind in _USER_SCOPE_ACTION_KINDS else "project"
            effect_key = _reverse_effect_key(
                kind=kind,
                target=target,
                source=source,
                scope=action_scope,
            )
            if effect_key in seen_reverse_effects:
                continue
            seen_reverse_effects.add(effect_key)
            reverse = Action(
                kind=f"reverse-{kind}",
                source=source, target=target,
                description=f"Reverse {action_dict['description']}",
                backup_path=backup_path,
            )
            try:
                if kind == "symlink" and target and target.is_symlink():
                    if not dry_run:
                        target.unlink()
                elif kind == "render-launcher":
                    # The launcher lives under ~/.metaensemble/bin/ (user
                    # state) and is the recovery anchor for re-installs.
                    # Per-action reversal would delete it during project
                    # rollback, breaking the next me-run invocation until
                    # bootstrap re-renders it. Defer cleanup to
                    # purge_user_state().
                    if purge_user_state_flag and target and target.exists():
                        if not dry_run:
                            target.unlink()
                elif kind == "convert-agent":
                    if restore and backup_path and backup_path.exists() and source:
                        if not dry_run:
                            _ensure_parent(source)
                            shutil.copy2(backup_path, source)
                    if target and target.exists():
                        if not dry_run:
                            target.unlink()
                elif kind == "merge-settings":
                    settings_target_path = target
                elif kind == "vendor-runtime":
                    # The runtime is a recovery anchor. Default teardown
                    # keeps it; `--purge-state` removes it as part of the
                    # explicit `purge-user-state` phase below. Do not report
                    # a separate reversal for the same path.
                    continue
                applied.append(reverse)
            except Exception as exc:
                errors.append((reverse, str(exc)))

    # Settings reversal happens once, after we have walked every plan, so it
    # does not get applied repeatedly (each install wrote a merge-settings
    # action targeting the same settings.json). Skipped when scope is
    # 'project' since settings.json is user-level.
    if settings_target_path is not None and user_in_scope:
        try:
            if not dry_run:
                if restore and oldest_clean_settings_backup is not None:
                    shutil.copy2(oldest_clean_settings_backup, settings_target_path)
                else:
                    # No clean pre-install backup exists (first install created
                    # settings.json from scratch). Strip MetaEnsemble hooks
                    # surgically so any user-added hooks survive.
                    _strip_metaensemble_hooks(settings_target_path)
        except Exception as exc:
            errors.append((Action(
                kind="reverse-merge-settings",
                source=None,
                target=settings_target_path,
                description="Reverse settings.json hook entries",
            ), str(exc)))

    # Reverse the gitignore block: it is project-tree pollution
    # `_ensure_project_state` created and the user did not opt into. The
    # reversal preserves any non-managed lines the user added. Skipped
    # when scope is 'user' since the gitignore is project-level.
    if project_in_scope:
        try:
            if dry_run:
                # Don't mutate; only check whether the managed block is
                # present so the dry-run count reflects what would happen.
                gitignore = project / ".gitignore"
                if gitignore.exists() and "metaensemble" in gitignore.read_text().lower():
                    applied.append(Action(
                        kind="reverse-seed-gitignore-block",
                        source=None,
                        target=gitignore,
                        description="Remove `.metaensemble/` from project .gitignore",
                    ))
            elif _strip_metaensemble_gitignore_block(project):
                applied.append(Action(
                    kind="reverse-seed-gitignore-block",
                    source=None,
                    target=project / ".gitignore",
                    description="Remove `.metaensemble/` from project .gitignore",
                ))
        except Exception as exc:
            errors.append((Action(
                kind="reverse-seed-gitignore-block",
                source=None,
                target=project / ".gitignore",
                description="Remove `.metaensemble/` from project .gitignore",
            ), str(exc)))

    # Optional purge paths run last so that earlier hooks-and-symlink
    # reversal has had a chance to log into the Ledger / hooks log; once
    # the state directory is gone, those logs are gone too.
    if purge_project_state_flag and project_in_scope:
        try:
            removed = (
                _project_state_residue(project) if dry_run
                else purge_project_state(project, home=home)
            )
            for path in removed:
                applied.append(Action(
                    kind="purge-project-state",
                    source=None,
                    target=path,
                    description=f"Purge `{path}`",
                ))
        except Exception as exc:
            errors.append((Action(
                kind="purge-project-state",
                source=None,
                target=project / ".metaensemble",
                description=f"Purge {project}/.metaensemble/",
            ), str(exc)))

    if purge_user_state_flag and user_in_scope:
        try:
            runtime_paths = (
                _user_runtime_integration_residue(home) if dry_run
                else _purge_user_runtime_integration(home)
            )
            # In dry-run, the residue helper sees every managed artifact
            # currently on disk — including ones already covered by the
            # per-action reversal loop above (reverse-symlink,
            # reverse-merge-settings). At apply time those get unlinked
            # before this step runs, so the actual `_purge_user_runtime_
            # integration` finds them gone and returns very little.
            # Filter the dry-run list to match: only emit purge entries
            # for paths the reversal loop did not already cover.
            if dry_run:
                already_handled = {
                    a.target for a in applied
                    if a.target is not None and a.kind in (
                        "reverse-symlink",
                        "reverse-merge-settings",
                    )
                }
                runtime_paths = [p for p in runtime_paths if p not in already_handled]
            for path in runtime_paths:
                applied.append(Action(
                    kind="purge-user-runtime-integration",
                    source=None,
                    target=path,
                    description=f"Purge managed user runtime artifact `{path}`",
                ))
            removed = (
                _user_state_residue(home) if dry_run
                else purge_user_state(home)
            )
            for path in removed:
                applied.append(Action(
                    kind="purge-user-state",
                    source=None,
                    target=path,
                    description=f"Purge `{path}`",
                ))
        except Exception as exc:
            errors.append((Action(
                kind="purge-user-state",
                source=None,
                target=(home or Path.home()) / ".metaensemble",
                description=f"Purge {(home or Path.home())}/.metaensemble/",
            ), str(exc)))

    return InstallReport(
        applied=applied, skipped=[], errors=errors,
        backup_root=backup_roots[-1] if backup_roots else None,
    )


def build_residue_report(
    project: Path | None = None,
    home: Path | None = None,
) -> ResidueReport:
    """Inspect what remains under the project and user state trees.

    Called after an uninstall to tell the Principal exactly which files
    and directories survived. Used by the CLI to surface the one-line
    `pip uninstall metaensemble` command and any leftover state the user
    can opt to remove themselves.
    """
    project = project or Path.cwd()
    project_remaining = _project_state_residue(project)
    user_remaining = _user_state_residue(home)
    runtime_remaining = _user_runtime_integration_residue(home)
    notes: list[str] = []
    if project_remaining:
        notes.append(
            f"Run `metaensemble unadopt --purge-state` to also remove "
            f"{project}/.metaensemble/."
        )
    if user_remaining:
        notes.append(
            "Run `metaensemble user-teardown --purge-state` to also remove "
            f"{(home or Path.home())}/.metaensemble/."
        )
    if runtime_remaining:
        notes.append(
            "Run `metaensemble user-teardown` to remove managed MetaEnsemble "
            "symlinks and hooks from ~/.claude/."
        )
    return ResidueReport(
        project_state_remaining=project_remaining,
        user_state_remaining=user_remaining,
        user_runtime_remaining=runtime_remaining,
        package_install_command="pip uninstall metaensemble",
        notes=notes,
    )


# --- Plan rendering for --dry-run output ---------------------------------


def render_plan(plan: InstallPlan) -> str:
    """Markdown rendering of a plan, for human review."""
    lines = [
        f"# MetaEnsemble install plan — layout `{plan.layout.value}`",
        "",
        f"Timestamp: {plan.timestamp}",
        f"Actions: {len(plan.actions)}",
        f"Active Roles: {', '.join(plan.active_roles) or '(none)'}",
    ]
    if plan.inactive_roles:
        lines.append(f"Inactive Roles: {', '.join(plan.inactive_roles)}")
    lines.append("")
    for i, action in enumerate(plan.actions, 1):
        lines.append(f"## {i}. {action.kind}")
        lines.append("")
        lines.append(f"- Description: {action.description}")
        if action.source:
            lines.append(f"- Source: `{action.source}`")
        if action.target:
            lines.append(f"- Target: `{action.target}`")
        if action.backup_path:
            lines.append(f"- Backup: `{action.backup_path}`")
        lines.append("")
    return "\n".join(lines)
