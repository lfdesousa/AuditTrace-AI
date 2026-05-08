"""Redaction-aware page extraction (tier-A item #8, ADR-050).

PDFs with unflattened redactions can leak content the document author
*intended* to obscure — the redaction annotation hides the rectangle
visually but the underlying text is still extractable via
``page.get_text()``. This module's helpers detect redaction
annotations and clip block-level text around them so the stored chunks
never contain the bytes the author meant to hide.

Public surface:

- ``_page_bbox`` — page-level rectangle for chunk metadata
  (per-block bbox is gap-inventory item #4, multi-column reading-
  order; deferred to a future ADR).
- ``_redaction_rects`` — list of redaction-annotation rects on a page,
  empty when no redactions or when ``page.annots()`` raises.
- ``_rects_intersect`` — inclusive rect-overlap test (when in doubt,
  drop the block — conservative for redaction safety).
- ``_text_clipped_around_redactions`` — join block-level text from
  blocks NOT intersecting any redaction. The clip-extract path the
  ``AUDITTRACE_PDF_REDACTION_POLICY`` setting routes to.
"""

from __future__ import annotations

from typing import Any

# pymupdf.PDF_ANNOT_REDACT — the integer code for redaction-type
# annotations in the PDF spec. Inlined to avoid importing pymupdf at
# module-load time (the heavy import is gated to inside the
# orchestrator). Matches pymupdf.PDF_ANNOT_REDACT == 12 across 1.24+
# versions; the type tuple's [1] string ("Redact") is also checked as
# a belt-and-braces fallback.
_PDF_ANNOT_REDACT = 12


def _page_bbox(page: Any) -> tuple[float, float, float, float]:
    """Return (x0, y0, x1, y1) in PDF user-space units for *page*.

    Page-level bbox in v1 — every chunk on a page shares the page's
    rectangle. Per-block bbox (one rect per text block) is a future
    item (#4 in docs/architecture/pdf-ingestion-gaps.md, multi-column
    reading order). Falls back to zeros if rect access raises — keeps
    the metadata schema stable on malformed PDFs.
    """
    try:
        rect = page.rect
        return (float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1))
    except (AttributeError, TypeError, ValueError):
        return (0.0, 0.0, 0.0, 0.0)


def _redaction_rects(page: Any) -> list[Any]:
    """Return list of redaction-annotation rects on *page*.

    Empty list if the page has no annotations or no redactions.
    Robust to ``page.annots()`` returning ``None`` or raising — both
    seen in the wild on malformed PDFs.
    """
    try:
        annots = page.annots() or []
    except Exception:
        return []
    rects: list[Any] = []
    for annot in annots:
        annot_type = getattr(annot, "type", None)
        if not annot_type:
            continue
        # type is a (int_code, name_str) tuple in pymupdf.
        type_code = annot_type[0] if len(annot_type) > 0 else None
        type_name = annot_type[1] if len(annot_type) > 1 else None
        if type_code == _PDF_ANNOT_REDACT or type_name == "Redact":
            rects.append(annot.rect)
    return rects


def _rects_intersect(a: tuple[float, float, float, float], b: Any) -> bool:
    """Standard rect-overlap test on (x0, y0, x1, y1) pairs.

    Inclusive on edges (two rects touching at a single line still
    count as intersecting — conservative for redaction safety: when
    in doubt, drop the block).
    """
    bx0, by0, bx1, by1 = float(b.x0), float(b.y0), float(b.x1), float(b.y1)
    return not (a[2] < bx0 or a[0] > bx1 or a[3] < by0 or a[1] > by1)


def _text_clipped_around_redactions(page: Any, redaction_rects: list[Any]) -> str:
    """Join block-level text from blocks NOT intersecting any redaction.

    Uses ``page.get_text("blocks")`` which returns
    ``(x0, y0, x1, y1, text, block_no, block_type)`` tuples. Blocks
    whose bbox intersects any redaction rect are dropped — the rest
    are joined in their existing reading order. This is the
    clip-extract path for the AUDITTRACE_PDF_REDACTION_POLICY setting.
    """
    blocks = page.get_text("blocks")
    safe_text: list[str] = []
    for block in blocks:
        # Each block tuple: (x0, y0, x1, y1, text, ...). Defensive
        # indexing — pymupdf has stayed stable on this shape, but
        # downstream changes shouldn't crash extraction.
        if len(block) < 5:
            continue
        bbox = (
            float(block[0]),
            float(block[1]),
            float(block[2]),
            float(block[3]),
        )
        text = block[4]
        if not isinstance(text, str) or not text.strip():
            continue
        if any(_rects_intersect(bbox, r) for r in redaction_rects):
            continue
        safe_text.append(text)
    return "\n".join(safe_text)
