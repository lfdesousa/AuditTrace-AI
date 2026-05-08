"""PDF/A conformance extraction from XMP metadata.

Tier-C item #14 (ADR-056). PDF/A conformance is encoded in the XMP
packet under the ``http://www.aiim.org/pdfa/ns/id/`` namespace as
``pdfaid:part`` (1 / 2 / 3 / 4) and ``pdfaid:conformance`` (A / B / U).
Both element form (``<pdfaid:part>3</pdfaid:part>``) and attribute form
(``pdfaid:part="3"``) appear in the wild; we handle both with one regex
each so the parser doesn't depend on full XML.

The two values map 1:1 to the ``pdfa_part`` and ``pdfa_conformance``
columns added in migration 011. Two columns rather than a combined
string so audit queries can filter on either independently (e.g.
``WHERE pdfa_part = '3'`` for ZUGFeRD invoices).
"""

from __future__ import annotations

import re
from typing import Any

_PDFA_PART_RE = re.compile(
    r"<pdfaid:part[^>]*>\s*([0-9]+)\s*</pdfaid:part>"
    r"|pdfaid:part\s*=\s*[\"']([0-9]+)[\"']",
    re.IGNORECASE,
)
_PDFA_CONFORMANCE_RE = re.compile(
    r"<pdfaid:conformance[^>]*>\s*([A-Z])\s*</pdfaid:conformance>"
    r"|pdfaid:conformance\s*=\s*[\"']([A-Z])[\"']",
    re.IGNORECASE,
)


def _extract_pdfa_conformance(doc: Any) -> tuple[str | None, str | None]:
    """Return ``(pdfa_part, pdfa_conformance)`` from XMP, or ``(None, None)``.

    pymupdf returns the XMP packet as a string from ``get_xml_metadata``.
    Defensive: any raise → both None. Empty / non-string XMP → both None.
    """
    try:
        xmp = doc.get_xml_metadata()
    except Exception:
        return None, None
    if not isinstance(xmp, str) or not xmp:
        return None, None
    part_match = _PDFA_PART_RE.search(xmp)
    conf_match = _PDFA_CONFORMANCE_RE.search(xmp)
    part = None
    conf = None
    if part_match is not None:
        part = (part_match.group(1) or part_match.group(2) or "").strip() or None
    if conf_match is not None:
        conf = (
            conf_match.group(1) or conf_match.group(2) or ""
        ).strip().upper() or None
    return part, conf
