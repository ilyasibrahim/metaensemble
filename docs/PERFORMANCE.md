# Performance

> *Engineering constraints, budgets, and rules that bind every implementation choice in MetaEnsemble. Changes that violate these constraints are flagged in review and rejected unless the constraints themselves are explicitly revised here.*

---

## Why this document exists

The architectural value of MetaEnsemble — lower coordination costs, persistent identity, observable runs — is realized only if the implementation preserves the cost and time properties the architecture promises. Token savings predicted by ARCHITECTURE.md and by the published intellectual argument are not automatic; they depend on specific implementation choices that, if violated, can erase the savings instantly. This document captures those choices as binding engineering constraints, with named rules and test commitments that any implementation in any language, on any future revision, must honor.

Three audiences:

1. **Authors of new implementation work** (maintainers and contributors). Read §3 before writing code that touches state, schemas, or hooks.
2. **Reviewers.** §3 is the review checklist. §4 benchmarks are CI gates.
3. **Readers of ARCHITECTURE.md** and of the published intellectual argument who want to know whether the architectural claims are backed by enforceable engineering practice.

This document is referenced from [ARCHITECTURE.md §15](./ARCHITECTURE.md) and is binding alongside that section.

---

## 1. Token budgets

MetaEnsemble's token economics depend on four explicit levers and three explicit risks. Both must remain visible in the implementation.

### 1.1 Where tokens are saved (the four levers)

| Lever | Mechanism | Expected magnitude |
|---|---|---|
| **Per-Role model tiering** | Roles declare model tier in spec; only Roles needing Opus get Opus | ~5× saving when shifting Opus→Sonnet, ~3× Sonnet→Haiku |
| **Manifest-driven handoffs** | Receiving Executor reads typed pointers (file paths, line ranges, schemas), does not re-derive context | Eliminates 500–2,000 tokens of context re-derivation per handoff |
| **Wire-format Briefs** | Inter-Executor messages are terse JSON, never English prose | ~5× smaller than equivalent prose context-injection |
| **Cross-session relaunch** | `/relaunch <alias>` loads last Brief + Deliverable summary, not full history | Avoids re-introduction cost, which scales N² across sessions |

Each lever is testable against synthetic workloads in `metaensemble/tests/test_perf_handoff.py`.

### 1.2 Where tokens could be wasted (the three risks)

The architecture reduces token use only if implementations actively engineer against three named failure modes.

**Risk T1 — Loading the Ledger into Coordinator context.**
A naive `SELECT * FROM runs` that pulls thousands of rows into a prompt destroys the savings instantly. Forbidden by Rule R1 (Named-query API only) and Rule R5 (Bounded result sets).

**Risk T2 — Redundant artifact paths.**
If a Brief and a Manifest and prose context all flow to the same Executor, the system triple-pays for one handoff. Forbidden by the one-artifact-per-audience principle: Brief for machine, Manifest for handoff context, Deliverable for human. Reviewers reject any change that routes the same content through multiple artifact channels to the same recipient.

**Risk T3 — Re-validating against uncached schemas.**
Compiling a JSON Schema validator on every Brief or Manifest validation adds 5–50ms per call and burns CPU that could be used for productive work. Forbidden by Rule R3 (Compiled-validator cache).

---

## 2. Time budgets

Time savings come from parallelism and from skipping re-introduction. Time costs come from hook overhead. The numbers below are targets enforced by the test suite.

| Operation | Target | Verified by |
|---|---|---|
| Hook latency (each hook) | p95 < 100ms on 10k-row Ledger | `metaensemble/tests/test_perf_hooks.py` |
| Ledger query latency | p95 < 10ms on 10k-row Ledger | `metaensemble/tests/test_perf_ledger.py` |
| Single-Task overhead vs baseline | < 5% of total wall-clock | `metaensemble/tests/test_perf_e2e.py` |
| Multi-Task fan-out (N=3) | Wall-clock = max(T₁, T₂, T₃) + ~10% coordination overhead | Same |
| Cross-session relaunch (cheap) | < 3s, < 500 tokens | Same |
| Python deliverable check (PostToolUse) — five runners | p95 < 5s on changed-file set (typically 1–20 files) | Field-measured for successful Runs whose Manifest declares Python deliverables; runners are subprocesses (bandit, ruff, radon, coverage) so the latency floor is dominated by tool startup |

The single-Task overhead target of <5% is calibrated against typical model-call latency (1–30s). Hook overhead is dwarfed by model latency in real workflows. If model latency drops dramatically (e.g., new fast-inference architectures), this target is revisited.

The Python deliverable check has its own latency profile because it shells out to external tools. Its target (p95 < 5s) sits well above the <100ms hook budget by design — running five static analysers on changed files is genuinely more expensive than a numeric threshold check, and the check is asynchronous to the dispatch (the Deliverable has already been produced when it fires). Each runner is independent and skips gracefully when its tool is absent, so a partial install never blocks PostToolUse.

---

## 3. Engineering rules (load-bearing)

These rules are binding on every implementation. They are enumerated so reviewers have a concrete checklist.

### R1 — Named-query API only

All access to the Ledger goes through named query functions in `metaensemble/lib/ledger.py`. Each function has a documented complexity bound in big-O notation, with the index it depends on. Ad-hoc SQL is forbidden in hooks, skills, and slash commands. The Coordinator never writes raw SQL.

The initial query API (revisable only via this document):

```python
def get_recent_runs(limit: int = 50, since: datetime | None = None) -> list[Run]:
    """O(log N + K) using idx_runs_ended_ts. K = limit (default 50)."""

def get_runs_by_executor(executor_id: str, limit: int = 50) -> list[Run]:
    """O(log N + K) using idx_runs_executor."""

def get_runs_by_task(task_id: str, limit: int = 50) -> list[Run]:
    """O(log N + K) using idx_runs_task."""

def get_window_burn(window_id: str) -> WindowSummary:
    """O(K) using idx_runs_window. K = runs in window."""

def get_executor_by_alias(alias: str) -> Executor | None:
    """O(log N) using idx_executors_alias."""

def get_active_executors(since: datetime, limit: int = 50) -> list[Executor]:
    """O(log N + K) using idx_executors_last_seen."""

def get_role(role_id: str) -> Role | None:
    """O(log N) using roles primary key."""

def get_executor(executor_id: str) -> Executor | None:
    """O(log N) using executors primary key."""

def count_runs_by_executor(executor_id: str) -> int:
    """O(log N) using idx_runs_executor."""

def ensure_role(...) -> None:
    """O(log N) insert-if-absent using roles primary key."""

def ensure_task(...) -> None:
    """O(log N) insert-if-absent using tasks primary key."""

def get_outcome_counts() -> dict[str, int]:
    """O(N) single-pass GROUP BY outcome; rides no index by design.
    Output bounded by the six ALLOWED_RUN_OUTCOMES literals (R5).
    Principal-invoked only (`metaensemble stats`), never hook-adjacent."""

def get_executor_run_counts(limit: int = 5) -> list[ExecutorRunCount]:
    """O(N + E log E) riding idx_runs_executor as a covering index plus
    the executors primary key for the join; LIMIT-bounded (R5)."""
```

Adding a new query function is a small, reviewable change. Introducing raw SQL outside the lib is release-blocking.

### R2 — Index-first SQLite schema

Every column used in a `WHERE`, `ORDER BY`, or `JOIN` clause has an index from migration 001. Indices ship in `metaensemble/state/migrations/001_init.sql` alongside the table definitions, never as an afterthought.

```sql
CREATE INDEX idx_runs_window         ON runs(window_id);
CREATE INDEX idx_runs_executor       ON runs(executor_id);
CREATE INDEX idx_runs_task           ON runs(task_id);
CREATE INDEX idx_runs_ended_ts       ON runs(ended_ts);
CREATE INDEX idx_executors_alias     ON executors(alias);
CREATE INDEX idx_executors_last_seen ON executors(last_seen_ts);
CREATE INDEX idx_executors_role      ON executors(role_id);
CREATE INDEX idx_tasks_status        ON tasks(status);
CREATE INDEX idx_tasks_parent        ON tasks(parent_task_id);
```

A new query that introduces a new `WHERE` column requires a corresponding index in the migration that introduces it.

### R3 — Compiled-validator cache

JSON Schema validators are compiled at module load and cached. `metaensemble/lib/manifest.py` exposes the Manifest and Brief validation functions that perform O(payload size) work, never O(schema size + payload size). Test target: a validator-cache hit takes <1ms.

### R4 — Connection pooling

One SQLite connection per process, opened once and reused. The connection enables foreign keys (`PRAGMA foreign_keys = ON`) and uses WAL mode for concurrent read access (`PRAGMA journal_mode = WAL`). Hooks that run in subprocesses each open their own connection at process start and let it close on process exit. No open/close per query.

### R5 — Bounded result sets

Every query function takes a `limit` argument with a small default (typically 50). The Coordinator pulls summaries first and drills down only when needed. No API path reads "the whole Ledger" into context. Result sets larger than 1,000 rows must be streamed or paginated, never loaded whole.

### R6 — One artifact per audience

Briefs go to machines. Deliverables go to humans. Manifests are handoff contracts read by the receiving Executor. The same content does not flow through multiple artifact channels to the same recipient. Reviewers reject any change that routes a Manifest's content into a Brief's prose context, or that copies a Deliverable summary into a Brief.

### R7 — Hooks are fast and idempotent

Hook scripts in `metaensemble/hooks/` complete in <100ms p95 with no model calls and no network I/O. Each hook is idempotent: running it twice on the same input produces the same state. Hooks log failures to `.metaensemble/hooks/log.jsonl` but never block the calling operation. A failed PostToolUse log produces a degraded run, never a stalled one.

---

## 4. Test commitments

Three named benchmarks ship as part of B1. Each benchmark is a CI gate: a regression in any of them blocks merge.

### Benchmark 1 — `metaensemble/tests/test_perf_handoff.py`

Validates the token-economics claim.

- Synthesizes a Manifest + Brief pair for a typical handoff scenario (representative file pointers, schema references, expected outputs).
- Synthesizes the equivalent prose context-injection for the same handoff.
- Asserts: the typed Brief is at most 1/3 the token count of the equivalent prose.
- Asserts: the Manifest content has zero overlap with the Brief content.

### Benchmark 2 — `metaensemble/tests/test_perf_hooks.py`

Validates hook latency.

- Generates 10,000 synthetic Run rows in the Ledger.
- Runs each hook (`session-start`, `pre-task`, `post-task`, `deliverable-sync`, `session-summary`) 100 times against the populated Ledger.
- Asserts: p95 latency for each hook is <100ms.

### Benchmark 3 — `metaensemble/tests/test_perf_ledger.py`

Validates query latency.

- Populates the Ledger with 10,000 rows.
- Runs each named query function 100 times with varying parameters.
- Asserts: p95 latency for each query is <10ms.
- Asserts: each query plan uses the expected index (verified via `EXPLAIN QUERY PLAN`).

These benchmarks run in CI on every pull request. Failure blocks merge.

### Benchmark 4 — `evals/` evaluation harness

Validates the quality-per-token claim — that the system around the model improves output quality relative to token spend.

- Three baselines (single-agent, single-agent + system prompt, runtime default subagent) plus a best-prompt baseline (B4) that gets the same Manifest pointers and acceptance criteria MetaEnsemble does.
- Three ablations (`MM − Manifest`, `MM − Ledger`, `MM − quality gate`).
- N seeds per cell (default 5; smoke tier uses 1).
- Per-cell metrics: `pass@budget` with Wilson 95% CI, `quality_per_1k_tokens`, `orchestration_overhead_ratio` (MM tokens / B4 tokens), `failed_run_token_waste`, `time_to_useful_deliverable_ms_p50`.
- Three tiers: `replay` (cassette-based, PR-gate, no API spend), `smoke` (nightly, one seed), `full` (release-gated, real money).

The replay tier runs in CI on every PR. v0.1.0's shipped replay pack is a non-empirical bootstrap fixture, so it gates harness regressions, not quality claims. The smoke tier runs live classification-smoke checks with one seed and the `MM_full` cell by default; the full tier is release-gated and requires `--allow-live` plus explicit budget/seed choices before it spends money. D-8 gates orchestration overhead at `<= 2.0x` the best-prompt single-agent baseline, and D-9 gates failed-run waste at `<= 10%` of total evaluated tokens. See `evals/README.md` for the directory layout and `SYSTEM-CARD.md` for the calibration caveats around the smoke fixture.

---

## 5. Observability

Performance commitments matter only if regressions are visible to the Principal. MetaEnsemble exposes live performance state through two surfaces.

**`/limits`** — Rolling 5-hour token burn, per-Executor breakdown, top-burning Tasks. Specified in ARCHITECTURE §9.

**`/perf`** — Rolling p95 hook latency, query latency, and Brief/Deliverable size distributions over the last 24 hours. Surfaces drift before it becomes a problem.

If `/perf` shows hook latency p95 above the budget for an extended period, the Principal sees the warning before the next workflow is dispatched. The system reports its own degradation rather than waiting for the Principal to notice through symptoms.

### 5.1 Ledger growth over a project's life

The Ledger grows linearly with Run count, and the constant is small. Measured on schema 001–003 with fully populated rows (provenance, tool-use counts, quality findings, cache-token fields — the worst realistic case):

| Runs | SQLite | JSONL mirror | Total |
|---|---|---|---|
| 500 | 352 KiB | 474 KiB | 0.81 MiB |
| 1,000 | 616 KiB | 948 KiB | 1.53 MiB |

That is roughly **1.6 KiB per Run**, with the JSONL mirror accounting for ~60% of it (the mirror trades bytes for replayability; see ARCHITECTURE on the two-layer Ledger). Projected forward: a project that dispatches 50 Runs a day for a working year lands near 20 MiB — well inside what a single SQLite writer sustains. The number to watch is not disk but writer concurrency, covered in §6: if parallel Executors ever contend on writes, the data layer is rethought before the file size matters.

Briefs and Deliverables are files on disk outside the Ledger and dominate total footprint on prose-heavy projects; the Ledger stores paths to them, not their content.

---

## 6. Honest caveats

The constraints above are calibrated against assumptions that may not hold indefinitely.

**SQLite is single-writer.** If the Ledger ever grows past what one writer can sustain (concurrent writes from many parallel Executors, for example), the data layer has to be rethought. Until then, SQLite with WAL is correct and substantially simpler than the alternatives.

**Hook overhead is small relative to model latency.** If model latency drops dramatically (new fast-inference architectures pushing Task latency below 1s), the 5% single-Task overhead target may need to tighten. The target is revisited when new model classes ship.

**Fan-out parallelism is model-rate-limited.** Spawning ten Executors in parallel does not give 10× speedup if the model API rate-limits at lower concurrency. The empirical wall-clock advantage of fan-out depends on the rate limits in effect.

**Schema validation cost grows with schema complexity.** Current Manifest and Brief schemas are deliberately simple. If they become substantially more complex (deep nesting, regex constraints, conditional schemas), Rule R3 may need refinement. The cache mitigates compile cost but does not eliminate per-payload validation cost.

When any of these assumptions stops holding, this document is revised. The architecture is the contract. The rules in §3 and the tests in §4 are how the implementation honors it.

---

## 7. How to use this document

**If you are implementing a new component:**

1. Read §3 (Engineering rules) before writing any code that touches state, schemas, or hooks.
2. Run the benchmarks in §4 before opening a pull request.
3. If your change requires violating any rule in §3, propose an explicit revision to this document in the same pull request. The revision must explain why the rule is being relaxed, what new constraint replaces it, and what test now proves the new constraint.

**If you are reviewing a pull request:**

1. The checklist in §3 is the review checklist.
2. Performance regressions in §4 benchmarks are release-blocking.
3. Any new query against the Ledger requires a named function in `metaensemble/lib/ledger.py` and a corresponding index in the migrations.

**If you are reading for context:**

The architectural value claims in ARCHITECTURE.md depend on the engineering rules here. The benchmarks in §4 are how those claims become testable. The honest caveats in §6 are where the architecture meets reality.
