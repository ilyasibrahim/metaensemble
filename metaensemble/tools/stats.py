#!/usr/bin/env python3
"""Print a one-screen Ledger growth and run-mix digest.

Invoked by `metaensemble stats`. Emits Markdown for direct relay.

PERFORMANCE.md §5.1 documents measured Ledger growth (~1.6 KiB/Run);
this tool shows a live project its own numbers next to that expectation
so drift in the storage constant is visible without lab measurement.
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
from metaensemble.lib.ledger import Ledger  # noqa: E402


# PERFORMANCE.md §5.1: measured on schema 001–003 with fully populated
# rows — the worst realistic case. Shown for comparison, not enforced.
EXPECTED_KIB_PER_RUN = 1.6
TOP_EXECUTOR_LIMIT = 5


def _file_size(path: Path) -> int:
    """Size in bytes; 0 when the file does not exist yet."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _kib(size_bytes: int) -> str:
    return f"{size_bytes / 1024:,.1f} KiB"


def render() -> str:
    ledger = Ledger(db_path=db_path(), jsonl_path=jsonl_path())
    ledger.initialize(migration_sql())

    outcome_counts = ledger.get_outcome_counts()
    total_runs = sum(outcome_counts.values())
    if total_runs == 0:
        ledger.close()
        return (
            "## Ledger stats\n\n"
            "(no Runs recorded yet — dispatch a Task and check back)"
        )

    top_executors = ledger.get_executor_run_counts(limit=TOP_EXECUTOR_LIMIT)
    window_id = current_window_id()
    window_burn = ledger.get_window_burn(window_id)
    ledger.close()

    db_size = _file_size(db_path())
    jsonl_size = _file_size(jsonl_path())
    total_size = db_size + jsonl_size
    # total_runs > 0 here — the empty Ledger returned above.
    kib_per_run = total_size / 1024 / total_runs

    mix = ", ".join(
        f"{outcome} {count} ({count / total_runs * 100:.1f}%)"
        for outcome, count in sorted(
            outcome_counts.items(), key=lambda kv: (-kv[1], kv[0])
        )
    )

    lines = [
        "## Ledger stats",
        "",
        f"- Runs recorded: {total_runs}",
        f"- Outcome mix: {mix}",
        "",
        "### Footprint",
        "",
        "| Store | Size |",
        "|---|---|",
        f"| SQLite (`department.db`) | {_kib(db_size)} |",
        f"| JSONL mirror (`runs.jsonl`) | {_kib(jsonl_size)} |",
        f"| Total | {_kib(total_size)} |",
        "",
        f"- Growth: {kib_per_run:.1f} KiB/Run "
        f"(PERFORMANCE.md §5.1 measured ~{EXPECTED_KIB_PER_RUN} KiB/Run)",
        "",
        f"### Top Executors by Run count (max {TOP_EXECUTOR_LIMIT})",
        "",
        "| Alias | Role | Runs |",
        "|---|---|---|",
    ]
    for entry in top_executors:
        lines.append(f"| `{entry.alias}` | {entry.role_id} | {entry.run_count} |")

    window_tokens = window_burn.total_tokens_in + window_burn.total_tokens_out
    lines.extend([
        "",
        f"- Current window `{window_id}`: {window_burn.total_runs} Run(s), "
        f"{window_tokens:,} tokens (dispatched Runs only)",
    ])
    return "\n".join(lines)


def main() -> int:
    print(render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
