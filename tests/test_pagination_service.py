"""Tests for the shared sort + paginate core (backlog #15 R2b, #375 /
RECALL-PAGINATION-20260803).

``audittrace.services.pagination.sort_and_paginate`` is exercised
indirectly by ``routes/memory.py``'s ``_sort_and_paginate`` (LIST endpoint)
and ``services/semantic.py``'s ``search_page`` (recall tools) test suites
already; this file locks in the primitive's own contract directly so a
regression here is caught at the source rather than only downstream.
"""

from audittrace.services.pagination import sort_and_paginate


def test_ascending_sort_and_slice():
    items = [{"v": 3}, {"v": 1}, {"v": 2}]
    page, total = sort_and_paginate(
        items,
        key_fn=lambda i: i["v"],
        tiebreak_fn=lambda i: i["v"],
        reverse=False,
        limit=2,
        offset=0,
    )
    assert [i["v"] for i in page] == [1, 2]
    assert total == 3


def test_descending_sort():
    items = [{"v": 3}, {"v": 1}, {"v": 2}]
    page, _ = sort_and_paginate(
        items,
        key_fn=lambda i: i["v"],
        tiebreak_fn=lambda i: i["v"],
        reverse=True,
        limit=3,
        offset=0,
    )
    assert [i["v"] for i in page] == [3, 2, 1]


def test_offset_slices_past_the_first_page():
    items = [{"v": i} for i in range(10)]
    page, total = sort_and_paginate(
        items,
        key_fn=lambda i: i["v"],
        tiebreak_fn=lambda i: i["v"],
        reverse=False,
        limit=3,
        offset=6,
    )
    assert [i["v"] for i in page] == [6, 7, 8]
    assert total == 10


def test_tiebreak_is_stable_and_ascending_regardless_of_reverse():
    """Items with equal ``key_fn`` values keep tiebreak-ascending order
    even when ``reverse=True`` — this is what makes paging deterministic:
    no two calls ever reorder a tied pair differently, which would let an
    item silently vanish between pages."""
    items = [{"k": 0, "id": "b"}, {"k": 0, "id": "a"}, {"k": 0, "id": "c"}]
    page, _ = sort_and_paginate(
        items,
        key_fn=lambda i: i["k"],
        tiebreak_fn=lambda i: i["id"],
        reverse=True,
        limit=3,
        offset=0,
    )
    assert [i["id"] for i in page] == ["a", "b", "c"]


def test_offset_beyond_total_returns_empty_page():
    items = [{"v": 1}, {"v": 2}]
    page, total = sort_and_paginate(
        items,
        key_fn=lambda i: i["v"],
        tiebreak_fn=lambda i: i["v"],
        reverse=False,
        limit=5,
        offset=10,
    )
    assert page == []
    assert total == 2


def test_empty_items_returns_empty_page_and_zero_total():
    page, total = sort_and_paginate(
        [],
        key_fn=lambda i: i,
        tiebreak_fn=lambda i: i,
        reverse=False,
        limit=5,
        offset=0,
    )
    assert page == []
    assert total == 0
