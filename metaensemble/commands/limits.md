---
name: limits
description: Show the current 5-hour window's token burn, remaining capacity, and run count.
---

When the Principal invokes `/limits`, run the runner and relay its output unchanged:

```bash
"$HOME/.metaensemble/runtime/bin/me-run" limits
```

The output is Markdown formatted for direct relay. If the Principal asks for a specific historical window, fall back to `/ledger window <window-id>` for arbitrary buckets.
