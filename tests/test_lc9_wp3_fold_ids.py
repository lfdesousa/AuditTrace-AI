"""LC-9 WP3 -- the fold sinks derive ChromaDB ids in ONE place.

``_chroma_ids`` is the single helper the markdown fold (``_index_md_objects``)
and the PDF pipeline (``_index_pdf_objects``) call before ``_upsert_in_batches``.
Private tier -> ``{owner}__{id}`` (always prepended); corpus tier -> the bare
id. Neuters: the helper returns the bare ids -> every private case RED;
the helper prefixes corpus ids too -> the corpus cases RED; the PDF pipeline
stops calling it (or its ``tier`` default flips to corpus) -> the PDF cases RED.

Doubles declared: the ChromaDB collection is an ``AsyncMock`` spy (only its
``upsert`` ids are read); ``audittrace.routes.memory.embed_via_nomic`` is an
``AsyncMock`` returning fixed vectors; the object store is a ``MagicMock``
returning real PDF bytes made by ``pymupdf`` (the PDF parser itself is real).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pymupdf
import pytest

from audittrace.identity import UserContext
from audittrace.routes.memory import _chroma_ids
from audittrace.routes.memory_pdf.pipeline import _index_pdf_objects
from audittrace.services.memory_keys import InvalidPrivateSubjectError


def _user(sub: str) -> UserContext:
    return UserContext(
        user_id=sub, username=sub, agent_type="test", scopes=(), is_admin=False
    )


class TestChromaIdsHelper:
    def test_private_ids_are_owner_prefixed_and_always_prepended(self) -> None:
        out = _chroma_ids(_user("user-a"), "private", ["h0", "user-a__h0"])
        assert out == ["user-a__h0", "user-a__user-a__h0"]

    def test_corpus_ids_are_unchanged(self) -> None:
        assert _chroma_ids(_user("user-a"), "corpus", ["h0", "h1"]) == ["h0", "h1"]

    def test_a_sub_that_cannot_own_a_private_id_is_refused(self) -> None:
        with pytest.raises(InvalidPrivateSubjectError):
            _chroma_ids(_user("bad__sub"), "private", ["h0"])


def _pdf_bytes() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "LC-9 WP3 fold id probe. " * 20)
    out = doc.tobytes()
    doc.close()
    return bytes(out)


def _minio(content: bytes) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.read.return_value = content
    response.__enter__.return_value = response
    client.get_object.return_value = response
    return client


async def _fold_rows(
    monkeypatch: pytest.MonkeyPatch, **kw: str
) -> tuple[list[str], list[dict]]:
    monkeypatch.setattr(
        "audittrace.routes.memory.embed_via_nomic",
        AsyncMock(side_effect=lambda texts, **_: [[0.1, 0.2, 0.3] for _ in texts]),
    )
    collection = AsyncMock()
    # the D1 pre-check reads the target ids: nothing is there yet
    collection.get = AsyncMock(return_value={"ids": [], "metadatas": []})
    await _index_pdf_objects(
        collection,
        _minio(_pdf_bytes()),
        "bucket",
        [{"key": "episodic/p.pdf", "filename": "p.pdf"}],
        "ai_research_papers",
        category="episodic",
        layer_prefix="episodic/",
        user_id="user-a",
        ingestion_ts_ms=0,
        user=_user("user-a"),
        manifest_service=None,
        **kw,
    )
    ids: list[str] = []
    metas: list[dict] = []
    for call in collection.upsert.await_args_list:
        ids.extend(call.kwargs["ids"])
        metas.extend(call.kwargs["metadatas"])
    return ids, metas


async def _fold(monkeypatch: pytest.MonkeyPatch, **kw: str) -> list[str]:
    return (await _fold_rows(monkeypatch, **kw))[0]


class TestPdfPipelineIds:
    async def test_private_tier_chunks_are_owner_prefixed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ids = await _fold(monkeypatch, tier="private")
        assert ids and all(i.startswith("user-a__") for i in ids)

    async def test_the_default_tier_is_private(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ids = await _fold(monkeypatch)
        assert ids and all(i.startswith("user-a__") for i in ids)

    async def test_corpus_tier_chunks_keep_the_bare_hash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ids = await _fold(monkeypatch, tier="corpus")
        assert ids and not any("__" in i for i in ids)


class TestPdfChunkMetadata:
    """ADDENDUM-5 D0 (+ condition C1): the PDF fold now carries ``tier``; a
    private chunk is stamped ``id_form == "lc9"``, a corpus chunk is NOT and
    keeps a bare 16-hex id. Neuters: omit ``tier`` / the stamp / stamp corpus."""

    async def test_private_chunks_carry_tier_and_the_form_stamp(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ids, metas = await _fold_rows(monkeypatch, tier="private")
        assert ids and len(ids) == len(metas)
        assert all(m["tier"] == "private" and m["id_form"] == "lc9" for m in metas)

    async def test_corpus_chunks_carry_tier_corpus_no_stamp_and_a_bare_hex_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ids, metas = await _fold_rows(monkeypatch, tier="corpus")
        assert ids and all(m["tier"] == "corpus" for m in metas)
        assert not any("id_form" in m for m in metas)
        assert all(len(i) == 16 and int(i, 16) >= 0 for i in ids)
