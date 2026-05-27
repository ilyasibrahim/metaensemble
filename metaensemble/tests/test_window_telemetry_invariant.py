"""Regression tests for the 5h/7d telemetry rendering invariant.

The pinned invariant:

    Absence of live plan telemetry must never render as "0% of plan used".

These tests cover the four arms of `resolve_window_report` and the three
arms of `resolve_seven_day`, then assert that every renderer surface
(session_start, session_summary, /standup, /limits) respects the
invariant.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from metaensemble.lib.native_state import (
    NativeRateLimits,
    WindowState,
    resolve_seven_day,
    resolve_window_report,
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _native(
    *,
    age_seconds: float,
    five_h_pct: float | None = 63.0,
    five_h_reset: datetime | None = None,
    seven_d_pct: float | None = 4.0,
    seven_d_reset: datetime | None = None,
) -> NativeRateLimits:
    """Build a NativeRateLimits with controllable freshness and currency.

    `age_seconds` drives `is_fresh` (< 300 ⇒ True). `five_h_reset` /
    `seven_d_reset` drive `is_current` (future ⇒ True).
    """
    now = datetime.now(timezone.utc)
    if five_h_reset is None:
        five_h_reset = now + timedelta(hours=2)
    if seven_d_reset is None:
        seven_d_reset = now + timedelta(days=5)
    captured_at = now - timedelta(seconds=age_seconds)
    return NativeRateLimits(
        five_hour=(
            WindowState(used_percentage=five_h_pct, resets_at=_iso(five_h_reset))
            if five_h_pct is not None else None
        ),
        seven_day=(
            WindowState(used_percentage=seven_d_pct, resets_at=_iso(seven_d_reset))
            if seven_d_pct is not None else None
        ),
        captured_at=_iso(captured_at),
        age_seconds=age_seconds,
    )


# --- resolve_window_report (5h) — cases 1-7 -----------------------------


def test_case1_live_plan_fresh_snapshot():
    """Case 1: fresh snapshot — `kind="live_plan"`, pct from native."""
    native = _native(age_seconds=8.0, five_h_pct=63.0)
    r = resolve_window_report(used_tokens=0, capacity_tokens=200_000, native=native)
    assert r.kind == "live_plan"
    assert r.pct_used == 63.0
    assert r.note is None


def test_case2_last_observed_plan_stale_rolled_snapshot():
    """Case 2: stale snapshot, 5h bucket rolled — `kind="last_observed_plan"`."""
    now = datetime.now(timezone.utc)
    native = _native(
        age_seconds=6 * 3600,
        five_h_pct=63.0,
        five_h_reset=now - timedelta(hours=1),
    )
    r = resolve_window_report(used_tokens=0, capacity_tokens=200_000, native=native)
    assert r.kind == "last_observed_plan"
    assert r.pct_used is None                       # not current
    assert r.last_observed_pct == 63.0              # but history is carried
    assert r.snapshot_age_seconds > 5 * 60
    assert "live usage unavailable" in (r.note or "").lower()


def test_case3_unavailable_no_snapshot_zero_usage():
    """Case 3: no snapshot + zero observed usage (the screenshot)."""
    r = resolve_window_report(used_tokens=0, capacity_tokens=200_000, native=None)
    assert r.kind == "unavailable"
    assert r.pct_used is None
    assert r.last_observed_pct is None


def test_case4_unavailable_seven_day_present_five_hour_missing():
    """Case 4: native present, but `five_hour is None` — same as no snapshot."""
    native = _native(age_seconds=8.0, five_h_pct=None, seven_d_pct=4.0)
    r = resolve_window_report(used_tokens=0, capacity_tokens=200_000, native=native)
    assert r.kind == "unavailable"
    assert r.pct_used is None


def test_case5_project_fallback_no_native_nonzero_usage():
    """Case 5: no usable 5h native + non-zero project burn."""
    r = resolve_window_report(used_tokens=24_000, capacity_tokens=200_000, native=None)
    assert r.kind == "project_fallback"
    assert r.pct_used == pytest.approx(12.0)
    assert "not plan-wide usage" in (r.note or "")


def test_case6_unavailable_usage_exceeds_capacity():
    """Case 6: usage > capacity ⇒ project-burn ratio unreliable, refuse percent."""
    r = resolve_window_report(used_tokens=300_000, capacity_tokens=200_000, native=None)
    assert r.kind == "unavailable"
    assert r.pct_used is None


def test_case7_malformed_snapshot_treated_as_no_snapshot():
    """Case 7: snapshot file existed but didn't parse a 5h window → behaves like case 3/5."""
    # Simulate the load_native_rate_limits return value when only seven_day parsed.
    native = NativeRateLimits(
        five_hour=None, seven_day=WindowState(used_percentage=4.0, resets_at=None),
        captured_at=None, age_seconds=10.0,
    )
    r_zero = resolve_window_report(used_tokens=0, capacity_tokens=200_000, native=native)
    assert r_zero.kind == "unavailable"
    r_nonzero = resolve_window_report(used_tokens=10_000, capacity_tokens=200_000, native=native)
    assert r_nonzero.kind == "project_fallback"


# --- resolve_seven_day — cases 8-10 -------------------------------------


def test_case8_seven_day_live_plan():
    """Case 8: fresh snapshot with seven_day."""
    native = _native(age_seconds=10.0, seven_d_pct=4.0)
    s = resolve_seven_day(native)
    assert s.kind == "live_plan"
    assert s.pct_used == 4.0


def test_case9_seven_day_last_observed_stale_snapshot():
    """Case 9: stale snapshot — seven_day surfaces as last-observed."""
    native = _native(age_seconds=6 * 3600, seven_d_pct=4.0)
    s = resolve_seven_day(native)
    assert s.kind == "last_observed_plan"
    assert s.pct_used is None
    assert s.last_observed_pct == 4.0
    assert s.snapshot_age_seconds > 5 * 60


def test_case10_seven_day_unavailable_no_data():
    """Case 10: native is None or seven_day is None ⇒ unavailable."""
    assert resolve_seven_day(None).kind == "unavailable"
    native_missing_7d = _native(age_seconds=10.0, seven_d_pct=None)
    assert resolve_seven_day(native_missing_7d).kind == "unavailable"


# --- Renderer parity — cases 11-14 --------------------------------------
#
# Each renderer is tested directly via its module-private helpers. We
# import them by name from each surface and check their outputs against
# the four kinds. The invariant assertion ("no '0% of plan used' on
# non-live paths") is in case 15 below.


def _renderer_helpers():
    """Import the four surfaces' helper functions.

    Returns a dict mapping surface name → (five_hour_helper, seven_day_helper).
    Late import so a failure here surfaces as an ImportError per surface,
    not at module-collection time.
    """
    from metaensemble.hooks import session_start, session_summary
    from metaensemble.tools import standup, limits
    return {
        "session_start": (
            session_start._format_five_hour_line,
            session_start._format_seven_day_line,
        ),
        "session_summary": (
            lambda r, w: session_summary._format_five_hour_line("2026-05-26T10", r, w),
            session_summary._format_seven_day_line,
        ),
        "standup": (
            standup._format_standup_five_hour,
            standup._format_standup_seven_day,
        ),
        "limits": (
            limits._plan_5h_row,
            # window's 7-day returns a list; collapse for uniform testing.
            lambda s, w: ("\n".join(limits._seven_day_block(s, w)) or None),
        ),
    }


@pytest.mark.parametrize("surface", ["session_start", "session_summary", "standup", "limits"])
def test_case11_14_renderer_live_plan_includes_plan_used(surface):
    """Live_plan must include the substring '% of plan used' (the warm path)."""
    helpers = _renderer_helpers()
    five_hour_helper, _ = helpers[surface]
    native = _native(age_seconds=8.0, five_h_pct=63.0)
    r = resolve_window_report(0, 200_000, native)
    if surface == "limits":
        # /limits uses "% used" in its table cell, never "% of plan used".
        out = five_hour_helper(r)
        assert "63.0% used" in out
    else:
        out = five_hour_helper(r, native.five_hour)
        assert "of plan used" in out
        assert "63" in out


@pytest.mark.parametrize("surface", ["session_start", "session_summary", "standup", "limits"])
def test_case11_14_renderer_last_observed_plan(surface):
    """Stale-rolled snapshot: line must NOT include 'of plan used' (it's historical)."""
    helpers = _renderer_helpers()
    five_hour_helper, _ = helpers[surface]
    now = datetime.now(timezone.utc)
    native = _native(
        age_seconds=6 * 3600,
        five_h_pct=63.0,
        five_h_reset=now - timedelta(hours=1),
    )
    r = resolve_window_report(0, 200_000, native)
    if surface == "limits":
        out = five_hour_helper(r)
    else:
        out = five_hour_helper(r, native.five_hour)
    assert "of plan used" not in out
    # The 63% MUST still be surfaced — just framed as history.
    assert "63" in out


@pytest.mark.parametrize("surface", ["session_start", "session_summary", "standup", "limits"])
def test_case11_14_renderer_unavailable_no_zero_percent(surface):
    """The screenshot scenario: native missing AND zero usage. No 0% leak."""
    helpers = _renderer_helpers()
    five_hour_helper, _ = helpers[surface]
    r = resolve_window_report(0, 200_000, None)
    if surface == "limits":
        out = five_hour_helper(r)
    else:
        out = five_hour_helper(r, None)
    assert "0% of plan used" not in out
    assert "0.0% of plan used" not in out
    assert "of plan used" not in out
    assert "unavailable" in out.lower()


@pytest.mark.parametrize("surface", ["session_start", "session_summary", "standup", "limits"])
def test_case11_14_renderer_project_fallback_not_labeled_plan(surface):
    """Project-fallback line must NOT include 'of plan used' (scope-confusion fix)."""
    helpers = _renderer_helpers()
    five_hour_helper, _ = helpers[surface]
    r = resolve_window_report(24_000, 200_000, None)
    if surface == "limits":
        out = five_hour_helper(r)
    else:
        out = five_hour_helper(r, None)
    assert "of plan used" not in out
    # Project framing should be discoverable in the text.
    assert "project" in out.lower() or "fallback" in out.lower()


# --- Case 15 — invariant property test ----------------------------------


@pytest.mark.parametrize(
    "used,cap,native_builder",
    [
        # The four non-live-plan inputs the renderers must survive.
        (0, 200_000, lambda: None),                                     # unavailable / no snapshot
        (0, 200_000, lambda: _native(                                   # unavailable / malformed 5h
            age_seconds=10.0, five_h_pct=None, seven_d_pct=4.0,
        )),
        (300_000, 200_000, lambda: None),                               # unavailable / over capacity
        (24_000, 200_000, lambda: None),                                # project_fallback
        (0, 200_000, lambda: _native(                                   # last_observed_plan
            age_seconds=6 * 3600, five_h_pct=63.0,
            five_h_reset=datetime.now(timezone.utc) - timedelta(hours=1),
        )),
    ],
)
def test_case15_no_zero_percent_of_plan_used_on_non_live_paths(used, cap, native_builder):
    """Property: across every non-live-plan input, no surface emits '0% of plan used'.

    Locks the single hard invariant from §8 of the regression report —
    closing the door on every future fifth surface inheriting the gate.
    """
    helpers = _renderer_helpers()
    native = native_builder()
    report = resolve_window_report(used, cap, native)
    assert report.kind != "live_plan", "fixture mis-set — must drive a non-live arm"

    for surface_name, (five_h_helper, _seven_helper) in helpers.items():
        five_h = native.five_hour if native is not None else None
        out = (
            five_h_helper(report)
            if surface_name == "limits"
            else five_h_helper(report, five_h)
        )
        haystack = out.lower()
        assert "0% of plan used" not in haystack, (
            f"surface={surface_name} kind={report.kind} leaked '0% of plan used': {out!r}"
        )
        assert "0.0% of plan used" not in haystack, (
            f"surface={surface_name} kind={report.kind} leaked '0.0% of plan used': {out!r}"
        )
