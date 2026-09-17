"""WU-1 of SPEC-2026-09-17-memory-retrieval-and-learning-loop-telemetry
(closes D15) — "retrieval correctness", proved end to end through the REAL
``GET /memory/semantic`` / ``GET /memory/episodic`` routes (the endpoints
``scripts/deploy/memory.py::recall_deploy_lessons`` and the ADR-059 fleet
actually read/write), per ``feedback_test_through_real_http_route``.

Drives the SAME ``POST /memory/upload`` -> ``POST /memory/index`` write
path ``log_deploy_record`` uses (mirrors
``TestADR059FleetRecallGap.test_fleet_agent_recalls_its_own_just_folded_canary``
in ``tests/test_memory_routes.py``), so the chunk counts/tiers this file
asserts on are whatever that code path actually produces, not a fixture
assumption.

Four defect classes, four test classes:

* ``TestDocumentCountDedup`` — item 1: ``limit`` counts DOCUMENTS, not
  chunks. Includes the spec's own non-vacuity acceptance criterion:
  neutering the grouping collapses the document count back toward the
  chunk count; restoring (monkeypatch teardown) brings it back.
* ``TestHonestPagination`` — item 3: ``total_documents``/``total_chunks``
  are both reported and distinct, and paging is over documents.
* ``TestWholeDocumentRead`` — item 2: ``?whole_document=true`` reconstructs
  the full document (chunk 0 onward) whenever a chunk sequence can be
  derived, regardless of which chunk id was requested — and degrades to
  the single fetched chunk (never an error) for the rows it can't
  reconstruct a sequence for.
* ``TestKeyShapeTrap`` — item 4: the upload response's FULL key round-trips
    through the read route without a 404.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from audittrace.routes import memory as m
from audittrace.routes.memory import _chunk_text


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    """Overrides the conftest ``client`` fixture FOR THIS FILE ONLY: wires
    the REAL ``ChromaSemanticService`` (backed by the SAME in-repo fake
    ChromaDB client the indexing pipeline writes to) in place of the
    default test container's ``MockSemanticService``.

    Why: ``read_semantic`` (``GET /memory/semantic/{collection}/
    {document_id}``) resolves its service via ``get_semantic_service()``,
    which the default ``client`` fixture wires to ``MockSemanticService`` —
    an INDEPENDENT in-memory store the ``POST /memory/index`` pipeline
    (which writes through ``get_chromadb()`` directly) never touches. Every
    ``TestWholeDocumentRead`` test needs a genuine round-trip through
    ``read_semantic``, so this fixture shares ONE real ChromaDB double
    (``MockChromaDBFactory``) between the container's ``chromadb`` AND
    ``semantic`` bindings — mirrors ``test_memory_promote_route.py``'s
    ``real_semantic_client`` fixture. ``list_semantic`` (used by the other
    test classes in this file) never calls ``get_semantic_service()`` at
    all, so this swap is a no-op for them — safe to apply file-wide."""
    from audittrace import dependencies
    from audittrace.db.factory import MockChromaDBFactory
    from audittrace.dependencies import create_test_container, reset_container
    from audittrace.server import create_app
    from audittrace.services.semantic import ChromaSemanticService

    monkeypatch.setattr(
        "audittrace.services.semantic.embed_via_nomic",
        AsyncMock(side_effect=lambda texts, **_: [[0.1, 0.2, 0.3] for _ in texts]),
    )

    factory = MockChromaDBFactory()
    real_chroma_client = asyncio.run(factory.get_client())

    test_container = create_test_container()
    test_container._instances["chromadb"] = real_chroma_client
    test_container._instances["semantic"] = ChromaSemanticService(
        client=real_chroma_client,
        default_collections=["semantic", "decisions"],
        manifest=test_container._instances["memory_manifest"],
    )
    dependencies.container = test_container
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_container()


def _override_identity(client: TestClient, user_id: str, scopes: tuple[str, ...]):
    """Same pattern as ``tests/test_memory_routes.py``'s helper of the same
    name (kept local rather than imported to avoid cross-test-module
    coupling). Caller MUST ``client.app.dependency_overrides.clear()``."""
    from audittrace.auth import require_user
    from audittrace.identity import UserContext

    identity = UserContext(
        user_id=user_id,
        username=user_id,
        agent_type="test",
        scopes=scopes,
        is_admin=False,
    )
    client.app.dependency_overrides[require_user] = lambda: identity
    return identity


def _fake_minio_get_object(contents: dict[str, bytes]) -> MagicMock:
    """A minimal MinIO double whose ``get_object`` returns the right bytes
    per key — several documents are folded per test, so (unlike the single-
    canary fixture in ``tests/test_memory_routes.py``) this dispatches on
    the key rather than hard-coding one expected call."""

    def get_object(_bucket: str, key: str) -> MagicMock:
        response = MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.read.return_value = contents[key]
        return response

    fake = MagicMock()
    fake.get_object.side_effect = get_object
    return fake


def _fold_markdown(
    client: TestClient,
    filename: str,
    content: bytes,
    *,
    layer: str = "episodic",
    collections: str = "decisions",
) -> dict[str, Any]:
    """Real ``POST /memory/upload`` -> ``POST /memory/index`` round-trip —
    the exact write path ``log_deploy_record`` drives. Returns the parsed
    ``POST /memory/index`` response body. Call once per document; each call
    patches ``_get_minio_client``/``embed_via_nomic`` for just its own two
    requests so multiple documents can be folded in one test without their
    MinIO doubles colliding."""
    with patch.object(m, "_get_minio_client", return_value=MagicMock()):
        up = client.post(
            "/memory/upload",
            params={"layer": layer, "filename": filename},
            files={"file": (filename, content, "text/markdown")},
        )
    assert up.status_code == 200, up.text
    key = up.json()["key"]

    fake_minio = _fake_minio_get_object({key: content})
    with (
        patch.object(m, "_get_minio_client", return_value=fake_minio),
        patch.object(
            m,
            "embed_via_nomic",
            AsyncMock(side_effect=lambda texts, **_: [[0.1, 0.2, 0.3] for _ in texts]),
        ),
    ):
        ix = client.post(
            "/memory/index", params={"file": key, "collections": collections}
        )
    assert ix.status_code == 200, ix.text
    body: dict[str, Any] = ix.json()
    body["_upload_key"] = key
    return body


# A big-enough body to span multiple ``_chunk_text`` chunks (CHUNK_SIZE=1500,
# CHUNK_OVERLAP=200) — computed from the real chunker, never hand-counted,
# so a chunking-parameter change can't silently desync this fixture from
# reality. UNIFORM by design (every char is 'x') — fine for the COUNT-only
# assertions in ``TestDocumentCountDedup``/``TestHonestPagination`` below,
# but round-1 review (F1) proved a uniform fixture is USELESS for proving
# byte-exact reconstruction: duplication, misordering and omission are all
# invisible when every position holds the same character. Whole-document
# CONTENT assertions use ``_DISTINCT_BODY`` instead — see its docstring.
_BIG_BODY = ("x" * 60 + "\n") * 120
_BIG_CHUNK_COUNT = len(_chunk_text(_BIG_BODY))

# WU-1 fix-round-2 (F1): every position in this fixture is UNIQUELY
# identifiable (a zero-padded running index), so duplication (a repeated
# index), misordering (indices out of sequence) and omission (a missing
# index) are all directly observable in the reconstructed text — unlike
# ``_BIG_BODY`` above. Long enough to span multiple ``_chunk_text`` chunks
# AND to exercise a truncated-tail chunk (see ``_reassemble_chunk_sequence``
# docstring for why the tail case matters).
_DISTINCT_BODY = "".join(f"[{i:06d}]\n" for i in range(1400))
_DISTINCT_CHUNK_COUNT = len(_chunk_text(_DISTINCT_BODY))


def _load_index_chromadb_module() -> Any:
    """Load ``scripts/index-chromadb.py`` by file path (the hyphen makes it
    unimportable as a dotted module — mirrors
    ``tests/test_scan_dlq_cli.py::_load_module`` for the sibling
    hyphenated-script pattern). No side effects at import time: the
    script's only top-level statements above ``if __name__ ==
    "__main__":`` are stdlib imports + constant/function definitions."""
    import importlib.util
    from importlib.machinery import SourceFileLoader
    from pathlib import Path

    path = Path(__file__).parent.parent / "scripts" / "index-chromadb.py"
    loader = SourceFileLoader("audittrace_index_chromadb", str(path))
    spec = importlib.util.spec_from_loader("audittrace_index_chromadb", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class TestChunkParameterAgreement:
    """WU-1 fix-round-3, F17: ``CHUNK_SIZE``/``CHUNK_OVERLAP`` are
    duplicated across ``routes/memory.py`` and ``scripts/index-chromadb.py``
    with only a "must match" COMMENT — no test previously pinned the two
    modules' values, or ``_reassemble_chunk_sequence``'s dependence on them
    actually agreeing. A drift here is silent corruption, not an error: a
    document chunked under one module's parameters and reassembled
    assuming the other's produces a wrong-but-plausible string, and
    ``validate_build_record`` (``scripts/deploy/build_record.py``) has no
    way to detect it from the corrupted text alone.
    """

    def test_chunk_size_and_overlap_match_across_both_indexers(self) -> None:
        """The actual falsifiable assert: if either module's constant is
        ever edited without the other, this test goes RED before any
        document is silently mis-reassembled."""
        legacy = _load_index_chromadb_module()
        assert m.CHUNK_SIZE == legacy.CHUNK_SIZE, (
            "routes/memory.py CHUNK_SIZE and scripts/index-chromadb.py "
            "CHUNK_SIZE have drifted apart — whole-document reassembly of "
            "a document chunked by one and read back assuming the other "
            "silently corrupts the result"
        )
        assert m.CHUNK_OVERLAP == legacy.CHUNK_OVERLAP, (
            "routes/memory.py CHUNK_OVERLAP and scripts/index-chromadb.py "
            "CHUNK_OVERLAP have drifted apart — _reassemble_chunk_sequence "
            "drops the wrong number of characters per chunk boundary, "
            "producing silent duplication or silent data loss"
        )

    def test_overlap_drift_silently_corrupts_reassembly(self) -> None:
        """Non-vacuity for the assert above: demonstrates WHY it matters,
        not just that the constants match today. A document chunked with
        ``overlap=300`` (simulating a stale/legacy indexer value that has
        drifted from today's) and reassembled assuming today's
        ``CHUNK_OVERLAP=200`` comes back a DIFFERENT length than the
        original — silently duplicated text, no exception raised — the
        exact failure mode F17 named. Measured on a 6750-char fixture:
        reassembles to 7250 chars (500 extra = 5 chunk boundaries under-
        dropping the 100-char difference each)."""
        body = "".join(f"[{i:06d}]\n" for i in range(750))  # 6750 chars
        chunks_drifted = _chunk_text(body, chunk_size=m.CHUNK_SIZE, overlap=300)
        assert len(chunks_drifted) > 1

        class _FakeDoc:
            def __init__(self, page_content: str) -> None:
                self.page_content = page_content

        reassembled = m._reassemble_chunk_sequence(
            [_FakeDoc(c) for c in chunks_drifted], overlap=m.CHUNK_OVERLAP
        )
        assert len(reassembled) == len(body) + 500
        assert reassembled != body, (
            "expected an overlap-parameter mismatch to silently duplicate "
            "text in the reassembled output instead of failing loudly"
        )


class TestDocumentCountDedup:
    """WU-1 item 1 — ``limit`` counts documents, not chunks."""

    def test_multi_chunk_document_lists_as_one_item_by_default(
        self, client: TestClient
    ) -> None:
        assert _BIG_CHUNK_COUNT > 1, (
            "fixture body must span >1 chunk for this test to mean anything"
        )
        result = _fold_markdown(client, "big-record.md", _BIG_BODY.encode())
        assert result["total_chunks"] == _BIG_CHUNK_COUNT

        r = client.get("/memory/semantic?collection=decisions")
        assert r.status_code == 200, r.text
        body = r.json()
        matches = [i for i in body["items"] if i["title"] == "big-record.md"]
        assert len(matches) == 1, (
            f"a {_BIG_CHUNK_COUNT}-chunk document must list as ONE item by "
            f"default (granularity=document): {body['items']}"
        )
        assert matches[0]["chunk_count"] == _BIG_CHUNK_COUNT

    def test_granularity_chunk_restores_the_pre_wu1_raw_view(
        self, client: TestClient
    ) -> None:
        """The Curator's contract (``scripts/curator/runner.py``) — one row
        per physical ChromaDB id — must still be reachable explicitly."""
        _fold_markdown(client, "big-record.md", _BIG_BODY.encode())

        r = client.get("/memory/semantic?collection=decisions&granularity=chunk")
        assert r.status_code == 200, r.text
        body = r.json()
        matches = [i for i in body["items"] if i["title"] == "big-record.md"]
        assert len(matches) == _BIG_CHUNK_COUNT, (
            f"granularity=chunk must list every chunk individually: {body['items']}"
        )

    def test_three_documents_with_different_chunk_counts_count_as_three(
        self, client: TestClient
    ) -> None:
        _fold_markdown(client, "big-record.md", _BIG_BODY.encode())
        _fold_markdown(client, "small-a.md", b"short body a")
        _fold_markdown(client, "small-b.md", b"short body b")

        r = client.get("/memory/semantic?collection=decisions")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total_documents"] == 3
        assert body["total_chunks"] == _BIG_CHUNK_COUNT + 2
        assert len(body["items"]) == 3

    def test_neutering_the_grouping_collapses_toward_chunk_count(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """WU-1 spec's own non-vacuity acceptance criterion: 'neuter the
        de-duplication -> the document count collapses back toward the
        chunk count -> restore.' ``monkeypatch`` guarantees the restore
        (teardown) half automatically — this is the RED half."""
        _fold_markdown(client, "big-record.md", _BIG_BODY.encode())
        _fold_markdown(client, "small-a.md", b"short body a")

        r_before = client.get("/memory/semantic?collection=decisions")
        assert r_before.status_code == 200, r_before.text
        assert r_before.json()["total_documents"] == 2

        # NEUTER: grouping becomes a no-op passthrough (the pre-fix shape).
        monkeypatch.setattr(m, "group_semantic_chunks_by_document", lambda items: items)

        r_neutered = client.get("/memory/semantic?collection=decisions")
        assert r_neutered.status_code == 200, r_neutered.text
        neutered_total = r_neutered.json()["total_documents"]
        assert neutered_total == _BIG_CHUNK_COUNT + 1, (
            "neutering the dedup must collapse the document count back to "
            f"the chunk count ({_BIG_CHUNK_COUNT + 1}), got {neutered_total}"
        )
        assert neutered_total > 2, (
            "the neutered count must be strictly WORSE (higher) than the "
            "fixed count — otherwise the neuter proved nothing"
        )
        # monkeypatch restores group_semantic_chunks_by_document on teardown;
        # the next test in this class sees the real function again.


class TestHonestPagination:
    """WU-1 item 3 — ``total_documents``/``total_chunks`` are both
    reported, distinct when they differ, and ``limit``/``offset`` page
    DOCUMENTS by default."""

    def test_total_documents_and_total_chunks_both_present_and_distinct(
        self, client: TestClient
    ) -> None:
        _fold_markdown(client, "big-record.md", _BIG_BODY.encode())
        _fold_markdown(client, "small-a.md", b"short body a")

        r = client.get("/memory/semantic?collection=decisions")
        body = r.json()
        assert body["total_documents"] == 2
        assert body["total_chunks"] == _BIG_CHUNK_COUNT + 1
        assert body["total_chunks"] != body["total_documents"]
        # The pre-WU-1 'total' field now tracks the ACTIVE view (documents
        # by default) — this is the exact confusion D15 named: a caller
        # reading 'total' alone must get the document count, not chunks.
        assert body["total"] == body["total_documents"]

    def test_limit_pages_documents_not_chunks(self, client: TestClient) -> None:
        _fold_markdown(client, "big-record.md", _BIG_BODY.encode())
        _fold_markdown(client, "small-a.md", b"short body a")
        _fold_markdown(client, "small-b.md", b"short body b")

        r = client.get("/memory/semantic?collection=decisions&limit=1")
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["items"]) == 1
        assert body["total_documents"] == 3, (
            "'total_documents' must report the TRUE full count even when "
            "'limit' truncates the page — otherwise a caller can't tell "
            "'4 documents total, page 1 of 4' from '4 documents, that's all'"
        )

    def test_default_granularity_field_is_document(self, client: TestClient) -> None:
        _fold_markdown(client, "small-a.md", b"short body a")
        r = client.get("/memory/semantic?collection=decisions")
        assert r.json()["granularity"] == "document"


class TestWholeDocumentRead:
    """WU-1 item 2 — ``?whole_document=true`` reconstructs chunk 0 onward
    regardless of which chunk id was requested."""

    def test_whole_document_joins_every_chunk_in_order(
        self, client: TestClient
    ) -> None:
        """WU-1 fix-round-2 (F1): asserts BYTE EQUALITY against the
        original text on a DISTINCT-CONTENT fixture (every position
        uniquely identifiable), not substring containment on a uniform
        fixture — round-1's ``chunk_text in body["content"]`` check on
        ``_BIG_BODY`` (all 'x's) could not have detected the round-1 bug
        (every chunk-overlap boundary duplicated: reconstructed length was
        `+200 chars per boundary` too long) because a repeated 'x' run
        looks identical to a correct one. This fixture would fail loudly
        under the round-1 defect: a duplicated overlap re-inserts an
        already-seen bracketed index, which breaks byte equality."""
        _fold_markdown(client, "distinct-record.md", _DISTINCT_BODY.encode())

        chunk_list = client.get(
            "/memory/semantic?collection=decisions&granularity=chunk"
        ).json()["items"]
        assert len(chunk_list) == _DISTINCT_CHUNK_COUNT
        assert _DISTINCT_CHUNK_COUNT > 1, (
            "fixture must span >1 chunk for this test to mean anything"
        )

        # Request the LAST chunk's id — the fix must still walk back to
        # chunk 0 and reconstruct the document byte-exact, not just
        # tail-serve the requested id.
        last_document_id = chunk_list[-1]["key"].split("/", 1)[1]
        r = client.get(
            f"/memory/semantic/decisions/{last_document_id}?whole_document=true"
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["chunk_count"] == _DISTINCT_CHUNK_COUNT
        assert len(body["chunk_keys"]) == _DISTINCT_CHUNK_COUNT
        assert body["content"] == _DISTINCT_BODY, (
            "whole_document=true must reconstruct the ORIGINAL bytes exactly "
            "(no duplicated overlap, no gap, no reordering)"
        )

    def test_whole_document_reconstructs_a_build_record_byte_exact(
        self, client: TestClient
    ) -> None:
        """Regression for round-1 F1: the round-1 bug's own worked example
        was THIS WU's build record itself ("a real obstacle to the Layer-2
        build-record verification every reviewer performs" — the
        docstring's stated use case). Mimics a real build record's shape
        (YAML front matter + several distinctly-worded Markdown sections)
        so duplication/misordering/omission are all observable, and reads
        it back whole-document, asserting byte equality end to end."""
        sections = [
            "---\nspec_ref: fixture-spec.md\nspec_hash: sha256:deadbeef\n---\n\n",
            "# Build record — regression fixture\n\n",
            "## Skills loaded\n\n" + ("PYTHON-ENGINEERING skill detail line.\n" * 25),
            "## What was built\n\n" + ("Implementation detail line.\n" * 25),
            "## Gates\n\n" + ("make test passed, coverage line.\n" * 25),
            "## Commit\n\nabc123def456 on a feature branch.\n",
        ]
        build_record_text = "".join(sections)
        assert len(_chunk_text(build_record_text)) > 1, (
            "fixture must span >1 chunk for this test to mean anything"
        )

        _fold_markdown(client, "regression-build-record.md", build_record_text.encode())
        chunk_list = client.get(
            "/memory/semantic?collection=decisions&granularity=chunk"
        ).json()["items"]
        matches = [i for i in chunk_list if i["title"] == "regression-build-record.md"]
        any_id = matches[0]["key"].split("/", 1)[1]

        r = client.get(f"/memory/semantic/decisions/{any_id}?whole_document=true")
        assert r.status_code == 200, r.text
        assert r.json()["content"] == build_record_text

    def test_whole_document_false_default_returns_single_chunk_unchanged(
        self, client: TestClient
    ) -> None:
        """Regression: the pre-WU-1 response shape is untouched when the
        new query param is omitted."""
        _fold_markdown(client, "big-record.md", _BIG_BODY.encode())
        chunk_list = client.get(
            "/memory/semantic?collection=decisions&granularity=chunk"
        ).json()["items"]
        first_id = chunk_list[0]["key"].split("/", 1)[1]

        r = client.get(f"/memory/semantic/decisions/{first_id}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "chunk_count" not in body
        assert "chunk_keys" not in body
        # 'first' in the LIST response is a sort-order artefact, not
        # necessarily chunk index 0 (list sorting ties break on key, not
        # chunk order) — the single-chunk read contract this test pins is
        # "you get exactly the ONE chunk you asked for", so assert
        # membership in the real chunk set, not a specific index.
        real_chunks = _chunk_text(_BIG_BODY)
        assert body["content"] in real_chunks

    def test_whole_document_on_untitled_row_degrades_to_single_chunk(
        self, client: TestClient
    ) -> None:
        """A direct write with no title cannot reconstruct a chunk
        sequence — must degrade gracefully, never error."""
        r = client.post(
            "/memory/semantic",
            json={
                "collection": "decisions",
                "document_id": "untitled-doc-1",
                "text": "no title, no chunk sequence",
            },
        )
        assert r.status_code == 200, r.text

        read = client.get(
            "/memory/semantic/decisions/untitled-doc-1?whole_document=true"
        )
        assert read.status_code == 200, read.text
        body = read.json()
        assert body["chunk_count"] == 1
        assert body["content"] == "no title, no chunk sequence"

    def test_whole_document_falls_back_when_deterministic_sequence_misses(
        self, client: TestClient
    ) -> None:
        """A row DOES carry a title/source, but its ``document_id`` predates
        (or otherwise doesn't match) the deterministic ``_doc_id`` hash
        scheme — chunk 0 of the recomputed sequence doesn't exist, so the
        walk finds nothing and must degrade to the single chunk actually
        found, never a 404/empty result for a row that demonstrably
        exists."""
        r = client.post(
            "/memory/semantic",
            json={
                "collection": "decisions",
                "document_id": "legacy-hash-mismatch",
                "text": "legacy content predating the hash scheme",
                "metadata": {"source": "legacy-file.md"},
            },
        )
        assert r.status_code == 200, r.text

        read = client.get(
            "/memory/semantic/decisions/legacy-hash-mismatch?whole_document=true"
        )
        assert read.status_code == 200, read.text
        body = read.json()
        assert body["chunk_count"] == 1
        assert body["content"] == "legacy content predating the hash scheme"
        assert body["chunk_keys"] == ["decisions/legacy-hash-mismatch"]

    def test_whole_document_falls_back_when_chunk_sequence_is_empty(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defensive branch: if the deterministic chunk-id sequence itself
        were ever empty (``_MAX_DOCUMENT_CHUNKS`` is a fixed positive
        constant today, so this cannot happen via the real function — this
        proves the CALLER handles it anyway rather than raising
        ``IndexError`` on an empty ``chunk_docs``)."""
        r = client.post(
            "/memory/semantic",
            json={
                "collection": "decisions",
                "document_id": "titled-doc",
                "text": "titled content",
                "metadata": {"source": "titled-file.md"},
            },
        )
        assert r.status_code == 200, r.text

        monkeypatch.setattr(m, "_iter_document_chunk_ids", lambda *_a, **_k: [])
        read = client.get("/memory/semantic/decisions/titled-doc?whole_document=true")
        assert read.status_code == 200, read.text
        body = read.json()
        assert body["chunk_count"] == 1
        assert body["content"] == "titled content"

    def test_whole_document_manifest_block_hidden_when_not_visible(
        self, client: TestClient
    ) -> None:
        """``whole_document=true``'s manifest block must respect the SAME
        ``_manifest_visible`` ownership rule as the single-chunk path: a
        content-level authorization pass (here, CORPUS-tier content ANY
        caller with the corpus-read scope may fetch) does not automatically
        make the MANIFEST row's authorship visible too — a documented
        pre-existing inconsistency guard (see ``_manifest_visible``'s
        docstring: a private-tier manifest row is 'operator-global, not
        per-user'). Constructs that inconsistency directly (owner folds
        the document normally, tier="private" end to end; the test then
        flips ONLY the ChromaDB-side tier metadata to "corpus" — a
        stand-in for the documented collision scenario — leaving the
        manifest row's own tier/authorship untouched) rather than through
        the ordinary write API, since the ordinary API keeps the two
        consistent by construction."""
        from audittrace.dependencies import get_chromadb

        title = "shared-record.md"
        _fold_markdown(client, title, b"owner-only content")

        chroma = get_chromadb()
        physical = asyncio.run(
            chroma.get_or_create_collection(
                name="decisions_v2", embedding_function=None
            )
        )
        for row in physical.data:
            row["metadata"]["tier"] = "corpus"

        chunk0_id = m._doc_id("decisions", title, 0)
        _override_identity(
            client,
            "someone-else",
            ("memory:semantic:read", "memory:corpus:decisions:read"),
        )
        try:
            r = client.get(
                f"/memory/semantic/decisions/{chunk0_id}?whole_document=true"
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["content"] == "owner-only content"
            assert body["manifest"] is None, (
                "the manifest block must be hidden for a caller who is "
                "neither the manifest row's owner nor the row's own "
                f"(unflipped) tier is corpus: {body}"
            )
        finally:
            client.app.dependency_overrides.clear()


class TestKeyShapeTrap:
    """WU-1 item 4 — a caller pasting the FULL upload/index key into the
    read route must not 404."""

    def test_full_upload_key_round_trips_through_read_episodic(
        self, client: TestClient
    ) -> None:
        up = client.post(
            "/memory/episodic",
            json={"filename": "ADR-key-shape.md", "content": "trap content"},
        )
        assert up.status_code == 200, up.text

        with patch.object(m, "_get_minio_client", return_value=MagicMock()):
            uploaded = client.post(
                "/memory/upload",
                params={"layer": "episodic", "filename": "ADR-key-shape-2.md"},
                files={
                    "file": ("ADR-key-shape-2.md", b"trap content 2", "text/markdown")
                },
            )
        assert uploaded.status_code == 200, uploaded.text
        full_key = uploaded.json()["key"]
        assert "/" in full_key, "the upload key must be the full sub-prefixed shape"

        # This 404'd before the WU-1 fix — the read route only accepted
        # the bare filename.
        r = client.get(f"/memory/episodic/{full_key}")
        assert r.status_code == 200, (
            f"pasting the upload key {full_key!r} into the read route must "
            f"not 404 (D15 item 4 — the key-shape trap): {r.text}"
        )
        assert r.json()["content"] == "trap content 2"

    def test_bare_filename_still_works_unchanged(self, client: TestClient) -> None:
        client.post(
            "/memory/episodic",
            json={"filename": "ADR-bare.md", "content": "bare content"},
        )
        r = client.get("/memory/episodic/ADR-bare.md")
        assert r.status_code == 200, r.text
        assert r.json()["content"] == "bare content"

    def test_layer_prefixed_key_without_sub_also_round_trips(
        self, client: TestClient
    ) -> None:
        client.post(
            "/memory/episodic",
            json={"filename": "ADR-layer-prefixed.md", "content": "layer-prefixed"},
        )
        r = client.get("/memory/episodic/episodic/ADR-layer-prefixed.md")
        assert r.status_code == 200, r.text
        assert r.json()["content"] == "layer-prefixed"

    def test_genuinely_missing_file_still_404s(self, client: TestClient) -> None:
        r = client.get("/memory/episodic/does-not-exist.md")
        assert r.status_code == 404

    def test_foreign_sub_prefix_400s_never_resolves_to_callers_own_file(
        self, client: TestClient
    ) -> None:
        """WU-1 fix-round-2 (F5): round-1's guards #10/#11 proved
        ``_strip_known_key_prefixes`` passes an UNRECOGNIZED prefix through
        unchanged (the documented ``pass through unchanged`` guarantee),
        but nothing exercised the CALLER-BOUND-PREFIX property that
        guarantee exists to protect: a plausible over-strip bug
        (``return raw.rsplit("/", 1)[-1]``) would leave every existing
        assertion green while silently resolving a FOREIGN-sub-prefixed
        key down to a bare filename — which the read route would then
        happily look up under the CALLER's OWN account. If the caller
        happens to own a file of that same name, the over-strip bug
        returns THAT file's content instead of 400, which is a
        content-confusion hole, not merely a missed-guard gap.

        Creates a same-named file under the CALLER's own account first, so
        a defective implementation that resolves the foreign key to "just
        the bare name under whoever is asking" would return live content
        (a false 200) rather than a 404 that could look accidentally
        correct."""
        own = client.post(
            "/memory/episodic",
            json={"filename": "victim.md", "content": "the CALLER's own content"},
        )
        assert own.status_code == 200, own.text

        foreign_key = "someone-else-entirely/episodic/victim.md"
        r = client.get(f"/memory/episodic/{foreign_key}")
        assert r.status_code == 400, (
            f"a foreign-sub-prefixed key must 400 (fail closed), never "
            f"silently resolve to the caller's own same-named file: "
            f"{r.status_code} {r.text}"
        )
        assert "the CALLER's own content" not in r.text

    def test_full_upload_key_round_trips_through_read_procedural(
        self, client: TestClient
    ) -> None:
        with patch.object(m, "_get_minio_client", return_value=MagicMock()):
            uploaded = client.post(
                "/memory/upload",
                params={"layer": "procedural", "filename": "SKILL-key-shape.md"},
                files={"file": ("SKILL-key-shape.md", b"skill trap", "text/markdown")},
            )
        assert uploaded.status_code == 200, uploaded.text
        full_key = uploaded.json()["key"]

        r = client.get(f"/memory/procedural/{full_key}")
        assert r.status_code == 200, r.text
        assert r.json()["content"] == "skill trap"
