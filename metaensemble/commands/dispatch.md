---
name: dispatch
description: Plan a Task, compose a Manifest, and spawn one or more Executors. Default is N=1 (Solo). Use --fanout, --consensus, --shadow, or --peer-review for multi-instance patterns.
---

When the Principal invokes `/dispatch <task description> [flags]`, follow the Coordinator protocol in `metaensemble/skills/metaensemble-protocol/SKILL.md`:

1. **Plan.** Decompose the task description into one or more Tasks. For each Task, pick the Role best suited to execute (the subagent_type) and the model tier the Role declares.

2. **Compose the Manifest** *(when the Task warrants a typed contract)*. Write a YAML Manifest at `.metaensemble/manifests/<manifest_id>.yaml`. The Manifest is mandatory for irreversible Tasks and recommended for any multi-step Task. Include `manifest_id` of the form `hm-<UUIDv7 hex>`, typed `context.files` pointers, explicit `expected_deliverables` with paths and verifiable assertions (`must_export`, `coverage`), and `constraints` for model tier and window budget. When the Task affects shared state, set `peer_review.mandatory_for_reversibility: true`. Validation is automatic: the PreToolUse hook validates the Manifest against `metaensemble/schemas/manifest.schema.json` when you reference it.

3. **Embed markers in the Task prompt.** The PreToolUse hook reads bracket-marker metadata from the prompt and uses it to drive the recording layer and the protocol guard. Markers (all optional):
   - `[manifest: hm-<id>]` — the Coordinator's Manifest for this Task. The hook validates the YAML; if validation fails, the dispatch blocks.
   - `[task: task-<id>]` — explicit Task id. Use to group multiple Executors under one Task (fan-out, consensus, peer review).
   - `[project: /absolute/project/root]` — explicit adopted project root. Include this on every Executor prompt when `/dispatch` is invoked from outside the target project or with `--project <path>`, so hooks write the Run to the intended project's Ledger.
   - `[continuing: <alias>]` — resume a specific Executor by alias (typically used from `/relaunch`).
   - `[fresh]` — force-create a new Executor of this Role rather than reusing the most-recent active one. Required for fan-out, consensus, and the reviewer leg of peer-review dispatches.
   - `[fanout: N]` — declared fanout size when dispatching under `--fanout N`. The hook rejects the dispatch deterministically when `N < 2`; this is the enforceable protocol guard for `--fanout 1` and similar invalid invocations. Required on every Executor prompt in a fan-out batch.
   - `[consensus: N]` — same shape and same guard for `--consensus N`. Required on every Executor prompt in a consensus batch.

4. **Dispatch.** Spawn the Executor(s) via Task invocations, embedding the markers from step 3 in each prompt. The PreToolUse hook runs the cost gate and stamps a pending Run; PostToolUse completes the Run when the Executor returns. The recording layer is automatic — you do not need to write to the Ledger directly.

5. **Respect overlap ownership.** Before composing Briefs, read `<project>/.metaensemble/install-decisions.yaml`. For every `overlaps.*.action: metaensemble_owned`, do not instruct Executors to maintain the listed project surface. For deliverable/work-record documentation, rely on the Ledger and Deliverables index for structural tracking unless the overlap is `project_owned` or `dual`.

6. **Verify and synthesize.** After Executors return, confirm declared Deliverables exist at the paths the Manifest declared. Write your synthesis to a Markdown file under `reports/`; the `deliverable_sync.py` hook will register it in the Deliverables index. When multiple Executors ran (fan-out, consensus, peer review), surface dissent explicitly rather than averaging it.

## Multi-instance patterns

`--fanout N` — N Executors of one Role with divergent Briefs; explore hypotheses. Generate one shared `task_id` and one Manifest. Spawn N Tasks, each with `[manifest: hm-...] [task: task-...] [fanout: N] [fresh]` plus a divergent prompt body. `N` must be >= 2; the PreToolUse hook rejects `[fanout: 1]` (or any `N < 2`) deterministically before any Executor work begins.

`--consensus N` — N Executors of one Role with the same Brief; surface majority and dissent rather than averaging. Same markers as fan-out, identical prompt bodies, with `[consensus: N]` in place of `[fanout: N]`. The same `N >= 2` guard applies.

`--shadow tier1,tier2` — two Executors at different model tiers; validate downward tiering. Same markers as fan-out, identical Brief, but each invocation's `subagent_type` may carry tier-specific routing.

`--peer-review role1,role2` — one Executor + one or more reviewer Executors of different Roles. Mandatory for irreversible Tasks. Spawn the executor Task with `[manifest: hm-...] [task: task-...]`, then spawn each reviewer Task with `[manifest: hm-...] [task: task-...] [fresh]` and the reviewer's `subagent_type`. The shared `task_id` groups them in the Ledger for later inspection.

`--project <path>` — dispatch against an adopted project even when the current Claude session cwd is somewhere else. Resolve `<path>` to an absolute path and include `[project: <absolute-path>]` on every Executor prompt and Manifest path decision. The hooks route pending sidecars, Run rows, file provenance, and quality checks through that project's `.metaensemble/state/`.

The full pattern reference is in `ARCHITECTURE.md` §12.
