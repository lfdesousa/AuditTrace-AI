"""Chunking for the markdown fold that loses no text.

``routes.memory._chunk_text`` drops a window that is only whitespace. The text
of that window is then in no chunk, so the document cannot be put back together
from its chunks: a whole-document read of it can never match what was folded.
This chunker cuts the SAME windows and, instead of dropping a whitespace-only
one, attaches it to the chunk before it (or, at the very start, to the first
chunk after it). Consecutive chunks still share exactly ``overlap`` characters,
so the reassembly rule is unchanged, and for a document with no whitespace-only
window the chunks are identical to ``_chunk_text``'s.

Not measured (gate r3 A12): a whitespace run is attached whole to one chunk, so
a document that is one long blank run inside text (up to the memory write bound,
4 MiB by default) yields a single chunk of that length. How the real embedder
copes with such a chunk was not measured; no document of today's corpus has one.
"""

from __future__ import annotations


def chunk_document(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Overlapping chunks of *text* whose reassembly is exactly *text*.
    A document that is ONLY whitespace has nothing to embed: no chunks."""
    if not text.strip():
        return []
    if len(text) <= chunk_size:
        return [text]
    spans: list[list[int]] = []
    held_from: int | None = (
        None  # start of whitespace-only windows before the first chunk
    )
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        if text[start:end].strip():
            spans.append([start if held_from is None else held_from, end])
            held_from = None
        elif spans:
            spans[-1][1] = end
        elif held_from is None:
            held_from = start
        start += chunk_size - overlap
    return [text[a:b] for a, b in spans]
