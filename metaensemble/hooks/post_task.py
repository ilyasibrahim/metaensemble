#!/usr/bin/env python3
"""PostToolUse hook for Task invocations.

PostToolUse completes the Run record that PreToolUse stamped. It reads
the pending-Run sidecar by session id, fills in the end-of-Run fields
(`ended_ts`, `tokens_out`, `outcome`, `deliverable_path`), writes the
completed Run to the Ledger, and removes the sidecar.

If no sidecar exists for this session — for example, because PreToolUse
blocked the Task or because the runtime invoked PostToolUse without a
matching PreToolUse — this hook logs the omission and exits 0. It never
blocks PostToolUse; the agent runtime has already produced the user-
visible result.

Hook contract:
- Stdin: agent runtime payload with `tool_name`, `tool_input`,
  `tool_response`, `session_id`
- Stdout: JSON with `continue: true`
- Exit: 0 always
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import json  # noqa: E402

from metaensemble.hooks._common import (  # noqa: E402
    db_path_for_state,
    emit,
    jsonl_path_for_state,
    log_error,
    migration_sql,
    project_root_from_payload,
    read_input,
    state_dir,
    state_dir_for_payload,
)
from metaensemble.lib.config import load_quality_config  # noqa: E402
from metaensemble.lib.cost_gate import GateState  # noqa: E402
from metaensemble.lib.file_events import (  # noqa: E402
    ActiveDispatch,
    clear_active_dispatch_for_run,
    clear_file_events,
    read_active_dispatch,
    read_file_events,
    write_active_dispatch_by_agent,
)
from metaensemble.lib.ledger import Executor, Ledger, Run  # noqa: E402
from metaensemble.lib.manifest import load_manifest  # noqa: E402
from metaensemble.lib.native_state import load_native_rate_limits  # noqa: E402
from metaensemble.lib.quality_gate import build_decision  # noqa: E402
from metaensemble.lib.quality_runners import run_all_axes  # noqa: E402
from metaensemble.lib.recording import (  # noqa: E402
    build_deliverable_ref,
    classify_failure_reason,
    classify_outcome,
    coerce_to_text,
    estimate_tokens,
    extract_deliverable_path,
)
from metaensemble.lib.runtime_payload import normalize_model_identity  # noqa: E402
from metaensemble.lib.sidecar import (  # noqa: E402
    PendingRun,
    delete_pending,
    latest_pending_for_session,
    pending_by_tool_use_id,
)
from metaensemble.lib.transcript import (  # noqa: E402
    dominant_model,
    transcript_path_for_session,
    walk_transcript,
)


def _quality_files_from_manifest(manifest_path: str | None) -> list[Path]:
    """Return every file declared in the Manifest's `expected_deliverables`.
    Relevance is decided per axis downstream: the Python runners filter to
    .py internally, and configured axis commands fire on non-Python
    deliverables. Empty list means the quality gate skips."""
    if not manifest_path:
        return []
    try:
        manifest = load_manifest(manifest_path)
    except Exception:
        # Manifest invalid or missing — quality gate cannot evaluate.
        return []
    files: list[Path] = []
    for entry in manifest.get("expected_deliverables", []):
        path = entry.get("path")
        if path:
            files.append(Path(path))
    return files


def _run_quality_gate(
    manifest_path: str | None, project_root: Path
) -> tuple[str | None, str | None, str | None]:
    """Run the quality gate on a Run's expected deliverables.

    Returns `(quality_state, quality_findings_json, summary)` where each
    is None when the gate does not run (no manifest, no evaluable
    deliverables, config load failure, etc.). The summary is the
    one-paragraph English block the hook surfaces to the Coordinator on
    NOTIFY or BLOCK.
    """
    files = _quality_files_from_manifest(manifest_path)
    if not files:
        return None, None, None
    try:
        config = load_quality_config()
        if all(f.suffix != ".py" for f in files) and not config.axis_commands:
            # Nothing the gate can evaluate: no Python deliverables for
            # the built-in runners and no configured axis commands.
            return None, None, None
        axes = run_all_axes(files, config, project_root)
        decision = build_decision(axes)
    except Exception as exc:
        log_error("quality-gate-failed", str(exc), {"manifest_path": manifest_path})
        return None, None, None

    findings_payload = {
        "axes": [
            {
                "name": a.name,
                "state": a.state.value,
                "findings": list(a.findings),
                "raw": a.raw,
            }
            for a in decision.axes
        ],
        "options": list(decision.options),
    }
    summary = decision.summary if decision.state != GateState.AUTO else None
    return decision.state.value, json.dumps(findings_payload), summary


def finalize_pending(
    pending: PendingRun,
    *,
    run_state_dir: Path,
    project_root: Path,
    response_text: str,
    outcome: str,
    failure_reason: str | None,
    deliverable_path: str | None,
    transcript_path: str | None,
    session_id: str,
) -> str | None:
    """Complete a pending Run and write it to the Ledger.

    Shared by PostToolUse (synchronous runtimes) and SubagentStop (background
    runtimes) so both produce identical Run records. Idempotent at the call
    site: callers invoke it only when a pending sidecar was found, and it
    deletes the sidecar at the end, so a second finalize finds no pending.
    Active-dispatch markers are cleared by run_id across both session and
    agentId indexes, so a completed run cannot keep writes authorized.
    Returns the quality-gate summary to surface, or None.
    """
    ledger = Ledger(
        db_path=db_path_for_state(run_state_dir),
        jsonl_path=jsonl_path_for_state(run_state_dir),
    )
    ledger.initialize(migration_sql())

    tokens_out = estimate_tokens(response_text)
    ended_ts = datetime.now(timezone.utc).isoformat()

    quality_state, quality_findings_json, quality_summary = (None, None, None)
    if outcome == "ok":
        quality_state, quality_findings_json, quality_summary = _run_quality_gate(
            pending.manifest_path, project_root
        )

    harvest = None
    resolved_transcript_path = (
        Path(transcript_path)
        if transcript_path
        else transcript_path_for_session(session_id, cwd=Path.cwd())
    )
    if resolved_transcript_path:
        try:
            harvest = walk_transcript(
                resolved_transcript_path,
                after_ts=pending.started_ts,
                before_ts=ended_ts,
                dispatch_task_id=pending.task_id,
                dispatch_role_id=pending.role_id,
                dispatch_prompt_sha256=(pending.extra or {}).get("tool_prompt_sha256"),
                dispatch_started_ts=pending.started_ts,
            )
        except Exception as exc:
            log_error(
                "post-task-transcript-walk-failed",
                str(exc),
                {"run_id": pending.run_id, "transcript_path": str(resolved_transcript_path)},
            )
            harvest = None

    event_records = read_file_events(
        run_state_dir,
        run_id=pending.run_id,
        session_id=pending.session_id,
        started_ts=pending.started_ts,
        ended_ts=ended_ts,
    )

    touched: set[str] = set()
    tool_counts: dict[str, int] = {}
    tool_input_tokens: dict[str, int] = {}
    files_touched_json: str | None = None
    tool_use_json: str | None = None
    cache_read = 0
    cache_create = 0
    runtime_model = None
    model_source = "tier_fallback"
    if harvest is not None:
        if harvest.files_touched:
            touched.update(harvest.files_touched)
        if harvest.tool_use:
            for t in harvest.tool_use:
                tool_counts[t.name] = tool_counts.get(t.name, 0) + t.count
                tool_input_tokens[t.name] = (
                    tool_input_tokens.get(t.name, 0) + t.total_input_tokens
                )
        cache_read = harvest.cache_read_tokens
        cache_create = harvest.cache_create_tokens
        runtime_model = normalize_model_identity(dominant_model(harvest))
        if runtime_model:
            model_source = "transcript"

    if runtime_model is None:
        native = load_native_rate_limits()
        if (
            native is not None
            and native.is_fresh
            and native.model
            and native.session_id
            and native.session_id in {pending.session_id, session_id}
        ):
            runtime_model = normalize_model_identity(native.model)
            model_source = "statusline"

    # Avoid double-counting tools already captured from the transcript harvest;
    # file events still always contribute to files_touched.
    harvest_has_tools = harvest is not None and bool(harvest.tool_use)
    for event in event_records:
        touched.add(event.rel_path or event.path)
        if not harvest_has_tools:
            tool_counts[event.tool_name] = tool_counts.get(event.tool_name, 0) + 1

    # Defense-in-depth: only real artifacts are provenance. Drop any path that
    # does not exist at finalization (a denied/attempted write the transcript
    # may still list, or a path parsed from prose), and never record a missing
    # file as a deliverable. The gap is logged as a diagnostic, not a deliverable.
    def _output_exists(pth: str) -> bool:
        candidate = Path(pth)
        return (
            candidate.exists() if candidate.is_absolute()
            else (project_root / pth).exists()
        )
    missing_outputs = sorted(p for p in touched if not _output_exists(p))
    touched = {p for p in touched if _output_exists(p)}
    if deliverable_path and not _output_exists(deliverable_path):
        deliverable_path = None
    if missing_outputs:
        log_error(
            "finalize-missing-outputs",
            "attempted outputs not found on disk; excluded from provenance",
            {"run_id": pending.run_id, "missing": missing_outputs[:10]},
        )

    if touched:
        files_touched_json = json.dumps(sorted(touched))
    if tool_counts:
        tool_use_json = json.dumps([
            {"name": name, "count": count, "input_tokens": tool_input_tokens.get(name, 0)}
            for name, count in sorted(tool_counts.items())
        ])

    recorded_model = (
        normalize_model_identity(runtime_model)
        or normalize_model_identity(pending.model_tier)
        or "unknown"
    )
    role = ledger.get_role(pending.role_id)
    role_version = role.version if role else None

    deliverable_ref = build_deliverable_ref(
        response_text,
        deliverable_path=deliverable_path,
        files_touched=tuple(sorted(touched)),
    )
    deliverable_ref_json = json.dumps(deliverable_ref) if deliverable_ref else None
    review_findings_json = quality_findings_json or None

    run_record = Run(
        run_id=pending.run_id,
        executor_id=pending.executor_id,
        task_id=pending.task_id,
        model=recorded_model,
        tokens_in=pending.estimated_tokens_in,
        tokens_out=tokens_out,
        window_id=pending.window_id,
        started_ts=pending.started_ts,
        ended_ts=ended_ts,
        outcome=outcome,
        brief_in_path=pending.brief_in_path,
        brief_out_path=None,
        deliverable_path=deliverable_path,
        failure_reason=failure_reason,
        quality_state=quality_state,
        quality_findings_json=quality_findings_json,
        role_version=role_version,
        requested_model_tier=pending.model_tier,
        model_source=model_source,
        deliverable_ref_json=deliverable_ref_json,
        files_touched_json=files_touched_json,
        tool_use_json=tool_use_json,
        review_findings_json=review_findings_json,
        cache_read_tokens=cache_read,
        cache_create_tokens=cache_create,
    )
    ledger.append_run(run_record)

    existing = ledger.get_executor(pending.executor_id)
    if existing:
        ledger.upsert_executor(
            Executor(
                executor_id=existing.executor_id,
                alias=existing.alias,
                role_id=existing.role_id,
                parent_executor_id=existing.parent_executor_id,
                created_ts=existing.created_ts,
                last_seen_ts=ended_ts,
                status=existing.status,
            )
        )
    if outcome in {"ok", "partial"}:
        ledger.update_task_status(pending.task_id, "done")

    ledger.close()
    delete_pending(run_state_dir, pending.run_id)
    # Clear EVERY marker pointing at this run (session- and agentId-keyed), so a
    # completed Run can never keep a coordinator/direct write authorized.
    clear_active_dispatch_for_run(pending.run_id, session_id=pending.session_id)
    clear_file_events(run_state_dir, run_id=pending.run_id, events=event_records)
    return quality_summary


def run() -> int:
    payload = read_input()
    if payload.get("tool_name") not in ("Task", "Agent"):
        emit({"continue": True})
        return 0
    tool_response = payload.get("tool_response") or payload.get("tool_output")

    # --- Background Agent path --------------------------------------------
    # The Agent tool returns a launch stub carrying the runtime agentId before
    # the subagent has done anything. Reconcile it to the pre_task stamp by
    # tool_use_id, record an agentId-keyed active dispatch (so the subagent's
    # writes are authorized and SubagentStop can finalize by agent_id), and
    # DEFER finalization. agentId is absent on synchronous runtimes.
    agent_id = tool_response.get("agentId") if isinstance(tool_response, dict) else None
    if payload.get("tool_name") == "Agent" and agent_id:
        tool_use_id = payload.get("tool_use_id")
        bg_session = payload.get("session_id") or ""
        bg_state_dir = state_dir_for_payload(payload)
        bg_root = project_root_from_payload(payload) or Path.cwd().resolve(strict=False)
        # Cross-project dispatches: the launch payload may lack the original
        # [project:] context, so consult the session marker pre_task wrote
        # (carries the dispatch's true project_root/state_dir) first.
        bg_active = read_active_dispatch(bg_session) if bg_session else None
        if bg_active is not None and bg_state_dir == state_dir():
            bg_state_dir = Path(bg_active.state_dir)
            bg_root = Path(bg_active.project_root)
        pending = pending_by_tool_use_id(bg_state_dir, tool_use_id) if tool_use_id else None
        if pending is not None:
            try:
                write_active_dispatch_by_agent(
                    ActiveDispatch(
                        session_id=payload.get("session_id") or "",
                        run_id=pending.run_id,
                        project_root=str(bg_root.resolve(strict=False)),
                        state_dir=str(bg_state_dir.resolve(strict=False)),
                        started_ts=pending.started_ts,
                        agent_id=agent_id,
                        tool_use_id=tool_use_id,
                    )
                )
            except Exception as exc:
                log_error(
                    "post-task-agent-index-failed",
                    str(exc),
                    {"run_id": pending.run_id, "agent_id": agent_id},
                )
        else:
            log_error(
                "post-task-no-pending-for-tooluse",
                "background launch with no matching pending sidecar",
                {"tool_use_id": tool_use_id, "agent_id": agent_id},
            )
        emit({"continue": True})
        return 0

    # --- Synchronous path -------------------------------------------------
    session_id = payload.get("session_id") or "unknown-session"
    transcript_path = payload.get("transcript_path")
    run_state_dir = state_dir_for_payload(payload)
    project_root = project_root_from_payload(payload) or Path.cwd().resolve(strict=False)
    active = read_active_dispatch(session_id)
    if active is not None and run_state_dir == state_dir():
        run_state_dir = Path(active.state_dir)
        project_root = Path(active.project_root)

    pending = latest_pending_for_session(run_state_dir, session_id)
    if pending is None:
        log_error(
            "post-task-no-pending",
            "no pending-Run sidecar matched this session",
            {"session_id": session_id},
        )
        emit({"continue": True})
        return 0

    try:
        response_text = coerce_to_text(tool_response)
        outcome = classify_outcome(tool_response)
        failure_reason = (
            classify_failure_reason(tool_response) if outcome == "failed" else None
        )
        deliverable_path = extract_deliverable_path(tool_response)
        quality_summary = finalize_pending(
            pending,
            run_state_dir=run_state_dir,
            project_root=project_root,
            response_text=response_text,
            outcome=outcome,
            failure_reason=failure_reason,
            deliverable_path=deliverable_path,
            transcript_path=transcript_path,
            session_id=session_id,
        )
    except Exception as exc:
        log_error("post-task-failed", str(exc), {"run_id": pending.run_id})
        emit({"continue": True})
        return 0

    if quality_summary:
        emit({"continue": True, "systemMessage": quality_summary})
    else:
        emit({"continue": True})
    return 0


if __name__ == "__main__":
    sys.exit(run())
