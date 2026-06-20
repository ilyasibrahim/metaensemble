#!/usr/bin/env python3
"""SubagentStop hook: finalize a background dispatched Run, keyed by agentId.

Background Agent dispatches return from `PostToolUse(Agent)` at launch time,
before the subagent has produced a result. `SubagentStop` fires when the
subagent actually stops, carrying `agent_id`, `agent_transcript_path` (the
subagent's own transcript), and `last_assistant_message` (its final output).

We correlate strictly by `agent_id`, the per-dispatch key, not by session, so
concurrent / fan-out dispatches in one session finalize independently. The hook
no-ops when no agentId-keyed active dispatch matches (already finalized, or a
synchronous runtime finalized in PostToolUse). It never blocks.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from metaensemble.hooks._common import emit, log_error, read_input  # noqa: E402
from metaensemble.hooks.post_task import finalize_pending  # noqa: E402
from metaensemble.lib.file_events import (  # noqa: E402
    clear_active_dispatch_by_agent,
    read_active_dispatch_by_agent,
)
from metaensemble.lib.recording import (  # noqa: E402
    classify_failure_reason,
    classify_outcome,
    coerce_to_text,
    extract_deliverable_path,
)
from metaensemble.lib.sidecar import read_pending  # noqa: E402


def run() -> int:
    payload = read_input()
    agent_id = payload.get("agent_id")
    if not agent_id:
        emit({"continue": True})
        return 0

    active = read_active_dispatch_by_agent(agent_id)
    if active is None:
        # No deferred dispatch for this agent (already finalized, or sync).
        emit({"continue": True})
        return 0

    run_state_dir = Path(active.state_dir)
    project_root = Path(active.project_root)
    pending = read_pending(run_state_dir, active.run_id)
    if pending is None:
        # Run already finalized elsewhere — drop the stale agent marker.
        clear_active_dispatch_by_agent(agent_id)
        emit({"continue": True})
        return 0

    last_message = payload.get("last_assistant_message")
    transcript_path = (
        payload.get("agent_transcript_path") or payload.get("transcript_path")
    )
    try:
        response_text = coerce_to_text(last_message)
        outcome = classify_outcome(last_message) if last_message is not None else "ok"
        failure_reason = (
            classify_failure_reason(last_message) if outcome == "failed" else None
        )
        deliverable_path = extract_deliverable_path(last_message)
        finalize_pending(
            pending,
            run_state_dir=run_state_dir,
            project_root=project_root,
            response_text=response_text,
            outcome=outcome,
            failure_reason=failure_reason,
            deliverable_path=deliverable_path,
            transcript_path=transcript_path,
            session_id=active.session_id or (payload.get("session_id") or ""),
        )
    except Exception as exc:
        log_error(
            "subagent-stop-failed",
            str(exc),
            {"run_id": pending.run_id, "agent_id": agent_id},
        )
        emit({"continue": True})
        return 0

    emit({"continue": True})
    return 0


if __name__ == "__main__":
    sys.exit(run())
