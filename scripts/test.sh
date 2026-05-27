#!/bin/sh
# MetaEnsemble test entry point — the canonical one-shot reproducer.
#
# Mirrors the `[test]` profile that CI installs (`.github/workflows/ci.yml`).
# A fresh contributor runs this from a clean checkout and sees the same
# green CI sees, without having to remember which extras are required.
#
# Usage:
#   ./scripts/test.sh              # full test suite
#   ./scripts/test.sh -k name      # pytest -k passthrough
#   ./scripts/test.sh metaensemble/tests/test_quality_gate.py  # subset
#
# Exit code mirrors pytest's: 0 on green, non-zero on failure.

set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-python3}"

echo "==> installing .[test] in editable mode"
"$PYTHON" -m pip install -e ".[test]" --quiet

echo "==> running pytest"
"$PYTHON" -m pytest metaensemble/tests/ "$@"
