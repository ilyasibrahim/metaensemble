"""Tests for the vocabulary migration (legacy `parallel`/`incorporate` -> `namespaced`/`top-level`).

The migration is idempotent and read-time-safe: on-disk state files written
by pre-rename installs migrate to canonical values on first invocation of any
state-touching CLI command. See addendum Addition 1.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from metaensemble.lib.installer import migrate_vocabulary_state


def _make_registered_project(home: Path, project: Path) -> None:
    """Register a project in `~/.claude/projects/` the way Claude Code does.

    The registry walker resolves a project's cwd by reading the first JSONL
    event in any session log file. `discover_projects()` uses that.
    """
    projects_root = home / ".claude" / "projects"
    project_dir = projects_root / f"-{str(project).replace('/', '-')}"
    project_dir.mkdir(parents=True, exist_ok=True)
    session_log = project_dir / "session.jsonl"
    session_log.write_text(json.dumps({"cwd": str(project)}) + "\n")


@pytest.fixture(autouse=True)
def _reset_migration_cache():
    """Per-test reset of the in-process migration cache."""
    from metaensemble.lib import installer
    installer._MIGRATION_DONE.clear()
    yield
    installer._MIGRATION_DONE.clear()


def test_legacy_survey_decisions_yaml_renames_and_rewrites(tmp_path):
    """A pre-rename survey-decisions.yaml with `suggested_mode: incorporate`
    must be renamed to install-decisions.yaml AND have its values rewritten
    to `suggested_layout: top-level`.
    """
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _make_registered_project(home, project)

    me_dir = project / ".metaensemble"
    me_dir.mkdir()
    legacy = me_dir / "survey-decisions.yaml"
    legacy.write_text(
        "# Recommended mode: incorporate\n"
        "suggested_mode: incorporate\n"
        "agents: []\n"
    )

    actions = migrate_vocabulary_state(home=home)

    new_file = me_dir / "install-decisions.yaml"
    assert new_file.is_file(), "migration must rename the legacy file"
    assert not legacy.exists(), "migration must remove the legacy file"
    content = new_file.read_text()
    assert "suggested_layout: top-level" in content
    assert "suggested_mode" not in content
    assert "incorporate" not in content
    # Cosmetic comment also migrated
    assert "# Recommended layout: top-level" in content
    # Action records present
    assert any(a["kind"] == "rename" for a in actions)
    assert any(a["kind"] == "rewrite-yaml" for a in actions)


def test_legacy_plan_json_in_user_installs_rewrites(tmp_path):
    """A pre-rename ~/.metaensemble/installs/<ts>/plan.json with
    `"mode": "incorporate"` must be rewritten to `"layout": "top-level"`.
    """
    home = tmp_path / "home"
    install_dir = home / ".metaensemble" / "installs" / "20260520T080000Z"
    install_dir.mkdir(parents=True)
    plan = install_dir / "plan.json"
    plan.write_text(json.dumps({
        "mode": "incorporate",
        "timestamp": "20260520T080000Z",
        "actions": [{"kind": "symlink", "source": "/x", "target": "/y"}],
    }))

    actions = migrate_vocabulary_state(home=home)

    data = json.loads(plan.read_text())
    assert "mode" not in data
    assert data["layout"] == "top-level"
    assert any(a["kind"] == "rewrite-json" for a in actions)


def test_legacy_plan_json_in_project_backups_rewrites(tmp_path):
    """A pre-rename <project>/.metaensemble/backups/<ts>/plan.json must
    rewrite legacy `mode`/`parallel` values.
    """
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _make_registered_project(home, project)

    backup = project / ".metaensemble" / "backups" / "20260520T080000Z"
    backup.mkdir(parents=True)
    plan = backup / "plan.json"
    plan.write_text(json.dumps({"mode": "parallel", "actions": []}))

    migrate_vocabulary_state(home=home)

    data = json.loads(plan.read_text())
    assert data.get("layout") == "namespaced"
    assert "mode" not in data
    assert "parallel" not in plan.read_text()


def test_migration_is_idempotent(tmp_path):
    """Running the migration twice produces zero actions the second time."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _make_registered_project(home, project)

    me_dir = project / ".metaensemble"
    me_dir.mkdir()
    (me_dir / "survey-decisions.yaml").write_text(
        "suggested_mode: parallel\nagents: []\n"
    )

    first = migrate_vocabulary_state(home=home)
    assert first, "first run must produce migration actions"

    # Clear the process cache so the second invocation actually re-scans
    from metaensemble.lib import installer
    installer._MIGRATION_DONE.clear()

    second = migrate_vocabulary_state(home=home)
    assert second == [], "second run must be a no-op (idempotent)"


def test_migration_skips_unregistered_projects(tmp_path):
    """Projects with `.metaensemble/` on disk but no registry entry must
    NOT be visited. The projects registry is the source of truth, not the
    filesystem, so stale on-disk registrations cannot be migrated accidentally.
    """
    home = tmp_path / "home"
    home.mkdir()

    # Unregistered project: has the .metaensemble/ dir but not in ~/.claude/projects/
    rogue = tmp_path / "rogue-project"
    me_dir = rogue / ".metaensemble"
    me_dir.mkdir(parents=True)
    legacy = me_dir / "survey-decisions.yaml"
    legacy.write_text("suggested_mode: incorporate\nagents: []\n")

    actions = migrate_vocabulary_state(home=home)

    # The unregistered project must be untouched
    assert legacy.is_file(), "unregistered project must not be migrated"
    assert "suggested_mode: incorporate" in legacy.read_text()
    assert not any("rogue-project" in str(a) for a in actions)


def test_migration_logs_to_hooks_log_jsonl(tmp_path):
    """Each migration action is logged to <project>/.metaensemble/hooks/log.jsonl
    with kind `vocabulary-migration`.
    """
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _make_registered_project(home, project)

    me_dir = project / ".metaensemble"
    me_dir.mkdir()
    (me_dir / "survey-decisions.yaml").write_text(
        "suggested_mode: parallel\nagents: []\n"
    )

    migrate_vocabulary_state(home=home)

    log = me_dir / "hooks" / "log.jsonl"
    assert log.is_file()
    records = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert records, "log must contain at least one migration record"
    assert all(r["kind"] == "vocabulary-migration" for r in records)
    assert all("ts" in r for r in records)


def test_migration_leaves_canonical_files_untouched(tmp_path):
    """A canonical install-decisions.yaml with `suggested_layout: top-level`
    must not be modified.
    """
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _make_registered_project(home, project)

    me_dir = project / ".metaensemble"
    me_dir.mkdir()
    canonical = me_dir / "install-decisions.yaml"
    original_text = (
        "# Recommended layout: top-level\n"
        "suggested_layout: top-level\n"
        "agents: []\n"
    )
    canonical.write_text(original_text)
    original_mtime = canonical.stat().st_mtime_ns

    actions = migrate_vocabulary_state(home=home)

    assert canonical.read_text() == original_text
    # mtime unchanged proves the migrator never rewrote the file, not
    # merely that it rewrote identical content.
    assert canonical.stat().st_mtime_ns == original_mtime
    # We may not see the file in actions (which is the correct no-op behavior)
    assert not any(
        a.get("path") == str(canonical) and a["kind"] == "rewrite-yaml"
        for a in actions
    )


def test_plan_json_migration_does_not_rewrite_nested_mode_payloads(tmp_path):
    """Regression: the JSON migrator must only touch the plan's top-level
    `mode` field. Nested `mode` keys inside action payloads (e.g. a future
    chmod-like action) must survive unmodified.
    """
    home = tmp_path / "home"
    install_dir = home / ".metaensemble" / "installs" / "20260520T080000Z"
    install_dir.mkdir(parents=True)
    plan = install_dir / "plan.json"
    plan.write_text(json.dumps({
        "mode": "incorporate",
        "timestamp": "20260520T080000Z",
        "actions": [
            {"kind": "chmod", "target": "/x", "mode": "0644"},
            {"kind": "symlink", "source": "/a", "target": "/b"},
        ],
        "settings": {"mode": "test"},  # nested under a config blob
    }))

    migrate_vocabulary_state(home=home)

    data = json.loads(plan.read_text())
    # Top-level mode -> layout, value rewritten.
    assert "mode" not in data
    assert data["layout"] == "top-level"
    # Nested action's `mode` field is preserved verbatim — both key and value.
    chmod_action = data["actions"][0]
    assert chmod_action["mode"] == "0644"
    assert "layout" not in chmod_action
    # Nested config blob with `mode` field is preserved.
    assert data["settings"]["mode"] == "test"
    assert "layout" not in data["settings"]


def test_plan_json_migration_idempotent_on_clean_plan(tmp_path):
    """A plan.json already in canonical form must produce zero migration
    actions and survive byte-for-byte (only the trailing newline normalization
    is permitted, but content is unchanged).
    """
    home = tmp_path / "home"
    install_dir = home / ".metaensemble" / "installs" / "20260520T080000Z"
    install_dir.mkdir(parents=True)
    plan = install_dir / "plan.json"
    canonical = {
        "layout": "top-level",
        "timestamp": "20260520T080000Z",
        "actions": [{"kind": "symlink", "source": "/a", "target": "/b"}],
    }
    plan.write_text(json.dumps(canonical, indent=2) + "\n")

    actions = migrate_vocabulary_state(home=home)

    # Migration must NOT have touched this canonical file.
    rewrites = [a for a in actions if a.get("path") == str(plan)]
    assert rewrites == [], f"canonical plan.json was rewritten: {rewrites}"


def test_migration_handles_both_files_present(tmp_path):
    """If both survey-decisions.yaml (legacy name) and install-decisions.yaml
    (canonical name) coexist, the migration must leave them as-is — the user
    has already migrated and we will not destroy their work. The canonical
    file may still be rewritten if it contains legacy values.
    """
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    _make_registered_project(home, project)

    me_dir = project / ".metaensemble"
    me_dir.mkdir()
    legacy = me_dir / "survey-decisions.yaml"
    legacy.write_text("suggested_mode: parallel\nagents: []\n")
    canonical = me_dir / "install-decisions.yaml"
    canonical.write_text("suggested_layout: namespaced\nagents: []\n")

    migrate_vocabulary_state(home=home)

    # Legacy file is left alone (not renamed over the canonical)
    assert legacy.is_file()
    assert canonical.is_file()
