"""Tests for the upgrade path from prior-layout installs.

When a release renames or removes a runtime artifact, the previous install's
symlink in `~/.claude/` is left pointing at a now-deleted target. The
installer must detect those managed dangling symlinks and remove them on the
next `user-setup`, without touching user-authored files at the same paths.

See addendum Addition 2.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from metaensemble.lib.installer import remediate_stale_managed_symlinks


def _make_managed_dangling_symlink(home: Path, name: str) -> Path:
    """Drop a symlink at `~/.claude/commands/<name>` pointing at a deleted
    target inside the (also nonexistent) runtime — i.e. the exact shape a
    prior install would have left behind after the runtime file was renamed.
    """
    cmds = home / ".claude" / "commands"
    cmds.mkdir(parents=True, exist_ok=True)
    dangling = cmds / name
    target = home / ".metaensemble" / "runtime" / "commands" / name
    # Note: target intentionally does not exist; the runtime tree is absent.
    dangling.symlink_to(target)
    return dangling


def _make_user_authored_file(home: Path, relpath: str, content: str = "user content") -> Path:
    """Drop a user-authored file (not a symlink) at the given relpath."""
    full = home / relpath
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    return full


def test_remediation_removes_stale_managed_window_symlink(tmp_path):
    """The pre-rename `window.md` symlink must be removed on remediation."""
    home = tmp_path / "home"
    dangling = _make_managed_dangling_symlink(home, "window.md")
    assert dangling.is_symlink()
    assert not dangling.exists()  # dangling

    removed = remediate_stale_managed_symlinks(home=home)

    assert dangling in removed
    assert not dangling.is_symlink()


def test_remediation_leaves_user_authored_files_alone(tmp_path):
    """A user-authored file at `~/.claude/commands/whatever.md` must survive."""
    home = tmp_path / "home"
    user_file = _make_user_authored_file(home, ".claude/commands/whatever.md")

    remediate_stale_managed_symlinks(home=home)

    assert user_file.is_file()
    assert user_file.read_text() == "user content"


def test_remediation_leaves_user_symlinks_to_non_runtime_targets_alone(tmp_path):
    """A user-authored symlink that does NOT point into `~/.metaensemble/`
    must survive the remediation pass.
    """
    home = tmp_path / "home"
    cmds = home / ".claude" / "commands"
    cmds.mkdir(parents=True)
    user_target = tmp_path / "user-target"
    user_target.mkdir()
    user_symlink = cmds / "user-cmd.md"
    user_symlink.symlink_to(user_target / "user-cmd.md")  # target doesn't exist but isn't ours

    remediate_stale_managed_symlinks(home=home)

    assert user_symlink.is_symlink()


def test_remediation_leaves_managed_symlinks_with_valid_targets_alone(tmp_path):
    """A managed symlink whose target file actually exists must NOT be
    removed by the remediation pass (it isn't stale).
    """
    home = tmp_path / "home"
    runtime_cmds = home / ".metaensemble" / "runtime" / "commands"
    runtime_cmds.mkdir(parents=True)
    target = runtime_cmds / "dispatch.md"
    target.write_text("# managed command")

    cmds = home / ".claude" / "commands"
    cmds.mkdir(parents=True)
    valid_symlink = cmds / "dispatch.md"
    valid_symlink.symlink_to(target)

    remediate_stale_managed_symlinks(home=home)

    assert valid_symlink.is_symlink()
    assert valid_symlink.exists()


def test_remediation_handles_multiple_stale_symlinks(tmp_path):
    """Several legacy command names land at once."""
    home = tmp_path / "home"
    for name in ("window.md", "survey.md", "old-thing.md"):
        _make_managed_dangling_symlink(home, name)

    removed = remediate_stale_managed_symlinks(home=home)

    assert len(removed) == 3
    cmds = home / ".claude" / "commands"
    for name in ("window.md", "survey.md", "old-thing.md"):
        assert not (cmds / name).is_symlink()


def test_remediation_handles_missing_home_directory(tmp_path):
    """A clean HOME with no ~/.claude/ must return empty without errors."""
    home = tmp_path / "home"
    home.mkdir()  # no .claude/
    removed = remediate_stale_managed_symlinks(home=home)
    assert removed == []


def test_remediation_cleans_output_styles_too(tmp_path):
    """Stale output style symlinks (managed, dangling) are also cleaned."""
    home = tmp_path / "home"
    styles = home / ".claude" / "output-styles"
    styles.mkdir(parents=True)
    runtime_styles = home / ".metaensemble" / "runtime" / "output-styles"
    # Don't create runtime_styles — make the symlinks dangling
    dangling = styles / "metaensemble-wire.md"
    dangling.symlink_to(runtime_styles / "wire.md")

    removed = remediate_stale_managed_symlinks(home=home)

    assert dangling in removed
    assert not dangling.is_symlink()


def test_remediation_handles_namespaced_commands_subdir(tmp_path):
    """Stale symlinks inside `~/.claude/commands/metaensemble/` (the
    namespaced layout) are also cleaned.
    """
    home = tmp_path / "home"
    ns_cmds = home / ".claude" / "commands" / "metaensemble"
    ns_cmds.mkdir(parents=True)
    runtime_cmds = home / ".metaensemble" / "runtime" / "commands"
    dangling = ns_cmds / "retired-cmd.md"
    dangling.symlink_to(runtime_cmds / "retired-cmd.md")

    removed = remediate_stale_managed_symlinks(home=home)

    assert dangling in removed
