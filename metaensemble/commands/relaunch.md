---
name: relaunch
description: Resume a past Executor by alias. Loads its last Brief and Deliverable summary into a fresh Run. Use --full for deep context restore.
---

When the Principal invokes `/relaunch <alias>` (e.g. `/relaunch arch-7b3`):

1. **Load the relaunch context.** Run the runner and read its output:

   ```bash
   "$HOME/.metaensemble/runtime/bin/me-run" relaunch <alias>
   ```

   Add `--full` if the Principal asked for the deep restore. The tool prints a Markdown context block: the Executor's identity, the most recent Brief, and a summary of the most recent Deliverable. If no Executor matches the alias, the tool exits non-zero with an explanatory message; relay it and stop.

2. **Compose the resumption Brief.** Combine the printed context with the new instruction the Principal just gave you. The resumption Brief is the synthesis the next Run will start from.

3. **Dispatch under the same identity.** Spawn the Task and include a continuation marker in the prompt so the recording layer reuses the same Executor row rather than creating a new one:

   ```
   [continuing: <alias>]
   <the resumption Brief, in plain English>
   ```

   The PreToolUse hook reads the marker, resolves the existing Executor by alias, and records the new Run under that identity. From this point on, the dispatch proceeds exactly like any other Task.

4. **Synthesize.** After the new Run completes, return the Deliverable to the Principal as you would for any Dispatch.

See `ARCHITECTURE.md` §14 for the relaunch contract.
