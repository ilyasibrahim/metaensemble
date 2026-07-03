# MetaEnsemble

**Stable identity, typed contracts, and observable runs for ensembles of cognitive agents.**

MetaEnsemble gives every agent a persistent ID, every handoff a schema-validated contract, and every run an entry in an append-only ledger. Multiple agents instantiated from one Role specification execute in parallel. Identities survive across sessions. Token-efficient by construction.

**v0.2.0 status:** feedback-first release. The software records and gates local agent work, but measured quality-per-token improvements remain a product hypothesis until the live evaluation set is larger and fully baseline-comparable. See [SYSTEM-CARD.md](./docs/SYSTEM-CARD.md).

---

## Why MetaEnsemble exists

Coordinating multiple cognitive agents tends to fail in the same three places:

1. **No stable identity.** Each agent invocation is anonymous. No way to say "follow up with the same Executor next week."
2. **No typed handoffs.** Context passes between agents as free-form prose. Every receiver re-derives state by searching, re-reading, re-grepping.
3. **No observability.** Token spend, model choice, outcome — nothing captured per run. Optimization is guesswork.

MetaEnsemble fixes all three at the substrate, not as features. Every primitive in the system carries an ID, every transport is schema-validated, every execution lands in the Ledger.

---

## What MetaEnsemble gives you

- **Persistent identities.** Every Executor has a UUIDv7 and a short alias (`arch-7b3`). Resume any past Executor across sessions with `/relaunch arch-7b3`.
- **Typed contracts.** Handoffs travel as YAML Manifests validated against a JSON Schema. Inter-Executor messages travel as terse JSON Briefs. No prose context-injection, no re-search on the receiving side.
- **Observable runs.** Append-only Ledger (SQLite live, JSONL mirror for replay) records every Run with token cost, requested model tier, runtime-observed model when available, outcome, and links to its Deliverable.
- **MetaEnsemble dispatch.** Spawn N Executors from one Role spec for parallel hypothesis exploration, consensus review, or fan-out implementation. Default is N=1; multi-instance is opt-in and currently validated at the planning/protocol layer.
- **Cross-session continuity.** An Executor's identity is a Ledger row, not a live process. Relaunch is cheap (last Brief + last Deliverable summary) by default, deep (`--full`) when needed.
- **Two-channel design.** Machine-to-machine traffic (Briefs) stays terse and structured. Human-facing output (Deliverables) stays full English. Same Run produces both. No "compression tier" knob to misset.
- **Threshold-based cost gating.** The Coordinator auto-decides cheap, reversible work. It surfaces only the calls that warrant Principal judgment, in a structured options table — never as conversational back-and-forth.

---

## Primitives

| Term | Shape | What it is |
|---|---|---|
| **Principal** | The human running the system | The person who dispatches work and approves above-threshold decisions. Maps to the IAM Principal concept. |
| **Coordinator** | The main agent in the active session | Plans Tasks, dispatches Executors, validates contracts, synthesizes Deliverables. Maps to the Kafka / ZooKeeper / Cassandra coordinator pattern. |
| **Role** | Markdown file with frontmatter spec | The Job Description. Declarative, versioned. Maps to a Kubernetes Deployment spec or IAM Role. |
| **Executor** | Row in the registry, identified by UUIDv7 + alias | A live instance of a Role. Multiple per Role per Task. Survives sessions. Maps to a Spark Executor or K8s Pod. |
| **Task** | Unit of work | What the Principal asks the ensemble to do. Has dependencies, expected deliverables, budget. |
| **Run** | Row in the Ledger | One execution attempt by one Executor for one Task. Maps to an MLflow run. |
| **Brief** | Schema-validated JSON | Wire-format message between Executors. Terse, machine-targeted. |
| **Manifest** | Schema-validated YAML | Handoff contract. Typed pointers to files, line ranges, schemas, prior runs. Maps to a dbt or OpenAPI manifest. |
| **Deliverable** | Markdown report | Human-readable output. English prose. Institutional memory. |
| **Ledger** | SQLite + JSONL mirror | Append-only log of every Run. Queryable, replayable. Maps to MLflow tracking. |
| **Registry** | View over the Ledger + Executor table | Current-state snapshot. Live Executors, open Tasks, dependencies. Maps to a service-mesh control-plane view. |
| **Dispatch** | Verb / slash command | The act of launching N Executors of a Role for a Task. |

---

## High-level flow

```
                ┌─────────────────────┐
                │      Principal      │  (you)
                └──────────┬──────────┘
                           │ intent
                ┌──────────▼──────────┐
                │     Coordinator     │  plans, dispatches, synthesizes
                └─────┬────────┬──────┘
                      │        │
        ┌─────────────┘        └───────────┐
        │                                  │
   ┌────▼─────────┐                ┌───────▼──────┐
   │ Role: backend│                │ Role: review │
   │  spec file   │                │  spec file   │
   └────┬─────────┘                └───────┬──────┘
        │  dispatch N=2                    │  dispatch N=3
   ┌────┴────┐                       ┌─────┼─────┐
   ▼         ▼                       ▼     ▼     ▼
 ┌─────┐  ┌─────┐                  ┌────┐┌────┐┌────┐
 │be-1 │  │be-2 │                  │rv-1││rv-2││rv-3│
 └──┬──┘  └──┬──┘                  └─┬──┘└─┬──┘└─┬──┘
    │ Brief  │ Brief                 │     │     │
    ▼        ▼                       ▼     ▼     ▼
 ┌────────────────────────────────────────────────┐
 │              Ledger (SQLite + JSONL)           │
 └────────────────────────────────────────────────┘
                          │
                          ▼
                  ┌──────────────┐
                  │ Deliverables │  English, for humans
                  └──────────────┘
```

A single `/dispatch` produces N Executors across one or more Roles. Each Executor emits a Brief downstream and a Deliverable upstream. Every Run is logged. The Principal sees Deliverables and the standup view; never the wire traffic.

---

## Why two channels

A single Run produces two artifacts:

- The **Brief** is what the next Executor receives. Terse JSON. Schema-validated. Machine-targeted. Cheap to emit, cheap to parse.
- The **Deliverable** is what you, the Principal, read. Full English. Prose. Institutional memory.

These are not intensity tiers. They are different artifacts for different audiences, produced together. The receiving Executor does not parse English; the human does not parse JSON. Each gets the format that earns its place.

---

## How it runs

MetaEnsemble runs entirely on your laptop. Clone the repo, drop the conventions into your local agent runtime configuration, and dispatch. No servers, no cloud accounts, no hosting. Your Ledger, your Executors, your Briefs all live on your filesystem. State is portable: copy the repo and the state directory, and MetaEnsemble runs anywhere the agent runtime is installed.

---

## Adopting MetaEnsemble in your project

MetaEnsemble is project-agnostic by design. Three layers, with project-specific knowledge confined to the project layer:

```
metaensemble/                           # shipped with MetaEnsemble; project-agnostic
~/.metaensemble/                # per-engineer preferences; the vendored runtime (runtime/, runtime-versions/); the runner at runtime/bin/me-run
<your-project>/.metaensemble/   # project-specific state, manifests, and install decisions
```

The adoption flow has two layers, asked separately:

```bash
metaensemble setup               # interactive wizard: picks a project, asks for layout, runs the two steps below
```

The wizard lists every Claude Code project on this machine, lets you pick one, asks once for the layout (namespaced or top-level), and then runs `user-setup` and `adopt` in sequence. The two underlying commands are explicit if you prefer them:

```bash
metaensemble user-setup --layout=namespaced    # once per machine: vendors runtime to ~/.metaensemble/runtime/, wires commands/hooks/statusline
# or
metaensemble user-setup --layout=top-level # same, but slash commands install top-level under ~/.claude/commands/

metaensemble adopt                         # per project: writes <project>/.metaensemble/ and honors install-decisions
```

`user-setup` is global (one layout for the whole machine; re-run with a different layout to switch). `adopt` is per-project and portable — run it once per project you want to register.

The inspection is the load-bearing piece. It writes two files into `<project>/.metaensemble/`:

- A short Markdown report naming what was found, what we recommend, and why.
- `install-decisions.yaml`, the editable choice surface. Every agent in your setup and every curated Role MetaEnsemble ships gets one entry with a sensible default. It also records the project's memory surfaces (`CLAUDE.md` and friends) so dispatch contracts hand Executors your existing project memory instead of rebuilding it. Read once, edit only what you disagree with.

Per-agent decisions span four cases (`collision`, `user_unique`, `curated_relevant`, `curated_optional`) and seven actions (`keep_yours`, `take_ours`, `keep_both`, `preserve`, `convert`, `activate`, `retire`). The installer reads the file and honors every choice. Nothing the user authored is silently converted; the default for every collision is to keep the user's agent.

Recovery mirrors the install split. `metaensemble unadopt` reverses one project's adoption: it walks `<project>/.metaensemble/backups/` in reverse, reverses project-scope actions, strips the managed `.gitignore` block, and leaves user-level integration intact. `metaensemble user-teardown` reverses `user-setup` by removing managed `~/.claude/` symlinks and hook entries. Each command accepts `--purge-state` for the matching `.metaensemble/` directory. For a full local rollback after live testing, run `metaensemble reconcile --older-than-minutes 0` first so stranded pending Runs are written to the Ledger, then run `metaensemble unadopt --purge-state` from the project root and `metaensemble user-teardown --purge-state` from anywhere. `metaensemble export-agents` reverse-converts MetaEnsemble Roles back to Claude Code agent files, even when the install's backups directory is missing. Every contract above is tested.

Starter packs (`--pack ml`, `--pack web`, `--pack data`) are planned for a future release.

If your project lives in an iCloud-synced directory (e.g., `~/Desktop/` with iCloud Desktop & Documents Sync enabled), consider excluding `.venv/` from iCloud sync. iCloud's conflict-resolution against rapid `pip install` file churn produces phantom duplicate files in `site-packages`; MetaEnsemble filters them correctly but they consume iCloud quota and slow installs. `metaensemble doctor` C11 surfaces this state as a WARN with remediation. The same caveat applies more strongly to `.metaensemble/state/`: when iCloud places `department.db` into a dataless placeholder state, SQLite's `open()` can fail intermittently and PreToolUse hooks surface as `Agent hook error` with no stderr. The robust fix is to host active MetaEnsemble projects outside iCloud-synced paths, or exclude the project from iCloud Drive. `metaensemble doctor` C4 names this cause when it detects the layout. See [USER-GUIDE.md — When something feels off](./docs/USER-GUIDE.md) for the troubleshooting recipe.

See [DEPLOYMENT.md](./docs/DEPLOYMENT.md) for the per-action behaviour and the full reference. See [ARCHITECTURE.md §4 — Portability](./docs/ARCHITECTURE.md) for the layering, merge order, and the hard rule that keeps Core project-agnostic.

---

## Status

v0.2.0. All core phases complete and tested:

- Typed substrate (Manifest YAML, Brief JSON, Ledger SQLite + JSONL).
- Lifecycle hooks for SessionStart, PreToolUse, PostToolUse, Write/deliverable-sync, file-tool provenance, SubagentStop (background-dispatch finalization), and Stop, with command-injection invariants enforced by an audit test.
- Principal-facing surface: seven slash commands plus CLI subcommands including `metaensemble setup`, `metaensemble user-setup`, `metaensemble adopt`, `metaensemble unadopt`, `metaensemble user-teardown`, `metaensemble reconcile`, `metaensemble eval`, and `metaensemble projects`.
- Multi-instance patterns (fanout / consensus / shadow / peer-review) with the `N ≥ 2` guard enforced deterministically by the PreToolUse marker hook.
- Installer with idempotent re-runs, explicit purge modes, and a residue report after every uninstall.
- Five-axis deliverable check on successful Runs: pytest, bandit, ruff, radon, and coverage for `.py` deliverables, plus project-configured per-axis commands (`axis_commands` in `quality.yaml`) so non-Python deliverables are checked across the same correctness/security/maintainability/complexity/coverage axes; quality runners ship in the `[test]` extras so CI runs the real tools.
- Failed-run accounting via the `interrupted` and `budget_exceeded` outcomes (schema migration 002) plus the two-layer reconcile module.
- Ledger field completeness — every documented Ledger field (Role version, model, tool use, files touched, output, gate state, review findings) is a column with an assertion test.
- Evaluation harness under `evals/` with replay/smoke/full tiers, Wilson confidence intervals, and `pass@budget` / `quality_per_1k_tokens` / `orchestration_overhead_ratio` metrics. The shipped replay pack is a non-empirical bootstrap fixture. Live smoke/full runs are wired for side-effect-free classification-smoke checks; calibration and baseline-superiority claims still require larger labeled/fixture sets.

v0.2.0 is feedback-first. Issues are welcome; see [CONTRIBUTING.md](./CONTRIBUTING.md) to get started.

See [PERFORMANCE.md](./docs/PERFORMANCE.md) for the engineering contract and benchmark numbers, [SYSTEM-CARD.md](./docs/SYSTEM-CARD.md) for known limitations and intended-use boundaries, and [SECURITY.md](./SECURITY.md) for the trust model.
Release publication is gated by [RELEASE-CHECKLIST.md](./docs/RELEASE-CHECKLIST.md).

---

## Where to start

- **[ARCHITECTURE.md](./docs/ARCHITECTURE.md)** — the layered design, the data model, the lifecycle, what MetaEnsemble is and is not.
- **[USER-GUIDE.md](./docs/USER-GUIDE.md)** — a friendly Principal guide for day-one users.
- **[PERFORMANCE.md](./docs/PERFORMANCE.md)** — the binding engineering contract: token budgets, time budgets, query rules, and CI-gated benchmarks. Required reading before changing performance-sensitive code.
- **[RELEASE-CHECKLIST.md](./docs/RELEASE-CHECKLIST.md)** — artifact, security, installer, and live-eval gates for publishing a release.
- **[GLOSSARY.md](./docs/GLOSSARY.md)** — every term defined precisely, every analog named.

---

## Operating principles

Three values drive every design choice in MetaEnsemble:

1. **Conserve the budget.** The constraint is window exhaustion, not dollars. Per-Executor model tiering, terse wire format, schema-driven handoffs that eliminate re-search — all designed to fit more useful work in fewer tokens.
2. **Move fast.** Parallel dispatch is a primitive, not a workaround. Hooks fire on lifecycle events automatically. The Principal never types boilerplate.
3. **Hold the line on quality.** Speed and budget never come at the cost of standards. The schema layer enforces correctness; the Ledger enforces accountability; the Deliverable channel preserves institutional memory at full fidelity.

If a proposed feature compromises any of these three, it does not ship.
