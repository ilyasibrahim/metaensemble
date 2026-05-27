#!/usr/bin/env python3
"""PostToolUse hook for Write invocations that produce Deliverables.

Per ARCHITECTURE.md §8: when a Write call lands a markdown file under a
`reports/` directory, this hook treats the file as a Deliverable and
records the path so the Registry view can surface it. The hook is
deliberately non-blocking; if the write turns out not to be a Deliverable
or the recording fails, the hook logs and exits 0.

Stdin shape:
    {
      "tool_name": "Write",
      "tool_input": { "file_path": "..." },
      "tool_output": { ... }
    }
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from metaensemble.hooks._common import (  # noqa: E402
    emit,
    log_error,
    read_input,
    state_dir,
)


DELIVERABLE_INDEX_PATH = "deliverables_index.jsonl"


def _is_deliverable(file_path: str) -> bool:
    """A Write counts as a Deliverable if its path lies under `reports/` and is markdown."""
    p = Path(file_path)
    if p.suffix.lower() != ".md":
        return False
    return "reports" in p.parts


def run() -> int:
    payload = read_input()
    if payload.get("tool_name") != "Write":
        emit({"continue": True})
        return 0

    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path")
    if not file_path or not _is_deliverable(file_path):
        emit({"continue": True})
        return 0

    try:
        index_path = state_dir() / DELIVERABLE_INDEX_PATH
        index_path.parent.mkdir(parents=True, exist_ok=True)
        # Append the path; the Registry view de-duplicates on read.
        import json
        from datetime import datetime, timezone
        record = {"ts": datetime.now(timezone.utc).isoformat(), "path": file_path}
        with index_path.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:
        log_error("deliverable-sync-failed", str(exc), {"file_path": file_path})

    emit({"continue": True})
    return 0


if __name__ == "__main__":
    sys.exit(run())
