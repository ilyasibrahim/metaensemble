#!/usr/bin/env python3
"""PreToolUse hook for Task invocations.

The PreToolUse hook is the gatekeeper for every Task tool invocation. Its
responsibilities (per ARCHITECTURE.md §8):

1. Derive identity. From the agent-runtime payload (`tool_input`,
   `session_id`, `cwd`), determine which Role is being dispatched, which
   Executor owns the work, and which Task it belongs to.
2. Materialize the rows that downstream queries depend on (`roles`,
   `executors`, `tasks`), all idempotent.
3. Validate the Manifest if one is referenced via the `[manifest: hm-...]`
   prompt marker.
4. Estimate token cost from the prompt text and run the cost gate.
5. Stamp a pending-Run sidecar on disk so PostToolUse can complete the
   record.
6. Either allow the Task (exit 0) or block it (exit 2 with structured
   reason payload).

Hook contract:
- Stdin: agent runtime payload
- Stdout: JSON with `continue`, optional `stopReason`, `systemMessage`
- Exit: 0 (allow / notify), 2 (block)
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from metaensemble.hooks._common import (  # noqa: E402
    current_window_id,
    db_path_for_state,
    emit,
    jsonl_path_for_state,
    log_error,
    migration_sql,
    project_root_from_payload,
    read_input,
    state_dir_for_payload,
)
from metaensemble.lib.config import effective_capacity_tokens, load_budget_config  # noqa: E402
from metaensemble.lib.cost_gate import (  # noqa: E402
    CostGateDecision,
    GateState,
    evaluate,
    is_action_irreversible,
)
from metaensemble.lib.file_events import ActiveDispatch, write_active_dispatch  # noqa: E402
from metaensemble.lib.ids import uuid7  # noqa: E402
from metaensemble.lib.ledger import Ledger  # noqa: E402
from metaensemble.lib.manifest import load_manifest  # noqa: E402
from metaensemble.lib.recording import (  # noqa: E402
    ensure_executor,
    ensure_role,
    ensure_task,
    estimate_tokens,
    manifest_path_for,
    parse_markers,
)
from metaensemble.lib.sidecar import PendingRun, write_pending  # noqa: E402
from metaensemble.lib.transcript import prompt_fingerprint  # noqa: E402


def _explain_manifest_failure(exc: Exception, manifest_id: str, path: Path) -> str:
    """Render a Coordinator-actionable error message from a validation failure.

    YAML parser/scanner errors carry a `problem_mark` with a line and column.
    JSON-Schema validation errors carry a `path` and a `message`. We surface
    whichever the underlying exception provides so the Coordinator can fix
    the specific offending line rather than guessing at the source of failure.
    """
    base = f"Manifest `{manifest_id}` failed validation at `{path}`."

    # YAML parser/scanner errors.
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None)
    if mark is not None and problem:
        line = getattr(mark, "line", None)
        column = getattr(mark, "column", None)
        loc = (
            f" at line {line + 1}, column {column + 1}"
            if line is not None and column is not None
            else ""
        )
        return (
            f"{base}\n"
            f"  YAML parser: {problem}{loc}.\n"
            "  Common cause: a string containing `:`, `→`, `#`, or a quote was "
            "left unquoted. Wrap the value in double quotes and retry."
        )

    # jsonschema.ValidationError carries `.message` and `.absolute_path`.
    message = getattr(exc, "message", None)
    abs_path = getattr(exc, "absolute_path", None)
    if message is not None:
        field_loc = ""
        if abs_path is not None:
            try:
                parts = list(abs_path)
                if parts:
                    field_loc = f" at field `{'.'.join(str(p) for p in parts)}`"
            except TypeError:
                pass
        return (
            f"{base}\n"
            f"  Schema: {message}{field_loc}.\n"
            "  See the Manifest authoring rules in metaensemble-protocol/SKILL.md. "
            "Non-contract fields belong under `extras` or in the Deliverable, not "
            "as top-level Manifest properties."
        )

    return f"{base}\n  {type(exc).__name__}: {exc}"


def _remaining_window(
    ledger: Ledger, window_id: str, capacity_tokens: int,
) -> tuple[int, int, dict]:
    """Compute remaining window tokens from BOTH the Ledger AND the runtime.

    Returns (remaining_tokens, used_tokens, breakdown). The breakdown
    surfaces where the burn came from — useful for the standup digest
    and for the doctor's drift check.

    Truth comes from the runtime's session jsonls (cross-session,
    captures the main agent's reads/edits and other sessions in the
    same 5-hour bucket). The Ledger is consulted as a fallback when the
    runtime data is unavailable, and as a comparator the doctor uses to
    detect drift.
    """
    # Local import keeps the hook startup fast when runtime_state isn't
    # needed (e.g., for non-Task tool invocations that short-circuit).
    from metaensemble.lib.runtime_state import get_window_burn as _runtime_burn

    ledger_burn = ledger.get_window_burn(window_id)
    ledger_used = ledger_burn.total_tokens_in + ledger_burn.total_tokens_out

    runtime_burn = _runtime_burn(window_id=window_id)
    runtime_used = runtime_burn.input_tokens + runtime_burn.output_tokens

    # When runtime data exists, it is authoritative. Otherwise fall back
    # to Ledger-only.
    used = max(runtime_used, ledger_used)
    remaining = max(0, capacity_tokens - used)
    breakdown = {
        "ledger_used": ledger_used,
        "runtime_used": runtime_used,
        "runtime_cache_read": runtime_burn.cache_read_tokens,
        "runtime_cache_create": runtime_burn.cache_creation_tokens,
        "runtime_message_count": runtime_burn.message_count,
    }
    return remaining, used, breakdown


# The same three structured options apply on both NOTIFY and BLOCK so the
# Principal never has to invent the right intervention under time pressure.
# What differs between the two states is the *default* action: NOTIFY
# proceeds in a moment unless intercepted, BLOCK pauses outright until the
# Principal chooses.
_DECISION_OPTIONS: tuple[dict, ...] = (
    {"id": 1, "label": "Approve and proceed at current tier"},
    {"id": 2, "label": "Drop the model tier and retry (haiku/sonnet)"},
    {"id": 3, "label": "Split the Task into smaller Manifests"},
)


def _format_decision_surface(
    decision: CostGateDecision, est_tokens: int, *, blocked: bool
) -> str:
    """Render the Principal-facing decision surface for NOTIFY or BLOCK.

    The diagnosis and option set are the same in both states. The
    header marks block vs notify so the Coordinator can route the
    message correctly, and the closing line states the default action
    so the Principal knows what happens if they take no action.
    """
    header = (
        "## MetaEnsemble cost gate — block"
        if blocked
        else "## MetaEnsemble cost gate — notify"
    )
    default_line = (
        "Default: paused. Choose an option to proceed."
        if blocked
        else "Default: proceed in a moment. Choose an option to intercept."
    )
    lines = [
        header,
        f"Reason: {decision.reason}",
        f"Estimated tokens: {est_tokens} ({decision.estimated_pct_of_window:.1f}% of window capacity)",
    ]
    # When the BLOCK fired because the Manifest pattern is novel, the
    # Principal needs to know that approving is normal first-occurrence
    # behavior, not an override of a real safety concern. The hint also
    # names the de-escalation path so the gate stops feeling adversarial
    # on the second and third dispatches of the same pattern.
    if "novel" in (decision.reason or "").lower():
        lines.extend([
            "",
            "Hint: first occurrence of this Manifest pattern. Pick option 1 to "
            "approve. The gate de-escalates to NOTIFY after 2 successful runs "
            "of the pattern, and to AUTO after 3.",
        ])
    lines.extend([
        "",
        "Options:",
    ])
    for opt in _DECISION_OPTIONS:
        lines.append(f"  {opt['id']}. {opt['label']}")
    lines.extend(["", default_line])
    return "\n".join(lines)


def _persist_decision_sentinel(
    session_id: str,
    task_id: str | None,
    manifest_id: str | None,
    decision: CostGateDecision,
    est_tokens: int,
    run_state_dir: Path,
    *,
    blocked: bool,
) -> None:
    """Write the structured decision surface to a sentinel file.

    The agent runtime renders a hook's exit-2 return as a generic
    "hook error" without surfacing the hook's stdout to the dispatching
    agent, and even for NOTIFY the Coordinator may want machine-readable
    options to surface to the Principal in a richer form than a single
    `systemMessage` line. Writing to `<state>/blocks/` or
    `<state>/notifies/` gives the Coordinator a stable place to query
    after a gated dispatch.
    """
    import json as _json
    bucket = "blocks" if blocked else "notifies"
    out_dir = run_state_dir / bucket
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "task_id": task_id,
            "manifest_id": manifest_id,
            "state": "block" if blocked else "notify",
            "reason": decision.reason,
            "estimated_tokens": est_tokens,
            "estimated_pct_of_window": decision.estimated_pct_of_window,
            "options": list(_DECISION_OPTIONS),
            "default": "paused" if blocked else "proceed",
        }
        sentinel = out_dir / f"{session_id}-{datetime.now(timezone.utc).strftime('%H%M%S%f')}.json"
        sentinel.write_text(_json.dumps(record, indent=2))
    except Exception:  # nosec B110
        # Never block the gate on the sentinel write.
        pass


def _resolve_manifest(
    ledger: Ledger, manifest_id: str | None, session_id: str, run_state_dir: Path
) -> tuple[str | None, dict | None, str, int | None, int | None]:
    """Resolve and validate the Manifest referenced by the dispatch prompt.

    Returns `(manifest_path, manifest_data, task_type, budget_from_manifest,
    early_exit_code)`. `early_exit_code` is non-None when the caller should
    return immediately — it carries the exit code to return after the hook
    has emitted the appropriate stopReason payload. The caller is
    responsible for emitting before returning.
    """
    if not manifest_id:
        return None, None, "task", None, None

    resolved = manifest_path_for(run_state_dir, manifest_id)
    if resolved is None:
        log_error(
            "manifest-not-found",
            f"prompt referenced manifest_id {manifest_id} but no file matched",
            {"manifest_id": manifest_id, "session_id": session_id},
        )
        return None, None, "task", None, None

    try:
        manifest_data = load_manifest(resolved)
    except Exception as exc:
        log_error(
            "manifest-validation-failed",
            str(exc),
            {"manifest_id": manifest_id, "path": str(resolved)},
        )
        reason = _explain_manifest_failure(exc, manifest_id, resolved)
        emit({"continue": False, "stopReason": reason})
        return None, None, "task", None, 2

    task_type = manifest_data.get("task", "task")
    constraints = manifest_data.get("constraints") or {}
    budget_value = constraints.get("window_budget")
    budget_from_manifest = (
        budget_value if isinstance(budget_value, int) and budget_value > 0 else None
    )
    return str(resolved), manifest_data, task_type, budget_from_manifest, None


def _count_pattern_runs(ledger: Ledger, role_id: str, task_type: str) -> int:
    """Count Runs that match this (role_id, task_type) pattern.

    Used by the novelty check. A pattern is novel when the count is below
    the configured drop-to-auto threshold. Indexed read on
    `idx_runs_executor` (covers role lookup via Executor) and the tasks
    join is bounded by the limit.
    """
    return ledger.count_pattern_runs(role_id, task_type)


def _emit_decision_result(
    decision: CostGateDecision,
    session_id: str,
    task_id: str | None,
    manifest_id: str | None,
    estimated_tokens: int,
    run_state_dir: Path,
) -> int:
    """Emit the hook's stdout payload for the cost-gate decision and return
    the exit code. AUTO proceeds silently; NOTIFY proceeds with options
    surfaced; BLOCK pauses with the same options surfaced."""
    if decision.state == GateState.AUTO:
        emit({"continue": True})
        return 0
    blocked = decision.state == GateState.BLOCK
    _persist_decision_sentinel(
        session_id, task_id, manifest_id, decision, estimated_tokens,
        run_state_dir,
        blocked=blocked,
    )
    payload = {
        "continue": not blocked,
        ("stopReason" if blocked else "systemMessage"): _format_decision_surface(
            decision, estimated_tokens, blocked=blocked,
        ),
    }
    emit(payload)
    return 2 if blocked else 0


def run() -> int:
    payload = read_input()
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}

    # Only gate subagent dispatches. The runtime has been called this tool
    # both `Task` (older) and `Agent` (current); accept either name. Every
    # other tool passes through cleanly.
    if tool_name not in ("Task", "Agent"):
        emit({"continue": True})
        return 0

    session_id = payload.get("session_id") or "unknown-session"

    # Identity derivation.
    subagent_type = tool_input.get("subagent_type") or "general-purpose"
    prompt = tool_input.get("prompt") or ""

    markers = parse_markers(prompt)
    run_state_dir = state_dir_for_payload(payload)
    project_root = (
        project_root_from_payload(payload) or Path.cwd().resolve(strict=False)
    )
    manifest_id = markers.get("manifest_id")
    continuing_alias = markers.get("continuing_alias")
    explicit_task_id = markers.get("task_id")

    # Protocol guard: multi-instance patterns (`--fanout N`, `--consensus N`)
    # are only meaningful for N >= 2. The Coordinator surfaces the requested
    # N via `[fanout: N]` / `[consensus: N]` markers in each Executor's
    # prompt; we reject the dispatch deterministically before any model work
    # happens. This is the enforceable line — slash-command files are read
    # by a model and cannot themselves prevent execution.
    for marker_name in ("fanout", "consensus"):
        raw = markers.get(marker_name)
        if raw is None:
            continue
        try:
            n = int(raw)
        except ValueError:
            n = -1
        if n < 2:
            stop_reason = (
                f"MetaEnsemble protocol: --{marker_name} requires N >= 2 "
                f"(got {raw}). The dispatch is rejected before any Executor "
                "work happens."
            )
            emit({"continue": False, "stopReason": stop_reason})
            return 2

    try:
        ledger = Ledger(
            db_path=db_path_for_state(run_state_dir),
            jsonl_path=jsonl_path_for_state(run_state_dir),
        )
        ledger.initialize(migration_sql())

        manifest_path, manifest_data, task_type, budget_from_manifest, early = (
            _resolve_manifest(ledger, manifest_id, session_id, run_state_dir)
        )
        if early is not None:
            return early

        # Role + Executor materialization.
        ensure_role(ledger, subagent_type)
        executor_result = ensure_executor(
            ledger,
            role_id=subagent_type,
            continuing_alias=continuing_alias,
            force_fresh=bool(markers.get("fresh")),
        )
        executor = executor_result.executor

        # Task identity (the row itself is materialized below, only when the
        # cost gate does not BLOCK — a blocked dispatch never ran and should
        # not leave an orphan `in_progress` row in the Ledger).
        task_id = explicit_task_id or f"task-{uuid7().hex[:12]}"

        # Cost gate.
        estimated_tokens_in = (
            budget_from_manifest
            if budget_from_manifest is not None
            else estimate_tokens(prompt)
        )
        config = load_budget_config()
        window_id = current_window_id()
        # Auto-calibrated capacity from observed historical peak. The
        # manual `window_capacity_tokens` is the floor; the runtime peak
        # raises the ceiling for users on larger plans.
        capacity = effective_capacity_tokens(config)
        remaining, used, _breakdown = _remaining_window(ledger, window_id, capacity)
        irreversible = is_action_irreversible(tool_name, tool_input, config.irreversible_actions)

        # Novelty applies only when the Coordinator referenced a Manifest. A
        # Manifest signature is what "pattern" means in the cost-gate sense;
        # auto-discovered Tasks without a Manifest skip the novelty check so
        # ordinary Claude Code subagent dispatches don't trip the block.
        if manifest_data is not None:
            pattern_runs = _count_pattern_runs(ledger, subagent_type, task_type)
            is_novel = (
                config.novelty_block_first_run
                and pattern_runs < config.novelty_drop_to_auto_after
            )
        else:
            is_novel = False

        decision = evaluate(
            estimated_tokens=estimated_tokens_in,
            remaining_window_tokens=remaining,
            is_irreversible=irreversible,
            is_novel_pattern=is_novel,
            config=config,
            window_capacity_tokens=capacity,
            used_window_tokens=used,
        )

        # Sidecar written only when proceeding (auto or notify). If the gate
        # blocks, PostToolUse never fires, so there is nothing to complete.
        pending = PendingRun(
            run_id=str(uuid7()),
            session_id=session_id,
            executor_id=executor.executor_id,
            task_id=task_id,
            role_id=subagent_type,
            model_tier=(
                (manifest_data or {}).get("constraints", {}).get("model_tier", "sonnet")
                if manifest_data
                else "sonnet"
            ),
            started_ts=datetime.now(timezone.utc).isoformat(),
            window_id=window_id,
            estimated_tokens_in=estimated_tokens_in,
            manifest_id=manifest_id,
            manifest_path=manifest_path,
            extra={
                "transcript_path": payload.get("transcript_path"),
                "tool_prompt_sha256": prompt_fingerprint(prompt),
                # Links this stamp to the PostToolUse(Agent) payload that
                # first exposes the runtime agentId correlation key.
                "tool_use_id": payload.get("tool_use_id"),
            },
        )
        if decision.state != GateState.BLOCK:
            # Materialize the task row now that the dispatch is proceeding.
            ensure_task(ledger, task_id, task_type, manifest_path)
            write_pending(run_state_dir, pending)
            try:
                write_active_dispatch(
                    ActiveDispatch(
                        session_id=session_id,
                        run_id=pending.run_id,
                        project_root=str(project_root.resolve(strict=False)),
                        state_dir=str(run_state_dir.resolve(strict=False)),
                        started_ts=pending.started_ts,
                    )
                )
            except Exception as exc:
                log_error(
                    "active-dispatch-write-failed",
                    str(exc),
                    {"session_id": session_id, "run_id": pending.run_id},
                )

        ledger.close()
    except Exception as exc:
        log_error("pre-task-evaluation-failed", str(exc))
        emit({"continue": True, "systemMessage": "(cost gate unavailable; proceeding)"})
        return 0

    return _emit_decision_result(
        decision, session_id, task_id, manifest_id, estimated_tokens_in, run_state_dir,
    )


if __name__ == "__main__":
    sys.exit(run())
