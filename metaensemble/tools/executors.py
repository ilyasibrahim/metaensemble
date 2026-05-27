#!/usr/bin/env python3
"""List live Executors: alias, Role, last seen, last Run.

Invoked by the `/executors` slash command.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from metaensemble.hooks._common import (  # noqa: E402
    db_path,
    jsonl_path,
    migration_sql,
)
from metaensemble.lib.ledger import Ledger  # noqa: E402


LOOKBACK_DAYS = 30


def render() -> str:
    ledger = Ledger(db_path=db_path(), jsonl_path=jsonl_path())
    ledger.initialize(migration_sql())

    since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    executors = ledger.get_active_executors(since=since, limit=200)

    if not executors:
        ledger.close()
        return f"## Executors\n\n(no Executors active in the last {LOOKBACK_DAYS} days)"

    lines = [
        f"## Executors active in the last {LOOKBACK_DAYS} days",
        "",
        "| Alias | Role | Status | Last seen | Last Run |",
        "|---|---|---|---|---|",
    ]
    for e in executors:
        last_runs = ledger.get_runs_by_executor(e.executor_id, limit=1)
        last_run = (
            f"`{last_runs[0].run_id[:8]}...` ({last_runs[0].outcome})"
            if last_runs else "—"
        )
        lines.append(
            f"| `{e.alias}` | {e.role_id} | {e.status} | {e.last_seen_ts[:19]} | {last_run} |"
        )

    ledger.close()
    return "\n".join(lines)


def main() -> int:
    print(render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
