#!/usr/bin/env python3
"""File-tool hook — enforces project boundary and records file provenance.

This hook handles Write/Edit/MultiEdit/NotebookEdit. On PreToolUse it blocks
file writes outside the active MetaEnsemble project root. On PostToolUse it
records successful file-tool events so the enclosing Task Run can persist
`files_touched_json` and `tool_use_json` without relying on subagent
transcript discovery.
"""
from __future__ import annotations

import sys
import json
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from metaensemble.hooks._common import emit, log_error, read_input  # noqa: E402
from metaensemble.lib.file_events import (  # noqa: E402
    FileToolEvent,
    append_file_event,
    is_within,
    nearest_project_root,
    read_active_dispatch,
    read_active_dispatch_by_agent,
    read_active_dispatch_for_project,
    resolve_against_cwd,
    resolve_tool_paths,
)
from metaensemble.lib.overlaps import (  # noqa: E402
    protected_overlap_for_path,
    report_root_for_project,
)
from metaensemble.lib.recording import coerce_to_text  # noqa: E402
from metaensemble.lib.runtime_state import _encode_cwd_for_runtime  # noqa: E402


def _payload_cwd(payload: dict) -> Path:
    raw = payload.get("cwd")
    if isinstance(raw, str) and raw:
        return Path(raw)
    return Path.cwd()


def _active_context(session_id: str, cwd: Path, agent_id: str | None = None):
    # Background path: authorize by the per-dispatch agentId first. This is the
    # only correlation key that survives same-session fan-out.
    if agent_id:
        active = read_active_dispatch_by_agent(agent_id)
        if active is not None:
            return active, Path(active.project_root), Path(active.state_dir)
    # Legacy session/project fallback — synchronous-runtime compatibility only.
    active = read_active_dispatch(session_id) if session_id else None
    if active is not None:
        return active, Path(active.project_root), Path(active.state_dir)
    root = nearest_project_root(cwd)
    if root is None:
        return None, None, None
    active = read_active_dispatch_for_project(root)
    if active is None:
        return None, None, None
    return active, Path(active.project_root), Path(active.state_dir)


def _tool_failed(payload: dict) -> bool:
    response = payload.get("tool_response") or payload.get("tool_output")
    if isinstance(response, dict) and response.get("is_error"):
        return True
    text = coerce_to_text(response).lower()
    return "error:" in text or "failed" in text


def _coerce_content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                raw = item.get("text") or item.get("content") or ""
                parts.append(str(raw))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content or "")


def _recent_user_texts(transcript_path: str | None) -> tuple[str, ...]:
    if not transcript_path:
        return ()
    path = Path(transcript_path)
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return ()
    out: list[str] = []
    for line in reversed(lines[-200:]):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "user":
            continue
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        out.append(_coerce_content_text(message.get("content")))
        if len(out) >= 20:
            break
    return tuple(out)


def _looks_like_dispatch_command(payload: dict) -> bool:
    for text in _recent_user_texts(payload.get("transcript_path")):
        if "<command-name>/dispatch</command-name>" in text:
            return True
        if (
            "ARGUMENTS:" in text
            and "When the Principal invokes `/dispatch" in text
            and "Coordinator protocol" in text
        ):
            return True
    return False


def _is_allowed_coordinator_write(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    parts = rel.parts
    if len(parts) >= 3 and parts[:2] == (".metaensemble", "manifests"):
        return True
    report_root = root / report_root_for_project(root)
    return path.suffix.lower() == ".md" and is_within(
        path.resolve(strict=False),
        report_root.resolve(strict=False),
    )


def _claude_project_state_dirs(cwd: Path, root: Path) -> tuple[Path, ...]:
    """Claude Code runtime state dirs that belong to this active project.

    The boundary guard should not block Claude Code from updating its own
    per-project transcript/memory state. Scope this carve-out to the current
    cwd and nearest MetaEnsemble root rather than all of `~/.claude/projects`.
    """
    base = Path.home() / ".claude" / "projects"
    dirs = [
        base / _encode_cwd_for_runtime(cwd),
        base / _encode_cwd_for_runtime(root),
    ]
    out: list[Path] = []
    seen: set[str] = set()
    for d in dirs:
        resolved = d.resolve(strict=False)
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            out.append(resolved)
    return tuple(out)


def _is_allowed_claude_project_state_write(path: Path, cwd: Path, root: Path) -> bool:
    resolved = path.resolve(strict=False)
    return any(is_within(resolved, d) for d in _claude_project_state_dirs(cwd, root))


def _emit_boundary_block(raw: str, resolved: Path, root: Path) -> int:
    emit({
        "continue": False,
        "stopReason": (
            "MetaEnsemble project boundary guard blocked a file edit outside "
            f"the active project root.\n\nRequested path: {raw}\n"
            f"Resolved path: {resolved}\nProject root: {root}\n\n"
            "Run MetaEnsemble from the parent project root, or dispatch a task "
            "whose file paths stay inside the installed project."
        ),
    })
    return 2


def _emit_overlap_ownership_block(path: Path, root: Path) -> int:
    surface = protected_overlap_for_path(root, path)
    metaensemble_surface = surface.metaensemble_surface if surface else "MetaEnsemble Ledger"
    emit({
        "continue": False,
        "stopReason": (
            "MetaEnsemble overlap ownership guard blocked a file edit to a "
            "project-maintained work-record surface assigned to MetaEnsemble.\n\n"
            f"Requested path: {path}\n"
            f"Project root: {root}\n"
            f"Overlap category: {surface.category if surface else 'unknown'}\n"
            f"Project surface: {surface.project_surface if surface else path}\n"
            f"MetaEnsemble surface: {metaensemble_surface}\n\n"
            "Change `.metaensemble/install-decisions.yaml` to "
            "`action: project_owned` or `action: dual` for this overlap if "
            "the manual document should still be maintained."
        ),
    })
    return 2


def _emit_direct_dispatch_edit_block(tool_name: str, root: Path) -> int:
    emit({
        "continue": False,
        "stopReason": (
            "MetaEnsemble dispatch protocol blocked a direct file edit. "
            f"`{tool_name}` was invoked while handling `/dispatch`, but no "
            "active Task/Agent Run was present.\n\n"
            f"Project root: {root}\n\n"
            "The Coordinator must spawn an Executor via Task/Agent so the "
            "Run is recorded in the Ledger with files touched and tool use."
        ),
    })
    return 2


def run() -> int:
    payload = read_input()
    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    paths = resolve_tool_paths(tool_name, tool_input)
    if not paths:
        emit({"continue": True})
        return 0

    session_id = payload.get("session_id") or ""
    agent_id = payload.get("agent_id")
    hook_event = payload.get("hook_event_name") or ""
    cwd = _payload_cwd(payload)
    installed_root = nearest_project_root(cwd)
    active, project_root, state_dir = _active_context(session_id, cwd, agent_id)
    if project_root is None or state_dir is None:
        if (
            hook_event == "PreToolUse"
            and installed_root is not None
            and _looks_like_dispatch_command(payload)
        ):
            resolved = [
                resolve_against_cwd(raw, cwd)
                for raw in paths
            ]
            if all(
                _is_allowed_coordinator_write(p, installed_root)
                or _is_allowed_claude_project_state_write(p, cwd, installed_root)
                for p in resolved
            ):
                emit({"continue": True})
                return 0
            return _emit_direct_dispatch_edit_block(tool_name, installed_root)
        emit({"continue": True})
        return 0

    resolved_paths: list[tuple[str, Path]] = []
    for raw in paths:
        resolved = resolve_against_cwd(raw, cwd)
        if not is_within(resolved, project_root):
            if _is_allowed_claude_project_state_write(resolved, cwd, project_root):
                continue
            return _emit_boundary_block(raw, resolved, project_root)
        if (
            hook_event == "PreToolUse"
            and protected_overlap_for_path(project_root, resolved) is not None
        ):
            return _emit_overlap_ownership_block(resolved, project_root)
        resolved_paths.append((raw, resolved))

    if hook_event == "PostToolUse" and not _tool_failed(payload):
        for _raw, resolved in resolved_paths:
            try:
                rel = str(resolved.relative_to(project_root.resolve(strict=False)))
            except ValueError:
                rel = None
            try:
                append_file_event(
                    state_dir,
                    FileToolEvent(
                        ts=datetime.now(timezone.utc).isoformat(),
                        session_id=session_id,
                        run_id=active.run_id if active else None,
                        tool_name=tool_name,
                        path=str(resolved),
                        rel_path=rel,
                        cwd=str(cwd),
                    ),
                )
            except Exception as exc:
                log_error("file-event-record-failed", str(exc), {
                    "tool_name": tool_name,
                    "path": str(resolved),
                    "session_id": session_id,
                })

    emit({"continue": True})
    return 0


if __name__ == "__main__":
    sys.exit(run())
