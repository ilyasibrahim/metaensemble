# MetaEnsemble System Card

Version: 0.2.0
Last updated: 2026-07-03
Author: MetaEnsemble project

This system card sits next to MetaEnsemble's documented capability
claims and names — as plainly as possible — what the system does well,
what it does not yet do well, and which limitations the Principal should
account for.

## Intended use

MetaEnsemble is a personal-scale orchestration substrate for an
individual Principal running ensembles of cognitive
agents through Claude Code. It is meant for solo founders, individual
researchers, and small teams who want persistent specialist identities,
typed handoffs, institutional memory, calibrated escalation, and
accountable review without standing up a hosted multi-agent platform.

Concrete use cases the v0.2.0 release supports:

- Dispatching one or more Executors of a Role for software-engineering
  tasks (implementation, refactor, review, doc updates, test addition).
- Maintaining persistent Executor identities across sessions so a
  reviewer Executor's history of dissent shapes its next review.
- Recording Run metadata (model, tool use, token cost, files touched,
  outcome) for later audit and continuity.
- Surface-level dispatch protocols: solo, fanout, consensus, peer
  review. v0.2.0 validates planning, markers, and guardrails for these
  modes; it does not yet publish a live end-to-end reliability metric for
  each multi-Executor mode.
- Local cost gating via threshold-based decisions surfaced as
  structured options.
- Deliverable checks for successful Runs: built-in five-axis Python
  runners for `.py` outputs (when the local toolchain is installed), and
  the same five axes via project-configured `axis_commands` in
  `quality.yaml` for non-Python outputs.

## Intended *not*-use

MetaEnsemble is **not** intended for:

- High-stakes irreversible decisions without explicit Principal review.
- Production-scale multi-tenant workloads. The Ledger is SQLite on the
  Principal's machine; concurrent multi-runtime use against the same
  project is not supported in v0.2.0.
- Domain-specific classification or scoring where calibrated confidence is
  required and not yet measured for that domain. MetaEnsemble can route and
  record such work, but the v0.2.0 eval harness does not certify domain
  accuracy.
- Replacing human specialist judgment on tasks that require tacit
  knowledge, embodied practice, or political/social context that does
  not transit through text.

## Evaluated capabilities

The evaluation harness exists at `evals/` and is documented in
`evals/README.md`. As of v0.2.0:

| Capability | Coverage | Evidence |
|---|---|---|
| Install / uninstall lifecycle | Unit + integration tests; uninstall round-trip and idempotency in CI. | `metaensemble/tests/test_installer.py` (56 tests) |
| Reconciliation of stranded pending sidecars | Two-layer (Stop-hook + on-demand CLI); 9 tests. Validated on 2 real stranded sidecars from this repo. | `metaensemble/tests/test_reconcile.py` |
| Protocol enforcement (`fanout`/`consensus` N≥2) | Deterministic block in PreToolUse hook; 4 tests. | `metaensemble/tests/test_hooks.py::test_pre_task_rejects_fanout_one` and siblings |
| Ledger field completeness (role version, model, tool_use, files_touched, deliverable_ref, cache tokens) | Schema migrations 002/003 + transcript walker + file-tool event hook + post-task hook wiring + schema-completeness tests. | `metaensemble/tests/test_ledger_schema_complete.py`, `metaensemble/tests/test_hooks.py::test_file_event_records_files_touched_for_post_task` |
| Hook security invariants | Pattern + AST audit. | `metaensemble/tests/test_hook_security_invariants.py` |
| Eval harness | Replay/smoke/full tiers + Wilson CI + pass@budget metrics. Replay has a non-empirical bootstrap cassette pack; smoke/full make live side-effect-free classification-smoke calls. Release confidence requires a fresh live run whose report is linked here. | `metaensemble/tests/test_eval_harness.py`, `evals/cassettes/README.md` |
| Performance benchmarks | Hook p95 <100ms, ledger query p95 <10ms — `test_perf_hooks.py`, `test_perf_ledger.py`. | CI-gated. |

**Calibrated AI-quality claims are not yet supported.** The harness now
reports `pass@budget` and `quality_per_1k_tokens` for live smoke/full
runs. On 2026-05-19, a constrained `MM_full` smoke run
scored 12/12 on the shipped smoke set. That is pipeline evidence, not a
calibration result and not a baseline superiority result. The claim
that the ensemble improves quality per token over a single-agent
baseline remains a product hypothesis until all baseline cells run
against real Suite-A fixtures and an independently labeled domain set.

## Evidence FAQ

### Do you have evidence MetaEnsemble produces higher-quality outputs?

Not yet at the standard required for a product claim. v0.2.0 has strong
engineering evidence for the local substrate — install lifecycle, hooks,
Ledger persistence, protocol guards, replay metrics, and Python
deliverable checks. It does **not** yet have a calibrated win-rate,
effect-size estimate, routing-accuracy metric, or per-mode reliability
metric showing that multi-Executor orchestration beats a strong
single-agent baseline on quality per token. Treat the quality-per-token
thesis as the hypothesis the harness is built to measure, not as an
established result.

### What evaluation numbers are required before stronger claims?

At minimum: a fresh live eval with baseline cells and MetaEnsemble cells
on the same task set, exact model IDs recorded, token estimates compared
with observed token usage, and the report checked into or linked from
this system card. A small n=20 release smoke is useful operational
evidence, but it is still not calibration.

## Known limitations

### Calibration

The shipped classification smoke suite has 12 domain-specific classification
items. This is a **smoke suite**: it confirms the pipeline runs end-to-end.
It is **not** a calibration set, and it does not define MetaEnsemble's
product scope. MetaEnsemble is project-agnostic; the smoke suite is one narrow
fixture used to exercise the harness.

Calibration claims require an independently labeled set that matches the
domain being claimed. Until that exists, any "High confidence" reading from
the model should be treated as suggestive, not authoritative.

### Failure-mode catalog

A documented failure-mode catalog for each claimed domain is a deliverable
of the first full eval cycle. The smoke suite does not exercise enough
domain variety to support such a catalog in v0.2.0.

### Model recording

The `runs.model` column records the runtime-observed model when a
transcript is available, and `runs.model_source` records whether that
value came from `transcript`, a fresh session-matched `statusline`
snapshot, or `tier_fallback`. Hook payloads do not carry the model
directly; transcript walking is the most exact source. When a transcript
is absent (some runtime versions, broken JSONL), the recorded model may
fall back to the requested tier, which is not an exact model ID. The
`requested_model_tier` column carries the Manifest's request separately
so drift is visible rather than hidden.

### Outcome detection (F-3)

The Stop hook reconciles pending sidecars belonging to its session.
External terminations (`kill -9`, budget exhaustion that exits the
runtime before Stop fires) do not trigger the Stop hook. The
on-demand `metaensemble reconcile` CLI and the session-start sweep
catch these. When the pending sidecar contains a parent transcript path
and that transcript contains Claude Code's max-budget marker, reconcile
records `outcome="budget_exceeded"`; otherwise it records
`outcome="interrupted"` with a generic failure reason. Older sidecars
created before transcript-path recording cannot be reclassified.

### Slash-command protocol enforcement

The PreToolUse hook is the enforceable line for protocol violations
(`/dispatch --fanout 1`, malformed markers). The Markdown slash
command file is *advisory*: it tells the Coordinator what to do, but
the Coordinator is a model and can deviate. The PreToolUse guard
catches the case where the model issues the dispatch anyway. A
third-line defense (a runtime-level pre-model argument validator)
would close the remaining gap; the v0.2.0 release does not include
it because the runtime does not expose the necessary hook.

### Eval budget (D-5, D-8, D-9)

The harness reports `pass@budget`, `quality_per_1k_tokens`, and
`orchestration_overhead_ratio`. Full-tier release gates now have
numerical thresholds:

- **D-8 orchestration overhead**: `MM_full` and ablation cells must stay
  at or below `2.0x` the best-prompt single-agent baseline (`B4`).
- **D-9 failed-run waste**: failed and budget-exceeded runs must account
  for no more than `10%` of total evaluated tokens.

Replay still gates harness mechanics only; smoke remains a live pipeline
preflight. These thresholds apply to the full tier once the required live
fixtures and baseline cells are present in the run.

## Operating constraints

- **Platform**: Tested on macOS Darwin 25.4 and Linux (CI matrix:
  Python 3.10, 3.11, 3.12, 3.13). Windows is not currently exercised.
- **Runtime**: Requires Claude Code with PreToolUse / PostToolUse /
  SessionStart / SubagentStop / Stop hook events. Older runtime versions
  that lack the Stop event will lose Layer-1 reconciliation; the on-demand
  `metaensemble reconcile` CLI is the workaround. Runtimes that lack the
  SubagentStop event cannot finalize background-dispatched Runs at
  subagent stop; those Runs are recovered by the reconcile sweep instead.
- **Concurrency**: Single-runtime per project. A future protocol may
  add file-locking around the pending-sidecar directory; v0.2.0 does
  not.
- **Local storage**: The Ledger is SQLite + a JSONL mirror under
  `<project>/.metaensemble/state/`. Measured size after 1,000 fully
  populated Runs is approximately 1.5 MB, growing linearly at ~1.6 KiB
  per Run (PERFORMANCE.md §5.1).

## Risks

| Risk | Mitigation |
|---|---|
| The Principal misreads an uncalibrated model label as authoritative. | This system card and the failure-mode catalog name the limitation. Domain-specific confidence claims require an independently labeled calibration set. |
| Stale pending sidecars accumulate after a crash. | Two-layer reconcile + on-demand CLI catch them. The session-start sweep runs every session; a sidecar that repeatedly fails to record is quarantined under `pending/quarantine/` instead of retrying forever. |
| Python quality runners are absent in the Principal's environment. | The runners degrade to "tool not installed" findings rather than failing closed. The default install profile `[test]` includes them. |
| Schema migrations conflict with a third-party tool reading the SQLite file directly. | The 001/002/003 migrations are documented; new migrations bump the additive backfill list, not the table shape, when possible. |

## Reporting issues

File issues on the project repository with the `system-card` label if
a stated limitation is wrong or out of date, the `security` label if
the issue is a security concern, or the default label otherwise.
