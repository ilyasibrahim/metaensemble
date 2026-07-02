# Architecture

This document explains how MetaEnsemble is built, what the data model is, how the runtime behaves at each lifecycle event, and — equally important — what MetaEnsemble is *not*.

---

## 1. Thesis

Cognitive agents are addressable services with persistent identity, typed contracts, and observable runs.

This single sentence determines every downstream decision. Identity goes in a registry. Contracts are schema-validated. Runs land in an append-only ledger. Communication patterns borrow from service mesh and typed RPC, not from file-based agent protocols or output-compression skills.

---

## 2. What MetaEnsemble is, and is not

Honesty up front: MetaEnsemble is **a layer of conventions, schemas, hooks, and skills** built on the agent runtime's existing primitives — Tasks, hooks, skills, slash commands, output styles, the permission surface (blocked dispatches ride the native PreToolUse permission decision), the memory files the runtime already loads (adoption records the project's `CLAUDE.md` surfaces in `install-decisions.yaml`, and Manifest scaffolds hand them to Executors as typed `role: memory` pointers), and the statusline telemetry feed the cost gate reads. It is not a new daemon, a new network protocol, or a literal service mesh.

What MetaEnsemble actually is:

- A **set of file conventions** that turn agent specs into versioned Roles, registry rows into addressable Executors, and reports into typed Deliverables.
- A **set of schemas** (Manifest YAML, Brief JSON) that make handoffs validatable and re-searches unnecessary.
- A **set of hooks** that fire on session start, every Task invocation, and session end — recording state, enforcing budgets, verifying deliverables.
- A **state backend** (SQLite + JSONL mirror) that gives Executors durable identity across sessions.
- A **set of slash commands** that turn the patterns above into a single-command surface for the Principal.

What MetaEnsemble is not:

- It is not a literal service mesh. There are no concurrent processes gossiping over a network.
- Executor identity is a row in the registry, not a live daemon. When you `/relaunch arch-7b3`, no process resumes; the agent runtime spawns a fresh agent with the prior Executor's last Brief and Deliverable summary loaded as context.
- Multi-instance fan-out is N parallel Task invocations choreographed by the Coordinator and recorded in the Ledger as N rows sharing a Task ID. It is not literal multithreading.
- Briefs are not transported over a wire in the network sense. They are JSON payloads passed between Task invocations through context.

The architecture is real. The runtime backing it is single-shot agent invocations made coherent through state, schemas, and hooks. This is how it can ship today on existing primitives.

---

## 3. Layers

```
┌───────────────────────────────────────────────────┐
│  Principal Layer                                  │
│  • slash commands (/dispatch, /relaunch, ...)     │
│  • standup, ledger queries                        │
└──────────────────────┬────────────────────────────┘
                       │
┌──────────────────────▼────────────────────────────┐
│  Coordination Layer                               │
│  • Coordinator (main agent)                       │
│  • metaensemble-protocol skill                        │
│  • routing, planning, synthesis                   │
└──────────────────────┬────────────────────────────┘
                       │
┌──────────────────────▼────────────────────────────┐
│  Contract Layer                                   │
│  • Manifest YAML (handoff)                        │
│  • Brief JSON (wire)                              │
│  • Deliverable Markdown (human output)            │
│  • Schema validation enforced at hooks            │
└──────────────────────┬────────────────────────────┘
                       │
┌──────────────────────▼────────────────────────────┐
│  Execution Layer                                  │
│  • Executors (Tasks invoked under Role specs)     │
│  • Per-Executor model tiering                     │
│  • Output style: wire (Brief) vs deliverable      │
└──────────────────────┬────────────────────────────┘
                       │
┌──────────────────────▼────────────────────────────┐
│  State Layer                                      │
│  • SQLite (live, queryable)                       │
│  • JSONL (append-only, replayable)                │
│  • Roles directory (declarative specs)            │
└──────────────────────┬────────────────────────────┘
                       │
┌──────────────────────▼────────────────────────────┐
│  Lifecycle Layer                                  │
│  • SessionStart hook                              │
│  • PreToolUse / PostToolUse hooks                 │
│  • SubagentStop hook                              │
│  • Stop hook                                      │
└───────────────────────────────────────────────────┘
```

Each layer depends only on the layer beneath it. The Principal never speaks to the Execution Layer directly; the Coordination Layer never reads the Lifecycle Layer's state directly — it queries the State Layer.

---

## 4. Portability — Core / User / Project

MetaEnsemble follows the standard overlay pattern (Kubernetes Kustomize, dbt profiles + projects, VSCode user-vs-workspace settings): a project-agnostic Core, with per-engineer and per-project layers stacked on top. This is what makes MetaEnsemble adoptable by any project, including but not limited to the project that authored it.

### Layers

| Layer | Location | Owns |
|---|---|---|
| **Core** | `metaensemble/` inside the MetaEnsemble repo | Schemas, hooks, library code, base Roles, slash commands. Project-agnostic. |
| **User** | `~/.metaensemble/` | Per-engineer preferences: budget thresholds, personal slash commands, model-tier overrides. Optional. |
| **Project** | `<project>/.metaensemble/` | Project-specific Roles, project Manifests, project skills, project starter packs. |

**Merge order, lowest priority to highest:** Core → User → Project. Project wins over User wins over Core. Same semantics as Kustomize overlays and VSCode workspace settings.

### Adoption

A single command turns any project into an MetaEnsemble consumer:

```
metaensemble init   # creates .metaensemble/ with Ledger, manifests dir, and budget config
```

The `--pack` option (`--pack ml`, `--pack web`, `--pack data`) follows the `dbt init` / `cargo new --lib` model of opinionated starter-pack overlays. Starter packs are reserved for v0.2.0; `metaensemble init` without a pack flag is the v0.1.0 bootstrap.

### Hard rule

Core ships zero project assumptions. No reference to any specific domain, dataset, language, or pipeline. Any Role spec that mentions a particular project's domain — a specific NLP language, a specific dataset, a specific cloud provider — lives in that project's `.metaensemble/`, never in Core.

This is the rule that prevents Core from drifting toward any one project's needs. Reviewers reject any PR that puts project knowledge into Core.

---

## 5. Data model

State lives in `.metaensemble/state/department.db` (SQLite) with mirror writes to `.metaensemble/state/runs.jsonl` (append-only).

```sql
-- Roles: declarative specs, one row per spec file in roles/
CREATE TABLE roles (
  role_id    TEXT PRIMARY KEY,        -- e.g. "backend"
  version    TEXT NOT NULL,           -- semver of the spec
  spec_path  TEXT NOT NULL,           -- roles/backend.md
  model_tier TEXT NOT NULL,           -- opus | sonnet | haiku
  created_ts TEXT NOT NULL
);

-- Executors: addressable instances, persist across sessions
CREATE TABLE executors (
  executor_id        TEXT PRIMARY KEY,  -- UUIDv7, time-sortable
  alias              TEXT UNIQUE,       -- "arch-7b3"
  role_id            TEXT NOT NULL REFERENCES roles(role_id),
  parent_executor_id TEXT REFERENCES executors(executor_id),  -- for fan-out lineage
  created_ts         TEXT NOT NULL,
  last_seen_ts       TEXT NOT NULL,
  status             TEXT NOT NULL      -- idle | active | retired
);

-- Tasks: units of work
CREATE TABLE tasks (
  task_id        TEXT PRIMARY KEY,
  task_type      TEXT NOT NULL,
  status         TEXT NOT NULL,       -- open | in_progress | done | failed
  manifest_path  TEXT,
  parent_task_id TEXT REFERENCES tasks(task_id),
  created_ts     TEXT NOT NULL
);

-- Runs: append-only execution log
-- The schema below is the post-003 shape (after migrations 001 + 002 + 003).
-- Older Ledgers migrate forward at `Ledger.initialize()` time: 002 rebuilds
-- the table via SQLite's create-copy-drop-rename pattern to extend the
-- `outcome` CHECK constraint, and 003 adds provenance + token-economics
-- columns via additive `ALTER TABLE ADD COLUMN`. See PERFORMANCE.md R2.
CREATE TABLE runs (
  -- 001 columns
  run_id           TEXT PRIMARY KEY,    -- UUIDv7
  executor_id      TEXT NOT NULL REFERENCES executors(executor_id),
  task_id          TEXT NOT NULL REFERENCES tasks(task_id),
  model            TEXT NOT NULL,       -- runtime-observed model when a transcript supplied one;
                                        -- falls back to manifest-declared tier otherwise
  tokens_in        INTEGER NOT NULL,
  tokens_out       INTEGER NOT NULL,
  window_id        TEXT NOT NULL,       -- 5-hour window bucket
  started_ts       TEXT NOT NULL,
  ended_ts         TEXT NOT NULL,
  outcome          TEXT NOT NULL,       -- ok | failed | partial | interrupted | budget_exceeded
  brief_in_path    TEXT,
  brief_out_path   TEXT,
  deliverable_path TEXT,

  -- 001 backfill (added via ALTER TABLE on legacy DBs)
  failure_reason         TEXT,          -- short label; see §8 for the categorical set
                                        -- plus free-form strings from reconciliation
  quality_state          TEXT,          -- auto | notify | block | NULL when gate did not run
  quality_findings_json  TEXT,          -- compact JSON: {axes: [{name, state, findings, raw}, ...]}

  -- 003 provenance + token-economics columns
  requested_model_tier   TEXT,          -- the manifest-declared tier the Coordinator asked for
                                        -- (may differ from `model` when transcript walking sees a fallback)
  deliverable_ref_json   TEXT,          -- structured deliverable reference; see metaensemble/lib/recording.py.
                                        -- Replaces the strict path-only `deliverable_path` contract.
                                        -- Shapes: {kind:"path",value,inferred} | {kind:"summary",value,len} | {kind:"hash",value}
  files_touched_json     TEXT,          -- JSON array of file paths the subagent wrote/edited,
                                        -- sourced from transcript walking or the file_event hook
  tool_use_json          TEXT,          -- JSON array of {name, count, input_tokens?} per tool
                                        -- the subagent invoked, sourced from transcript walking
  review_findings_json   TEXT,          -- JSON aggregating quality-gate findings and any
                                        -- peer-review Executor verdicts on this Run
  cache_read_tokens      INTEGER NOT NULL DEFAULT 0,    -- Anthropic prompt-cache read tokens
  cache_create_tokens    INTEGER NOT NULL DEFAULT 0,    -- Anthropic prompt-cache creation tokens
  orchestration_tokens   INTEGER NOT NULL DEFAULT 0     -- tokens attributable to MetaEnsemble
                                                        -- orchestration (manifest, brief, hooks),
                                                        -- separate from Executor output
);
```

JSONL mirror schema mirrors the `runs` table one record per line. SQLite is the live view; JSONL is the source of truth for replay if the database is lost or migrated. The mirror carries every column the SQLite row carries, including the 003 additions, so a replay rebuilds the full provenance.

---

## 6. Wire format: Brief

```json
{
  "v": 1,
  "brief_id": "01h2x3y4z5a6b7c8d9e0f1g2h3",
  "from": "arch-7b3",
  "to":   "be-9c1",
  "task_id": "auth-endpoints",
  "ctx": {
    "prior_runs": ["run-01h2x3a1"],
    "files": [
      ["src/auth/spec.md", "1-120"],
      ["src/auth/types.ts", "14-58"]
    ]
  },
  "out": {
    "files": ["src/auth/handlers.ts", "tests/auth.spec.ts"],
    "schema": "schemas/auth-response.json"
  },
  "tier":   "sonnet",
  "budget": 8000
}
```

Validation: the schema at `schemas/brief.schema.json` is shipped and `metaensemble/lib/manifest.py:validate_brief()` will check a Brief dict against it; in v0.1 this is a Coordinator-driven validator (the protocol skill calls it when composing or receiving a Brief) rather than a hook-enforced gate. The Brief is in-prompt JSON between Agent/Task invocations; the runtime offers no hook point at which to intercept the prompt itself, so the validator runs at the Coordinator layer instead. v0.2 will move validation into a dedicated pre-emit hook when the runtime exposes one.

Persistence: Briefs live in the dispatch prompt context, not on disk. The `runs.brief_in_path` / `runs.brief_out_path` columns and the `<project>/.metaensemble/briefs/` directory exist as forward-compatibility; both are unused in v0.1. The `runs.jsonl` mirror records `brief_in_path: null` for v0.1 Runs.

Versioning: the `"v"` field is canonical. Future changes to the Brief schema bump the version; readers refuse unknown versions rather than guess.

Strictness: the Brief schema sets `additionalProperties: false` at the top level and inside its structured `ctx` and `out` objects, matching the Manifest's fail-closed posture in §7. v0.1.0 deliberately keeps Briefs narrow: cross-Executor wire traffic should carry the fields downstream Executors are known to consume, not ad hoc routing hints that disappear silently. Future Coordinator-only metadata belongs in a versioned `extras` field rather than as unknown top-level keys.

---

## 7. Handoff format: Manifest

```yaml
manifest_id: hm-019e4bb1-1099-7490-a187-e160e7827a5b
version: 1
from: arch-7b3
to: be-9c1
task: auth-endpoints

context:
  prior_runs: [run-01h2x3a1, run-01h2x3a2]
  files:
    - { path: src/auth/spec.md,  lines: 1-120, role: design-spec }
    - { path: src/auth/types.ts, lines: 14-58, role: types }

expected_deliverables:
  - path: src/auth/handlers.ts
    must_export: [login, logout, refresh]
  - path: tests/auth.spec.ts
    coverage: ">=80%"

constraints:
  model_tier: sonnet
  window_budget: 8000

acceptance:
  - "all expected exports present"
  - "tests/auth.spec.ts passes"
  - "no regression in tests/"

peer_review:
  mandatory_for_reversibility: true
  reviewer_role_must_differ: true
  min_reviewers: 1
  dissent_handling: surface_minority
```

YAML for human readability and git diff-friendliness; JSON Schema validation for correctness. Manifests live in `.metaensemble/manifests/<manifest_id>.yaml`. The `peer_review` block governs cross-Role validation (see §12) and is mandatory when the Task action is irreversible and the flag `mandatory_for_reversibility` is true.

---

## 8. Lifecycle (hooks)

Hooks live in `metaensemble/hooks/`. Each is a Python script with no model calls.

| Event | Hook | Behavior |
|---|---|---|
| SessionStart | `session_start.py` | Load Registry summary into Coordinator context. Inject 5-hour window status. |
| PreToolUse (Task / Agent) | `pre_task.py` | Resolve target Manifest. Validate against schema. Run cost gate (see §9). A BLOCK rides the runtime's native permission surface — `hookSpecificOutput.permissionDecision: "deny"` with the full decision surface as `permissionDecisionReason`, plus the legacy `decision: "block"` / `reason` pair for older runtimes — so the Coordinator receives the four structured options inline as a proper denial instead of a generic hook error. On NOTIFY *or* BLOCK the same options are also persisted to `state/notifies/<session>-<ts>.json` or `state/blocks/<session>-<ts>.json` as the machine-readable record. |
| PreToolUse (Write / Edit / MultiEdit / NotebookEdit) | `file_event.py` | Enforce the active project boundary before a dispatched file edit runs; edits outside the installed project root are blocked with a recovery message. If the current transcript is a raw or expanded `/dispatch` command but no Task/Agent Run is active, direct file edits are blocked so the Coordinator must spawn an Executor and produce a Ledger row. |
| PostToolUse (Task / Agent) | `post_task.py` | For a **synchronous** dispatch, complete the Run record from the sidecar and append it to SQLite + JSONL with classified outcome (`ok` / `failed` / `partial`) and, when failed, a `failure_reason` label. The label is one of the categorical values `metaensemble/lib/recording.py:classify_failure_reason` emits (`cost_gate_block` / `manifest_invalid` / `timeout` / `exception` / `other`); the same column also carries free-form strings written by the reconcile module on `interrupted` / `budget_exceeded` outcomes (see §8.1). Run the deliverable check (see §10) on successful Runs whose Manifest declared deliverables — built-in Python runners for `.py` paths, configured `axis_commands` for the rest; persist `quality_state` and `quality_findings_json` on the Run row, and (when transcript walking succeeded) the 003 provenance columns `requested_model_tier`, `model_source`, `deliverable_ref_json`, `files_touched_json`, `tool_use_json`, `review_findings_json`, `cache_read_tokens`, `cache_create_tokens`. Update Executor `last_seen_ts`. For a **background** dispatch (the Agent tool returns a launch stub carrying a runtime `agentId` before the subagent runs), reconcile the stub to the sidecar by `tool_use_id`, record an `agentId`-keyed active-dispatch marker so the subagent's writes stay authorized, and **defer** finalization to `SubagentStop`. |
| PostToolUse (Write / Edit / MultiEdit / NotebookEdit) | `file_event.py` | Record successful file-tool events so the enclosing Run can persist `files_touched_json` and `tool_use_json`. |
| PostToolUse (Write) | `deliverable_sync.py` | If a Deliverable was written under the project's report root (the `report_root` in `install-decisions.yaml`; `.metaensemble/reports/` for greenfield projects, an existing convention such as `.claude/reports/` otherwise), register its path in the deliverables index. |
| SubagentStop | `subagent_stop.py` | Finalize a **background** dispatched Run, correlated strictly by `agentId` so concurrent / fan-out dispatches in one session finalize independently. Uses the subagent's own transcript and final message to write the same Run record `post_task.py` would for a synchronous dispatch (shared `finalize_pending`). No-ops when no `agentId`-keyed active dispatch matches (already finalized, or a synchronous runtime finalized in PostToolUse). Never blocks. |
| Stop | `session_summary.py` | Render session digest: Executors active, Runs completed, outputs recorded, window % consumed. |

Hooks register for both `Task` and `Agent` matchers. The agent runtime's subagent-dispatch tool has been called both names across versions; matching both ensures hooks fire regardless of which name the current runtime uses. The hook scripts accept either `tool_name` value.

Every hook is fast, idempotent, and errors are logged to `.metaensemble/hooks/log.jsonl` for debugging.

### 8.1 Reconciliation — closing Runs the hooks could not finish

The PreToolUse hook stamps a sidecar at `<state>/pending/<run_id>.json` before the runtime spawns the Task; PostToolUse reads it back, writes the Run, and deletes the sidecar. Some terminations skip PostToolUse: a `kill -9` of the runtime process, a budget-exhaustion exit from `claude --max-budget-usd`, an out-of-memory crash. The sidecar is stranded; the Ledger would be silent on work that actually consumed tokens.

`metaensemble/lib/reconcile.py` closes the gap in two layers:

- **Layer 1 — Stop hook.** `session_summary.py` reconciles every sidecar whose `session_id` matches the ending session. Catches Ctrl-C and graceful exits where Stop still fires. A still-in-flight **background** dispatch is skipped here (its `agentId`-keyed active marker is live): it outlives the parent turn and `SubagentStop` finalizes it, often after this Stop hook — sweeping it would record a bogus "session ended" Run.
- **Layer 2 — on-demand / on session-start sweep.** `metaensemble reconcile [--older-than-minutes N] [--dry-run]` walks sidecars older than the threshold (default 0 — every sidecar) regardless of session and writes a failed Run per stranded sidecar. The session-start hook calls Layer 2 with a 1-hour threshold so abandoned sidecars from prior sessions are cleaned up at the start of every fresh session.

The reconciled Run row uses `outcome="interrupted"` by default and `outcome="budget_exceeded"` when the parent transcript can be parsed for a budget-kill marker. The `failure_reason` is a free-form string distinguishing the two layers: `"session ended before PostToolUse"` for Layer 1, `"stale sidecar reconciled by metaensemble"` for Layer 2. This is the only path that writes free-form `failure_reason` values; the categorical set in §8 still applies to live PostToolUse failures.

Recording is idempotent by `run_id`. `append_run` inserts under `ON CONFLICT(run_id) DO NOTHING` and mirrors to JSONL only on a real insert, and reconcile skips any sidecar whose `run_id` is already recorded (cleaning up its residue without re-inserting). So a background dispatch finalized by `SubagentStop` and then swept by a later reconcile — or any double-finalize — records exactly one Run and never raises on the primary key.

A sidecar the reconciler cannot record — the Ledger insert raises, for example a foreign-key failure against pruned parent rows — is moved to `<state>/pending/quarantine/<run_id>.json` rather than retried on every future session start. The hook log records `reconcile-sidecar-quarantined` with the destination path (`reconcile-sidecar-failed` appears only when the quarantine move itself fails), and `_iter_pending` never descends into the quarantine directory, so one poisoned sidecar can neither take down a reconcile pass nor generate a permanent error-log drip.

### Runtime rate-limit feed (statusline integration)

Beyond the lifecycle hooks, MetaEnsemble installs a statusline script
(`metaensemble/statusline/me_status.py`, configured in `settings.json` under
`statusLine`). Claude Code v2.1.80+ pipes a JSON payload to the
configured statusline command on every refresh; the payload includes
a `rate_limits` field with the 5-hour and 7-day window usage
(`used_percentage`, `resets_at`). The script captures that field to
`~/.metaensemble/state/runtime-rate-limits.json` atomically on every
refresh. The cost gate and the `/limits` / `/standup` tools read this
file as the authoritative window-state source — the runtime knows the
user's actual plan, and its `used_percentage` is more accurate than
any value MetaEnsemble could derive on its own. The capture file is
treated as fresh for five minutes; older snapshots cause the cost
gate to fall back to the configured `window_capacity_tokens`.

---

## 9. Cost gating — threshold-based escalation

Following the SRE error-budget pattern, AWS Budgets, and Kubernetes admission controllers: most decisions are auto-delegated to the Coordinator; only decisions above explicit thresholds escalate to the Principal. The Principal never sees boilerplate; only judgment calls that need them.

### Three states

| State | Trigger | Behavior | Token cost of the gate |
|---|---|---|---|
| **Auto** | AUTO on both axes AND reversible AND not novel | Coordinator decides, dispatches, logs to Ledger. Zero narration. | ~0 (numeric hook check) |
| **Notify** | At least one axis NOTIFY, none BLOCK | Surfaces a one-paragraph diagnosis and the same three structured options BLOCK would offer, with `default: proceed in a moment`. The dispatch proceeds unless intercepted; the structured payload is persisted to `state/notifies/<session>-<ts>.json` for Coordinator recovery. | ~40 tokens |
| **Block** | At least one axis BLOCK OR irreversible action OR novel pattern | Coordinator emits three structured options with token estimates and waits for the Principal. Default action is `paused`. Persisted to `state/blocks/<session>-<ts>.json`. | ~80 tokens |

The gate is two-axis. *Axis 1 — run size* compares the dispatch's estimated tokens to the **window capacity** (a fixed reference, not a moving denominator). *Axis 2 — window headroom* compares the **remaining percentage** of capacity to two thresholds, surfacing "running out of window" as its own independent signal regardless of how big the next dispatch is.

### Defaults (configurable via `~/.metaensemble/budgets.yaml`)

```yaml
thresholds:
  # Axis 1 — run size, as % of window capacity.
  run_soft_pct_of_capacity: 20      # substantive dispatch — NOTIFY
  run_hard_pct_of_capacity: 40      # outsized — BLOCK

  # Axis 2 — window headroom remaining, as % of capacity.
  window_warn_pct_remaining:  30    # warn when less than this remains
  window_block_pct_remaining: 10    # block all dispatches below this

  # Capacity fallback. Used only when the runtime's native rate_limits
  # feed is unavailable; otherwise the live percentage from
  # Claude Code's statusline payload is the source of truth.
  window_capacity_tokens: 88000

irreversible_actions:
  - "Write to existing files"
  - "Bash matching git push|rm|DROP|DELETE"
  - "any non-localhost network call"

novelty:
  treat_first_run_of_pattern_as_block: true
  drop_to_notify_after_n_runs: 2
  drop_to_auto_after_n_runs: 3

capacity_calibration:
  auto_calibrate_capacity: true     # consult runtime rate_limits feed
```

### Why this works

- **The threshold check runs in the PreToolUse hook as a numeric comparison.** Generating prose costs tokens; comparing numbers does not. The gate itself is near-zero cost.
- **Blocking emits a structured options table, never a paragraph.** The Principal sees a list, not a wall of text.
- **Reversibility overrides cost.** A 50-token git push still blocks because blast radius matters more than dollar cost. `Write to existing files` is evaluated against the file tool's target path at dispatch time: writes to new files do not trip that pattern; writes over existing files do. Standard SRE rule.
- **Novelty escalates.** The first time the Ledger sees a new Manifest pattern, the gate blocks by default. Second time, it notifies. Third time, it auto-runs. The system learns Principal preferences without explicit configuration — same shape as the gradual-rollout pattern in feature-flag systems.
- **Irreversibility triggers mandatory peer review.** Beyond blocking for explicit Principal approval, irreversible actions are dispatched with mandatory cross-Role peer review (see §12). The Coordinator gathers the reviewer's Deliverable before surfacing the original work to the Principal. Cross-Role review surfaces errors and assumptions the executing Role would miss in itself.

### Override

Override capability — bypassing the gate for genuinely urgent work and logging the override in the Ledger as a Run attribute — is reserved for v0.2.0. In v0.1.0, raising the thresholds in `~/.metaensemble/budgets.yaml` is the mechanism for relaxing the gate.

---

## 10. Deliverable check — output evaluation after a Run

The cost gate addresses the dimension of consequence — what is *about* to happen. The deliverable check addresses the dimension of output — what has *just* happened for successful Runs whose Manifest declares deliverables. Python deliverables are checked by the built-in runners below; non-Python deliverables run the same five axes through project-configured commands (`axis_commands` in `quality.yaml` — e.g. `npm test` as the correctness command), each reported under a distinct `<axis>:cmd` name, with unconfigured axes skipping rather than inventing confidence. A run with no `.py` deliverables and no configured commands is not evaluated at all, so the check stays opt-in rather than a universal quality judge. Both gates share the same three-state grammar (AUTO / NOTIFY / BLOCK), the worst-of-axes aggregation, and the structured-options Principal surface. The gates run at different moments because the data they need lands at different moments: cost is a property of the intended Run and is knowable PreToolUse; deliverable quality signals are only knowable PostToolUse.

### Five axes

| Axis | Tool | AUTO | NOTIFY | BLOCK |
|---|---|---|---|---|
| Correctness | pytest exit code | tests pass | 1 failure | 3+ failures |
| Security | bandit (+ optional pip-audit) | zero medium+ | 1+ medium | 1+ high or critical |
| Maintainability | ruff issue count (SonarQube A/B/C/D/E mapping) | A or B (0–5) | C (6–15) | D or E (16+) |
| Complexity | radon McCabe per function | < 10 | 10–15 | > 15 |
| Coverage | coverage.py absolute line floor (v0.1) / delta baseline (v0.2) | ≥ 80% and not dropping | drop < 5pp | drop ≥ 5pp or absolute < 80% |

Final state is the worst of the axes that actually ran (`worst_state` in `metaensemble/lib/quality_gate.py`). The mapping anchors to industry sources: Snyk's medium-severity PR-check default, SonarQube's SQALE issue-count grades, McCabe's stable 10-and-15 thresholds since 1976, NISTIR 8397's 80% coverage floor. Missing tools and absent coverage data produce skipped axes rather than synthetic confidence.

### Hook contract

The gate runs in the PostToolUse hook on the Task / Agent tool:

1. Read the Manifest's `expected_deliverables`; if none are `.py`, the gate skips and returns AUTO.
2. Load `<project>/.metaensemble/quality.yaml` over `~/.metaensemble/quality.yaml` over `_DEFAULT_QUALITY` in `metaensemble/lib/config.py`.
3. For each enabled axis, invoke its runner (`metaensemble/lib/quality_runners.py`). Each runner returns a `QualityAxis` with state, findings, and a raw metric, or `None` when the underlying tool is absent (gate degrades to a partial check).
4. Aggregate with `worst_state`; build a `QualityGateDecision` carrying axes, the four-option Principal surface (when state ≠ AUTO), and the one-paragraph English summary.
5. Persist `quality_state` and `quality_findings_json` on the Run row in the Ledger.
6. On NOTIFY or BLOCK, surface the summary to the Coordinator via `systemMessage`.

### Tool installation

Runners are declared in `pyproject.toml` under `[project.optional-dependencies] quality = [...]`. Install with `pip install -e ".[quality]"`. The five tools (bandit, ruff, radon, coverage, pip-audit) are independent; missing tools produce a partial check rather than failing closed.

### Principal-facing surface

Same shape as the cost-gate surface, four options:

```
## MetaEnsemble quality gate — block
Deliverable from {alias} fails the quality gate.
Failures: {axis: state pairs}.
Findings: {top three with file:line}.

Options:
  1. Accept the Deliverable as-is, log the override
  2. Send to peer review with the findings as the brief
  3. Re-dispatch with the findings folded into acceptance criteria
  4. Split the work, dispatch the remediation as its own Task
```

NOTIFY uses the same shape with `Default: proceed`. BLOCK uses `Default: paused`. The Coordinator surfaces the options without inventing new ones, the same discipline the cost gate enforces.

### Why PostToolUse

The cost gate fires before dispatch because cost is a property of the *intended* Run. Quality is a property of what was actually produced, so it must fire after the Deliverable lands. The asymmetry shows in the failure mode: a cost-gate block prevents a Run that would have happened; a quality-gate block does not undo the Run — it surfaces options for what to do with the Deliverable that already exists.

### Override

Same as the cost gate: v0.1.0 has no in-band override verb. The Principal either accepts the Deliverable (option 1, logged as an override), routes it to peer review, re-dispatches, or splits the work. Project-level threshold relaxation lives in `<project>/.metaensemble/quality.yaml`. v0.2.0 will surface an explicit override verb that records to the Ledger as a Run attribute.

---

## 11. Integration with the agent runtime

MetaEnsemble does not invent runtime primitives. It composes existing ones:

| MetaEnsemble primitive | Agent runtime mechanism |
|---|---|
| Role | Markdown file in `roles/` (extended frontmatter on the standard agent spec format) |
| Executor dispatch | Task invocation with the Role's spec loaded |
| Multi-instance | Multiple parallel Task invocations in one Coordinator response |
| Brief | Structured JSON passed in Task prompt, generated by the wire output style |
| Deliverable | Markdown report written by the Executor, captured by the deliverable output style |
| Lifecycle hooks | Native session, tool, and stop hooks |
| Slash commands | Native custom commands |
| State persistence | SQLite + JSONL on disk; no runtime feature required |

The two shipped output styles are both first-class runtime assets. `wire` is for intra-team Brief JSON and should be selected for Executor-to-Executor handoffs. `deliverable` is for the final Principal-facing Markdown synthesis. Installer layout only changes their installed names: `metaensemble-wire.md`/`metaensemble-deliverable.md` in namespaced layout, `wire.md`/`deliverable.md` in top-level layout.

This is why MetaEnsemble can ship today. Every required primitive exists; MetaEnsemble is the conventions layer that gives them a coherent shape.

---

## 12. Multi-instance patterns

Default dispatch is N=1 — one Executor per Role per Task. The Principal opts in to parallelism explicitly:

### Solo (default)

```
/dispatch implement-auth-endpoints --role backend
```

One Executor. One Run. One Deliverable.

### Fan-out

```
/dispatch explore-cache-strategies --role backend --fanout 3
```

Three Executors from `backend`, each given a divergent Brief (different hypothesis or constraint). Three Deliverables. Coordinator synthesizes; the Principal sees the synthesis plus links to each independent Deliverable.

### Consensus

```
/dispatch review-pr-42 --role review --consensus 3
```

Three review Executors execute the same task. Outputs joined into a majority/dissent report. Diverging signals surface explicitly rather than averaging away.

### Shadow

```
/dispatch tier-test --role test-engineer --shadow sonnet,haiku
```

Two Executors from the same Role on the same task at different model tiers. Used to validate downward tiering before committing to a model change in the Role spec.

### Team dispatch (Coordinator-as-head)

```
/dispatch ship-the-password-reset
```

The Coordinator reads a Manifest whose `delegates_to` block lists the
team members (sub-Roles) and their budget shares, then dispatches each
member as its own Run in parallel. The members are siblings under one
shared `task_id`, not nested under a head Executor: Claude's runtime
does not permit a subagent to invoke `Agent` itself, so the Coordinator
plays both chief-of-staff and team-head roles. The Coordinator collects
each member's Deliverable and synthesizes them into one report.

The pattern is faithful to the M-form concept Alfred Sloan introduced
at GM in 1923 and Williamson formalized: separating coordination from
execution, with each team owning a coherent outcome. The structural
difference from a literal M-form is that the head is the same agent
that runs the session (one less indirection); the trade-off is that
the Coordinator's context is shared across all team plans, which
constrains the maximum team count and total parallelism per session.
For a typical Task, three to five team members under one Coordinator
is comfortable.

### Peer review

```
/dispatch deploy-prod-changes --role devops --peer-review security,sre
```

An Executor of one Role completes the work, and one or more Executors of *different* Roles validate it. Cross-Role review surfaces errors and assumptions the executing Role would miss in itself, which is why productive teams routinely send backend changes to security review and architectural decisions to SRE review. Same-Role peer review is structurally weak; the value comes from the validating Role having a perspective the executing Role lacks.

For irreversible actions (matching the cost gate's reversibility check in §9), peer review is **mandatory** rather than optional. The Coordinator triggers it automatically when a Manifest's `peer_review.mandatory_for_reversibility` flag is set and the proposed Task action affects shared state. Manual invocation via `--peer-review <reviewer-role>,...` works for any Task at any time.

The Coordinator's synthesis surfaces any dissenting reviewer position alongside the executing Executor's Deliverable. A 2-1 split between a primary review and two dissents produces a Deliverable that names both positions with their reasoning. Dissent is not averaged away. The Manifest fields that govern this pattern are `peer_review.mandatory_for_reversibility`, `peer_review.reviewer_role_must_differ`, `peer_review.min_reviewers`, and `peer_review.dissent_handling`, defined in §7.

Every variant logs distinct Executors with shared `parent_task_id` and divergent `parent_executor_id` lineage in the registry.

---

## 13. Role lifecycle

A Role is not a static spec. It is a versioned organizational structure with a creation event, an onboarding phase, ongoing versioning, and an eventual sunset. Treating Role specs as durable lifecycle artifacts rather than disposable prompts is what allows institutional memory to accumulate at the Role level rather than the conversation level.

### 13.1 Creation

Roles enter the system in two ways. The first is **manual creation** by the Principal: a new file at `roles/<role_id>.md` with the spec frontmatter populated (responsibilities, model tier, allowed tools, output style, onboarding fields), committed to the project's `.metaensemble/`. The Coordinator picks up the new Role on the next session. The second is **observability-driven recruitment**, deferred to v0.2.0, in which the Ledger surfaces patterns of bad-fit work and proposes a new Role spec for Principal approval. v0.1.0 ships only manual creation.

### 13.2 Onboarding

A newly created Role enters the system without local knowledge. Its first Executor would, by default, operate without the institutional context that productive specialists rely on, and even the explicit, articulable layer of that context is non-trivial. The Role spec's `onboarding` block addresses this by declaring what the new Executor should read before its first dispatch:

```yaml
# Excerpt from roles/security.md frontmatter
onboarding:
  read_first:
    - reports/arch/system-overview-20260301.md
    - reports/security/threat-model-baseline-20260315.md
  coordinate_with: [backend, devops, sre]
  conventions:
    - metaensemble/conventions/security-review-checklist.md
  mentor_role: code-quality
```

The Coordinator loads the onboarding Manifest into the Brief on the first dispatch of any Executor of this Role. Subsequent dispatches do not re-load it because the Executor's accumulated Run history makes the onboarding redundant. **Onboarding fires once per Executor identity, not once per Run.** This single-fire semantics is why the Ledger query `runs WHERE executor_id = ?` is the basis of the "is this a first dispatch?" check, not a separate flag.

Onboarding has the same shape as the typed handoff in §7: file pointers, peer references, convention references. It is a Manifest, written ahead of time, that defines the new Role's institutional context.

### 13.3 Versioning

Role specs are versioned. The frontmatter `version` field follows semver. Breaking changes (renamed responsibilities, removed tools, changed model tier) bump the major version; additive changes (a new convention reference, a new mentor pointer) bump the minor; clarifications and typo fixes bump the patch. When a Role's major version bumps, all existing Executors of that Role are flagged for Principal review. The Principal decides whether to retire them and spawn new Executors against the new spec, or migrate them via a one-time onboarding pass to the new spec's added context.

### 13.4 Sunset

Planned lifecycle semantics: Roles unused for 60 days should be flagged for archive. Archive will not delete the Role — it will move the spec to `roles/_archive/<role_id>.md` and mark all Executors of that Role as `status: retired`. The archive is intended as a soft delete: restoring a Role should be a single-file move back, with all prior Run history intact in the Ledger. The 60-day auto-flag and manual archive command are reserved for v0.2.0; v0.1.0 ships the Role fields that make this future lifecycle possible, but it does not enforce or execute archiving.

### 13.5 What ships in v0.1.0

| Capability | v0.1.0 | Reserved |
|---|---|---|
| Manual Role creation | ✅ | |
| Onboarding-Manifest schema (field in spec) | ✅ | |
| Role version frontmatter (field in spec) | ✅ | |
| Onboarding loaded on first dispatch | | v0.2.0 |
| Auto-retire Executors on major version bump | | v0.2.0 |
| Manual sunset / archive | | v0.2.0 |
| Observability-driven recruitment | | v0.2.0 |
| Sophisticated onboarding synthesis | | v0.2.0 |
| Sunset auto-flag at 60 days | | v0.2.0 |

---

## 14. Cross-session relaunch

Executor identity is a registry row, so it persists trivially. Relaunch reads back the Executor and reconstructs context.

**Cheap relaunch (default):**

```
/relaunch arch-7b3
```

Loads:
- The Executor's Role spec
- The Brief from its last Run
- The summary section of its last Deliverable
- The list of related Executors (for handoff context)

This is enough to continue most lines of work without re-reading full prior outputs.

**Deep relaunch:**

```
/relaunch arch-7b3 --full
```

Loads everything cheap relaunch loads, plus:
- Full text of the last Deliverable
- Full text of all Briefs in the Executor's Run history
- The full Manifest if one was active

Use deep relaunch when the cheap version proves insufficient — never as the default. Cost is proportional to the Executor's Run history length.

---

## 15. Performance properties

MetaEnsemble's design produces measurable budget benefits. The summary below is the architectural framing. The binding engineering contract — token budgets, time budgets, named rules, and CI-gated benchmarks that block any regression — lives in **[PERFORMANCE.md](./PERFORMANCE.md)** and is required reading before performance-sensitive implementation work.

| Property | Mechanism | Where it is measured |
|---|---|---|
| Per-Executor model tiering | Role spec declares tier; only Roles that need Opus get Opus | Ledger; PERFORMANCE §1.1 |
| Eliminated re-search | Manifests carry typed file/line pointers | Hook log; PERFORMANCE §1.1 |
| Compressed wire | Briefs are terse JSON, not English context | `test_perf_handoff.py`; PERFORMANCE §4 |
| Window-aware dispatch | PreToolUse hook blocks at hard cap | Ledger; ARCHITECTURE §9 |
| Eliminated re-derivation | Persistent Executor identity skips re-introduction | `test_perf_e2e.py`; PERFORMANCE §2 |

Every claim about MetaEnsemble's performance is tied to a measurement in the Ledger or to a benchmark in PERFORMANCE.md §4. No claim ships without backing data from MetaEnsemble's own runs, and no implementation lands that violates the engineering rules in PERFORMANCE.md §3 without explicit revision of those rules in the same change.

---

## 16. What changes in v0.1.0 versus later

**v0.1.0 ships:**
- All schemas and the typed substrate, including peer-review and onboarding fields
- All hooks
- Solo, fan-out, consensus, shadow, and peer-review dispatch patterns
- Cheap relaunch
- Cost gating with auto / notify / block states
- The full slash command surface
- Core / User / Project layering with `metaensemble init` bootstrap
- Manual Role lifecycle: creation, semver version field, onboarding schema field

**Reserved for after v0.1.0:**
- Deep relaunch optimizations (caching of prior context)
- Cross-repo Executors (single-repo only at v0.1.0)
- Observability-driven recruitment subsystem (v0.2.0)
- Manual sunset/archive and the 60-day auto-flag
- Sophisticated peer-review dissent synthesis (basic minority-surfacing ships in v0.1.0)

This sequencing keeps the launch surface small and verifiable. Every reserved feature has a clear acceptance criterion before it ships.

---

## 17. Deployment architecture

A system that exists in code but not in deployment is not a system. The architecture described in the prior fifteen sections — the layers, the primitives, the contracts, the lifecycle — becomes useful only once it enters a project that already has its own conventions and a user who has invested in their existing workflow. The deployment architecture is the load-bearing piece that makes everything above operationally real.

Two operational phases govern deployment. The **inspection phase** reads the user's `~/.claude/` and the project's `.claude/` to take typed inventory of existing artifacts, detect name collisions with MetaEnsemble's curated set, and propose per-Role relevance from filesystem signals. Inspect writes a short Markdown report at `<project>/.metaensemble/inspection-<timestamp>.md` AND a companion `install-decisions.yaml` — the user's editable choice surface. Every agent and every curated Role gets one entry with a sensible default and a one-line rationale; the user edits the lines they disagree with and saves. No other state changes during inspection.

The **install phase** reads `install-decisions.yaml` and honors every per-agent choice. The layout flag controls only where MetaEnsemble's own pieces install — namespaced subdirectories in `namespaced` layout, top-level placement with collision refusal in `top-level` layout. The four agent-handling cases (`collision`, `user_unique`, `curated_relevant`, `curated_optional`) and the seven actions (`keep_yours`, `take_ours`, `keep_both`, `preserve`, `convert`, `activate`, `retire`) are defined in DEPLOYMENT.md and consumed by the installer at plan time. Every applied action is recorded in a backup directory at `<project>/.metaensemble/backups/<timestamp>/`. The full set is reversible by `metaensemble unadopt` (project scope) and `metaensemble user-teardown` (user scope), which walk every install's plan in reverse chronological order. `metaensemble export-agents` provides a parallel escape hatch that reverse-converts Roles back to Claude Code agent files even when the install backups directory is missing.

The Coordinator reads `<project>/.metaensemble/active-roles.yaml` at dispatch time. The file lists every Role the Coordinator can dispatch in this project: the user's preserved native agents, the user's converted Roles, the curated Roles the user activated, and (for `keep_both` collisions) the `-me`-suffixed curated counterparts. Names the user explicitly retired live in `inactive_roles`; the Coordinator refuses dispatches against them.

The full deployment behavior, the per-action handling, the recovery paths, and the reversibility contract are documented in [`DEPLOYMENT.md`](./DEPLOYMENT.md). The short version: the installer respects what the user already has, asks for explicit per-agent choice, refuses to do semantic rewriting of user content, and treats reversibility as a tested contract rather than a documented intention.
