"""Pending-sidecar reconciliation.

The PreToolUse hook stamps a sidecar in `<state>/pending/<run_id>.json`
before the runtime spawns the Task. The PostToolUse hook is supposed to
read it back, write the completed Run row to the Ledger, and delete the
sidecar. When that loop is interrupted — `kill -9`, runtime crash, the
parent process exiting before PostToolUse fires (notably budget
exhaustion via `claude --max-budget-usd`) — the sidecar is stranded and
the Ledger has no record that anything happened.

This module closes the gap. Reconciliation is two-layer:

  Layer 1: the Stop hook calls `reconcile_session_pending(...)` to mop
           up sidecars that belong to the ending session. Catches Ctrl-C
           and graceful runtime exit.

  Layer 2: `reconcile_stale_pending(...)` walks every sidecar older than
           a threshold, regardless of session, and reconciles them. The
           SessionStart hook calls this with a generous default (1 hour);
           the `metaensemble reconcile` CLI calls it on demand with a
           configurable threshold. Catches `kill -9` and budget kills.

The reconciled Run row uses `outcome="interrupted"` unless the parent
transcript proves a budget kill, in which case it uses
`outcome="budget_exceeded"`, or a matching hook-log `post-task-failed`
entry proves the recording pipeline itself failed, in which case it uses
`outcome="recording_failed"`. The `failure_reason` distinguishes:

  - "session ended before PostToolUse"
    The session simply ended without the Task's PostToolUse firing.

  - "stale sidecar reconciled by metaensemble"
    The sidecar was older than the staleness threshold and reconciled
    without other evidence of the cause.

  - "budget exceeded before PostToolUse"
    The parent transcript contains Claude Code's max-budget marker.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from metaensemble.lib.file_events import (
    clear_active_dispatch_for_run,
    clear_file_events,
    has_active_agent_marker,
    read_file_events,
)
from metaensemble.lib.ledger import Executor, Ledger, Run
from metaensemble.lib.ledger import (
    OUTCOME_BUDGET_EXCEEDED,
    OUTCOME_INTERRUPTED,
    OUTCOME_RECORDING_FAILED,
    PostTaskFailedLogEntry,
    read_post_task_failed_log_entries,
    recording_failure_reason,
)
from metaensemble.lib.recording import build_deliverable_ref
from metaensemble.lib.runtime_payload import normalize_model_identity
from metaensemble.lib.sidecar import (
    PendingRun,
    delete_pending,
    pending_dir,
    read_pending,
    SIDECAR_SUFFIX,
)


# `failure_reason` strings. Stable identifiers — `standup`/`perf` and any
# downstream tooling key off these literals.
REASON_SESSION_END = "session ended before PostToolUse"
REASON_STALE = "stale sidecar reconciled by metaensemble"
REASON_BUDGET_EXCEEDED = "budget exceeded before PostToolUse"
_BUDGET_MARKERS = (
    "error_max_budget_usd",
    "reached maximum budget",
    "maximum budget",
    "max_budget_usd",
)


@dataclass(frozen=True)
class ReconciledRun:
    """One reconciled sidecar, returned so callers can report or test the result."""

    run_id: str
    session_id: str
    executor_id: str
    task_id: str
    reason: str


def _build_failed_run(
    pending: PendingRun,
    reason: str,
    outcome: str = OUTCOME_INTERRUPTED,
    role_version: str | None = None,
    ended_ts: str | None = None,
    files_touched_json: str | None = None,
    tool_use_json: str | None = None,
    deliverable_ref_json: str | None = None,
) -> Run:
    ended_ts = ended_ts or datetime.now(timezone.utc).isoformat()
    return Run(
        run_id=pending.run_id,
        executor_id=pending.executor_id,
        task_id=pending.task_id,
        model=normalize_model_identity(pending.model_tier) or "unknown",
        tokens_in=pending.estimated_tokens_in,
        tokens_out=0,
        window_id=pending.window_id,
        started_ts=pending.started_ts,
        ended_ts=ended_ts,
        outcome=outcome,
        brief_in_path=pending.brief_in_path,
        brief_out_path=None,
        deliverable_path=None,
        failure_reason=reason,
        quality_state=None,
        quality_findings_json=None,
        role_version=role_version,
        requested_model_tier=normalize_model_identity(pending.model_tier),
        model_source="tier_fallback",
        deliverable_ref_json=deliverable_ref_json,
        files_touched_json=files_touched_json,
        tool_use_json=tool_use_json,
    )


def _collect_file_provenance(
    state_dir: Path,
    pending: PendingRun,
    ended_ts: str,
) -> tuple[str | None, str | None, str | None, tuple]:
    events = read_file_events(
        state_dir,
        run_id=pending.run_id,
        session_id=pending.session_id,
        started_ts=pending.started_ts,
        ended_ts=ended_ts,
    )
    if not events:
        return None, None, None, ()
    touched = sorted({e.rel_path or e.path for e in events})
    tool_counts: dict[str, int] = {}
    for event in events:
        tool_counts[event.tool_name] = tool_counts.get(event.tool_name, 0) + 1
    tool_use = [
        {"name": name, "count": count, "input_tokens": 0}
        for name, count in sorted(tool_counts.items())
    ]
    deliverable_ref = build_deliverable_ref("", files_touched=tuple(touched))
    return (
        json.dumps(touched),
        json.dumps(tool_use),
        json.dumps(deliverable_ref) if deliverable_ref else None,
        events,
    )


def _record_failed_run(
    ledger: Ledger,
    state_dir: Path,
    pending: PendingRun,
    reason: str,
    outcome: str | None = None,
) -> None:
    """Append the failed Run, update the Executor's last_seen, drop the sidecar.

    Idempotent: callers can run `reconcile_stale_pending` repeatedly and
    only the first call recording a given run_id will write a row;
    subsequent calls see the sidecar already gone and skip.
    """
    role = ledger.get_role(pending.role_id)
    ended_ts = datetime.now(timezone.utc).isoformat()
    files_touched_json, tool_use_json, deliverable_ref_json, events = (
        _collect_file_provenance(state_dir, pending, ended_ts)
    )
    run = _build_failed_run(
        pending,
        reason,
        outcome=outcome or OUTCOME_INTERRUPTED,
        role_version=role.version if role else None,
        ended_ts=ended_ts,
        files_touched_json=files_touched_json,
        tool_use_json=tool_use_json,
        deliverable_ref_json=deliverable_ref_json,
    )
    ledger.append_run(run)
    clear_file_events(state_dir, run_id=pending.run_id, events=events)
    clear_active_dispatch_for_run(pending.run_id, session_id=pending.session_id)
    existing = ledger.get_executor(pending.executor_id)
    if existing:
        ledger.upsert_executor(
            Executor(
                executor_id=existing.executor_id,
                alias=existing.alias,
                role_id=existing.role_id,
                parent_executor_id=existing.parent_executor_id,
                created_ts=existing.created_ts,
                last_seen_ts=run.ended_ts,
                status=existing.status,
            )
        )


def _classify_reconcile_cause(
    state_dir: Path,
    pending: PendingRun,
    default_reason: str,
) -> tuple[str, str]:
    post_task_failed = _post_task_failed_entry_for_run(state_dir, pending.run_id)
    if post_task_failed is not None:
        return recording_failure_reason(post_task_failed), OUTCOME_RECORDING_FAILED
    if _pending_transcript_has_budget_marker(pending):
        return REASON_BUDGET_EXCEEDED, OUTCOME_BUDGET_EXCEEDED
    return default_reason, OUTCOME_INTERRUPTED


def _post_task_failed_entry_for_run(
    state_dir: Path,
    run_id: str,
) -> PostTaskFailedLogEntry | None:
    log_path = state_dir.parent / "hooks" / "log.jsonl"
    matches = [
        entry
        for entry in read_post_task_failed_log_entries(log_path)
        if entry.run_id == run_id
    ]
    return matches[-1] if matches else None


def _pending_transcript_has_budget_marker(pending: PendingRun) -> bool:
    extra = pending.extra or {}
    transcript = extra.get("transcript_path") or extra.get("parent_transcript_path")
    if not transcript:
        return False
    path = Path(str(transcript)).expanduser()
    try:
        text = path.read_text(errors="ignore")[-1_000_000:].lower()
    except OSError:
        return False
    return any(marker in text for marker in _BUDGET_MARKERS)


def _iter_pending(state_dir: Path) -> list[tuple[Path, PendingRun]]:
    """Walk the pending directory and yield (path, parsed-sidecar) pairs.

    Sidecars that fail to parse are skipped silently — they will be picked
    up by the next reconcile pass once the file is repaired or removed by
    hand. Returning a list (not a generator) lets callers query length
    cheaply for reporting.
    """
    p = pending_dir(state_dir)
    if not p.exists():
        return []
    out: list[tuple[Path, PendingRun]] = []
    for entry in p.glob(f"*{SIDECAR_SUFFIX}"):
        pending = read_pending(state_dir, entry.stem)
        if pending is None:
            continue
        out.append((entry, pending))
    return out


def reconcile_session_pending(
    ledger: Ledger,
    state_dir: Path,
    session_id: str,
) -> list[ReconciledRun]:
    """Layer 1: reconcile every pending sidecar belonging to one session.

    Called from the Stop hook. Walks sidecars whose `session_id` matches
    the ending session, writes a failed Run for each, and removes the
    sidecar. Sidecars for other sessions are left untouched so a
    concurrent runtime instance can still complete its own Tasks.
    """
    reconciled: list[ReconciledRun] = []
    for entry, pending in _iter_pending(state_dir):
        if pending.session_id != session_id:
            continue
        # An in-flight BACKGROUND dispatch outlives the parent turn: post_task
        # deferred it (live agent marker) and SubagentStop will finalize it,
        # often AFTER this Stop hook. Never sweep it here, or we record a bogus
        # "session ended" Run and clear the marker out from under the subagent.
        if has_active_agent_marker(pending.run_id):
            continue
        reason, outcome = _classify_reconcile_cause(
            state_dir, pending, REASON_SESSION_END
        )
        _record_failed_run(ledger, state_dir, pending, reason, outcome=outcome)
        delete_pending(state_dir, pending.run_id)
        reconciled.append(
            ReconciledRun(
                run_id=pending.run_id,
                session_id=pending.session_id,
                executor_id=pending.executor_id,
                task_id=pending.task_id,
                reason=reason,
            )
        )
    return reconciled


def reconcile_stale_pending(
    ledger: Ledger,
    state_dir: Path,
    max_age: timedelta = timedelta(hours=1),
    now: datetime | None = None,
) -> list[ReconciledRun]:
    """Layer 2: reconcile sidecars older than `max_age`, regardless of session.

    Called from SessionStart with a 1-hour default to clean up sidecars
    abandoned by previous sessions, and from `metaensemble reconcile`
    with a user-supplied threshold (default 0, meaning "every sidecar
    right now").

    Age is measured against the sidecar's `started_ts` rather than the
    file mtime, because mtime can be touched by backup tooling. If
    `started_ts` is unparseable, the file mtime is used as a fallback.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - max_age
    reconciled: list[ReconciledRun] = []
    for entry, pending in _iter_pending(state_dir):
        try:
            sidecar_ts: datetime
            try:
                sidecar_ts = datetime.fromisoformat(pending.started_ts)
                if sidecar_ts.tzinfo is None:
                    sidecar_ts = sidecar_ts.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                sidecar_ts = datetime.fromtimestamp(
                    entry.stat().st_mtime, tz=timezone.utc
                )
            if sidecar_ts > cutoff:
                continue
            # Idempotency guard: if this run_id is already recorded (finalized by
            # a background SubagentStop, a prior reconcile, or a duplicate/restored
            # sidecar), the sidecar is stale. Drop it without re-inserting — a
            # second append would hit the runs PRIMARY KEY. This unguarded insert
            # is what kept session_start crashing with "(state unavailable)".
            if ledger.run_exists(pending.run_id):
                # Stale duplicate: the Run is already recorded. Clean up its
                # residue without re-inserting. File-event residue is keyed by
                # run_id, so it is always safe to clear. The active-dispatch
                # marker is cleared ONLY when it still points at THIS run_id --
                # clearing by session alone is unsafe while markers collide on
                # "unknown-session" (it could delete a different live dispatch's
                # marker).
                clear_file_events(state_dir, run_id=pending.run_id)
                clear_active_dispatch_for_run(
                    pending.run_id, session_id=pending.session_id
                )
                delete_pending(state_dir, pending.run_id)
                continue
            reason, outcome = _classify_reconcile_cause(state_dir, pending, REASON_STALE)
            _record_failed_run(ledger, state_dir, pending, reason, outcome=outcome)
            delete_pending(state_dir, pending.run_id)
            reconciled.append(
                ReconciledRun(
                    run_id=pending.run_id,
                    session_id=pending.session_id,
                    executor_id=pending.executor_id,
                    task_id=pending.task_id,
                    reason=reason,
                )
            )
        except Exception as exc:
            # One malformed or uncooperative sidecar must never take down the
            # whole reconcile pass (and with it session_start). Log and skip.
            try:
                from metaensemble.hooks._common import log_error

                log_error(
                    "reconcile-sidecar-failed",
                    str(exc),
                    {"run_id": getattr(pending, "run_id", None)},
                )
            except Exception:
                pass
            continue
    return reconciled
