---
name: standup
description: Daily-style summary — current window status, last-24h Runs, top token consumers, active Executors over the last 7 days.
---

When the Principal invokes `/standup`, run the runner and relay its output unchanged:

```bash
"$HOME/.metaensemble/runtime/bin/me-run" standup
```

The runner pins one absolute Python interpreter and execs the CLI, so it works regardless of whether the venv is activated in the current shell. It reads from the Ledger via named queries (`get_window_burn`, `get_recent_runs`, `get_active_executors`) and prints a Markdown digest. No further commentary unless the Principal asks for it.

If the runner does not exist (`No such file or directory`), MetaEnsemble has not been installed yet; tell the Principal to run `metaensemble user-setup --layout=namespaced` (or `--layout=top-level`).
