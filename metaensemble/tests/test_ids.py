"""Tests for UUIDv7 and Executor alias generation."""
from __future__ import annotations

import re
import time

import pytest

from metaensemble.lib.ids import derive_alias_prefix, make_alias, parse_alias, uuid7


def test_uuid7_version_field():
    u = uuid7()
    assert (u.bytes[6] >> 4) == 7


def test_uuid7_variant_field():
    u = uuid7()
    assert (u.bytes[8] >> 6) == 0b10


def test_uuid7_time_sortable():
    u1 = uuid7()
    time.sleep(0.005)
    u2 = uuid7()
    assert u1.bytes[:6] <= u2.bytes[:6]


def test_uuid7_uniqueness_at_scale():
    ids = {uuid7() for _ in range(2000)}
    assert len(ids) == 2000


def test_alias_format_matches_pattern():
    u = uuid7()
    alias = make_alias("be", u)
    assert re.match(r"^be-[0-9a-f]{3}$", alias) is not None


def test_alias_uses_last_three_hex_of_uuid():
    u = uuid7()
    alias = make_alias("arch", u)
    assert alias.endswith(u.hex[-3:])


def test_alias_rejects_empty_prefix():
    with pytest.raises(ValueError):
        make_alias("", uuid7())


def test_alias_rejects_uppercase_prefix():
    with pytest.raises(ValueError):
        make_alias("Backend", uuid7())


def test_alias_rejects_non_alphanumeric_prefix():
    with pytest.raises(ValueError):
        make_alias("be-ops", uuid7())


def test_alias_rejects_overlong_prefix():
    with pytest.raises(ValueError):
        make_alias("toolongprefix", uuid7())


def test_parse_alias_roundtrip():
    u = uuid7()
    alias = make_alias("backend", u)
    prefix, suffix = parse_alias(alias)
    assert prefix == "backend"
    assert suffix == u.hex[-3:]


def test_parse_alias_rejects_missing_separator():
    with pytest.raises(ValueError):
        parse_alias("noseparator")


def test_parse_alias_rejects_non_hex_suffix():
    with pytest.raises(ValueError):
        parse_alias("prefix-zzz")


def test_parse_alias_rejects_wrong_length_suffix():
    with pytest.raises(ValueError):
        parse_alias("prefix-1234")


def test_derive_alias_prefix_basic():
    # Default prefix is the first up-to-4 alphanumeric characters of the
    # Role name. Role specs that want a more evocative prefix (e.g. "meng"
    # for ml-engineer) should declare `alias_prefix` explicitly.
    assert derive_alias_prefix("backend") == "back"
    assert derive_alias_prefix("ml-engineer") == "mlen"
    assert derive_alias_prefix("ux") == "ux"


def test_derive_alias_prefix_rejects_no_alphanumeric():
    with pytest.raises(ValueError):
        derive_alias_prefix("---")
