"""Cross-session relaunch context preparation.

Implements ARCHITECTURE.md §14. Given an Executor alias, builds the context
the Coordinator needs to spawn a fresh Run under the same Executor identity:
the prior Brief, a summary of the prior Deliverable, the lineage of related
Executors, and (in `--full` mode) the entire prior Deliverable and every
prior Brief.

Cheap relaunch is the default. `--full` is opt-in and pays a cost proportional
to the Executor's Run history length.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from metaensemble.lib.dispatch import _extract_summary  # noqa: WPS450 (intentional reuse)
from metaensemble.lib.ledger import Executor, Ledger, Run


@dataclass(frozen=True)
class RelaunchContext:
    """The reconstructed context for spawning a new Run under an existing Executor."""

    executor: Executor
    role_id: str
    last_run: Run | None
    last_brief_in: dict | None
    last_brief_out: dict | None
    last_deliverable_summary: str | None
    last_deliverable_full: str | None
    related_executors: list[Executor] = field(default_factory=list)
    history_length: int = 0
    full: bool = False


def _load_json_file(path: str | None) -> dict | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _load_text_file(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        return p.read_text()
    except OSError:
        return None


def _find_related_executors(ledger: Ledger, executor: Executor, limit: int = 50) -> list[Executor]:
    """Executors that share a parent_executor_id with this one, or are this Executor's parent.

    The lineage in MetaEnsemble's data model is the `parent_executor_id` field
    on `executors`. Related Executors are the parent (if any) and the siblings
    that share that parent.
    """
    return ledger.get_related_executors(executor, limit=limit)


def prepare_relaunch(
    ledger: Ledger, alias: str, full: bool = False
) -> RelaunchContext | None:
    """Build the context needed to relaunch an Executor by alias.

    Args:
        ledger: open Ledger.
        alias: short Executor alias (e.g. `arch-7b3`).
        full: when True, load the entire prior Deliverable and every prior Brief.
              When False (default), load only the most recent Brief and a
              summary section of the most recent Deliverable.

    Returns:
        A RelaunchContext for the Coordinator to compose the resumption Brief,
        or None if no Executor matches the alias.
    """
    executor = ledger.get_executor_by_alias(alias)
    if executor is None:
        return None

    history_count = ledger.count_runs_by_executor(executor.executor_id)
    history_limit = 50 if full else 1
    runs = ledger.get_runs_by_executor(executor.executor_id, limit=history_limit)
    related = _find_related_executors(ledger, executor, limit=10)

    if not runs:
        return RelaunchContext(
            executor=executor,
            role_id=executor.role_id,
            last_run=None,
            last_brief_in=None,
            last_brief_out=None,
            last_deliverable_summary=None,
            last_deliverable_full=None,
            related_executors=related,
            history_length=history_count,
            full=full,
        )

    last_run = runs[0]
    last_brief_in = _load_json_file(last_run.brief_in_path)
    last_brief_out = _load_json_file(last_run.brief_out_path)
    last_deliverable_text = _load_text_file(last_run.deliverable_path)
    last_deliverable_summary = (
        _extract_summary(last_deliverable_text) if last_deliverable_text else None
    )
    last_deliverable_full = last_deliverable_text if full else None

    return RelaunchContext(
        executor=executor,
        role_id=executor.role_id,
        last_run=last_run,
        last_brief_in=last_brief_in,
        last_brief_out=last_brief_out,
        last_deliverable_summary=last_deliverable_summary,
        last_deliverable_full=last_deliverable_full,
        related_executors=related,
        history_length=history_count,
        full=full,
    )


def render_relaunch_context(ctx: RelaunchContext) -> str:
    """Render a RelaunchContext as Markdown for the Coordinator or Principal to read."""
    lines = [
        f"## Relaunch context — `{ctx.executor.alias}`",
        "",
        f"- Role: `{ctx.role_id}`",
        f"- Executor ID: `{ctx.executor.executor_id}`",
        f"- Status: {ctx.executor.status}",
        f"- Created: {ctx.executor.created_ts[:19]}",
        f"- Last seen: {ctx.executor.last_seen_ts[:19]}",
        f"- History: {ctx.history_length} prior Run(s){' (full)' if ctx.full else ''}",
    ]
    if ctx.related_executors:
        lines.append("- Related Executors: " + ", ".join(
            f"`{e.alias}`" for e in ctx.related_executors[:5]
        ))

    if ctx.last_run is None:
        lines.append("")
        lines.append("No prior Runs recorded for this Executor.")
        return "\n".join(lines)

    lines.append("")
    lines.append(f"### Last Run `{ctx.last_run.run_id[:8]}...`")
    lines.append(f"- Outcome: {ctx.last_run.outcome}")
    lines.append(f"- Tokens: {ctx.last_run.tokens_in:,} in / {ctx.last_run.tokens_out:,} out")
    lines.append(f"- Window: {ctx.last_run.window_id}")

    if ctx.last_deliverable_summary:
        lines.append("")
        lines.append("### Prior Deliverable (summary)")
        lines.append("")
        lines.append(ctx.last_deliverable_summary)

    if ctx.full and ctx.last_deliverable_full:
        lines.append("")
        lines.append("### Prior Deliverable (full)")
        lines.append("")
        lines.append(ctx.last_deliverable_full)

    return "\n".join(lines)
