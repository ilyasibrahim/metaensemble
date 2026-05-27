"""Read the native rate-limit data Claude Code persists via the statusline.

`metaensemble/statusline/me_status.py` captures the runtime's `rate_limits`
payload on every statusline refresh and writes it to
`~/.metaensemble/state/runtime-rate-limits.json`. This module is the
reader side: it loads that file, normalizes the field names (the
runtime's exact key shape may change across versions), and returns a
typed `NativeRateLimits` that the cost gate, the /limits tool, and the
doctor consume.

Design notes:
- A missing or stale file is not an error. The caller treats absence
  as "no native data available" and falls back to its configured
  capacity. We never invent capacity from indirect evidence — the
  user's plan can change between yesterday and today, and the only
  trustworthy source is the runtime's own rate-limit headers.
- Staleness is reported (`age_seconds`) so consumers can decide
  whether to trust the snapshot. Statusline refreshes are frequent;
  if the file is older than a few minutes it usually means the
  user has not opened a Claude Code session recently, in which case
  cost-gate decisions are best made against the configured fallback.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from metaensemble.lib.runtime_payload import normalize_model_identity


def format_duration(td: "timedelta | None") -> str:
    """Render a timedelta as a compact "1h32m" / "4d8h" / "0m" string."""
    if td is None:
        return ""
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


_DEFAULT_PATH = Path.home() / ".metaensemble" / "state" / "runtime-rate-limits.json"


@dataclass(frozen=True)
class WindowState:
    """One of the rate-limit windows the runtime exposes (5-hour or 7-day)."""

    used_percentage: float
    resets_at: str | None = None  # ISO-8601 string from the runtime

    @property
    def remaining_percentage(self) -> float:
        return max(0.0, 100.0 - self.used_percentage)

    @property
    def resets_at_dt(self) -> datetime | None:
        """Parse the reset timestamp; None when missing or unparseable."""
        if not self.resets_at:
            return None
        try:
            return datetime.fromisoformat(self.resets_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

    def time_until_reset(self, now: datetime | None = None) -> "timedelta | None":
        """Return time remaining until this window resets; None when unknown."""
        reset = self.resets_at_dt
        if reset is None:
            return None
        ref = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        delta = reset - ref.astimezone(timezone.utc)
        return delta if delta.total_seconds() > 0 else timedelta(0)

    def is_current(self, now: datetime | None = None) -> bool:
        """True when this window has not yet rolled over (reset is in the future)."""
        td = self.time_until_reset(now=now)
        return td is not None and td.total_seconds() > 0


@dataclass(frozen=True)
class NativeRateLimits:
    """Parsed snapshot of the runtime's rate-limit data."""

    five_hour: WindowState | None
    seven_day: WindowState | None
    captured_at: str | None
    age_seconds: float | None
    model: str | None = None
    session_id: str | None = None

    @property
    def is_fresh(self) -> bool:
        """Treat older than 5 minutes as stale.

        Statusline refreshes are frequent — a few seconds typically. A
        snapshot older than 5 minutes usually means the user has not
        opened Claude Code recently, so the runtime's view of the
        window may be out of date.
        """
        return self.age_seconds is not None and self.age_seconds < 300.0


def _format_reset_time(raw: Any) -> str | None:
    """Normalize whatever shape the runtime sends for `resets_at`.

    Claude Code may send the reset time as an ISO-8601 string, a Unix
    epoch number (seconds), or a Unix epoch number with milliseconds.
    All three render as ISO-8601 UTC for display consistency.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        seconds = raw / 1000.0 if raw > 1_000_000_000_000 else float(raw)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return str(raw)
    text = str(raw)
    # If it's a numeric string, parse it as epoch.
    try:
        as_num = float(text)
        seconds = as_num / 1000.0 if as_num > 1_000_000_000_000 else as_num
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except ValueError:
        pass
    return text


def _coerce_window(raw: Any) -> WindowState | None:
    if not isinstance(raw, dict):
        return None
    pct = raw.get("used_percentage")
    if pct is None:
        return None
    try:
        used = float(pct)
    except (TypeError, ValueError):
        return None
    return WindowState(
        used_percentage=used,
        resets_at=_format_reset_time(raw.get("resets_at") or raw.get("reset_at")),
    )


def load_native_rate_limits(path: Path | None = None) -> NativeRateLimits | None:
    """Load the latest statusline-captured rate-limit snapshot.

    Returns None when the file is missing, unreadable, or malformed —
    NEVER raises. Callers fall back to their configured capacity.
    """
    src = path or _DEFAULT_PATH
    if not src.exists():
        return None
    try:
        data = json.loads(src.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None

    rate_limits = data.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return NativeRateLimits(
            five_hour=None, seven_day=None,
            captured_at=data.get("captured_at"),
            age_seconds=_age_seconds(data.get("captured_at")),
            model=normalize_model_identity(data.get("model")),
            session_id=data.get("session_id"),
        )

    five_h_raw = (
        rate_limits.get("five_hour_window")
        or rate_limits.get("five_hour")
        or rate_limits.get("5h")
    )
    seven_d_raw = (
        rate_limits.get("seven_day_window")
        or rate_limits.get("seven_day")
        or rate_limits.get("7d")
    )

    return NativeRateLimits(
        five_hour=_coerce_window(five_h_raw),
        seven_day=_coerce_window(seven_d_raw),
        captured_at=data.get("captured_at"),
        age_seconds=_age_seconds(data.get("captured_at")),
        model=normalize_model_identity(data.get("model")),
        session_id=data.get("session_id"),
    )


def _age_seconds(captured_at_iso: Any) -> float | None:
    if not isinstance(captured_at_iso, str):
        return None
    try:
        dt = datetime.fromisoformat(captured_at_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    now = datetime.now(timezone.utc)
    return (now - dt.astimezone(timezone.utc)).total_seconds()


# --- Window-percentage rendering (honest about uncertainty) -------------
#
# The four `WindowKind` values enumerate every way the 5-hour percentage
# can be answered. Renderers MUST branch on `WindowReport.kind`, never on
# `source` substrings — that's what keeps this module misuse-resistant.
# A 2026-05-26 regression rendered `0% of plan used` when no usable
# native snapshot existed; the fix moves the discriminator into the type
# system so a future renderer can't silently re-introduce that string.

WindowKind = Literal[
    "live_plan",            # Fresh (or still-current) native — plan-wide percentage.
    "last_observed_plan",   # Native present but stale AND 5h bucket has rolled.
    "project_fallback",     # No usable 5h native; non-zero observed project burn.
    "unavailable",          # No usable 5h native; zero observed burn OR burn > capacity.
]


@dataclass(frozen=True)
class WindowReport:
    """Resolved 5-hour window-usage state for display.

    `pct_used` is set only when the value is a CURRENT percentage worth
    showing as such. Stale native data lives in `last_observed_pct`
    instead, so a 6-hour-old 63% never silently renders as the live 5h
    figure. Renderers branch on `kind`; see `resolve_window_report` for
    the full decision ladder.
    """

    kind: WindowKind
    used_tokens: int
    capacity_tokens: int
    pct_used: float | None
    pct_remaining: float | None
    source: str
    note: str | None = None
    last_observed_pct: float | None = None
    snapshot_age_seconds: float | None = None

    @property
    def has_percentage(self) -> bool:
        """True when `pct_used` is set — i.e., a current percentage worth showing.

        Both `last_observed_plan` and `unavailable` return False; renderers
        must branch on `kind` to distinguish "no telemetry" from "stale
        telemetry available as history."
        """
        return self.pct_used is not None


def format_age(age_seconds: float | None) -> str:
    """Compact age string for stale-snapshot rendering: `6m`, `2h14m`, `4d8h`."""
    if age_seconds is None or age_seconds < 0:
        return "unknown age"
    rendered = format_duration(timedelta(seconds=age_seconds))
    return rendered or "0m"


def resolve_window_report(
    used_tokens: int,
    capacity_tokens: int,
    native: "NativeRateLimits | None",
) -> WindowReport:
    """Render the 5-hour window-usage state honestly.

    Decision ladder — every cold-start regression maps to exactly one arm:

      1. `live_plan`          — Fresh native OR still-current 5h bucket.
                                Authoritative plan-wide percentage.
      2. `last_observed_plan` — Native present but stale AND bucket rolled.
                                `pct_used` is None; `last_observed_pct`
                                carries the historical value so renderers
                                frame it as history, not as current state.
      3. `project_fallback`   — No usable 5h native window AND non-zero
                                local burn. Percentage IS meaningful as
                                project-burn vs configured fallback, but
                                is NOT plan-wide — the label MUST NOT
                                include "of plan used."
      4. `unavailable`        — No usable 5h native window AND zero local
                                burn (or burn > capacity, where even the
                                project-burn ratio is unreliable).
                                `pct_used = None`. Renderers MUST NEVER
                                synthesize 0% here.

    Mode A (stale snapshot) maps to (2). Mode B (no usable snapshot,
    the screenshot scenario) maps to (4) when `used_tokens == 0`, (3)
    when `used_tokens > 0`. The "no usable 5h native window" check
    covers both `native is None` and `native.five_hour is None` —
    a payload with `seven_day` but no `five_hour` still routes to the
    no-native arms.
    """
    five_h = native.five_hour if native is not None else None
    age = native.age_seconds if native is not None else None

    # (1) live_plan — fresh or still-current native snapshot.
    if (
        native is not None
        and five_h is not None
        and (native.is_fresh or five_h.is_current())
    ):
        pct = five_h.used_percentage
        source = (
            "runtime rate_limits feed"
            if native.is_fresh
            else "runtime rate_limits feed (snapshot from prior session, window still open)"
        )
        return WindowReport(
            kind="live_plan",
            used_tokens=used_tokens,
            capacity_tokens=capacity_tokens,
            pct_used=pct,
            pct_remaining=max(0.0, 100.0 - pct),
            source=source,
            snapshot_age_seconds=age,
        )

    # (2) last_observed_plan — snapshot present but stale and bucket rolled.
    # A 6h-old 63% is NOT the current 5h percentage. Carry it in
    # `last_observed_pct` so the renderer frames it explicitly as
    # history; leave `pct_used` None so `has_percentage` is False.
    if native is not None and five_h is not None:
        pct = five_h.used_percentage
        age_str = format_age(age)
        return WindowReport(
            kind="last_observed_plan",
            used_tokens=used_tokens,
            capacity_tokens=capacity_tokens,
            pct_used=None,
            pct_remaining=None,
            source="last-observed runtime rate_limits feed",
            note=(
                f"Live usage unavailable; last runtime snapshot was "
                f"{pct:.0f}%, {age_str} old, from a previous window. "
                f"Open a Claude Code session so the statusline refreshes "
                f"`~/.metaensemble/state/runtime-rate-limits.json`."
            ),
            last_observed_pct=pct,
            snapshot_age_seconds=age,
        )

    # No usable five-hour native window past this point.

    # (3) project_fallback — local burn vs configured capacity. Meaningful
    # as a project metric; NOT plan usage. Renderers MUST surface scope.
    if capacity_tokens > 0 and 0 < used_tokens <= capacity_tokens:
        pct = (used_tokens / capacity_tokens) * 100.0
        return WindowReport(
            kind="project_fallback",
            used_tokens=used_tokens,
            capacity_tokens=capacity_tokens,
            pct_used=pct,
            pct_remaining=max(0.0, 100.0 - pct),
            source="observed project tokens vs configured fallback capacity",
            note=(
                f"This is the project's observed burn ({used_tokens:,} tokens) "
                f"against the configured fallback ({capacity_tokens:,} tokens), "
                "not plan-wide usage. Plan-wide numbers require a statusline refresh."
            ),
        )

    # (4) unavailable — refuse to render a percentage. Never synthesize 0%.
    if used_tokens == 0:
        unavailable_note = (
            "Live plan telemetry is not yet available. Open a Claude Code "
            "session and let the statusline refresh "
            "`~/.metaensemble/state/runtime-rate-limits.json`."
        )
    else:
        unavailable_note = (
            f"Observed project burn ({used_tokens:,} tokens) exceeds the "
            f"configured fallback ({capacity_tokens:,} tokens), so even the "
            "project-burn ratio is unreliable. Refresh the statusline."
        )
    return WindowReport(
        kind="unavailable",
        used_tokens=used_tokens,
        capacity_tokens=capacity_tokens,
        pct_used=None,
        pct_remaining=None,
        source="plan usage unavailable",
        note=unavailable_note,
    )


# --- Seven-day window rendering (separate from WindowReport) ------------
#
# The 7-day row has no project-scoped fallback (Ledger/runtime burn is
# bucketed in 5h windows; no aggregated 7-day denominator exists), so
# `project_fallback` is not one of its kinds. Kept as a sibling type
# rather than folded into WindowReport to avoid overloading one type
# with two unrelated decision trees.

SevenDayKind = Literal["live_plan", "last_observed_plan", "unavailable"]


@dataclass(frozen=True)
class SevenDayLine:
    """Resolved 7-day window-usage state for display."""

    kind: SevenDayKind
    pct_used: float | None
    last_observed_pct: float | None = None
    snapshot_age_seconds: float | None = None
    source: str = ""
    note: str | None = None

    @property
    def has_percentage(self) -> bool:
        return self.pct_used is not None


def resolve_seven_day(native: "NativeRateLimits | None") -> SevenDayLine:
    """Render the 7-day window-usage state honestly.

    Decision tree (renderers branch on `kind`):

      1. `live_plan`          — `native.is_fresh` AND `seven_day` present.
      2. `last_observed_plan` — Snapshot present with `seven_day`, but stale.
                                Surfaces as history, never as current.
      3. `unavailable`        — No usable `seven_day` block. Renderers
                                omit the 7-day line entirely.
    """
    if native is None or native.seven_day is None:
        return SevenDayLine(
            kind="unavailable",
            pct_used=None,
            source="7-day plan usage unavailable",
        )

    seven_d = native.seven_day
    age = native.age_seconds

    if native.is_fresh:
        return SevenDayLine(
            kind="live_plan",
            pct_used=seven_d.used_percentage,
            source="runtime rate_limits feed",
            snapshot_age_seconds=age,
        )

    age_str = format_age(age)
    return SevenDayLine(
        kind="last_observed_plan",
        pct_used=None,
        last_observed_pct=seven_d.used_percentage,
        snapshot_age_seconds=age,
        source="last-observed runtime rate_limits feed",
        note=(
            f"Live 7-day usage unavailable; last runtime snapshot was "
            f"{seven_d.used_percentage:.0f}%, {age_str} old."
        ),
    )
