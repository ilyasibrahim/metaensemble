"""Materialize Suite-A fixture workspaces as deterministic git repos.

Each fixture source tree under ``evals/fixtures/`` is plain files with
no ``.git``. :func:`build_fixture` copies a tree into a destination
directory, normalizes file modes, and creates exactly one commit with a
fixed author/committer identity and date, so the resulting commit SHA
is identical on every machine. ``FIXTURE_SHAS`` records the expected
SHAs; ``evals/datasets/suite_a/tasks.yaml`` pins the same values as
``starting_sha`` for the ``oss-fixture-*`` tasks.

Recompute the SHAs after editing a fixture source tree with::

    python -m evals.fixtures.build --print-shas
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

_FIXTURES_ROOT = Path(__file__).resolve().parent

_SOURCE_DIRS: dict[str, Path] = {
    "oss-fixture-paginator": _FIXTURES_ROOT / "paginator",
    "oss-fixture-legacy": _FIXTURES_ROOT / "legacy",
}

# Expected deterministic single-commit SHAs, produced by running this
# builder. `metaensemble/tests/test_eval_fixtures.py` fails when a
# fixture source tree drifts from these values without re-pinning.
FIXTURE_SHAS: dict[str, str] = {
    "oss-fixture-paginator": "cbb6c2178af85ab778dd215379bf0928b6e52268",
    "oss-fixture-legacy": "c04afa1fb995fc47c53a7336dcb5873c4a4bdeb4",
}

# Fixed commit identity: with author, committer, and both dates pinned,
# the commit SHA depends only on the tree contents and the message.
_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "MetaEnsemble Fixtures",
    "GIT_AUTHOR_EMAIL": "fixtures@metaensemble.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00 +0000",
    "GIT_COMMITTER_NAME": "MetaEnsemble Fixtures",
    "GIT_COMMITTER_EMAIL": "fixtures@metaensemble.invalid",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00 +0000",
}

_IGNORED_NAMES = ("__pycache__", "*.pyc", ".pytest_cache", ".DS_Store", ".git")


def _git(args: list[str], cwd: Path) -> str:
    """Run git with the pinned identity and no user/system config."""
    env = dict(os.environ)
    env.update(_COMMIT_ENV)
    # Isolate from user- and machine-level git config (gpg signing,
    # autocrlf, templates) so the commit is byte-identical everywhere.
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd}: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def build_fixture(name: str, dest: Path) -> str:
    """Materialize fixture ``name`` into ``dest`` as a one-commit git repo.

    ``name`` is one of ``FIXTURE_SHAS``'s keys. ``dest`` is created if
    needed and must be empty. Returns the full 40-character commit SHA,
    which is deterministic across machines.
    """
    source = _SOURCE_DIRS.get(name)
    if source is None:
        known = ", ".join(sorted(_SOURCE_DIRS))
        raise ValueError(f"unknown fixture {name!r}; expected one of: {known}")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()):
        raise ValueError(f"fixture destination {dest} is not empty")
    shutil.copytree(
        source,
        dest,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(*_IGNORED_NAMES),
    )
    # Normalize modes so umask and checkout quirks cannot change the
    # tree hash: directories 755, files 644.
    for path in sorted(dest.rglob("*")):
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            path.chmod(0o644)
    _git(["init", "-q"], dest)
    _git(["add", "-A"], dest)
    _git(
        ["commit", "-q", "--no-gpg-sign", "-m", f"fixture: {name} frozen starting state"],
        dest,
    )
    sha = _git(["rev-parse", "HEAD"], dest)
    if len(sha) != 40:
        raise RuntimeError(f"unexpected rev-parse output: {sha!r}")
    return sha


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.fixtures.build",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--print-shas",
        action="store_true",
        help="build every fixture into a temp dir and print `name sha` lines",
    )
    args = parser.parse_args(argv)
    if not args.print_shas:
        parser.print_help()
        return 2
    for name in sorted(_SOURCE_DIRS):
        with tempfile.TemporaryDirectory(prefix="me-fixture-") as tmp:
            sha = build_fixture(name, Path(tmp) / "repo")
        expected = FIXTURE_SHAS.get(name)
        marker = "" if sha == expected else "  (differs from FIXTURE_SHAS)"
        print(f"{name} {sha}{marker}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
