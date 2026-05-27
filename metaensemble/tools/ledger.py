#!/usr/bin/env python3
"""Ledger query CLI.

Invoked by the `/ledger <subcommand>` slash command. Subcommands map onto
the named-query API in `metaensemble/lib/ledger.py`; ad-hoc SQL is forbidden by
PERFORMANCE.md §3 R1, and this tool honors that by exposing only the
named queries as subcommands.

Usage:
    python -m metaensemble.tools.ledger recent [--limit N]
    python -m metaensemble.tools.ledger by-executor <executor_id_or_alias> [--limit N]
    python -m metaensemble.tools.ledger by-task <task_id> [--limit N]
    python -m metaensemble.tools.ledger window <window_id>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from metaensemble.hooks._common import (  # noqa: E402
    db_path,
    jsonl_path,
    migration_sql,
)
from metaensemble.lib.ledger import Ledger, Run  # noqa: E402


def _format_runs(runs: list[Run]) -> str:
    if not runs:
        return "(no matching runs)"
    lines = [
        "| Run | Executor | Task | Model | In / Out | Window | Outcome |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in runs:
        lines.append(
            f"| `{r.run_id[:8]}...` "
            f"| `{r.executor_id[:8]}...` "
            f"| {r.task_id} "
            f"| {r.model} "
            f"| {r.tokens_in:,} / {r.tokens_out:,} "
            f"| {r.window_id} "
            f"| {r.outcome} |"
        )
    return "\n".join(lines)


def _cmd_recent(ledger: Ledger, args) -> str:
    runs = ledger.get_recent_runs(limit=args.limit)
    return f"## Recent runs (limit {args.limit})\n\n" + _format_runs(runs)


def _cmd_by_executor(ledger: Ledger, args) -> str:
    target = args.executor
    # Allow either UUID or alias.
    if "-" in target and len(target.split("-")[-1]) == 3:
        executor = ledger.get_executor_by_alias(target)
        if not executor:
            return f"## by-executor `{target}`\n\nNo Executor with alias `{target}`."
        executor_id = executor.executor_id
    else:
        executor_id = target
    runs = ledger.get_runs_by_executor(executor_id, limit=args.limit)
    return f"## Runs for Executor `{target}` (limit {args.limit})\n\n" + _format_runs(runs)


def _cmd_by_task(ledger: Ledger, args) -> str:
    runs = ledger.get_runs_by_task(args.task, limit=args.limit)
    return f"## Runs for Task `{args.task}` (limit {args.limit})\n\n" + _format_runs(runs)


def _cmd_window(ledger: Ledger, args) -> str:
    summary = ledger.get_window_burn(args.window)
    lines = [
        f"## Window `{args.window}`",
        "",
        f"- Runs: {summary.total_runs}",
        f"- Tokens in: {summary.total_tokens_in:,}",
        f"- Tokens out: {summary.total_tokens_out:,}",
        f"- Total: {summary.total_tokens_in + summary.total_tokens_out:,}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ledger")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_recent = sub.add_parser("recent", help="Recent Runs across all Executors")
    p_recent.add_argument("--limit", type=int, default=20)
    p_recent.set_defaults(func=_cmd_recent)

    p_exec = sub.add_parser("by-executor", help="Runs for one Executor (UUID or alias)")
    p_exec.add_argument("executor")
    p_exec.add_argument("--limit", type=int, default=20)
    p_exec.set_defaults(func=_cmd_by_executor)

    p_task = sub.add_parser("by-task", help="Runs for one Task")
    p_task.add_argument("task")
    p_task.add_argument("--limit", type=int, default=20)
    p_task.set_defaults(func=_cmd_by_task)

    p_window = sub.add_parser("window", help="Window-bucket aggregate burn")
    p_window.add_argument("window")
    p_window.set_defaults(func=_cmd_window)

    args = parser.parse_args(argv)

    ledger = Ledger(db_path=db_path(), jsonl_path=jsonl_path())
    ledger.initialize(migration_sql())
    output = args.func(ledger, args)
    ledger.close()
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
