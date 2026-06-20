# Changelog

All notable changes to MetaEnsemble are recorded here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Added

**Background and fan-out dispatch lifecycle.** A `SubagentStop` hook (`subagent_stop.py`) finalizes background-dispatched Runs, correlated strictly by the runtime `agentId` so concurrent and fan-out dispatches in one session finalize independently. `post_task.py` now detects the Agent tool's launch stub, reconciles it to the pre-task sidecar by `tool_use_id`, records an `agentId`-keyed active-dispatch marker so the subagent's file writes stay authorized, and defers finalization to `SubagentStop`. Synchronous and background dispatches share one `finalize_pending` path, so both produce identical Run records.

**Configurable report location.** `install-decisions.yaml` gains a `report_root` field. Greenfield projects default to the machine-local, ignored `.metaensemble/reports`; existing projects keep a detected convention such as `.claude/reports`. The Coordinator writes a synthesis report file only when the Manifest explicitly declares it, synthesizing in the Principal-facing response otherwise.

### Fixed

**Idempotent Run recording.** `append_run` now inserts under `ON CONFLICT(run_id) DO NOTHING` and mirrors to JSONL only on a real insert; reconcile skips any sidecar whose `run_id` is already recorded. A background Run finalized by `SubagentStop` and later swept by reconcile (or any double-finalize) records exactly one Run instead of crashing on the primary key — the failure that surfaced as `session_start` reporting `(state unavailable)`.

**Provenance no longer records phantom outputs.** A file is recorded as touched only when its Write tool result exists and did not error, and only when the path exists on disk at finalization; denied writes and paths parsed from prose are excluded and logged. Active-dispatch markers for a finalized Run are cleared across both the session and `agentId` indexes, so a completed Run can never keep a write authorized.

### Changed

**Session digest.** The Stop digest now reports "Outputs recorded" (real on-disk artifacts, rel/abs duplicates collapsed) rather than "Deliverables produced".

---

## [0.1.0] — 2026-05-25

Initial public release. MetaEnsemble ships as a project-agnostic local runtime for coordinating ensembles of cognitive agents with stable identities, typed contracts, and an append-only Ledger of every Run.

### Added

**Typed substrate.** UUIDv7 identifiers and Role-prefixed aliases for every Executor. Append-only SQLite Ledger with a JSONL mirror for replay. YAML Manifests and JSON Briefs, both schema-validated. Two output styles (`wire`, `deliverable`) co-emitted per Run so machine-to-machine traffic stays terse and human-facing output stays full English.

**Lifecycle hooks.** SessionStart, PreToolUse, PostToolUse, Write, and Stop hooks fire automatically. A two-axis cost gate (run-size and window-headroom) escalates auto → notify → block. A five-axis quality gate (correctness, security, maintainability, complexity, coverage) uses the same three-state grammar. The Reconcile module recovers stranded Runs after crashes or budget kills.

**Principal-facing surface.** Seven slash commands: `/dispatch`, `/relaunch`, `/executors`, `/standup`, `/window`, `/perf`, `/ledger`. A `metaensemble` CLI covering `setup`, `user-setup`, `adopt`, `unadopt`, `user-teardown`, `doctor`, `survey`, `reconcile`, `eval`, `relaunch`, `projects`, `export-agents`, and three manifest helpers: `manifest validate`, `manifest new-id`, `manifest scaffold`.

**Multi-instance dispatch.** Solo, fan-out, consensus, shadow, and peer-review patterns. The `N ≥ 2` requirement for multi-instance work is enforced deterministically before any Executor runs.

**Curated Roles.** Seven base Roles ship in `metaensemble/roles/`: architect, backend, frontend, code-quality, test-engineer, devops, docs. Activated by the survey based on detected project signals; per-agent decisions live in `.metaensemble/survey-decisions.yaml` and are never silently overwritten.

**Adoption flow.** `metaensemble setup` is the interactive wizard: choose a known Claude Code project, run user-level setup when needed, and adopt the selected project. `metaensemble user-setup` owns machine-level integration and vendors the runtime atomically into `~/.metaensemble/runtime/` (a symlink to a versioned directory under `~/.metaensemble/runtime-versions/<id>/`); `metaensemble adopt` owns project state. Rollback mirrors that split through `metaensemble unadopt` and `metaensemble user-teardown`, each with `--purge-state` for full cleanup. `metaensemble doctor` runs twelve diagnostic checks (C1 and C6 marked `SKIP` as legacy), with `--fix` applying safe repairs.

**Install topology check.** `metaensemble doctor` C12 reads PEP 610 `direct_url.json` from the runner's pinned Python to classify the install. `OK` for wheel installs (the runner is independent of any source tree). `WARN` for editable installs (the runner resolves `import metaensemble` back to the source tree) with a wheel-install recovery command. `FAIL` when the pinned interpreter has no `metaensemble` distribution. `metaensemble user-setup` prints the same notice on editable installs.

**Packaging (L3).** Wheel install (`pip install metaensemble`) is the supported flow. The wheel ships every asset bucket under `metaensemble/` plus the `evals/` package. `metaensemble user-setup` vendors a self-contained snapshot of the runtime into `~/.metaensemble/runtime-versions/<id>/` and atomically swaps `~/.metaensemble/runtime` to it via `os.replace`; the runner at `runtime/bin/me-run` pins one absolute Python interpreter (shell-quoted) so paths with spaces work. The vendored runtime is independent of the source: after `metaensemble setup` completes, the source clone can be deleted. Re-running `user-setup` always re-vendors (so `pip install --upgrade` cannot leave stale assets in front of the runner), and the version-dir GC keeps the last two valid versions plus the currently-linked one. A MANIFEST inside each version dir is verified before the symlink swap, so an interrupted vendor never produces a half-applied state.

**Evaluation harness.** `evals/` ships three tiers — replay (PR-gate, no API spend), smoke (one seed, live, no project mutation), full (release-gated, requires `--allow-live`). Headline metrics — `pass@budget`, `quality_per_1k_tokens`, `orchestration_overhead_ratio`, `failed_run_token_waste` — are reported with Wilson 95% confidence intervals. The shipped Suite B Somali smoke set is labelled smoke-only; calibration is a v0.2.0 deliverable.

**Documentation.** README, ARCHITECTURE, USER-GUIDE, DEPLOYMENT, PERFORMANCE, SYSTEM-CARD, SECURITY, GLOSSARY. CI matrix runs across Python 3.10–3.13.

### Migration from a pre-0.1.0 development install

Operators running a pre-L3 editable install (with `./scripts/bootstrap.sh`, a launcher at `~/.metaensemble/bin/me-run`, and the doctor `.pth` workaround) should follow this one-shot migration:

```bash
# 1. Reconcile any stranded Runs so the Ledger captures them before purge.
metaensemble reconcile --older-than-minutes 0

# 2. Reverse the project-level adoption and tear down the legacy user-level integration.
metaensemble unadopt --purge-state                 # in each adopted project
metaensemble user-teardown --purge-state           # clears ~/.metaensemble/

# 3. Replace the development install with the wheel install.
pip uninstall -y metaensemble
pip install metaensemble                            # or: pip install -e . from a clone

# 4. Re-bootstrap: vendors the new runtime, wires commands/hooks/statusline.
metaensemble setup
```

The legacy `bootstrap.sh` script and the `~/.metaensemble/bin/me-run` launcher are removed in this release; the new runner lives at `~/.metaensemble/runtime/bin/me-run` and is generated as part of the atomic vendor step inside `metaensemble user-setup`. The recovery sweep that runs at the start of every `user-setup` removes legacy launcher residue, so step 4 cleans up any leftover artifacts from the old install path. Doctor checks `C1` (`.pth file processed`) and `C6` (venv entry-point script) return `SKIP` post-migration — both targeted editable-install failure modes that the wheel install eliminates by construction.

### Known limitations

See [SYSTEM-CARD.md](./docs/SYSTEM-CARD.md) for intended use, evaluated capabilities, known limitations, and the operating constraints carried into the v0.2.0 roadmap.
