"""Prompt-surface token budget.

Every file the Coordinator reads on every dispatch (the slash command
markdown, the role specs, the protocol SKILL.md) contributes to the
input tokens of *every* run. A 10% growth in any of these files is a
10% growth in every dispatch's tokens-in. This test pins a numeric
budget per file so growth becomes a deliberate CHANGELOG entry rather
than an unnoticed drift.

When a budget is intentionally raised, update the table below and
include the reason in the commit message.

The 4-chars-per-token estimate matches the rest of the codebase
(see `metaensemble/lib/recording.py:estimate_tokens`).
"""
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# (relative path, token budget). Each budget is the current file size
# divided by 4, rounded up to the next 100, plus a 10% headroom. A file
# that crosses its budget should either be split, trimmed, or have its
# budget bumped explicitly — never silently.
PROMPT_SURFACE_BUDGETS: dict[str, int] = {
    "metaensemble/commands/dispatch.md":      1500,
    "metaensemble/commands/relaunch.md":       800,
    "metaensemble/commands/standup.md":        500,
    "metaensemble/commands/limits.md":         500,
    "metaensemble/commands/perf.md":           500,
    "metaensemble/commands/ledger.md":         900,
    "metaensemble/commands/executors.md":      500,
    "metaensemble/skills/metaensemble-protocol/SKILL.md": 6000,
}


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def test_prompt_surface_files_exist():
    """Every budgeted file must actually exist; missing files are a regression."""
    for rel in PROMPT_SURFACE_BUDGETS:
        path = REPO_ROOT / rel
        assert path.exists(), f"budgeted prompt-surface file missing: {rel}"


def test_prompt_surface_files_stay_under_budget():
    """Every file's estimated tokens stay <= its declared budget."""
    overages: list[str] = []
    for rel, budget in PROMPT_SURFACE_BUDGETS.items():
        path = REPO_ROOT / rel
        if not path.exists():
            # Earlier test pinned existence; if we get here without it
            # existing, the existence test already failed.
            continue
        tokens = _estimate_tokens(path.read_text())
        if tokens > budget:
            overages.append(
                f"{rel}: {tokens} tokens > budget {budget} "
                f"(+{tokens - budget}, +{((tokens - budget) / budget) * 100:.1f}%)"
            )
    if overages:
        msg = (
            "Prompt-surface token budget violated. Either trim the file(s) "
            "or raise the budget in test_prompt_surface_budget.py with a "
            "CHANGELOG entry explaining why:\n  "
            + "\n  ".join(overages)
        )
        raise AssertionError(msg)


def test_prompt_surface_budget_table_has_no_orphans():
    """Every entry in the budget table points to a real file.

    Catches the inverse drift: a file was deleted or renamed and the
    budget table was not updated.
    """
    for rel in PROMPT_SURFACE_BUDGETS:
        path = REPO_ROOT / rel
        assert path.exists(), (
            f"orphan budget entry: {rel} is in the table but not on disk"
        )
