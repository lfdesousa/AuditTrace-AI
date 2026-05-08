"""PDF document metadata extraction (tier-C item #10, ADR-056).

Surfaces ``doc.metadata`` from pymupdf into the manifest's audit-pivot
columns: ``pdf_title`` / ``pdf_author`` / ``pdf_creator`` /
``pdf_creation_date``. The two heavy parts:

- ``_parse_pdf_date`` — defensive parser for the PDF 1.7 §7.9.4 date
  format (``D:YYYYMMDDHHmmSSOHH'mm``) plus its real-world variants.
- ``_extract_pdf_metadata`` — the orchestrator the pipeline invokes
  once per document. Returns ``(title, author, creator, creation_date,
  metadata_warning_codes)``; the warning codes are appended into the
  document's ``extraction_warnings`` list at the call site.

Both fields use defensive degradation — a malformed date / missing
metadata key / pymupdf raise yields ``None`` for that field, never an
exception. A ``"pdf_metadata_parse_error"`` sentinel surfaces in
``warning_codes`` only when the date string was non-empty and refused
to parse (so the audit trail records "we tried but the date was
malformed", distinct from "no date supplied").
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any


def _parse_pdf_date(raw: str) -> datetime | None:
    """Best-effort PDF-date parser.

    PDF 1.7 §7.9.4 specifies ``D:YYYYMMDDHHmmSSOHH'mm`` where ``O`` is
    ``+`` / ``-`` / ``Z``, and trailing fields are optional. Real-world
    producers vary widely (missing seconds, missing TZ, extra whitespace,
    UTF-16-decoded leftovers from pymupdf). We accept what we can; the
    caller logs ``pdf_metadata_parse_error`` when this returns ``None``.

    Returns a tz-aware ``datetime`` (UTC for ``Z``-suffixed or offset-
    converted; local-as-stated otherwise — but always tz-aware so the
    Postgres ``TIMESTAMPTZ`` column stays consistent).
    """
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if s.startswith("D:"):
        s = s[2:]
    # Strip trailing apostrophes/quotes — PDF dates use ``+HH'mm'`` form.
    s = s.replace("'", "")
    if not s:
        return None
    # Pull off optional timezone suffix.
    tz: timezone | None = None
    tz_marker = None
    tz_part = ""
    for marker in ("Z", "+", "-"):
        idx = s.find(marker, 8)  # Search after the date portion.
        if idx >= 0:
            tz_marker = marker
            tz_part = s[idx:]
            s = s[:idx]
            break
    if tz_marker == "Z":
        tz = UTC
    elif tz_marker in ("+", "-"):
        try:
            sign = 1 if tz_marker == "+" else -1
            digits = tz_part[1:].replace(":", "")
            hh = int(digits[:2]) if len(digits) >= 2 else 0
            mm = int(digits[2:4]) if len(digits) >= 4 else 0
            tz = timezone(sign * timedelta(hours=hh, minutes=mm))
        except (ValueError, IndexError):
            tz = UTC  # Fallback rather than fail the parse.
    else:
        tz = UTC  # No TZ → assume UTC for consistency.
    fmts = (
        "%Y%m%d%H%M%S",
        "%Y%m%d%H%M",
        "%Y%m%d%H",
        "%Y%m%d",
        "%Y%m",
        "%Y",
    )
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    return None


def _trim_pdf_metadata_string(value: Any, *, max_len: int = 255) -> str | None:
    """Coerce a pymupdf metadata value to a clean ``str`` ≤ ``max_len``.

    pymupdf returns either ``str`` or ``None``. Empty / whitespace-only
    strings collapse to ``None``. Over-long strings are truncated (255 is
    the schema cap from migration 011) — auditors are not relying on
    these fields for cryptographic identity (that's ``document_sha256``);
    truncation is a UX concession not a correctness loss.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    return s[:max_len] if len(s) > max_len else s


def _extract_pdf_metadata(
    doc: Any,
) -> tuple[str | None, str | None, str | None, datetime | None, list[str]]:
    """Extract the four ADR-056 #10 metadata fields from pymupdf ``doc.metadata``.

    Returns ``(title, author, creator, creation_date, metadata_warning_codes)``.
    The warning-codes list contains zero or more ``"pdf_metadata_parse_error"``
    sentinels — the caller wraps them into structured ``extraction_warnings``
    entries. Any pymupdf raise inside this function is swallowed and reported
    as one warning code (the function never re-raises).
    """
    warning_codes: list[str] = []
    try:
        meta = doc.metadata
    except Exception:  # pragma: no cover — defensive
        return None, None, None, None, ["pdf_metadata_parse_error"]
    if not isinstance(meta, dict):
        # pymupdf always returns a dict in real use; non-dict (None, a
        # MagicMock from a unit-test fixture, anything else) means there
        # is nothing useful to extract — return clean empties without
        # raising the metadata-parse-error sentinel.
        return None, None, None, None, []
    title = _trim_pdf_metadata_string(meta.get("title"))
    author = _trim_pdf_metadata_string(meta.get("author"))
    creator = _trim_pdf_metadata_string(meta.get("creator"))
    raw_date = meta.get("creationDate")
    creation_date: datetime | None = None
    if raw_date is not None:
        if isinstance(raw_date, str) and raw_date.strip():
            creation_date = _parse_pdf_date(raw_date)
            if creation_date is None:
                warning_codes.append("pdf_metadata_parse_error")
        elif not isinstance(raw_date, str):
            warning_codes.append("pdf_metadata_parse_error")
    return title, author, creator, creation_date, warning_codes
