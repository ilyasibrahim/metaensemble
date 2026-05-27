---
name: perf
description: Rolling performance metrics over the last 24 hours — Run latency distribution, outcome breakdown, hook error log health.
---

When the Principal invokes `/perf`, run the runner and relay its output unchanged:

```bash
"$HOME/.metaensemble/runtime/bin/me-run" perf
```

This surface lets the Principal see drift against the `PERFORMANCE.md` §3 budgets before it becomes a problem. If the Principal wants to drill into specific hook errors, point them at `.metaensemble/hooks/log.jsonl`.
