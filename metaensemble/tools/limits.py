#!/usr/bin/env python3
"""Print the current 5-hour token-limit burn and remaining capacity.

Invoked by the `/limits` slash command. Emits Markdown for direct relay.
"""
from __future__ import annotations

import sys
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
    load_native_rate_limits,
    resolve_seven_day,
    resolve_window_report,
)
from metaensemble.lib.runtime_state import get_window_burn as get_runtime_burn  # noqa: E402


def _plan_5h_row(report: WindowReport) -> str:
    """The `| Plan 5h ... |` row, branching on `report.kind`.

    All four arms emit a Markdown table row. The `unavailable` arm
    must NEVER render a percentage — that is the invariant from the
    2026-05-26 regression report.
    """
    if report.kind == "live_plan":
        return (
            f"| Plan 5h   | {report.pct_used:.1f}% used "
            f"| rate_limits feed (live, plan-wide) |"
        )
    if report.kind == "last_observed_plan":
        age_str = format_age(report.snapshot_age_seconds)
        return (
            f"| Plan 5h   | last seen {report.last_observed_pct:.1f}% "
            f"({age_str} old) | live plan telemetry unavailable; snapshot from a previous window |"
        )
    if report.kind == "project_fallback":
        return (
            f"| Plan 5h   | project burn: {report.pct_used:.1f}% of "
            f"{report.capacity_tokens:,} fallback "
            f"({report.used_tokens:,} tokens) | derived from fallback capacity "
            "(not plan-wide) |"
        )
    # kind == "unavailable"
    return (
        "| Plan 5h   | % unavailable "
        "| live plan telemetry not yet captured; awaiting statusline refresh |"
    )


def _seven_day_block(seven_line: SevenDayLine, seven_d_window) -> list[str]:
    """Return zero or two lines (blank separator + the 7-day line)."""
    if seven_line.kind == "unavailable":
        return []
    if seven_line.kind == "live_plan":
        resets = seven_d_window.resets_at if seven_d_window is not None else "?"
        return [
            "",
            f"- 7-day window: {seven_line.pct_used:.1f}% used (resets {resets})",
        ]
    # kind == "last_observed_plan"
    age_str = format_age(seven_line.snapshot_age_seconds)
    return [
        "",
        (
            f"- 7-day window: live usage unavailable; last runtime snapshot "
            f"was {seven_line.last_observed_pct:.1f}%, {age_str} old"
        ),
    ]


def render() -> str:
    """Render a three-row window display.

    Earlier output collapsed Ledger tokens, runtime tokens, and
    cache tokens into a single percentage, which produced confusing
    output like `0 tokens (13%)` when the Ledger was empty but the
    runtime had already touched the window. The three-row layout
    separates the three independent measurements so the Principal can
    see exactly what each one says.

    The Plan-5h row routes through `resolve_window_report` so the
    "live vs last-observed vs project-fallback vs unavailable"
    decision is shared with every other surface (session_start,
    session_summary, /standup). That central function refuses to
    synthesize `0.0% used` when no live snapshot exists.
    """
    ledger = Ledger(db_path=db_path(), jsonl_path=jsonl_path())
    ledger.initialize(migration_sql())
    config = load_budget_config()
    capacity = effective_capacity_tokens(config)

    window_id = current_window_id()
    ledger_burn = ledger.get_window_burn(window_id)
    ledger_used = ledger_burn.total_tokens_in + ledger_burn.total_tokens_out

    runtime_burn = get_runtime_burn(window_id=window_id)
    runtime_used = runtime_burn.input_tokens + runtime_burn.output_tokens

    ledger.close()
    native = load_native_rate_limits()
    # Plan-5h uses the runtime-scoped burn (cross-project token total in
    # this 5h bucket) as the numerator for the project_fallback arm; the
    # Ledger burn is shown separately on its own row.
    report = resolve_window_report(runtime_used, capacity, native)
    seven_line = resolve_seven_day(native)

    lines: list[str] = [
        f"## Window `{window_id}`",
        "",
        "| Source | Count | Note |",
        "|---|---|---|",
    ]

    # Row 1: Ledger Runs and tokens in this window (cost-accountable).
    lines.append(
        f"| Ledger    | {ledger_burn.total_runs} runs, {ledger_used:,} tokens "
        f"| dispatched Runs only |"
    )

    # Row 2: plan-window percentage. The Plan 5h row is plan-wide when
    # available; `runtime_used` is project-scoped and lives in Row 3.
    # They are NOT a unit pair and must not appear in the same cell.
    lines.append(_plan_5h_row(report))
    if report.note:
        lines.append(f"|           |  | {report.note} |")
    lines.append(
        f"| Project   | {runtime_used:,} input+output tokens "
        f"| this project's runtime burn in window |"
    )

    # Row 3: Cache. Two separate token streams kept distinct because they
    # bill differently and have different optimization implications.
    cache_read = runtime_burn.cache_read_tokens
    cache_create = runtime_burn.cache_creation_tokens
    lines.append(
        f"| Cache     | {cache_read:,} cache_read, {cache_create:,} cache_create "
        f"| amortizes across long contexts |"
    )

    lines.extend(_seven_day_block(seven_line, native.seven_day if native is not None else None))

    return "\n".join(lines)


def main() -> int:
    print(render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
