"""Runtime state introspection — read Claude Code's actual window burn.

The agent runtime writes per-message usage records to JSONL files under
`~/.claude/projects/<encoded-cwd>/**/*.jsonl`. Each assistant message
carries a `usage` block with `input_tokens`, `output_tokens`,
`cache_creation_input_tokens`, and `cache_read_input_tokens`. By
aggregating these across every session in the project for the current
5-hour bucket, MetaEnsemble can report the real window state — not just
the portion it logged itself through the dispatch path.

This module is the answer to the launch-time complaint that
`metaensemble standup` reports a much smaller window-burn number than
the runtime's actual usage. MetaEnsemble only counts dispatched Runs;
the bulk of token usage in a session typically comes from the main
agent's own reads, edits, and conversation turns, none of which produce
Run rows. The runtime is the source of truth for total burn; this module
reads it.

Design choices:
- Pure read; no writes, no side effects.
- Streamed reads, never load whole files into memory.
- Caches results per (project_dir, window_id) in-process with a short
  TTL so the hook layer is not re-scanning megabytes of JSONL on every
  invocation.
- Returns zero counters on any I/O error so callers degrade gracefully
  to the Ledger-only fallback rather than blocking work.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


# --- Project-directory resolution -----------------------------------------

def _encode_cwd_for_runtime(cwd: Path) -> str:
    """Match the runtime's project-directory naming.

    Claude Code stores each project's sessions under
    `~/.claude/projects/<encoded>/` where `<encoded>` is the absolute
    cwd with non-alphanumeric / non-dot characters replaced by `-`.
    Spaces, slashes, parentheses, and shell-sensitive characters all
    collapse to `-`. We mirror that encoding without spawning any
    process so this stays fast on the hook path.

    Examples:
        /Users/x/My Project    →  -Users-x-My-Project
        /home/user/proj        →  -home-user-proj
    """
    import re
    resolved = str(cwd.resolve())
    return re.sub(r"[^A-Za-z0-9.]", "-", resolved)


def claude_project_dir_for_cwd(
    cwd: Path | None = None,
    home: Path | None = None,
) -> Path | None:
    """Return the Claude Code project directory matching this cwd, if any.

    Returns None when the directory does not exist — typically because
    no session has yet been opened in this project, or because the
    runtime is configured to store sessions elsewhere. Callers treat
    `None` as "no runtime data available" and fall back gracefully.
    """
    home = home or Path.home()
    cwd = (cwd or Path.cwd()).resolve()
    candidate = home / ".claude" / "projects" / _encode_cwd_for_runtime(cwd)
    return candidate if candidate.is_dir() else None


# --- Window bucket math ---------------------------------------------------

def window_id_for(ts: datetime | None = None) -> str:
    """Return the 5-hour bucket string for a moment, matching `_common.py`.

    Buckets align to 5-hour blocks starting at 00:00 UTC each day,
    formatted as `YYYY-MM-DDTHH` where HH is the start of the bucket.
    """
    now = (ts or datetime.now(timezone.utc)).astimezone(timezone.utc)
    bucket_start = (now.hour // 5) * 5
    return f"{now.year:04d}-{now.month:02d}-{now.day:02d}T{bucket_start:02d}"


def _bucket_from_iso(ts_str: str) -> str | None:
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return window_id_for(dt)


# --- Aggregation data class ----------------------------------------------


@dataclass(frozen=True)
class RuntimeWindowBurn:
    """Aggregate token counts the runtime recorded for one 5-hour window.

    Fields mirror the Anthropic API's `usage` shape. `input_tokens` and
    `output_tokens` are the metered values most cleanly comparable to a
    plan's window cap; cache tokens are surfaced separately so the
    Principal can see whether their session is hitting cache efficiently
    (which the plan typically counts at a discounted rate).
    """

    window_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    message_count: int = 0
    project_dir: Path | None = None

    @property
    def metered_total(self) -> int:
        """Input + output, the most cap-relevant total for most plans."""
        return self.input_tokens + self.output_tokens

    @property
    def all_in_tokens(self) -> int:
        """Every token the runtime touched, including cache creation/read."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_tokens
            + self.cache_read_tokens
        )


# --- Cached scanner -------------------------------------------------------

# In-process cache. The key is (project_dir_str, window_id). Re-scans on
# every call would be wasteful when this runs from the hook path; the
# cache stays valid for the TTL below, which is long enough to amortize
# scans across the rapid hook invocations of a single dispatch and short
# enough that mid-session updates show up in /standup within seconds.
_CACHE_TTL_SECONDS = 15.0
_cache: dict[tuple[str, str], tuple[float, RuntimeWindowBurn]] = {}


def _scan_jsonls(project_dir: Path, target_bucket: str) -> RuntimeWindowBurn:
    """Walk every JSONL under the project dir and sum usage for one bucket."""
    in_t = out_t = cc = cr = 0
    n_msgs = 0
    try:
        for jsonl in project_dir.rglob("*.jsonl"):
            try:
                with jsonl.open() as fp:
                    for line in fp:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts = event.get("timestamp")
                        if not ts or _bucket_from_iso(ts) != target_bucket:
                            continue
                        msg = event.get("message")
                        if not isinstance(msg, dict):
                            continue
                        usage = msg.get("usage")
                        if not isinstance(usage, dict):
                            continue
                        in_t += int(usage.get("input_tokens") or 0)
                        out_t += int(usage.get("output_tokens") or 0)
                        cc += int(usage.get("cache_creation_input_tokens") or 0)
                        cr += int(usage.get("cache_read_input_tokens") or 0)
                        n_msgs += 1
            except OSError:
                continue
    except OSError:
        pass
    return RuntimeWindowBurn(
        window_id=target_bucket,
        input_tokens=in_t,
        output_tokens=out_t,
        cache_creation_tokens=cc,
        cache_read_tokens=cr,
        message_count=n_msgs,
        project_dir=project_dir,
    )


def get_window_burn(
    window_id: str | None = None,
    cwd: Path | None = None,
    home: Path | None = None,
    use_cache: bool = True,
) -> RuntimeWindowBurn:
    """Return the real runtime burn for one 5-hour window.

    Falls back to a zeroed `RuntimeWindowBurn` when no runtime data is
    available for the cwd — which is the right behavior: callers should
    treat the absence of runtime data as "I have no information about
    sessions outside MetaEnsemble" rather than panic.
    """
    target = window_id or window_id_for()
    project_dir = claude_project_dir_for_cwd(cwd=cwd, home=home)
    if project_dir is None:
        return RuntimeWindowBurn(window_id=target)

    key = (str(project_dir), target)
    now = time.monotonic()
    if use_cache:
        cached = _cache.get(key)
        if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]

    burn = _scan_jsonls(project_dir, target)
    _cache[key] = (now, burn)
    return burn


def clear_cache() -> None:
    """Used by tests to force a fresh scan."""
    _cache.clear()


def get_session_burn(
    session_id: str | None,
    cwd: Path | None = None,
    home: Path | None = None,
    window_id: str | None = None,
) -> RuntimeWindowBurn:
    """Sum the runtime's usage records for one session_id.

    Claude Code stores each session under
    `~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`. We scan that
    one file (and, defensively, any other JSONL in the project dir whose
    records carry the matching `sessionId`) and sum the `usage` blocks.

    When `window_id` is supplied, only records whose timestamp falls in
    that 5-hour bucket are included. The returned `RuntimeWindowBurn.
    window_id` reflects the filter ("session" when unfiltered, the
    bucket id when filtered) so callers can label the figure honestly.

    Returns a zeroed `RuntimeWindowBurn` when no session_id is supplied
    or no matching file exists. The cwd and home arguments mirror
    `get_window_burn` and exist mostly for test injection.
    """
    if not session_id:
        return RuntimeWindowBurn(window_id=window_id or "session")
    project_dir = claude_project_dir_for_cwd(cwd=cwd, home=home)
    if project_dir is None:
        return RuntimeWindowBurn(window_id=window_id or "session")

    in_t = out_t = cc = cr = 0
    n_msgs = 0
    direct = project_dir / f"{session_id}.jsonl"
    candidates: list[Path] = [direct] if direct.exists() else []
    if not candidates:
        try:
            candidates = list(project_dir.rglob("*.jsonl"))
        except OSError:
            candidates = []

    for jsonl in candidates:
        try:
            with jsonl.open() as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if jsonl != direct and event.get("sessionId") != session_id:
                        continue
                    if window_id is not None:
                        ts = event.get("timestamp")
                        if not ts or _bucket_from_iso(ts) != window_id:
                            continue
                    msg = event.get("message")
                    if not isinstance(msg, dict):
                        continue
                    usage = msg.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    in_t += int(usage.get("input_tokens") or 0)
                    out_t += int(usage.get("output_tokens") or 0)
                    cc += int(usage.get("cache_creation_input_tokens") or 0)
                    cr += int(usage.get("cache_read_input_tokens") or 0)
                    n_msgs += 1
        except OSError:
            continue
    return RuntimeWindowBurn(
        window_id=window_id or "session",
        input_tokens=in_t,
        output_tokens=out_t,
        cache_creation_tokens=cc,
        cache_read_tokens=cr,
        message_count=n_msgs,
        project_dir=project_dir,
    )


# Capacity calibration lives in `metaensemble.lib.native_state`, which reads the
# runtime's `rate_limits` feed (captured by the statusline script). That
# feed is the authoritative source — it tracks the user's actual plan
# (Pro / Max-5 / Max-20) and reflects the current 5-hour window's
# `used_percentage` directly.
