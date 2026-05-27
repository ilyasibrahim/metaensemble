"""Normalizers for loosely-shaped runtime payload fields."""
from __future__ import annotations

import json
from typing import Any


def _scalar_text(raw: Any) -> str | None:
    if isinstance(raw, str):
        text = raw.strip()
        return text or None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return str(raw)
    return None


def normalize_model_identity(raw: Any) -> str | None:
    """Return a stable model identity string from runtime payload data.

    Runtime payloads are not schema-owned by MetaEnsemble. Current releases
    may expose a plain string, while others expose structured objects such as
    ``{"id": "...", "display_name": "..."}``. The Ledger owns a scalar model
    column, so every ingress path must pass through this boundary normalizer.
    """
    scalar = _scalar_text(raw)
    if scalar:
        return scalar

    if isinstance(raw, dict):
        for key in ("id", "display_name"):
            value = _scalar_text(raw.get(key))
            if value:
                return value
        if not raw:
            return None
        try:
            return json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            text = str(raw).strip()
            return text or None

    return None
