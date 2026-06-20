"""Pending-Run sidecar storage.

The PreToolUse hook stamps a pending Run record on disk before the agent
runtime spawns the Task. PostToolUse reads the sidecar back, fills in
end-of-Run fields (ended_ts, tokens_out, outcome, deliverable_path), and
writes the completed Run to the Ledger.

Sidecars live under `<state>/pending/<run_id>.json`. We key by `run_id`
because Claude Code's hook payload includes `session_id` reliably, and
pre_task / post_task share that session id for correlation.

The sidecar approach avoids extending the Ledger schema (which forbids
'pending' as an outcome) while still giving us the start-time and
estimated-budget fields we need at completion time.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


SIDECAR_SUFFIX = ".json"


@dataclass(frozen=True)
class PendingRun:
    """Everything pre_task records about a Run before it completes.

    `manifest_id` and `manifest_path` are populated when the Coordinator
    references a Manifest via the `[manifest: hm-...]` prompt marker.
    """

    run_id: str
    session_id: str
    executor_id: str
    task_id: str
    role_id: str
    model_tier: str
    started_ts: str
    window_id: str
    estimated_tokens_in: int
    manifest_id: str | None = None
    manifest_path: str | None = None
    brief_in_path: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def pending_dir(state_dir: Path) -> Path:
    """Resolve the sidecar directory under the project state root."""
    return Path(state_dir) / "pending"


def _sidecar_path(state_dir: Path, run_id: str) -> Path:
    return pending_dir(state_dir) / f"{run_id}{SIDECAR_SUFFIX}"


def write_pending(state_dir: Path, pending: PendingRun) -> Path:
    """Write a PendingRun JSON sidecar. Returns the file path written."""
    target = _sidecar_path(state_dir, pending.run_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(asdict(pending), indent=2))
    return target


def read_pending(state_dir: Path, run_id: str) -> PendingRun | None:
    """Read one PendingRun by run_id. Returns None if the sidecar is absent."""
    path = _sidecar_path(state_dir, run_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return _coerce_pending(data)


def latest_pending_for_session(state_dir: Path, session_id: str) -> PendingRun | None:
    """Return the most-recently-written PendingRun for a given session.

    PostToolUse uses this when it does not know the run_id directly:
    the most recent pending sidecar belonging to this session is the
    Run whose PostToolUse we are handling.
    """
    p = pending_dir(state_dir)
    if not p.exists():
        return None
    candidates: list[tuple[float, PendingRun]] = []
    for entry in p.glob(f"*{SIDECAR_SUFFIX}"):
        try:
            data = json.loads(entry.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("session_id") != session_id:
            continue
        pending = _coerce_pending(data)
        if pending is None:
            continue
        candidates.append((entry.stat().st_mtime, pending))
    if not candidates:
        return None
    candidates.sort(key=lambda kv: kv[0], reverse=True)
    return candidates[0][1]


def pending_by_tool_use_id(state_dir: Path, tool_use_id: str) -> PendingRun | None:
    """Return the PendingRun whose stamp carries this tool_use_id.

    post_task(Agent) uses this to reconcile a background launch (which carries
    the runtime agentId) back to the pre_task stamp, since agentId is unknown at
    PreToolUse time. Keyed by tool_use_id rather than session, so concurrent /
    fan-out dispatches in one session never cross-correlate.
    """
    if not tool_use_id:
        return None
    p = pending_dir(state_dir)
    if not p.exists():
        return None
    for entry in p.glob(f"*{SIDECAR_SUFFIX}"):
        try:
            data = json.loads(entry.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if (data.get("extra") or {}).get("tool_use_id") == tool_use_id:
            pending = _coerce_pending(data)
            if pending is not None:
                return pending
    return None


def delete_pending(state_dir: Path, run_id: str) -> bool:
    """Delete a sidecar after the Run has been written to the Ledger.

    Returns True if a file was removed, False if no sidecar existed.
    """
    path = _sidecar_path(state_dir, run_id)
    if not path.exists():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _coerce_pending(data: dict[str, Any]) -> PendingRun | None:
    """Build a PendingRun from a dict, tolerating extra/missing fields."""
    try:
        return PendingRun(
            run_id=data["run_id"],
            session_id=data["session_id"],
            executor_id=data["executor_id"],
            task_id=data["task_id"],
            role_id=data["role_id"],
            model_tier=data.get("model_tier", "sonnet"),
            started_ts=data["started_ts"],
            window_id=data["window_id"],
            estimated_tokens_in=int(data.get("estimated_tokens_in", 0)),
            manifest_id=data.get("manifest_id"),
            manifest_path=data.get("manifest_path"),
            brief_in_path=data.get("brief_in_path"),
            extra=data.get("extra") or {},
        )
    except (KeyError, ValueError, TypeError):
        return None
