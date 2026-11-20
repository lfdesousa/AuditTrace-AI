"""The memory write bound as an HTTP refusal (413), and the bounded upload read."""

from __future__ import annotations

from fastapi import HTTPException, UploadFile

from audittrace.routes.memory_upload.quarantine import PDF_MAGIC, is_pdf_upload
from audittrace.services.memory_document_too_large_error import (
    MemoryDocumentTooLargeError,
)
from audittrace.services.memory_write_limit import (
    check_document_size,
    memory_max_document_bytes,
    pdf_max_document_bytes,
)


def refuse_oversize_or_413(document: str | bytes, *, pdf: bool = False) -> None:
    """413 with the constant detail naming the limit in bytes. Call it BEFORE a
    write route's first side effect."""
    try:
        check_document_size(document, pdf=pdf)
    except MemoryDocumentTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc


async def read_upload_bounded(file: UploadFile) -> bytes:
    """Read an upload no further than one byte past the bound that applies to
    it, and refuse it there. A text document has the memory bound; a PDF (by
    content type AND magic bytes) has the PDF bound, at upload as at fold."""
    head = await file.read(len(PDF_MAGIC))
    pdf = is_pdf_upload(claimed_content_type=file.content_type or "", content=head)
    await file.seek(0)
    # ONE read into ONE buffer: the kind is known from the first bytes, so the
    # file is never read in two pieces that then have to be joined.
    bound = pdf_max_document_bytes() if pdf else memory_max_document_bytes()
    content = await file.read(bound + 1)
    refuse_oversize_or_413(content, pdf=pdf)
    return content
