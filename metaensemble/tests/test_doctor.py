"""Tests for the doctor module and the `metaensemble doctor` CLI."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


from metaensemble.lib.doctor import (
    check_hook_log,
    check_ledger_recording_health,
    check_project_state,
    check_schemas,
    check_window_capacity_calibrated,
    render_report,
    run_doctor,
)


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _write_settings(home: Path, hooks: dict | None) -> Path:
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    payload = {"hooks": hooks} if hooks is not None else {}
    settings.write_text(json.dumps(payload))
    return settings


# --- C1: .pth files -------------------------------------------------------


def test_check_pth_files_skipped_after_wheel_installs(monkeypatch):
    """C1 returns SKIP regardless of state.

    The old check looked for the macOS UF_HIDDEN flag on the editable
    install's `.pth` file. The supported install is now a wheel; the
    `.pth` file does not exist, so the check is meaningless. Kept with
    its ID stable so doctor still emits the row but always SKIP.
    """
    from metaensemble.lib import doctor

    result = doctor.check_pth_files()
    assert result.check_id == "C1"
    assert result.status == "SKIP"
    assert "deprecated" in result.detail.lower()


def test_check_pth_files_skip_accepts_fix_flag():
    """The fix flag is accepted for API stability and ignored."""
    from metaensemble.lib import doctor

    result = doctor.check_pth_files(fix=True)
    assert result.status == "SKIP"


def test_check_hook_wiring_missing_settings(monkeypatch, tmp_path):
    """C2 returns WARN when ~/.claude/settings.json does not exist."""
    from metaensemble.lib import doctor
    monkeypatch.setattr(doctor, "_claude_settings_path",
                        lambda: tmp_path / "missing-settings.json")
    result = doctor.check_hook_wiring()
    assert result.status == "WARN"


def test_check_hook_wiring_all_paths_exist(monkeypatch, tmp_path):
    """C2 returns OK when every hook command points at existing paths."""
    from metaensemble.lib import doctor
    interp = tmp_path / "python"
    interp.write_text("#!/bin/sh\n")
    interp.chmod(0o755)
    script = tmp_path / "hook.py"
    script.write_text("#!/usr/bin/env python\n")

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "hooks": {
            "SessionStart": [{
                "matcher": "*",
                "hooks": [{"type": "command",
                           "command": f"'{interp}' '{script}'"}],
            }],
        }
    }))
    monkeypatch.setattr(doctor, "_claude_settings_path", lambda: settings_path)
    result = doctor.check_hook_wiring()
    assert result.status == "OK"


def test_check_hook_wiring_missing_script_returns_fail(monkeypatch, tmp_path):
    """C2 returns FAIL when a referenced hook script is absent."""
    from metaensemble.lib import doctor
    interp = tmp_path / "python"
    interp.write_text("#!/bin/sh\n")
    interp.chmod(0o755)

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "hooks": {
            "PreToolUse": [{
                "matcher": "Task",
                "hooks": [{"type": "command",
                           "command": f"'{interp}' '{tmp_path}/missing.py'"}],
            }],
        }
    }))
    monkeypatch.setattr(doctor, "_claude_settings_path", lambda: settings_path)
    result = doctor.check_hook_wiring()
    assert result.status == "FAIL"
    assert "missing.py" in result.detail


# --- C3: schemas ---------------------------------------------------------


def test_check_schemas_all_load():
    """The shipped JSON schemas should always compile cleanly."""
    result = check_schemas()
    assert result.status == "OK"


# --- C4: project state ---------------------------------------------------


def test_check_project_state_missing_state_dir_returns_warn(tmp_path, monkeypatch):
    """C4 returns WARN when .metaensemble/state is absent in cwd."""
    monkeypatch.chdir(tmp_path)
    result = check_project_state()
    assert result.status == "WARN"


def test_check_project_state_initialized_returns_ok(tmp_path, monkeypatch):
    """C4 returns OK when init was run."""
    monkeypatch.chdir(tmp_path)
    # Invoke init through the CLI to materialize the state dir.
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    subprocess.run(
        [sys.executable, "-m", "metaensemble.cli", "init"],
        capture_output=True, text=True, env=env, cwd=str(tmp_path),
        check=True,
    )
    result = check_project_state()
    assert result.status == "OK"


def test_check_project_state_corrupted_db_returns_fail(tmp_path, monkeypatch):
    """C4 returns FAIL when the SQLite file is unreadable."""
    monkeypatch.chdir(tmp_path)
    state_dir = tmp_path / ".metaensemble" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "department.db").write_bytes(b"not a sqlite file")
    result = check_project_state()
    assert result.status == "FAIL"


def test_check_project_state_open_permission_error_returns_warn(tmp_path, monkeypatch):
    """C4 should not label sandbox/permission open failures as corruption."""
    import sqlite3

    monkeypatch.chdir(tmp_path)
    state_dir = tmp_path / ".metaensemble" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "department.db").write_bytes(b"placeholder")

    def fake_connect(*_args, **_kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    result = check_project_state()
    assert result.status == "WARN"
    assert "permission" in result.detail.lower()
    assert "corruption" in result.detail.lower()


def _stage_unopenable_project_under(parent: Path, monkeypatch) -> None:
    """Helper: create a project at `parent` whose Ledger DB raises
    `unable to open database file` on connect. The caller is responsible
    for monkeypatching `Path.home` and `sys.platform` as needed."""
    import sqlite3

    state_dir = parent / ".metaensemble" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "department.db").write_bytes(b"placeholder")
    monkeypatch.chdir(parent)

    def fake_connect(*_args, **_kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(sqlite3, "connect", fake_connect)


def test_check_project_state_open_failure_under_desktop_names_icloud(tmp_path, monkeypatch):
    """When the project sits under ~/Desktop on macOS and SQLite fails to
    open the DB, C4's remediation must name iCloud as a likely cause."""
    import metaensemble.lib.doctor as doctor_mod

    fake_home = tmp_path
    project = fake_home / "Desktop" / "proj"
    project.mkdir(parents=True)
    _stage_unopenable_project_under(project, monkeypatch)

    monkeypatch.setattr(doctor_mod.Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr(doctor_mod.sys, "platform", "darwin")

    result = check_project_state()
    assert result.status == "WARN"
    assert "icloud" in result.remediation.lower()
    assert "desktop" in result.remediation.lower()


def test_check_project_state_open_failure_outside_icloud_default_omits_hint(
    tmp_path, monkeypatch
):
    """The iCloud hint must NOT appear when the project is outside the
    Desktop/Documents default-sync roots — that would be a misleading
    remediation."""
    import metaensemble.lib.doctor as doctor_mod

    fake_home = tmp_path
    project = fake_home / "code" / "proj"
    project.mkdir(parents=True)
    _stage_unopenable_project_under(project, monkeypatch)

    monkeypatch.setattr(doctor_mod.Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr(doctor_mod.sys, "platform", "darwin")

    result = check_project_state()
    assert result.status == "WARN"
    assert "icloud" not in result.remediation.lower()


def test_check_window_capacity_labels_source_and_cwd(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(tmp_path)
    state = home / ".metaensemble" / "state"
    state.mkdir(parents=True)
    native_path = state / "runtime-rate-limits.json"
    from metaensemble.lib import native_state
    monkeypatch.setattr(native_state, "_DEFAULT_PATH", native_path)
    captured_at = datetime.now(timezone.utc).isoformat()
    reset_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    native_path.write_text(json.dumps({
        "captured_at": captured_at,
        "rate_limits": {
            "five_hour_window": {
                "used_percentage": 10.0,
                "resets_at": reset_at,
            },
        },
    }))

    result = check_window_capacity_calibrated()

    assert result.status == "OK"
    assert "source:" in result.detail
    assert "cwd:" in result.detail


# --- C5: hook error log --------------------------------------------------


def test_check_hook_log_missing_returns_ok(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = check_hook_log()
    assert result.status == "OK"
    assert "No hook errors" in result.detail


def test_check_hook_log_empty_returns_ok(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log_path = tmp_path / ".metaensemble" / "hooks" / "log.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("")
    result = check_hook_log()
    assert result.status == "OK"


def test_check_hook_log_with_few_entries_returns_ok(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log_path = tmp_path / ".metaensemble" / "hooks" / "log.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        json.dumps({"ts": "2026-05-15T00:00:00Z", "kind": "manifest-validation-failed",
                    "message": "bad yaml", "context": {}}) + "\n"
    )
    result = check_hook_log()
    assert result.status == "OK"
    assert "manifest-validation-failed" in result.detail


def test_check_hook_log_many_entries_returns_warn(tmp_path, monkeypatch):
    """C5 escalates to WARN when ≥10 entries accumulate."""
    monkeypatch.chdir(tmp_path)
    log_path = tmp_path / ".metaensemble" / "hooks" / "log.jsonl"
    log_path.parent.mkdir(parents=True)
    lines = [
        json.dumps({"ts": f"t{i}", "kind": "manifest-validation-failed",
                    "message": "x", "context": {}})
        for i in range(15)
    ]
    log_path.write_text("\n".join(lines) + "\n")
    result = check_hook_log()
    assert result.status == "WARN"


# --- C10: Ledger recording health ---------------------------------------


def _seed_project_ledger(project: Path):
    from metaensemble.lib.ledger import Executor, Ledger

    state = project / ".metaensemble" / "state"
    state.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(db_path=state / "department.db", jsonl_path=state / "runs.jsonl")
    migration = (
        REPO_ROOT / "metaensemble" / "state" / "migrations" / "001_init.sql"
    ).read_text()
    ledger.initialize(migration)
    ledger.ensure_role(
        role_id="backend",
        version="1.0.0",
        spec_path="roles/backend.md",
        model_tier="sonnet",
    )
    executor = Executor(
        executor_id="exec-1",
        alias="be-001",
        role_id="backend",
        parent_executor_id=None,
        created_ts=datetime.now(timezone.utc).isoformat(),
        last_seen_ts=datetime.now(timezone.utc).isoformat(),
        status="active",
    )
    ledger.upsert_executor(executor)
    ledger.ensure_task(task_id="task-1", task_type="test", status="open")
    return ledger


def test_c10_fails_on_unmatched_post_task_failed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ledger = _seed_project_ledger(tmp_path)
    ledger.close()
    log_path = tmp_path / ".metaensemble" / "hooks" / "log.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": "post-task-failed",
            "message": "Error binding parameter 4: type 'dict' is not supported",
            "context": {"run_id": "run-missing"},
        }) + "\n"
    )

    result = check_ledger_recording_health()

    assert result.status == "FAIL"
    assert "run-missing" in result.detail
    assert "recording_failed" in result.detail


def test_c10_suppresses_matched_recording_failed_history(tmp_path, monkeypatch):
    from metaensemble.lib.ledger import Run

    monkeypatch.chdir(tmp_path)
    ledger = _seed_project_ledger(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    ledger.append_run(Run(
        run_id="run-explained",
        executor_id="exec-1",
        task_id="task-1",
        model="sonnet",
        tokens_in=100,
        tokens_out=0,
        window_id="2026-05-27T05",
        started_ts=now,
        ended_ts=now,
        outcome="recording_failed",
        failure_reason="post-task recording failed: bind error",
    ))
    ledger.close()
    log_path = tmp_path / ".metaensemble" / "hooks" / "log.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        json.dumps({
            "ts": now,
            "kind": "post-task-failed",
            "message": "bind error",
            "context": {"run_id": "run-explained"},
        }) + "\n"
    )

    result = check_ledger_recording_health()

    assert result.status == "OK"
    assert "Matched recording_failed log pairs: 1" in result.detail


# --- Integration: CLI exit code ------------------------------------------


def test_cli_doctor_exit_code_with_failures(tmp_path, monkeypatch):
    """The CLI returns non-zero when at least one check fails."""
    # Create a tmp project with a corrupted DB so C4 fails.
    state_dir = tmp_path / ".metaensemble" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "department.db").write_bytes(b"not a sqlite file")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    # Point HOME at the tmp_path so the launcher-presence check doesn't pick up
    # a real launcher on the developer's machine and downgrade the .pth state
    # away from FAIL.
    env["HOME"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "metaensemble.cli", "doctor"],
        capture_output=True, text=True, env=env, cwd=str(tmp_path),
    )
    assert proc.returncode != 0
    assert "[FAIL]" in proc.stdout
    assert "C4" in proc.stdout


def test_cli_doctor_renders_markdown_header(tmp_path):
    """Doctor output starts with the canonical Markdown header."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-m", "metaensemble.cli", "doctor"],
        capture_output=True, text=True, env=env, cwd=str(tmp_path),
    )
    assert proc.stdout.startswith("## MetaEnsemble doctor")


# --- C6: venv entry-point script -----------------------------------------


def test_check_venv_entry_point_skipped_after_wheel_installs():
    """C6 returns SKIP regardless of state.

    The old check looked for the resilient launcher rendered by
    bootstrap.sh into the venv's `metaensemble` script. The bootstrap
    script and the launcher template are gone; the wheel install's
    entry point is correct by construction.
    """
    from metaensemble.lib import doctor

    result = doctor.check_venv_entry_point()
    assert result.check_id == "C6"
    assert result.status == "SKIP"
    assert "deprecated" in result.detail.lower()


def test_render_report_includes_each_check():
    """The renderer should include a section per check and a final status line."""
    report = run_doctor()
    text = render_report(report)
    for cid in ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9"):
        assert cid in text
    assert "Status:" in text


# --- C9: runtime vendored -------------------------------------------------


def test_doctor_c1_c6_deprecated_c9_active(tmp_path, monkeypatch):
    """C1 and C6 must return SKIP; C9 must
    actively verify the vendored runtime (symlink + MANIFEST + runner)."""
    from metaensemble.lib import doctor as _doctor
    from metaensemble.lib.installer import _vendor_runtime_atomically

    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    # Pre-condition: with no runtime vendored, C9 WARNs.
    result = _doctor.check_runtime_vendored()
    assert result.check_id == "C9"
    assert result.status == "WARN"

    # Vendor it.
    _vendor_runtime_atomically(home=home)

    # Now C9 is OK; C1 and C6 stay SKIP.
    result = _doctor.check_runtime_vendored()
    assert result.status == "OK"
    assert "MANIFEST verified" in result.detail
    assert _doctor.check_pth_files().status == "SKIP"
    assert _doctor.check_venv_entry_point().status == "SKIP"
