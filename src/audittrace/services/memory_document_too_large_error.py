"""The refusal every memory write path raises for an over-limit document."""

from __future__ import annotations


class MemoryDocumentTooLargeError(Exception):
    """A memory document is larger than ``memory_max_document_bytes``.

    Deliberately NOT a ``ValueError``: the write routes map ``ValueError`` to
    400, and this refusal has its own status (413)."""

    def __init__(self, limit_bytes: int) -> None:
        super().__init__(f"memory document exceeds the limit of {limit_bytes} bytes")
        self.limit_bytes = limit_bytes
