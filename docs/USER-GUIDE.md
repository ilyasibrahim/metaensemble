# User Guide

*How to operate MetaEnsemble — in five minutes, in thirty minutes, or as long as you want.*

---

## What you are looking at

MetaEnsemble turns your agent runtime into a small department of named specialists that you, the **Principal**, direct. The mental model is the one your working life already runs on: you act as a CEO, the **Coordinator** (the main agent in your session) acts as your chief of staff, and the **Executors** are the specialists the Coordinator dispatches under your direction. This guide is for the moment when you want to know what to do tomorrow morning.

If you remember nothing else, remember this: you speak English to the Coordinator, the Coordinator handles everything in between, and you read English Deliverables when work is done. The system writes JSON between Executors, validates schemas, writes to a Ledger, runs hooks, and so on, but none of that is in your daily flow.

### Two scopes: user-level and project-level

MetaEnsemble keeps its state in two places, by design.

- **`~/.metaensemble/`** is **user-level** and shared across every project on your machine. It holds the vendored runtime — a symlink at `runtime/` pointing into a versioned directory under `runtime-versions/<id>/`, with the runner at `runtime/bin/me-run` — your converted user-layer Roles, your default `budgets.yaml`, install records under `installs/`, and the latest snapshot of the runtime's rate-limit feed.
- **`<project>/.metaensemble/`** is **project-level** and isolated per project. It holds that project's Ledger (`state/department.db`), its Manifests, its `active-roles.yaml`, its `install-decisions.yaml`, backups, hooks log, and any block sentinels.

Installation has two layers that are now asked separately. The wizard `metaensemble setup` lists every Claude Code project on this machine, lets you pick one, asks for layout once, and invokes the two explicit commands in sequence. Long form: `metaensemble user-setup --layout={namespaced|top-level}` configures the user-level pieces (vendors the runtime, generates `runtime/bin/me-run`, writes hooks into `~/.claude/settings.json`, installs slash commands and the statusline) and is invoked once per machine; `metaensemble adopt` registers a specific project and writes its `.metaensemble/`. `metaensemble projects` (invokable from any directory) shows which projects on your machine have MetaEnsemble installed — it lists every project Claude Code has seen, with run counts and last-run timestamps for the ones with active Ledgers.

**Important: layout is not scope.** `namespaced` and `top-level` only choose the user-level `~/.claude/` placement: namespaced commands such as `/metaensemble:dispatch` versus top-level commands such as `/dispatch`. They do not mean "install only globally" or "install only into this project." A working setup always has both layers: `user-setup` once per machine, then `adopt` for each project.

The installer adds `.metaensemble/` to the project's root `.gitignore` (creating one if your project does not have it yet), following the same convention as `.venv`, `node_modules`, and `dist/`. The whole per-project directory is treated as machine-specific working state — Ledger DB, manifests, backups, inspection output, hooks log, active-roles, and install-decisions are all derived from the local Claude Code agent inventory and runtime, so they are not portable across teammates anyway. Sharing dispatch configuration is done by sharing the metaensemble repo (curated Roles, the protocol skill, budgets template); each teammate runs `metaensemble user-setup` once on their machine and `metaensemble adopt` per project. The installer never overwrites an existing `.gitignore`: it appends a managed block only when `.metaensemble/` is not already listed, and it is idempotent across re-installs.

---

## Five-minute install and first run

If you want one command instead of five, run `metaensemble setup` after the pip install — the wizard walks you through layout choice, `user-setup`, and `adopt` for one project. The explicit flow below is what `setup` does under the hood; reach for it when you want to be in the driver's seat or scripting the install.

### Step 1 — Install MetaEnsemble

```bash
pip install metaensemble
```

`pip install` drops the `metaensemble` console script in your venv's `bin/` and installs the package into `site-packages/`. Step 2 (`metaensemble user-setup`) then vendors a self-contained copy of the runtime into `~/.metaensemble/runtime/` and generates the runner at `runtime/bin/me-run`; once that completes you can delete any source clone — the runtime is independent.

If you prefer to install from a clone (for development, or because you want to track `main`), use `pip install -e .` from the repo root instead. The flow below is the same; `user-setup` and `doctor` C12 will flag the editable install — see *When something feels off* item 11 for the recovery path.

**What the install configures, beyond the CLI**: a *statusline script*
that captures the runtime's rate-limit feed on every refresh. Claude
Code v2.1.80+ pipes the 5-hour and 7-day window usage (`used_percentage`,
`resets_at`) to the configured statusline command, and MetaEnsemble's
script writes that data to `~/.metaensemble/state/runtime-rate-limits.json`.
The cost gate, `/limits`, and `/standup` read from there so the
displayed window state matches what the runtime itself sees — you do
not need to configure your plan's window cap by hand.

### Step 2 — Configure user-level integration

```bash
metaensemble user-setup --layout=top-level
```

This runs **once per machine**. It vendors a self-contained snapshot of the runtime into `~/.metaensemble/runtime/` (a versioned directory with an atomic symlink swap), generates the runner at `~/.metaensemble/runtime/bin/me-run` pinned to the active Python interpreter, and writes MetaEnsemble's lifecycle hooks, statusline, slash commands, output styles, and the `metaensemble-protocol` skill into `~/.claude/`. After this step the dev source tree is no longer required for the install to function — the vendored runtime is independent.

`--layout=top-level` installs slash commands at the root of `~/.claude/commands/` (`/dispatch`, `/standup`, `/ledger`, …). Pick `--layout=namespaced` instead if you already have commands of those names and want MetaEnsemble's installed under `~/.claude/commands/metaensemble/` (`/metaensemble:dispatch`, etc.). You can switch later by re-running `user-setup` with the other flag — the symlinks are remapped atomically.

If you installed from a clone with `pip install -e .`, `user-setup` prints a notice naming the source path and a wheel-install recovery command, then continues. See troubleshooting item 11 to switch to a wheel install.

### Step 3 — Verify the install

```bash
metaensemble doctor
```

The doctor prints a Markdown digest of the install's health. With Steps 1 and 2 done, `C9 Runtime vendored` and `C12 Install topology` should both report `OK`. `C1` and `C6` are legacy in v0.1.0 and will show as `SKIP`. `C4 Project state directory initialized` will WARN until you complete Step 4 below from your project's root.

### Step 4 — Bootstrap your project

From inside the project you want to use MetaEnsemble in:

```bash
metaensemble init
```

This creates `.metaensemble/` with the Ledger database, the manifests directory, and a copy of the budget configuration template. Nothing about your project is touched outside that directory. **`metaensemble adopt` auto-runs `init` internally**, so running this step manually is optional — it exists so you can initialize project state without committing to an install yet, and so the doctor's `C4` check reports OK before you inspect.

### Step 5 — Survey, edit decisions, then install

Three commands. The inspection is read-only; it does not change your setup. It writes two files:

1. **`<project>/.metaensemble/inspection-<timestamp>.md`** — a short, opinionated Markdown report. It names what was found, what we recommend, and why. Roughly one screen of reading.
2. **`<project>/.metaensemble/install-decisions.yaml`** — your editable choice surface. Every agent in your setup, and every curated Role MetaEnsemble ships, gets one entry with a sensible default. Read it, change the lines you disagree with, save. The installer reads this file at install time.

```bash
metaensemble inspect
```

Now open `.metaensemble/install-decisions.yaml`. You will see entries shaped like this:

```yaml
agents:
  - name: backend          # exists in both your setup AND MetaEnsemble's curated set
    kind: collision
    action: keep_yours     # default; edit to take_ours or keep_both
    # collision options: keep_yours | take_ours | keep_both

  - name: data-engineer    # unique to your setup
    kind: user_unique
    action: preserve       # default; edit to convert if you want it managed by MetaEnsemble
    # user_unique options: preserve | convert

  - name: devops           # MetaEnsemble's curated Role; your project has CI signals
    kind: curated_relevant
    action: activate       # default; edit to retire if you do not want it dispatchable

  - name: frontend         # MetaEnsemble's curated Role; no signals in your project
    kind: curated_optional
    action: retire         # default; edit to activate if you want it anyway
```

The four `kind` values map to the four cases the inspection distinguishes:

- **`collision`** — same name lives in both places. Three choices:
  - `keep_yours` — your agent stays, MetaEnsemble's Role of that name is retired
  - `take_ours` — your agent is backed up and replaced by MetaEnsemble's Role
  - `keep_both` — your agent stays, MetaEnsemble's Role installs under a `-me` suffix (e.g. `backend-me`)
- **`user_unique`** — only in your setup. Default: `preserve` (the agent stays as-is). Switch to `convert` to make it a MetaEnsemble Role.
- **`curated_relevant`** — a curated Role whose project signals match your codebase. Default: `activate`.
- **`curated_optional`** — a curated Role with no project signals. Default: `retire`. Switch to `activate` if you want it on your roster anyway.

Project-signal detection is filesystem-based, not a semantic model of your repo. v0.1.0 covers Python ML, data engineering, web app, library, and infrastructure project archetypes via deterministic filesystem signals (no model calls). Detection is signal-based and additive — your project may match multiple Roles. Domain-specific agents you already have are discovered during inspection and can be preserved or converted into Roles; they do not need to be part of MetaEnsemble's default curated set. The inspection report includes a **Signal probe summary** section listing, for every curated Role, exactly which signals were probed and which fired, so you can see why a Role matched or didn't. If a Role you expect to match did not, edit `install-decisions.yaml` to override the default; the signal catalog can also grow in a v0.1.x point release.

When you have made your edits, install:

```bash
# Once per machine — pick a layout:
metaensemble user-setup --layout=namespaced       # safe default; existing setup untouched
metaensemble user-setup --layout=top-level    # slash commands install top-level (/dispatch)

# Per project — honors install-decisions.yaml:
metaensemble adopt
metaensemble adopt --dry-run                  # preview the per-project actions first
```

The two user-setup layouts differ in whether MetaEnsemble's own pieces (slash commands, output styles) install in namespaced subdirectories or at the top level. The per-agent and per-Role behaviour is driven by your `install-decisions.yaml`, not by the layout flag — your agents will be handled the way you said, regardless of which layout you pick.

Two output styles ship with the runtime. `wire` is terse JSON for Briefs passed between Executors; `deliverable` is Markdown for the final Principal-facing answer. In `namespaced` layout they install as `metaensemble-wire.md` and `metaensemble-deliverable.md`; in `top-level` layout they install as `wire.md` and `deliverable.md`.

Everything `adopt` does is backed up to `<project>/.metaensemble/backups/<timestamp>/` and reversible via `metaensemble unadopt`. The unadopt walks the full chain of installs in the project, restores converted agents from backup, strips the managed `.gitignore` block, and prints a residue report naming everything that survived. Pass `--purge-state` to also delete `<project>/.metaensemble/` entirely. To remove the user-level integration too, run `metaensemble user-teardown` (pass `--purge-state` to also delete `~/.metaensemble/`).

`adopt --dry-run` is a read-only preview. It names the inspection report, decisions file, project state, `.gitignore`, active-role write, and per-agent actions that a real adopt would touch, but it does not create inspection files or project state.

When an existing `install-decisions.yaml` is present, real `adopt` keeps your edited file and writes a timestamped `install-decisions.<timestamp>.yaml` beside it so you can diff your choices against the current defaults. Survey snapshots are rotated; MetaEnsemble keeps the newest five timestamped inspection reports and default-decision snapshots.

For the full deployment behaviour, the action-by-action plan, and the reversibility contract, see [`DEPLOYMENT.md`](./DEPLOYMENT.md).

### Step 6 — Open your first session

Open your agent runtime in your project directory. Before you type anything, you should see something like:

```
## MetaEnsemble — session start
- Current window: `2026-05-13T05`
- 5-hour window: 12% used (3h47m left)
- 7-day window: 8% used (5d22h left)
- This session so far: 0 tokens (main agent: 0, MetaEnsemble: 0)
- Runs in last 24h: 0
- Active Executors (last 7 days): 0
```

If the previous session was interrupted (`kill -9`, budget exhaustion, or a runtime crash that skipped the Stop hook), you may also see a line like `- Reconciled 1 stale pending Run(s) from prior sessions`. That is `session_start.py` running the on-demand reconciler over sidecars older than one hour; the housekeeping is automatic. See [`/dispatch` recovery](#when-something-feels-off) for the on-demand `metaensemble reconcile` command if you ever need to force the sweep yourself.

You are oriented. You are ready to work.

---

## Walking through your first session

State your intent through `/dispatch`. In `--layout=namespaced`, use the
namespaced form `/metaensemble:dispatch`; in `--layout=top-level`, use
top-level `/dispatch`. The slash command is the trigger that engages
the Coordinator's protocol; the description after it is plain English,
the way you would brief a chief of staff.

```
/dispatch add password reset to the auth flow in this project, with rate-limiting on the new endpoint, and a cross-Role security review because writing to production credentials is irreversible
```

The Coordinator will respond in English with a plan: which Executors will run, which Manifest they will work from, what the budget is. Read three or four sentences and either approve or redirect.

If the proposed work is small and the 5-hour window has headroom, the Coordinator dispatches without asking. If either the run is large or the window is running low, the Coordinator pauses before dispatching and brings you in:

```
MetaEnsemble cost gate — block
  Reason: action is irreversible; mandatory peer review and Principal approval
  Estimated tokens: 8000 (9.1% of window capacity)

Options:
  1. Approve and proceed at current tier
  2. Drop the model tier and retry (haiku/sonnet)
  3. Split the Task into smaller Manifests

Default: paused. Choose an option to proceed.
```

You pick a number. The Coordinator proceeds with your choice; it never auto-overrides. On NOTIFY the same options appear with `Default: proceed in a moment. Choose an option to intercept.` so the dispatch goes through unless you stop it.

When the Executors are done, the Coordinator returns a Deliverable: a Markdown report in plain English describing what was done, what was decided, and what was discovered. You read it, decide whether to ship, and move on.

When you close the session, the Coordinator emits a summary:

```
## MetaEnsemble — session summary
- Window `2026-05-13T05`: 15.4% of capacity consumed (13,547 tokens, source: runtime rate_limits feed, 2h13m left in window)
- 7-day window: 9.1% used (5d20h left)
- This session: 6,210 tokens (main agent: 4,180, MetaEnsemble: 2,030)
- Runs completed this session: 4
- Executors active this session: 3
- Deliverables produced (4):
  - reports/implementation/auth-reset-20260513.md
  - reports/review/auth-security-20260513.md
  - reports/tests/auth-tests-20260513.md
  - reports/implementation/auth-synthesis-20260513.md
```

That is the full loop. State intent, approve when asked, read Deliverables, close.

---

## How MetaEnsemble engages

MetaEnsemble is opt-in by design. The Coordinator's protocol engages when you invoke `/dispatch` (or any other MetaEnsemble slash command). Plain Claude Code work — asking the main agent to help with something in English, without naming MetaEnsemble vocabulary and without a slash command — passes through the hooks invisibly. The hooks fire on every lifecycle event because they are wired in your settings, but they only record a Run when a Task invocation comes through the Coordinator protocol with the appropriate metadata.

The practical consequence is the one you might already have noticed: if you have just installed MetaEnsemble, opened a session, asked the main agent for help with something, and then run `/standup`, you should expect to see zero Runs. That is not a bug; the system is honestly telling you that no MetaEnsemble Run has been created yet because no engagement command has been used. The remedy is to use `/dispatch` for the next piece of work and watch the numbers move.

The design respects your existing workflow. You did not lose Claude Code by installing MetaEnsemble; you gained an opt-in layer on top of it. When you want the layer engaged, you invoke it.

For dispatched work, MetaEnsemble treats the directory where you ran
`metaensemble adopt` as the project boundary. File edits made by an
Executor are allowed inside that root and recorded on the Run; an edit
that resolves outside the root is blocked with a recovery message. If
your real project root is one directory higher, install and dispatch
from that higher directory rather than from a generated subfolder.
During a raw or expanded `/dispatch` command, direct file edits by the
Coordinator are also blocked unless an Executor Run is active. That
keeps `/dispatch` accountable: file-changing work must go through
Task/Agent so the Ledger can record what happened.
For file-changing tasks, be explicit when needed: "use a frontend
Executor via Task/Agent; the parent Coordinator must not call Edit or
Write directly." If the Coordinator deviates, the guard blocks the edit
and the Run remains auditable instead of silently mutating the project.

---

## The seven slash commands

Seven commands ship with MetaEnsemble. Below they are organized by how often you will actually reach for each.

### Tier 1 — commands you use every session

#### `/dispatch <task description> [flags]`

The most important command, and the engagement trigger for everything else. `/dispatch` hands a piece of work to the Coordinator, which plans the Task, composes the Manifest, spawns one or more Executors, and synthesizes a Deliverable for you. Without `/dispatch` (or another MetaEnsemble slash command) the Coordinator's protocol does not engage and your work goes through the agent runtime's normal subagent machinery without producing any Run entries.

The description after `/dispatch` is plain English; the Coordinator reads it as your intent. Flags trigger the multi-instance patterns from [`ARCHITECTURE.md`](./ARCHITECTURE.md) §12:

- `--fanout N` — spawn N Executors of one Role with divergent Briefs; explore alternative approaches in parallel.
- `--consensus N` — spawn N Executors of one Role with the same Brief; surface majority and dissent rather than averaging.
- `--shadow tier1,tier2` — run two Executors of the same Role at different model tiers; validate downward tiering before committing to a model change.
- `--peer-review role1,role2` — cross-Role validation; mandatory for irreversible Tasks per ARCHITECTURE §12.

Example invocations:

```
/dispatch run a UX review on the hero section against the new terracotta palette
/dispatch implement password reset with rate-limiting on the new endpoint
/dispatch --fanout 3 explore three cache strategies for the search endpoint
/dispatch --peer-review security,sre deploy the auth changes to production
```

After you send the command, the Coordinator responds in English with a plan: which Role or Roles will execute, which Manifest it will compose, what the token budget is. You approve or redirect. When the Executors finish, the Coordinator returns a Deliverable in plain English describing what was done, what was decided, and what was discovered. The cost gate may surface an options table in between if the proposed work crosses a threshold or is irreversible.

#### `/standup`

Daily orientation. Surfaces the current window status, Runs completed in the last twenty-four hours, top token consumers, and Executors active in the last week. Run it first thing when you open a session you did not just create — five-second read that tells you where you left off and where the budget stands. The session-start digest already shows a shorter version of this when a fresh session opens; `/standup` is the on-demand fuller view.

#### `/limits`

Mid-session budget check. Shows current 5-hour window tokens used, tokens remaining, and Runs counted in this window. Use it when you are about to dispatch a substantial piece of work and want to know whether the budget will hold, or when a session has run longer than you expected and you want to know how close to exhaustion you are.

Telemetry labels are deliberately literal. `of plan used` appears only when the runtime rate-limit feed is live and plan-wide. `last runtime snapshot` means the value came from stale native data and is shown only as historical context. `project burn` or `fallback capacity` means MetaEnsemble is showing local Ledger burn against the configured fallback, not claiming plan-wide usage.

### Tier 2 — commands for continuity and inspection

#### `/relaunch <alias> [--full]`

Resume a prior Executor's thread across sessions. Aliases are short and Role-prefixed (`arch-7b3`, `be-9c1`); you learn them from `/standup`, `/executors`, or from a Deliverable's byline. The cheap default loads the Executor's last Brief and the summary of its last Deliverable, which is usually enough for most resumptions. `--full` reads the entire prior Deliverable and every prior Brief in the Executor's Run history; the cost grows with history length, so use sparingly. Use `/relaunch` when you want continuity by Executor — picking up the same colleague's thread — rather than spinning up a fresh Executor of the same Role.

#### `/executors`

Lists Executors active in the last thirty days, with alias, Role, status, when last seen, and most recent Run. Reach for it when you want to know "who is on the roster" — before deciding whether to `/relaunch` an existing thread or `/dispatch` a fresh Executor. Also useful as a sanity check that the roster is not bloating; if you see Executors you do not recognize or remember dispatching, that is a signal worth investigating.

#### `/ledger <subcommand>`

Ad-hoc queries against the Run log. Subcommands:

- `recent [--limit N]` — most recent Runs across all Executors.
- `by-executor <alias-or-id> [--limit N]` — Runs for one Executor.
- `by-task <task-id> [--limit N]` — Runs for one Task.
- `window <window-id>` — aggregate burn for one 5-hour window bucket.

The other commands cover the common questions; `/ledger` is for when you have something specific in mind. A common reason to use it after install: `/ledger recent --limit 10` as a quick sanity check that MetaEnsemble has actually been logging Runs — if the list is empty, you have not used `/dispatch` yet and the engagement-model section above explains why.

### Tier 3 — commands for diagnosis

#### `/perf`

Rolling performance metrics over the last twenty-four hours — hook latency p95, Ledger query latency, Run outcome distribution, hook error log health. Use it when something feels slow or off, or when you want to confirm the engineering budgets in [`PERFORMANCE.md`](./PERFORMANCE.md) are being honored on your machine. Most days you will not need it. If it shows a hook regularly exceeding its 100 ms p95 budget, you have a real performance regression and PERFORMANCE.md §3 is the engineering contract to investigate against.

---

## The MetaEnsemble CLI — beyond the slash commands

The seven slash commands above are what the Coordinator surfaces inside a Claude Code session. The same operations are also available from your shell as `metaensemble <subcommand>`, plus a handful of lifecycle and recovery commands the slash surface intentionally hides.

| Group | Command | What it does |
|---|---|---|
| Install lifecycle | `metaensemble setup [--layout=...]` | **Interactive wizard.** Lists projects, prompts you to pick one, asks for layout (only if user-setup hasn't run), then runs user-setup and adopt. The recommended entry point. |
| | `metaensemble user-setup --layout={namespaced\|top-level} [--dry-run]` | User-level integration: vendors the runtime atomically into `~/.metaensemble/runtime/`, generates the runner at `runtime/bin/me-run`, and writes slash commands, hooks, statusline, output styles, and the skill into `~/.claude/`. Run once per machine; re-run after a `pip install --upgrade` to vendor the new assets (always re-vendors, so upgrades cannot ship stale state). Re-run with a different layout to switch. |
| | `metaensemble adopt [<path>] [--dry-run]` | Per-project: inspection + `<project>/.metaensemble/` + agent conversions per `install-decisions.yaml`. Defaults to cwd; pass a path to adopt elsewhere. Requires user-setup to have run first. |
| | `metaensemble doctor [--fix]` | Run the nine-check health audit (C1 and C6 marked `SKIP` as legacy; C9 actively verifies the vendored runtime). Always your first stop when something feels off. |
| | `metaensemble init [--force]` | Create `<project>/.metaensemble/` from scratch. `adopt` auto-calls this; the standalone form exists for the user who wants project state before committing to an install. |
| | `metaensemble inspect` | Read-only inventory of the user's and project's existing setup. Writes `install-decisions.yaml` for you to edit. |
| | `metaensemble manifest validate <path>` | Load and schema-validate a Manifest YAML file. Surfaces YAML errors with line:column and schema errors with the failing field path. |
| | `metaensemble manifest new-id` | Print a fresh `hm-<UUIDv7>` Manifest id to stdout. |
| | `metaensemble manifest scaffold <task> [-o <path>]` | Write a starter Manifest YAML (with TODO markers in every author-supplied field) to stdout, or to `-o <path>` (parent directories are created). The scaffold deliberately fails validation until the TODOs are replaced. |
| | `metaensemble projects [--prune]` | List every Claude Code project on this machine with MetaEnsemble install status, run count, and last-run timestamp. `--prune` removes stale Claude Code project registrations whose cwd no longer exists before printing the table. |
| Day-2 ops (read-only) | `metaensemble limits` / `standup` / `executors` / `perf` | Same as the slash commands above, callable from outside the session. |
| | `metaensemble ledger {recent\|by-executor\|by-task\|window}` | Ledger queries. |
| | `metaensemble relaunch <alias> [--full]` | Print the relaunch context for an Executor without dispatching. |
| Recovery and continuity | `metaensemble reconcile [--older-than-minutes N] [--dry-run]` | Write every stranded pending sidecar to the Ledger as an `interrupted` (or `budget_exceeded`) Run. Run this if a dispatch was `kill -9`'d, budget-killed, or otherwise skipped its PostToolUse hook — and always before a destructive teardown, so the Ledger captures every Run before state is removed. The session-start hook also runs the sweep with a one-hour threshold on every fresh session, so manual invocation is rarely needed. |
| | `metaensemble unadopt [<path>] [--purge-state]` | Reverse a project's adoption: walk its `.metaensemble/backups/` in reverse, restore converted agents, strip the managed `.gitignore` block. `--purge-state` also deletes `<project>/.metaensemble/`. User-level integration stays intact. |
| | `metaensemble user-teardown [--purge-state]` | Reverse user-setup: remove the managed `~/.claude/` symlinks and hook entries. The vendored runtime at `~/.metaensemble/runtime/` survives so it stays usable as a recovery anchor; `--purge-state` deletes `~/.metaensemble/` entirely (runtime, runtime-versions, roles, state). Other projects' `.metaensemble/` stays intact. |
| | `metaensemble export-agents [--target-dir] [--overwrite] [--user-only\|--project-only]` | Reverse-convert MetaEnsemble Roles back to Claude Code agent files. The documented escape hatch for the case where `unadopt` cannot replay conversions (e.g. backups directory deleted). |
| Evaluation | `metaensemble eval [--tier replay\|smoke\|full] [--config <path>] [--allow-live]` | Run the evaluation harness in `evals/`. `replay` reads JSONL cassettes and costs nothing; `smoke` runs a single-seed live classification check; `full` is release-gated and requires `--allow-live` plus signed-off D-8/D-9 thresholds (see [`SYSTEM-CARD.md`](./SYSTEM-CARD.md)). |
| Runtime | `metaensemble hook <name>` | The runner entry point the installed settings.json hooks dispatch through. Not for direct use. |

For the action-by-action install behaviour and the reversibility contract, see [`DEPLOYMENT.md`](./DEPLOYMENT.md). For the harness contract, see [`evals/README.md`](../evals/README.md).

---

## What approval looks like, in practice

The cost gate evaluates every dispatch along two axes: *run size* (how
large the dispatch is relative to your 5-hour window capacity) and
*window headroom* (how much of the window is still available). Either
axis can independently push the dispatch into NOTIFY or BLOCK; the
final state is the worst of the two. Irreversible actions and novel
Manifest patterns are independent hard-blocks on top.

**Auto.** Both axes are clear — the dispatch is well under the
run-size soft threshold and the window has plenty of headroom. The
Coordinator dispatches without asking. You see no prompt; the
Deliverable arrives when the work is done. This is the default for
small reversible work, and it should be most of your sessions.

**Notify and Block — both surface the same options.** When the
Coordinator can see a threshold is about to be crossed, it pauses
*before* invoking the dispatch and brings the situation to you in
plain English. The difference between NOTIFY and BLOCK is the default
action: on NOTIFY the dispatch proceeds in a moment unless you
intercept; on BLOCK it pauses outright until you choose. You will
see something like:

```
MetaEnsemble cost gate — notify
  Reason: this dispatch is 27.4% of window capacity (soft limit 20%)
  Estimated tokens: 24100 (27.4% of window capacity)

Options:
  1. Approve and proceed at current tier
  2. Drop the model tier and retry (haiku/sonnet)
  3. Split the Task into smaller Manifests

Default: proceed in a moment. Choose an option to intercept.
```

The block surface is identical apart from the header and the closing
line, which reads `Default: paused. Choose an option to proceed.`
You pick a number. *Window-pressure* escalations are especially
important because they affect what your remaining session budget
pays for — spending the last 20% of the window on a single dispatch
trades away several smaller ones later in the same window.

**Capacity is auto-calibrated from the runtime.** The percentages you
see come from Claude Code's own `rate_limits` feed (5-hour and 7-day
windows, `used_percentage`, reset times) — captured by the MetaEnsemble
statusline on every refresh. You do not configure your plan's window
cap; the displays match exactly what the runtime itself reports.

If you find the gate blocking too often, edit
`~/.metaensemble/budgets.yaml` or `.metaensemble/budgets.yaml` and
relax the run-size or window-headroom thresholds. The defaults
(20% / 40% / 30% / 10%) are calibrated so that single dispatches up
to one-fifth of capacity pass through silently, dispatches up to two-
fifths NOTIFY, and dispatches larger than that BLOCK for approval —
tight enough to catch a runaway dispatch, loose enough to keep
substantive work flowing without prompting on every Run.

---

## Python deliverable check — what the Coordinator checks after a Run

Every successful Run whose Manifest declares Python deliverables can
pass through a five-axis Python deliverable check before the Deliverable
is treated as final. The code still calls this component the quality
gate, but v0.1.0 scope is narrower than a universal quality judge:
non-Python Deliverables, missing Manifests, absent tools, and missing
coverage data produce skipped axes rather than fabricated confidence.
The check runs in the PostToolUse hook and never blocks the dispatch
itself — the Deliverable has already been produced — but it surfaces
findings to the Coordinator, which surfaces them to you, when output
crosses a threshold.

The five axes anchor to industry-standard sources:

- **Correctness** — runs your project's pytest suite when the
  Manifest declared `.py` deliverables. NOTIFY on one failure;
  BLOCK on three or more.
- **Security** — runs `bandit` on the changed files. NOTIFY on any
  medium-severity finding; BLOCK on any high or critical, matching
  Snyk's default PR-check threshold.
- **Maintainability** — runs `ruff` and maps the issue count to
  SonarQube-style A/B/C/D/E grades. Six or more issues NOTIFY (grade
  C); sixteen or more BLOCK (grade D or E).
- **Complexity** — runs `radon` and reports the per-function
  cyclomatic number. McCabe's stable threshold has been 10 since
  1976; functions above 10 NOTIFY, above 15 BLOCK.
- **Coverage** — reads `coverage.py`'s last report. A drop of
  five percentage points or absolute coverage below 80% (the floor
  NISTIR 8397 settled on) BLOCKs.

The Coordinator surfaces the worst-of-axes verdict and four
structured options:

```
## MetaEnsemble quality gate — block
Deliverable from be-9c1 fails the quality gate.
Failures: security: block, maintainability: notify.
Findings:
  - bandit B602 high in src/auth/login.py:42 — subprocess with shell=True
  - bandit B105 medium in src/auth/login.py:18 — hardcoded password
  - ruff F401 in src/auth/login.py:3 — unused import

Options:
  1. Accept the Deliverable as-is, log the override
  2. Send to peer review with the findings as the brief
  3. Re-dispatch the Manifest with the findings folded in
  4. Split the work, dispatch the remediation as its own Task
```

Install the optional runners with `pip install -e ".[quality]"`. Each
axis skips gracefully if its tool is absent — the gate degrades to a
partial check rather than failing closed. Override thresholds per
project in `.metaensemble/quality.yaml`; the shipped example file at
`metaensemble/config/quality.example.yaml` documents the defaults and the
industry sources they anchor on.

---

## Cross-session continuity

Stable Executor identities exist so that work picks up where it left off, across sessions, across days, across weeks. The pattern in three steps:

1. Note the Executor alias mentioned in a Deliverable (`arch-7b3`, `be-9c1`, etc.). The Coordinator names them in its synthesis.
2. Next session, when you want to continue, type `/relaunch arch-7b3`.
3. The Coordinator reconstructs the prior Brief and a summary of the prior Deliverable, then dispatches a new Run under that identity.

If you do not remember the alias, `/standup` or `/executors` will show you what is active. If the alias no longer appears, start fresh with a new Executor of the same Role — the Role specification still exists in `metaensemble/roles/` (or your project's `.metaensemble/roles/`), so the new Executor inherits the same capabilities.

---

## Managing the roster

Executors accumulate as you work. A solo Principal typically runs five to ten active Executors at any given time, not fifty. Two practices keep the roster manageable:

- Aliases are short and Role-prefixed (`arch-7b3` rather than the full UUIDv7) so they sit in working memory the way colleagues' names do.
- Use `/executors` to audit the roster periodically. Executors you no longer need can be retired by removing their Ledger entries directly. Automated sunset flagging at sixty days is a v0.2 feature.

The day-to-day shape: when you start a new project or a new line of work, you spawn the Executors that work needs. As that work pauses or finishes, the Executors fall idle. Returning to the work a quarter later, either you relaunch what is still active or you start fresh against the same Role specifications. Continuity is by Executor for active work; by Role for projects you come back to after a break.

---

## When something feels off

Reach for `metaensemble doctor` first — it walks ten live checks (C1 and C6 are legacy SKIPs in v0.1.0) and tells you which ones failed and how to fix them. Most operational issues map to one of the patterns below.

If your project lives in an iCloud-synced directory (e.g., `~/Desktop/` with iCloud Desktop & Documents Sync enabled), consider excluding `.venv/` from iCloud sync. iCloud's conflict-resolution against rapid `pip install` file churn produces phantom duplicate files in `site-packages` (`architect 2.md`, `cli 2.py`, etc.); MetaEnsemble filters them correctly at catalog enumeration time and `metaensemble doctor` C11 surfaces them as a WARN, but they consume iCloud quota and slow installs.

### Common failure patterns and remedies

**1. The CLI fails with `ModuleNotFoundError: No module named 'metaensemble'` (or `'core'`).**
The wheel install is missing or broken. Reinstall with `pip install --force-reinstall metaensemble` (or `pip install -e .` from a clone for development). The `No module named 'core'` form is a stale-install symptom from a pre-v0.1.0 layout; CHANGELOG's migration block has the one-shot purge-and-reinstall sequence. After reinstalling, re-run `metaensemble user-setup --layout={namespaced|top-level}` so the vendored runtime at `~/.metaensemble/runtime/` regenerates against the fresh package.

**2. `/dispatch` produces a Deliverable but `/standup` reports zero Runs.**
The hook chain is not firing. Two causes are likely:
- Your `~/.claude/settings.json` is missing MetaEnsemble's hook entries, or the runtime's tool name does not match a registered matcher. Run `metaensemble doctor` — `C2` will name the missing wiring. Re-running `metaensemble user-setup` rewrites the entries with both `Task` and `Agent` matchers (the runtime has been called both names across versions).
- The settings.json change happened mid-session and the runtime cached the previous configuration. Close the session and open a new one in the same project, then try again.

**3. A dispatch is blocked with `PreToolUse:Agent hook error`.**
This is the cost-gate BLOCK rendering as a generic error. The structured options are persisted to `<project>/.metaensemble/state/blocks/<session>-<ts>.json`. Read the most recent file there; it carries `reason`, `estimated_tokens`, `estimated_pct_of_window`, `state` (`block`), `default` (`paused`), and the three options the Coordinator can present. NOTIFY decisions write the same shape to `<project>/.metaensemble/state/notifies/` with `default: proceed`. To proceed past a BLOCK, either lower the threshold in `~/.metaensemble/budgets.yaml` / `<project>/.metaensemble/budgets.yaml`, or split the Manifest to reduce the per-Run budget.

**4. A Manifest fails validation with a YAML parser error.**
The PreToolUse hook prints the specific failing line and column. The most common cause is a string containing `:`, `→`, `#`, or quotes left unquoted. Wrap the value in double quotes and retry. The Manifest authoring rules in `metaensemble/skills/metaensemble-protocol/SKILL.md` go through this in detail.

**5. A Manifest fails validation with `Additional properties are not allowed`.**
The Manifest schema is strict about the top-level field set. Rich context that does not belong in the contract goes under the `extras` block (which accepts any open-shape object), in the Manifest prompt body, or in the Deliverable. See `metaensemble/skills/metaensemble-protocol/SKILL.md` for examples.

**6. The doctor reports `C9 FAIL` or `C9 WARN`.**
`C9 Runtime vendored` is the post-install sanity check on the vendored runtime at `~/.metaensemble/runtime/`. `WARN` typically means you have not run `metaensemble user-setup` yet; `FAIL` means the runtime symlink, version dir, MANIFEST, or runner is missing or corrupted. The remediation is the same in both cases: `metaensemble user-setup --layout={namespaced|top-level}` re-vendors the runtime atomically (no half-applied state).

`user-teardown` only removes the user-level integration it installed under `~/.claude/` and, with `--purge-state`, `~/.metaensemble/`. It does not uninstall the Python package or delete the `metaensemble` console script from your environment. You can recover by rerunning `metaensemble user-setup --layout={namespaced|top-level}` without reinstalling the wheel.

When teardown removes the `metaensemble-protocol` skill and slash-command symlinks from `~/.claude/`, the Claude Code harness drops those commands/skills from new tool surfaces automatically. Re-running `user-setup` restores them in the selected layout.

**7. An agent name is in `active_roles` but `Agent(subagent_type="<name>")` fails to resolve.**
After top-level install with `take_ours` or `convert`, MetaEnsemble leaves a thin shim at the original `~/.claude/agents/<name>.md` path so the runtime keeps recognizing the name. If the shim is missing, the runtime did not pick up the file change in the current session. Open a new session.

**8. `metaensemble unadopt` left a converted agent unrestored.**
Current releases walk every adoption plan in reverse chronological order. If you unadopted with an older build, that chain-walking behaviour may not have run. Use `metaensemble export-agents --overwrite` to reverse-convert the Roles in `~/.metaensemble/roles/` back into agent files at `~/.claude/agents/`.

**9. `/limits` or `/standup` shows "% unavailable until the statusline refreshes".**
The runtime's `rate_limits` feed has not been captured yet for this session. The MetaEnsemble statusline script writes the capture every time Claude Code refreshes the statusline; on a fresh install or after a long idle period the file may not exist or may be stale (older than 5 minutes). The displayed tokens are still accurate as an absolute count; the percentage will appear once you do any work in Claude Code that triggers a statusline refresh. The cost gate's window-headroom axis is paused in this state — you will see a clear note in the output rather than a fabricated percentage.

**10. Intermittent `PreToolUse:Agent hook error` on a project under `~/Desktop/` or `~/Documents/` (macOS).**
On macOS, the Desktop and Documents folders are iCloud-synced by default. iCloud can place files into a dataless placeholder state — present in `ls`, but not materialised locally — and SQLite's `open()` of `.metaensemble/state/department.db` then fails with `unable to open database file`. The hook crashes before it can emit a stop reason, so Claude Code renders it as the generic `Agent hook error` with no stderr text. `metaensemble doctor` C4 surfaces the same failure with an iCloud-aware remediation when it detects this layout. The real fix is to host active MetaEnsemble projects outside iCloud-synced paths, or to exclude the project in System Settings → iCloud → Drive → Desktop & Documents Folders. A recursive `chflags` walk over `.metaensemble/` (e.g. `chflags -R nohidden .metaensemble`) sometimes unblocks the immediate failure by side-effect, because the metadata touch prompts iCloud to materialise the files — but it is not the root fix and the failure will recur.

**11. `metaensemble doctor` reports `C12 — pinned interpreter is editable` (or `user-setup` printed an editable-install notice).**
The pinned Python in `~/.metaensemble/runtime/bin/me-run` has `metaensemble` installed editable, so the runner resolves `import metaensemble` to your source tree. Moving or deleting that source breaks every hook. To switch to a wheel install where the runner is independent of any source tree, build a wheel and install it into a non-editable interpreter, then re-run `user-setup` from that interpreter:

```bash
python -m build --wheel
<other-python> -m pip install dist/metaensemble-*.whl
<other-python> -m metaensemble user-setup --layout={namespaced|top-level}
```

`C12 FAIL` means the pinned interpreter has no `metaensemble` install at all (e.g. you uninstalled the package without running `user-teardown`). Same recovery, with an additional `pip install` step.

### Three diagnostic surfaces

1. **`metaensemble doctor`** — nine checks (C1, C6 marked as legacy SKIP in v0.1.0), action-oriented status line. Always your first stop.
2. **`.metaensemble/hooks/log.jsonl`** — structured error log written by every hook on failure. Each line is one event. The last 5–10 entries usually tell the story.
3. **`/ledger recent --limit 20` or `metaensemble ledger recent --limit 20`** — what the system has been doing. If the recent Runs do not match the work you asked for, the problem is upstream of MetaEnsemble, in the conversation between you and the Coordinator. Surface it to the Coordinator and redirect.

### Performance and budgets

`metaensemble perf` shows rolling hook latency, Run latency, outcome distribution, and hook error counts over the last 24 hours. If a hook is regularly slower than its 100 ms p95 budget, you have a real performance regression and `PERFORMANCE.md §3` is the engineering contract to investigate against.

### When all else fails — recovery

If MetaEnsemble has left your machine in a state you do not want to keep, every command supports `--dry-run` so you can see the plan before it runs:

- `metaensemble reconcile --older-than-minutes 0` — **run this first if a dispatch was interrupted, budget-killed, or left files under `.metaensemble/state/pending/`**. It records every stranded sidecar as an `interrupted` (or, when transcript evidence is available, `budget_exceeded`) Run so the Ledger stays truthful before you remove state. Defaults to zero minutes so every sidecar is reconciled immediately; use a larger threshold if you want to spare recent dispatches.
- `metaensemble unadopt` from the project where you installed — reverses every install action in that project, strips the managed `.metaensemble/` block from the project's `.gitignore`, restores any converted agents from backup, and prints a residue report naming everything that survived. The project's `.metaensemble/` (Ledger, manifests, briefs, inspection outputs) is preserved so re-adoption is cheap.
- `metaensemble unadopt --purge-state` — above plus delete `<project>/.metaensemble/` entirely. Use when you are done with MetaEnsemble in this project but keeping it on the machine for other projects.
- `metaensemble user-teardown` — remove the user-level integration: managed `~/.claude/` symlinks, hook entries, statusline. Other projects' `.metaensemble/` stays intact.
- `metaensemble user-teardown --purge-state` — above plus delete `~/.metaensemble/` entirely: the vendored runtime (`runtime/`, `runtime-versions/`), user-layer Roles, install records, and the runtime-rate-limit cache.
- **Full rollback**: run `metaensemble reconcile --older-than-minutes 0` first (so the Ledger captures any stranded sidecars), then `metaensemble unadopt --purge-state` in each adopted project, then `metaensemble user-teardown --purge-state`. The Python package remains installed unless you also run `pip uninstall metaensemble`.
- `metaensemble export-agents` — reverse-converts every Role under `~/.metaensemble/roles/` and `<project>/.metaensemble/roles/` back into Claude Code agent files at `~/.claude/agents/`. Use `--overwrite` if you want it to replace files already there. This is the documented escape hatch and does not depend on the install's backups directory.
- Manual rollback — every install creates `<project>/.metaensemble/backups/<timestamp>/` with a `plan.json` and copies of every file the install moved. You can read the plan and undo it by hand.

`DEPLOYMENT.md` documents the full recovery path in one place.

---

## What to ignore, deliberately

Several surfaces exist for auditability rather than daily attention. You will see them if you look, but they are not part of the daily flow:

- **Briefs** in `.metaensemble/briefs/*.json` are the wire format Executors use to talk to each other. You are not in this conversation.
- **Manifest YAML files** in `.metaensemble/manifests/*.yaml` are the typed contracts the Coordinator writes for Executors. They are specs, not reports.
- **The SQLite Ledger** at `.metaensemble/state/department.db` is where every Run lands for accountability. Query it through the slash commands; do not open it directly unless you are investigating something specific.
- **Hook scripts** in `metaensemble/hooks/` are infrastructure. They run automatically on every lifecycle event; you do not invoke them.

If you find yourself reading these files regularly, the system probably needs a slash command for whatever question you are answering. Open an issue; the named-query rule in `PERFORMANCE.md` makes adding such a command a small, reviewable change.

---

## When you forget the vocabulary

Seven nouns and one verb. Principal, Coordinator, Role, Executor, Task, Deliverable, Dispatch. The verb is "dispatch."

If you forget which is which, the glossary at [`GLOSSARY.md`](./GLOSSARY.md) has every term defined precisely, with the industry analog it maps to (IAM Principal, K8s Pod, MLflow run, dbt manifest, and so on). Most Principals reach for it once or twice in the first week and then rarely again.

---

## Where to go next

- [`ARCHITECTURE.md`](./ARCHITECTURE.md) covers the layered design, the data model, the lifecycle, and what MetaEnsemble is and is not.
- [`DEPLOYMENT.md`](./DEPLOYMENT.md) explains the inspection-then-install layout, both install layouts, the per-Role activation flow, and the reversibility contract the installer honors.
- [`PERFORMANCE.md`](./PERFORMANCE.md) is the binding engineering contract — token budgets, time budgets, the R1–R7 rules, and the CI-gated benchmarks.
- [`GLOSSARY.md`](./GLOSSARY.md) is the vocabulary reference.

For day-to-day operation, this guide is the document you keep open. Everything else is depth you can dive into when you want it.
