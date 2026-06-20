"""Read the agent-runtime's JSONL transcript to harvest Run provenance.

Hook payloads expose `tool_name`, `tool_input`, `tool_response`, and
`session_id`. They do **not** expose the model the subagent used or the
list of tool calls the subagent issued. That information lives in the
runtime's session transcript JSONL — Claude Code writes it under
`~/.claude/projects/<project-slug>/<session_id>.jsonl`, one record per
message, with `tool_use` content blocks for every tool invocation and
a `model` field on assistant messages.

This module provides a thin, defensive reader for that file. It is
defensive because:

  - The transcript path is opportunistic — when absent (test fixture,
    runtime version mismatch, file rotated), the extractor returns
    empty results rather than raising.
  - The transcript format is not contractual. The reader tolerates
    extra keys, missing fields, and malformed lines. A single bad
    line never breaks the harvest.
  - The reader returns structured results rather than mutating the
    Ledger directly so the post-task hook can decide how to record
    the harvest under its quality budget.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
import unicodedata

from metaensemble.lib.runtime_payload import normalize_model_identity
from metaensemble.lib.runtime_state import claude_project_dir_for_cwd


# Tools that signal a file was written or modified. Used to derive the
# `files_touched_json` set from a subagent's tool-use history.
_FILE_WRITE_TOOLS = frozenset({"Write", "Edit", "NotebookEdit", "MultiEdit"})

# Tool-input keys that name a file path, in priority order. The first
# present non-empty value wins.
_FILE_PATH_KEYS = ("file_path", "notebook_path", "path", "filename")


@dataclass(frozen=True)
class ToolUseStat:
    """Per-tool aggregate from a transcript walk."""

    name: str
    count: int
    total_input_tokens: int = 0


@dataclass(frozen=True)
class TranscriptHarvest:
    """Everything we can recover about a Run from its transcript slice.

    `model_observations` is a list rather than a single value because a
    subagent dispatch can produce multiple assistant messages with
    potentially different model fields (tier fallback, retry); the
    post-task hook picks the dominant value for recording.
    """

    files_touched: tuple[str, ...] = ()
    tool_use: tuple[ToolUseStat, ...] = ()
    model_observations: tuple[str, ...] = ()
    cache_read_tokens: int = 0
    cache_create_tokens: int = 0
    raw_message_count: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


def _iter_transcript_lines(path: Path) -> Iterable[dict[str, Any]]:
    """Yield each non-empty JSONL line as a dict. Malformed lines skipped."""
    if not path.exists() or not path.is_file():
        return
    try:
        with path.open() as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _content_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize the various ways the runtime represents message content.

    Claude Code's transcript wraps messages in different shapes across
    versions. We accept both `{"message": {"content": [...]}}` and
    `{"content": [...]}` at the top level, and treat a plain string
    content as a single text block.
    """
    msg = message.get("message") if isinstance(message.get("message"), dict) else message
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def _extract_file_path(tool_input: dict[str, Any]) -> str | None:
    for key in _FILE_PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def canonical_prompt_for_hash(prompt: Any) -> str | None:
    """Return the canonical prompt text used for dispatch correlation.

    Runtime payloads and transcript records can differ in harmless ways:
    Unicode composition, CRLF vs LF line endings, and trailing whitespace
    added by prompt assembly. Those differences must not produce a different
    dispatch fingerprint.
    """
    if not isinstance(prompt, str) or not prompt:
        return None
    text = prompt.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    text = unicodedata.normalize("NFC", text)
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def prompt_fingerprint(prompt: Any) -> str | None:
    """Return a stable fingerprint for an Agent/Task prompt."""
    canonical = canonical_prompt_for_hash(prompt)
    if canonical is None:
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_iso_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _matches_dispatch_tool_use(
    block: dict[str, Any],
    *,
    task_id: str | None,
    role_id: str | None,
    prompt_sha256: str | None = None,
) -> bool:
    """Return True when a tool-use block is the Agent call for this Run."""
    if block.get("type") != "tool_use":
        return False
    if block.get("name") not in {"Task", "Agent"}:
        return False
    tool_input = block.get("input")
    if not isinstance(tool_input, dict):
        return False
    if role_id and tool_input.get("subagent_type") != role_id:
        return False
    prompt = tool_input.get("prompt")
    task_matches = bool(task_id and isinstance(prompt, str) and task_id in prompt)
    prompt_matches = bool(
        prompt_sha256
        and prompt_fingerprint(prompt) == prompt_sha256
    )
    return task_matches or prompt_matches


def _record_in_window(
    record: dict[str, Any],
    *,
    after_ts: str | None,
    before_ts: str | None,
) -> bool:
    ts = record.get("timestamp")
    in_window = True
    if isinstance(ts, str):
        if after_ts and ts < after_ts:
            in_window = False
        if before_ts and ts > before_ts:
            in_window = False
    return in_window


def _select_dispatch_tool_use_indices(
    records: list[dict[str, Any]],
    *,
    after_ts: str | None,
    before_ts: str | None,
    dispatch_task_id: str | None,
    dispatch_role_id: str | None,
    dispatch_prompt_sha256: str | None,
    dispatch_started_ts: str | None,
    dispatch_time_tolerance_seconds: int,
) -> set[int]:
    """Select the pre-window Agent/Task record for this dispatch.

    The prompt hash alone is not unique: two identical prompts can be sent in
    the same session. When a dispatch start timestamp is available, choose the
    nearest matching pre-window tool-use record inside a small time window.
    """
    if not (dispatch_task_id or dispatch_prompt_sha256):
        return set()

    candidates: list[tuple[float, int]] = []
    fallback: set[int] = set()
    started = _parse_iso_timestamp(dispatch_started_ts)
    for idx, record in enumerate(records):
        if _record_in_window(record, after_ts=after_ts, before_ts=before_ts):
            continue
        ts = record.get("timestamp")
        if after_ts and isinstance(ts, str) and ts >= after_ts:
            continue
        if not any(
            _matches_dispatch_tool_use(
                block,
                task_id=dispatch_task_id,
                role_id=dispatch_role_id,
                prompt_sha256=dispatch_prompt_sha256,
            )
            for block in _content_blocks(record)
        ):
            continue
        fallback.add(idx)
        if started is None:
            continue
        observed = _parse_iso_timestamp(ts)
        if observed is None:
            continue
        distance = abs((started - observed).total_seconds())
        if distance <= dispatch_time_tolerance_seconds:
            candidates.append((distance, idx))

    if started is None:
        return fallback
    if not candidates:
        return set()
    _, chosen_idx = min(candidates, key=lambda item: (item[0], -item[1]))
    return {chosen_idx}


def transcript_path_for_session(
    session_id: str | None,
    *,
    cwd: Path | None = None,
    home: Path | None = None,
) -> Path | None:
    """Resolve Claude Code's transcript path from session id and cwd.

    Some hook payloads include `transcript_path`; `claude -p` did not in
    the live dispatch validation. The runtime still writes the transcript
    under `~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`, so the
    hook can recover provenance without relying on a payload field.
    """
    if not session_id:
        return None
    project_dir = claude_project_dir_for_cwd(cwd=cwd, home=home)
    if project_dir is None:
        return None
    direct = project_dir / f"{session_id}.jsonl"
    return direct if direct.is_file() else None


def walk_transcript(
    path: Path,
    *,
    after_ts: str | None = None,
    before_ts: str | None = None,
    dispatch_task_id: str | None = None,
    dispatch_role_id: str | None = None,
    dispatch_prompt_sha256: str | None = None,
    dispatch_started_ts: str | None = None,
    dispatch_time_tolerance_seconds: int = 60,
) -> TranscriptHarvest:
    """Walk a session transcript and harvest provenance for one Run.

    `after_ts` and `before_ts` bound the message window — the post-task
    hook passes the pending sidecar's `started_ts` and the moment the
    PostToolUse fired so the walk only sees messages that belong to
    *this* subagent dispatch. Timestamps are ISO-8601 strings; the
    comparison is lexicographic, which is correct for ISO-8601.

    `dispatch_task_id`/`dispatch_role_id`/`dispatch_prompt_sha256` allow one
    pre-window exception: Claude Code records the assistant `Agent` tool-use
    message just before PreToolUse runs, so its model field can precede
    `started_ts`. When that tool-use block matches this Run, we include its
    model observation but still exclude its usage/tool statistics from the Run
    window.

    The harvest is "best effort": malformed lines, missing fields, and
    unparseable timestamps drop into the `errors` list rather than
    bubbling up. The caller writes whatever non-empty fields the
    harvest produced into the Ledger.
    """
    files: set[str] = set()
    tool_counter: Counter[str] = Counter()
    tool_input_tokens: Counter[str] = Counter()
    model_observations: list[str] = []
    cache_read = 0
    cache_create = 0
    raw_count = 0
    errors: list[str] = []

    records = list(_iter_transcript_lines(path))
    dispatch_indices = _select_dispatch_tool_use_indices(
        records,
        after_ts=after_ts,
        before_ts=before_ts,
        dispatch_task_id=dispatch_task_id,
        dispatch_role_id=dispatch_role_id,
        dispatch_prompt_sha256=dispatch_prompt_sha256,
        dispatch_started_ts=dispatch_started_ts,
        dispatch_time_tolerance_seconds=dispatch_time_tolerance_seconds,
    )

    # Map tool_use_id -> is_error for every tool_result in the transcript. A
    # file write becomes provenance only when its matching result exists and did
    # NOT error: a denied/failed Write (is_error=true) or a write with no result
    # must never be recorded as a touched file (it would become a phantom
    # deliverable). Built over all records so a result just outside the harvest
    # window still resolves its write.
    tool_results: dict[str, bool] = {}
    for record in records:
        for block in _content_blocks(record):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tuid = block.get("tool_use_id")
                if isinstance(tuid, str) and tuid:
                    tool_results[tuid] = bool(block.get("is_error"))

    for idx, record in enumerate(records):
        raw_count += 1
        in_window = _record_in_window(record, after_ts=after_ts, before_ts=before_ts)

        blocks = _content_blocks(record)
        matching_dispatch = idx in dispatch_indices
        if not in_window and not matching_dispatch:
            continue

        # Assistant messages carry the model and may include content
        # blocks of type `tool_use` plus usage statistics.
        msg = record.get("message") if isinstance(record.get("message"), dict) else None
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or record.get("role")
        if role == "assistant":
            model = normalize_model_identity(msg.get("model") or record.get("model"))
            if model:
                model_observations.append(model)
            usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
            if usage and in_window:
                try:
                    cache_read += int(usage.get("cache_read_input_tokens") or 0)
                    cache_create += int(usage.get("cache_creation_input_tokens") or 0)
                except (TypeError, ValueError):
                    errors.append("usage tokens unparseable")

        if not in_window:
            continue

        for block in blocks:
            if block.get("type") == "tool_use":
                tool_name = block.get("name")
                if not isinstance(tool_name, str) or not tool_name:
                    continue
                tool_counter[tool_name] += 1
                tool_input = block.get("input")
                if isinstance(tool_input, dict):
                    if tool_name in _FILE_WRITE_TOOLS:
                        fp = _extract_file_path(tool_input)
                        tuid = block.get("id")
                        # Count only writes whose tool_result confirms success.
                        if (
                            fp
                            and isinstance(tuid, str)
                            and tuid in tool_results
                            and not tool_results[tuid]
                        ):
                            files.add(fp)
                    # Rough input-token estimate for the tool call body —
                    # length of the serialized input divided by the
                    # 4-chars-per-token constant used elsewhere in the
                    # codebase. Cheap and consistent.
                    try:
                        serialized = json.dumps(tool_input)
                        tool_input_tokens[tool_name] += max(1, len(serialized) // 4)
                    except (TypeError, ValueError):
                        pass

    tool_stats = tuple(
        ToolUseStat(
            name=name,
            count=count,
            total_input_tokens=tool_input_tokens[name],
        )
        for name, count in sorted(tool_counter.items(), key=lambda kv: (-kv[1], kv[0]))
    )

    return TranscriptHarvest(
        files_touched=tuple(sorted(files)),
        tool_use=tool_stats,
        model_observations=tuple(model_observations),
        cache_read_tokens=cache_read,
        cache_create_tokens=cache_create,
        raw_message_count=raw_count,
        errors=tuple(errors),
    )


def dominant_model(harvest: TranscriptHarvest) -> str | None:
    """Pick the most-frequent model from the observations, or None.

    Used by the post-task hook to set `runs.model` to the runtime-observed
    model rather than the manifest-declared tier when a transcript is
    available. Ties are broken by first-seen.
    """
    if not harvest.model_observations:
        return None
    counter = Counter(harvest.model_observations)
    # Counter.most_common(1)[0][0] picks the highest count; ties broken
    # by insertion order, which matches Counter semantics on CPython.
    return counter.most_common(1)[0][0]
