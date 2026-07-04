"""Tests for ``pagination``.

Four tests cover full interior pages, the out-of-range edge, and page
arithmetic.
"""
from pagination import page_count, paginate


def test_first_page_is_full():
    assert paginate(list(range(10)), 0, 3) == [0, 1, 2]


def test_middle_page_is_full():
    assert paginate(list(range(10)), 1, 3) == [3, 4, 5]


def test_page_past_the_end_is_empty():
    assert paginate(list(range(10)), 5, 3) == []


def test_page_count_rounds_up():
    assert page_count(10, 3) == 4
    assert page_count(9, 3) == 3
    assert page_count(0, 3) == 0
