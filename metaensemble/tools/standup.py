#!/usr/bin/env python3
"""Daily standup digest: window status, active Executors, recent Runs.

Invoked by the `/standup` slash command.
"""
from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from metaensemble.hooks._common import (  # noqa: E402
    current_window_id,
    db_path,
    jsonl_path,
    migration_sql,
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
from metaensemble.lib.runtime_state import get_window_burn as get_runtime_burn  # noqa: E402


def _format_standup_five_hour(report: WindowReport, five_h_window) -> str:
    """Render the standup 5-hour summary line, branching on `report.kind`."""
    if report.kind == "live_plan":
        remaining = (
            format_duration(five_h_window.time_until_reset())
            if five_h_window is not None else ""
        )
        time_suffix = f"  ·  **{remaining} left**" if remaining else ""
        return (
            f"- **{report.pct_used:.1f}% of plan used**{time_suffix}  ·  "
            f"**{report.pct_remaining:.1f}% remaining**"
        )
    if report.kind == "last_observed_plan":
        age_str = format_age(report.snapshot_age_seconds)
        return (
            f"- **Live plan usage unavailable** — last runtime snapshot "
            f"was {report.last_observed_pct:.1f}%, {age_str} old, "
            "from a previous window"
        )
    if report.kind == "project_fallback":
        return (
            f"- **Project burn: {report.pct_used:.1f}% of "
            f"{report.capacity_tokens:,} fallback capacity** — "
            "plan-wide usage unavailable"
        )
    # kind == "unavailable"
    return "- **% of plan unavailable** until the statusline refreshes"


def _format_standup_seven_day(seven_line: SevenDayLine, seven_d_window) -> str | None:
    """Render the standup 7-day line, or None to omit it."""
    if seven_line.kind == "unavailable":
        return None
    if seven_line.kind == "live_plan":
        remaining = (
            format_duration(seven_d_window.time_until_reset())
            if seven_d_window is not None else ""
        )
        time_suffix = f"  ·  **{remaining} left**" if remaining else ""
        return f"- **7-day window: {seven_line.pct_used:.1f}% of plan used**{time_suffix}"
    # kind == "last_observed_plan"
    age_str = format_age(seven_line.snapshot_age_seconds)
    return (
        f"- **7-day window: live plan usage unavailable** — last runtime "
        f"snapshot was {seven_line.last_observed_pct:.1f}%, {age_str} old"
    )


def render() -> str:
    ledger = Ledger(db_path=db_path(), jsonl_path=jsonl_path())
    ledger.initialize(migration_sql())

    config = load_budget_config()
    capacity = effective_capacity_tokens(config)

    window_id = current_window_id()
    ledger_burn = ledger.get_window_burn(window_id)
    ledger_used = ledger_burn.total_tokens_in + ledger_burn.total_tokens_out

    # Cross-session truth: the runtime's jsonls hold every token the
    # session actually emitted, not just what MetaEnsemble dispatched.
    runtime_burn = get_runtime_burn(window_id=window_id)
    runtime_used = runtime_burn.input_tokens + runtime_burn.output_tokens
    used = max(runtime_used, ledger_used)
    native = load_native_rate_limits()
    report = resolve_window_report(used, capacity, native)
    seven_line = resolve_seven_day(native)

    since_24h = datetime.now(timezone.utc) - timedelta(hours=24)
    since_7d = datetime.now(timezone.utc) - timedelta(days=7)
    recent_24h = ledger.get_recent_runs(limit=500, since=since_24h)
    active = ledger.get_active_executors(since=since_7d, limit=200)

    # Top burners over the last 24h (Ledger-only — these are dispatched
    # Executors, not the main agent).
    by_executor: Counter[str] = Counter()
    for r in recent_24h:
        by_executor[r.executor_id] += r.tokens_in + r.tokens_out
    top_burners = by_executor.most_common(5)

    alias_by_id: dict[str, str] = {}
    for executor_id, _ in top_burners:
        executor = ledger.get_executor(executor_id)
        if executor:
            alias_by_id[executor_id] = executor.alias

    ledger.close()

    lines = [
        "## Standup",
        "",
        f"### Window `{window_id}`",
    ]
    # When the plan percentage is shown, it is plan-wide (from the runtime
    # rate_limits feed); the token figures below are project-scoped. They
    # appear on separate lines with explicit scope labels so neither
    # implies the other. The four `report.kind` arms enumerate the cases.
    five_h = native.five_hour if native is not None else None
    seven_d_window = native.seven_day if native is not None else None
    lines.append(_format_standup_five_hour(report, five_h))
    lines.append(f"- Source: {report.source}")
    if report.note:
        lines.append(f"- Note: {report.note}")
    seven_str = _format_standup_seven_day(seven_line, seven_d_window)
    if seven_str:
        lines.append(seven_str)
    lines.append(
        f"  - Project tokens in 5h window (runtime): {runtime_used:,} input+output · "
        f"cache_read: {runtime_burn.cache_read_tokens:,} · "
        f"cache_create: {runtime_burn.cache_creation_tokens:,} · "
        f"messages: {runtime_burn.message_count}"
    )
    lines.append(
        f"  - MetaEnsemble-tracked (Ledger): {ledger_used:,} tokens "
        f"across {ledger_burn.total_runs} dispatched Run(s)"
    )
    if runtime_used > 0 and ledger_used < runtime_used * 0.2:
        lines.append(
            "  - Note: Ledger captures only dispatched Runs. The main agent's "
            "reads, edits, and conversation turns make up the difference."
        )
    lines.append("")
    lines.append("### Last 24 hours")
    lines.append(f"- Runs completed: {len(recent_24h)}")
    lines.append(f"- Distinct Executors active: {len({r.executor_id for r in recent_24h})}")
    if top_burners:
        lines.append("- Top token consumers (dispatched Executors only):")
        for executor_id, tokens in top_burners:
            alias = alias_by_id.get(executor_id, executor_id[:12] + "...")
            lines.append(f"  - `{alias}` — {tokens:,} tokens")
    lines.append("")
    lines.append("### Last 7 days")
    lines.append(f"- Active Executors: {len(active)}")
    return "\n".join(lines)


def main() -> int:
    print(render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
