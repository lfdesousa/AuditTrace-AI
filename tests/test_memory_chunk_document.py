"""MEMORY-SERVER-SILENT-TRUNCATION ADDENDUM-2 (gate r2 S12): the fold's chunker
loses no text, and for a document ``_chunk_text`` drops nothing from it cuts
the SAME chunks (so what recall embeds for such a document does not change)."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from audittrace.routes import memory as m

REPO = Path(__file__).resolve().parents[1]
STEP = m.CHUNK_SIZE - m.CHUNK_OVERLAP


def _chunk_document(text: str) -> list[str]:
    from audittrace.routes.memory_chunking import chunk_document

    return chunk_document(text, m.CHUNK_SIZE, m.CHUNK_OVERLAP)


def _reassemble(chunks: list[str]) -> str:
    return chunks[0] + "".join(c[m.CHUNK_OVERLAP :] for c in chunks[1:])


def _random_text(rng: random.Random) -> str:
    parts: list[str] = []
    for _ in range(rng.randint(1, 8)):
        kind = rng.random()
        if kind < 0.45:
            parts.append(rng.choice(" \n\t") * rng.randint(1, 2 * m.CHUNK_SIZE))
        else:
            parts.append(
                "".join(rng.choice("abc é\n") for _ in range(rng.randint(1, 4000)))
            )
    return "".join(parts)


class TestChunkDocument:
    def test_reassembly_is_the_text_for_2000_random_documents(self) -> None:
        rng = random.Random(20261116)
        checked = 0
        for _ in range(2000):
            text = _random_text(rng)
            chunks = _chunk_document(text)
            if not text.strip():
                assert chunks == []
                continue
            assert _reassemble(chunks) == text
            assert all(c.strip() for c in chunks)
            checked += 1
        assert checked > 1000

    def test_same_chunks_as_chunk_text_when_it_drops_nothing(self) -> None:
        """Every markdown file of the repository: where ``_chunk_text`` keeps
        every window, the two chunkers agree chunk for chunk."""
        compared = 0
        differing: list[Any] = []
        for path in sorted(REPO.glob("docs/**/*.md")) + sorted(REPO.glob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            old = m._chunk_text(text)
            lossless = _reassemble(old) == text if old else not text
            if not lossless:
                continue
            compared += 1
            if _chunk_document(text) != old:
                differing.append(path.name)
        assert compared >= 50
        assert differing == []

    def test_the_whitespace_window_goes_to_the_chunk_before_it(self) -> None:
        text = "x" * (2 * STEP) + "\n" * 250
        old = m._chunk_text(text)
        new = _chunk_document(text)

        assert _reassemble(old) != text
        assert len(new) == len(old)
        assert new[:-1] == old[:-1]
        assert new[-1].rstrip("\n") == old[-1].rstrip("\n")
        assert _reassemble(new) == text
