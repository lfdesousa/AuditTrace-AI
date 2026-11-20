"""The size bounds of the memory write paths (MEMORY-SERVER-SILENT-TRUNCATION).

Two bounds, both on the document's length in BYTES: text documents
(``Settings.memory_max_document_bytes``) and PDFs at upload
(``Settings.pdf_upload_max_size_mb``, never above ``pdf_max_size_mb``, the bound
the PDF fold applies to objects that reached storage another way). Every write
path checks its bound
BEFORE its first side effect, so a refused write stores nothing, indexes
nothing and writes no manifest row.
"""

from __future__ import annotations

from audittrace.config import get_settings
from audittrace.services.memory_document_too_large_error import (
    MemoryDocumentTooLargeError,
)

# Room for multipart framing and JSON structure around a document.
_FRAMING_BYTES = 1_048_576
# A JSON string can spend six bytes (``\\uXXXX``) on one byte of text.
_JSON_ESCAPE_FACTOR = 6


def memory_max_document_bytes() -> int:
    """The bound on a text document, in bytes."""
    return get_settings().memory_max_document_bytes


def pdf_max_document_bytes() -> int:
    """The bound on a PDF at upload, in bytes: ``pdf_upload_max_size_mb``, never
    above the bound the PDF fold applies (``pdf_max_size_mb``)."""
    settings = get_settings()
    return min(settings.pdf_upload_max_size_mb, settings.pdf_max_size_mb) * 1024 * 1024


def check_document_size(document: str | bytes, *, pdf: bool = False) -> None:
    """Raise :class:`MemoryDocumentTooLargeError` when *document* is over its bound."""
    limit = pdf_max_document_bytes() if pdf else memory_max_document_bytes()
    size = len(document if isinstance(document, bytes) else document.encode("utf-8"))
    if size > limit:
        raise MemoryDocumentTooLargeError(limit)


def request_body_ceiling(path: str) -> int:
    """The most bytes a request to a memory write path may carry at all. Above
    it no document inside could be under its bound, so the body is not read."""
    if path.rstrip("/") == "/memory/upload":
        return (
            max(pdf_max_document_bytes(), memory_max_document_bytes()) + _FRAMING_BYTES
        )
    return _JSON_ESCAPE_FACTOR * memory_max_document_bytes() + _FRAMING_BYTES
