"""Tests for native_state and the me_status statusline capture script."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from metaensemble.lib import native_state


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
STATUSLINE_SCRIPT = REPO_ROOT / "metaensemble" / "statusline" / "me_status.py"


def _invoke_statusline(payload: dict, *, home: Path) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["HOME"] = str(home)
    proc = subprocess.run(
        [sys.executable, str(STATUSLINE_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True, text=True, env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_load_returns_none_when_file_missing(tmp_path):
    """Absent capture file → None, no exceptions."""
    state = tmp_path / "missing.json"
    assert native_state.load_native_rate_limits(state) is None


def test_load_handles_malformed_json(tmp_path):
    """Malformed JSON returns None rather than raising."""
    state = tmp_path / "bad.json"
    state.write_text("{not really json")
    assert native_state.load_native_rate_limits(state) is None


def test_parses_well_formed_capture(tmp_path):
    state = tmp_path / "rate.json"
    captured_at = datetime.now(timezone.utc).isoformat()
    state.write_text(json.dumps({
        "captured_at": captured_at,
        "rate_limits": {
            "five_hour_window": {"used_percentage": 42.5, "resets_at": "2026-05-15T20:00:00Z"},
            "seven_day_window": {"used_percentage": 12.0, "resets_at": "2026-05-22T00:00:00Z"},
        },
        "model": "claude-opus-4-7",
        "session_id": "abc123",
    }))
    n = native_state.load_native_rate_limits(state)
    assert n is not None
    assert n.is_fresh
    assert n.five_hour is not None
    assert n.five_hour.used_percentage == 42.5
    assert n.five_hour.remaining_percentage == 57.5
    assert n.seven_day is not None
    assert n.seven_day.used_percentage == 12.0
    assert n.model == "claude-opus-4-7"
    assert n.session_id == "abc123"


def test_load_normalizes_structured_model_identity(tmp_path):
    state = tmp_path / "rate.json"
    captured_at = datetime.now(timezone.utc).isoformat()
    state.write_text(json.dumps({
        "captured_at": captured_at,
        "rate_limits": {},
        "model": {"id": "claude-opus-4-7", "display_name": "Opus 4.7"},
        "session_id": "abc123",
    }))

    n = native_state.load_native_rate_limits(state)

    assert n is not None
    assert n.model == "claude-opus-4-7"


def test_old_capture_is_not_fresh(tmp_path):
    state = tmp_path / "old.json"
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    state.write_text(json.dumps({
        "captured_at": old_ts,
        "rate_limits": {"five_hour_window": {"used_percentage": 50.0, "resets_at": "x"}},
    }))
    n = native_state.load_native_rate_limits(state)
    assert n is not None
    assert not n.is_fresh
    assert n.age_seconds is not None and n.age_seconds > 3000


def test_unknown_window_shape_falls_through(tmp_path):
    """When the runtime ships a different key name, native data returns None for those windows."""
    state = tmp_path / "alt.json"
    state.write_text(json.dumps({
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "rate_limits": {"surprise_window": {"used_percentage": 99}},
    }))
    n = native_state.load_native_rate_limits(state)
    assert n is not None
    assert n.five_hour is None
    assert n.seven_day is None


# --- The statusline script itself ----------------------------------------


def test_statusline_captures_rate_limits_to_state_file(tmp_path):
    """Sending a payload with rate_limits writes the capture file."""
    payload = {
        "session_id": "sess-x",
        "model": "claude-opus-4-7",
        "cwd": "/tmp/proj",
        "rate_limits": {
            "five_hour_window": {"used_percentage": 33.3, "resets_at": "2026-05-15T22:00:00Z"},
            "seven_day_window": {"used_percentage": 8.5, "resets_at": "2026-05-22T00:00:00Z"},
        },
    }
    code, out, err = _invoke_statusline(payload, home=tmp_path)
    assert code == 0, err
    captured = tmp_path / ".metaensemble" / "state" / "runtime-rate-limits.json"
    assert captured.exists()
    data = json.loads(captured.read_text())
    assert data["rate_limits"]["five_hour_window"]["used_percentage"] == 33.3
    assert "5h: 33%" in out
    assert "7d: 8%" in out  # banker's rounding on 8.5 → 8


def test_statusline_handles_missing_rate_limits(tmp_path):
    """Payloads without rate_limits → no file written (preserves last-good
    snapshot when one exists). The earlier behaviour wrote `rate_limits:
    null`, which destroyed prior in-bucket data and forced SessionStart
    into the `unavailable` arm even when valid telemetry had existed
    seconds before. See `test_statusline.py` for the full no-clobber
    invariant suite."""
    payload = {"session_id": "x", "model": "y"}
    code, out, err = _invoke_statusline(payload, home=tmp_path)
    assert code == 0, err
    captured = tmp_path / ".metaensemble" / "state" / "runtime-rate-limits.json"
    assert not captured.exists()
    assert "MetaEnsemble" in out


def test_statusline_handles_empty_stdin(tmp_path):
    """Empty stdin → graceful degradation."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["HOME"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(STATUSLINE_SCRIPT)],
        input="", capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0
    assert "MetaEnsemble" in proc.stdout
