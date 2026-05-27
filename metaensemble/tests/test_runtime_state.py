"""Tests for runtime_state — reading the real window burn from Claude Code's jsonls."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from metaensemble.lib.runtime_state import (
    _encode_cwd_for_runtime,
    clear_cache,
    get_window_burn,
    window_id_for,
)


def _write_session(jsonl: Path, *, ts: str, usage: dict | None) -> None:
    """Append one assistant-message event with the given usage block."""
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "timestamp": ts,
        "type": "assistant",
        "message": {"role": "assistant", "content": [], "usage": usage} if usage else {"role": "user"},
    }
    with jsonl.open("a") as f:
        f.write(json.dumps(event) + "\n")


def test_encode_cwd_matches_runtime_naming(tmp_path):
    """The runtime's project-dir encoding replaces non-alphanumeric chars with `-`.

    The runtime collapses slashes, spaces, parentheses, and other
    shell-sensitive characters to `-`. We mirror that encoding so the
    project-directory lookup hits the correct path.
    """
    import re
    encoded = _encode_cwd_for_runtime(tmp_path)
    expected = re.sub(r"[^A-Za-z0-9.]", "-", str(tmp_path.resolve()))
    assert encoded == expected
    assert encoded.startswith("-")  # absolute path
    assert "/" not in encoded
    assert " " not in encoded


def test_encode_cwd_handles_spaces_and_special_chars(tmp_path):
    """A cwd containing a space encodes the space as `-`, not preserved."""
    proj = tmp_path / "My Cool Project"
    proj.mkdir()
    encoded = _encode_cwd_for_runtime(proj)
    assert " " not in encoded
    assert "My-Cool-Project" in encoded


def test_window_id_aligns_to_5_hour_buckets():
    assert window_id_for(datetime(2026, 5, 15, 0, 0, 0, tzinfo=timezone.utc)) == "2026-05-15T00"
    assert window_id_for(datetime(2026, 5, 15, 4, 59, 0, tzinfo=timezone.utc)) == "2026-05-15T00"
    assert window_id_for(datetime(2026, 5, 15, 5, 0, 0, tzinfo=timezone.utc)) == "2026-05-15T05"
    assert window_id_for(datetime(2026, 5, 15, 12, 30, 0, tzinfo=timezone.utc)) == "2026-05-15T10"
    assert window_id_for(datetime(2026, 5, 15, 23, 59, 0, tzinfo=timezone.utc)) == "2026-05-15T20"


def test_no_runtime_dir_returns_zeros(tmp_path):
    """Absent project directory produces a zeroed burn, not an exception."""
    clear_cache()
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    burn = get_window_burn(cwd=tmp_path / "nowhere", home=fake_home, use_cache=False)
    assert burn.input_tokens == 0
    assert burn.output_tokens == 0
    assert burn.message_count == 0
    assert burn.project_dir is None


def test_scans_jsonls_and_sums_usage(tmp_path):
    """The scanner aggregates usage across every JSONL in the project dir."""
    clear_cache()
    home = tmp_path / "home"
    cwd = tmp_path / "proj"
    cwd.mkdir(parents=True)
    proj_dir = home / ".claude" / "projects" / _encode_cwd_for_runtime(cwd)
    _write_session(proj_dir / "session-1.jsonl",
                   ts="2026-05-15T10:00:00.000+00:00",
                   usage={"input_tokens": 100, "output_tokens": 200,
                          "cache_creation_input_tokens": 50,
                          "cache_read_input_tokens": 1000})
    _write_session(proj_dir / "session-1.jsonl",
                   ts="2026-05-15T11:00:00.000+00:00",
                   usage={"input_tokens": 50, "output_tokens": 300,
                          "cache_creation_input_tokens": 0,
                          "cache_read_input_tokens": 500})
    # Second session in same window
    _write_session(proj_dir / "abc/session-2.jsonl",
                   ts="2026-05-15T13:00:00.000+00:00",
                   usage={"input_tokens": 25, "output_tokens": 75,
                          "cache_creation_input_tokens": 10,
                          "cache_read_input_tokens": 100})
    # A different window — should NOT count
    _write_session(proj_dir / "session-old.jsonl",
                   ts="2026-05-15T05:00:00.000+00:00",
                   usage={"input_tokens": 9999, "output_tokens": 9999})

    burn = get_window_burn(window_id="2026-05-15T10", cwd=cwd, home=home, use_cache=False)
    assert burn.input_tokens == 175  # 100 + 50 + 25
    assert burn.output_tokens == 575  # 200 + 300 + 75
    assert burn.cache_creation_tokens == 60  # 50 + 0 + 10
    assert burn.cache_read_tokens == 1600  # 1000 + 500 + 100
    assert burn.message_count == 3
    assert burn.metered_total == 750
    assert burn.all_in_tokens == 2410


def test_corrupt_jsonl_does_not_crash(tmp_path):
    """A line that fails JSON parse must not abort the scan."""
    clear_cache()
    home = tmp_path / "home"
    cwd = tmp_path / "proj"
    cwd.mkdir(parents=True)
    proj_dir = home / ".claude" / "projects" / _encode_cwd_for_runtime(cwd)
    proj_dir.mkdir(parents=True)
    (proj_dir / "broken.jsonl").write_text(
        "this is not json\n"
        "{\"timestamp\": \"2026-05-15T10:00:00Z\", "
        "\"message\": {\"role\": \"assistant\", \"usage\": "
        "{\"input_tokens\": 7, \"output_tokens\": 11}}}\n"
    )
    burn = get_window_burn(window_id="2026-05-15T10", cwd=cwd, home=home, use_cache=False)
    assert burn.input_tokens == 7
    assert burn.output_tokens == 11


def test_missing_usage_skipped(tmp_path):
    """Events without `usage` (user messages, system events) are not counted."""
    clear_cache()
    home = tmp_path / "home"
    cwd = tmp_path / "proj"
    cwd.mkdir(parents=True)
    proj_dir = home / ".claude" / "projects" / _encode_cwd_for_runtime(cwd)
    _write_session(proj_dir / "s.jsonl", ts="2026-05-15T10:00:00Z", usage=None)
    burn = get_window_burn(window_id="2026-05-15T10", cwd=cwd, home=home, use_cache=False)
    assert burn.input_tokens == 0
    assert burn.message_count == 0


def test_effective_capacity_uses_manual_when_no_native_data(tmp_path, monkeypatch):
    """Without native rate-limit data, capacity uses the manual fallback."""
    from metaensemble.lib import native_state
    from metaensemble.lib.config import BudgetConfig, effective_capacity_tokens
    # Point load_native_rate_limits at a missing file.
    monkeypatch.setattr(native_state, "_DEFAULT_PATH", tmp_path / "missing.json")

    cfg = BudgetConfig(
        run_soft_pct_of_capacity=5.0, run_hard_pct_of_capacity=15.0,
        window_warn_pct_remaining=30.0, window_block_pct_remaining=10.0,
        window_capacity_tokens=88_000,
        irreversible_actions=[], auto_calibrate_capacity=True,
    )
    cap = effective_capacity_tokens(cfg, home=tmp_path / "empty-home")
    assert cap == 88_000


def test_effective_capacity_pinned_when_calibration_disabled(tmp_path):
    """Manual pin overrides any native data when auto_calibrate_capacity is False."""
    from metaensemble.lib.config import BudgetConfig, effective_capacity_tokens
    cfg = BudgetConfig(
        run_soft_pct_of_capacity=5.0, run_hard_pct_of_capacity=15.0,
        window_warn_pct_remaining=30.0, window_block_pct_remaining=10.0,
        window_capacity_tokens=44_000,
        irreversible_actions=[], auto_calibrate_capacity=False,
    )
    cap = effective_capacity_tokens(cfg)
    assert cap == 44_000


def test_cache_is_used_when_enabled(tmp_path):
    """Calling twice within the TTL hits the in-process cache."""
    clear_cache()
    home = tmp_path / "home"
    cwd = tmp_path / "proj"
    cwd.mkdir(parents=True)
    proj_dir = home / ".claude" / "projects" / _encode_cwd_for_runtime(cwd)
    _write_session(proj_dir / "s.jsonl",
                   ts="2026-05-15T10:00:00Z",
                   usage={"input_tokens": 10, "output_tokens": 20})

    first = get_window_burn(window_id="2026-05-15T10", cwd=cwd, home=home, use_cache=True)
    assert first.input_tokens == 10

    # Write more data — the cached call should NOT see it.
    _write_session(proj_dir / "s.jsonl",
                   ts="2026-05-15T10:00:01Z",
                   usage={"input_tokens": 5000, "output_tokens": 5000})

    second = get_window_burn(window_id="2026-05-15T10", cwd=cwd, home=home, use_cache=True)
    assert second.input_tokens == 10  # cached

    third = get_window_burn(window_id="2026-05-15T10", cwd=cwd, home=home, use_cache=False)
    assert third.input_tokens == 5010  # bypassed cache
