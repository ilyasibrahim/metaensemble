#!/usr/bin/env python3
"""MetaEnsemble statusline script.

Claude Code v2.1.80+ ships rate-limit information to statusline scripts via
the JSON payload it pipes to stdin. The payload includes a `rate_limits`
field with the 5-hour and 7-day window usage in `used_percentage` and the
reset timestamps. This is the runtime's native answer to "what's my real
window state?", and it is what MetaEnsemble's cost gate, /limits, /standup,
and doctor should read.

This script does two things on every statusline refresh (typically every
few seconds during an interactive session):

1.  Capture: write the latest `rate_limits` payload (plus model id,
    session id, and a timestamp) to
    `~/.metaensemble/state/runtime-rate-limits.json`. Any MetaEnsemble
    consumer reads from there.

2.  Render: print a short status line for the user — `MetaEnsemble | 5h:
    28% | 7d: 12%` or a quieter form when rate-limit info is absent.
    The script is generous with whatever payload shape the runtime
    sends; missing fields produce blank substrings, never errors.

Design notes:
- The script must complete fast (Claude Code refreshes the statusline
  often). The file write is a single open() + os.replace() for
  atomicity; no JSON validation beyond what `json.load` already does.
- No tokens are consumed; this is a pure local read/write.
- Errors are swallowed and logged so a misshapen payload never breaks
  the user's status line.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _state_dir() -> Path:
    """User-level MetaEnsemble state directory; created on first write."""
    base = Path.home() / ".metaensemble" / "state"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _atomic_write_json(target: Path, payload: dict[str, Any]) -> None:
    """Atomic JSON write so concurrent readers never see a partial file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, target)


def _format_window_summary(window: dict[str, Any]) -> str:
    """Render one `rate_limits` window block as `"66% (1h32m left)"`.

    The leading percentage comes from `used_percentage`; the parenthetical
    is derived from `resets_at` (ISO-8601 string or Unix epoch). When the
    reset timestamp is missing or unparseable the parenthetical is
    omitted.
    """
    if not isinstance(window, dict):
        return ""
    pct = window.get("used_percentage")
    if pct is None:
        return ""
    try:
        pct_str = f"{float(pct):.0f}%"
    except (TypeError, ValueError):
        return ""
    reset_raw = window.get("resets_at") or window.get("reset_at")
    reset_dt = _parse_reset(reset_raw)
    if reset_dt is None:
        return pct_str
    delta = reset_dt - datetime.now(timezone.utc)
    remaining = _format_duration(delta)
    if not remaining:
        return pct_str
    return f"{pct_str} ({remaining} left)"


def _parse_reset(raw: Any) -> "datetime | None":
    """Parse the runtime's reset timestamp into a UTC datetime.

    Accepts ISO-8601 strings, Unix-epoch seconds, and Unix-epoch
    milliseconds — same shapes the runtime has shipped across versions.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        secs = raw / 1000.0 if raw > 1_000_000_000_000 else float(raw)
        try:
            return datetime.fromtimestamp(secs, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw)
    try:
        secs = float(text)
        secs = secs / 1000.0 if secs > 1_000_000_000_000 else secs
        return datetime.fromtimestamp(secs, tz=timezone.utc)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def _format_duration(td) -> str:
    """Compact duration: `1h32m`, `4d8h`, `45m`, `0m`."""
    total = int(td.total_seconds())
    if total <= 0:
        return "0m"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d{hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        print("MetaEnsemble")
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print("MetaEnsemble")
        return 0

    rate_limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    # Claude Code does not always include `rate_limits` in the statusline
    # payload — e.g. on certain refresh events, model switches, or the
    # first fires of a new session. Writing the file with `rate_limits:
    # null` in those cases destroys the prior snapshot, and the next
    # SessionStart hook then falls through to the "unavailable" arm even
    # when a perfectly good in-bucket snapshot existed seconds ago. So:
    # only update the snapshot when the runtime actually gave us rate
    # limits to record. Otherwise leave the last-good file untouched.
    if isinstance(rate_limits, dict) and rate_limits:
        captured = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "rate_limits": rate_limits,
            # Surface a few extra fields that are often useful alongside
            # the rate-limit numbers.
            "model": payload.get("model"),
            "session_id": payload.get("session_id"),
            "workspace": payload.get("workspace") or payload.get("cwd"),
        }
        try:
            _atomic_write_json(_state_dir() / "runtime-rate-limits.json", captured)
        except OSError:
            pass

    # Compose the displayed status line.
    parts: list[str] = ["MetaEnsemble"]
    if isinstance(rate_limits, dict):
        # Defensive walk: the runtime's exact key naming may change.
        five_h = (
            rate_limits.get("five_hour_window")
            or rate_limits.get("five_hour")
            or rate_limits.get("5h")
        )
        seven_d = (
            rate_limits.get("seven_day_window")
            or rate_limits.get("seven_day")
            or rate_limits.get("7d")
        )
        five_h_str = _format_window_summary(five_h) if isinstance(five_h, dict) else ""
        seven_d_str = _format_window_summary(seven_d) if isinstance(seven_d, dict) else ""
        if five_h_str:
            parts.append(f"5h: {five_h_str}")
        if seven_d_str:
            parts.append(f"7d: {seven_d_str}")
    print(" · ".join(parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
