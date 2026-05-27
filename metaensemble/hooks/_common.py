"""Shared utilities for MetaEnsemble hooks.

Per PERFORMANCE.md §3 R7, hooks are fast (<100ms p95), idempotent, and
free of model calls and network I/O. They share state-directory location
and error-logging behavior through this module so each hook script stays
small and single-purpose.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# --- State directory resolution --------------------------------------------

def state_dir() -> Path:
    """Return the project-level `.metaensemble/state/` directory.

    Honors the `METAENSEMBLE_STATE_DIR` environment variable for tests and
    custom deployments; otherwise resolves to `<cwd>/.metaensemble/state/`.
    """
    override = os.environ.get("METAENSEMBLE_STATE_DIR")
    if override:
        return Path(override)
    return Path.cwd() / ".metaensemble" / "state"


def project_root_from_payload(payload: dict[str, Any]) -> Path | None:
    """Resolve an explicit `[project: ...]` prompt marker, if present."""
    try:
        from metaensemble.lib.recording import parse_markers
    except Exception:
        return None
    tool_input = payload.get("tool_input")
    prompt = tool_input.get("prompt") if isinstance(tool_input, dict) else None
    markers = parse_markers(prompt if isinstance(prompt, str) else None)
    project_path = markers.get("project_path")
    if not project_path:
        return None
    root = Path(project_path).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    root = root.resolve(strict=False)
    if not (root / ".metaensemble").is_dir():
        return None
    return root


def state_dir_for_payload(payload: dict[str, Any]) -> Path:
    """Return the Ledger state dir for a hook payload.

    Explicit `[project: ...]` markers take precedence so a dispatch started
    from a different cwd still records into the intended adopted project.
    """
    project_root = project_root_from_payload(payload)
    if project_root is not None:
        return project_root / ".metaensemble" / "state"
    return state_dir()


def db_path() -> Path:
    return state_dir() / "department.db"


def db_path_for_state(run_state_dir: Path) -> Path:
    return Path(run_state_dir) / "department.db"


def jsonl_path() -> Path:
    return state_dir() / "runs.jsonl"


def jsonl_path_for_state(run_state_dir: Path) -> Path:
    return Path(run_state_dir) / "runs.jsonl"


def hooks_log_path() -> Path:
    return state_dir().parent / "hooks" / "log.jsonl"


def migration_sql() -> str:
    """Return the canonical migration script text. Idempotent to apply."""
    here = Path(__file__).resolve().parent.parent
    return (here / "state" / "migrations" / "001_init.sql").read_text()


# --- Stdin / stdout JSON contract -----------------------------------------

def read_input() -> dict[str, Any]:
    """Read hook input from stdin as JSON. Returns {} if stdin is empty."""
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        log_error("invalid-stdin-json", str(exc), {"raw": raw[:500]})
        return {}


def emit(payload: dict[str, Any]) -> None:
    """Write a JSON object to stdout. The agent runtime parses it as the hook's response."""
    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()


# --- Error logging --------------------------------------------------------

def log_error(kind: str, message: str, context: dict[str, Any] | None = None) -> None:
    """Append a structured error record to `.metaensemble/hooks/log.jsonl`.

    Hooks never block on logging failures — if the log write itself fails,
    we silently swallow it, because a stuck hook is worse than a missed
    log entry (PERFORMANCE.md §3 R7).
    """
    try:
        log_file = hooks_log_path()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "message": message,
            "context": context or {},
        }
        with log_file.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:  # nosec B110
        # Hooks must remain non-blocking when the diagnostic sink itself
        # fails; raising here would turn a missed log line into a user-visible
        # hook failure.
        pass


# --- Window-id derivation -------------------------------------------------

def current_window_id(at: datetime | None = None) -> str:
    """Return the 5-hour window bucket for the given moment.

    Buckets align to 5-hour blocks starting at 00:00 UTC each day.
    Format: `YYYY-MM-DDTHH` where HH is the start of the bucket.
    """
    now = at or datetime.now(timezone.utc)
    bucket_start = (now.hour // 5) * 5
    return f"{now.year:04d}-{now.month:02d}-{now.day:02d}T{bucket_start:02d}"
