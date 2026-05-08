"""TOC (table-of-contents) → page-section index.

Tier-C item #9 (ADR-056). pymupdf's ``Document.get_toc(simple=True)``
returns ``[[level, title, page_1based], ...]``. We build a forward-fill
``{page_1based: title}`` index so every page in a chunked document
carries the title of the most-recent TOC entry at or before it. The
index is consumed during PDF chunk metadata assembly so each chunk
carries a ``toc_section`` field; auditors filter chunks by section
without the embedder needing to be section-aware.

Multi-level TOCs collapse to the leaf title for now — breadcrumbs
(``Chapter / Section / Subsection``) are a future iteration documented
in ADR-056 §Out of scope.
"""

from __future__ import annotations

from typing import Any


def _build_toc_index(doc: Any) -> dict[int, str]:
    """Return ``{page_number_1based: section_title}`` from the document TOC.

    Pages before the first TOC entry stay unmapped (``None`` at lookup
    time / key absent in the dict). Defensive: any pymupdf raise → empty
    dict (degrades to per-page chunking with no toc_section metadata,
    the legacy pre-tier-C behaviour).

    Skips malformed entries silently:
    - Missing page (IndexError on ``entry[2]``)
    - Title is None / empty / whitespace-only
    - Page is non-numeric / ≤ 0
    """
    try:
        toc = doc.get_toc(simple=True)
    except Exception:
        return {}
    if not toc:
        return {}
    index: dict[int, str] = {}
    sorted_entries: list[tuple[int, str]] = []
    for entry in toc:
        try:
            _level, title, page = entry[0], entry[1], entry[2]
        except (IndexError, TypeError):
            continue
        try:
            page_int = int(page)
        except (TypeError, ValueError):
            continue
        if page_int <= 0:
            continue
        title_clean = (title or "").strip()
        if not title_clean:
            continue
        sorted_entries.append((page_int, title_clean[:255]))
    if not sorted_entries:
        return {}
    sorted_entries.sort(key=lambda t: t[0])
    page_count = getattr(doc, "page_count", 0) or 0
    if page_count <= 0:
        page_count = sorted_entries[-1][0]
    cursor = 0
    current_title: str | None = None
    for page_n in range(1, page_count + 1):
        while cursor < len(sorted_entries) and sorted_entries[cursor][0] <= page_n:
            current_title = sorted_entries[cursor][1]
            cursor += 1
        if current_title is not None:
            index[page_n] = current_title
    return index
