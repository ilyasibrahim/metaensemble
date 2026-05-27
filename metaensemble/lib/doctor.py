"""Diagnostic checks for MetaEnsemble installs.

The doctor exists for the first-run trust property: a new user who has
followed USER-GUIDE step 1 should have a single command that confirms
the install is healthy and points at any remediation needed. Each check
is a pure inspection (it does not change state) unless the user passes
`--fix`, in which case the doctor applies the documented remediation
for the issues it can fix safely.

Checks shipped today:
  - C1: `metaensemble` is importable from any working directory. The
        macOS-specific failure is that the editable-install `.pth` file
        in the venv's site-packages has had its `UF_HIDDEN` flag set
        (a known macOS condition), which causes Python's site.py to
        skip it at startup ("Skipping hidden .pth file" in -v output).
        The fix is `chflags nohidden` on the affected files.
  - C2: Hook scripts referenced in `~/.claude/settings.json` exist on
        disk and the Python interpreter the hook command names is
        executable. A missing interpreter or stale path produces the
        silent no-op failure mode that the user describes as "nothing
        is happening."
  - C3: The Manifest, Brief, and Role JSON-Schema files load and
        produce a working Draft 2020-12 validator. A corrupted schema
        would cause every Manifest to fail validation with an
        unhelpful error.
  - C4 (project-context only): `.metaensemble/state/` exists in cwd,
        the Ledger DB is initialized, and the JSONL mirror is present
        or creatable.
  - C5 (project-context only): The hook error log under
        `.metaensemble/hooks/log.jsonl` is tailed; the last few
        entries are surfaced. A populated log is not itself a failure
        — it is the place a curious user should look — but recurring
        identical errors indicate a real regression.

The doctor returns a non-zero exit code when at least one check is in
the `FAIL` state. `WARN` states pass with messaging. The output is
plain Markdown, suitable for direct relay to the Principal.
"""
from __future__ import annotations

import json
import os
import platform
import re
import stat
import subprocess  # nosec B404
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from metaensemble.lib.ledger import (
    OUTCOME_RECORDING_FAILED,
    read_post_task_failed_log_entries,
)


# --- Result types ---------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one check."""

    check_id: str
    title: str
    status: str  # "OK" | "WARN" | "FAIL" | "SKIP"
    detail: str
    remediation: str | None = None
    fixed: bool = False


@dataclass(frozen=True)
class DoctorReport:
    """The collected results of a doctor run."""

    results: list[CheckResult] = field(default_factory=list)

    @property
    def has_failures(self) -> bool:
        return any(r.status == "FAIL" for r in self.results)


# --- Helpers --------------------------------------------------------------


def _is_macos() -> bool:
    return platform.system() == "Darwin"


def _venv_site_packages() -> Path | None:
    """Locate the active interpreter's site-packages directory.

    Uses `sys.prefix` (the venv root) without `.resolve()`, because the
    venv's `bin/python` is a symlink back to the system interpreter and
    resolving the symlink would point us at the wrong tree.
    """
    if sys.prefix == sys.base_prefix:
        return None  # not in a venv
    venv_root = Path(sys.prefix)
    if not (venv_root / "pyvenv.cfg").exists():
        return None
    for libdir in (venv_root / "lib").glob("python*"):
        sp = libdir / "site-packages"
        if sp.is_dir():
            return sp
    return None


def _is_uf_hidden(path: Path) -> bool:
    """Return True when the macOS UF_HIDDEN flag is set on this path.

    On non-macOS systems this returns False unconditionally — the flag
    does not exist on Linux/Windows and `stat()` does not surface it.
    """
    if not _is_macos():
        return False
    try:
        st = path.stat()
    except OSError:
        return False
    return bool(getattr(st, "st_flags", 0) & getattr(stat, "UF_HIDDEN", 0x8000))


def _claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _read_settings() -> dict | None:
    p = _claude_settings_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def _iter_hook_commands(settings: dict) -> Iterable[tuple[str, str]]:
    """Yield (event_name, full_command_string) for every wired hook.

    The agent-runtime hook spec nests: hooks -> event -> [matcher groups]
    -> hooks -> [{type, command}]. We flatten and surface each command.
    """
    hooks_root = settings.get("hooks", {}) or {}
    for event_name, groups in hooks_root.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            for hook in group.get("hooks", []) or []:
                cmd = hook.get("command")
                if isinstance(cmd, str):
                    yield event_name, cmd


def _shellsplit_first_two(cmd: str) -> tuple[str | None, str | None]:
    """Return (interpreter_path, script_path) from a hook command.

    Hook commands are 'pyinterp script.py'. They may have either argument
    quoted with single or double quotes. We use shlex to parse robustly.
    """
    import shlex

    try:
        parts = shlex.split(cmd)
    except ValueError:
        return None, None
    interp = parts[0] if parts else None
    script = parts[1] if len(parts) >= 2 else None
    return interp, script


# --- Checks ---------------------------------------------------------------


def _launcher_path() -> Path:
    """Standard location for the rendered me-run launcher."""
    return Path.home() / ".metaensemble" / "bin" / "me-run"


def check_pth_files(*, fix: bool = False) -> CheckResult:
    """C1: deprecated after wheel-based installs; kept as SKIP for ID stability.

    Older versions checked the editable-install `.pth` file for the
    macOS `UF_HIDDEN` flag that caused `ModuleNotFoundError: No module
    named 'core'`. The supported install is now `pip install
    metaensemble` (wheel), which has no `.pth` file. C9 (new) covers
    the runtime-vendoring story that replaces this.

    Returns SKIP so doctor still emits the row (preserving check-ID
    stability for any tutorial or muscle memory pointing at C1) but
    never flags a problem. The `fix` flag is accepted and ignored.
    """
    _ = fix  # accepted for API stability, no-op
    return CheckResult(
        check_id="C1",
        title="Editable-install .pth file readable",
        status="SKIP",
        detail=(
            "Deprecated. The supported install is `pip install "
            "metaensemble` (wheel), which has no .pth file. See C9 for the "
            "runtime-vendoring health check that replaces this."
        ),
    )


def _legacy_check_pth_files(*, fix: bool = False) -> CheckResult:
    """Legacy implementation, kept inert as reference.

    Will be deleted after we are confident no caller imports the
    pre-deprecation behavior. Not invoked by run_doctor.
    """
    sp = _venv_site_packages()
    if sp is None:
        return CheckResult(
            check_id="C1",
            title="Editable-install .pth file readable",
            status="WARN",
            detail="Not running inside a venv; .pth-flag check skipped.",
        )

    hidden = [p for p in sp.glob("*.pth") if _is_uf_hidden(p)]
    launcher = _launcher_path()
    launcher_present = launcher.exists() and os.access(launcher, os.X_OK)

    if not hidden:
        return CheckResult(
            check_id="C1",
            title="Editable-install .pth file readable",
            status="OK",
            detail=f"All .pth files in {sp.name}/ are readable by site.py.",
        )

    paths = ", ".join(p.name for p in hidden)

    # The durable fix: render the launcher (or note it already exists).
    launcher_remediation = (
        "Run the bootstrap script to render the durable launcher:\n"
        "  ./scripts/bootstrap.sh\n"
        "Then invoke MetaEnsemble through `~/.metaensemble/bin/me-run <subcommand>`. "
        "The launcher sets PYTHONPATH explicitly and never touches the .pth file, "
        "so the hidden flag stops mattering."
    )

    chflags_remediation = (
        "If the project lives outside iCloud-synced folders, the flag can be "
        "cleared and will stay clear:\n"
        f"  chflags nohidden {' '.join(repr(str(p)) for p in hidden)}"
    )

    remediation = launcher_remediation + "\n\n" + chflags_remediation

    if fix and _is_macos():
        try:
            subprocess.run(  # nosec
                ["chflags", "nohidden", *[str(p) for p in hidden]],
                check=True,
                capture_output=True,
            )
            # The chflags ran; whether it sticks depends on iCloud sync state.
            return CheckResult(
                check_id="C1",
                title="Editable-install .pth file readable",
                status="OK",
                detail=(
                    f"Cleared UF_HIDDEN on: {paths}. site.py will now process the file. "
                    "If the flag returns within a few seconds, the project is in an "
                    "iCloud-synced location; render the durable launcher via "
                    "`./scripts/bootstrap.sh`."
                ),
                remediation=None,
                fixed=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            return CheckResult(
                check_id="C1",
                title="Editable-install .pth file readable",
                status="FAIL",
                detail=f"Hidden .pth files found ({paths}); chflags failed: {exc}.",
                remediation=remediation,
            )

    # Launcher already present: hidden .pth no longer blocks operation.
    if launcher_present:
        return CheckResult(
            check_id="C1",
            title="Editable-install .pth file readable",
            status="WARN",
            detail=(
                f"Hidden .pth files present ({paths}), but the launcher at "
                f"{launcher} works around them. Invoke MetaEnsemble through "
                "`~/.metaensemble/bin/me-run`."
            ),
        )

    return CheckResult(
        check_id="C1",
        title="Editable-install .pth file readable",
        status="FAIL",
        detail=f"Hidden .pth files (site.py will skip): {paths}.",
        remediation=remediation,
    )


def check_hook_wiring() -> CheckResult:
    """C2: Each hook command's interpreter and script exist on disk."""
    settings = _read_settings()
    if settings is None:
        return CheckResult(
            check_id="C2",
            title="Hook scripts wired correctly in ~/.claude/settings.json",
            status="WARN",
            detail="~/.claude/settings.json not found or unreadable.",
            remediation=(
                "Run `metaensemble user-setup --layout=namespaced` "
                "(or `--layout=top-level`) to register MetaEnsemble's hooks."
            ),
        )

    commands = list(_iter_hook_commands(settings))
    if not commands:
        return CheckResult(
            check_id="C2",
            title="Hook scripts wired correctly in ~/.claude/settings.json",
            status="WARN",
            detail="settings.json contains no hook entries.",
            remediation=(
                "Run `metaensemble user-setup` to register the SessionStart, "
                "PreToolUse, PostToolUse, and Stop hooks."
            ),
        )

    broken: list[str] = []
    for event, cmd in commands:
        import shlex
        try:
            parts = shlex.split(cmd)
        except ValueError:
            broken.append(f"{event}: command does not shell-parse: {cmd!r}")
            continue

        if not parts:
            broken.append(f"{event}: empty hook command")
            continue

        # Two valid forms (see installer._hook_command):
        #
        #   Launcher form: `<launcher> hook <script>.py`
        #     parts[0] = launcher path, parts[1] = "hook", parts[2] = script.
        #     The script is a filename relative to `metaensemble/hooks/`; resolve it.
        #
        #   Direct form:   `<interpreter> <script_abs_path>`
        #     parts[0] = interpreter, parts[1] = absolute script path.
        if (
            len(parts) >= 3
            and parts[0].endswith("/me-run")
            and parts[1] == "hook"
        ):
            launcher = Path(parts[0])
            if not launcher.exists():
                broken.append(f"{event}: launcher `{launcher}` not found")
                continue
            from metaensemble.cli import CORE_DIR
            script_path = CORE_DIR / "hooks" / parts[2]
            if not script_path.exists():
                broken.append(f"{event}: launcher script `{parts[2]}` not in metaensemble/hooks/")
            continue

        interp = parts[0]
        script = parts[1] if len(parts) >= 2 else None
        if interp and not Path(interp).exists():
            broken.append(f"{event}: interpreter `{interp}` not found")
        if script and not Path(script).exists():
            broken.append(f"{event}: script `{script}` not found")

    if not broken:
        return CheckResult(
            check_id="C2",
            title="Hook scripts wired correctly in ~/.claude/settings.json",
            status="OK",
            detail=f"All {len(commands)} hook command(s) point at existing interpreter and script.",
        )

    return CheckResult(
        check_id="C2",
        title="Hook scripts wired correctly in ~/.claude/settings.json",
        status="FAIL",
        detail="; ".join(broken),
        remediation=(
            "Re-run `metaensemble user-setup` to rewrite settings.json with the current "
            "interpreter and script paths, or edit ~/.claude/settings.json by hand."
        ),
    )


def check_schemas() -> CheckResult:
    """C3: The shipped JSON schemas load and compile."""
    try:
        from metaensemble.lib.manifest import _validator

        for name in ("manifest.schema.json", "brief.schema.json", "role.schema.json"):
            _validator(name)
    except Exception as exc:
        return CheckResult(
            check_id="C3",
            title="JSON schemas load and validate",
            status="FAIL",
            detail=f"{type(exc).__name__}: {exc}",
            remediation=(
                "metaensemble/schemas/*.json is corrupted or unreadable. Restore from "
                "version control or reinstall MetaEnsemble."
            ),
        )

    return CheckResult(
        check_id="C3",
        title="JSON schemas load and validate",
        status="OK",
        detail="manifest, brief, and role schemas all compile cleanly.",
    )


def check_project_state() -> CheckResult:
    """C4: The current project has an initialized .metaensemble/ tree."""
    state_dir = Path.cwd() / ".metaensemble" / "state"
    if not state_dir.exists():
        return CheckResult(
            check_id="C4",
            title="Project state directory initialized",
            status="WARN",
            detail=f"{state_dir} does not exist in this cwd.",
            remediation=(
                "If this project should use MetaEnsemble, run `metaensemble adopt` "
                "from the project root. Use `metaensemble init` only when you want "
                "project state without full adoption. If you ran the doctor from "
                "outside a project, this warning is expected."
            ),
        )

    db = state_dir / "department.db"
    if not db.exists():
        return CheckResult(
            check_id="C4",
            title="Project state directory initialized",
            status="FAIL",
            detail=f"{state_dir} exists but {db.name} is missing.",
            remediation="Run `metaensemble init --force` to rebuild the Ledger schema.",
        )

    # Confirm the schema is initialized by reading the four expected tables.
    try:
        import sqlite3
        # Open read-only so doctor can inspect a project Ledger without
        # needing to create SQLite journal/WAL side files. Under restricted
        # filesystem sandboxes a valid DB can otherwise surface as
        # "unable to open database file", which is a permissions problem,
        # not corruption.
        uri = f"{db.resolve(strict=False).as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        conn.close()
    except sqlite3.DatabaseError as exc:
        message = str(exc)
        if "unable to open database file" in message.lower():
            home = Path.home()
            cwd = Path.cwd()
            in_icloud_default = sys.platform == "darwin" and any(
                cwd.is_relative_to(home / sub) for sub in ("Desktop", "Documents")
            )
            remediation = (
                "Re-run `metaensemble doctor` with normal project filesystem "
                "permissions. If it still fails, inspect the DB with "
                "`sqlite3 <path> '.schema runs'`."
            )
            if in_icloud_default:
                remediation += (
                    " This project sits under ~/Desktop or ~/Documents, which "
                    "are iCloud-synced by default on macOS. Intermittent "
                    '"unable to open database file" errors can occur when '
                    "iCloud has placed state files in a dataless placeholder "
                    "state. Either host active MetaEnsemble projects outside "
                    "iCloud-synced paths, or exclude this project in System "
                    "Settings → iCloud → Drive → Desktop & Documents Folders. "
                    "See docs/USER-GUIDE.md (When something feels off) for the "
                    "full recipe."
                )
            return CheckResult(
                check_id="C4",
                title="Project state directory initialized",
                status="WARN",
                detail=(
                    f"Ledger DB at {db} exists but could not be opened "
                    f"read-only: {exc}. This is usually a filesystem "
                    "permission or sandbox restriction, not database corruption."
                ),
                remediation=remediation,
            )
        return CheckResult(
            check_id="C4",
            title="Project state directory initialized",
            status="FAIL",
            detail=f"Ledger DB at {db} is unreadable or corrupted: {exc}",
            remediation="Move the corrupted DB aside and run `metaensemble init --force`.",
        )

    expected = {"roles", "executors", "tasks", "runs"}
    missing = expected - tables
    if missing:
        return CheckResult(
            check_id="C4",
            title="Project state directory initialized",
            status="FAIL",
            detail=f"Ledger DB missing tables: {sorted(missing)}",
            remediation="Run `metaensemble init --force` to apply migrations.",
        )

    return CheckResult(
        check_id="C4",
        title="Project state directory initialized",
        status="OK",
        detail=f"{state_dir} present with all four Ledger tables.",
    )


def check_hook_log(*, tail: int = 5) -> CheckResult:
    """C5: Tail the hook error log to surface recurring silent failures."""
    log_path = Path.cwd() / ".metaensemble" / "hooks" / "log.jsonl"
    if not log_path.exists():
        return CheckResult(
            check_id="C5",
            title="Hook error log healthy",
            status="OK",
            detail="No hook errors recorded.",
        )

    try:
        lines = log_path.read_text().splitlines()
    except OSError as exc:
        return CheckResult(
            check_id="C5",
            title="Hook error log healthy",
            status="WARN",
            detail=f"Could not read {log_path}: {exc}",
        )

    if not lines:
        return CheckResult(
            check_id="C5",
            title="Hook error log healthy",
            status="OK",
            detail="Hook error log is empty.",
        )

    recent = lines[-tail:]
    parsed: list[dict] = []
    for line in recent:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not parsed:
        return CheckResult(
            check_id="C5",
            title="Hook error log healthy",
            status="WARN",
            detail=f"{len(lines)} log line(s) present but none parsed as JSON.",
        )

    # Surface the kinds and timestamps of the last few entries.
    summary = "; ".join(f"{e.get('ts','?')}: {e.get('kind','?')}" for e in parsed)
    status = "WARN" if len(lines) >= 10 else "OK"
    return CheckResult(
        check_id="C5",
        title="Hook error log healthy",
        status=status,
        detail=f"Last {len(parsed)} of {len(lines)} entries — {summary}",
        remediation=(
            "Read .metaensemble/hooks/log.jsonl. Recurring identical errors point at "
            "a real regression. Fresh-Manifest validation failures are the most common "
            "early-install cause."
        ) if status == "WARN" else None,
    )


# --- Public API -----------------------------------------------------------


def check_venv_entry_point() -> CheckResult:
    """C6: deprecated after wheel-based installs; kept as SKIP for ID stability.

    Older versions checked whether the venv's `metaensemble` script
    was the resilient launcher (rendered by bootstrap.sh) versus the
    auto-generated stub from `pip install -e .` — relevant when the
    editable-install `.pth` file was hidden by iCloud sync. The supported
    install is now `pip install metaensemble` (wheel); the
    bootstrap script and the resilient launcher template are gone.
    """
    return CheckResult(
        check_id="C6",
        title="Venv entry-point script is resilient",
        status="SKIP",
        detail=(
            "Deprecated. The supported install is `pip install "
            "metaensemble` (wheel); the bootstrap-rendered launcher this check "
            "looked for no longer exists. See C9 for the runtime-vendoring "
            "health check that replaces this."
        ),
    )


def _legacy_check_venv_entry_point() -> CheckResult:
    """Legacy implementation, kept inert as reference. Not called."""
    sp = _venv_site_packages()
    if sp is None:
        return CheckResult(
            check_id="C6",
            title="Venv entry-point script is resilient",
            status="WARN",
            detail="Not running inside a venv; entry-point check skipped.",
        )

    venv_root = Path(sys.prefix)
    script = venv_root / "bin" / "metaensemble"
    if not script.exists():
        return CheckResult(
            check_id="C6",
            title="Venv entry-point script is resilient",
            status="WARN",
            detail=f"Entry-point script {script} is missing.",
            remediation="Run `./scripts/bootstrap.sh` from the repo root to render it.",
        )

    try:
        body = script.read_text(errors="replace")
    except OSError as exc:
        return CheckResult(
            check_id="C6",
            title="Venv entry-point script is resilient",
            status="WARN",
            detail=f"Could not read {script}: {exc}",
        )

    # The resilient launcher exports PYTHONPATH; the auto-generated stub does
    # `from metaensemble.cli import main`.
    if "PYTHONPATH" in body and "metaensemble.cli" in body:
        return CheckResult(
            check_id="C6",
            title="Venv entry-point script is resilient",
            status="OK",
            detail=f"{script} is the resilient launcher.",
        )

    return CheckResult(
        check_id="C6",
        title="Venv entry-point script is resilient",
        status="WARN",
        detail=(
            f"{script} is the auto-generated stub. It will fail when the editable "
            "install's .pth file is hidden — common under iCloud-synced folders."
        ),
        remediation=(
            "Run `./scripts/bootstrap.sh` from the repo root. The script will rewrite "
            "the entry point to use PYTHONPATH directly, bypassing the .pth file. "
            "Re-run after any `pip install` that regenerates the entry point."
        ),
    )


def check_window_capacity_calibrated() -> CheckResult:
    """C7: Is the runtime's rate-limit feed wired up?

    The cost gate's window-headroom axis is most accurate when it reads
    the runtime's own `rate_limits` field — exposed to statusline
    scripts in Claude Code v2.1.80+. `metaensemble/statusline/me_status.py`
    captures the feed and persists it for hooks and tools to read.
    This check surfaces whether the wiring is active and the data
    fresh.
    """
    try:
        from metaensemble.lib.config import effective_capacity_tokens, load_budget_config
        from metaensemble.lib.native_state import load_native_rate_limits

        config = load_budget_config()
        native = load_native_rate_limits()

        if native is None:
            return CheckResult(
                check_id="C7",
                title="Runtime rate-limit feed",
                status="WARN",
                detail=(
                    "No native rate-limit data captured. Capacity falls "
                    f"back to the manual setting ({config.window_capacity_tokens:,})."
                ),
                remediation=(
                    "Register MetaEnsemble's statusline script: run "
                    "`metaensemble user-setup` (or re-run it) — the installer "
                    "wires the statusline. The first Claude Code session "
                    "that runs with the statusline will populate the data."
                ),
            )

        if not native.is_fresh:
            age_min = (native.age_seconds or 0) / 60.0
            return CheckResult(
                check_id="C7",
                title="Runtime rate-limit feed",
                status="WARN",
                detail=(
                    f"Last rate-limit capture is {age_min:.0f} min old "
                    "(considered stale beyond 5 min). The cost gate falls "
                    "back to the manual capacity until a session refreshes "
                    "the statusline."
                ),
            )

        if native.five_hour is None:
            return CheckResult(
                check_id="C7",
                title="Runtime rate-limit feed",
                status="WARN",
                detail=(
                    "Native data present but `five_hour_window` is missing. "
                    "The runtime version may not yet ship this field; "
                    "Claude Code v2.1.80+ is required."
                ),
            )

        five_h = native.five_hour
        capacity = effective_capacity_tokens(config)
        source = (
            "manual fallback"
            if capacity == config.window_capacity_tokens
            else "native used_percentage + observed burn for current cwd"
        )
        seven_d_str = (
            f", 7-day {native.seven_day.used_percentage:.1f}%"
            if native.seven_day is not None else ""
        )
        return CheckResult(
            check_id="C7",
            title="Runtime rate-limit feed",
            status="OK",
            detail=(
                f"Capacity: {capacity:,} tokens (source: {source}; "
                f"cwd: {Path.cwd()}). "
                f"5-hour window: {five_h.used_percentage:.1f}% used"
                f"{seven_d_str}. Resets at {five_h.resets_at}."
            ),
        )
    except Exception as exc:
        return CheckResult(
            check_id="C7",
            title="Runtime rate-limit feed",
            status="WARN",
            detail=f"Could not read native rate-limit state: {exc}",
        )


def check_command_namespacing() -> CheckResult:
    """C8: Detect cross-project duplicate slash-command installation.

    A user who runs `metaensemble user-setup --layout=namespaced`
    and `--layout=top-level` later can end up with both forms of the
    seven slash commands wired at the user runtime: the namespaced
    `~/.claude/commands/metaensemble/dispatch.md` from namespaced layout and
    the top-level `~/.claude/commands/dispatch.md` from top-level layout.
    Both work; the runtime exposes both as live commands. The redundancy
    is functional but easy to misread as a misconfiguration, and it
    inflates the runtime's command palette.

    This check surfaces the state and points at the resolution paths
    (uninstall the unwanted layout or accept the duplication and ignore
    one form).
    """
    commands_dir = Path.home() / ".claude" / "commands"
    namespaced_dir = commands_dir / "metaensemble"
    has_namespaced = namespaced_dir.is_dir()

    canonical_top_level = (
        "dispatch", "executors", "ledger", "perf",
        "relaunch", "standup", "limits",
    )
    duplicated_top_level = [
        name for name in canonical_top_level
        if (commands_dir / f"{name}.md").exists()
    ]

    styles_dir = Path.home() / ".claude" / "output-styles"
    duplicated_styles: list[str] = []
    for name in ("wire", "deliverable"):
        if (styles_dir / f"{name}.md").exists() and (
            styles_dir / f"metaensemble-{name}.md"
        ).exists():
            duplicated_styles.append(name)

    if has_namespaced and duplicated_top_level:
        labels = []
        labels.append(
            f"top-level commands also present ({len(duplicated_top_level)}): "
            + ", ".join(duplicated_top_level)
        )
        if duplicated_styles:
            labels.append(
                "duplicated output styles: "
                + ", ".join(f"{s} + metaensemble-{s}" for s in duplicated_styles)
            )
        return CheckResult(
            check_id="C8",
            title="Slash-command namespacing",
            status="WARN",
            detail=(
                "Both namespaced and top-level slash commands are installed at "
                "the user runtime. " + "; ".join(labels) + "."
            ),
            remediation=(
                "If this is intentional (you want both `/dispatch` and "
                "`/metaensemble:dispatch` available), no action is needed. "
                "Otherwise: run `metaensemble user-teardown`, then re-run "
                "`metaensemble user-setup --layout={namespaced,top-level}` "
                "to install the layout you want to keep."
            ),
        )

    return CheckResult(
        check_id="C8",
        title="Slash-command namespacing",
        status="OK",
        detail=(
            "No duplicate namespacing detected — only one of "
            "{namespaced, top-level} is installed at the user runtime."
        ),
    )


def check_runtime_vendored() -> CheckResult:
    """C9: runtime vendoring health.

    Verifies the user-level runtime that backs every slash command, the
    skill, output styles, and the runner:

      - `~/.metaensemble/runtime` exists AND is a symlink (not a regular
        directory from a legacy install).
      - Resolves to a versioned dir under `~/.metaensemble/runtime-versions/`.
      - That version dir has a MANIFEST that verifies (every listed file
        present, hashes match, required files present).
      - The runner at `runtime/bin/me-run` is executable.
    """
    from metaensemble.lib.installer import (
        _runtime_root, _runtime_versions_dir, _runner_path,
        _verify_runtime_manifest_safe,
    )

    runtime = _runtime_root()
    if not runtime.exists() and not runtime.is_symlink():
        return CheckResult(
            check_id="C9",
            title="Runtime vendored",
            status="WARN",
            detail=f"{runtime} does not exist.",
            remediation="Run `metaensemble user-setup` to vendor the runtime.",
        )
    if not runtime.is_symlink():
        return CheckResult(
            check_id="C9",
            title="Runtime vendored",
            status="FAIL",
            detail=(
                f"{runtime} exists but is not a symlink. The "
                "runtime must be a symlink into runtime-versions/ for the "
                "atomic-swap guarantee to hold."
            ),
            remediation=(
                "Run `metaensemble user-teardown --purge-state` then "
                "`metaensemble user-setup` to re-vendor cleanly."
            ),
        )

    try:
        target = runtime.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return CheckResult(
            check_id="C9",
            title="Runtime vendored",
            status="FAIL",
            detail=f"{runtime} symlink does not resolve: {exc}",
            remediation="Run `metaensemble user-setup` to re-vendor.",
        )

    versions_dir = _runtime_versions_dir()
    try:
        target.relative_to(versions_dir)
    except ValueError:
        return CheckResult(
            check_id="C9",
            title="Runtime vendored",
            status="WARN",
            detail=(
                f"{runtime} -> {target}: target is outside "
                f"{versions_dir}. This may be intentional (manual override) "
                "but bypasses the GC + recovery contract."
            ),
        )

    if not _verify_runtime_manifest_safe(target):
        return CheckResult(
            check_id="C9",
            title="Runtime vendored",
            status="FAIL",
            detail=f"{target}/MANIFEST is missing or fails verification.",
            remediation="Run `metaensemble user-setup` to re-vendor.",
        )

    runner = _runner_path()
    if not runner.exists() or not os.access(runner, os.X_OK):
        return CheckResult(
            check_id="C9",
            title="Runtime vendored",
            status="FAIL",
            detail=f"Runner {runner} is missing or not executable.",
            remediation="Run `metaensemble user-setup` to re-vendor.",
        )

    return CheckResult(
        check_id="C9",
        title="Runtime vendored",
        status="OK",
        detail=(
            f"Runtime at {target.name}; MANIFEST verified; runner executable."
        ),
    )


def _parse_doctor_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _stale_pending_sidecars(
    pending_path: Path,
    *,
    older_than: timedelta,
) -> list[str]:
    if not pending_path.exists():
        return []
    cutoff = datetime.now(timezone.utc) - older_than
    stale: list[str] = []
    for entry in pending_path.glob("*.json"):
        ts: datetime | None = None
        try:
            payload = json.loads(entry.read_text())
            if isinstance(payload, dict):
                ts = _parse_doctor_ts(payload.get("started_ts"))
        except (OSError, json.JSONDecodeError):
            ts = None
        if ts is None:
            try:
                ts = datetime.fromtimestamp(entry.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
        if ts <= cutoff:
            stale.append(entry.stem)
    return stale


def _read_run_rows(db_path: Path, run_ids: set[str]) -> dict[str, tuple[str, str | None]]:
    if not run_ids:
        return {}
    import sqlite3

    placeholders = ",".join("?" for _ in sorted(run_ids))
    uri = f"{db_path.resolve(strict=False).as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            f"SELECT run_id, outcome, failure_reason FROM runs WHERE run_id IN ({placeholders})",
            tuple(sorted(run_ids)),
        ).fetchall()
    finally:
        conn.close()
    return {str(row[0]): (str(row[1]), row[2]) for row in rows}


def _recent_reconciled_rows(db_path: Path, since: datetime) -> list[str]:
    import sqlite3

    uri = f"{db_path.resolve(strict=False).as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            """
            SELECT run_id, outcome, failure_reason
            FROM runs
            WHERE ended_ts >= ?
              AND (
                outcome IN ('interrupted', 'budget_exceeded')
                OR failure_reason LIKE '%before PostToolUse%'
                OR failure_reason LIKE '%stale sidecar%'
              )
            ORDER BY ended_ts DESC
            LIMIT 10
            """,
            (since.isoformat(),),
        ).fetchall()
    finally:
        conn.close()
    return [
        f"{row[0]} ({row[1]}: {row[2] or 'no failure_reason'})"
        for row in rows
    ]


def check_ledger_recording_health() -> CheckResult:
    """C10: surface failed Run recording rather than burying it in hook logs."""
    cwd = Path.cwd()
    state_path = cwd / ".metaensemble" / "state"
    log_path = cwd / ".metaensemble" / "hooks" / "log.jsonl"
    db = state_path / "department.db"
    pending_path = state_path / "pending"
    source = f"cwd={cwd}; state={state_path}; log={log_path}"

    if not state_path.exists():
        return CheckResult(
            check_id="C10",
            title="Ledger recording health",
            status="SKIP",
            detail=f"No project state directory to inspect ({source}).",
        )
    if not db.exists():
        return CheckResult(
            check_id="C10",
            title="Ledger recording health",
            status="SKIP",
            detail=f"No Ledger DB to inspect yet ({source}).",
        )

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    try:
        failures = read_post_task_failed_log_entries(log_path, since=since)
        run_ids = {entry.run_id for entry in failures if entry.run_id}
        runs = _read_run_rows(db, run_ids)
        reconciled = _recent_reconciled_rows(db, since)
    except Exception as exc:
        return CheckResult(
            check_id="C10",
            title="Ledger recording health",
            status="WARN",
            detail=f"Could not inspect Ledger recording health: {exc} ({source}).",
        )

    unmatched = []
    matched_recording_failed = 0
    for entry in failures:
        row = runs.get(entry.run_id or "")
        if row is not None and row[0] == OUTCOME_RECORDING_FAILED:
            matched_recording_failed += 1
            continue
        unmatched.append(entry)

    stale_pending = _stale_pending_sidecars(
        pending_path,
        older_than=timedelta(hours=1),
    )

    if unmatched:
        sample = "; ".join(
            f"{entry.run_id or '<missing-run-id>'}: {entry.message or 'no message'}"
            for entry in unmatched[:3]
        )
        return CheckResult(
            check_id="C10",
            title="Ledger recording health",
            status="FAIL",
            detail=(
                f"{len(unmatched)} recent post-task-failed hook-log entr"
                f"{'y' if len(unmatched) == 1 else 'ies'} have no matching "
                f"`{OUTCOME_RECORDING_FAILED}` Run row. {sample}. {source}."
            ),
            remediation=(
                "Run `metaensemble reconcile` from the project root. If the "
                "same run remains unmatched, inspect the hook-log message and "
                "the pending sidecar for that run_id."
            ),
        )

    warnings: list[str] = []
    if stale_pending:
        warnings.append(
            f"{len(stale_pending)} stale pending sidecar(s): "
            + ", ".join(stale_pending[:5])
        )
    if reconciled:
        warnings.append(
            f"{len(reconciled)} recent reconciled incomplete Run row(s): "
            + "; ".join(reconciled[:3])
        )
    if warnings:
        return CheckResult(
            check_id="C10",
            title="Ledger recording health",
            status="WARN",
            detail=(
                "; ".join(warnings)
                + f". Matched recording_failed log pairs: {matched_recording_failed}. "
                + source
            ),
        )

    return CheckResult(
        check_id="C10",
        title="Ledger recording health",
        status="OK",
        detail=(
            "No unmatched recent post-task recording failures or stale pending "
            f"sidecars. Matched recording_failed log pairs: "
            f"{matched_recording_failed}. {source}."
        ),
    )


# C11: catalog hygiene -- detect macOS Finder / iCloud sync duplicate files
# in the catalog directories that MetaEnsemble enumerates.

_CATALOG_DUPLICATE_PATTERN = re.compile(r"^.+ \d+$")


def _catalog_scan_dirs(home: Path | None = None) -> list[Path]:
    """Catalog directories to scan for duplicate-file leakage.

    Three layers:
      - Source tree   : metaensemble/{roles,commands,skills,output-styles}
      - Vendored runtime: ~/.metaensemble/runtime/{roles,commands,skills,output-styles}
      - User-level     : ~/.claude/{commands,skills,output-styles}

    The user-level layer has no `agents/` (agents are user-authored, not a
    MetaEnsemble catalog), so it is intentionally omitted.
    """
    from metaensemble.lib import installer  # local import to avoid cycle

    home = home or Path.home()
    dirs: list[Path] = []
    catalog_subdirs = ("roles", "commands", "skills", "output-styles")

    source_root = installer.CORE_DIR
    for sub in catalog_subdirs:
        dirs.append(source_root / sub)

    runtime_root = home / ".metaensemble" / "runtime"
    for sub in catalog_subdirs:
        dirs.append(runtime_root / sub)

    user_runtime = home / ".claude"
    for sub in ("commands", "skills", "output-styles"):
        dirs.append(user_runtime / sub)

    return dirs


def _scan_catalog_for_duplicates(directory: Path) -> list[Path]:
    """Return paths in `directory` whose stem matches the duplicate pattern."""
    if not directory.is_dir():
        return []
    hits: list[Path] = []
    try:
        for entry in directory.iterdir():
            stem = entry.stem if entry.is_file() else entry.name
            if _CATALOG_DUPLICATE_PATTERN.match(stem):
                hits.append(entry)
    except OSError:
        return []
    return hits


def check_catalog_hygiene(home: Path | None = None) -> CheckResult:
    """C11: detect filesystem duplicates (`architect 2.md`, etc.) in catalogs.

    macOS Finder, iCloud Drive sync, and some pip-install upgrade paths
    leave behind " N"-suffixed conflict copies. MetaEnsemble filters them
    correctly at catalog enumeration time (`_is_canonical_curated_name`),
    so they do not poison the inspect report or the install plan. This
    check still surfaces them as a WARN so the Principal can clean them
    up: they consume iCloud quota, slow installs, and risk confusing
    third-party tooling that does not know to filter them.
    """
    all_hits: list[Path] = []
    for scan_dir in _catalog_scan_dirs(home):
        all_hits.extend(_scan_catalog_for_duplicates(scan_dir))

    if not all_hits:
        return CheckResult(
            check_id="C11",
            title="Catalog hygiene (no duplicate files in MetaEnsemble catalogs)",
            status="OK",
            detail="Zero duplicate files detected across source tree, vendored runtime, and user-level catalogs.",
        )

    # Sort for deterministic output; truncate examples to first 5.
    all_hits.sort()
    examples = "\n".join(f"  - {p}" for p in all_hits[:5])
    if len(all_hits) > 5:
        examples += f"\n  - ... ({len(all_hits) - 5} more)"

    detail = (
        f"Detected {len(all_hits)} duplicate file(s) matching the macOS Finder / "
        f"iCloud sync pattern (stem ends with ` N`). First examples:\n{examples}"
    )
    remediation = (
        "Catalog hygiene: detected duplicate files matching the macOS Finder / "
        "iCloud sync pattern (e.g., `architect 2.md`). MetaEnsemble filters these "
        "correctly at catalog enumeration time. Likely cause: iCloud Desktop & "
        "Documents sync of the project's parent directory creating conflict copies "
        "during pip install operations.\n\n"
        "To eliminate the source: exclude `.venv/` from iCloud Drive sync, OR move "
        "the project outside `~/Desktop/`. The files are safe to delete manually "
        "if you don't want them."
    )
    return CheckResult(
        check_id="C11",
        title="Catalog hygiene (no duplicate files in MetaEnsemble catalogs)",
        status="WARN",
        detail=detail,
        remediation=remediation,
    )


def check_install_topology() -> CheckResult:
    """C12: The Python pinned in `me-run` has a non-editable install of
    `metaensemble`.

    The documented install promise is that once `metaensemble user-setup`
    has vendored the runtime, the dev source tree is dispensable. That
    holds only when the pinned Python's `metaensemble` distribution was
    installed from a wheel. An editable install (`pip install -e .`)
    leaves the runner load-bearing on the source tree; deleting or
    moving the source breaks every hook.

    Returns:
      - OK when the pinned interpreter has a non-editable install.
      - WARN when the install is editable. Names the source path and
        the wheel-rebuild recovery path so the user can re-create the
        install in the documented topology.
      - FAIL when the pinned interpreter has no `metaensemble` install
        at all. Hooks and the runner would fail with `ModuleNotFoundError`.
      - WARN when the runner is missing or unparseable (user-setup has
        not been run yet, or the template shape changed).
    """
    from metaensemble.lib.topology import (
        detect_editable_install,
        runner_python_path,
    )

    pinned = runner_python_path()
    if pinned is None:
        return CheckResult(
            check_id="C12",
            title="Install topology — pinned interpreter is non-editable",
            status="WARN",
            detail=(
                "Could not locate or parse `~/.metaensemble/runtime/bin/me-run`. "
                "user-setup has not run yet, or the runner template no longer "
                "matches the generated shape."
            ),
            remediation=(
                "Run `metaensemble user-setup --layout={namespaced|top-level}` "
                "from the Python interpreter you want pinned."
            ),
        )

    topology = detect_editable_install(pinned)

    if not topology.installed:
        return CheckResult(
            check_id="C12",
            title="Install topology — pinned interpreter is non-editable",
            status="FAIL",
            detail=(
                f"Pinned Python `{pinned}` does not have `metaensemble` "
                "installed. Hooks and the runner will fail with "
                "ModuleNotFoundError on every invocation."
            ),
            remediation=(
                f"Install metaensemble into that interpreter and re-run user-setup:\n"
                f"  {pinned} -m pip install metaensemble\n"
                f"  {pinned} -m metaensemble user-setup --layout={{namespaced|top-level}}"
            ),
        )

    if topology.editable:
        return CheckResult(
            check_id="C12",
            title="Install topology — pinned interpreter is non-editable",
            status="WARN",
            detail=(
                f"Pinned Python `{pinned}` has `metaensemble` installed in "
                f"editable mode (source: {topology.source}). The runner is "
                "load-bearing on that source tree — deleting or moving it "
                "breaks every hook."
            ),
            remediation=(
                "Build a wheel and install it into a non-editable interpreter, "
                "then re-run user-setup from that interpreter:\n"
                "  python -m build --wheel\n"
                "  <other-python> -m pip install dist/metaensemble-*.whl\n"
                "  <other-python> -m metaensemble user-setup --layout=<layout>"
            ),
        )

    return CheckResult(
        check_id="C12",
        title="Install topology — pinned interpreter is non-editable",
        status="OK",
        detail=(
            f"Pinned Python `{pinned}` has a non-editable install of metaensemble. "
            "The runner is independent of any dev source tree."
        ),
    )


def run_doctor(*, fix: bool = False) -> DoctorReport:
    """Run every check; optionally apply safe fixes.

    C1 and C6 are SKIPs (deprecated, kept for ID stability).
    C9 covers runtime vendoring health.
    C12 covers install-topology (non-editable, non-load-bearing on dev source).
    """
    return DoctorReport(results=[
        check_pth_files(fix=fix),
        check_hook_wiring(),
        check_schemas(),
        check_project_state(),
        check_hook_log(),
        check_venv_entry_point(),
        check_window_capacity_calibrated(),
        check_command_namespacing(),
        check_runtime_vendored(),
        check_ledger_recording_health(),
        check_catalog_hygiene(),
        check_install_topology(),
    ])


def render_report(report: DoctorReport) -> str:
    """Render a Markdown digest for the Principal.

    Status is text-only (no glyphs) so the output renders consistently in
    every terminal and pipes cleanly into logs. The closing summary is
    *action-oriented*: when WARNs reflect setup steps that have not yet
    happened (no hooks registered, no project state), we say
    "N setup steps remaining" with the specific commands to run, rather
    than "clean (N warnings) — no failures detected", which read as
    "ready to go" even when the system was not yet installed.
    """
    lines = ["## MetaEnsemble doctor"]
    for r in report.results:
        suffix = " (fixed)" if r.fixed else ""
        lines.append(f"\n### [{r.status}] {r.check_id} — {r.title}{suffix}")
        lines.append(r.detail)
        if r.remediation and not r.fixed:
            lines.append("")
            lines.append("**Remediation:**")
            lines.append(r.remediation)
    lines.append("")

    by_id = {r.check_id: r for r in report.results}
    n_fail = sum(1 for r in report.results if r.status == "FAIL")
    n_warn = sum(1 for r in report.results if r.status == "WARN")

    # Recognize the "setup not yet done" pattern: hook wiring missing
    # and/or project state missing. Surface as setup steps, not warnings.
    setup_steps: list[str] = []
    c2 = by_id.get("C2")
    c4 = by_id.get("C4")
    if c2 and c2.status == "WARN" and ("no hook entries" in c2.detail or "not found" in c2.detail):
        setup_steps.append("Run `metaensemble user-setup --layout=namespaced` (or `--layout=top-level`) to register the lifecycle hooks.")
    if c4 and c4.status == "WARN" and "does not exist" in c4.detail:
        setup_steps.append("Run `metaensemble adopt` from your project root (or `metaensemble init` if you only want project state).")

    if n_fail:
        lines.append(
            f"**Status: {n_fail} failure(s), {n_warn} warning(s).** "
            "Address failures before the next install or dispatch."
        )
    elif setup_steps:
        lines.append(f"**Status: {len(setup_steps)} setup step(s) remaining.**")
        for s in setup_steps:
            lines.append(f"- {s}")
    elif n_warn:
        lines.append(f"**Status: clean ({n_warn} warning(s)).** No failures detected.")
    else:
        lines.append("**Status: clean.** All checks passed.")
    return "\n".join(lines)
