"""UUIDv7 and Executor alias generation for MetaEnsemble.

UUIDv7 (RFC 9562) is the canonical identifier for Executors and Runs.
Aliases are human-typeable shorthand: <prefix>-<3 hex>.

See ARCHITECTURE.md §5 for the data model and PERFORMANCE.md §3 R1 for the
named-API discipline this module supports.
"""
from __future__ import annotations

import os
import time
import uuid as _uuid


def uuid7() -> _uuid.UUID:
    """Generate a UUIDv7 (RFC 9562). Time-sortable, ~74 bits of randomness.

    Layout: 48 bits unix-ms timestamp, 4 bits version (=7), 12 bits random,
    2 bits variant (=10), 62 bits random. Lexicographic order on the hex
    form matches creation order, which is what makes UUIDv7 suitable as a
    primary key in time-ordered ledgers.
    """
    timestamp_ms = int(time.time() * 1000)
    rand = os.urandom(10)

    b = bytearray(16)
    # 48 bits: unix epoch milliseconds (big-endian)
    b[0:6] = timestamp_ms.to_bytes(6, "big")
    # Version 7 in upper nibble of byte 6, randomness in the lower nibble.
    b[6] = 0x70 | (rand[0] & 0x0F)
    b[7] = rand[1]
    # Variant 10xx in the upper bits of byte 8, randomness in the lower 6.
    b[8] = 0x80 | (rand[2] & 0x3F)
    b[9:16] = rand[3:10]

    return _uuid.UUID(bytes=bytes(b))


def make_alias(role_alias_prefix: str, executor_uuid: _uuid.UUID) -> str:
    """Generate a human-typeable Executor alias: <prefix>-<3 hex>.

    Collisions within a Role are resolved at the registration layer (the
    `executors.alias` UNIQUE constraint catches duplicates). The caller
    retries with a fresh UUID on collision.

    Args:
        role_alias_prefix: 1-8 char alphanumeric prefix from the Role spec.
        executor_uuid: the canonical UUIDv7 for the Executor.

    Returns:
        Formatted alias, e.g. 'arch-7b3'.

    Raises:
        ValueError: if the prefix is empty, too long, or non-alphanumeric.
    """
    if not role_alias_prefix:
        raise ValueError("role_alias_prefix must be non-empty")
    if not role_alias_prefix.isalnum() or not role_alias_prefix.islower():
        raise ValueError(
            f"role_alias_prefix must be lowercase alphanumeric; got {role_alias_prefix!r}"
        )
    if len(role_alias_prefix) > 8:
        raise ValueError(
            f"role_alias_prefix exceeds 8 chars: {role_alias_prefix!r}"
        )
    suffix = executor_uuid.hex[-3:]
    return f"{role_alias_prefix}-{suffix}"


def parse_alias(alias: str) -> tuple[str, str]:
    """Split an alias into (prefix, hex_suffix). Raises ValueError if malformed."""
    if "-" not in alias:
        raise ValueError(f"malformed alias (missing separator): {alias!r}")
    prefix, _, suffix = alias.rpartition("-")
    if not prefix:
        raise ValueError(f"malformed alias (empty prefix): {alias!r}")
    if len(suffix) != 3 or not all(c in "0123456789abcdef" for c in suffix):
        raise ValueError(f"malformed alias suffix: {alias!r}")
    return prefix, suffix


def derive_alias_prefix(role_name: str) -> str:
    """Default alias prefix from a Role name when the spec does not declare one.

    Takes the first up-to-4 alphanumeric characters of the Role name.
    """
    cleaned = "".join(c for c in role_name.lower() if c.isalnum())
    if not cleaned:
        raise ValueError(f"role_name has no alphanumeric content: {role_name!r}")
    return cleaned[:4]
