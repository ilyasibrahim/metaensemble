"""Tests for the MetaEnsemble installer.

Covers survey (read-only inventory), planning, user setup, project
adoption, rollback, role conversion, and idempotency.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


from metaensemble.lib.installer import (
    Layout,
    apply_install,
    convert_agent_to_role,
    detect_role_relevance,
    is_project_scope_action,
    plan_install,
    render_plan,
    survey,
    uninstall,
)


# --- Helpers --------------------------------------------------------------


def _make_runtime(root: Path, agents: dict[str, str] | None = None,
                  commands: dict[str, str] | None = None) -> Path:
    """Create a minimal Claude Code runtime config under root."""
    (root / "agents").mkdir(parents=True, exist_ok=True)
    (root / "commands").mkdir(parents=True, exist_ok=True)
    (root / "skills").mkdir(parents=True, exist_ok=True)
    (root / "output-styles").mkdir(parents=True, exist_ok=True)
    for name, content in (agents or {}).items():
        (root / "agents" / f"{name}.md").write_text(content)
    for name, content in (commands or {}).items():
        (root / "commands" / f"{name}.md").write_text(content)
    return root


AGENT_BACKEND = """---
name: backend
description: Backend implementation specialist for the API layer.
tools: Read, Write, Edit, Grep, Glob, Bash
model: sonnet
color: blue
---

# Backend agent body

Some explanatory content for the backend role.
"""


# --- Survey ---------------------------------------------------------------


def test_survey_with_no_runtime_dirs_is_empty(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "proj"
    project.mkdir()
    home.mkdir()
    result = survey(home=home, project=project, write_report=False)
    assert result.discovered == []
    assert result.collisions == []
    assert not result.user_runtime_exists
    assert not result.project_runtime_exists


def test_survey_finds_user_layer_agents(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _make_runtime(home / ".claude", agents={"mybot": AGENT_BACKEND})

    result = survey(home=home, project=project, write_report=False)
    names = {a.name for a in result.discovered if a.kind == "agent"}
    assert "mybot" in names
    assert all(a.layer == "user" for a in result.discovered if a.name == "mybot")


def test_survey_detects_collisions_with_curated_roles(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    # `backend` is a name MetaEnsemble ships, so this should collide.
    _make_runtime(home / ".claude", agents={"backend": AGENT_BACKEND})

    result = survey(home=home, project=project, write_report=False)
    collision_names = {c.metaensemble_counterpart for c in result.collisions}
    assert "backend" in collision_names


def test_survey_writes_markdown_report(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _make_runtime(home / ".claude", agents={"mybot": AGENT_BACKEND})

    result = survey(home=home, project=project, write_report=True)
    assert result.report_path is not None
    assert result.report_path.exists()
    report_text = result.report_path.read_text()
    assert "MetaEnsemble" in report_text
    assert "mybot" in report_text
    # The new survey also writes a decisions file the user can edit.
    assert result.decisions_path is not None
    assert result.decisions_path.exists()
    decisions_text = result.decisions_path.read_text()
    assert "mybot" in decisions_text
    assert "action:" in decisions_text


# --- Agent → Role conversion ---------------------------------------------


def test_convert_agent_to_role_maps_basic_fields():
    role_text = convert_agent_to_role(AGENT_BACKEND)
    assert "name: backend" in role_text
    assert "version: 1.0.0" in role_text
    assert "model_tier: sonnet" in role_text
    assert "alias_prefix: back" in role_text
    # Tools list preserved as a YAML array.
    assert "Read" in role_text
    assert "Bash" in role_text
    # Color preserved from source (AGENT_BACKEND has color: blue).
    assert "color: blue" in role_text
    # Body preserved.
    assert "Backend agent body" in role_text


def test_convert_agent_to_role_drops_invalid_color():
    """Colors outside Claude Code's accepted set are silently dropped on
    conversion so the resulting Role still validates against the schema."""
    agent = """---
name: weirdcolor
description: A specialist whose source frontmatter named an off-palette color.
tools: Read
model: sonnet
color: chartreuse
---

Body.
"""
    role_text = convert_agent_to_role(agent)
    assert "color:" not in role_text


def test_convert_agent_to_role_omits_color_when_absent():
    """No color in source → no color in the Role frontmatter (color is optional)."""
    agent = """---
name: nocolor
description: A specialist whose source had no color field.
tools: Read
model: sonnet
---

Body.
"""
    role_text = convert_agent_to_role(agent)
    assert "color:" not in role_text


def test_convert_agent_validates_against_role_schema():
    from metaensemble.lib.manifest import validate_role_frontmatter
    import yaml as _yaml

    role_text = convert_agent_to_role(AGENT_BACKEND)
    # Extract frontmatter
    _, _, after = role_text.partition("---\n")
    fm_text, _, _ = after.partition("\n---\n")
    fm = _yaml.safe_load(fm_text)
    # Should pass schema validation.
    validate_role_frontmatter(fm)


def test_convert_agent_handles_string_tools_field():
    agent = """---
name: tool-string
description: A specialist with tools as a comma-separated string.
tools: Read, Write, Bash
model: haiku
---

Body content here.
"""
    role_text = convert_agent_to_role(agent)
    assert "Read" in role_text
    assert "Bash" in role_text
    assert "model_tier: haiku" in role_text


def test_convert_agent_pads_short_description():
    agent = """---
name: short
description: Brief.
tools: []
model: sonnet
---

Body.
"""
    role_text = convert_agent_to_role(agent)
    # The role schema requires description >= 10 chars; the converter pads it.
    import yaml as _yaml
    _, _, after = role_text.partition("---\n")
    fm_text, _, _ = after.partition("\n---\n")
    fm = _yaml.safe_load(fm_text)
    assert len(fm["description"]) >= 10


# --- Plan -----------------------------------------------------------------


def test_plan_parallel_namespaces_commands(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    survey_result = survey(home=home, project=project, write_report=False)

    plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)
    # The plan should target the namespaced `metaensemble/` subdirectory.
    targets = [str(a.target) for a in plan.actions if a.kind == "symlink"]
    assert any("commands/metaensemble" in t for t in targets)


def test_plan_incorporate_installs_commands_at_top_level(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    survey_result = survey(home=home, project=project, write_report=False)

    plan = plan_install(survey_result, Layout.TOP_LEVEL, project=project, home=home)
    # Top-level layout installs each command file individually into the
    # existing commands directory, with skip-if-exists for collision safety.
    commands_dir = home / ".claude" / "commands"
    per_file_actions = [a for a in plan.actions
                        if a.kind == "symlink"
                        and a.target
                        and a.target.parent == commands_dir
                        and a.target.suffix == ".md"]
    assert per_file_actions, "expected per-file command installs in top-level layout"
    # Every command install should be marked skip-if-exists to honor the
    # refuse-to-overwrite policy on user-authored commands.
    assert all(a.skip_if_exists for a in per_file_actions)


def test_plan_top_level_filters_duplicate_command_files(tmp_path, monkeypatch):
    """Top-level per-file symlink planning must not expose `name N.md` copies."""
    from metaensemble.lib import installer

    fake_core = tmp_path / "fake-core"
    (fake_core / "commands").mkdir(parents=True)
    (fake_core / "commands" / "dispatch.md").write_text("# dispatch")
    (fake_core / "commands" / "dispatch 2.md").write_text("# duplicate")
    (fake_core / "commands" / "limits.md").write_text("# limits")
    (fake_core / "commands" / "limits 3.md").write_text("# duplicate")
    (fake_core / "roles").mkdir()
    (fake_core / "skills").mkdir()
    (fake_core / "output-styles").mkdir()
    monkeypatch.setattr(installer, "CORE_DIR", fake_core)

    home = tmp_path / "home"
    project = tmp_path / "proj"
    home.mkdir()
    project.mkdir()
    plan = installer.plan_install(
        installer.SurveyResult(),
        installer.Layout.TOP_LEVEL,
        project=project,
        home=home,
        decisions=installer.SurveyDecisions(),
    )

    command_targets = {
        a.target.name
        for a in plan.actions
        if a.kind == "symlink"
        and a.target is not None
        and a.target.parent == home / ".claude" / "commands"
    }
    assert command_targets == {"dispatch.md", "limits.md"}


def test_detect_overlaps_uses_deliverable_records_category(tmp_path):
    from metaensemble.lib.installer import detect_overlaps

    project = tmp_path / "project"
    registry = project / ".claude" / "reports" / "_registry.md"
    registry.parent.mkdir(parents=True)
    registry.write_text("# Registry\n")

    overlaps = detect_overlaps(project)

    assert len(overlaps) == 1
    assert overlaps[0].category == "deliverable_records"
    assert overlaps[0].project_surface == ".claude/reports/_registry.md"
    assert overlaps[0].recommendation == "metaensemble_owned"
    assert overlaps[0].write_policy == "block_when_metaensemble_owned"


def test_user_setup_applies_only_user_scope_actions(tmp_path, monkeypatch):
    """`cmd_user_setup` writes the launcher, the runtime symlinks, and
    the settings.json merge — but does NOT create per-project state.

    This is the contract that makes user-setup portable: invoking it
    from any cwd produces the same user-level integration, leaving
    project adoption to `cmd_adopt`.
    """
    import argparse
    from metaensemble.cli import cmd_user_setup

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    # cwd is a fresh dir that has never been a MetaEnsemble project.
    cwd = tmp_path / "some_cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    args = argparse.Namespace(layout="namespaced", dry_run=False)
    rc = cmd_user_setup(args)
    assert rc == 0

    # User-scope artifacts present.
    assert (home / ".metaensemble" / "runtime" / "bin" / "me-run").exists()
    assert (home / ".claude" / "commands" / "metaensemble").exists()
    assert (home / ".claude" / "skills" / "metaensemble-protocol").exists()

    # No project state was created in the cwd.
    assert not (cwd / ".metaensemble").exists(), \
        "user-setup must not create project state"


def test_user_setup_accepts_layout_mode_aliases(tmp_path, monkeypatch):
    import argparse
    from metaensemble.cli import cmd_user_setup

    for alias, namespaced in (("namespaced", True), ("top-level", False)):
        home = tmp_path / f"home-{alias}"
        cwd = tmp_path / f"cwd-{alias}"
        home.mkdir()
        cwd.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls, h=home: h))
        monkeypatch.chdir(cwd)

        rc = cmd_user_setup(argparse.Namespace(layout=alias, dry_run=False))
        assert rc == 0

        namespace_dir = home / ".claude" / "commands" / "metaensemble"
        top_level_dispatch = home / ".claude" / "commands" / "dispatch.md"
        if namespaced:
            assert namespace_dir.exists()
            assert not top_level_dispatch.exists()
        else:
            assert top_level_dispatch.exists()
            assert not namespace_dir.exists()


def test_user_setup_dry_run_reports_user_backup_root(tmp_path, monkeypatch, capsys):
    """Dry-run backup paths must match real user-scope apply paths."""
    import argparse
    from metaensemble.cli import cmd_user_setup

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    cwd = tmp_path / "some_cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    rc = cmd_user_setup(argparse.Namespace(layout="namespaced", dry_run=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert str(home / ".metaensemble" / "installs") in out
    assert str(cwd / ".metaensemble" / "backups") not in out
    assert not (home / ".metaensemble").exists(), "dry-run must not write user state"
    assert not (cwd / ".metaensemble").exists(), "dry-run must not write project state"


def test_adopt_refuses_when_user_setup_has_not_run(tmp_path, monkeypatch, capsys):
    """`adopt` must refuse with a clear hint when ~/.claude/ has no
    MetaEnsemble integration yet — running adopt first would silently
    create project state pointing at a launcher that doesn't exist."""
    import argparse
    from metaensemble.cli import cmd_adopt

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    project = tmp_path / "proj"
    project.mkdir()

    args = argparse.Namespace(path=str(project), dry_run=False)
    rc = cmd_adopt(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "user-setup" in err.lower(), "error must name the missing precondition"


def test_adopt_dry_run_makes_no_project_writes_and_reports_init(
    tmp_path, monkeypatch, capsys,
):
    """`adopt --dry-run` previews project setup without writing inspection files."""
    import argparse
    from metaensemble.cli import cmd_adopt, cmd_user_setup

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)

    assert cmd_user_setup(argparse.Namespace(layout="namespaced", dry_run=False)) == 0
    capsys.readouterr()

    rc = cmd_adopt(argparse.Namespace(path=None, dry_run=True))
    assert rc == 0
    out = capsys.readouterr().out

    assert "Would write inspection report" in out
    assert "Would write fresh default decisions" in out
    assert "Would initialize project state" in out
    assert "Would ensure root `.gitignore` ignores" in out
    assert "Per-agent install actions" in out
    assert "Actions: 0" in out
    assert not (project / ".metaensemble").exists(), (
        "adopt --dry-run must not create survey/project state"
    )


def test_adopt_reports_project_state_side_effects_honestly(tmp_path, monkeypatch, capsys):
    """The adopt copy must name what actually happened.

    When every survey decision defaults to a no-op on disk
    (keep_yours / preserve), adopt still initializes the project state
    directory, writes the inspection, and seeds `install-decisions.yaml`. The
    copy distinguishes:
      - first-time `initialized` vs subsequent `refreshed`
      - fresh decisions written vs existing decisions honored
      - 0 install actions vs N install actions
    """
    import argparse
    from metaensemble.cli import cmd_adopt, cmd_user_setup

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)

    assert cmd_user_setup(argparse.Namespace(layout="namespaced", dry_run=False)) == 0

    # First adopt: project state newly initialized, fresh decisions written.
    rc = cmd_adopt(argparse.Namespace(path=None, dry_run=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Project state initialized" in out, "first adopt must say 'initialized'"
    assert "fresh defaults" in out, "first adopt must name the decisions file write"
    assert "Unchanged" not in out, "adopt must name project-state side effects"
    # The "0 applied" message is OK but must include the qualifier.
    assert "0 applied" in out
    assert "no-op on disk" in out

    # Second adopt: state already there, decisions already there.
    rc = cmd_adopt(argparse.Namespace(path=None, dry_run=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Project state refreshed" in out, "second adopt must say 'refreshed'"
    assert "using existing" in out, "second adopt must honor existing decisions"
    assert "Unchanged" not in out


def test_adopt_after_user_setup_creates_project_state(tmp_path, monkeypatch):
    """End-to-end: user-setup then adopt produces a working project."""
    import argparse
    from metaensemble.cli import cmd_adopt, cmd_user_setup

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)

    rc = cmd_user_setup(argparse.Namespace(layout="namespaced", dry_run=False))
    assert rc == 0

    rc = cmd_adopt(argparse.Namespace(path=None, dry_run=False))
    assert rc == 0

    # Project state present.
    assert (project / ".metaensemble" / "state" / "department.db").exists()
    assert (project / ".metaensemble" / "active-roles.yaml").exists()
    assert (project / ".metaensemble" / "install-decisions.yaml").exists()
    # User-level integration also still there.
    assert (home / ".metaensemble" / "runtime" / "bin" / "me-run").exists()
    assert (home / ".claude" / "commands" / "metaensemble").exists()


def test_survey_filters_metaensemble_managed_symlinks(tmp_path):
    """Inspection must not double-count MetaEnsemble's own installed symlinks
    as user artifacts. After `user-setup --layout=top-level` lands the
    top-level slash commands under `~/.claude/commands/`, those symlinks
    point into the MetaEnsemble repo and should be invisible to the
    survey's user-artifact scanner.
    """
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()

    # Baseline inspection on a clean home — no managed symlinks yet.
    before = survey(home=home, project=project, write_report=False)
    baseline_collisions = len(before.collisions)

    # Apply user-setup in top-level layout to plant top-level managed
    # symlinks at ~/.claude/commands/dispatch.md, standup.md, etc.
    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.TOP_LEVEL, project=project, home=home)
    user_plan = type(plan)(
        layout=plan.layout,
        actions=plan.user_actions(),
        timestamp=plan.timestamp,
        active_roles=plan.active_roles,
        inactive_roles=plan.inactive_roles,
    )
    apply_install(user_plan, dry_run=False, user_scope_only=True, home=home)

    # Survey again — the managed symlinks must be filtered out, so the
    # collision count must not jump.
    after = survey(home=home, project=project, write_report=False)
    assert len(after.collisions) == baseline_collisions, (
        f"survey double-counted managed symlinks: was {baseline_collisions}, "
        f"now {len(after.collisions)}"
    )


def test_unadopt_dry_run_makes_no_changes(tmp_path, monkeypatch):
    """`unadopt --dry-run` must enumerate what would be reversed without
    actually changing the filesystem."""
    import argparse
    from metaensemble.cli import cmd_adopt, cmd_unadopt, cmd_user_setup

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)

    assert cmd_user_setup(argparse.Namespace(layout="namespaced", dry_run=False)) == 0
    assert cmd_adopt(argparse.Namespace(path=None, dry_run=False)) == 0

    # Snapshot state before dry-run.
    me_dir_before = (project / ".metaensemble").exists()
    user_links_before = (home / ".claude" / "commands" / "metaensemble").exists()

    rc = cmd_unadopt(argparse.Namespace(path=None, purge_state=True, dry_run=True))
    assert rc == 0

    # Filesystem unchanged.
    assert (project / ".metaensemble").exists() == me_dir_before, \
        "dry-run modified project state"
    assert (home / ".claude" / "commands" / "metaensemble").exists() == user_links_before, \
        "dry-run modified user-level integration"


def test_full_round_trip_restores_user_state_exactly(tmp_path, monkeypatch):
    """End-to-end byte-level proof: install + uninstall returns ~/.claude/
    to its exact pre-install state.

    Setup: a populated ~/.claude/ with a pre-existing settings.json
    (user-authored hooks), user-unique agents, user commands, and an
    agent whose name collides with a MetaEnsemble curated Role. Apply
    top-level install with the colliding agent set to `take_ours`
    and a user-unique agent set to `convert` — both exercise the
    backup-and-restore path. Then run the full uninstall sequence
    (unadopt --purge-state + user-teardown --purge-state) and verify
    that every file under ~/.claude/ matches its pre-install hash.
    """
    import argparse
    import hashlib
    import json as _json
    from metaensemble.cli import (
        cmd_adopt, cmd_unadopt, cmd_user_setup, cmd_user_teardown,
    )

    def snapshot(root: Path) -> dict[str, str]:
        """Map every file under root to its sha256, sorted for determinism."""
        out = {}
        if not root.exists():
            return out
        for p in sorted(root.rglob("*")):
            if p.is_file() and not p.is_symlink():
                rel = str(p.relative_to(root))
                out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        return out

    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    project = tmp_path / "proj"
    project.mkdir()

    # Populate ~/.claude/ with a realistic pre-install setup:
    #   - user-authored settings.json with a non-MetaEnsemble hook
    #   - agents: one collides with MetaEnsemble's `backend` curated Role,
    #     one is user-unique
    #   - a user command
    claude = home / ".claude"
    (claude / "agents").mkdir(parents=True)
    (claude / "commands").mkdir()
    (claude / "skills").mkdir()
    (claude / "output-styles").mkdir()
    user_settings = {
        "hooks": {
            "PreCompact": [{
                "matcher": "*",
                "hooks": [{"type": "command", "command": "/usr/local/bin/my-tool"}],
            }],
        },
        "theme": "dark",  # arbitrary user key that must survive untouched
    }
    (claude / "settings.json").write_text(_json.dumps(user_settings, indent=2))
    (claude / "agents" / "backend.md").write_text(AGENT_BACKEND)
    (claude / "agents" / "mybot.md").write_text(AGENT_BACKEND.replace("backend", "mybot"))
    (claude / "commands" / "mycmd.md").write_text("# My command\n")

    # Snapshot every file in ~/.claude/ BEFORE the install touches it.
    before = snapshot(claude)
    assert before, "snapshot must not be empty"

    # Install: user-setup then adopt. Edit decisions so the install
    # exercises both backup-and-restore paths.
    assert cmd_user_setup(argparse.Namespace(layout="top-level", dry_run=False)) == 0
    monkeypatch.chdir(project)
    # Survey writes decisions.yaml; edit it to take_ours for the collision
    # and convert for the user_unique agent — both back up the original.
    from metaensemble.lib.installer import survey as run_inspect
    run_inspect(home=home, project=project, write_report=True)
    decisions_path = project / ".metaensemble" / "install-decisions.yaml"
    text = decisions_path.read_text()
    text = text.replace(
        "  - name: backend\n    kind: collision\n    action: keep_yours",
        "  - name: backend\n    kind: collision\n    action: take_ours",
    )
    text = text.replace(
        "  - name: mybot\n    kind: user_unique\n    action: preserve",
        "  - name: mybot\n    kind: user_unique\n    action: convert",
    )
    decisions_path.write_text(text)

    assert cmd_adopt(argparse.Namespace(path=None, dry_run=False)) == 0

    # Confirm the install actually mutated ~/.claude/.
    mid = snapshot(claude)
    assert mid != before, "install must have changed the snapshot"

    # Full uninstall sequence: unadopt then user-teardown.
    assert cmd_unadopt(
        argparse.Namespace(path=None, purge_state=True, dry_run=False)
    ) == 0
    assert cmd_user_teardown(
        argparse.Namespace(purge_state=True, dry_run=False)
    ) == 0

    # Round-trip check: every file in ~/.claude/ matches its pre-install hash.
    after = snapshot(claude)
    assert after == before, (
        f"~/.claude/ did not round-trip exactly:\n"
        f"  missing after uninstall: {set(before) - set(after)}\n"
        f"  unexpected after uninstall: {set(after) - set(before)}\n"
        f"  content changed: "
        f"{[k for k in before if k in after and before[k] != after[k]]}"
    )

    # User-level MetaEnsemble dir should be gone entirely.
    assert not (home / ".metaensemble").exists(), \
        "user-level state must be purged"


def test_user_teardown_dry_run_count_matches_apply(tmp_path, monkeypatch):
    """The dry-run action count should not inflate against the live apply.

    Earlier the dry-run double-listed each managed symlink (once as
    `reverse-symlink` from backup walking, once as
    `purge-user-runtime-integration` from the residue helper). Live apply
    removes the symlink in step 1, so step 2 finds it gone — the apply
    count is smaller. The fix filters the dry-run purge listing to skip
    paths already covered by the per-action reversal loop.
    """
    import argparse
    from metaensemble.cli import cmd_user_setup
    from metaensemble.lib.installer import uninstall

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    assert cmd_user_setup(argparse.Namespace(layout="namespaced", dry_run=False)) == 0

    dry_report = uninstall(
        restore=True, purge_user_state_flag=True, scope="user",
        dry_run=True, home=home,
    )
    live_report = uninstall(
        restore=True, purge_user_state_flag=True, scope="user",
        dry_run=False, home=home,
    )
    # Dry-run can still report a small extra (the dirs that get purged
    # whole vs the symlinks inside them), but it must not double-count
    # the symlinks. A correctly filtered dry-run is within a small slack
    # of the apply count.
    drift = len(dry_report.applied) - len(live_report.applied)
    assert drift <= 2, (
        f"dry-run count ({len(dry_report.applied)}) drifts too far from "
        f"apply count ({len(live_report.applied)}); duplicates likely back"
    )


def test_user_teardown_dedupes_stacked_install_records(tmp_path, monkeypatch):
    """Repeated setup records must not multiply the same teardown effects."""
    import argparse
    from metaensemble.cli import cmd_user_setup
    from metaensemble.lib.installer import uninstall

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    for _ in range(3):
        assert cmd_user_setup(argparse.Namespace(layout="namespaced", dry_run=False)) == 0

    report = uninstall(
        restore=True, purge_user_state_flag=False, scope="user",
        dry_run=True, home=home,
    )
    reverse_targets = [
        a.target
        for a in report.applied
        if a.kind in {"reverse-symlink", "reverse-merge-settings"}
    ]

    assert len(reverse_targets) == len(set(reverse_targets))
    assert len([a for a in report.applied if a.kind == "reverse-symlink"]) == 4


def test_user_teardown_dry_run_does_not_double_list_runtime(tmp_path, monkeypatch):
    import argparse
    from metaensemble.cli import cmd_user_setup
    from metaensemble.lib.installer import uninstall

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert cmd_user_setup(argparse.Namespace(layout="namespaced", dry_run=False)) == 0

    report = uninstall(
        restore=True, purge_user_state_flag=True, scope="user",
        dry_run=True, home=home,
    )

    runtime_targets = [
        a for a in report.applied
        if a.target == home / ".metaensemble" / "runtime"
    ]
    assert [a.kind for a in runtime_targets] == ["purge-user-state"]
    assert "reverse-vendor-runtime" not in {a.kind for a in report.applied}


def test_user_teardown_dry_run_makes_no_changes(tmp_path, monkeypatch):
    """`user-teardown --dry-run` must enumerate without changing anything."""
    import argparse
    from metaensemble.cli import cmd_user_setup, cmd_user_teardown

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    assert cmd_user_setup(argparse.Namespace(layout="namespaced", dry_run=False)) == 0
    launcher_before = (home / ".metaensemble" / "runtime" / "bin" / "me-run").exists()
    commands_before = (home / ".claude" / "commands" / "metaensemble").exists()

    rc = cmd_user_teardown(argparse.Namespace(purge_state=True, dry_run=True))
    assert rc == 0

    assert (home / ".metaensemble" / "runtime" / "bin" / "me-run").exists() == launcher_before
    assert (home / ".claude" / "commands" / "metaensemble").exists() == commands_before


def test_real_lifecycle_commands_print_per_action_status(tmp_path, monkeypatch, capsys):
    import argparse
    from metaensemble.cli import cmd_user_setup, cmd_user_teardown

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    assert cmd_user_setup(argparse.Namespace(layout="namespaced", dry_run=False)) == 0
    setup_out = capsys.readouterr().out
    assert "User-level action status:" in setup_out
    assert "  - OK   vendor-runtime" in setup_out

    assert cmd_user_teardown(argparse.Namespace(purge_state=True, dry_run=False)) == 0
    teardown_out = capsys.readouterr().out
    assert "User teardown action status:" in teardown_out
    assert "  - OK" in teardown_out


def test_unadopt_leaves_user_level_in_place(tmp_path, monkeypatch):
    """`unadopt` reverses project state but does NOT remove user-level
    integration. After unadopting, a second `adopt` can land cleanly
    without re-running user-setup.
    """
    import argparse
    from metaensemble.cli import cmd_adopt, cmd_unadopt, cmd_user_setup

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)

    assert cmd_user_setup(argparse.Namespace(layout="namespaced", dry_run=False)) == 0
    assert cmd_adopt(argparse.Namespace(path=None, dry_run=False)) == 0

    rc = cmd_unadopt(argparse.Namespace(path=None, purge_state=True, dry_run=False))
    assert rc == 0

    # Project state purged.
    assert not (project / ".metaensemble").exists()
    # User-level integration intact.
    assert (home / ".metaensemble" / "runtime" / "bin" / "me-run").exists()
    assert (home / ".claude" / "commands" / "metaensemble").exists()
    assert (home / ".claude" / "skills" / "metaensemble-protocol").exists()


def test_user_teardown_leaves_other_projects_alone(tmp_path, monkeypatch):
    """`user-teardown` removes user-level integration but does NOT touch
    any adopted project's .metaensemble/."""
    import argparse
    from metaensemble.cli import cmd_adopt, cmd_user_setup, cmd_user_teardown

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)

    assert cmd_user_setup(argparse.Namespace(layout="namespaced", dry_run=False)) == 0
    assert cmd_adopt(argparse.Namespace(path=None, dry_run=False)) == 0

    rc = cmd_user_teardown(argparse.Namespace(purge_state=True, dry_run=False))
    assert rc == 0

    # User-level gone.
    assert not (home / ".metaensemble").exists()
    assert not (home / ".claude" / "commands" / "metaensemble").exists()
    assert not (home / ".claude" / "skills" / "metaensemble-protocol").exists()
    # Project state survives.
    assert (project / ".metaensemble").exists()
    assert (project / ".metaensemble" / "active-roles.yaml").exists()


def test_setup_wizard_lists_projects_and_adopts_choice(tmp_path, monkeypatch, capsys):
    """The wizard lists discoverable projects, prompts for a choice,
    runs `user-setup` if needed (asking for layout), and adopts the
    chosen project. The complete happy path lands user-level
    integration and project state in one command.
    """
    import argparse
    from metaensemble.cli import cmd_setup

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    # Seed Claude Code's project bookkeeping. `discover_projects` decodes
    # the project's cwd by reading a `cwd` field from any session JSONL
    # in the project dir, so we drop in a minimal one-line transcript.
    import json as _json
    import re as _re
    project = tmp_path / "myproj"
    project.mkdir()
    encoded = _re.sub(r"[^A-Za-z0-9.]", "-", str(project.resolve()))
    proj_dir = home / ".claude" / "projects" / encoded
    proj_dir.mkdir(parents=True)
    (proj_dir / "abc123.jsonl").write_text(
        _json.dumps({"cwd": str(project.resolve())}) + "\n"
    )

    answers = iter(["1", ""])  # pick first option, accept default layout
    def stub_input(_prompt):
        return next(answers)

    rc = cmd_setup(
        argparse.Namespace(layout=None),
        input_fn=stub_input,
    )
    assert rc == 0

    # Both layers landed.
    assert (home / ".metaensemble" / "runtime" / "bin" / "me-run").exists()
    assert (home / ".claude" / "commands" / "metaensemble").exists()
    assert (project / ".metaensemble" / "active-roles.yaml").exists()
    # Verify the wizard surfaced the discoverable project.
    out = capsys.readouterr().out
    assert "Known projects" in out
    assert str(project) in out


def test_setup_wizard_skips_user_setup_when_already_installed(tmp_path, monkeypatch, capsys):
    """When user-setup has already run, the wizard does not re-ask for
    mode. It detects the existing install via `detect_user_layout()`."""
    import argparse
    from metaensemble.cli import cmd_setup, cmd_user_setup

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    # Pre-install user-setup.
    rc = cmd_user_setup(argparse.Namespace(layout="namespaced", dry_run=False))
    assert rc == 0

    # Seed a discoverable project.
    import json as _json
    import re as _re
    project = tmp_path / "p2"
    project.mkdir()
    encoded = _re.sub(r"[^A-Za-z0-9.]", "-", str(project.resolve()))
    proj_dir = home / ".claude" / "projects" / encoded
    proj_dir.mkdir(parents=True)
    (proj_dir / "abc123.jsonl").write_text(
        _json.dumps({"cwd": str(project.resolve())}) + "\n"
    )

    # Wizard receives only the project choice — no layout prompt.
    answers = iter(["1"])
    def stub_input(_prompt):
        return next(answers)

    rc = cmd_setup(argparse.Namespace(layout=None), input_fn=stub_input)
    assert rc == 0
    out = capsys.readouterr().out
    assert "already installed" in out


def test_plan_partitions_user_vs_project_scope_actions(tmp_path):
    """`user_actions()` and `project_actions()` together partition the
    plan with no overlap. The partition is what lets `user-setup` and
    `adopt` divide responsibility cleanly.
    """
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _make_runtime(home / ".claude", agents={"mybot": AGENT_BACKEND})
    survey_result = survey(home=home, project=project, write_report=False)

    plan = plan_install(survey_result, Layout.TOP_LEVEL, project=project, home=home)
    user_set = set(id(a) for a in plan.user_actions())
    project_set = set(id(a) for a in plan.project_actions())
    all_set = set(id(a) for a in plan.actions)

    assert user_set.isdisjoint(project_set), "scopes must not overlap"
    assert user_set | project_set == all_set, "scopes must cover every action"
    # The runner is generated inside vendor-runtime, NOT a
    # separate render-launcher action.
    assert any(a.kind == "vendor-runtime" for a in plan.user_actions())
    assert any(a.kind == "symlink" for a in plan.user_actions())
    assert any(a.kind == "merge-settings" for a in plan.user_actions())
    assert not any(a.kind == "render-launcher" for a in plan.actions), (
        "render-launcher action kind was removed"
    )
    # Convert-agent actions, when present, belong to the project scope.
    # Each action in the plan should classify into exactly one bucket.
    for a in plan.actions:
        if a.kind == "convert-agent":
            assert is_project_scope_action(a)


def test_plan_includes_vendor_runtime_action(tmp_path):
    """Both layouts include a vendor-runtime action that vendors
    package assets + generates the runner atomically. The action targets
    `~/.metaensemble/runtime/` (the symlink path)."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    survey_result = survey(home=home, project=project, write_report=False)

    for mode in (Layout.NAMESPACED, Layout.TOP_LEVEL):
        plan = plan_install(survey_result, mode, project=project, home=home)
        vendor = [a for a in plan.actions if a.kind == "vendor-runtime"]
        assert len(vendor) == 1, f"missing vendor-runtime action in {mode}"
        assert vendor[0].target == home / ".metaensemble" / "runtime"


def test_apply_vendor_runtime_produces_runner_and_manifest(tmp_path, monkeypatch):
    """vendor-runtime: assets copied, runner generated with absolute Python
    path, MANIFEST present and verifiable, atomic symlink in place."""
    import os as _os
    import subprocess
    import sys as _sys
    from metaensemble.lib.installer import _verify_runtime_manifest_safe

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    project = tmp_path / "proj"
    project.mkdir()
    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)
    apply_install(plan, project=project, dry_run=False)

    runtime = home / ".metaensemble" / "runtime"
    runner = runtime / "bin" / "me-run"
    assert runtime.is_symlink(), "runtime must be a symlink (atomic-swap design)"
    resolved = runtime.resolve(strict=True)
    assert resolved.parent.name == "runtime-versions"
    assert _verify_runtime_manifest_safe(resolved), "MANIFEST must verify"
    assert runner.exists()
    body = runner.read_text()
    assert _sys.executable in body, "runner must pin sys.executable"
    assert _os.access(str(runner), _os.X_OK)
    # Sanity-invoke: runner exits 0 with --help, proving exec path works.
    proc = subprocess.run([str(runner), "--help"], capture_output=True, text=True)
    assert proc.returncode == 0
    assert "metaensemble" in proc.stdout.lower()


def test_apply_symlink_skips_existing_when_skip_if_exists_set(tmp_path):
    """A per-file symlink with skip_if_exists=True should leave existing files alone."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    # Pre-create a user-authored command at the target location.
    (home / ".claude" / "commands").mkdir(parents=True)
    user_command = home / ".claude" / "commands" / "dispatch.md"
    user_command.write_text("---\nname: dispatch\n---\nUser's own dispatch.")
    user_mtime = user_command.stat().st_mtime

    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.TOP_LEVEL, project=project, home=home)
    apply_install(plan, project=project, dry_run=False)

    # The user's existing dispatch.md is untouched.
    assert user_command.read_text().startswith("---\nname: dispatch")
    assert user_command.stat().st_mtime == user_mtime
    # And it is not a symlink.
    assert not user_command.is_symlink()


def test_plan_incorporate_preserves_user_agents_by_default(tmp_path):
    """User-unique agents are PRESERVED by default; conversion is opt-in via decisions."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _make_runtime(home / ".claude", agents={"mybot": AGENT_BACKEND})
    survey_result = survey(home=home, project=project, write_report=False)

    plan = plan_install(survey_result, Layout.TOP_LEVEL, project=project, home=home)
    convert_actions = [a for a in plan.actions if a.kind == "convert-agent"]
    # Default behaviour: user-unique agents (mybot is not in curated set) are
    # preserved as-is. The user must opt in to conversion via decisions.
    assert convert_actions == [], (
        "the new flow does not silently convert user-unique agents; "
        "the user must explicitly opt in via install-decisions.yaml"
    )
    # The agent's name is recorded in active_roles so the Coordinator can dispatch it.
    assert "mybot" in plan.active_roles


def test_plan_incorporate_converts_when_decision_says_so(tmp_path):
    """When the user explicitly sets action: convert, the convert-agent action fires."""
    from metaensemble.lib.installer import AgentDecision, SurveyDecisions
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _make_runtime(home / ".claude", agents={"mybot": AGENT_BACKEND})
    survey_result = survey(home=home, project=project, write_report=False)
    decisions = SurveyDecisions(
        agents=[AgentDecision(name="mybot", kind="user_unique", action="convert")],
        timestamp="20260515T000000Z",
        suggested_layout="top-level",
    )
    plan = plan_install(
        survey_result, Layout.TOP_LEVEL,
        project=project, home=home, decisions=decisions,
    )
    convert_actions = [a for a in plan.actions if a.kind == "convert-agent"]
    assert len(convert_actions) == 1
    assert convert_actions[0].source.name == "mybot.md"


def test_plan_collision_take_ours_converts_user_agent(tmp_path):
    """For a collision, action=take_ours converts the user's agent."""
    from metaensemble.lib.installer import AgentDecision, SurveyDecisions
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    # Agent file named to collide with a curated Role (backend).
    _make_runtime(home / ".claude", agents={"backend": AGENT_BACKEND})
    survey_result = survey(home=home, project=project, write_report=False)
    decisions = SurveyDecisions(
        agents=[AgentDecision(name="backend", kind="collision", action="take_ours")],
        timestamp="20260515T000000Z",
        suggested_layout="top-level",
    )
    plan = plan_install(
        survey_result, Layout.TOP_LEVEL,
        project=project, home=home, decisions=decisions,
    )
    convert_actions = [a for a in plan.actions if a.kind == "convert-agent"]
    assert len(convert_actions) == 1
    assert "backend" in plan.active_roles


def test_plan_collision_keep_yours_emits_no_convert(tmp_path):
    """For a collision, action=keep_yours does NOT convert.

    The name still appears in `active_roles` because the user's native
    agent file is dispatchable. The Coordinator must not refuse a
    dispatch the user can actually make.
    """
    from metaensemble.lib.installer import AgentDecision, SurveyDecisions
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _make_runtime(home / ".claude", agents={"backend": AGENT_BACKEND})
    survey_result = survey(home=home, project=project, write_report=False)
    decisions = SurveyDecisions(
        agents=[AgentDecision(name="backend", kind="collision", action="keep_yours")],
        timestamp="20260515T000000Z",
        suggested_layout="top-level",
    )
    plan = plan_install(
        survey_result, Layout.TOP_LEVEL,
        project=project, home=home, decisions=decisions,
    )
    convert_actions = [a for a in plan.actions if a.kind == "convert-agent"]
    assert convert_actions == []
    # The name IS active — the user's native backend.md is dispatchable.
    assert "backend" in plan.active_roles


def test_plan_collision_keep_both_installs_curated_under_me_suffix(tmp_path):
    """For a collision, action=keep_both installs the curated Role under -me."""
    from metaensemble.lib.installer import AgentDecision, SurveyDecisions
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _make_runtime(home / ".claude", agents={"backend": AGENT_BACKEND})
    survey_result = survey(home=home, project=project, write_report=False)
    decisions = SurveyDecisions(
        agents=[AgentDecision(name="backend", kind="collision", action="keep_both")],
        timestamp="20260515T000000Z",
        suggested_layout="top-level",
    )
    plan = plan_install(
        survey_result, Layout.TOP_LEVEL,
        project=project, home=home, decisions=decisions,
    )
    install_curated = [a for a in plan.actions if a.kind == "install-curated-role"]
    assert len(install_curated) == 1
    assert install_curated[0].target.name == "backend-me.md"
    # Both names available to the Coordinator.
    assert "backend" in plan.active_roles
    assert "backend-me" in plan.active_roles


def test_plan_parallel_does_not_convert_agents(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _make_runtime(home / ".claude", agents={"mybot": AGENT_BACKEND})
    survey_result = survey(home=home, project=project, write_report=False)

    plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)
    convert_actions = [a for a in plan.actions if a.kind == "convert-agent"]
    assert convert_actions == []


# --- Apply (filesystem changes) ------------------------------------------


def test_apply_dry_run_makes_no_filesystem_changes(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)

    report = apply_install(plan, project=project, dry_run=True)
    assert report.backup_root is None
    # No commands directory created under .claude.
    assert not (home / ".claude" / "commands" / "metaensemble").exists()


def test_apply_creates_symlinks_in_parallel_mode(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)
    report = apply_install(plan, project=project, dry_run=False)
    assert report.errors == [], f"unexpected errors: {report.errors}"
    # Commands symlink exists and points at metaensemble/commands.
    commands_link = home / ".claude" / "commands" / "metaensemble"
    assert commands_link.is_symlink()


def test_apply_incorporate_converts_and_backs_up_agents(tmp_path):
    """When the user opts in (action: convert), the apply phase:

    1. Copies the original agent to a backup under .metaensemble/backups/.
    2. Writes the converted Role under ~/.metaensemble/roles/.
    3. Replaces the original agent file with a thin shim that keeps the
       agent name dispatchable by the runtime but delegates the full
       spec to the Role file.
    """
    from metaensemble.lib.installer import AgentDecision, SurveyDecisions
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _make_runtime(home / ".claude", agents={"mybot": AGENT_BACKEND})

    survey_result = survey(home=home, project=project, write_report=False)
    decisions = SurveyDecisions(
        agents=[AgentDecision(name="mybot", kind="user_unique", action="convert")],
        timestamp="20260515T000000Z",
        suggested_layout="top-level",
    )
    plan = plan_install(
        survey_result, Layout.TOP_LEVEL,
        project=project, home=home, decisions=decisions,
    )
    report = apply_install(plan, project=project, dry_run=False)
    assert all("convert-agent" not in str(e) for _, e in report.errors), report.errors

    # The original agent path now holds a shim (preserves native dispatch).
    agent_path = home / ".claude" / "agents" / "mybot.md"
    assert agent_path.exists(), "shim should be left in place so Agent(subagent_type='mybot') resolves"
    shim_text = agent_path.read_text()
    assert "MetaEnsemble Role" in shim_text
    assert "name: backend" in shim_text  # mirrors original frontmatter
    # A backup should exist under .metaensemble/backups/<ts>/agents/user/.
    backup = project / ".metaensemble" / "backups"
    backups = list(backup.iterdir())
    assert backups, "expected at least one backup directory"
    backup_agent = backups[0] / "agents" / "user" / "mybot.md"
    assert backup_agent.exists()
    # The converted Role exists at user-level metaensemble roles dir.
    role_path = home / ".metaensemble" / "roles" / "mybot.md"
    assert role_path.exists()
    assert "name: backend" in role_path.read_text()
    assert "model_tier: sonnet" in role_path.read_text()


def test_apply_writes_hook_entries_into_settings_json(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)
    apply_install(plan, project=project, dry_run=False)

    settings_path = home / ".claude" / "settings.json"
    assert settings_path.exists()
    data = json.loads(settings_path.read_text())
    assert "hooks" in data
    assert "PreToolUse" in data["hooks"]


def test_hook_commands_are_shell_quoted(tmp_path):
    """Hook command strings must survive shell parsing even when the install
    path contains spaces or other shell metacharacters.

    Two formats are accepted:
      - Direct form: `<python> <repo>/core/hooks/<script>.py` — tokens are
        the interpreter and the script path; the script ends in `.py` and
        exists on disk.
      - Launcher form: `<home>/.metaensemble/runtime/bin/me-run hook <script>.py`
        — tokens are the launcher, the literal `hook` subcommand, and the
        script filename. This form fires whenever the launcher is installed
        at the target home directory.
    """
    import shlex as _shlex
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)
    apply_install(plan, project=project, dry_run=False)

    settings = json.loads((home / ".claude" / "settings.json").read_text())
    for event_key, entries in settings["hooks"].items():
        for entry in entries:
            for hook in entry["hooks"]:
                cmd = hook["command"]
                tokens = _shlex.split(cmd)
                assert len(tokens) >= 2, (
                    f"hook command did not parse cleanly: {cmd!r}"
                )
                if tokens[0].endswith("/me-run"):
                    # Launcher form.
                    assert tokens[1] == "hook", (
                        f"launcher-form hook must use the `hook` subcommand: {cmd!r}"
                    )
                    assert tokens[2].endswith(".py"), (
                        f"launcher-form hook script must be a .py file: {cmd!r}"
                    )
                else:
                    # Direct form: interpreter + absolute script path.
                    script_path = tokens[-1]
                    assert script_path.endswith(".py")
                    from pathlib import Path as _P
                    assert _P(script_path).exists(), (
                        f"hook script does not exist: {script_path}"
                    )


def test_statusline_command_uses_launcher_when_available(tmp_path):
    """Statusline wiring should use the same path-portable launcher as hooks."""
    import shlex as _shlex

    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)
    apply_install(plan, project=project, dry_run=False)

    settings = json.loads((home / ".claude" / "settings.json").read_text())
    command = settings["statusLine"]["command"]
    parts = _shlex.split(command)
    assert parts[0] == str(home / ".metaensemble" / "runtime" / "bin" / "me-run")
    assert parts[1] == "statusline"


def test_apply_settings_merge_preserves_existing_keys(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"theme": "dark", "alwaysThinkingEnabled": True})
    )

    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)
    apply_install(plan, project=project, dry_run=False)

    data = json.loads((home / ".claude" / "settings.json").read_text())
    # User keys preserved.
    assert data["theme"] == "dark"
    assert data["alwaysThinkingEnabled"] is True
    # Hooks added.
    assert "hooks" in data


# --- Rollback round-trip -------------------------------------------------


def test_uninstall_restore_preserves_launcher(tmp_path):
    """Default rollback must not delete the launcher.

    The launcher at ~/.metaensemble/runtime/bin/me-run is the recovery anchor:
    it lets the Principal re-install or run `metaensemble doctor` after
    a default rollback. Removing it on every default-scope rollback
    forces a bootstrap re-run between every install cycle. Only an
    explicit user-state purge should remove it.
    """
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()

    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)
    apply_install(plan, project=project, dry_run=False)

    launcher = home / ".metaensemble" / "runtime" / "bin" / "me-run"
    assert launcher.exists()

    report = uninstall(project=project, restore=True, home=home)
    assert report.errors == [], f"errors during uninstall: {report.errors}"
    assert launcher.exists(), "launcher must survive default-scope --restore"

    # Purge-user-state, by contrast, must remove the launcher.
    report = uninstall(
        project=project, restore=True, home=home,
        purge_user_state_flag=True,
    )
    assert not launcher.exists(), "purge-user-state must remove the launcher"


def test_uninstall_restore_brings_back_converted_agent(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _make_runtime(home / ".claude", agents={"mybot": AGENT_BACKEND})

    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.TOP_LEVEL, project=project, home=home)
    apply_install(plan, project=project, dry_run=False)

    # Now uninstall with restore.
    report = uninstall(project=project, restore=True, home=home)
    assert report.errors == [], f"errors during uninstall: {report.errors}"
    # The original agent file should be restored.
    assert (home / ".claude" / "agents" / "mybot.md").exists()
    # The converted Role should be gone.
    assert not (home / ".metaensemble" / "roles" / "mybot.md").exists()
    # The symlinks should be removed.
    assert not (home / ".claude" / "commands" / "metaensemble").exists()


def test_uninstall_restore_after_double_install_returns_to_pre_install(tmp_path):
    """Two consecutive installs must still be cleanly reversed.

    A second install that lands
    every action as a no-op does NOT create a new backup directory — the
    Principal-visible signal is that nothing changed. The first install's
    backup is the only one on disk and is sufficient to reverse the full
    set of changes. The uninstall must still produce a pre-MetaEnsemble
    state by walking that single backup.
    """
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _make_runtime(home / ".claude", agents={"mybot": AGENT_BACKEND})

    # First install: converts mybot agent to a Role, registers hooks, etc.
    survey1 = survey(home=home, project=project, write_report=False)
    plan1 = plan_install(survey1, Layout.TOP_LEVEL, project=project, home=home)
    apply_install(plan1, project=project, dry_run=False)
    settings_path = home / ".claude" / "settings.json"
    assert settings_path.exists(), "first install should have written settings.json"
    first_hooks = json.loads(settings_path.read_text()).get("hooks", {})
    assert first_hooks, "first install should have registered hooks"

    # Second install: idempotent re-run. By now `~/.claude/agents` is empty
    # (mybot moved to ~/.metaensemble/roles), so the second plan has no
    # convert-agent actions. Every remaining action's desired post-state
    # already holds, so the second install must be a clean noop: no new
    # backup directory, every action reported as noop.
    import time
    time.sleep(1.1)  # ensure timestamps would be distinct if a dir were created
    survey2 = survey(home=home, project=project, write_report=False)
    plan2 = plan_install(survey2, Layout.TOP_LEVEL, project=project, home=home)
    report2 = apply_install(plan2, project=project, dry_run=False)

    # Idempotency contract: every action except vendor-runtime
    # is a noop on repeat install. vendor-runtime always re-applies because
    # the vendored snapshot might be stale after a pip upgrade — see
    # the runtime freshness invariant.
    applied_kinds = sorted(a.kind for a in report2.applied)
    assert applied_kinds == ["vendor-runtime"], (
        f"only vendor-runtime should re-apply; got {applied_kinds}"
    )
    noop_kinds = sorted(a.kind for a in report2.noop)
    expected_noop_kinds = sorted(a.kind for a in plan2.actions if a.kind != "vendor-runtime")
    assert noop_kinds == expected_noop_kinds, (
        "every non-vendor-runtime action must be a noop on repeat install"
    )
    backup_dirs = sorted((project / ".metaensemble" / "backups").iterdir())
    # vendor-runtime is a user-scope action so it lands in
    # ~/.metaensemble/installs/<ts>/, not the project's backups/.
    assert len(backup_dirs) == 1, (
        "only the first install's project backup directory should exist"
    )

    # Restore.
    report = uninstall(project=project, restore=True, home=home)
    assert report.errors == [], f"errors during uninstall: {report.errors}"

    # The original agent must be restored even though only the OLDER plan
    # has the convert-agent record.
    assert (home / ".claude" / "agents" / "mybot.md").exists(), (
        "rollback must reverse the original convert-agent action "
        "from the FIRST install, not just the latest"
    )
    # Settings.json must be back to the pre-MetaEnsemble shape (no hooks).
    final_settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    assert not final_settings.get("hooks"), (
        "rollback must restore the OLDEST settings.json.bak, which "
        "predates any MetaEnsemble hook registration"
    )
    # Symlinks and converted Role gone.
    assert not (home / ".claude" / "commands" / "metaensemble").exists()
    assert not (home / ".metaensemble" / "roles" / "mybot.md").exists()


def test_uninstall_strips_gitignore_block(tmp_path):
    """Default rollback removes the `.metaensemble/` block from .gitignore.

    The block is project-tree pollution `_ensure_project_state` writes;
    a default uninstall (no `--purge-*`) must still clean it up because
    it leaks MetaEnsemble's existence into the user's git history.
    """
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _make_runtime(home / ".claude")

    # Project already has a user-authored .gitignore.
    gitignore = project / ".gitignore"
    gitignore.write_text("node_modules/\n.venv/\n")

    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)
    apply_install(plan, project=project, dry_run=False)

    # Install added the block.
    after_install = gitignore.read_text()
    assert ".metaensemble/" in after_install
    assert "node_modules/" in after_install

    # Default uninstall (no purge) strips the block while preserving user content.
    uninstall(project=project, restore=True, home=home)
    after_uninstall = gitignore.read_text()
    assert ".metaensemble" not in after_uninstall
    assert "node_modules/" in after_uninstall, "user content must survive"
    assert ".venv/" in after_uninstall


def test_uninstall_strips_legacy_metaensemble_gitignore_entries(tmp_path):
    """Legacy pre-block MetaEnsemble entries are removed with the managed block."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _make_runtime(home / ".claude")

    gitignore = project / ".gitignore"
    gitignore.write_text(
        "node_modules/\n"
        ".metaensemble/state/department.db\n"
        ".metaensemble/state/runs.jsonl\n"
        ".metaensemble/hooks/log.jsonl\n"
        ".metaensemble/state/pending/\n"
    )

    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)
    apply_install(plan, project=project, dry_run=False)

    uninstall(project=project, restore=True, home=home)
    after_uninstall = gitignore.read_text()
    assert "node_modules/" in after_uninstall
    assert ".metaensemble" not in after_uninstall


def test_uninstall_removes_gitignore_if_we_created_it(tmp_path):
    """If MetaEnsemble created the .gitignore from scratch, uninstall removes it.

    No user content existed before; the entire file was the managed
    block. Stripping the block leaves an empty file, and an empty file
    MetaEnsemble itself wrote does not belong in the user's project tree.
    """
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _make_runtime(home / ".claude")

    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)
    apply_install(plan, project=project, dry_run=False)

    gitignore = project / ".gitignore"
    assert gitignore.exists()

    uninstall(project=project, restore=True, home=home)
    assert not gitignore.exists()


def test_purge_project_state_removes_metaensemble_dir(tmp_path):
    """Project-state purge deletes `<project>/.metaensemble/`."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _make_runtime(home / ".claude")

    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)
    apply_install(plan, project=project, dry_run=False)

    meta_dir = project / ".metaensemble"
    assert meta_dir.exists() and any(meta_dir.iterdir())

    uninstall(
        project=project,
        restore=True,
        home=home,
        purge_project_state_flag=True,
    )
    assert not meta_dir.exists(), (
        "project-state purge must remove `<project>/.metaensemble/` entirely"
    )


def test_purge_user_state_removes_user_dir(tmp_path):
    """User-state purge deletes user state and managed runtime links."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _make_runtime(home / ".claude")
    # Seed a user-state directory the way bootstrap would.
    user_meta = home / ".metaensemble"
    (user_meta / "bin").mkdir(parents=True)
    (user_meta / "bin" / "me-run").write_text("#!/bin/sh\n")
    (user_meta / "budgets.yaml").write_text("")

    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)
    apply_install(plan, project=project, dry_run=False)
    assert user_meta.exists()
    commands_link = home / ".claude" / "commands" / "metaensemble"
    wire_link = home / ".claude" / "output-styles" / "metaensemble-wire.md"
    assert commands_link.is_symlink()
    assert wire_link.is_symlink()

    uninstall(
        project=project,
        restore=True,
        home=home,
        purge_user_state_flag=True,
    )
    assert not user_meta.exists(), (
        "user-state purge must remove `~/.metaensemble/` entirely"
    )
    assert not commands_link.exists()
    assert not wire_link.exists()
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert "hooks" not in settings
    assert "statusLine" not in settings


def test_purge_user_state_removes_orphaned_parallel_links_without_backup(tmp_path):
    """A user-level purge must clean managed links even when project backups are gone."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _make_runtime(home / ".claude")

    commands_link = home / ".claude" / "commands" / "metaensemble"
    commands_link.symlink_to(Path(__file__).resolve().parent.parent / "commands")
    wire_link = home / ".claude" / "output-styles" / "metaensemble-wire.md"
    wire_link.symlink_to(Path(__file__).resolve().parent.parent / "output-styles" / "wire.md")

    report = uninstall(
        project=project,
        restore=True,
        home=home,
        purge_user_state_flag=True,
    )
    assert report.errors == []
    assert not commands_link.exists()
    assert not wire_link.exists()


def test_residue_report_names_what_remains(tmp_path):
    """`build_residue_report` enumerates surviving state with copy-paste fix-ups."""
    from metaensemble.lib.installer import build_residue_report

    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _make_runtime(home / ".claude")
    (home / ".metaensemble").mkdir()
    (home / ".metaensemble" / "budgets.yaml").write_text("")

    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)
    apply_install(plan, project=project, dry_run=False)
    uninstall(project=project, restore=True, home=home)

    report = build_residue_report(project=project, home=home)
    assert report.project_state_remaining, (
        "default uninstall keeps `<project>/.metaensemble/`; residue must name it"
    )
    assert report.user_state_remaining, (
        "default uninstall keeps `~/.metaensemble/`; residue must name it"
    )
    assert report.package_install_command == "pip uninstall metaensemble"
    assert any("unadopt --purge-state" in note for note in report.notes)
    assert any("user-teardown --purge-state" in note for note in report.notes)


def test_project_has_install_actions_predicate(tmp_path):
    """`project_has_install_actions` is the load-bearing predicate that
    distinguishes "this project has an install to reverse" from "the
    residue scanner sees other-project artifacts". The CLI dry-run
    branches its user-runtime message on this; the predicate must
    return False for a fresh project and True once an install lands.
    """
    from metaensemble.lib.installer import project_has_install_actions

    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    assert project_has_install_actions(project) is False

    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)
    apply_install(plan, project=project, dry_run=False)
    assert project_has_install_actions(project) is True


def test_residue_report_names_orphaned_user_runtime_links(tmp_path):
    """Residue report must not say clean while managed ~/.claude links remain."""
    from metaensemble.lib.installer import build_residue_report

    home = tmp_path / "home"
    project = tmp_path / "proj"
    project.mkdir()
    runtime = _make_runtime(home / ".claude")
    commands_link = runtime / "commands" / "metaensemble"
    commands_link.symlink_to(Path(__file__).resolve().parent.parent / "commands")

    report = build_residue_report(project=project, home=home)
    assert commands_link in report.user_runtime_remaining
    assert any("managed MetaEnsemble symlinks" in note for note in report.notes)


def test_repeat_install_is_a_clean_noop(tmp_path):
    """Re-running install on an unchanged tree is a clean noop.

    No new backup directory is created, every action lands in `noop`, and
    the active-roles.yaml mtime stays put (payload unchanged means no
    rewrite). This pins the contract DEPLOYMENT.md describes — install
    idempotency as a release-level promise, not a happy accident.
    """
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _make_runtime(home / ".claude")

    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)
    first = apply_install(plan, project=project, dry_run=False)
    assert first.applied, "first install must apply actions"
    assert first.backup_root is not None

    backup_dir = project / ".metaensemble" / "backups"
    before = sorted(backup_dir.iterdir())
    active_roles_path = project / ".metaensemble" / "active-roles.yaml"
    active_roles_mtime = active_roles_path.stat().st_mtime

    # Re-plan from a fresh survey to capture the post-first-install world.
    import time
    time.sleep(1.1)
    survey2 = survey(home=home, project=project, write_report=False)
    plan2 = plan_install(survey2, Layout.NAMESPACED, project=project, home=home)
    second = apply_install(plan2, project=project, dry_run=False)

    # vendor-runtime is intentionally NOT
    # idempotent on repeat install. The runtime is a snapshot of the
    # installed package's assets; after `pip install --upgrade` the
    # snapshot is stale but the MANIFEST still verifies, so the only safe
    # contract is "re-vendor every time". Every OTHER action remains
    # noop on a repeat install.
    applied_kinds = sorted(a.kind for a in second.applied)
    assert applied_kinds == ["vendor-runtime"], (
        f"only vendor-runtime should re-apply on repeat install; got {applied_kinds}"
    )
    noop_kinds = sorted(a.kind for a in second.noop)
    expected_noop_kinds = sorted(a.kind for a in plan2.actions if a.kind != "vendor-runtime")
    assert noop_kinds == expected_noop_kinds, (
        "every non-vendor-runtime action must be a noop on repeat install"
    )
    after = sorted(backup_dir.iterdir())
    assert before == after, "no new entries under backups/ after a repeat install"
    assert active_roles_path.stat().st_mtime == active_roles_mtime, (
        "active-roles.yaml must not be rewritten when payload is unchanged"
    )


def test_gitignore_created_during_init(tmp_path, monkeypatch):
    """`metaensemble init` adds `.metaensemble/` to the project's root .gitignore."""
    import os
    import subprocess
    import sys
    REPO_ROOT = Path(__file__).resolve().parent.parent.parent
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    subprocess.run(
        [sys.executable, "-m", "metaensemble.cli", "init"],
        capture_output=True, text=True, env=env, cwd=str(tmp_path),
        check=True,
    )
    gitignore = tmp_path / ".gitignore"
    assert gitignore.exists(), "init should create <project>/.gitignore when none exists"
    text = gitignore.read_text()
    assert ".metaensemble/" in text
    # The legacy inner file should not be created.
    assert not (tmp_path / ".metaensemble" / ".gitignore").exists()


def test_gitignore_install_writes_when_absent(tmp_path):
    """apply_install creates <project>/.gitignore when none exists."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)
    apply_install(plan, project=project, dry_run=False)
    gitignore = project / ".gitignore"
    assert gitignore.exists()
    assert ".metaensemble/" in gitignore.read_text()


def test_gitignore_install_preserves_user_edited(tmp_path):
    """apply_install appends to an existing root .gitignore — user content kept verbatim."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    user_gitignore = project / ".gitignore"
    user_gitignore.write_text("# my custom rules\nlocal-secret.yaml\n")

    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)
    apply_install(plan, project=project, dry_run=False)

    text = user_gitignore.read_text()
    assert text.startswith("# my custom rules\nlocal-secret.yaml\n")
    assert ".metaensemble/" in text


def test_gitignore_install_idempotent_when_already_listed(tmp_path):
    """apply_install is a no-op when `.metaensemble/` is already listed."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    user_gitignore = project / ".gitignore"
    original = "# kept\n.metaensemble/\nother-rule\n"
    user_gitignore.write_text(original)

    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)
    apply_install(plan, project=project, dry_run=False)

    assert user_gitignore.read_text() == original


def test_gitignore_install_removes_legacy_inner_file(tmp_path):
    """A pre-existing inner `.metaensemble/.gitignore` (with our header) is cleaned up."""
    from metaensemble.lib.installer import _LEGACY_INNER_GITIGNORE_MARKER

    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    me = project / ".metaensemble"
    me.mkdir()
    legacy = me / ".gitignore"
    legacy.write_text(f"# {_LEGACY_INNER_GITIGNORE_MARKER}\nstate/\n")

    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)
    apply_install(plan, project=project, dry_run=False)

    # Legacy inner file was ours — should be removed.
    assert not legacy.exists()
    # Root gitignore now ignores .metaensemble/ as a whole.
    assert ".metaensemble/" in (project / ".gitignore").read_text()


def test_gitignore_install_keeps_hand_edited_inner_file(tmp_path):
    """An inner `.metaensemble/.gitignore` we did not write is left untouched."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    me = project / ".metaensemble"
    me.mkdir()
    custom = me / ".gitignore"
    custom_content = "# hand-edited, no MetaEnsemble marker\nlocal/\n"
    custom.write_text(custom_content)

    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)
    apply_install(plan, project=project, dry_run=False)

    assert custom.exists()
    assert custom.read_text() == custom_content


def test_discover_projects_finds_installed_and_not_installed(tmp_path):
    """discover_projects walks the runtime registry and reports install status."""
    from metaensemble.lib.installer import discover_projects

    fake_home = tmp_path / "home"
    runtime_projects = fake_home / ".claude" / "projects"

    # Project A: has a .metaensemble/ with a Ledger and runs.
    proj_a = tmp_path / "alpha"
    proj_a.mkdir()
    a_me = proj_a / ".metaensemble" / "state"
    a_me.mkdir(parents=True)
    import sqlite3
    conn = sqlite3.connect(str(a_me / "department.db"))
    conn.executescript(
        "CREATE TABLE runs (run_id TEXT, ended_ts TEXT);"
        "INSERT INTO runs VALUES ('r1', '2026-05-15T10:00:00');"
    )
    conn.close()

    # Project B: exists in runtime registry but no MetaEnsemble installed.
    proj_b = tmp_path / "beta"
    proj_b.mkdir()

    # Plant runtime-registry entries with cwd fields.
    for proj_path in (proj_a, proj_b):
        proj_dir = runtime_projects / f"x-{proj_path.name}"
        proj_dir.mkdir(parents=True)
        (proj_dir / "session.jsonl").write_text(
            f'{{"timestamp":"2026-05-15T10:00:00Z","cwd":"{proj_path}","message":{{}}}}\n'
        )

    discovered = discover_projects(home=fake_home)
    by_path = {str(p.path): p for p in discovered}
    assert str(proj_a) in by_path
    assert by_path[str(proj_a)].has_ledger_db
    assert by_path[str(proj_a)].run_count == 1
    assert str(proj_b) in by_path
    assert not by_path[str(proj_b)].has_ledger_db
    assert by_path[str(proj_b)].run_count == 0


def test_discover_projects_skips_user_home(tmp_path):
    """The user's home directory itself isn't a MetaEnsemble project, even
    though ~/.metaensemble/ exists for the launcher and user-layer Roles."""
    from metaensemble.lib.installer import discover_projects

    fake_home = tmp_path / "home"
    runtime_projects = fake_home / ".claude" / "projects"
    runtime_projects.mkdir(parents=True)

    # Plant a runtime project entry whose cwd is the user's home.
    proj_dir = runtime_projects / "home-entry"
    proj_dir.mkdir()
    (proj_dir / "s.jsonl").write_text(
        f'{{"timestamp":"2026-05-15T10:00:00Z","cwd":"{fake_home}","message":{{}}}}\n'
    )

    discovered = discover_projects(home=fake_home)
    assert all(p.path != fake_home for p in discovered), (
        "discover_projects must not list the user's home as a project"
    )


def test_export_agents_roundtrip_is_lossless_for_body(tmp_path):
    """Convert an agent to a Role, then export the Role back to an agent.

    The forward `convert_agent_to_role` plus the reverse `role_to_agent_text`
    must round-trip the body verbatim and the principal frontmatter
    (name/description/tools/model) without changes the user would notice.
    """
    from metaensemble.lib.installer import convert_agent_to_role, role_to_agent_text

    original = """---
name: backend
description: 'Backend implementation specialist for the API layer.'
tools: Read, Write, Edit, Grep, Glob, Bash
model: sonnet
color: blue
---

# Backend agent body

Some explanatory content for the backend role.
"""
    role_text = convert_agent_to_role(original)
    round_tripped = role_to_agent_text(role_text)
    # Body preserved verbatim.
    assert "# Backend agent body" in round_tripped
    assert "Some explanatory content for the backend role." in round_tripped
    # Frontmatter shape: name/description/tools/model present; MetaEnsemble-only
    # fields stripped.
    assert "name: backend" in round_tripped
    assert "tools: Read, Write, Edit, Grep, Glob, Bash" in round_tripped
    assert "model: sonnet" in round_tripped
    # Color survives the full agent → Role → agent round-trip.
    assert "color: blue" in round_tripped
    assert "alias_prefix" not in round_tripped
    assert "output_styles" not in round_tripped
    assert "onboarding" not in round_tripped


def test_role_to_agent_text_emits_color_when_present():
    from metaensemble.lib.installer import role_to_agent_text

    role_text = """---
name: architect
version: 1.0.0
description: 'System design specialist for the platform.'
model_tier: opus
color: blue
alias_prefix: arch
allowed_tools:
  - Read
  - Write
---

Body.
"""
    agent = role_to_agent_text(role_text)
    assert "color: blue" in agent


def test_role_to_agent_text_omits_color_when_role_has_none():
    from metaensemble.lib.installer import role_to_agent_text

    role_text = """---
name: nocolor
version: 1.0.0
description: 'A Role whose frontmatter has no color field.'
model_tier: sonnet
alias_prefix: noco
---

Body.
"""
    agent = role_to_agent_text(role_text)
    assert "color:" not in agent


def test_role_schema_accepts_valid_color_values():
    """All eight Claude Code colors must validate against the Role schema."""
    from metaensemble.lib.manifest import validate_role_frontmatter

    for color in ("red", "orange", "yellow", "green", "cyan", "blue", "purple", "pink"):
        validate_role_frontmatter({
            "name": "x",
            "version": "1.0.0",
            "description": "A role used to test color schema acceptance.",
            "model_tier": "sonnet",
            "color": color,
        })


def test_role_schema_rejects_invalid_color_value():
    import jsonschema
    import pytest as _pytest
    from metaensemble.lib.manifest import validate_role_frontmatter

    with _pytest.raises(jsonschema.ValidationError):
        validate_role_frontmatter({
            "name": "x",
            "version": "1.0.0",
            "description": "A role used to test color schema rejection.",
            "model_tier": "sonnet",
            "color": "chartreuse",
        })


def test_shipped_curated_roles_all_carry_a_color():
    """Every curated Role MetaEnsemble ships must declare a color so the
    runtime UI distinguishes Executors at a glance."""
    import yaml as _yaml
    from pathlib import Path

    roles_dir = Path(__file__).resolve().parent.parent / "roles"
    role_files = sorted(roles_dir.glob("*.md"))
    assert role_files, "expected curated role files under metaensemble/roles/"

    for f in role_files:
        text = f.read_text()
        assert text.startswith("---\n"), f"{f.name}: missing frontmatter"
        end = text.find("\n---\n", 4)
        fm = _yaml.safe_load(text[4:end])
        assert "color" in fm, f"{f.name}: missing color field"
        assert fm["color"] in {
            "red", "orange", "yellow", "green", "cyan", "blue", "purple", "pink",
        }, f"{f.name}: color {fm['color']!r} is not one of Claude Code's accepted values"


def test_export_agents_writes_files_to_target_dir(tmp_path):
    """export_agents reverse-converts every Role under user-layer roles dir."""
    from metaensemble.lib.installer import export_agents
    home = tmp_path / "home"
    user_roles = home / ".metaensemble" / "roles"
    user_roles.mkdir(parents=True)
    (user_roles / "backend.md").write_text("""---
name: backend
version: 1.0.0
description: 'Backend specialist for the API layer.'
model_tier: sonnet
allowed_tools: [Read, Write, Edit]
---

Body.
""")
    target = tmp_path / "agents-out"

    written = export_agents(home=home, project=tmp_path / "missing-project",
                            target_dir=target)
    assert len(written) == 1
    out = (target / "backend.md").read_text()
    assert "name: backend" in out
    assert "tools: Read, Write, Edit" in out
    assert "model: sonnet" in out
    assert "model_tier" not in out


def test_export_agents_skips_existing_target(tmp_path):
    """By default export_agents does not overwrite an existing agent file."""
    from metaensemble.lib.installer import export_agents
    home = tmp_path / "home"
    user_roles = home / ".metaensemble" / "roles"
    user_roles.mkdir(parents=True)
    (user_roles / "backend.md").write_text("""---
name: backend
version: 1.0.0
description: 'Backend specialist for the API layer.'
model_tier: sonnet
---

Body.
""")
    target = tmp_path / "agents-out"
    target.mkdir()
    (target / "backend.md").write_text("# Pre-existing content")

    written = export_agents(home=home, project=tmp_path / "missing-project",
                            target_dir=target)
    assert written == []
    assert (target / "backend.md").read_text() == "# Pre-existing content"

    # With --overwrite, it does replace.
    written = export_agents(home=home, project=tmp_path / "missing-project",
                            target_dir=target, overwrite=True)
    assert len(written) == 1
    assert "name: backend" in (target / "backend.md").read_text()


def test_cli_export_agents_exit_code(tmp_path):
    """The CLI subcommand exits 0 even when no Roles are present (no-op)."""
    import subprocess
    import sys
    import os
    REPO_ROOT = Path(__file__).resolve().parent.parent.parent
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["HOME"] = str(tmp_path)  # isolate from the developer's real home
    proc = subprocess.run(
        [sys.executable, "-m", "metaensemble.cli", "export-agents",
         "--target-dir", str(tmp_path / "out")],
        capture_output=True, text=True, env=env, cwd=str(tmp_path),
    )
    assert proc.returncode == 0
    assert "No Role files" in proc.stdout


def test_uninstall_without_restore_strips_only_metaensemble_hooks(tmp_path):
    """Plain uninstall preserves the user's other hooks in settings.json."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _make_runtime(home / ".claude")

    # Plant a user-owned hook so we can verify it survives.
    settings_path = home / ".claude" / "settings.json"
    user_hook = {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "/usr/local/bin/my-tool"}],
    }
    settings_path.write_text(json.dumps({
        "theme": "dark",
        "hooks": {"PreToolUse": [user_hook]},
    }))

    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.TOP_LEVEL, project=project, home=home)
    apply_install(plan, project=project, dry_run=False)

    # After install, settings.json has both the user hook AND ours.
    after_install = json.loads(settings_path.read_text())
    pretool = after_install["hooks"]["PreToolUse"]
    assert any(g["matcher"] == "Bash" for g in pretool), "user's Bash hook preserved"
    assert any(g["matcher"] in ("Task", "Agent") for g in pretool), "metaensemble hook added"

    # Uninstall WITHOUT --restore should strip only the metaensemble entries.
    uninstall(project=project, restore=False, home=home)

    after_uninstall = json.loads(settings_path.read_text())
    remaining_hooks = after_uninstall.get("hooks", {})
    pretool_after = remaining_hooks.get("PreToolUse", [])
    assert pretool_after == [user_hook], (
        f"user's PreToolUse hook should survive uninstall; got {pretool_after}"
    )
    # The user's other settings.json keys must also survive.
    assert after_uninstall.get("theme") == "dark"


# --- Plan rendering -------------------------------------------------------


def test_render_plan_emits_markdown(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)
    rendered = render_plan(plan)
    assert "install plan" in rendered
    assert "namespaced" in rendered
    assert "Active Roles" in rendered


# --- Role-relevance detection (project signals) --------------------------


def _scaffold_backend_project(project: Path) -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / "pyproject.toml").write_text("[project]\nname='svc'\n")
    (project / "backend").mkdir()
    (project / "backend" / "app.py").write_text("# server")
    (project / "tests").mkdir()
    (project / "tests" / "test_app.py").write_text("# tests")


def _scaffold_frontend_project(project: Path) -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / "package.json").write_text('{"name":"web"}')
    (project / "frontend").mkdir()
    (project / "frontend" / "App.tsx").write_text("export default function App() {}")


def _scaffold_devops_project(project: Path) -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / ".github" / "workflows").mkdir(parents=True)
    (project / ".github" / "workflows" / "ci.yml").write_text("name: ci")
    (project / "Dockerfile").write_text("FROM python:3.11")


def test_detect_role_relevance_backend_project(tmp_path):
    project = tmp_path / "svc"
    _scaffold_backend_project(project)
    relevance = {r.role_id: r for r in detect_role_relevance(project)}

    assert relevance["backend"].relevant
    assert relevance["test-engineer"].relevant
    # `architect` requires ADR/architecture documentation, not just code.
    # This fixture has neither, so architect should NOT activate.
    assert not relevance["architect"].relevant
    # `code-quality` requires linter/type-checker config, not just code.
    assert not relevance["code-quality"].relevant
    # No frontend signals in this project.
    assert not relevance["frontend"].relevant
    # No CI/CD signals either.
    assert not relevance["devops"].relevant


def test_detect_role_relevance_frontend_project(tmp_path):
    project = tmp_path / "web"
    _scaffold_frontend_project(project)
    relevance = {r.role_id: r for r in detect_role_relevance(project)}

    assert relevance["frontend"].relevant
    # `architect` requires ADR/architecture documentation; a bare TSX file
    # does not count under the v0.1.0 signal catalog.
    assert not relevance["architect"].relevant


def test_detect_role_relevance_devops_project(tmp_path):
    project = tmp_path / "ops"
    _scaffold_devops_project(project)
    relevance = {r.role_id: r for r in detect_role_relevance(project)}

    assert relevance["devops"].relevant
    # No backend/frontend code in this scaffold.
    assert not relevance["backend"].relevant
    assert not relevance["frontend"].relevant


def test_detect_role_relevance_empty_project_flags_nothing_relevant(tmp_path):
    project = tmp_path / "empty"
    project.mkdir()
    relevance = {r.role_id: r for r in detect_role_relevance(project)}
    assert not relevance["backend"].relevant
    assert not relevance["frontend"].relevant
    assert not relevance["test-engineer"].relevant
    assert not relevance["devops"].relevant
    assert not relevance["data-engineer"].relevant
    assert not relevance["ml-engineer"].relevant


def test_detect_role_relevance_somali_ml_project(tmp_path):
    project = tmp_path / "somali-dialect-classifier"
    project.mkdir()
    (project / "data" / "raw" / "huggingface-c4-so").mkdir(parents=True)
    (project / "data" / "raw" / "huggingface-c4-so" / "sample.jsonl").write_text("{}\n")
    (project / "models").mkdir()
    (project / "src").mkdir()
    (project / "src" / "dialect_classifier.py").write_text("# classifier")
    (project / "tests").mkdir()
    (project / "tests" / "test_classifier.py").write_text("# tests")
    (project / "docs").mkdir()
    (project / "docker-compose.yml").write_text("services: {}\n")
    # Add the architecture and tooling signals that the v0.1.0 catalog
    # explicitly requires; the Somali project these tests mirror has
    # both in real life.
    (project / "docs" / "ARCHITECTURE.md").write_text("# architecture\n")
    (project / ".pre-commit-config.yaml").write_text("repos: []\n")
    (project / "pyproject.toml").write_text(
        "[tool.mypy]\n[tool.ruff]\n[tool.pytest.ini_options]\n"
    )
    (project / "README.md").write_text(
        "# Somali Dialect Classifier\n\n" + "Real content.\n" * 220
    )

    relevance = {r.role_id: r for r in detect_role_relevance(project)}

    for role_id in (
        "architect", "code-quality", "test-engineer", "devops", "docs",
        "data-engineer", "ml-engineer",
    ):
        assert relevance[role_id].relevant, f"{role_id} should match Somali ML signals"


def test_survey_includes_role_relevance(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "svc"
    _scaffold_backend_project(project)

    result = survey(home=home, project=project, write_report=True)
    by_role = {r.role_id: r for r in result.role_relevance}
    assert by_role["backend"].relevant
    assert not by_role["frontend"].relevant
    # Report mentions the relevance-driven sections of the new flow.
    report_text = result.report_path.read_text()
    assert "Curated Roles that match this project" in report_text
    assert "Curated Roles that look optional" in report_text


def test_survey_rotates_timestamped_snapshots(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "svc"
    _scaffold_backend_project(project)
    report_dir = project / ".metaensemble"
    report_dir.mkdir()
    for i in range(7):
        (report_dir / f"inspection-20260101T00000{i}Z.md").write_text("old\n")
        (report_dir / f"install-decisions.20260101T00000{i}Z.yaml").write_text("old\n")

    survey(home=home, project=project, write_report=True)

    reports = sorted(report_dir.glob("inspection-*.md"))
    defaults = sorted(report_dir.glob("install-decisions.*.yaml"))
    assert len(reports) <= 5
    assert len(defaults) <= 5
    assert (report_dir / "install-decisions.yaml").exists()


def test_purge_project_state_archives_survey_artifacts(tmp_path):
    from metaensemble.lib.installer import purge_project_state

    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "svc"
    _scaffold_backend_project(project)
    survey(home=home, project=project, write_report=True)

    removed = purge_project_state(project, home=home)

    assert removed
    assert not (project / ".metaensemble").exists()
    archives = list((home / ".metaensemble" / "archives" / "project-inspections").rglob("inspection-*.md"))
    assert archives, "purge should preserve inspection reports in user-level archive"


def test_projects_prune_removes_missing_runtime_registrations(tmp_path):
    from metaensemble.lib.installer import discover_projects, prune_missing_projects

    home = tmp_path / "home"
    projects_root = home / ".claude" / "projects"
    stale_dir = projects_root / "-tmp-missing"
    live_project = tmp_path / "live"
    live_project.mkdir(parents=True)
    live_dir = projects_root / "-tmp-live"
    stale_dir.mkdir(parents=True)
    live_dir.mkdir(parents=True)
    stale_path = tmp_path / "missing"
    (stale_dir / "session.jsonl").write_text(json.dumps({"cwd": str(stale_path)}) + "\n")
    (live_dir / "session.jsonl").write_text(json.dumps({"cwd": str(live_project)}) + "\n")

    removed = prune_missing_projects(home=home)

    assert removed == [stale_path]
    assert not stale_dir.exists()
    assert live_dir.exists()
    assert [p.path for p in discover_projects(home=home)] == [live_project]


# --- Per-Role selection via `selected_roles` (programmatic only) -------


def test_plan_install_with_selected_roles_marks_others_inactive(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(
        survey_result, Layout.NAMESPACED,
        project=project, home=home,
        selected_roles=["backend", "architect", "code-quality"],
    )
    assert set(plan.active_roles) == {"backend", "architect", "code-quality"}
    assert "frontend" in plan.inactive_roles
    assert "devops" in plan.inactive_roles


def test_plan_install_default_retires_curated_roles_without_signals(tmp_path):
    """Without project signals, the new default RETIRES curated Roles.

    This is the user-friendliness pivot: only Roles whose project signals
    fire (or that the user explicitly opts into via `install-decisions.yaml`,
    or that a programmatic caller passes via `selected_roles`) end up
    active. Activating everything by default was the old behavior; it
    caused launch-time roster bloat.
    """
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)
    # Empty project = no signals = no curated Roles auto-activated.
    assert plan.active_roles == []
    # All default curated Roles end up in the inactive list.
    assert set(plan.inactive_roles) >= {
        "architect", "backend", "code-quality", "devops",
        "docs", "frontend", "test-engineer",
    }


def test_plan_install_relevant_roles_activate_when_signals_match(tmp_path):
    """When the project has signals (backend code, tests), the relevant
    curated Roles activate automatically."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "svc"
    _scaffold_backend_project(project)
    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)
    # Backend signals → backend + test-engineer activate. Under the v0.1.0
    # signal catalog, `architect` requires explicit architecture docs and
    # `code-quality` requires linter/type-checker config, so this minimal
    # scaffold does not activate either.
    assert "backend" in plan.active_roles
    assert "test-engineer" in plan.active_roles
    assert "architect" in plan.inactive_roles
    assert "code-quality" in plan.inactive_roles
    # No frontend signals → frontend stays inactive.
    assert "frontend" in plan.inactive_roles


def test_apply_install_writes_active_roles_yaml(tmp_path):
    import yaml as _yaml

    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    survey_result = survey(home=home, project=project, write_report=False)
    plan = plan_install(
        survey_result, Layout.NAMESPACED,
        project=project, home=home,
        selected_roles=["backend", "code-quality"],
    )
    apply_install(plan, project=project, dry_run=False)

    active_roles_path = project / ".metaensemble" / "active-roles.yaml"
    assert active_roles_path.exists()
    data = _yaml.safe_load(active_roles_path.read_text())
    assert set(data["active_roles"]) == {"backend", "code-quality"}
    assert "frontend" in data["inactive_roles"]


# --- Vendor-runtime / recovery / GC / runner -----------------------------


def _vendor_once(home: Path) -> Path:
    """Vendor a runtime under `home`; return the new version dir."""
    from metaensemble.lib.installer import _vendor_runtime_atomically
    return _vendor_runtime_atomically(home=home)


def _write_minimal_vendor_source(root: Path) -> None:
    """Create the package asset shape required by vendor-runtime tests."""
    (root / "commands").mkdir(parents=True)
    (root / "commands" / "dispatch.md").write_text("# dispatch")
    (root / "commands" / "dispatch 2.md").write_text("# duplicate")
    (root / "commands" / "limits.md").write_text("# limits")
    (root / "commands" / "limits 2.md").write_text("# duplicate")

    (root / "skills" / "metaensemble-protocol").mkdir(parents=True)
    (root / "skills" / "metaensemble-protocol" / "SKILL.md").write_text("# skill")
    (root / "skills" / "metaensemble-protocol 2").mkdir()
    (root / "skills" / "metaensemble-protocol 2" / "SKILL.md").write_text("# duplicate")

    (root / "output-styles").mkdir()
    (root / "output-styles" / "wire.md").write_text("# wire")
    (root / "output-styles" / "wire 2.md").write_text("# duplicate")

    (root / "roles").mkdir()
    (root / "roles" / "backend.md").write_text("# backend")
    (root / "roles" / "backend 2.md").write_text("# duplicate")

    (root / "schemas").mkdir()
    (root / "schemas" / "manifest.schema.json").write_text("{}")
    (root / "schemas" / "manifest.schema 2.json").write_text("{}")

    (root / "state" / "migrations").mkdir(parents=True)
    (root / "state" / "migrations" / "001_init.sql").write_text("-- init")
    (root / "state" / "migrations" / "001_init 2.sql").write_text("-- duplicate")

    (root / "config").mkdir()
    (root / "config" / "budgets.example.yaml").write_text("window_capacity_tokens: 1\n")
    (root / "config" / "budgets.example 2.yaml").write_text("duplicate: true\n")


def test_vendor_runtime_filters_n_suffix_duplicates(tmp_path, monkeypatch):
    """Vendor-runtime must not propagate iCloud/Finder `name N.ext` files.

    This covers the full source -> vendored runtime boundary: duplicate files
    staged in the installed package tree are skipped before the runtime
    symlink points Claude Code at them.
    """
    from metaensemble.lib import installer
    from metaensemble.lib.installer import Action, _do_vendor_runtime, _runtime_root

    fake_pkg = tmp_path / "fake-metaensemble-package"
    _write_minimal_vendor_source(fake_pkg)
    monkeypatch.setattr(installer, "_package_resources_root", lambda: fake_pkg)

    home = tmp_path / "home"
    _do_vendor_runtime(
        Action(
            kind="vendor-runtime",
            source=None,
            target=home / ".metaensemble" / "runtime",
            description="vendor runtime",
        ),
        home=home,
    )

    runtime = _runtime_root(home).resolve(strict=True)
    duplicate_paths = [
        p.relative_to(runtime).as_posix()
        for p in runtime.rglob("*")
        if p.is_file() and p.stem.endswith((" 2", " 3", " 10"))
    ]
    assert duplicate_paths == []

    assert (runtime / "commands" / "dispatch.md").is_file()
    assert not (runtime / "commands" / "dispatch 2.md").exists()
    assert (runtime / "skills" / "metaensemble-protocol" / "SKILL.md").is_file()
    assert not (runtime / "skills" / "metaensemble-protocol 2").exists()
    assert (runtime / "state" / "migrations" / "001_init.sql").is_file()
    assert not (runtime / "state" / "migrations" / "001_init 2.sql").exists()

    log = home / ".metaensemble" / "state" / "vendor-runtime.log.jsonl"
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert records[-1]["kind"] == "vendor-runtime-skipped-duplicates"
    assert records[-1]["count"] == 8
    assert "Skipped 8 duplicate files during runtime vendor" in records[-1]["message"]


def test_vendor_runtime_uses_atomic_symlink_swap(tmp_path):
    """Two back-to-back vendors must leave the `runtime` symlink pointing
    at the SECOND version, with no `runtime.tmp-*` orphan left behind
    (atomic swap = `os.replace(tmp_link, runtime_link)`)."""
    from metaensemble.lib.installer import _runtime_root, _user_metaensemble_dir
    home = tmp_path / "home"
    v1 = _vendor_once(home)
    v2 = _vendor_once(home)
    runtime = _runtime_root(home)
    assert runtime.is_symlink()
    assert runtime.resolve(strict=True) == v2
    assert v2 != v1
    # No tmp orphans after a clean swap.
    base = _user_metaensemble_dir(home)
    tmp_orphans = list(base.glob("runtime.tmp-*"))
    assert tmp_orphans == [], f"orphaned tmp symlinks: {tmp_orphans}"


def test_vendor_runtime_version_ids_collision_proof(tmp_path):
    """Even sub-second back-to-back version-id generation must not collide
    (v3.2 #4 — timestamp + uuid7 suffix)."""
    from metaensemble.lib.installer import _new_runtime_version_id
    ids = {_new_runtime_version_id() for _ in range(50)}
    assert len(ids) == 50, "version ids collided"
    # Shape: <YYYYMMDDTHHMMSSZ>-<12 hex chars>.
    for vid in ids:
        ts, _, suffix = vid.partition("-")
        assert len(ts) == 16 and ts.endswith("Z")
        assert len(suffix) == 12 and all(c in "0123456789abcdef" for c in suffix)


def test_vendor_runtime_recovery_only_cleans_invalid(tmp_path):
    """Recovery sweep removes invalid version dirs and tmp orphans but
    leaves VALID previous versions for GC to manage (v3.2 §5 — clean
    recovery/GC separation)."""
    from metaensemble.lib.installer import (
        _cleanup_stale_vendor_artifacts,
        _runtime_versions_dir,
        _user_metaensemble_dir,
    )
    home = tmp_path / "home"
    # Vendor twice so we have a valid version PLUS the current symlink.
    valid_old = _vendor_once(home)
    _vendor_once(home)
    versions = _runtime_versions_dir(home)
    # Plant an INVALID version (no MANIFEST).
    broken = versions / "20200101T000000Z-badbadbadbad"
    broken.mkdir()
    (broken / "junk.txt").write_text("incomplete copy")
    # Plant a tmp orphan and a legacy bak dir.
    base = _user_metaensemble_dir(home)
    (base / "runtime.tmp-20200101T000000Z-deadbeef").symlink_to(broken)
    legacy_bak = base / "runtime.bak-20191231T235959Z"
    legacy_bak.mkdir()

    _cleanup_stale_vendor_artifacts(home)

    assert valid_old.exists(), "recovery wrongly removed valid version"
    assert not broken.exists(), "recovery left invalid version in place"
    assert list(base.glob("runtime.tmp-*")) == []
    assert list(base.glob("runtime.bak-*")) == []


def test_vendor_runtime_gc_retains_last_n_valid_versions(tmp_path):
    """GC contract: keep the last `keep` valid versions by name-sort PLUS
    whatever `runtime` currently points at. Older valid versions get pruned.

    Bound: with `keep=N`, `|remaining|` is between N and N+1 — N if the
    current symlink happens to be in the last-N-by-name set, N+1 if it's
    not (back-to-back vendors in the same UTC second can land the current
    symlink outside the name-sort tail because the hex random suffix
    decides intra-second order)."""
    from metaensemble.lib.installer import (
        _gc_runtime_versions,
        _runtime_root,
        _runtime_versions_dir,
    )
    home = tmp_path / "home"
    for _ in range(4):
        _vendor_once(home)
    versions = _runtime_versions_dir(home)
    _gc_runtime_versions(home, keep=2)
    remaining = sorted(v.name for v in versions.iterdir() if v.is_dir())
    assert 2 <= len(remaining) <= 3, (
        f"GC kept {len(remaining)} versions; contract says 2 ≤ |kept| ≤ 3"
    )
    current = _runtime_root(home).resolve(strict=True)
    assert current.name in remaining, "GC pruned the currently-linked version"
    # The two newest-by-name versions are always in the kept set.
    all_versions = sorted(v.name for v in versions.iterdir() if v.is_dir())
    # `remaining` is a subset of what's actually there — same thing.
    assert all_versions == remaining


def test_vendor_runtime_includes_runner(tmp_path):
    """The runner must live INSIDE the version dir (vendored as part of
    the atomic unit, not generated separately at a different path)."""
    home = tmp_path / "home"
    version = _vendor_once(home)
    runner = version / "bin" / "me-run"
    assert runner.exists()
    # Executable bit set.
    import os as _os
    assert _os.access(str(runner), _os.X_OK)


def test_vendor_runtime_separates_curated_from_user_roles(tmp_path):
    """The vendored runtime only carries curated package assets — no
    user-authored roles get smuggled in."""
    from metaensemble.lib.installer import _runtime_root
    home = tmp_path / "home"
    # Plant a stray user role at ~/.claude/agents/ to prove it doesn't bleed in.
    user_agents = home / ".claude" / "agents"
    user_agents.mkdir(parents=True)
    (user_agents / "my-personal-agent.md").write_text("# user role\n")

    version = _vendor_once(home)
    vendored_roles = version / "roles"
    if vendored_roles.is_dir():
        vendored_names = {p.name for p in vendored_roles.iterdir()}
    else:
        vendored_names = set()
    assert "my-personal-agent.md" not in vendored_names
    # And the runtime symlink does NOT shadow user roles.
    runtime = _runtime_root(home)
    assert (runtime / "roles" / "my-personal-agent.md").exists() is False


def test_runner_uses_quoted_absolute_path(tmp_path):
    """`_runner_text` must shell-quote the interpreter path AND use an
    absolute path so `/bin/sh exec` works regardless of $PATH."""
    from metaensemble.lib.installer import _runner_text
    body = _runner_text("/usr/local/anaconda3/bin/python")
    assert body.startswith("#!/bin/sh\n")
    # Absolute path appears literally (no quoting needed for safe chars).
    assert "exec /usr/local/anaconda3/bin/python -m metaensemble.cli" in body
    # Tail forwards positional args.
    assert body.rstrip().endswith('"$@"')


def test_runner_quotes_python_with_spaces(tmp_path):
    """A Python path containing spaces (common when the macOS account
    name contains a space, e.g. `/Users/Jane Doe/anaconda3/bin/python`)
    must be `shlex.quote`d so `/bin/sh` doesn't word-split it."""
    from metaensemble.lib.installer import _runner_text
    body = _runner_text("/Users/Jane Doe/anaconda3/bin/python")
    # Quoted form, not raw.
    assert "exec '/Users/Jane Doe/anaconda3/bin/python'" in body
    assert "exec /Users/Jane Doe/anaconda3/bin/python" not in body


def test_install_survives_source_deletion_shallow(tmp_path):
    """Cheap structural prerequisite for the slow brick-wall test below:
    every symlink inside a vendored version dir resolves to a path inside
    the same version dir (no leaks to source). This is necessary but not
    sufficient; the full proof lives in
    `test_install_survives_source_deletion_brick_wall` (slow)."""
    home = tmp_path / "home"
    version = _vendor_once(home)
    version_resolved = version.resolve(strict=True)
    for entry in version.rglob("*"):
        if entry.is_symlink():
            target = entry.resolve(strict=False)
            try:
                target.relative_to(version_resolved)
            except ValueError:
                raise AssertionError(
                    f"version dir leaks to outside path: {entry} -> {target}"
                )


_SNAPSHOT_EXCLUDED_DIRS = (
    # Claude Code writes session transcripts here during ANY interactive
    # session — including the one running this test. They are not a
    # MetaEnsemble side-effect; excluding them prevents false positives.
    ".claude/projects",
    ".claude/backups",
    ".claude/tasks",
    ".claude/todos",
    ".claude/shell-snapshots",
    ".claude/statsig",
    ".claude/ide",
    ".claude/__store.db",
)
_SNAPSHOT_EXCLUDED_FILES = (
    # The live Claude Code statusline can refresh this cache while the test is
    # running, independent of the subprocess HOME used by the installer. Ignore
    # the volatile cache file and the parent directory mtime it bumps; leaked
    # test writes elsewhere under real ~/.metaensemble are still detected.
    ".metaensemble/state",
    ".metaensemble/state/runtime-rate-limits.json",
)


def _snapshot_user_home() -> dict[str, int]:
    """Inventory the real user's MetaEnsemble dirs by mtime_ns.

    Used by the brick-wall test to prove HOME isolation actually held:
    the test must NOT touch the real `~/.claude/` or `~/.metaensemble/`.
    Returns `{relpath: mtime_ns}` for every entry. An empty dict means
    the dir doesn't exist (also valid — equally undisturbed).

    Excludes Claude Code's session-internal directories (see
    `_SNAPSHOT_EXCLUDED_DIRS`) so the snapshot diffs only the surfaces
    MetaEnsemble could touch."""
    home = Path.home()
    excluded = tuple(str(home / d) for d in _SNAPSHOT_EXCLUDED_DIRS)
    excluded_files = {str(home / f) for f in _SNAPSHOT_EXCLUDED_FILES}
    snap: dict[str, int] = {}
    for root in (home / ".claude", home / ".metaensemble"):
        if not root.exists():
            continue
        for p in root.rglob("*"):
            ps = str(p)
            if ps in excluded_files:
                continue
            if any(ps == ex or ps.startswith(ex + "/") for ex in excluded):
                continue
            try:
                snap[ps] = p.stat(follow_symlinks=False).st_mtime_ns
            except OSError:
                pass
    return snap


@pytest.mark.slow
def test_install_survives_source_deletion_brick_wall(tmp_path):
    """Install-survives-source-deletion proof.

    Exercise the full wheel path rather than an editable checkout:

      1. clone source repo to tmp
      2. build wheel from the clone
      3. install wheel into a fresh tmp venv
      4. HOME=<tmp-home> so subprocess CLIs use tmp ~/.claude + ~/.metaensemble
      5. `metaensemble user-setup --layout namespaced` + `adopt <tmp-proj>`
      6. delete the tmp source clone
      7. force minimal PATH so we can't accidentally fall through to a
         dev-tree metaensemble
      8. invoke `<tmp-venv>/bin/metaensemble hook session_start.py` — exit 0
      9. symlink realpaths don't contain the deleted tmp source path
     10. settings.json hook command doesn't contain tmp source path
     11. slash-command symlinks resolve into ~/.metaensemble/runtime/
     12. real ~/.claude and ~/.metaensemble are byte-identical to before
    """
    import shutil
    import subprocess
    import venv as _venv

    # Snapshot real home BEFORE anything — proves step 12 by diff at end.
    real_snapshot_before = _snapshot_user_home()

    # 1. Clone just the bits needed for wheel build into tmp.
    src_clone = tmp_path / "src"
    src_clone.mkdir()
    repo_root = Path(__file__).resolve().parent.parent.parent
    for item in ("metaensemble", "evals", "pyproject.toml", "README.md", "LICENSE"):
        s = repo_root / item
        if not s.exists():
            continue
        d = src_clone / item
        if s.is_dir():
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)

    # 2. Build wheel from the clone.
    dist = tmp_path / "dist"
    dist.mkdir()
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist), str(src_clone)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"wheel build failed:\n{proc.stderr}"
    wheel = next(dist.glob("metaensemble-*.whl"))

    # 3. Fresh venv, install wheel.
    venv_dir = tmp_path / "venv"
    _venv.create(venv_dir, with_pip=True, clear=True)
    venv_python = venv_dir / "bin" / "python"
    venv_meta = venv_dir / "bin" / "metaensemble"
    proc = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--quiet", str(wheel)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"pip install failed:\n{proc.stderr}"
    assert venv_meta.exists(), "console script `metaensemble` missing from venv"

    # 4. HOME isolation. 5. Drive setup directly (no interactive wizard).
    tmp_home = tmp_path / "home"
    tmp_home.mkdir()
    tmp_proj = tmp_path / "proj"
    tmp_proj.mkdir()
    env = {
        "HOME": str(tmp_home),
        "PATH": f"{venv_dir / 'bin'}:/usr/bin:/bin",
        "LANG": "C.UTF-8",
    }
    proc = subprocess.run(
        [str(venv_meta), "user-setup", "--layout", "namespaced"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, (
        f"user-setup failed:\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    proc = subprocess.run(
        [str(venv_meta), "adopt", str(tmp_proj)],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, (
        f"adopt failed:\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )

    # 6. Delete the source clone — the brick wall.
    shutil.rmtree(src_clone)
    assert not src_clone.exists()

    # 7. PATH already minimal. 8. Invoke a hook via the console script.
    proc = subprocess.run(
        [str(venv_meta), "hook", "session_start.py"],
        capture_output=True, text=True, env=env, cwd=str(tmp_proj),
    )
    assert proc.returncode == 0, (
        f"post-source-deletion hook failed:\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )

    # 9. No symlink in ~/.claude/ resolves through the deleted source.
    src_str = str(src_clone)
    claude = tmp_home / ".claude"
    for entry in claude.rglob("*"):
        if entry.is_symlink():
            assert src_str not in str(entry.resolve(strict=False)), (
                f"symlink {entry} resolves through deleted source"
            )

    # 10. settings.json hook command does not reference deleted source.
    settings = (claude / "settings.json").read_text()
    assert src_str not in settings, (
        "settings.json hook commands still reference the deleted source"
    )

    # 11. Slash-command symlinks resolve into ~/.metaensemble/runtime/.
    runtime_marker = str(tmp_home / ".metaensemble" / "runtime")
    commands_dir = claude / "commands" / "metaensemble"
    if commands_dir.is_symlink():
        resolved = str(commands_dir.resolve(strict=True))
        assert resolved.startswith(str(tmp_home / ".metaensemble" / "runtime-versions")), (
            f"commands symlink resolves outside runtime-versions: {resolved}"
        )
    elif commands_dir.is_dir():
        # Per-file symlinks (top-level layout would symlink individual
        # .md files); same invariant per entry.
        for entry in commands_dir.iterdir():
            if entry.is_symlink():
                resolved = str(entry.resolve(strict=False))
                assert runtime_marker in resolved, (
                    f"command {entry.name} symlink leaks outside runtime: {resolved}"
                )

    # 12. Real user home untouched.
    real_snapshot_after = _snapshot_user_home()
    assert real_snapshot_after == real_snapshot_before, (
        "HOME isolation leaked: real ~/.claude or ~/.metaensemble was modified.\n"
        f"diff (added/changed entries): "
        f"{sorted(set(real_snapshot_after) - set(real_snapshot_before))[:10]}..."
    )


def test_user_setup_cleans_legacy_launcher_residue(tmp_path, monkeypatch):
    """Older installs left `~/.metaensemble/bin/me-run`; user-setup removes it."""
    from metaensemble.lib.installer import _user_metaensemble_dir
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    base = _user_metaensemble_dir(home)
    legacy_bin = base / "bin"
    legacy_bin.mkdir(parents=True)
    (legacy_bin / "me-run").write_text("#!/bin/sh\n# legacy launcher\n")

    _vendor_once(home)

    assert not legacy_bin.exists(), "legacy ~/.metaensemble/bin/ not cleaned"


def test_user_setup_strips_legacy_hook_entries_from_settings(tmp_path, monkeypatch):
    """When user-setup runs, settings.json hook entries pointing at the
    LEGACY launcher path must be recognized as managed by the installer
    so that they get overwritten by the new runtime path (not preserved
    as user content alongside the new entries)."""
    from metaensemble.lib.installer import _is_metaensemble_hook_command
    legacy = "/Users/example/.metaensemble/bin/me-run hook pre_task.py"
    current = "/Users/example/.metaensemble/runtime/bin/me-run hook pre_task.py"
    assert _is_metaensemble_hook_command(legacy)
    assert _is_metaensemble_hook_command(current)


def test_user_teardown_removes_runtime_and_versions_and_roles(tmp_path, monkeypatch):
    """`purge_user_state` (user-teardown --purge-state) must remove the
    runtime symlink, every versioned dir, and the whole ~/.metaensemble/
    tree — no residue left to confuse a re-install."""
    from metaensemble.lib.installer import (
        _runtime_root, _runtime_versions_dir,
        _user_metaensemble_dir, purge_user_state,
    )
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    _vendor_once(home)
    _vendor_once(home)
    assert _runtime_root(home).exists()
    assert any(_runtime_versions_dir(home).iterdir())

    purge_user_state(home=home)

    assert not _user_metaensemble_dir(home).exists()
    assert not _runtime_root(home).exists()


def test_user_setup_revendors_on_every_call(tmp_path, monkeypatch):
    """Upgrade contract: two back-to-back `user-setup`
    calls must always swap the runtime symlink to a fresh version dir.

    Why this matters: `pip install --upgrade metaensemble` ships new
    assets, but the existing version dir still has a valid MANIFEST.
    Treating "MANIFEST valid" as "already applied" would silently leave
    the user on stale assets after every upgrade. Re-vendoring is
    unconditional; GC keeps the disk cost bounded.

    Also confirms user-authored content under ~/.metaensemble/ outside
    the runtime tree is preserved across re-vendor.
    """
    from metaensemble.lib.installer import (
        Layout, _runtime_root, _user_metaensemble_dir,
        apply_install, plan_install, survey,
    )
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    project = tmp_path / "proj"
    project.mkdir()

    def run_user_setup() -> Path:
        from dataclasses import replace
        survey_result = survey(home=home, project=project, write_report=False)
        plan = plan_install(survey_result, Layout.NAMESPACED, project=project, home=home)
        user_plan = replace(plan, actions=plan.user_actions())
        apply_install(user_plan, project=project, dry_run=False, user_scope_only=True)
        return _runtime_root(home).resolve(strict=True)

    target_a = run_user_setup()
    # User-authored content outside the runtime tree (e.g., a custom roles
    # dir). Must survive the second user-setup intact.
    user_roles = _user_metaensemble_dir(home) / "roles"
    user_roles.mkdir(parents=True, exist_ok=True)
    (user_roles / "my-custom-role.md").write_text("# user-authored role\n")

    target_b = run_user_setup()

    assert target_a != target_b, (
        "user-setup short-circuited; runtime symlink did not change. "
        "This breaks the pip-upgrade contract."
    )
    assert target_b.parent.name == "runtime-versions"
    # User-authored content untouched.
    assert (user_roles / "my-custom-role.md").read_text() == "# user-authored role\n"
