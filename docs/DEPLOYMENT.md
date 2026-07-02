# Deployment

*How `metaensemble inspect` and `metaensemble adopt` work, what they touch, and how to undo every change.*

---

## The two phases

Deployment runs in two phases, and the split is the load-bearing safety property of the installer.

**Phase 1 — inspect (read-only).** `metaensemble inspect` examines the user's `~/.claude/` and the project's `.claude/` directories, takes inventory of the agents, slash commands, skills, and output styles found there, and produces two files: a short Markdown report at `<project>/.metaensemble/inspection-<timestamp>.md`, and an editable `<project>/.metaensemble/install-decisions.yaml`. The decisions file is the user's choice surface: every agent and every curated Role gets one entry with a sensible default, and the inspection also records the project's `report_root` and its runtime memory surfaces (`CLAUDE.md`, `.claude/CLAUDE.md`, `CLAUDE.local.md` when present) so Manifests can hand Executors typed pointers to the project's existing memory. No filesystem changes happen during inspection beyond writing these two files in the project's `.metaensemble/`.

**Phase 2 — install (filesystem changes, fully reversible).** Installation has two layers, installed by two commands. `metaensemble user-setup --layout={namespaced|top-level}` installs the user-level integration once per machine (commands, hooks, statusline, and the vendored runtime at `~/.metaensemble/runtime/` with its runner at `runtime/bin/me-run`; layout controls whether slash commands install namespaced or top-level). `metaensemble adopt` reads the project's `install-decisions.yaml`, plans the per-project actions, optionally previews them with `--dry-run`, and applies them. Every project change is backed up to `<project>/.metaensemble/backups/<timestamp>/` and reversible via `metaensemble unadopt`; every user-level change is backed up to `~/.metaensemble/installs/<timestamp>/` and reversible via `metaensemble user-teardown`. The install also adds `.metaensemble/` to the project's root `.gitignore` (creating the file if absent) so the per-project working directory stays out of git, the same way `.venv` and `node_modules` do. An existing `.gitignore` is appended to, never overwritten, and the operation is idempotent.

**Layout is not deployment scope.** `namespaced` means MetaEnsemble's user-level slash commands/output styles are namespaced; `top-level` means they install at the top level. Both layouts still use the same two layers: `user-setup` once per machine and `adopt` once per project.

### Multi-project installations

MetaEnsemble is installed *per project*: `cd` into a project's root, run `metaensemble adopt`, and that project's `.metaensemble/` is created in isolation from any other project's. Each project keeps its own Ledger, Manifests, budgets, and install decisions. The user-level layer (`~/.metaensemble/` — the vendored runtime under `runtime/` and `runtime-versions/`, converted user-layer Roles, default budgets, install records, captured runtime rate-limits feed) is shared across every project.

Run `metaensemble projects` from any directory to see the full inventory: every project Claude Code has seen on this machine, with install status (installed / init-only / not installed), run count, and last-run timestamp. `metaensemble projects --prune` removes stale Claude Code registry entries whose cwd no longer exists before printing the table. The Coordinator reads only the current project's state, so day-2 commands like `/standup`, `/limits`, and `/perf` always show the project you `cd`'d into.

---

## Agent handling taxonomy

The deployment system distinguishes four kinds of agents and asks the user to confirm the handling for each. Default actions are conservative — nothing the user authored is silently overwritten — but the user can override every default in `install-decisions.yaml`.

| Kind | Definition | Default action | User can also choose |
|---|---|---|---|
| `collision` | Name exists in BOTH the user's `.claude/agents/` AND MetaEnsemble's `metaensemble/roles/`. | `keep_yours` (heuristic; `take_ours` is suggested when the user's agent declares no tools or model) | `take_ours`, `keep_both` |
| `user_unique` | Only in the user's setup. | `preserve` (agent stays as-is) | `convert` |
| `curated_relevant` | Curated Role whose project signals match the codebase (e.g. `backend` when `pyproject.toml` or backend dirs are present). | `activate` | `retire` |
| `curated_optional` | Curated Role with no matching project signals. | `retire` | `activate` |

What each action means at install time:

- **`keep_yours` (collision)** — user's agent left at `~/.claude/agents/<name>.md`. MetaEnsemble's curated Role of the same name is recorded in `active-roles.yaml` as inactive so the Coordinator does not dispatch to it.
- **`take_ours` (collision)** — user's agent is copied to `<project>/.metaensemble/backups/<timestamp>/agents/user/<name>.md`, then the original is replaced by a shim that delegates to the MetaEnsemble Role at `~/.metaensemble/roles/<name>.md`. The Role's full spec is in the Role file; the shim only preserves the agent name's dispatchability via `Agent(subagent_type="<name>")`.
- **`keep_both` (collision)** — user's agent stays untouched at `~/.claude/agents/<name>.md`. MetaEnsemble's Role is installed at `~/.metaensemble/roles/<name>-me.md` (note the `-me` suffix) so both names are dispatchable.
- **`preserve` (user_unique)** — no action. The user's agent stays where it was. `active-roles.yaml` records the name so the Coordinator knows it is available.
- **`convert` (user_unique)** — same handling as `take_ours`: backup, write Role spec, leave shim.
- **`activate` (curated_relevant / curated_optional)** — Role name added to `active-roles.yaml`'s `active_roles` list.
- **`retire` (curated_relevant / curated_optional)** — Role name added to `inactive_roles` list. The spec remains in `metaensemble/roles/` but the Coordinator does not dispatch to it.

The shim file written for converted agents preserves `name`, `description`, `tools`, and `model` in the frontmatter so the agent runtime continues to resolve `Agent(subagent_type="<name>")`. The body of the shim notes that the full spec is in the Role file and that `/dispatch` is the richer dispatch path.

---

## The two install layouts

Modes differ in whether MetaEnsemble's pieces install in namespaced subdirectories or at the top level, and in whether the user's existing agents get converted to MetaEnsemble Roles.

### `--layout=namespaced`

The cautious adopter's mode. MetaEnsemble installs in namespaced subdirectories so it cannot collide with the user's existing setup, and the user's existing artifacts are not touched.

Specifically:

- Slash commands install as a symlink at `~/.claude/commands/metaensemble/` rather than at the top level. The user's existing commands at `~/.claude/commands/*.md` are not affected.
- In Claude Code, these namespaced commands are invoked with the namespace form (for example `/metaensemble:dispatch`). Run `--layout=top-level` if you want top-level `/dispatch`, `/standup`, and related commands.
- The `metaensemble-protocol` skill installs at `~/.claude/skills/metaensemble-protocol/`. Skills are already namespaced by directory name, so there is no collision risk.
- Output styles install with a `metaensemble-` prefix (e.g. `metaensemble-wire.md`, `metaensemble-deliverable.md`).
- Hooks register in `~/.claude/settings.json` under the `hooks` key, alongside any hooks the user already has. The runtime chains them; MetaEnsemble's hooks fire in addition to, not instead of, the user's.
- The MetaEnsemble statusline script registers under the `statusLine` key in `~/.claude/settings.json`. The installer refuses to overwrite a user-configured statusline; only an empty slot is filled.
- No user agents are converted. The user's existing setup continues to function exactly as it did before install.

Use this mode when you want to evaluate MetaEnsemble against a workflow you have invested in heavily, or when you have not yet decided whether to commit fully.

### `--layout=top-level`

The committed adopter's mode. MetaEnsemble becomes the primary workflow for the project. Existing agents convert to MetaEnsemble Roles (with backups), and slash commands install at the top level rather than under a namespace.

Specifically:

- Slash commands install as a symlink at `~/.claude/commands/` directly. If a name collision exists (the user already has a `/dispatch` command, for example), the installer detects it during planning and refuses to overwrite; the conflict is surfaced for explicit resolution.
- Output styles install at the top level (no prefix). Collisions follow the same refusal-to-overwrite rule.
- **Existing user-layer agents are converted to user-layer MetaEnsemble Roles** at `~/.metaensemble/roles/`. The conversion is mechanical field mapping: `name` to `name`, `description` to `description`, `tools` to `allowed_tools`, `model` to `model_tier`. Fields the Role schema requires that the agent lacks (`version`, `alias_prefix`, `output_styles`, `onboarding`) get sensible defaults. The body of the file is preserved unchanged. The original agent file moves to a backup, and the new Role file replaces it.
- **Existing project-layer agents convert to project-layer Roles** at `<project>/.metaensemble/roles/` by the same rules.
- Skills and hooks are additive: the metaensemble-protocol skill installs alongside the user's other skills, and MetaEnsemble's hooks register in `settings.json` alongside the user's hooks. Neither replaces anything.

Use this mode when you want MetaEnsemble to be the way you work, with your existing agents folded into the MetaEnsemble paradigm so they participate in the Ledger, the cost gate, the Python deliverable check, and persistent identity.

---

## Per-Role activation for the project

MetaEnsemble ships curated software and ML/data Roles (`architect`,
`backend`, `frontend`, `code-quality`, `test-engineer`, `devops`, `docs`,
`data-engineer`, `ml-engineer`). Not all of them are relevant for every
project. The inspection walks the project for typed
filesystem signals per Role and proposes which curated Roles look relevant
based on what it finds. v0.1.0 covers five archetypes: Python ML, data
engineering (including dbt), web apps, libraries, and infrastructure-as-code.
The detector is deterministic and signal-based — no model calls, no
semantic inference. Signals include directory existence, file existence,
glob matches, and cost-bounded `file_contains` checks against config
files (e.g. `pyproject.toml` declaring `[tool.mypy]` activates
`code-quality`; `dbt_project.yml` activates `data-engineer` and routes
the ambiguous `models/` directory away from `ml-engineer`). The user can
override every choice in `install-decisions.yaml`.

The inspection report includes three sections built from the signals:

- **Curated Roles relevant to this project**, with the evidence found.
- **Curated Roles that look less relevant**, with the absence-of-signal noted.
- **Signal probe summary**, listing for every curated Role the signals
  the detector probed and which fired — so the Principal can see why a
  Role matched or didn't, and what would change the verdict.

The user reads these and decides which Roles to activate by editing
`<project>/.metaensemble/install-decisions.yaml` (the editable choice
surface the inspection produces) before the install step. The install then
honors every decision file entry.

```bash
metaensemble inspect                # writes inspection-<ts>.md + install-decisions.yaml
$EDITOR <project>/.metaensemble/install-decisions.yaml   # adjust per-Role activate/retire
metaensemble user-setup --layout=namespaced
metaensemble adopt
```

The active set is written to `<project>/.metaensemble/active-roles.yaml`,
which the Coordinator reads at dispatch time so it only spawns Executors
of active Roles. The defaults applied when the user does not edit
`install-decisions.yaml` are signal-based: curated Roles that match
project signals (`backend/` dir, `tests/`, `Dockerfile`,
`.github/workflows/`, etc.) activate by default and the rest retire.

## Python deliverable-check runners (optional)

The Python deliverable check ships as an optional dependency group. Install the five
runners with:

```bash
pip install -e ".[quality]"
```

This pulls in `bandit`, `ruff`, `radon`, `coverage`, and `pip-audit`.
The gate skips any axis whose tool is not installed, so a partial install
degrades the check rather than blocking PostToolUse. Project overrides
live in `<project>/.metaensemble/quality.yaml`; the shipped example file
at `metaensemble/config/quality.example.yaml` documents the defaults and the
industry sources they anchor on. Non-Python deliverables are checked
across the same five axes through the optional `axis_commands` block in
the same file — one command per axis (for example `npm test --silent` as
the correctness command), no extra install required.

To change the active set after install, edit
`<project>/.metaensemble/install-decisions.yaml` (flip the per-Role
`action` between `activate` and `retire`) and re-run `metaensemble
adopt`. Inactive Roles remain available in `metaensemble/roles/` and
can be reactivated later without reinstalling.

---

## Requirements and operating constraints

What to plan around before adopting a project. SYSTEM-CARD.md remains the
authoritative statement of capabilities and limitations; this section is the
deployment-facing summary.

**Python and platform.** Python 3.10–3.13 (the CI matrix). Runtime
dependencies are `jsonschema` and `pyyaml` only; the quality runners are the
optional `[quality]` extras above. Tested on macOS and Linux; Windows is not
currently exercised.

**Claude Code hook events.** The full lifecycle needs `SessionStart`,
`PreToolUse`, `PostToolUse`, `SubagentStop`, and `Stop`. Older runtimes
degrade rather than break: without `Stop`, Layer-1 reconciliation is lost and
`metaensemble reconcile` is the workaround; without `SubagentStop`,
background-dispatched Runs are recovered by the reconcile sweep instead of
finalizing at subagent stop.

**Concurrency.** One runtime per project. The Ledger is a single-writer
SQLite database; concurrent multi-runtime use against the same project is not
supported in v0.1.0.

**Storage.** Measured Ledger growth is ~1.6 KiB per Run — about 1.5 MiB
after 1,000 fully populated Runs (see PERFORMANCE.md §5.1). Briefs and
Deliverables are separate files and dominate footprint on prose-heavy
projects.

**iCloud-synced paths.** Host active projects outside iCloud-synced
directories, or exclude them from sync. iCloud's dataless-placeholder state
can make SQLite `open()` fail intermittently, surfacing as `Agent hook error`
with no stderr; `metaensemble doctor` C4 and C11 name this cause when
detected.

---

## How collisions are resolved

A collision occurs when a user-authored artifact (an agent, command, or output style) shares a name with one of MetaEnsemble's shipped items.

In `namespaced` layout, collisions cannot occur at install time because MetaEnsemble's pieces install in namespaced subdirectories.

In `top-level` layout, the installer's posture is **conservative refusal**: when a top-level install would overwrite a user-authored file, the installer does not overwrite. Instead the conflict appears in the inspection report and the install plan, with the user's existing artifact remaining in place. The user can resolve the conflict by:

1. Renaming the existing artifact to free up the name, then re-running install.
2. Removing the existing artifact (after their own backup) if it is obsolete.
3. Leaving the conflict in place — the user's version keeps precedence, MetaEnsemble's version of that name is simply not installed, and other MetaEnsemble pieces proceed normally.

The principle: the installer never silently overwrites user content. Conflicts are surfaced and decided.

---

## Recovery and rollback

MetaEnsemble offers three rollback paths, in increasing order of disruption to the rest of your setup.

### 1. `metaensemble unadopt` and `metaensemble user-teardown`

Project adoption writes a backup directory at `<project>/.metaensemble/backups/<timestamp>/` containing:

- `plan.json` — every Action the installer planned and applied.
- `agents/user/<name>.md` — pre-conversion copies of any agents the install converted.

User setup writes a backup directory at `~/.metaensemble/installs/<timestamp>/` containing the user-scope plan and a settings backup when `~/.claude/settings.json` already existed.

Rollback mirrors the install split:

- `metaensemble unadopt [<project>]` reverses project-scope actions and strips the managed `.gitignore` block. The project's `.metaensemble/` directory remains so the Ledger, manifests, briefs, inspection outputs, and backups are available for re-adoption.
- `metaensemble unadopt --purge-state [<project>]` also deletes `<project>/.metaensemble/`.
- `metaensemble user-teardown` reverses user-scope actions: managed slash commands, output styles, the protocol skill, lifecycle hooks, and statusline wiring. The vendored runtime at `~/.metaensemble/runtime/` survives so it stays usable as a recovery anchor; `--purge-state` removes it along with the rest of `~/.metaensemble/`.
- `metaensemble user-teardown --purge-state` also deletes `~/.metaensemble/`.

Both rollback commands support `--dry-run` to preview the plan without applying it.

This is the path with the strongest guarantees: every change the installer made is reversible by reading the manifest of plans on disk.

### 2. `metaensemble export-agents` — escape hatch when backups are missing

If the project backup directory is absent because it was deleted, `metaensemble unadopt` cannot replay converted-agent restores. `metaensemble export-agents` is the documented escape hatch:

```bash
metaensemble export-agents               # write to ~/.claude/agents/, skip existing
metaensemble export-agents --overwrite   # replace existing files
metaensemble export-agents --target-dir /path/to/output
```

The command reverse-converts every Role file under `~/.metaensemble/roles/` and `<project>/.metaensemble/roles/` back into Claude Code agent format. The mapping is mechanical (the inverse of `convert_agent_to_role`): `name`→`name`, `description`→`description` (with the install-time pad stripped), `allowed_tools`→`tools`, `model_tier`→`model`; MetaEnsemble-only fields (`version`, `alias_prefix`, `output_styles`, `onboarding`) are dropped; the body is preserved verbatim.

Export does not remove the Role files. If you want a clean separation, run `metaensemble unadopt` after exporting to reverse the project adoption, then `metaensemble user-teardown` if you also want to remove user-level runtime integration.

### 3. Manual rollback — last resort

The installer leaves enough provenance on disk that a determined user can reverse changes by hand:

- `<project>/.metaensemble/backups/<timestamp>/plan.json` lists every Action that ran.
- `<project>/.metaensemble/backups/<timestamp>/agents/user/<name>.md` is the byte-for-byte original of any converted agent.
- `<project>/.metaensemble/backups/<timestamp>/settings.json.bak` is the original settings.json (when one existed).

Manual rollback steps:

1. Copy backed-up agents back to `~/.claude/agents/`.
2. Restore the oldest settings.json.bak to `~/.claude/settings.json` (or hand-edit hooks out).
3. Remove the symlinks the installer placed: `~/.claude/commands/metaensemble/` (namespaced layout), top-level `~/.claude/commands/<name>.md` symlinks (top-level layout), `~/.claude/skills/metaensemble-protocol/`, and the MetaEnsemble output styles in `~/.claude/output-styles/`.
4. Optionally delete `<project>/.metaensemble/` and `~/.metaensemble/` entirely (the latter holds the vendored runtime at `runtime/` plus `runtime-versions/`, user-layer Roles, install records, and the rate-limit cache).

The CLI subcommands are designed so this manual path is the last resort, not the first. But the data is on disk in a readable shape for the user who wants the certainty of doing it themselves.

### Verifying the rollback

`metaensemble doctor` should report `[WARN] C2 — settings.json contains no hook entries` and `[WARN] C4 — state directory does not exist` after a full rollback. That is the expected post-rollback state. Both warnings disappear after the next install.

### Full local rollback after live testing

When you want to return a project and the user-level runtime to
"no MetaEnsemble state", run the cleanup from the project root where
MetaEnsemble was installed:

```bash
metaensemble reconcile --older-than-minutes 0
metaensemble unadopt --purge-state
metaensemble user-teardown --purge-state
```

The reconcile step writes every stranded pending sidecar to the Ledger
before the purge removes project state. The two rollback commands then
remove managed hooks, commands, skills, output styles, the project
`.metaensemble/`, and `~/.metaensemble/`. They intentionally leave the
Python package installation in place; remove that separately with
`pip uninstall metaensemble` if you also want the `metaensemble` CLI
gone.

---

## Manual setup as fallback

If the installer fails or you prefer to do the wiring yourself, the manual setup is:

```bash
# From the MetaEnsemble runtime root after `metaensemble user-setup` has
# vendored it (~/.metaensemble/runtime/ resolves to a version dir under
# ~/.metaensemble/runtime-versions/):
ln -sf "$HOME/.metaensemble/runtime/commands" ~/.claude/commands/metaensemble
ln -sf "$HOME/.metaensemble/runtime/skills/metaensemble-protocol" ~/.claude/skills/metaensemble-protocol
ln -sf "$HOME/.metaensemble/runtime/output-styles/wire.md" ~/.claude/output-styles/wire.md
ln -sf "$HOME/.metaensemble/runtime/output-styles/deliverable.md" ~/.claude/output-styles/deliverable.md
```

Then add MetaEnsemble's hooks to `~/.claude/settings.json` under the `hooks` key. Two forms are accepted; both are recognized by `metaensemble doctor` and by `metaensemble unadopt`'s strip path.

**Runner form (what `metaensemble adopt` writes by default).** Each command is `$HOME/.metaensemble/runtime/bin/me-run hook <script>`. The runner lives inside the vendored runtime, so project moves and venv recreation do not invalidate the entry. Re-running `metaensemble user-setup` re-vendors atomically (new version dir + symlink swap) without rewriting settings.json.

```json
{
  "hooks": {
    "SessionStart": [{"matcher": "*", "hooks": [{"type": "command", "command": "/Users/<you>/.metaensemble/runtime/bin/me-run hook session_start.py"}]}]
  }
}
```

**Direct form (the fallback the installer uses when the runner is absent — pre-`user-setup` state).** Replace `/path/to/venv/bin/python` with the Python interpreter in your venv. The hook paths point into the installed `metaensemble` package; for a wheel install that resolves to `<venv>/lib/python3.X/site-packages/metaensemble/hooks/`.

```json
{
  "hooks": {
    "SessionStart": [{"matcher": "*", "hooks": [{"type": "command", "command": "/path/to/venv/bin/python <site-packages>/metaensemble/hooks/session_start.py"}]}],
    "PreToolUse": [
      {"matcher": "Task", "hooks": [{"type": "command", "command": "/path/to/venv/bin/python <site-packages>/metaensemble/hooks/pre_task.py"}]},
      {"matcher": "Agent", "hooks": [{"type": "command", "command": "/path/to/venv/bin/python <site-packages>/metaensemble/hooks/pre_task.py"}]},
      {"matcher": "Write", "hooks": [{"type": "command", "command": "/path/to/venv/bin/python <site-packages>/metaensemble/hooks/file_event.py"}]},
      {"matcher": "Edit", "hooks": [{"type": "command", "command": "/path/to/venv/bin/python <site-packages>/metaensemble/hooks/file_event.py"}]},
      {"matcher": "MultiEdit", "hooks": [{"type": "command", "command": "/path/to/venv/bin/python <site-packages>/metaensemble/hooks/file_event.py"}]},
      {"matcher": "NotebookEdit", "hooks": [{"type": "command", "command": "/path/to/venv/bin/python <site-packages>/metaensemble/hooks/file_event.py"}]}
    ],
    "PostToolUse":  [
      {"matcher": "Task",  "hooks": [{"type": "command", "command": "/path/to/venv/bin/python <site-packages>/metaensemble/hooks/post_task.py"}]},
      {"matcher": "Agent", "hooks": [{"type": "command", "command": "/path/to/venv/bin/python <site-packages>/metaensemble/hooks/post_task.py"}]},
      {"matcher": "Write", "hooks": [{"type": "command", "command": "/path/to/venv/bin/python <site-packages>/metaensemble/hooks/file_event.py"}]},
      {"matcher": "Edit", "hooks": [{"type": "command", "command": "/path/to/venv/bin/python <site-packages>/metaensemble/hooks/file_event.py"}]},
      {"matcher": "MultiEdit", "hooks": [{"type": "command", "command": "/path/to/venv/bin/python <site-packages>/metaensemble/hooks/file_event.py"}]},
      {"matcher": "NotebookEdit", "hooks": [{"type": "command", "command": "/path/to/venv/bin/python <site-packages>/metaensemble/hooks/file_event.py"}]},
      {"matcher": "Write", "hooks": [{"type": "command", "command": "/path/to/venv/bin/python <site-packages>/metaensemble/hooks/deliverable_sync.py"}]}
    ],
    "SubagentStop": [{"matcher": "*", "hooks": [{"type": "command", "command": "/path/to/venv/bin/python <site-packages>/metaensemble/hooks/subagent_stop.py"}]}],
    "Stop": [{"matcher": "*", "hooks": [{"type": "command", "command": "/path/to/venv/bin/python <site-packages>/metaensemble/hooks/session_summary.py"}]}]
  }
}
```

The installer (`metaensemble adopt`) generates these entries automatically with the correct absolute paths and your active Python interpreter.

Manual setup gives you the equivalent of `--layout=namespaced`. Replicating `--layout=top-level` manually is not recommended because the agent-to-Role conversion is mechanical but tedious; the installer exists to handle it.

---

## What the installer deliberately does not do

A few capabilities you might expect from a deployment tool are intentionally absent, for reasons that matter.

**It does not semantically rewrite user content.** The agent-to-Role conversion maps frontmatter fields. It does not edit the agent's body, does not "improve" descriptions, does not call any model. Anything beyond mechanical field mapping is post-install work the user does on their own.

**It does not decide which curated Roles to activate without the user's input.** The inspection detects filesystem signals (the presence of `frontend/`, `tests/`, `Dockerfile`, and so on) and proposes which Roles look relevant given what it finds, but the activation choice remains the Principal's via `install-decisions.yaml` or the per-Role decisions in `install-decisions.yaml`. The installer's job is to surface a defensible recommendation and let the user override; it does not impose its inference. The signal-based defaults activate curated Roles that match what the inspection found and retire those that don't.

**It does not learn from runtime patterns at install time.** The observability-driven recruitment subsystem that adapts the roster based on Ledger evidence is a v0.2 feature, fired by accumulated run history rather than by file-system snapshots. Conflating it with install-time deployment would force premature judgments without data.

**It does not modify Claude Code itself.** The installer's only writes are to your `~/.claude/` config, to `~/.metaensemble/` (user-layer Roles), and to `<project>/.metaensemble/` (project state). The Claude Code binary, its source, and its other configuration files are not touched.

---

## Design discipline behind the installer

The installer's behavior rests on a small set of choices, none of them invented from first principles and all of them deliberately combined for the problem at hand. The combination, applied to AI-agent personal configuration, is what makes the deployment story coherent. Each choice has prior art worth crediting.

**Survey before install, dry-run before apply.** This pattern comes from infrastructure tooling. Helm's `--dry-run` flag and Terraform's `plan` followed by `apply` exist because users about to make non-trivial changes want to see exactly what will happen before it happens, in a form they can read, and tools want to describe the changes only once. Applying the pattern to personal configuration rather than to infrastructure is the move worth naming; the discipline itself is well-established.

**Two layouts, namespaced and top-level, offered as first-class choices.** A one-layout installer forces a false dichotomy on the user. Migration tools in the web-framework space (codemods, `nx migrate`, framework-specific upgraders) tend to be conversion-only tools, which is why their adoption is slow and their reversibility is poor. Personal-configuration managers like GNU Stow and chezmoi tend to be coexistence-only tools, which is why they cannot help with adoption when an incumbent system already exists. Offering both layouts lets the user choose against their actual risk tolerance — cautious adoption through namespaced placement, committed adoption through top-level placement — with the same backup and reversibility infrastructure underneath either choice.

**Mechanical conversion as a deliberate refusal.** The loud option in 2026 for "convert an agent definition to a different framework's format" is to call a model and let it semantically rewrite the file. The installer does not do this. The agent-to-Role conversion is field mapping: name to name, description to description, tools to allowed_tools, model to model_tier. The body of the file is preserved unchanged. Refusing semantic rewriting buys predictability (the same source produces the same target every time), testability (the conversion is a pure function with deterministic output), absence of model dependency at deployment time, and zero token cost for any user. It also avoids the failure mode of an installer that subtly rewrites a user's careful prompt engineering and erodes trust before any work has been done.

**Project-signal detection driving per-Role relevance.** The inspection walks the project root for typed filesystem signals — directory existence, file existence, glob matching on code and configuration files — and proposes which curated Roles look relevant given what it finds. The closest analog is the kind of project-shape inference dbt does about model lineage; the AI-tooling space mostly does not detect relevance at install time at all. The signals feed into `<project>/.metaensemble/active-roles.yaml`, which the Coordinator reads at dispatch time so the installer's decisions persist into runtime rather than being one-shot scaffolding.

**Reversibility as a contract enforced by tests.** Every install action writes its provenance to a plan manifest at `<project>/.metaensemble/backups/<timestamp>/plan.json` (project-scope) or `~/.metaensemble/installs/<timestamp>/plan.json` (user-scope). Every backed-up file lives at a predictable path under the same root. `metaensemble unadopt` walks the project manifest in reverse and reverses every action it finds; `metaensemble user-teardown` does the same for user-scope actions. The full round-trip — `user-setup` + `adopt` in top-level layout, then `unadopt` + `user-teardown` — is tested in CI on every change. If a future action cannot be reversed, the test catches it before the change ships. This is the discipline Terraform applies to infrastructure state, applied here to the personal-configuration substrate the installer operates against.

The combination of these five choices, integrated into a single installer for AI-agent personal configurations, is what the v0.1.0 deployment story rests on. None of the techniques is novel in isolation. The integration of all of them, in this specific domain, appears to be original to this work — though the honest framing is "thoughtful integration of known patterns into a domain where they have not been applied" rather than "novel pattern invented from first principles." That framing is more defensible and more interesting than marketing language would be.

---

## Reference: commands

```bash
# One-command bring-up (recommended)
metaensemble setup
# → interactive wizard: lists every Claude Code project on this machine,
#   asks which one to adopt, asks for layout if user-setup hasn't run yet,
#   then vendors the runtime atomically into
#   ~/.metaensemble/runtime-versions/<id>/, swaps the ~/.metaensemble/runtime
#   symlink, and adopts the chosen project.
metaensemble setup --layout=namespaced       # skip the layout prompt; the project picker still asks
metaensemble setup --layout=top-level        # same, with top-level commands

# Pre-install (long form — same effect as the steps `setup` runs)
pip install metaensemble
# → drops the `metaensemble` console script into your venv and installs
#   the package into site-packages/. Editable install (`pip install -e .`)
#   works equivalently for development against a clone.

metaensemble user-setup --layout=namespaced  # or --layout=top-level
# → vendors the runtime, generates ~/.metaensemble/runtime/bin/me-run,
#   writes managed slash-command symlinks into ~/.claude/, and merges
#   hook entries into ~/.claude/settings.json.

metaensemble doctor
# → nine diagnostic checks (C1 and C6 marked SKIP in v0.1.0 as legacy);
#   --fix applies safe remediations.
metaensemble doctor --fix

# Survey
metaensemble inspect
# → writes .metaensemble/inspection-<timestamp>.md AND
#   .metaensemble/install-decisions.yaml (the editable choice surface)

# Install
metaensemble init
# → initialize project state (idempotent; install auto-calls this too)

metaensemble user-setup --layout=namespaced
# → namespaced install; respects install-decisions.yaml

metaensemble user-setup --layout=top-level
# → respects install-decisions.yaml; converts only where the user
#   chose take_ours / keep_both / convert; preserves everything else

metaensemble user-setup --layout=top-level --dry-run
# → print the install plan without applying anything

metaensemble adopt --dry-run
# → read-only project preview: reports the inspection/decision paths,
#   project-state init, `.gitignore` update, active-role write, and
#   per-agent actions without writing inspection artifacts or state.

# Recovery — project scope
metaensemble unadopt
# → walk <project>/.metaensemble/backups/<ts>/ in reverse, restore
#   converted agents from backup, strip the managed `.metaensemble/`
#   block from the project's `.gitignore`. User-level integration
#   (commands, hooks, statusline, vendored runtime) stays intact.

metaensemble unadopt --purge-state
# → above plus delete `<project>/.metaensemble/` entirely
#   (Ledger, manifests, briefs, inspection outputs, backups). Before deletion,
#   timestamped inspection artifacts are copied under
#   ~/.metaensemble/archives/project-inspections/<project-slug>/<timestamp>/.

# Recovery — user scope
metaensemble user-teardown
# → walk ~/.metaensemble/installs/<ts>/ in reverse, remove the managed
#   ~/.claude/ symlinks (commands, skill, output styles), strip
#   MetaEnsemble's hook entries from settings.json (restores the
#   oldest clean backup; falls back to surgical hook stripping when
#   no clean backup exists). Project state stays intact.

metaensemble user-teardown --purge-state
# → above plus delete `~/.metaensemble/` entirely
#   (vendored runtime under runtime/ and runtime-versions/,
#   user-layer Roles, install history, rate-limit cache)

metaensemble reconcile [--older-than-minutes N] [--dry-run]
# → write every pending sidecar older than N minutes (default 0 = all)
#   to the Ledger as outcome=`interrupted` (or `budget_exceeded` when
#   transcript evidence is available). Run this before any destructive
#   cleanup so the Ledger records the work before state is removed.

# Full local rollback (project + user) — chain the two purge-state forms:
metaensemble unadopt --purge-state          # from each adopted project
metaensemble user-teardown --purge-state    # once, from anywhere
# → restores agents from backup, removes project state, removes
#   user-level MetaEnsemble state and managed runtime links. Residue
#   report names `pip uninstall metaensemble` as the one-line follow-up.

metaensemble projects
# → list every project Claude Code has seen on this machine, with
#   MetaEnsemble install status (installed | init-only | not installed),
#   run count, and last-run timestamp. Use to see which projects you
#   have MetaEnsemble configured in across your machine — adopt runs
#   per-project (cd into the project and run `metaensemble adopt`)
#   and each project keeps its own .metaensemble/ state in isolation.

# Day-2 ops (read-only — no install state changes)
metaensemble limits     # current 5-hour window: used, remaining, cache, source
metaensemble standup    # full digest: window, last 24h Runs, top consumers, last 7d
metaensemble executors  # roster of Executors active in the last 30 days
metaensemble perf       # rolling latency / outcome / hook-error metrics
metaensemble ledger recent --limit N      # query the Ledger
metaensemble ledger by-executor <alias>
metaensemble ledger by-task <task-id>
metaensemble ledger window <window-id>
metaensemble relaunch <alias> [--full]    # print the relaunch context for an Executor

metaensemble manifest validate <path>
# → load + schema-validate a Manifest YAML file. YAML errors surface
#   with line:column and schema errors with the failing field path.
metaensemble manifest new-id
# → print one `hm-<UUIDv7>` Manifest id to stdout.
metaensemble manifest scaffold <task> [-o <path>]
# → write a starter Manifest YAML with TODO markers in every author-
#   supplied field (deliberately fails validation until the TODOs are
#   replaced). `-o` writes to a file; parent directories are created.

metaensemble export-agents
# → reverse-convert ~/.metaensemble/roles/* to ~/.claude/agents/*
#   (the documented escape hatch when backups are missing)
metaensemble export-agents --overwrite
metaensemble export-agents --target-dir <path>
metaensemble export-agents --user-only
metaensemble export-agents --project-only

# Evaluation
metaensemble eval --tier replay
# → run the harness in PR-gate replay mode against the shipped JSONL
#   cassettes. Zero API spend, deterministic, suitable for CI.
metaensemble eval --tier smoke
# → run one seed × classification smoke set live against Claude Code.
#   No tools, no project mutations; one no-tools Claude call classifies
#   the whole batch and tokens are prorated across items.
metaensemble eval --tier full --allow-live
# → release-gated. Requires --allow-live and explicit budget/seed
#   choices. D-8 blocks when orchestration overhead exceeds 2.0x the
#   best-prompt baseline; D-9 blocks when failed-run waste exceeds 10%
#   of evaluated tokens. See evals/README.md.
```

For day-to-day operation after install, see [`USER-GUIDE.md`](./USER-GUIDE.md). For the architectural design these install layouts enforce, see [`ARCHITECTURE.md`](./ARCHITECTURE.md). For the engineering rules the installer honors, see [`PERFORMANCE.md`](./PERFORMANCE.md).
