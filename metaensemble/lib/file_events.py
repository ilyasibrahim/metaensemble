"""File-tool provenance for MetaEnsemble Runs.

Claude Code's Task/Agent PostToolUse payload does not reliably include the
subagent's internal tool history. MetaEnsemble therefore records file-tool
events directly from Write/Edit/MultiEdit/NotebookEdit hooks and merges them
into the Run row when the enclosing Task completes.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FILE_TOOL_NAMES = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})


@dataclass(frozen=True)
class ActiveDispatch:
    session_id: str
    run_id: str
    project_root: str
    state_dir: str
    started_ts: str


@dataclass(frozen=True)
class FileToolEvent:
    ts: str
    session_id: str
    run_id: str | None
    tool_name: str
    path: str
    rel_path: str | None
    cwd: str


def user_active_dispatch_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".metaensemble" / "state" / "active-dispatches"


def _safe_name(value: str) -> str:
    return value.replace("/", "_")


def active_dispatch_path(session_id: str, *, home: Path | None = None) -> Path:
    return user_active_dispatch_dir(home) / f"{_safe_name(session_id)}.json"


def write_active_dispatch(active: ActiveDispatch, *, home: Path | None = None) -> Path:
    target = active_dispatch_path(active.session_id, home=home)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(asdict(active), indent=2))
    return target


def read_active_dispatch(session_id: str, *, home: Path | None = None) -> ActiveDispatch | None:
    path = active_dispatch_path(session_id, home=home)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return ActiveDispatch(
            session_id=data["session_id"],
            run_id=data["run_id"],
            project_root=data["project_root"],
            state_dir=data["state_dir"],
            started_ts=data["started_ts"],
        )
    except (KeyError, TypeError):
        return None


def read_active_dispatch_for_project(
    project_root: Path,
    *,
    home: Path | None = None,
) -> ActiveDispatch | None:
    """Find an active parent dispatch for a child-session file-tool hook."""
    root = project_root.resolve(strict=False)
    active_dir = user_active_dispatch_dir(home)
    if not active_dir.exists():
        return None
    newest: ActiveDispatch | None = None
    newest_ts = ""
    for path in active_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        try:
            active = ActiveDispatch(
                session_id=data["session_id"],
                run_id=data["run_id"],
                project_root=data["project_root"],
                state_dir=data["state_dir"],
                started_ts=data["started_ts"],
            )
        except (KeyError, TypeError):
            continue
        if Path(active.project_root).resolve(strict=False) != root:
            continue
        if newest is None or active.started_ts > newest_ts:
            newest = active
            newest_ts = active.started_ts
    return newest


def clear_active_dispatch(session_id: str, *, home: Path | None = None) -> bool:
    path = active_dispatch_path(session_id, home=home)
    if not path.exists():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def nearest_project_root(cwd: Path) -> Path | None:
    """Return the nearest ancestor carrying `.metaensemble/state`."""
    cur = cwd.resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / ".metaensemble" / "state").is_dir():
            return candidate
    return None


def resolve_tool_paths(tool_name: str, tool_input: dict[str, Any] | None) -> tuple[str, ...]:
    """Extract file paths from a Claude Code file-tool input payload."""
    if tool_name not in FILE_TOOL_NAMES or not isinstance(tool_input, dict):
        return ()
    if tool_name in {"Write", "Edit", "MultiEdit"}:
        raw = tool_input.get("file_path") or tool_input.get("path")
        return (str(raw),) if raw else ()
    if tool_name == "NotebookEdit":
        raw = tool_input.get("notebook_path") or tool_input.get("file_path") or tool_input.get("path")
        return (str(raw),) if raw else ()
    return ()


def resolve_against_cwd(raw_path: str, cwd: Path) -> Path:
    p = Path(raw_path).expanduser()
    if not p.is_absolute():
        p = cwd / p
    return p.resolve(strict=False)


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def file_events_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "file-events"


def append_file_event(state_dir: Path, event: FileToolEvent) -> Path:
    out_dir = file_events_dir(state_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = event.run_id or _safe_name(event.session_id)
    target = out_dir / f"{name}.jsonl"
    with target.open("a") as f:
        f.write(json.dumps(asdict(event)) + "\n")
    return target


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _coerce_event(data: dict[str, Any]) -> FileToolEvent | None:
    try:
        return FileToolEvent(
            ts=data["ts"],
            session_id=data.get("session_id") or "",
            run_id=data.get("run_id"),
            tool_name=data["tool_name"],
            path=data["path"],
            rel_path=data.get("rel_path"),
            cwd=data.get("cwd") or "",
        )
    except (KeyError, TypeError):
        return None


def read_file_events(
    state_dir: Path,
    *,
    run_id: str,
    session_id: str,
    started_ts: str,
    ended_ts: str,
) -> tuple[FileToolEvent, ...]:
    """Read events relevant to one Run.

    Prefer exact run/session matches, and also accept timestamp-window
    matches inside the same project state. The latter covers runtime versions
    where subagent file-tool hooks carry a child session id while the Task
    PostToolUse hook carries the parent session id.
    """
    root = file_events_dir(state_dir)
    if not root.exists():
        return ()
    start = _parse_ts(started_ts)
    end = _parse_ts(ended_ts)
    events: list[FileToolEvent] = []
    for path in root.glob("*.jsonl"):
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = _coerce_event(raw)
            if event is None:
                continue
            if event.run_id == run_id or event.session_id == session_id:
                events.append(event)
                continue
            if start is not None and end is not None:
                event_ts = _parse_ts(event.ts)
                if event_ts is not None and start <= event_ts <= end:
                    events.append(event)
    # Stable de-dupe by (tool, path, ts).
    seen: set[tuple[str, str, str]] = set()
    deduped: list[FileToolEvent] = []
    for event in events:
        key = (event.tool_name, event.path, event.ts)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(event)
    return tuple(deduped)


def clear_file_events(
    state_dir: Path,
    *,
    run_id: str,
    events: tuple[FileToolEvent, ...] = (),
) -> None:
    paths = {file_events_dir(state_dir) / f"{run_id}.jsonl"}
    for event in events:
        if event.run_id:
            paths.add(file_events_dir(state_dir) / f"{event.run_id}.jsonl")
        elif event.session_id:
            paths.add(file_events_dir(state_dir) / f"{_safe_name(event.session_id)}.jsonl")
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
