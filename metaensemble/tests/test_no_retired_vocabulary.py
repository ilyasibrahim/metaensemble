"""Hard CI gate ensuring retired vocabulary tokens do not leak into the
launch-facing surface.

The retired tokens are CLI flags, subcommand invocations, slash commands,
and Python identifiers that the v0.1.0 vocabulary rename removed. They must
not appear in production code, CLI help text, generated artifacts, public
docs, or system-card prose.

Bare technical words like `window`, `survey`, `mode`, `parallel`,
`incorporate`, `quota` remain allowed — they appear legitimately as
`window_budget`, `survey methodology`, `mode of operation`, "in parallel",
etc. Only the specific retired tokens are prohibited.

See addendum Addition 3.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]

# Exact substring matches. These are unambiguous: any literal occurrence is
# a regression.
PROHIBITED_TOKENS: tuple[str, ...] = (
    # Retired CLI subcommand invocations (exact phrase)
    "metaensemble survey",
    "metaensemble window",

    # Retired generated artifact names
    "survey-decisions.yaml",

    # Retired Python enum references
    "Mode.PARALLEL",
    "Mode.INCORPORATE",
)

# Word-boundary regex matches. Use these for short tokens that could appear
# as a substring inside legitimate longer tokens — e.g. `--mode` would
# otherwise match `--model`, and `cmd_window` would match `_cmd_window`.
PROHIBITED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![A-Za-z0-9_-])--mode(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![A-Za-z0-9_])cmd_window(?![A-Za-z0-9_])"),
    re.compile(r"(?<![A-Za-z0-9_])cmd_survey(?![A-Za-z0-9_])"),
    # Retired slash commands. The negative-lookahead allows them to be
    # followed by any non-identifier character (punctuation, backticks,
    # whitespace, end-of-line) so `/window,`, `` `/window` ``, `/window:`,
    # and `/window.` are all caught — but `/window_id` and `/window-foo`
    # (legitimate identifiers) are not.
    re.compile(r"(?<![A-Za-z0-9_/])/window(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![A-Za-z0-9_/])/quota(?![A-Za-z0-9_-])"),
    # Legacy `survey-<YYYYMMDDThhmmss>.md` report names; canonical is
    # `inspection-<ts>.md` now.
    re.compile(r"(?m)^survey-\d{8}T"),
)

# Files that deliberately exercise the retired vocabulary (negative tests,
# this very file, and the migration code that knows about the legacy values).
ALLOWED_PREFIXES: tuple[str, ...] = (
    "metaensemble/tests/test_no_retired_vocabulary.py",
    "metaensemble/tests/test_vocabulary_migration.py",
    "metaensemble/tests/test_upgrade_path.py",
    # The CLI test that asserts retired commands fail
    "metaensemble/tests/test_cli.py",
    # Internal planning docs reference both vocabs (RFC, addendum, etc.)
    "_internal/",
    # The CHANGELOG documents the rename for the public record.
    "CHANGELOG.md",
)

# Directories we never scan (binary, generated, hidden).
SKIP_DIR_NAMES = {
    "__pycache__", ".git", ".venv", "node_modules", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", "dist", "build", "_site",
    ".metaensemble", "evals", "cassettes",
}

# Only scan these extensions / specific filenames.
SCAN_EXTENSIONS = {".py", ".md", ".yaml", ".yml", ".json", ".txt", ".cfg", ".toml"}
SCAN_FILENAMES = {"Dockerfile", "Makefile", "CHANGELOG.md", "README.md", "LICENSE"}


def _is_allowed(path: Path) -> bool:
    """Whether to skip enforcement for this path entirely."""
    rel = path.relative_to(REPO_ROOT)
    rel_str = str(rel)
    for prefix in ALLOWED_PREFIXES:
        if rel_str.startswith(prefix):
            return True
    return False


def _candidate_files() -> list[Path]:
    """Walk the repo for files in scope, honoring SKIP_DIR_NAMES."""
    out: list[Path] = []
    stack: list[Path] = [REPO_ROOT]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name in SKIP_DIR_NAMES:
                    continue
                if entry.name.startswith("."):
                    continue
                stack.append(entry)
                continue
            if not entry.is_file():
                continue
            if entry.suffix in SCAN_EXTENSIONS or entry.name in SCAN_FILENAMES:
                out.append(entry)
    return out


_INLINE_EXEMPT_MARKER = "vocab-migration: legacy-name"


def _violations(path: Path) -> list[tuple[int, str, str]]:
    """Return (line_no, token_or_pattern, line_text) tuples for hits.

    A line containing the marker `vocab-migration: legacy-name` is exempt;
    this is the inline opt-out for migration code that legitimately needs
    to reference the retired filename (e.g. to rename it on first run).
    """
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    hits: list[tuple[int, str, str]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if _INLINE_EXEMPT_MARKER in line:
            continue
        for token in PROHIBITED_TOKENS:
            if token in line:
                hits.append((line_no, token, line))
        for pattern in PROHIBITED_PATTERNS:
            if pattern.search(line):
                hits.append((line_no, pattern.pattern, line))
    return hits


def test_no_retired_vocabulary_in_production_code():
    """Production source, public docs, and CLI artifacts must not contain
    any retired CLI surface tokens.

    The list of allowed exceptions (`ALLOWED_PREFIXES`) covers tests and
    archived planning notes that deliberately reference both old and new
    vocabularies. Anything else is a leak.
    """
    violations: list[str] = []
    for path in _candidate_files():
        if _is_allowed(path):
            continue
        for line_no, token, line in _violations(path):
            violations.append(
                f"  {path.relative_to(REPO_ROOT)}:{line_no} [{token!r}] {line.strip()[:120]}"
            )
    if violations:
        pytest.fail(
            "Retired vocabulary tokens found in production code:\n"
            + "\n".join(violations[:30])
            + ("\n  ... (truncated)" if len(violations) > 30 else "")
        )


def test_prohibited_lists_have_no_overlaps():
    """Defensive: no token should be both an exact match and a regex pattern."""
    token_strs = set(PROHIBITED_TOKENS)
    pattern_strs = {p.pattern for p in PROHIBITED_PATTERNS}
    overlap = token_strs & pattern_strs
    assert not overlap, f"tokens and patterns must not overlap: {overlap}"


def test_slash_command_pattern_catches_all_punctuation_variants():
    """Regression for the missed-slash-command class. The grep must catch
    `/window` followed by punctuation (comma, period, colon), backticks,
    whitespace, end-of-line — and must NOT match legitimate identifiers
    like `/window_id`, `/window-foo`, or filesystem paths containing
    `metaensemble/window/`.
    """
    window_pat = next(
        p for p in PROHIBITED_PATTERNS if "/window" in p.pattern
    )
    # All of these are violations
    for line in [
        "the /window tool",                     # space after
        "uses /window, /standup, and doctor",    # comma after
        "see /window.",                          # period (end of sentence)
        "calling /window:",                      # colon
        "`/window`",                              # backtick-wrapped
        "/window\twas retired",                   # tab after
        "/window",                                # end of line
        "(the /window command)",                 # paren
    ]:
        assert window_pat.search(line), f"expected violation in: {line!r}"
    # Legitimate uses must NOT match
    for line in [
        "window_id is the bucket",
        "the /window_id partition",              # identifier suffix
        "passing /window-foo not allowed",       # hyphenated identifier
        "src/window/handler.py",                 # filesystem path component
        "no_window_id_here",
    ]:
        assert not window_pat.search(line), f"unexpected match in: {line!r}"


def test_allowed_prefixes_resolve_to_real_paths():
    """ALLOWED_PREFIXES must point at real files or directories so we don't
    accidentally allow nothing or allow everything.
    """
    for prefix in ALLOWED_PREFIXES:
        candidate = REPO_ROOT / prefix
        if prefix.endswith("/"):
            assert candidate.is_dir() or candidate.parent.is_dir(), (
                f"allowed prefix does not resolve to a directory: {prefix}"
            )
        else:
            assert (candidate.exists() or candidate.parent.is_dir()), (
                f"allowed prefix does not resolve to anything: {prefix}"
            )
