# MetaEnsemble Evaluation Harness

The harness exists so the quality-per-token claim — *the system around
the model is strong enough to deploy the competence the model already
has* — can be tested rather than asserted. Replay keeps the harness
deterministic in CI. Smoke and full tiers make live, side-effect-free
Claude Code calls and write measured reports under the caller's
`evals/reports/`.
The shipped classification data is one narrow smoke fixture, not a calibration set
and not a statement of product scope. MetaEnsemble is project-agnostic.

## Directory layout

```
evals/
├── README.md                  # this file
├── configs/
│   └── default.yaml           # eval-cycle parameters (seeds, budget, model routing)
├── datasets/
│   ├── suite_a/               # 8 software-engineering tasks
│   │   ├── README.md
│   │   └── tasks.yaml
│   └── suite_b/               # domain-specific classification smoke set
│       ├── README.md
│       └── items.yaml
├── baselines/                 # B1 / B2 / B3 baseline definitions
│   ├── b1_single_agent.yaml
│   ├── b2_single_agent_prompted.yaml
│   ├── b3_subagent_default.yaml
│   └── b4_best_prompt.yaml    # best-single-agent baseline
├── cassettes/                 # replay fixtures; bootstrap pack is non-empirical
├── runners/                   # cell × seed executors
│   ├── __init__.py
│   ├── api.py                 # tiered runner: replay / live / smoke
│   ├── metrics.py             # Wilson CI, pass@budget, quality_per_1k_tokens
│   └── replay.py              # cassette-based PR runner
└── reports/                   # generated reports per cycle (gitignored)
```

## Tiered evaluation

| Tier | When it runs | Live API calls | Budget |
|---|---|---|---|
| Replay | Every PR; reads recorded cassette responses. | No. | $0 |
| Smoke | Nightly cron or local preflight. 1 seed, `MM_full`, classification smoke set only. | Yes. | ~$0.30 default cap |
| Full | Release-gated. Defaults to 5 seeds × every configured cell. | Yes. | Principal-set cap |

The PR tier exists to keep regressions cheap to catch; the full tier
exists to certify a release. A release candidate is not allowed
to claim quality-per-token superiority unless the same report includes
baseline cells and MetaEnsemble cells over the same task set.

The release ships a compact `evals/cassettes/bootstrap.jsonl` pack so the
replay tier works in a clean checkout. That pack is deliberately marked
non-empirical; it verifies the harness mechanics, not MetaEnsemble's
quality claim. Live smoke/full reports are empirical for the cells and
datasets actually run; the report notes any skipped deferred fixtures.

## Headline metrics

The harness reports three co-primary metrics per cell:

| Metric | Definition |
|---|---|
| `pass@budget` | Pass-rate against the cell's per-task budget. A "win" that overspends does not count. |
| `quality_per_1k_tokens` | Average score across passing runs divided by tokens / 1000. Directly tests the efficiency thesis. |
| `orchestration_overhead_ratio` | MetaEnsemble token cost over the best single-agent baseline's token cost, on the same task. |

Plus the supporting metrics in `runners/metrics.py`:
`failed_run_token_waste`, `time_to_useful_deliverable`,
`minimum_useful_answer_score`.

For live reports, include these context fields in the release note or
system-card link: exact model IDs when the runtime exposes them, seed
count, cells run, skipped fixtures, total observed tokens, estimated vs
observed token error where available, and any cost-gate or Python
deliverable-check interventions.

## Suite A — software engineering (8 tasks)

Eight tasks drawn from the project's own backlog and from small
open-source repos. Each task has:

- A one-paragraph description (English).
- A frozen-commit starting state (commit SHA of the project under test).
- Graded acceptance criteria (build passes, tests count ≥ N, lint
  clean, manifest existed, deliverable file present).

See `evals/datasets/suite_a/tasks.yaml` for the current set.

The current Suite-A rows still contain deferred fixture SHAs. The live
full tier names those skipped tasks in the report rather than treating
them as passed or failed. Release certification across software tasks
requires replacing the deferred SHAs with real fixture repositories.

## Suite B — domain-specific classification (12 items, *smoke only*)

Twelve items is too few for calibration claims. The 12-item set in
`evals/datasets/suite_b/items.yaml` is the **smoke suite** that proves
the pipeline end-to-end. It is intentionally narrow; it does not make
MetaEnsemble domain-specific. Any release claim about a particular domain
needs its own independently labeled calibration set. The system card states
this limitation explicitly so no calibration claim is implied by the smoke
set.

## Running the harness

```bash
# PR-tier replay (no API calls).
metaensemble eval --tier replay --cells all

# Nightly smoke (one cell × one seed × classification smoke set only).
metaensemble eval --tier smoke

# Constrained full-tier check.
metaensemble eval --tier full --allow-live --cells MM_full --seeds 1 --budget-usd 0.30

# Release-gated full cycle once fixture SHAs and budget are set.
metaensemble eval --tier full --allow-live --cells all --seeds 5 --budget-usd 0.30
```

The output report lands in the current working directory at
`evals/reports/<UTC-date>-<tier>.md` and is linked from
`PERFORMANCE.md §4` once a cycle ships.

Supported flags:

| Flag | Meaning |
|---|---|
| `--cells all` or `--cells A,B` | Select all cells or a comma-separated subset. Smoke defaults to `MM_full`; replay/full default to all. |
| `--seeds N` | Override seed count. Smoke defaults to 1; replay/full default to `evals/configs/default.yaml`. |
| `--budget-usd X` | Override the live-tier per-run budget shown in preflight. |
| `--allow-live` | Required before the full tier proceeds past preflight. |

## Sign-off thresholds (D-8, D-9)

D-8 and D-9 are numerical full-tier release gates:

- **D-8 orchestration overhead**: any measured MetaEnsemble cell above
  `2.0x` the best single-agent prompt baseline (`B4`) blocks the full
  tier's ship verdict.
- **D-9 failed-run waste**: failed and budget-exceeded runs above `10%`
  of total evaluated tokens block the full tier's ship verdict.

The thresholds live in `evals/configs/default.yaml`. If a full run does
not include the `B4_best_prompt` baseline, D-8 is reported as not
evaluated rather than silently passing.
