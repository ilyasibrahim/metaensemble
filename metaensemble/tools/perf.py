#!/usr/bin/env python3
"""Rolling performance metrics: hook log health, recent Run latency, Brief size.

Invoked by the `/perf` slash command. Surfaces drift against the
PERFORMANCE.md §3 budgets before it becomes a problem.
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from metaensemble.hooks._common import (  # noqa: E402
    db_path,
    hooks_log_path,
    jsonl_path,
    migration_sql,
)
from metaensemble.lib.ledger import Ledger  # noqa: E402


WINDOW_HOURS = 24


def _hook_errors_in_window() -> list[dict]:
    log = hooks_log_path()
    if not log.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
    out: list[dict] = []
    with log.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts = datetime.fromisoformat(rec["ts"])
                if ts >= cutoff:
                    out.append(rec)
            except Exception:  # nosec B112
                # Hook-log lines are append-only diagnostics. Skip malformed
                # rows so a broken log record cannot break the perf view.
                continue
    return out


def _is_reconciled_stale_sidecar(run) -> bool:
    reason = (getattr(run, "failure_reason", None) or "").lower()
    return "stale sidecar reconciled" in reason


def _run_latency_ms(run) -> float | None:
    # A reconciled stale sidecar's ended_ts is the reconciliation time, not
    # the model-run completion time. Including it in latency turns crash
    # recovery age into "performance" and produces multi-day p95 values.
    if _is_reconciled_stale_sidecar(run):
        return None
    try:
        start = datetime.fromisoformat(run.started_ts)
        end = datetime.fromisoformat(run.ended_ts)
        return (end - start).total_seconds() * 1000.0
    except Exception:
        return None


def render() -> str:
    ledger = Ledger(db_path=db_path(), jsonl_path=jsonl_path())
    ledger.initialize(migration_sql())
    since = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
    runs = ledger.get_recent_runs(limit=500, since=since)
    ledger.close()

    latency_skipped = sum(1 for r in runs if _is_reconciled_stale_sidecar(r))
    latencies = [v for v in (_run_latency_ms(r) for r in runs) if v is not None and v >= 0]
    errors = _hook_errors_in_window()
    error_kinds: dict[str, int] = {}
    for e in errors:
        error_kinds[e.get("kind", "?")] = error_kinds.get(e.get("kind", "?"), 0) + 1

    lines = [
        f"## Performance — last {WINDOW_HOURS}h",
        "",
        f"### Runs ({len(runs)})",
    ]
    if latencies:
        latencies.sort()
        p50 = latencies[len(latencies) // 2]
        p95 = latencies[int(0.95 * len(latencies))]
        mean = statistics.mean(latencies)
        lines.append(f"- Latency: p50 {p50:.0f}ms · p95 {p95:.0f}ms · mean {mean:.0f}ms")
    else:
        lines.append("- Latency: insufficient timing data")
    if latency_skipped:
        lines.append(
            f"- Latency exclusions: {latency_skipped} stale reconciled "
            "sidecar run(s) excluded from latency aggregates"
        )

    by_outcome: dict[str, int] = {}
    for r in runs:
        by_outcome[r.outcome] = by_outcome.get(r.outcome, 0) + 1
    if by_outcome:
        outcome_str = ", ".join(f"{k}: {v}" for k, v in sorted(by_outcome.items()))
        lines.append(f"- Outcomes: {outcome_str}")

    # The "failed-run waste" metric is the gross tokens consumed by runs
    # whose outcome was not `ok` — surfaces reliability and cost together
    # so the Principal can see whether instability is also costly.
    failed_outcomes = {"failed", "partial", "interrupted", "budget_exceeded"}
    failed_runs = [r for r in runs if r.outcome in failed_outcomes]
    if failed_runs:
        waste_in = sum(r.tokens_in for r in failed_runs)
        waste_out = sum(r.tokens_out for r in failed_runs)
        lines.append(
            f"- Failed-run waste: {len(failed_runs)} run(s), "
            f"{waste_in:,} tokens_in + {waste_out:,} tokens_out"
        )

    # Cache effectiveness shows whether MetaEnsemble overhead is benefiting
    # from Anthropic prompt caching.
    total_cache_read = sum((r.cache_read_tokens or 0) for r in runs)
    total_cache_create = sum((r.cache_create_tokens or 0) for r in runs)
    if total_cache_read or total_cache_create:
        lines.append(
            f"- Cache: {total_cache_read:,} cache_read, "
            f"{total_cache_create:,} cache_create across {len(runs)} run(s)"
        )

    lines.append("")
    lines.append("### Hook health")
    lines.append(f"- Error log entries: {len(errors)}")
    if error_kinds:
        for kind, count in sorted(error_kinds.items(), key=lambda kv: -kv[1])[:5]:
            lines.append(f"  - `{kind}`: {count}")

    return "\n".join(lines)


def main() -> int:
    print(render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
