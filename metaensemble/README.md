# metaensemble/ — MetaEnsemble project-agnostic substrate

This is the `core` package: schemas, SQLite Ledger, identity generation,
and Manifest validation. It is binding on every project that adopts
MetaEnsemble (see ARCHITECTURE.md §4 — Portability) and contains zero
project-specific assumptions.

## Layout

```
metaensemble/
├── schemas/
│   ├── role.schema.json      # Role spec (frontmatter of roles/<role_id>.md)
│   ├── brief.schema.json     # Wire-format JSON between Executors
│   └── manifest.schema.json  # Typed handoff contract (YAML)
├── state/
│   └── migrations/
│       └── 001_init.sql      # Ledger DDL + indices (PERFORMANCE.md R2)
├── lib/
│   ├── ids.py                # UUIDv7 + alias generation
│   ├── ledger.py             # Named-query API (PERFORMANCE.md R1)
│   └── manifest.py           # Schema validation, validator-cached (R3)
└── tests/
    ├── conftest.py           # Shared fixtures
    ├── test_ids.py
    ├── test_ledger.py
    ├── test_manifest.py
    ├── test_perf_handoff.py  # PERFORMANCE.md §4 Benchmark 1
    └── test_perf_ledger.py   # PERFORMANCE.md §4 Benchmark 3
```

## Running tests

From the repo root:

```bash
pip install -e '.[test]'
pytest metaensemble/tests
```

The performance benchmarks (`test_perf_*.py`) run as part of the same
suite. Latency thresholds in `test_perf_ledger.py` assume modest hardware;
adjust `P95_BUDGET_MS` if running on constrained CI runners.

## Reserved for Later Releases

- **Observability-driven recruitment** (v0.2): reserved for a later release.
- **Role sunset/archive** (v0.2): v0.1.0 ships the Role fields; manual archive and the 60-day auto-flag follow.
- **Sophisticated peer-review dissent synthesis** (v0.2): the lib ships the minority-surfacing form; richer synthesis requires real Ledger data to calibrate.

## B3 additions

The Principal-facing surface landed in B3. New under `metaensemble/`:

```
metaensemble/
├── commands/                    # slash commands the Principal invokes
│   ├── dispatch.md
│   ├── relaunch.md
│   ├── executors.md
│   ├── standup.md
│   ├── limits.md
│   ├── perf.md
│   └── ledger.md
├── tools/                       # CLI helpers the commands delegate to
│   ├── limits.py
│   ├── standup.py
│   ├── executors.py
│   ├── ledger.py
│   └── perf.py
├── roles/                       # curated, project-agnostic Role specs
│   ├── architect.md
│   ├── backend.md
│   ├── frontend.md
│   ├── code-quality.md
│   ├── test-engineer.md
│   ├── devops.md
│   └── docs.md
└── cli.py                       # `metaensemble` console script
```

After `pip install -e .`, the CLI is available as:

```
metaensemble init                    # bootstrap .metaensemble/ in the current project
metaensemble limits                  # current 5-hour window status
metaensemble standup                 # daily digest
metaensemble executors               # list active Executors
metaensemble perf                    # rolling perf metrics
metaensemble ledger recent --limit N # query the Ledger via named subcommands
```

**Install path.** Wheel install is the supported flow in v0.1.0 (`pip install metaensemble`). The wheel ships the full asset set under `site-packages/metaensemble/`. `metaensemble user-setup` vendors a self-contained snapshot of those assets into `~/.metaensemble/runtime/` (via an atomic symlink swap into a versioned directory under `~/.metaensemble/runtime-versions/`) and generates the runner at `~/.metaensemble/runtime/bin/me-run`. After that step completes, the vendored runtime is independent of the package install — you can `pip uninstall` and reinstall, or upgrade the package, and the next `user-setup` re-vendors atomically.

Editable installs (`pip install -e .`) remain supported for development. Doctor checks `C1` and `C6` are legacy in v0.1.0 and return `SKIP`; `C9 Runtime vendored` is the active sanity check on the vendored runtime.

Each tool reads through the named-query API in `metaensemble/lib/ledger.py`. No
raw SQL anywhere in `tools/` (PERFORMANCE.md §3 R1).

## B2 additions

The runtime layer landed in B2. New under `metaensemble/`:

```
metaensemble/
├── hooks/
│   ├── _common.py            # shared utilities (state paths, stdin/stdout JSON contract)
│   ├── session_start.py      # SessionStart hook — Registry summary, window status
│   ├── pre_task.py           # PreToolUse (Task) — Manifest validation + cost gate
│   ├── post_task.py          # PostToolUse (Task) — Ledger append (sync) / defer to SubagentStop (background)
│   ├── deliverable_sync.py   # PostToolUse (Write) — Deliverable index update
│   ├── subagent_stop.py      # SubagentStop hook — finalize background-dispatched Runs by agentId
│   └── session_summary.py    # Stop hook — session digest
├── skills/
│   └── metaensemble-protocol/SKILL.md   # the Coordinator's operational protocol
├── output-styles/
│   ├── wire.md               # terse JSON for inter-Executor Briefs
│   └── deliverable.md        # full English Markdown for human-facing Deliverables
├── config/
│   └── budgets.example.yaml  # cost-gate configuration template
└── lib/
    ├── config.py             # budgets.yaml loader with Core / User / Project merging
    └── cost_gate.py          # three-state escalation logic (auto / notify / block)
```

The hooks read JSON from stdin and emit a JSON decision on stdout, following the
agent runtime's lifecycle contract. They are invoked as subprocesses; each
honors the <100ms p95 budget on a 10k-row Ledger (PERFORMANCE.md §4
Benchmark 2 — verified by `metaensemble/tests/test_perf_hooks.py`).

## Engineering rules

Any change to this package must comply with the rules in
[`PERFORMANCE.md`](../docs/PERFORMANCE.md) §3 (R1–R7). Reviewers reject changes
that introduce ad-hoc SQL outside `lib/ledger.py`, queries without indices,
or hooks with model calls. The CI gates in §4 are non-negotiable.
