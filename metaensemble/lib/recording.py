"""Derive Run / Executor records from agent-runtime hook payloads.

The hook layer is the source of truth for Ledger writes. Hooks see what the
agent runtime gives them (tool_name, tool_input, tool_response) and derive
the typed Run/Executor records from those fields directly. The Coordinator
does not need to inject structured metadata into tool outputs because the
runtime provides no API for that; hooks self-derive instead.

See ARCHITECTURE.md §8 for the recording contract.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from metaensemble.lib.ids import derive_alias_prefix, make_alias, uuid7
from metaensemble.lib.ledger import Executor, Ledger


# --- Token estimation -----------------------------------------------------

# Anthropic published guidance places English text near 3.5–4 characters
# per token. We use 4 as a slightly-conservative integer divisor; the
# Ledger documents this number as an approximation so downstream queries
# treat token counts as estimates, not exact billing.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str | None) -> int:
    """Approximate token count from text length. Returns >= 0."""
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


# --- Prompt-marker parsing -----------------------------------------------

_MANIFEST_RE = re.compile(r"\[manifest:\s*(hm-[0-9a-fA-F\-]+)\]")
_CONTINUING_RE = re.compile(r"\[continuing:\s*([a-z0-9]+-[0-9a-f]{3})\]")
_TASK_RE = re.compile(r"\[task:\s*(task-[0-9a-fA-F\-]+)\]")
_FRESH_RE = re.compile(r"\[fresh\]")
_FANOUT_RE = re.compile(r"\[fanout:\s*(\d+)\]")
_CONSENSUS_RE = re.compile(r"\[consensus:\s*(\d+)\]")
_PROJECT_RE = re.compile(r"\[project:\s*([^\]\r\n]+)\]")


def parse_markers(prompt: str | None) -> dict[str, str]:
    """Extract Coordinator-supplied markers from a Task prompt.

    Recognized markers (all optional, all bracketed, case-insensitive
    keyword, lowercase-only values):
    - `[manifest: hm-<id>]` — Manifest the Coordinator composed for this Task
    - `[continuing: <alias>]` — Executor alias to reuse rather than create
    - `[task: task-<id>]` — explicit Task id (for grouping under a shared
      Task, e.g. fanout, peer review)
    - `[fresh]` — force-create a new Executor of this Role rather than
      reusing the most-recent active one (used by fan-out, consensus,
      and the reviewer leg of peer-review dispatches)
    - `[fanout: N]` — declared fanout size for a `--fanout N` dispatch.
      Surfaces the requested pattern to the PreToolUse guard so an
      invalid `N < 2` request blocks deterministically before any
      Executor work happens.
    - `[consensus: N]` — same shape and same guard for `--consensus N`.
    - `[project: /abs/path]` — explicit adopted project root for sessions
      whose cwd is not the project being dispatched against.

    Anything that does not match is ignored. The markers may appear anywhere
    in the prompt.
    """
    if not prompt:
        return {}
    markers: dict[str, str] = {}
    m = _MANIFEST_RE.search(prompt)
    if m:
        markers["manifest_id"] = m.group(1)
    c = _CONTINUING_RE.search(prompt)
    if c:
        markers["continuing_alias"] = c.group(1)
    t = _TASK_RE.search(prompt)
    if t:
        markers["task_id"] = t.group(1)
    if _FRESH_RE.search(prompt):
        markers["fresh"] = "1"
    f = _FANOUT_RE.search(prompt)
    if f:
        markers["fanout"] = f.group(1)
    cn = _CONSENSUS_RE.search(prompt)
    if cn:
        markers["consensus"] = cn.group(1)
    p = _PROJECT_RE.search(prompt)
    if p:
        markers["project_path"] = p.group(1).strip().strip("\"'")
    return markers


# --- Outcome classification ---------------------------------------------

_FAILURE_PATTERNS = (
    re.compile(r"(?im)^\s*(?:error|exception|failed|failure)\s*:"),
    re.compile(r"(?im)^\s*traceback\b"),
    re.compile(r"(?im)\bcommand failed\b"),
    re.compile(r"(?im)\bfailed with exit\b"),
    re.compile(r"(?im)\bexit code\s+(?!0\b)[1-9]\d*\b"),
)


def coerce_to_text(tool_response: Any) -> str:
    """Normalize an agent-runtime `tool_response` payload to a single string.

    The runtime can hand back any of: None, a string, a dict with one of several
    keys (`content`, `text`, `output`), or a dict whose `content` is a list of
    message segments each of which is either a string or a dict with a `text`
    field. This helper folds all of those into one string the heuristic
    classifiers and length-based token estimators can read.
    """
    if tool_response is None:
        return ""
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, dict):
        text = (
            tool_response.get("content")
            or tool_response.get("text")
            or tool_response.get("output")
            or ""
        )
        if isinstance(text, list):
            return " ".join(
                seg.get("text", "") if isinstance(seg, dict) else str(seg)
                for seg in text
            )
        return str(text)
    return str(tool_response)


def classify_outcome(tool_response: Any) -> str:
    """Classify a Task's outcome from its response.

    Returns one of: `ok`, `failed`, `partial`. The classifier is
    conservative: it returns `failed` only when there are explicit error
    indicators, `partial` when the response signals incomplete work,
    and `ok` otherwise. v0.1.0 is text-heuristic; richer signals can
    layer in later without changing the contract.
    """
    if tool_response is None:
        return "failed"
    if isinstance(tool_response, dict) and tool_response.get("is_error"):
        return "failed"

    text = coerce_to_text(tool_response)
    if any(pattern.search(text) for pattern in _FAILURE_PATTERNS):
        return "failed"
    lowered = text.lower()
    if "partial" in lowered and "complete" not in lowered:
        return "partial"
    return "ok"


def classify_failure_reason(tool_response: Any) -> str:
    """Categorize *why* a failed Run failed. Always returns a short label.

    Categories the Principal can act on:
    - `cost_gate_block` — PreToolUse hook blocked the dispatch
    - `manifest_invalid` — Manifest schema validation failed
    - `timeout` — Run exceeded its time budget
    - `exception` — Python traceback in the response
    - `other` — failed but did not match any known signal

    Callers gate on `classify_outcome(...) == "failed"` before invoking this
    function; the function itself does not know whether the Run succeeded,
    so it always returns a label rather than None. The hook in `post_task.py`
    decides whether to record the label by reading the outcome first.

    **Branch precedence.** When a response contains multiple signals
    (e.g., "cost gate blocked the dispatch — manifest validation failed"),
    the first matching category wins. Order is: `cost_gate_block`,
    `manifest_invalid`, `timeout`, `exception`, `other`. This order is
    chosen because the Principal's first useful question — "did the gate
    block me?" — should be answered before more specific diagnostics.
    """
    lowered = coerce_to_text(tool_response).lower()
    if "cost gate" in lowered or "blocked the dispatch" in lowered:
        return "cost_gate_block"
    if "manifest" in lowered and ("invalid" in lowered or "schema" in lowered or "validation" in lowered):
        return "manifest_invalid"
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    if "traceback" in lowered or "exception" in lowered:
        return "exception"
    return "other"


# --- Deliverable path extraction ----------------------------------------

_QUOTED_REPORTS_PATH_RE = re.compile(r"""['"`]([^'"`\r\n]*?reports[\\/][^'"`\r\n]*?\.md)['"`]""")
_ABSOLUTE_REPORTS_PATH_RE = re.compile(
    r"(?<![\w./\\-])((?:/|~/|~\\|\./|\.\./|[A-Za-z]:[\\/])[^:\r\n]*?reports[\\/][^\r\n]*?\.md)"
)
_RELATIVE_REPORTS_PATH_RE = re.compile(
    r"(?<![\w./\\-])((?:[\w.-]+[\\/])*reports[\\/][\w./\\-]+\.md)"
)


def extract_deliverable_path(tool_response: Any) -> str | None:
    """Pull a reports/.../*.md path out of the Task response if present.

    Returns the first matching path, or None. The Coordinator typically
    names the Deliverable file it wrote; the hook records the path so
    `deliverable_sync.py` and the digest queries can surface it.
    """
    text = coerce_to_text(tool_response)
    for pattern in (
        _QUOTED_REPORTS_PATH_RE,
        _ABSOLUTE_REPORTS_PATH_RE,
        _RELATIVE_REPORTS_PATH_RE,
    ):
        match = pattern.search(text)
        if match:
            return match.group(1).strip()
    return None


def build_deliverable_ref(
    tool_response: Any,
    *,
    deliverable_path: str | None = None,
    files_touched: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """Construct the structured `deliverable_ref` payload for a Run.

    Not every dispatch produces a long Markdown report under a reports directory.
    The Run row records a structured reference
    so short answers, hash-only diffs, and traditional Markdown
    deliverables all have a place to live without forcing the
    Coordinator to invent path-shaped artifacts.

    Return value (or None when no information is available):
      {"kind": "path",    "value": "<path>", "inferred": bool}
      {"kind": "summary", "value": "<truncated text>", "len": int}
      {"kind": "hash",    "value": "<sha256:hex>"}

    `kind="path"` wins when a Markdown report path is present. Otherwise
    we fall back to a truncated summary of the response text, and finally
    to a hash of the touched-file set when even a summary is empty (e.g.
    the deliverable was a pure code edit).
    """
    if deliverable_path:
        # Heuristic-extracted paths (via regex over the tool_response)
        # are flagged `inferred=True` so downstream tooling knows the
        # Coordinator did not emit a sentinel; sentinel-driven paths
        # would be flagged `inferred=False`. The sentinel contract is
        # documented in `metaensemble/commands/dispatch.md`; until the
        # Coordinator protocol enforces it, every recorded path is
        # treated as inferred to remain truthful.
        return {"kind": "path", "value": deliverable_path, "inferred": True}

    text = coerce_to_text(tool_response).strip()
    if text:
        truncated = text[:500]
        return {
            "kind": "summary",
            "value": truncated,
            "len": len(text),
        }

    if files_touched:
        import hashlib
        digest = hashlib.sha256(
            "\n".join(sorted(files_touched)).encode("utf-8")
        ).hexdigest()
        return {"kind": "hash", "value": f"sha256:{digest}"}

    return None


# --- Role / Executor ensure ---------------------------------------------

ROLE_SPEC_AUTO = "(auto-discovered)"


def ensure_role(ledger: Ledger, role_id: str, default_tier: str = "sonnet") -> None:
    """Register a Role row if one does not already exist. Idempotent.

    Auto-discovery: when a Task fires with a subagent_type that has no
    corresponding Role row (because the user's own agents are not in the
    curated set), the recording layer inserts a placeholder Role so the
    foreign key on `executors.role_id` is satisfied. The placeholder has
    `spec_path = '(auto-discovered)'` so the Registry can distinguish
    auto-discovered Roles from formally-installed ones.
    """
    ledger.ensure_role(
        role_id=role_id,
        version="auto",
        spec_path=ROLE_SPEC_AUTO,
        model_tier=default_tier,
        created_ts=datetime.now(timezone.utc).isoformat(),
    )


def ensure_task(ledger: Ledger, task_id: str, task_type: str, manifest_path: str | None) -> None:
    """Register a Task row if one does not already exist. Idempotent."""
    ledger.ensure_task(
        task_id=task_id,
        task_type=task_type,
        status="in_progress",
        manifest_path=manifest_path,
        created_ts=datetime.now(timezone.utc).isoformat(),
    )


@dataclass(frozen=True)
class EnsureExecutorResult:
    """Outcome of `ensure_executor` — the Executor row plus whether it was newly created."""

    executor: Executor
    created: bool


def ensure_executor(
    ledger: Ledger,
    *,
    role_id: str,
    continuing_alias: str | None = None,
    project_key: str | None = None,
    force_fresh: bool = False,
) -> EnsureExecutorResult:
    """Resolve or create the Executor for this Run.

    Resolution order:
    1. If `continuing_alias` is provided and matches an Executor, return it
       (the Coordinator asked to continue a specific thread).
    2. If `force_fresh` is set, skip the reuse path and always mint a new
       Executor. Used by fan-out, consensus, and peer-review dispatches
       so each parallel Executor is a distinct identity even though they
       share a Role.
    3. Otherwise, look for the most-recent active Executor of this Role
       in this project. If one exists and is `active`, reuse it — one
       persistent Executor per (Role, project) by default.
    4. Otherwise, create a new Executor with a fresh UUIDv7 and a Role-
       prefixed alias.
    """
    if continuing_alias:
        existing = ledger.get_executor_by_alias(continuing_alias)
        if existing is not None:
            return EnsureExecutorResult(executor=existing, created=False)

    if not force_fresh:
        # Default reuse: most recent active Executor for this Role.
        existing = ledger.get_active_executor_for_role(role_id)
        if existing is not None:
            return EnsureExecutorResult(executor=existing, created=False)

    # Create a fresh Executor.
    try:
        prefix = derive_alias_prefix(role_id)
    except ValueError:
        prefix = "exec"

    alias: str | None = None
    for _ in range(8):
        candidate = make_alias(prefix, uuid7())
        if ledger.get_executor_by_alias(candidate) is None:
            alias = candidate
            break
    if alias is None:
        # Final fallback: append timestamp to avoid an infinite collision loop.
        alias = f"{prefix}-{int(datetime.now(timezone.utc).timestamp()) % 0xFFF:03x}"

    now = datetime.now(timezone.utc).isoformat()
    executor = Executor(
        executor_id=str(uuid7()),
        alias=alias,
        role_id=role_id,
        parent_executor_id=None,
        created_ts=now,
        last_seen_ts=now,
        status="active",
    )
    ledger.upsert_executor(executor)
    return EnsureExecutorResult(executor=executor, created=True)


def project_key_for(cwd: str | None = None) -> str:
    """Stable project identity. Currently: absolute path to the cwd.

    The project key is reserved for future per-project Executor scoping;
    today it is the resolved cwd. v0.2 may move to a hashed value to keep
    aliases stable across renames.
    """
    if cwd:
        return str(Path(cwd).resolve())
    return os.environ.get("METAENSEMBLE_PROJECT_KEY", str(Path.cwd().resolve()))


# --- Manifest discovery -------------------------------------------------

MANIFEST_FILENAME_RE = re.compile(r"hm-[0-9a-fA-F\-]+\.ya?ml$")


def manifest_path_for(state_dir: Path, manifest_id: str) -> Path | None:
    """Resolve a manifest_id to its YAML file under `<project>/.metaensemble/manifests/`.

    Returns the path if a matching file exists; otherwise None. The
    caller can then pass the path to `load_manifest` for validation.
    """
    manifests_dir = state_dir.parent / "manifests"
    if not manifests_dir.exists():
        return None
    candidate = manifests_dir / f"{manifest_id}.yaml"
    if candidate.exists():
        return candidate
    # Tolerate .yml as well.
    candidate_yml = manifests_dir / f"{manifest_id}.yml"
    return candidate_yml if candidate_yml.exists() else None
