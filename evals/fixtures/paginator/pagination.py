"""Slice sequences into fixed-size pages.

``paginate`` is the module's entry point. Intended behavior:

- ``paginate(items, page, page_size)`` returns the zero-indexed
  ``page``-th chunk of ``items``. Every chunk except possibly the
  last has exactly ``page_size`` elements; the last chunk carries
  the remainder.
- Concatenating ``paginate(items, p, n)`` for ``p = 0, 1, 2, ...``
  reproduces ``items`` exactly. In particular
  ``paginate(list(range(6)), 1, 3)`` returns ``[3, 4, 5]`` and
  ``paginate(list(range(10)), 3, 3)`` returns ``[9]``.
- A ``page`` that starts at or past the end of ``items`` yields ``[]``.
"""
from __future__ import annotations

from typing import Sequence, TypeVar

T = TypeVar("T")


def page_count(total: int, page_size: int) -> int:
    """Number of pages needed to hold ``total`` items."""
    if page_size <= 0:
        raise ValueError(f"page_size must be positive, got {page_size}")
    if total <= 0:
        return 0
    return (total + page_size - 1) // page_size


def paginate(items: Sequence[T], page: int, page_size: int) -> list[T]:
    """Return the zero-indexed ``page``-th page of ``items``.

    The final page carries the remainder, so
    ``paginate(list(range(7)), 2, 3)`` returns ``[6]``.
    """
    if page_size <= 0:
        raise ValueError(f"page_size must be positive, got {page_size}")
    if page < 0:
        raise ValueError(f"page must be non-negative, got {page}")
    start = page * page_size
    stop = min(start + page_size, len(items) - 1)
    return list(items[start:stop])
