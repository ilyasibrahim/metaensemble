---
name: executors
description: List the Executors active in the last 30 days, with their Role, status, and most recent Run.
---

When the Principal invokes `/executors`, run the runner and relay its output unchanged:

```bash
"$HOME/.metaensemble/runtime/bin/me-run" executors
```

The tool reads from the Ledger via `get_active_executors(since=now-30d, limit=200)` and renders a Markdown table. No further interpretation needed; the table is the answer.
