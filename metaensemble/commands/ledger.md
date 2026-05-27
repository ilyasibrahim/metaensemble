---
name: ledger
description: Query the Ledger via named subcommands. Subcommands — recent, by-executor, by-task, window. Forbidden — ad-hoc SQL.
---

When the Principal invokes `/ledger <subcommand> [args]`, run the runner and relay its output unchanged:

```bash
"$HOME/.metaensemble/runtime/bin/me-run" ledger <subcommand> [args]
```

Subcommands:

- `recent [--limit N]` — most recent Runs across all Executors (default 20)
- `by-executor <executor_id_or_alias> [--limit N]` — Runs for one Executor; accepts UUID or alias
- `by-task <task_id> [--limit N]` — Runs for one Task
- `window <window_id>` — aggregate burn for one 5-hour window bucket

Per `PERFORMANCE.md` §3 R1, every subcommand maps to a named query in `metaensemble/lib/ledger.py`. Ad-hoc SQL is forbidden; if the Principal asks a question that no subcommand answers, propose a new named query in `lib/ledger.py` rather than reaching for raw SQL.
