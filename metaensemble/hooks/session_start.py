#!/usr/bin/env python3
"""SessionStart hook.

Per ARCHITECTURE.md §8: loads a brief Registry summary into the Coordinator
context, injects the current window status, and verifies state DB integrity.

Stdin: the agent runtime's session-start payload (JSON; we ignore its specifics).
Stdout: a JSON payload containing a `systemMessage` string to inject as context.
Exit: 0 always (this hook never blocks).
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow running as a script from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from metaensemble.hooks._common import (  # noqa: E402
    current_window_id,
    db_path,
    emit,
    jsonl_path,
    log_error,
    migration_sql,
    read_input,
    state_dir,
)
from metaensemble.lib.config import effective_capacity_tokens, load_budget_config  # noqa: E402
from metaensemble.lib.ledger import Ledger  # noqa: E402
from metaensemble.lib.native_state import (  # noqa: E402
    SevenDayLine,
    WindowReport,
    format_age,
    format_duration,
    load_native_rate_limits,
    resolve_seven_day,
    resolve_window_report,
)
from metaensemble.lib.reconcile import reconcile_stale_pending  # noqa: E402
from metaensemble.lib.runtime_state import (  # noqa: E402
    get_session_burn,
    get_window_burn as get_runtime_burn,
)


SUMMARY_HEADER = "## MetaEnsemble session start"


def _format_five_hour_line(report: WindowReport, five_h_window) -> str:
    """Render the 5-hour window line, branching on `report.kind`.

    `five_h_window` is `native.five_hour` (passed so the `live_plan` arm
    can append the time-until-reset suffix; the other arms do not use it).
    """
    if report.kind == "live_plan":
        suffix = ""
        if five_h_window is not None:
            remaining = format_duration(five_h_window.time_until_reset())
            if remaining:
                suffix = f" ({remaining} left)"
        return f"- 5-hour window: {report.pct_used:.0f}% of plan used{suffix}"
    if report.kind == "last_observed_plan":
        age_str = format_age(report.snapshot_age_seconds)
        return (
            f"- 5-hour window: live usage unavailable; last runtime "
            f"snapshot was {report.last_observed_pct:.0f}%, "
            f"{age_str} old, from a previous window"
        )
    if report.kind == "project_fallback":
        return (
            f"- 5-hour window (project burn): {report.pct_used:.0f}% of "
            f"{report.capacity_tokens:,} fallback capacity — "
            "plan-wide usage unavailable"
        )
    # kind == "unavailable"
    return "- 5-hour window: plan usage unavailable until statusline refreshes"


def _format_seven_day_line(seven_line: SevenDayLine, seven_d_window) -> str | None:
    """Render the 7-day window line, or return None to omit it.

    `seven_d_window` is `native.seven_day` (used only for the
    time-until-reset suffix on `live_plan`).
    """
    if seven_line.kind == "unavailable":
        return None
    if seven_line.kind == "live_plan":
        suffix = ""
        if seven_d_window is not None:
            remaining = format_duration(seven_d_window.time_until_reset())
            if remaining:
                suffix = f" ({remaining} left)"
        return f"- 7-day window: {seven_line.pct_used:.0f}% of plan used{suffix}"
    # kind == "last_observed_plan"
    age_str = format_age(seven_line.snapshot_age_seconds)
    return (
        f"- 7-day window: live usage unavailable; last runtime "
        f"snapshot was {seven_line.last_observed_pct:.0f}%, {age_str} old"
    )


def _format_summary(
    *,
    window_id: str,
    recent_runs: int,
    active_executors: int,
    reconciled_count: int,
    report: WindowReport,
    seven_line: SevenDayLine,
    native,                       # NativeRateLimits | None
    session_main_tokens: int,
    session_me_tokens: int,
) -> str:
    five_h = native.five_hour if native is not None else None
    seven_d = native.seven_day if native is not None else None

    lines = [
        f"{SUMMARY_HEADER}",
        f"- Current window: `{window_id}`",
        _format_five_hour_line(report, five_h),
    ]
    seven_line_str = _format_seven_day_line(seven_line, seven_d)
    if seven_line_str:
        lines.append(seven_line_str)
    session_total = session_main_tokens + session_me_tokens
    lines.append(
        f"- This session so far: {session_total:,} tokens "
        f"(main agent: {session_main_tokens:,}, MetaEnsemble: {session_me_tokens:,})"
    )
    lines.append(f"- Runs in last 24h: {recent_runs}")
    lines.append(f"- Active Executors (last 7 days): {active_executors}")
    if report.note:
        lines.append(f"- Note: {report.note}")
    if reconciled_count:
        lines.append(
            f"- Reconciled {reconciled_count} stale pending Run(s) from prior sessions"
        )
    return "\n".join(lines) + "\n"


def run() -> int:
    payload = read_input() or {}
    session_id = payload.get("session_id") or ""

    try:
        ledger = Ledger(db_path=db_path(), jsonl_path=jsonl_path())
        # Idempotent: if the migration already ran, this is a no-op.
        ledger.initialize(migration_sql())

        # Reconcile sidecars older than 1 hour. Catches the `kill -9` and
        # budget-exhaustion cases where the previous session's PostToolUse
        # never fired. Cheap (O(N) over pending/) and safe (idempotent),
        # so it runs on every session start.
        reconciled = reconcile_stale_pending(
            ledger,
            state_dir(),
            max_age=timedelta(hours=1),
        )

        window_id = current_window_id()
        ledger_burn = ledger.get_window_burn(window_id)
        ledger_used = ledger_burn.total_tokens_in + ledger_burn.total_tokens_out
        runtime_burn = get_runtime_burn(window_id=window_id)
        runtime_used = runtime_burn.input_tokens + runtime_burn.output_tokens
        used = max(runtime_used, ledger_used)

        config = load_budget_config()
        capacity = effective_capacity_tokens(config)
        native = load_native_rate_limits()
        report = resolve_window_report(used, capacity, native)
        seven_line = resolve_seven_day(native)

        # Window-scoped: only count tokens already burned in the current
        # 5h bucket. A truly-fresh session reads zero; a resumed session
        # whose prior turns are in this same bucket reads its prior burn.
        session_burn = (
            get_session_burn(session_id, window_id=window_id) if session_id else None
        )
        session_main = (
            session_burn.input_tokens + session_burn.output_tokens
            if session_burn else 0
        )

        since_24h = datetime.now(timezone.utc) - timedelta(hours=24)
        since_7d = datetime.now(timezone.utc) - timedelta(days=7)
        recent = ledger.get_recent_runs(limit=200, since=since_24h)
        active = ledger.get_active_executors(since=since_7d, limit=200)

        summary = _format_summary(
            window_id=window_id,
            recent_runs=len(recent),
            active_executors=len(active),
            reconciled_count=len(reconciled),
            report=report,
            seven_line=seven_line,
            native=native,
            session_main_tokens=session_main,
            session_me_tokens=0,  # Fresh session: no MetaEnsemble runs yet.
        )
        emit({"continue": True, "systemMessage": summary})
        ledger.close()
        return 0
    except Exception as exc:
        log_error("session-start-failed", str(exc))
        emit({"continue": True, "systemMessage": f"{SUMMARY_HEADER}\n(state unavailable)\n"})
        return 0


if __name__ == "__main__":
    sys.exit(run())
