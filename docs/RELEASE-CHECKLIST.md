# MetaEnsemble Release Checklist

This checklist is the gate for publishing a release to PyPI (current: `0.2.0`). It exists to
close the gap between "the source tree passes tests" and "the artifact
strangers install is the artifact we meant to ship."

## Required Before Publish

1. Build and test the wheel in a clean environment:

   ```bash
   python -m build
   python -m venv /tmp/metaensemble-wheel-smoke
   /tmp/metaensemble-wheel-smoke/bin/python -m pip install dist/metaensemble-0.2.0-py3-none-any.whl
   /tmp/metaensemble-wheel-smoke/bin/metaensemble --help
   /tmp/metaensemble-wheel-smoke/bin/metaensemble eval --tier replay --cells all
   ```

2. Run the installed-CLI smoke from the development checkout before
   trusting any live local verification:

   ```bash
   ./scripts/verify-installed-cli.sh
   ```

   This rebuilds the wheel, force-reinstalls it into the active Python,
   then runs the installer/doctor smokes from outside the repo with a
   temporary `HOME`. It specifically guards against source-backed tests
   passing while the local console script still imports stale
   `site-packages` code.

3. Run the full test matrix on the lowest supported Python and current
   Python. `pyproject.toml` declares `requires-python = ">=3.10"`, so
   Python 3.10 is the compatibility floor:

   ```bash
   python3.10 -m venv /tmp/metaensemble-py310
   /tmp/metaensemble-py310/bin/python -m pip install -e ".[test]"
   /tmp/metaensemble-py310/bin/python -m pytest
   ```

4. Run static and advisory checks:

   ```bash
   python -m ruff check metaensemble evals scripts
   python -m bandit -q -r metaensemble -x metaensemble/tests
   python -m pip_audit
   ```

5. Exercise installer lifecycle end to end in environments that resemble
   first-time users:

   ```bash
   metaensemble user-setup --layout namespaced
   metaensemble adopt /path/to/throwaway/project
   metaensemble doctor
   metaensemble unadopt /path/to/throwaway/project --purge-state
   metaensemble user-teardown --purge-state
   ```

   Run this once on a throwaway macOS user account and once in a fresh
   Linux container or VM. Do not rely only on unit tests for this gate,
   because the installer writes user-level Claude Code configuration.

6. Run a small live evaluation before making any quality-per-token claim:

   ```bash
   metaensemble eval --tier full --allow-live --cells all --seeds 1 --budget-usd 0.30
   ```

   The system card must link the generated report and state the exact
   cells, task count, model IDs when available, observed tokens, skipped
   fixtures, and whether any baseline-superiority claim is justified.
   If the run is smaller than 20 live tasks, keep the README and system
   card in workflow/accountability language only.

7. Publish release integrity data:

   ```bash
   shasum -a 256 dist/metaensemble-0.2.0*
   ```

   Publish SHA256 digests next to the release artifacts. Prefer Sigstore
   signing when the publishing environment has trusted identity available.

8. Verify PyPI from a new virtual environment after upload:

   ```bash
   python -m venv /tmp/metaensemble-pypi-smoke
   /tmp/metaensemble-pypi-smoke/bin/python -m pip install metaensemble==0.2.0
   /tmp/metaensemble-pypi-smoke/bin/metaensemble --version
   /tmp/metaensemble-pypi-smoke/bin/metaensemble eval --tier replay --cells all
   ```

## CI Gates

The GitHub Actions matrix runs Python 3.10, 3.11, 3.12, and 3.13. It
also builds the wheel, installs it in a clean venv, verifies the console
entry point, runs replay from the installed artifact, runs `pip-audit`,
and prints the wheel SHA256 digest.

## Claim Discipline

For `0.2.0`, public copy may claim:

- persistent Executor identity
- typed Manifests and strict Briefs
- append-only Ledger records
- local cost gating
- five-axis deliverable checks: built-in Python runners for Manifest-declared
  `.py` outputs, and project-configured `axis_commands` for other deliverables
- project memory surfaces recorded at adoption and handed to Manifests as
  typed `role: memory` pointers
- blocked dispatches delivered through the runtime's native permission surface
- replay/smoke/full evaluation harness mechanics

Public copy must not claim calibrated quality improvement, routing
accuracy, consensus reliability, or cost-gate calibration until the
system card links a live report that measures those quantities.
