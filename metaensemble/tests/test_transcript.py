"""Tests for `metaensemble.lib.transcript`.

Synthetic JSONL fixtures stand in for runtime transcripts. The format
follows what Claude Code writes under `~/.claude/projects/`, with
defensive coverage for shape variations (malformed lines, missing
fields, top-level `message` wrapper vs. flat record).
"""
from __future__ import annotations

import json
from pathlib import Path

from metaensemble.lib.transcript import (
    TranscriptHarvest,
    canonical_prompt_for_hash,
    dominant_model,
    prompt_fingerprint,
    walk_transcript,
)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_walk_missing_file_returns_empty_harvest(tmp_path):
    harvest = walk_transcript(tmp_path / "absent.jsonl")
    assert harvest == TranscriptHarvest()


def test_walk_extracts_files_touched(tmp_path):
    """Write/Edit/NotebookEdit calls populate files_touched."""
    path = tmp_path / "sess.jsonl"
    _write_jsonl(path, [
        {
            "timestamp": "2026-05-19T10:00:00Z",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [
                    {"type": "tool_use", "name": "Write",
                     "input": {"file_path": "/src/foo.py", "content": "x"}},
                    {"type": "tool_use", "name": "Edit",
                     "input": {"file_path": "/src/bar.py",
                               "old_string": "a", "new_string": "b"}},
                ],
            },
        },
    ])
    harvest = walk_transcript(path)
    assert harvest.files_touched == ("/src/bar.py", "/src/foo.py")
    assert harvest.raw_message_count == 1


def test_walk_aggregates_tool_use_with_counts(tmp_path):
    path = tmp_path / "sess.jsonl"
    _write_jsonl(path, [
        {
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Read",
                     "input": {"file_path": "/src/a.py"}},
                    {"type": "tool_use", "name": "Read",
                     "input": {"file_path": "/src/b.py"}},
                    {"type": "tool_use", "name": "Write",
                     "input": {"file_path": "/src/a.py", "content": "x"}},
                ],
            },
        },
    ])
    harvest = walk_transcript(path)
    names = {t.name: t.count for t in harvest.tool_use}
    assert names == {"Read": 2, "Write": 1}
    # Total-input-tokens estimate is non-zero for tools we observed.
    read_stat = next(t for t in harvest.tool_use if t.name == "Read")
    assert read_stat.total_input_tokens > 0


def test_walk_observes_model_per_assistant_message(tmp_path):
    path = tmp_path / "sess.jsonl"
    _write_jsonl(path, [
        {"message": {"role": "assistant", "model": "claude-sonnet-4-6",
                      "content": [{"type": "text", "text": "hi"}]}},
        {"message": {"role": "user", "content": "hello"}},
        {"message": {"role": "assistant", "model": "claude-sonnet-4-6",
                      "content": [{"type": "text", "text": "yes"}]}},
        {"message": {"role": "assistant", "model": "claude-haiku-4-5",
                      "content": [{"type": "text", "text": "fallback"}]}},
    ])
    harvest = walk_transcript(path)
    assert harvest.model_observations == (
        "claude-sonnet-4-6",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    )
    assert dominant_model(harvest) == "claude-sonnet-4-6"


def test_walk_normalizes_structured_model_identity(tmp_path):
    path = tmp_path / "sess.jsonl"
    _write_jsonl(path, [
        {"message": {"role": "assistant",
                      "model": {"id": "claude-opus-4-7", "display_name": "Opus 4.7"},
                      "content": [{"type": "text", "text": "hi"}]}},
    ])

    harvest = walk_transcript(path)

    assert harvest.model_observations == ("claude-opus-4-7",)
    assert dominant_model(harvest) == "claude-opus-4-7"


def test_walk_dominant_model_returns_none_when_no_observations(tmp_path):
    path = tmp_path / "sess.jsonl"
    _write_jsonl(path, [
        {"message": {"role": "user", "content": "hi"}},
    ])
    harvest = walk_transcript(path)
    assert harvest.model_observations == ()
    assert dominant_model(harvest) is None


def test_walk_accumulates_cache_tokens_from_usage(tmp_path):
    path = tmp_path / "sess.jsonl"
    _write_jsonl(path, [
        {"message": {"role": "assistant", "model": "claude-sonnet-4-6",
                      "content": [{"type": "text", "text": "x"}],
                      "usage": {"cache_read_input_tokens": 1000,
                                "cache_creation_input_tokens": 200}}},
        {"message": {"role": "assistant", "model": "claude-sonnet-4-6",
                      "content": [{"type": "text", "text": "y"}],
                      "usage": {"cache_read_input_tokens": 500,
                                "cache_creation_input_tokens": 100}}},
    ])
    harvest = walk_transcript(path)
    assert harvest.cache_read_tokens == 1500
    assert harvest.cache_create_tokens == 300


def test_walk_respects_timestamp_window(tmp_path):
    """Only messages between after_ts and before_ts contribute to the harvest."""
    path = tmp_path / "sess.jsonl"
    _write_jsonl(path, [
        {"timestamp": "2026-05-19T09:00:00Z",
         "message": {"role": "assistant", "model": "older",
                      "content": [{"type": "tool_use", "name": "Read",
                                   "input": {"file_path": "/before.py"}}]}},
        {"timestamp": "2026-05-19T10:00:00Z",
         "message": {"role": "assistant", "model": "current",
                      "content": [{"type": "tool_use", "name": "Write",
                                   "input": {"file_path": "/within.py"}}]}},
        {"timestamp": "2026-05-19T11:00:00Z",
         "message": {"role": "assistant", "model": "later",
                      "content": [{"type": "tool_use", "name": "Edit",
                                   "input": {"file_path": "/after.py"}}]}},
    ])
    harvest = walk_transcript(
        path,
        after_ts="2026-05-19T09:30:00Z",
        before_ts="2026-05-19T10:30:00Z",
    )
    assert harvest.files_touched == ("/within.py",)
    assert harvest.model_observations == ("current",)


def test_walk_includes_matching_dispatch_model_before_window(tmp_path):
    """The Agent tool-use line can precede PreToolUse's pending timestamp."""
    path = tmp_path / "sess.jsonl"
    _write_jsonl(path, [
        {"timestamp": "2026-05-19T09:00:00Z",
         "message": {"role": "assistant", "model": "old-unrelated",
                      "content": [{"type": "tool_use", "name": "Agent",
                                   "input": {"subagent_type": "docs",
                                             "prompt": "[task: other] read"}}]}},
        {"timestamp": "2026-05-19T09:59:59Z",
         "message": {"role": "assistant", "model": "claude-opus-4-7",
                      "usage": {"cache_read_input_tokens": 999},
                      "content": [{"type": "tool_use", "name": "Agent",
                                   "input": {"subagent_type": "docs",
                                             "prompt": "[task: task-live] read"}}]}},
        {"timestamp": "2026-05-19T10:00:01Z",
         "message": {"role": "assistant", "model": "within",
                      "usage": {"cache_read_input_tokens": 10},
                      "content": [{"type": "tool_use", "name": "Read",
                                   "input": {"file_path": "/within.md"}}]}},
    ])

    harvest = walk_transcript(
        path,
        after_ts="2026-05-19T10:00:00Z",
        before_ts="2026-05-19T10:01:00Z",
        dispatch_task_id="task-live",
        dispatch_role_id="docs",
    )

    assert harvest.model_observations == ("claude-opus-4-7", "within")
    assert harvest.cache_read_tokens == 10
    assert {t.name: t.count for t in harvest.tool_use} == {"Read": 1}


def test_walk_includes_dispatch_model_by_prompt_fingerprint(tmp_path):
    """Live dispatches may omit the generated task marker from Agent prompts."""
    path = tmp_path / "sess.jsonl"
    prompt = "read README.md and summarize"
    _write_jsonl(path, [
        {"timestamp": "2026-05-19T09:59:58Z",
         "message": {"role": "assistant", "model": "wrong",
                      "content": [{"type": "tool_use", "name": "Agent",
                                   "input": {"subagent_type": "docs",
                                             "prompt": "different prompt"}}]}},
        {"timestamp": "2026-05-19T09:59:59Z",
         "message": {"role": "assistant", "model": "claude-opus-4-7",
                      "usage": {"cache_read_input_tokens": 999},
                      "content": [{"type": "tool_use", "name": "Agent",
                                   "input": {"subagent_type": "docs",
                                             "prompt": prompt}}]}},
        {"timestamp": "2026-05-19T10:00:01Z",
         "message": {"role": "assistant", "model": "within",
                      "usage": {"cache_read_input_tokens": 10},
                      "content": [{"type": "tool_use", "name": "Read",
                                   "input": {"file_path": "/within.md"}}]}},
    ])

    harvest = walk_transcript(
        path,
        after_ts="2026-05-19T10:00:00Z",
        before_ts="2026-05-19T10:01:00Z",
        dispatch_task_id="task-generated-but-not-in-prompt",
        dispatch_role_id="docs",
        dispatch_prompt_sha256=prompt_fingerprint(prompt),
    )

    assert harvest.model_observations == ("claude-opus-4-7", "within")
    assert harvest.cache_read_tokens == 10
    assert {t.name: t.count for t in harvest.tool_use} == {"Read": 1}


def test_prompt_fingerprint_uses_canonical_prompt_text():
    composed = "Review Caf\u00e9\n"
    decomposed = "Review Cafe\u0301  \r\n\r\n"

    assert canonical_prompt_for_hash(decomposed) == "Review Caf\u00e9"
    assert prompt_fingerprint(composed) == prompt_fingerprint(decomposed)


def test_walk_uses_canonical_prompt_fingerprint(tmp_path):
    path = tmp_path / "sess.jsonl"
    pretool_prompt = "Read Caf\u00e9 notes."
    transcript_prompt = "Read Cafe\u0301 notes.  \r\n"
    _write_jsonl(path, [
        {"timestamp": "2026-05-19T09:59:59Z",
         "message": {"role": "assistant", "model": "claude-opus-4-7",
                      "content": [{"type": "tool_use", "name": "Agent",
                                   "input": {"subagent_type": "docs",
                                             "prompt": transcript_prompt}}]}},
        {"timestamp": "2026-05-19T10:00:01Z",
         "message": {"role": "assistant", "model": "within",
                      "content": [{"type": "tool_use", "name": "Read",
                                   "input": {"file_path": "/within.md"}}]}},
    ])

    harvest = walk_transcript(
        path,
        after_ts="2026-05-19T10:00:00Z",
        before_ts="2026-05-19T10:01:00Z",
        dispatch_role_id="docs",
        dispatch_prompt_sha256=prompt_fingerprint(pretool_prompt),
        dispatch_started_ts="2026-05-19T10:00:00Z",
    )

    assert harvest.model_observations == ("claude-opus-4-7", "within")


def test_walk_disambiguates_identical_prompts_by_dispatch_time(tmp_path):
    path = tmp_path / "sess.jsonl"
    prompt = "read README.md and summarize"
    _write_jsonl(path, [
        {"timestamp": "2026-05-19T09:58:59Z",
         "message": {"role": "assistant", "model": "first-dispatch",
                      "content": [{"type": "tool_use", "name": "Agent",
                                   "input": {"subagent_type": "docs",
                                             "prompt": prompt}}]}},
        {"timestamp": "2026-05-19T09:59:59Z",
         "message": {"role": "assistant", "model": "second-dispatch",
                      "content": [{"type": "tool_use", "name": "Agent",
                                   "input": {"subagent_type": "docs",
                                             "prompt": prompt}}]}},
        {"timestamp": "2026-05-19T10:00:01Z",
         "message": {"role": "assistant", "model": "within",
                      "content": [{"type": "tool_use", "name": "Read",
                                   "input": {"file_path": "/within.md"}}]}},
    ])

    harvest = walk_transcript(
        path,
        after_ts="2026-05-19T10:00:00Z",
        before_ts="2026-05-19T10:01:00Z",
        dispatch_role_id="docs",
        dispatch_prompt_sha256=prompt_fingerprint(prompt),
        dispatch_started_ts="2026-05-19T10:00:00Z",
    )

    assert harvest.model_observations == ("second-dispatch", "within")


def test_walk_tolerates_malformed_lines(tmp_path):
    path = tmp_path / "sess.jsonl"
    path.write_text(
        json.dumps({"message": {"role": "assistant", "model": "claude-sonnet-4-6",
                                  "content": [{"type": "text", "text": "ok"}]}}) + "\n"
        "{not valid json\n"
        "\n"
        + json.dumps({"message": {"role": "assistant", "model": "claude-haiku-4-5",
                                  "content": [{"type": "text", "text": "ok"}]}}) + "\n"
    )
    harvest = walk_transcript(path)
    assert harvest.model_observations == ("claude-sonnet-4-6", "claude-haiku-4-5")


def test_walk_handles_flat_content_string(tmp_path):
    """Some runtime versions deliver `content` as a string. Treat it as text."""
    path = tmp_path / "sess.jsonl"
    _write_jsonl(path, [
        {"message": {"role": "assistant", "model": "m", "content": "plain text response"}},
    ])
    harvest = walk_transcript(path)
    # No tool calls in plain text; the walker just notes the model.
    assert harvest.model_observations == ("m",)
    assert harvest.tool_use == ()
    assert harvest.files_touched == ()
