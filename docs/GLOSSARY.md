# Glossary

Every MetaEnsemble primitive defined precisely, with its industry analog and where it lives on disk or in the database.

Terms are grouped by concept. Within each group, the primary term comes first, followed by closely related terms.

---

## Identity and roles

### Role
**Shape:** Markdown file with extended frontmatter. Declarative.
**Location:** `roles/<role_id>.md`
**Analog:** Kubernetes Deployment spec, IAM Role, HuggingFace model card, Job Description.

A versioned specification of a kind of cognitive worker. Names the Role's responsibilities, declares its model tier, lists allowed tools, defines its output style preferences. One Role spec produces zero or more live Executors.

A Role is a *type*, not an *instance*. `roles/backend.md` describes what a backend Executor does; it is not itself a backend Executor.

### Executor
**Shape:** Row in the `executors` table.
**Location:** SQLite at `.metaensemble/state/department.db`; mirrored to `.metaensemble/state/runs.jsonl` on creation.
**Analog:** Spark Executor, Kubernetes Pod, MLflow run with stable ID, OpenTelemetry trace.

A live, addressable instance of a Role. Has a UUIDv7 canonical ID and a short alias (`arch-7b3`) for typing convenience. Persists across sessions — an Executor spawned today can be relaunched next month with `/relaunch arch-7b3`.

Multiple Executors from the same Role can exist concurrently (fan-out) or serially (one Executor followed by another for the next Task).

### Principal
**Shape:** Person.
**Location:** Outside the system.
**Analog:** AWS / GCP / OAuth Principal — the entity that requests actions and assumes Roles.

The human running the system. Does not invoke Executors directly; speaks to the Coordinator via slash commands and natural language. Approves above-threshold dispatch decisions surfaced by the cost gate.

### Coordinator
**Shape:** The main agent in the active session.
**Location:** Whatever conversation the Principal is in.
**Analog:** Kafka coordinator, ZooKeeper coordinator, Cassandra coordinator, Spark Driver — the single orchestrator that dispatches workers and aggregates results.

The single agent the Principal talks to. Plans Tasks, dispatches Executors, validates Manifests, synthesizes Deliverables, queries the Ledger. Holds the routing logic via the `metaensemble-protocol` skill.

The Coordinator is not itself an Executor — it is the orchestrator that invokes Executors.

---

## Work and execution

### Task
**Shape:** Row in the `tasks` table.
**Location:** SQLite.
**Analog:** Issue, ticket, MLflow experiment, Kubernetes Job, Airflow DAG node.

A unit of work. Has a type, a status (`open` / `in_progress` / `done` / `failed`), an optional Manifest, optional dependency on a parent Task. May be served by one or many Executors across one or many Runs.

Tasks are not the same as Runs. One Task may have several Runs (retries, fan-out, consensus). One Run always belongs to exactly one Task.

### Run
**Shape:** Row in the `runs` table; one line in `runs.jsonl`.
**Location:** SQLite + JSONL.
**Analog:** MLflow run, span in distributed tracing, single CI job execution.

One execution attempt by one Executor for one Task. Records: Executor ID, Task ID, model used, tokens in, tokens out, window bucket, start/end timestamps, outcome (`ok` / `failed` / `partial`), a failure reason when the outcome is `failed` (`cost_gate_block`, `manifest_invalid`, `timeout`, `exception`, or `other`), and paths to the Brief in, Brief out, and Deliverable.

The Ledger is the table of Runs. Every Run is append-only; corrections happen by writing a new Run that supersedes the prior one.

### Dispatch
**Shape:** Verb. Implemented as `/dispatch` slash command.
**Location:** `metaensemble/commands/dispatch.md` defines the command surface.
**Analog:** `kubectl apply`, `dbt run`, scheduling a job on a queue.

The act of launching N Executors of a given Role for a given Task. N defaults to 1. Higher N opts into fan-out, consensus, or shadow patterns explicitly.

---

## Communication and contracts

### Brief
**Shape:** JSON object validated against `schemas/brief.schema.json`.
**Location:** Inline JSON inside the Agent/Task tool prompt. The runtime contract does not give MetaEnsemble a hook point at which to persist Briefs to disk, so v0.1 keeps them in-context. The `brief_in_path` / `brief_out_path` columns on the `runs` table and the `<project>/.metaensemble/briefs/` directory exist as forward-compatibility for a future persistence layer; both are unused in v0.1.
**Analog:** gRPC request, OpenAPI request body, typed RPC payload.

The wire-format message between Executors. Terse, machine-targeted, schema-validated. Carries: Executor IDs (from / to), Task ID, context (prior Run IDs, file pointers), expected output shape, model tier, budget.

Briefs are not English. They are JSON. Receiving Executors parse Briefs, not prose.

### Manifest
**Shape:** YAML file validated against `schemas/manifest.schema.yaml`.
**Location:** `.metaensemble/manifests/<manifest_id>.yaml`
**Analog:** dbt manifest, OpenAPI contract, dataset card, Parquet schema.

A handoff contract between Executors or between phases of a Task. Lists: typed file pointers (path + line range + role), expected deliverables (with structural assertions like `must_export`, `coverage`), model tier, budget, acceptance criteria.

A Manifest replaces what other systems do as free-form prose context-injection. The receiving Executor reads only the Manifest, never re-searches.

### Deliverable
**Shape:** Markdown file. Full English prose. No compression.
**Location:** `<report_root>/<category>/<name>-<date>.md` per category convention. The `report_root` is read from the project's `install-decisions.yaml`: `.metaensemble/reports` for greenfield projects (machine-local, ignored), or an existing convention such as `.claude/reports` when inspection detected one.
**Analog:** Pull request description, ML experiment report, design doc.

The human-readable output of a Run. Targeted at the Principal and at institutional memory. Linked from the Run row in the Ledger; surfaced through the Registry.

The Deliverable channel is full prose because humans read it. The Brief channel is terse JSON because machines parse it. They are produced together by the same Run. They are not the same artifact at different compression tiers.

---

## State and observability

### Ledger
**Shape:** Two-store: SQLite `runs` table (live, queryable) + `runs.jsonl` (append-only, replayable).
**Location:** `.metaensemble/state/department.db` + `.metaensemble/state/runs.jsonl`
**Analog:** MLflow tracking, event log, audit trail.

The append-only record of every Run. Every Run lands in both stores at PostToolUse. SQLite serves queries (`/ledger top-burn this-week`); JSONL serves replay if the database is lost or migrated.

### Registry
**Shape:** Logical view assembled from `executors`, `tasks`, and recent `runs`.
**Location:** Computed from SQLite on demand; surfaced via `/executors`, `/standup`.
**Analog:** Service mesh control-plane view, Kubernetes `kubectl get pods`.

The current-state snapshot. Lists live Executors (status, last seen, last Run), open Tasks, dependencies, recent Deliverables. The Registry is what `/standup` renders.

The Registry is computed, not stored separately. The Ledger and the `executors` / `tasks` tables are the source of truth.

---

## Lifecycle and runtime

### Hook
**Shape:** Python script, no model calls.
**Location:** `metaensemble/hooks/`
**Analog:** Kubernetes admission controller, CI lifecycle script, git hook.

Code that fires on an agent runtime lifecycle event (SessionStart, PreToolUse, PostToolUse, SubagentStop, Stop). Hooks enforce schemas, log Runs, check budgets, finalize background-dispatched Runs, render summaries. They do not call models. They do not block on network I/O.

### Window
**Shape:** A 5-hour rolling token allocation imposed by the agent runtime's subscription tier.
**Location:** Tracked by `window_id` on every Run; queried via the `/limits` command.
**Analog:** API rate-limit window, SLO error budget, monthly cap.

The fundamental scarce resource. MetaEnsemble's PreToolUse hook reads the live window state and enforces run-size warnings as a percentage of total window capacity, plus separate headroom warnings when the remaining window percentage gets low.

### Cost gate
**Shape:** Logic implemented in the PreToolUse hook plus configuration in `~/.metaensemble/budgets.yaml`. Authoritative capacity comes from the runtime's `rate_limits` feed (see *Runtime rate-limit feed* below).
**Location:** `metaensemble/hooks/pre_task.py`, `metaensemble/lib/cost_gate.py`, `~/.metaensemble/budgets.yaml`
**Analog:** SRE error-budget gate, AWS Budget alarms, K8s admission webhook.

A two-axis escalation pattern (auto / notify / block) on every dispatch. *Axis 1 — run size* compares the dispatch's estimated tokens to the window capacity. *Axis 2 — window headroom* compares the remaining percentage of the window to a warn and a block threshold. The final state is the worst of the two axes. Irreversible actions and novel Manifest patterns are independent hard-blocks on top. The Coordinator checks the window state proactively before composing a dispatch and brings the Principal in *before* invoking Agent when an escalation is likely, so the Principal sees plain-English options rather than a hook-error rendering. See ARCHITECTURE §9.

### Runtime rate-limit feed
**Shape:** JSON payload Claude Code v2.1.80+ pipes to the configured statusline script on every refresh.
**Location:** Captured by `metaensemble/statusline/me_status.py` to `~/.metaensemble/state/runtime-rate-limits.json`; read by `metaensemble.lib.native_state`.
**Analog:** Anthropic API rate-limit response headers, made available at the runtime layer.

The runtime's own view of the user's plan limits — 5-hour and 7-day window `used_percentage`, `resets_at`. The cost gate consults this feed as the authoritative source of window state, deriving capacity from `observed_burn / (used_percentage / 100)` rather than asking the user to configure their plan's cap by hand. When the feed is stale (older than 5 minutes) or absent, the cost gate falls back to the manual `window_capacity_tokens` setting and `/limits` and `/standup` surface the uncertainty explicitly rather than fabricating a percentage.

### Dispatch fan-out, consensus, shadow
**Shape:** Multi-instance dispatch patterns. See [ARCHITECTURE.md §11](./ARCHITECTURE.md#11-multi-instance-patterns).
**Analog:** Map-reduce (fan-out), Paxos voting (consensus), shadow deployment (shadow).

Three opt-in patterns for launching multiple Executors from one Role. Each produces multiple Runs sharing a `parent_task_id` and lineage in the `parent_executor_id` field.

---

## Portability

### Core
**Shape:** Directory inside the MetaEnsemble repo.
**Location:** `metaensemble/`
**Analog:** Kustomize base, dbt package, npm library.

Project-agnostic substrate. Contains schemas, hooks, library code, base Roles, slash commands. Ships with MetaEnsemble. Receives no project-specific knowledge — that lives in the Project layer.

### User layer
**Shape:** Directory in the engineer's home.
**Location:** `~/.metaensemble/`
**Analog:** VSCode user settings, dbt profiles directory, `~/.aws/config`.

Per-engineer overrides: budget thresholds, personal slash commands, model-tier preferences. Optional. Merges between Core and Project.

### Project layer
**Shape:** Directory inside any project that adopts MetaEnsemble.
**Location:** `<project>/.metaensemble/`
**Analog:** VSCode workspace settings, project `.dbt/`, in-repo `.github/`.

Project-specific Roles, Manifests, skills, starter packs. Wins on conflicts with User and Core. This is where domain knowledge lives.

### `metaensemble init`
**Shape:** CLI bootstrap command.
**Analog:** `dbt init`, `npx create-next-app`, `cargo new --lib`.

Creates the `<project>/.metaensemble/` skeleton. In v0.1.0 the optional `--pack <name>` flag is accepted only to explain that starter packs (`ml`, `web`, `data`) are reserved for v0.2.0; it does not pre-fill Roles yet. The same skeleton is also created idempotently by `metaensemble adopt`, so explicit init is optional.

### `metaensemble doctor`
**Shape:** CLI diagnostic command.
**Analog:** `npm doctor`, `brew doctor`, `git fsck`.

Nine checks the user runs after install (or whenever something feels off). `C1` and `C6` are legacy in v0.1.0 and return `SKIP`. The active checks validate that the hook commands in `~/.claude/settings.json` resolve to existing scripts (`C2`), that the JSON schemas compile (`C3`), that the project's state directory is initialized (`C4`), that the hook error log is healthy (`C5`), that the runtime's rate-limit feed is wired up and freshly captured (`C7`), that slash commands are not duplicated across `~/.claude/commands/` and `~/.claude/commands/metaensemble/` (`C8`), and that the vendored runtime at `~/.metaensemble/runtime/` is a valid symlink into a version dir with a verifying MANIFEST and an executable runner (`C9`). The `--fix` flag applies safe remediations.

### Survey decisions
**Shape:** YAML file at `<project>/.metaensemble/install-decisions.yaml`.
**Analog:** dbt project file, Helm values, Kustomize patches.

The user's editable choice surface for per-agent and per-Role handling. Every agent the inspection finds gets one entry — `name`, `kind` (one of `collision`, `user_unique`, `curated_relevant`, `curated_optional`), `action` (one of seven actions scoped by kind), and a `recommendation` comment. The installer reads this file and honors every choice. Nothing the user authored is silently converted; the default for every collision is to keep the user's agent.

### Agent shim
**Shape:** Markdown file at `~/.claude/agents/<name>.md` left by the installer.
**Analog:** Linker stub, façade pattern.

When an agent is converted (action `take_ours` or `convert`), the installer writes a thin shim at the original path so the agent runtime's `Agent(subagent_type="<name>")` keeps resolving. The shim mirrors the original agent's frontmatter (name/description/tools/model) and has a body noting that the full spec lives at the corresponding Role file and that `/dispatch` is the richer dispatch path.

### Runner (`me-run`)
**Shape:** Shell script at `~/.metaensemble/runtime/bin/me-run`, generated atomically inside a versioned runtime directory under `~/.metaensemble/runtime-versions/<id>/bin/me-run`.
**Analog:** Console-script wrapper, `npx`-style launcher.

Invocation path for the `metaensemble` CLI from hooks and slash commands. Pins one absolute Python interpreter (shell-quoted via `shlex.quote`, so paths with spaces are safe) and execs `python -m metaensemble.cli "$@"`. Generated by `metaensemble user-setup` as part of the atomic vendor step — written into a new `~/.metaensemble/runtime-versions/<id>/` directory along with the rest of the runtime assets, MANIFEST-verified, then exposed via `os.replace` of the `~/.metaensemble/runtime` symlink. Re-running `user-setup` always re-vendors (so `pip install --upgrade metaensemble` never leaves stale assets in front of the runner); the GC keeps the last two valid versions plus whatever the symlink currently points at.

### BLOCK sentinel
**Shape:** JSON file at `<project>/.metaensemble/state/blocks/<session>-<ts>.json`.
**Analog:** Outbox pattern, sidecar file.

Persisted record of a cost-gate BLOCK decision. Carries `reason`, `estimated_tokens`, `estimated_pct_of_window`, `manifest_id`, `state` (`block`), the three structured options (`approve at current tier`, `drop tier and retry`, `split into smaller Manifests`), and `default: paused`. The Coordinator reads the most recent sentinel after a blocked dispatch and surfaces the options to the Principal — the runtime's "hook error" rendering does not pass the structured options through, so the sentinel is the recovery path. v0.2 will surface the options directly in `systemMessage`.

### NOTIFY sentinel
**Shape:** JSON file at `<project>/.metaensemble/state/notifies/<session>-<ts>.json`.
**Analog:** Same shape as the BLOCK sentinel.

Persisted record of a cost-gate NOTIFY decision. Same fields as the BLOCK sentinel with `state: notify` and `default: proceed`. The dispatch proceeds while the Coordinator surfaces the options so the Principal can intercept. Introduced in v0.1.0 alongside the grammar change that put structured options on both NOTIFY and BLOCK rather than BLOCK alone.

### Python deliverable check
**Shape:** PostToolUse evaluation in `metaensemble/hooks/post_task.py`, backed by `metaensemble/lib/quality_gate.py` and `metaensemble/lib/quality_runners.py`.
**Analog:** SonarQube quality gate, Snyk PR check, GitHub Advanced Security branch ruleset.

Five-axis Python check that runs after a successful Run whose Manifest declared Python deliverables. The implementation module is still named `quality_gate` for compatibility, but the v0.1.0 product scope is intentionally narrower than a universal quality gate. Axes: *correctness* (pytest), *security* (bandit), *maintainability* (ruff issue count mapped to SonarQube A–E), *complexity* (radon McCabe), *coverage* (coverage.py absolute floor). Worst axis sets the state; on NOTIFY or BLOCK the Coordinator surfaces a four-option Principal surface (`accept`, `peer review`, `re-dispatch with stricter`, `split`). Thresholds anchor to industry-standard sources documented in `metaensemble/config/quality.example.yaml`. Each runner skips gracefully when its underlying tool is not installed, so the check degrades to a partial check rather than failing closed.

### Extras (Manifest)
**Shape:** Open-shape `extras: {}` map at the top level of a Manifest.
**Analog:** `metadata` block in Kubernetes, `x-` prefixed properties in OpenAPI.

Reference material the receiving Executor consults but the contract does not verify. Examples: rationale strings, design-token tables, hero copy, KPI specifications. Anything that belongs to the typed contract (file pointers, expected deliverables, acceptance criteria, constraints, peer-review policy) goes in the typed top-level fields, not in `extras`.

### `metaensemble export-agents`
**Shape:** CLI recovery command.
**Analog:** `dbt clean`, `terraform destroy`, package-eject.

Reverse-converts MetaEnsemble Roles back into Claude Code agent files. The documented escape hatch when the install backups directory is missing or the user wants to abandon MetaEnsemble while keeping the agents that were converted into it. Mapping is mechanical (inverse of `convert_agent_to_role`); body preserved verbatim.

---

## Cross-cutting concepts

### Window budget
A specific quantity of tokens reserved for a Task or workflow, declared in the Manifest and enforced by the PreToolUse hook. Budgets are advisory at the soft threshold and binding at the hard threshold.

### Model tier
One of `opus`, `sonnet`, `haiku`. Declared in the Role spec; overridable per Run via Manifest constraints. Tiering is the largest single budget lever in MetaEnsemble.

### Output style
The format an Executor writes in for a given output. Two styles ship: `wire` (terse JSON for Briefs) and `deliverable` (full English Markdown for human reports). Executors produce one Brief and one Deliverable per Run.

### Alias
A short, human-typeable name for an Executor. Format: `<role-prefix>-<3-hex>`, e.g. `arch-7b3`, `be-9c1`. Auto-generated at Executor creation. Unique within the registry. Stable for the Executor's lifetime.

### Lineage
The graph formed by `parent_executor_id` (Executor spawned from another) and `parent_task_id` (Task spawned from another). Lineage is how the Registry reconstructs fan-out / consensus groups.

### Replay
The act of reconstructing SQLite state from `runs.jsonl`. Used after a database corruption or migration. Replay is deterministic; running it twice produces identical SQLite content.

---

## Recovery and provenance (v0.1.0 additions)

These terms cover the v0.1.0 recovery surface: closing Runs the live hooks could not finish, and the per-Run provenance the post-task hook now persists. Schema source of truth: `metaensemble/state/migrations/002_outcome_extended.sql` (CHECK constraint extension) and `metaensemble/state/migrations/003_run_provenance.sql` (additive columns).

### Reconcile (verb / CLI command)
**Shape:** `metaensemble reconcile [--older-than-minutes N] [--dry-run]` plus the Stop-hook and session-start automatic sweeps.
**Analog:** systemd `journalctl --vacuum`, dead-letter requeue, database recovery log.

Walk `<state>/pending/` and write a failed Run row for every stranded sidecar — the residue of a dispatch whose PostToolUse hook never fired (`kill -9`, budget kill, runtime crash). Two layers: Layer 1 runs from the Stop hook for sidecars belonging to the ending session; Layer 2 runs from the CLI command and from the session-start hook (1-hour threshold) for sidecars regardless of session. Writes `outcome="interrupted"` by default, upgrading to `outcome="budget_exceeded"` when the parent transcript supplies budget-kill evidence.

### Pending sidecar
**Shape:** JSON file at `<project>/.metaensemble/state/pending/<run_id>.json`.
**Analog:** Two-phase commit prepared record, outbox row awaiting confirmation.

Written by the PreToolUse hook with the executor, task, role, model tier, started_ts, window, manifest pointer, and estimated input tokens. Deleted by PostToolUse on a clean Run completion. A sidecar that survives both PostToolUse and the Stop hook is "stranded" and is recovered by the reconcile module (see above).

### `interrupted` (outcome)
**Shape:** Value of the `runs.outcome` column.

A Run that did not complete because its PostToolUse hook never fired. Written by the reconcile module when no other evidence is available. The `failure_reason` distinguishes Layer 1 (`"session ended before PostToolUse"`) from Layer 2 (`"stale sidecar reconciled by metaensemble"`).

### `budget_exceeded` (outcome)
**Shape:** Value of the `runs.outcome` column.

A Run whose parent `claude --max-budget-usd` invocation exited because the budget was exhausted. Today, v0.1.0 reconciles such Runs as `interrupted` and upgrades to `budget_exceeded` only when transcript evidence of the budget kill is present. The schema accepts both values via the 002 migration; the detection path is documented as a remaining v0.2.0 sharpening in `SYSTEM-CARD.md`.

### `requested_model_tier`
**Shape:** TEXT column on `runs`.

The model tier the Coordinator's Manifest declared for this Run, captured by the PreToolUse hook from `constraints.model_tier`. The companion `model` column records the runtime-observed model when transcript walking succeeds (so an explicit `claude --model haiku` invocation can be distinguished from the manifest's `sonnet` request). When the transcript walker cannot recover a runtime model, `model` falls back to the same value as `requested_model_tier`.

### `deliverable_ref` / `deliverable_ref_json`
**Shape:** TEXT column on `runs` holding a JSON document; built by `metaensemble/lib/recording.py:build_deliverable_ref`.

Structured reference to the Deliverable a Run produced. Replaces the strict `deliverable_path` contract (which required a Markdown report under `reports/`) with three honest shapes:

- `{"kind": "path",    "value": "<path>", "inferred": <bool>}` — a Markdown report path. `inferred` is true when the path came from regex over the tool response rather than from a Coordinator sentinel.
- `{"kind": "summary", "value": "<first 500 chars>", "len": <total chars>}` — short text deliverable (an answer, a review summary) with a length stamp.
- `{"kind": "hash",    "value": "sha256:<hex>"}` — opaque deliverable (e.g. a pure code edit) digested from the touched-file set.

A Run that produced no meaningful output stores `NULL`. The Principal reads this column when they want the Ledger to be honest about what kind of artifact a Run actually produced.

### `files_touched_json` / `tool_use_json`
**Shape:** TEXT columns on `runs` holding JSON arrays; populated by the transcript walker in `metaensemble/lib/transcript.py` and/or by the `file_event.py` hook.

`files_touched_json` is a JSON array of file paths the Executor wrote or edited during the Run; `tool_use_json` is a JSON array of `{name, count, input_tokens}` objects, one per tool the Executor invoked. Both are sourced from the runtime's session JSONL when the post-task hook can resolve a transcript path; the file-event hook supplies a fallback for the file paths. The Ledger holds them as the documented "tool use" and "files touched" provenance.

### `review_findings_json`
**Shape:** TEXT column on `runs` holding a JSON document.

Aggregates the quality-gate findings (same content as `quality_findings_json`) plus any peer-review verdicts on this Run, in one place. The Principal consumes this column when reviewing a Run's audit trail without joining across other tables.

### `cache_read_tokens` / `cache_create_tokens`
**Shape:** INTEGER columns on `runs`, default 0.
**Analog:** Anthropic prompt-cache hit / fill counters.

Anthropic prompt-cache read and creation tokens for the Run, extracted from the transcript walker's `usage` blocks. Surfaced by `metaensemble perf` so Principals can see whether MetaEnsemble's orchestration overhead is being amortized by prompt caching. Zero when no transcript is available.

### `orchestration_tokens`
**Shape:** INTEGER column on `runs`, default 0.

Tokens spent on MetaEnsemble orchestration (Manifest YAML, Brief composition, hook payloads) separately from the Executor's output tokens. The token-economics axis of the evaluation harness — `orchestration_overhead_ratio` — uses this column to expose whether the overhead is acceptable per the W11 budget.

### `metaensemble eval`
**Shape:** CLI command. Three tiers: `replay`, `smoke`, `full`.
**Analog:** `pytest --replay`, `dbt test`, ML benchmark runner (e.g. lm-eval-harness).

The evaluation harness in `evals/`. `replay` reads JSONL cassettes and costs nothing — the PR gate. `smoke` runs one seed of the classification smoke set live against Claude Code with tools disabled (no project mutation). `full` is release-gated and requires `--allow-live` plus signed-off thresholds for D-8 (orchestration-overhead ceiling) and D-9 (failed-run waste ceiling) before its results gate a "ship" decision. See `evals/README.md` and `SYSTEM-CARD.md` for the calibration caveats around the smoke fixture.
