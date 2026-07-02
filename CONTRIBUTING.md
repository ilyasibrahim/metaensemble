# Contributing to MetaEnsemble

Thanks for considering a contribution. MetaEnsemble is a young project with a
narrow, deliberate scope — an organizational layer for ensembles of cognitive
agents — and contributions that respect that scope land quickly.

## Development setup

```bash
git clone https://github.com/ilyasibrahim/metaensemble.git
cd metaensemble
pip install -e ".[test]"     # one-shot reproducer: everything the suite needs
python -m pytest metaensemble/tests -q
```

The `[test]` extras include the real quality-gate runners (`bandit`, `ruff`,
`radon`, `coverage`) so the gate integration tests assert real tool behavior
rather than tool-absent skip paths. Python 3.10–3.13 are supported; CI runs
the full matrix.

## Before you write code

1. **Read `docs/PERFORMANCE.md` §3 (Engineering rules).** They are
   load-bearing and review-blocking — R1 (named-query API only; no ad-hoc SQL
   outside `lib/ledger.py`) and R7 (hooks are fast, idempotent, and never
   block on their own failures) reject more PRs than anything else.
2. **Check `docs/ARCHITECTURE.md`** for the layer you are touching. Core
   (`metaensemble/`) is project-agnostic by a hard rule; project-specific
   knowledge belongs in `<project>/.metaensemble/`.
3. **Keep docs in sync.** A change that alters user-facing behavior updates
   the affected document in the same pull request — README, USER-GUIDE,
   ARCHITECTURE, GLOSSARY, SYSTEM-CARD, or DEPLOYMENT, whichever describes
   the surface you changed, plus a CHANGELOG entry under `[Unreleased]`.

## Commit and PR conventions

- [Conventional Commits](https://www.conventionalcommits.org):
  `<type>(<scope>): <message>` — e.g. `fix(reconcile): quarantine sidecars
  that fail to record`.
- One logical change per commit; tests in the same commit as the code they
  pin.
- PRs describe the behavior change first and the mechanism second. If a
  performance rule is being relaxed, the PR must propose the PERFORMANCE.md
  revision in the same diff (§7 of that document).

## Tests

- Every claimed behavior gets a test that would fail if the claim were
  false. The suite is the contract; "it works locally" is not.
- Tests must not touch real user state. The suite isolates `HOME` and the
  hook error log; if your test needs a project layout, build it under
  `tmp_path`.
- Perf-sensitive paths have CI-gated benchmarks (`test_perf_*.py`). Run them
  before opening a PR that touches hooks, the Ledger, or schema validation.

## Where to start

Issues labeled `good first issue` are scoped to be completable without deep
context. Issues labeled `roadmap` track the larger workstreams (eval
calibration, plugin packaging, MCP surface, OTel export) — comment there
before starting anything sizable so the approach is agreed first.

## Security

Do not open public issues for vulnerabilities. See [SECURITY.md](SECURITY.md)
for the reporting process and the trust model (what the hooks may read and
write, and why).

## License

MIT. By contributing you agree your contributions are licensed under the
project license.
