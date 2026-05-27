"""Tests for the statusline writer in `metaensemble/statusline/me_status.py`.

The statusline is the only conduit through which the runtime's `rate_limits`
payload reaches MetaEnsemble. SessionStart, `/limits`, `/standup`, and the
cost gate's capacity calibration all read the file the statusline writes
at `~/.metaensemble/state/runtime-rate-limits.json`.

A previous regression had the writer overwrite the file with
`rate_limits: null` whenever the runtime piped a statusline payload that
did not include rate_limits (a common case on certain refresh events,
model switches, or the first fires of a new session). The next
SessionStart then fell through to the "unavailable" arm even though a
perfectly good in-bucket snapshot had been alive seconds earlier. The
tests below lock the invariant: a payload without rate_limits must
preserve the prior snapshot, never clobber it.
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path


def _run_with_stdin(monkeypatch, stdin_text: str, state_dir: Path) -> None:
    """Drive `me_status.main()` with the given stdin and a controlled state dir."""
    from metaensemble.statusline import me_status

    monkeypatch.setattr(me_status, "_state_dir", lambda: state_dir)
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    buf = io.StringIO()
    with redirect_stdout(buf):
        me_status.main()


def test_payload_without_rate_limits_preserves_prior_snapshot(tmp_path, monkeypatch):
    """A statusline fire without rate_limits must NOT destroy the prior file."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    feed = state_dir / "runtime-rate-limits.json"
    feed.write_text(json.dumps({
        "captured_at": "2026-05-27T17:55:00+00:00",
        "rate_limits": {
            "five_hour":  {"used_percentage": 37, "resets_at": 1779910800},
            "seven_day":  {"used_percentage": 19, "resets_at": 1780354800},
        },
        "model": {"id": "claude-opus-4-7"},
    }))

    payload_without = json.dumps({
        "session_id": "sim",
        "model": {"id": "claude-opus-4-7"},
        "workspace": {"current_dir": "/tmp"},
    })
    _run_with_stdin(monkeypatch, payload_without, state_dir)

    after = json.loads(feed.read_text())
    assert after["rate_limits"] is not None
    assert after["rate_limits"]["five_hour"]["used_percentage"] == 37
    assert after["rate_limits"]["seven_day"]["used_percentage"] == 19
    assert after["captured_at"] == "2026-05-27T17:55:00+00:00"


def test_payload_with_rate_limits_overwrites_snapshot(tmp_path, monkeypatch):
    """A statusline fire with rate_limits must update the file as expected."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    feed = state_dir / "runtime-rate-limits.json"
    feed.write_text(json.dumps({
        "captured_at": "2026-05-27T17:55:00+00:00",
        "rate_limits": {
            "five_hour":  {"used_percentage": 37, "resets_at": 1779910800},
            "seven_day":  {"used_percentage": 19, "resets_at": 1780354800},
        },
    }))

    payload_with = json.dumps({
        "session_id": "sim2",
        "model": {"id": "claude-opus-4-7"},
        "rate_limits": {
            "five_hour":  {"used_percentage": 42, "resets_at": 1779910800},
            "seven_day":  {"used_percentage": 21, "resets_at": 1780354800},
        },
    })
    _run_with_stdin(monkeypatch, payload_with, state_dir)

    after = json.loads(feed.read_text())
    assert after["rate_limits"]["five_hour"]["used_percentage"] == 42
    assert after["rate_limits"]["seven_day"]["used_percentage"] == 21
    assert after["captured_at"] != "2026-05-27T17:55:00+00:00"


def test_payload_with_empty_rate_limits_dict_preserves_prior(tmp_path, monkeypatch):
    """An empty `rate_limits: {}` is just as unhelpful as missing — preserve prior."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    feed = state_dir / "runtime-rate-limits.json"
    feed.write_text(json.dumps({
        "captured_at": "2026-05-27T17:55:00+00:00",
        "rate_limits": {
            "five_hour": {"used_percentage": 50, "resets_at": 1779910800},
        },
    }))

    payload_empty = json.dumps({"session_id": "sim", "rate_limits": {}})
    _run_with_stdin(monkeypatch, payload_empty, state_dir)

    after = json.loads(feed.read_text())
    assert after["rate_limits"]["five_hour"]["used_percentage"] == 50


def test_first_fire_without_rate_limits_writes_nothing(tmp_path, monkeypatch):
    """When the file does not exist yet and the first payload lacks
    rate_limits, do not create a placeholder with rate_limits=None — wait
    for a payload that actually carries the data."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    feed = state_dir / "runtime-rate-limits.json"
    assert not feed.exists()

    payload_without = json.dumps({"session_id": "sim", "model": {"id": "claude-opus-4-7"}})
    _run_with_stdin(monkeypatch, payload_without, state_dir)

    assert not feed.exists()
