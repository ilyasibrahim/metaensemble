#!/usr/bin/env python3
"""Stop hook — renders a session digest for the Principal.

Per ARCHITECTURE.md §8: at session end, surfaces Executors active, Runs
completed, recorded outputs, window percentage consumed. Reads from
the Ledger; performs no model calls.

Stdin: agent runtime's session-stop payload (informational; we ignore it).
Stdout: JSON with a `systemMessage` containing the digest.
Exit: 0 always.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from metaensemble.hooks._common import (  # noqa: E402
    current_window_id,
    db_path,
    emit,
    jsonl_path,
    log_error,
    migration_sql,
    read_input,
    state_dir,
)
from metaensemble.lib.config import effective_capacity_tokens, load_budget_config  # noqa: E402
from metaensemble.lib.ledger import Ledger  # noqa: E402
from metaensemble.lib.native_state import (  # noqa: E402
    SevenDayLine,
    WindowReport,
    format_age,
    format_duration,
    load_native_rate_limits,
    resolve_seven_day,
    resolve_window_report,
)
from metaensemble.lib.reconcile import reconcile_session_pending  # noqa: E402
from metaensemble.lib.runtime_state import (  # noqa: E402
    get_session_burn,
    get_window_burn as get_runtime_burn,
)


SESSION_LOOKBACK_HOURS = 6  # generous; covers typical session duration


def _deliverables_index_recent(within: timedelta) -> list[str]:
    index = state_dir() / "deliverables_index.jsonl"
    if not index.exists():
        return []
    cutoff = datetime.now(timezone.utc) - within
    paths: list[str] = []
    with index.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts = datetime.fromisoformat(rec["ts"])
                if ts >= cutoff:
                    paths.append(rec["path"])
            except Exception:  # nosec B112
                # Malformed transcript-index rows are diagnostic residue, not
                # a reason to block Stop-hook session summarization.
                continue
    # De-duplicate while preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    return deduped


def _safe_json_loads(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:  # nosec B112
        return None


def _short_path(path: str, *, max_len: int = 96) -> str:
    if len(path) <= max_len:
        return path
    return "..." + path[-(max_len - 3):]


def _dedupe_files(files: list) -> list[str]:
    """Collapse rel/abs duplicates of the same file (e.g. ``x.md`` and
    ``/proj/x.md``) so one write is not counted twice. Keeps the shorter
    (relative) representative."""
    paths: list[str] = []
    for f in files:
        s = str(f).strip()
        if s and s not in paths:
            paths.append(s)
    # Drop a path when a shorter path is the same file (the absolute twin).
    return [f for f in paths if not any(o != f and f.endswith("/" + o) for o in paths)]


def _file_exists(path: str, project_root: "Path | None") -> bool:
    """True when `path` exists. Absolute paths are checked directly; relative
    paths are resolved against project_root. When project_root is unknown we do
    NOT hide a relative path (avoid false-negatives), since the finalization
    guard already drops phantom relatives from the record."""
    p = Path(path)
    if p.is_absolute():
        return p.exists()
    if project_root is not None:
        return (project_root / path).exists()
    return True


def _run_output_entry(run, executor_label=None, project_root=None) -> str | None:
    """Summarize a Ledger Run's file outputs for the Stop digest.

    A recorded output is a file artifact that exists on disk: an explicit
    deliverable path or files the Run actually wrote. Free-text results
    (deliverable_ref kind "summary") and paths that no longer exist are not
    counted. Only successful (ok/partial) runs are considered.
    """
    if getattr(run, "outcome", None) not in ("ok", "partial"):
        return None
    run_short = run.run_id[:13]
    owner = (
        f"{executor_label} (run {run_short})"
        if executor_label else f"run {run_short}"
    )
    ref = _safe_json_loads(getattr(run, "deliverable_ref_json", None))
    files = _safe_json_loads(getattr(run, "files_touched_json", None))
    files = _dedupe_files(files) if isinstance(files, list) else []
    files = [f for f in files if _file_exists(f, project_root)]

    # An explicit deliverable file path (only if it exists).
    if isinstance(ref, dict) and ref.get("kind") == "path":
        value = str(ref.get("value") or "").strip()
        if value and _file_exists(value, project_root):
            return f"{owner}: {_short_path(value)}"

    # Files the run actually wrote and that still exist.
    if files:
        preview = ", ".join(_short_path(str(p), max_len=44) for p in files[:3])
        more = f", +{len(files) - 3} more" if len(files) > 3 else ""
        return f"{owner}: {len(files)} file(s) touched ({preview}{more})"

    return None


def _executor_label(ledger: Ledger, executor_id: str, cache: dict[str, str | None]) -> str | None:
    if executor_id in cache:
        return cache[executor_id]
    executor = ledger.get_executor(executor_id)
    label = f"{executor.alias}/{executor.role_id}" if executor else None
    cache[executor_id] = label
    return label


def _run_outputs_recent(ledger: Ledger, recent_runs, project_root=None) -> list[str]:
    outputs: list[str] = []
    executor_cache: dict[str, str | None] = {}
    for run in recent_runs:
        entry = _run_output_entry(
            run,
            executor_label=_executor_label(ledger, run.executor_id, executor_cache),
            project_root=project_root,
        )
        if entry:
            outputs.append(entry)
    return outputs


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def _format_five_hour_line(window_id: str, report: WindowReport, five_h_window) -> str:
    """5h line for the session-summary digest, branching on `report.kind`.

    `five_h_window` is `native.five_hour` (used only for the
    time-until-reset suffix on the `live_plan` arm).
    """
    if report.kind == "live_plan":
        remaining = ""
        if five_h_window is not None:
            r = format_duration(five_h_window.time_until_reset())
            if r:
                remaining = f", {r} left"
        return (
            f"- 5h window `{window_id}`: {report.pct_used:.1f}% of plan used "
            f"(source: {report.source}{remaining})"
        )
    if report.kind == "last_observed_plan":
        age_str = format_age(report.snapshot_age_seconds)
        return (
            f"- 5h window `{window_id}`: live usage unavailable; last runtime "
            f"snapshot was {report.last_observed_pct:.1f}%, {age_str} old, "
            "from a previous window"
        )
    if report.kind == "project_fallback":
        return (
            f"- 5h window `{window_id}` (project burn): "
            f"{report.pct_used:.1f}% of {report.capacity_tokens:,} fallback capacity "
            "— plan-wide usage unavailable"
        )
    # kind == "unavailable"
    return f"- 5h window `{window_id}`: plan usage unavailable until statusline refreshes"


def _format_seven_day_line(seven_line: SevenDayLine, seven_d_window) -> str | None:
    """7-day line, or None to omit when 7d telemetry is missing."""
    if seven_line.kind == "unavailable":
        return None
    if seven_line.kind == "live_plan":
        remaining = ""
        if seven_d_window is not None:
            r = format_duration(seven_d_window.time_until_reset())
            if r:
                remaining = f" ({r} left)"
        return f"- 7-day window: {seven_line.pct_used:.1f}% of plan used{remaining}"
    # kind == "last_observed_plan"
    age_str = format_age(seven_line.snapshot_age_seconds)
    return (
        f"- 7-day window: live usage unavailable; last runtime "
        f"snapshot was {seven_line.last_observed_pct:.1f}%, {age_str} old"
    )


def _format_digest(
    *,
    window_id: str,
    report: WindowReport,
    seven_line: SevenDayLine,
    native,                       # NativeRateLimits | None
    runs: int,
    distinct_executors: int,
    outputs: list[str],
    session_main_tokens: int,     # window-scoped main agent tokens
    session_me_tokens: int,       # window-scoped MetaEnsemble Ledger tokens
    session_main_lifetime: int = 0,  # session-lifetime main agent tokens
) -> str:
    # The plan percentage and the raw project-scope token figures come
    # from different scopes and must not be displayed as if one explains
    # the other. The percentage (when present) is plan-wide; the token
    # figures below are project-scoped. Separate lines, explicit labels.
    five_h = native.five_hour if native is not None else None
    seven_d = native.seven_day if native is not None else None

    lines = [
        "## MetaEnsemble session summary",
        _format_five_hour_line(window_id, report, five_h),
    ]
    seven_str = _format_seven_day_line(seven_line, seven_d)
    if seven_str:
        lines.append(seven_str)
    session_total_window = session_main_tokens + session_me_tokens
    # Project-scope tokens in the current 5-hour window. Only worth
    # showing when distinct from the window-scoped session total —
    # otherwise the next line carries the same information.
    if report.used_tokens and report.used_tokens != session_total_window:
        lines.append(
            f"- All sessions, this project, 5h window: {report.used_tokens:,} tokens "
            "(runtime input+output, cache excluded)"
        )
    lines.append(
        f"- This session in 5h window: {session_total_window:,} tokens "
        f"(main agent: {session_main_tokens:,}, MetaEnsemble: {session_me_tokens:,})"
    )
    # Lifetime line only when the session crossed a window boundary —
    # i.e. the session has more main-agent tokens overall than fit in
    # the current bucket.
    if session_main_lifetime and session_main_lifetime > session_main_tokens:
        lines.append(
            f"- This session lifetime: {session_main_lifetime:,} main-agent tokens "
            "(session started before current 5h window)"
        )
    lines.append(f"- Runs completed this session: {runs}")
    lines.append(f"- Executors active this session: {distinct_executors}")
    if report.note:
        lines.append(f"- Note: {report.note}")
    if outputs:
        lines.append(f"- Outputs recorded ({len(outputs)}):")
        for d in outputs[:10]:
            lines.append(f"  - {d}")
        if len(outputs) > 10:
            lines.append(f"  - ...and {len(outputs) - 10} more")
    else:
        lines.append("- Outputs recorded: none")
    return "\n".join(lines)


def run() -> int:
    payload = read_input()
    session_id = (payload or {}).get("session_id") or ""
    _cwd = (payload or {}).get("cwd")
    project_root = Path(_cwd) if _cwd else None

    try:
        ledger = Ledger(db_path=db_path(), jsonl_path=jsonl_path())
        ledger.initialize(migration_sql())

        # Layer 1 reconcile: capture any pending sidecars belonging to the
        # ending session as failed Runs before we render the digest, so
        # the count below includes the interrupted Tasks.
        if session_id:
            reconcile_session_pending(ledger, state_dir(), session_id)

        window_id = current_window_id()
        # Prefer the runtime's view of the window; the Ledger only counts
        # dispatched Runs, while the runtime counts everything the session
        # touched (main agent's reads, edits, conversation turns).
        ledger_burn = ledger.get_window_burn(window_id)
        ledger_used = ledger_burn.total_tokens_in + ledger_burn.total_tokens_out
        runtime_burn = get_runtime_burn(window_id=window_id)
        runtime_used = runtime_burn.input_tokens + runtime_burn.output_tokens
        used = max(runtime_used, ledger_used)
        config = load_budget_config()
        capacity = effective_capacity_tokens(config)
        native = load_native_rate_limits()
        report = resolve_window_report(used, capacity, native)
        seven_line = resolve_seven_day(native)

        since = datetime.now(timezone.utc) - timedelta(hours=SESSION_LOOKBACK_HOURS)
        recent = ledger.get_recent_runs(limit=500, since=since)
        distinct_executors = len({r.executor_id for r in recent})
        outputs = _dedupe_preserve_order(
            _deliverables_index_recent(timedelta(hours=SESSION_LOOKBACK_HOURS))
            + _run_outputs_recent(ledger, recent, project_root)
        )

        # Window-scoped session burn so the per-session line is
        # apples-to-apples with the 5h plan window above.
        session_burn_window = (
            get_session_burn(session_id, window_id=window_id) if session_id else None
        )
        session_main_window = (
            session_burn_window.input_tokens + session_burn_window.output_tokens
            if session_burn_window else 0
        )
        # Lifetime burn — only shown when the session crossed a window
        # boundary (lifetime > window).
        session_burn_lifetime = (
            get_session_burn(session_id) if session_id else None
        )
        session_main_lifetime = (
            session_burn_lifetime.input_tokens + session_burn_lifetime.output_tokens
            if session_burn_lifetime else 0
        )
        # MetaEnsemble share is scoped to the current 5h window via the
        # Ledger query above (ledger_used). The Runs table has no
        # session_id column, so this is an approximation: the assumption
        # is that the Principal's dispatches in this window came from this
        # session. For the typical solo-Principal workflow this holds.
        session_me_window = ledger_used

        digest = _format_digest(
            window_id=window_id,
            report=report,
            seven_line=seven_line,
            native=native,
            runs=len(recent),
            distinct_executors=distinct_executors,
            outputs=outputs,
            session_main_tokens=session_main_window,
            session_me_tokens=session_me_window,
            session_main_lifetime=session_main_lifetime,
        )
        emit({"continue": True, "systemMessage": digest})
        ledger.close()
        return 0
    except Exception as exc:
        log_error("session-summary-failed", str(exc))
        emit({"continue": True, "systemMessage": "## MetaEnsemble session summary\n(state unavailable)\n"})
        return 0


if __name__ == "__main__":
    sys.exit(run())
