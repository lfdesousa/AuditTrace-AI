"""AUDIT-ROW-TIER-DEFAULT-CORPUS ADDENDUM-2, WU-2 delta: RED tests (gate r2 C1).

Two properties ADDENDUM-1's write-through did not hold.

B1. A corpus FOLD is one document stored as N chunks, each with its own
manifest row. ``POST /memory/index`` stores them at the bare id in the
collection private chunks share (``<c>_v2``), tier-stamped ``corpus``; a corpus
``upsert`` writes another collection (``<c>_corpus_v2``). So ADDENDUM-1's
write-through over a fold chunk answered 200 ``corpus`` and stored a row no
by-id read returns; with the stamped chunks IN the corpus collection (the gate
r2 probe's arrangement) it broke the whole-document read of every reader. Now
such a ``PUT`` is refused (409) BEFORE any write, decided on the metadata
STORED in both collections, never on the request's.

S1. The tier an update writes was resolved by a second read, after the
authorization. A corpus row created between the two made an unauthorized
caller's content land in the corpus collection. Now the corpus write is
authorized against the tier it writes; the interleaving is injected, not timed.

Run on both manifest backends. Builder: copy unchanged to
``tests/test_memory_update_corpus_fold_chunk.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from audittrace.routes import memory as m
from audittrace.routes.memory_whole_document import STAMP_FOLD, STAMP_TOTAL
from audittrace.services.semantic import ChromaSemanticService
from tests.memory_truncation_support import (
    USER,
    _object_store,
    distinct_body,
    memory_client,
)
from tests.memory_truncation_support_a1 import STUB_VECTOR, manifest
from tests.test_memory_update_corpus_document import (
    BODY,
    CURATOR,
    KEY,
    LAYERS,
    READER,
    URL,
    as_caller,
    audit_rows,
    state,
    use_sql_manifest,
)

EDITOR = "00000000-0000-0000-0000-00000000e005"
SOURCE = "doc.md"
FOLD_TEXT = distinct_body(m.CHUNK_SIZE + (m.CHUNK_SIZE - m.CHUNK_OVERLAP) + 90)
CHUNKS = 3
WHOLE = [[200, True]] * CHUNKS
FOLD_REFUSAL = (
    "{id} is one chunk of the folded corpus document doc.md. Not supported: "
    "updating one chunk of a folded corpus document; re-fold the source."
)
NO_CONTENT_REFUSAL = (
    "doc-c is a corpus document with no content a PUT can replace. "
    "Not supported: updating it in place; re-create it."
)
SOFT_DELETED = (
    "manifest row for layer='semantic' key='decisions/doc-c' is soft-deleted; "
    "use record_create to revive"
)
UNAUTHORIZED = (
    "caller lacks shared-write authorization to write over an existing "
    "corpus-tier target layer={layer!r} key={key!r}"
)


@pytest.fixture(params=["mock", "sql"])
def client(request: pytest.FixtureRequest) -> Iterator[TestClient]:
    with memory_client() as c:
        if request.param == "sql":
            use_sql_manifest()
        yield c


def chunk_ids() -> list[str]:
    return [m._doc_id("decisions", SOURCE, i) for i in range(CHUNKS)]


SHARED, CORPUS = "decisions_v2", "decisions_corpus_v2"
Rows = dict[str, dict[str, tuple[str, dict[str, Any]]]]


def physical(name: str) -> Any:
    from audittrace import dependencies

    chroma = dependencies.container._instances["chromadb"]
    return asyncio.run(
        chroma.get_or_create_collection(name=name, embedding_function=None)
    )


def stored() -> Rows:
    """Every row of both physical collections: name -> id -> (text, metadata)."""
    assert SHARED == m._physical_collection("decisions")
    assert CORPUS == ChromaSemanticService._physical_corpus("decisions")
    out: Rows = {}
    for name in (SHARED, CORPUS):
        got = asyncio.run(physical(name).get(include=["documents", "metadatas"]))
        out[name] = {
            cid: (text, dict(meta))
            for cid, text, meta in zip(
                got["ids"], got["documents"], got["metadatas"], strict=True
            )
        }
    return out


def rewrite(name: str, rows: dict[str, tuple[str, dict[str, Any]]]) -> None:
    """Replace the rows of one physical collection on the double."""
    col = physical(name)
    present = asyncio.run(col.get(include=[]))["ids"]
    if present:
        asyncio.run(col.delete(ids=present))
    if rows:
        asyncio.run(
            col.upsert(
                ids=list(rows),
                documents=[text for text, _ in rows.values()],
                metadatas=[meta for _, meta in rows.values()],
                embeddings=[STUB_VECTOR for _ in rows],
            )
        )


def fold_corpus(client: TestClient) -> None:
    """A 3-chunk corpus fold of ``doc.md`` into ``decisions``, by the curator,
    through the product's own route. It lands where the route puts it."""
    assert len(m._chunk_text(FOLD_TEXT)) == CHUNKS
    store = _object_store({f"episodic/{SOURCE}": FOLD_TEXT.encode("utf-8")})
    with (
        as_caller(client, CURATOR, admin=True),
        patch.object(m, "_get_minio_client", return_value=store),
    ):
        folded = client.post(
            "/memory/index",
            params={"file": f"episodic/{SOURCE}", "collections": "decisions"},
        )
    assert folded.status_code == 200, folded.text
    rows = stored()
    assert (sorted(rows[SHARED]), rows[CORPUS]) == (sorted(chunk_ids()), {})
    assert {meta["tier"] for _, meta in rows[SHARED].values()} == {"corpus"}


def put_chunk(client: TestClient, target: str, *scopes: str, admin: bool) -> Any:
    with as_caller(client, EDITOR, *scopes, admin=admin):
        return client.put(
            f"/memory/semantic/decisions/{target}", json={"text": "edited"}
        )


def whole_reads(client: TestClient) -> list[list[Any]]:
    """``[status, verified]`` (or ``[status, detail]``) of the whole-document
    read through each chunk, by ANOTHER user holding the corpus read scope."""
    out: list[list[Any]] = []
    with as_caller(client, READER, "memory:corpus:decisions:read"):
        for cid in chunk_ids():
            got = client.get(
                f"/memory/semantic/decisions/{cid}", params={"whole_document": "true"}
            )
            body = got.json()
            if got.status_code == 200:
                assert body["content"] == FOLD_TEXT
                out.append([200, body["verified"]])
            else:
                out.append([got.status_code, body["detail"]])
    return out


def all_audit_rows() -> list[tuple[str, str]]:
    return [row for cid in chunk_ids() for row in audit_rows(cid)]


class TestUpdateOfACorpusFoldChunk:
    @pytest.mark.parametrize("index", range(CHUNKS))
    @pytest.mark.parametrize("caller", ["admin", "scope-holder"])
    def test_a_put_is_refused_and_every_reader_still_reads_the_whole_document(
        self, client: TestClient, caller: str, index: int
    ) -> None:
        fold_corpus(client)
        target = chunk_ids()[index]
        before, rows = stored(), all_audit_rows()
        assert whole_reads(client) == WHOLE
        extra = () if caller == "admin" else ("memory:corpus:decisions:write",)

        put = put_chunk(client, target, *extra, admin=caller == "admin")

        assert put.status_code == 409, put.text
        assert put.json() == {"detail": FOLD_REFUSAL.format(id=target)}
        assert whole_reads(client) == WHOLE
        assert stored() == before
        # Six whole reads (3 before, 3 after) and NO row for the refused PUT.
        assert all_audit_rows() == [*rows, *[("read", "corpus")] * (2 * CHUNKS)]

    def test_stamped_chunks_in_the_corpus_collection_are_refused_too(
        self, client: TestClient
    ) -> None:
        """The gate r2 probe's arrangement (B1): the fold's chunks sit in the
        corpus collection. No writer of the repo puts them there; a store that
        holds such rows must not be damaged either."""
        fold_corpus(client)
        rows = stored()
        rewrite(CORPUS, rows[SHARED])
        rewrite(SHARED, {})
        before = stored()
        assert whole_reads(client) == WHOLE
        target = chunk_ids()[0]

        put = put_chunk(client, target, admin=True)

        assert put.status_code == 409, put.text
        assert put.json() == {"detail": FOLD_REFUSAL.format(id=target)}
        assert whole_reads(client) == WHOLE
        assert stored() == before

    def test_a_fold_written_before_the_stamp_is_refused_by_its_source(
        self, client: TestClient
    ) -> None:
        """No ``chunk_total`` / ``fold_sha256``: the stored ``source`` decides."""
        fold_corpus(client)
        rewrite(
            SHARED,
            {
                cid: (
                    text,
                    {
                        k: v
                        for k, v in meta.items()
                        if k not in (STAMP_TOTAL, STAMP_FOLD)
                    },
                )
                for cid, (text, meta) in stored()[SHARED].items()
            },
        )
        before = stored()
        target = chunk_ids()[0]

        put = put_chunk(client, target, admin=True)

        assert put.status_code == 409, put.text
        assert put.json() == {"detail": FOLD_REFUSAL.format(id=target)}
        assert stored() == before

    def test_a_private_copy_left_by_an_earlier_put_does_not_hide_the_fold(
        self, client: TestClient
    ) -> None:
        """Before WU-2 an admin's PUT left a private chunk at the same logical
        id and no private manifest row. The refusal reads the corpus-tier rows,
        not the chunk the caller's own read would resolve first."""
        from audittrace.identity import UserContext
        from audittrace.services.memory_keys import ID_FORM_LC9, private_physical_id

        fold_corpus(client)
        target = chunk_ids()[0]
        editor = UserContext(
            user_id=EDITOR, username=EDITOR, agent_type="test", scopes=(), is_admin=True
        )
        asyncio.run(
            physical(SHARED).upsert(
                ids=[private_physical_id(editor, target)],
                documents=["shadow"],
                metadatas=[
                    {"user_id": EDITOR, "tier": "private", "id_form": ID_FORM_LC9}
                ],
                embeddings=[STUB_VECTOR],
            )
        )
        before = stored()

        put = put_chunk(client, target, admin=True)

        assert put.status_code == 409, put.text
        assert put.json() == {"detail": FOLD_REFUSAL.format(id=target)}
        assert stored() == before
        assert whole_reads(client) == WHOLE

    def test_a_corpus_row_with_no_stored_content_is_refused(
        self, client: TestClient
    ) -> None:
        """A corpus manifest row and nothing in the store: there is no content
        a PUT can replace, and an unreadable chunk is never read as unstamped."""
        asyncio.run(
            manifest().record_create(
                layer="semantic",
                key="decisions/doc-c",
                title="t",
                size_bytes=9,
                user_id=CURATOR,
                tier="corpus",
                caller_can_write_shared=True,
            )
        )

        with as_caller(client, EDITOR, admin=True):
            put = client.put(URL["semantic"], json={"text": "v2"})

        assert put.status_code == 409, put.text
        assert put.json() == {"detail": NO_CONTENT_REFUSAL}
        assert stored() == {SHARED: {}, CORPUS: {}}
        assert audit_rows("doc-c") == []

    def test_a_put_over_a_soft_deleted_corpus_document_writes_nothing(
        self, client: TestClient
    ) -> None:
        """The 409 ``record_update`` gave AFTER the content write is given
        before it: a tombstoned corpus document keeps its text."""
        with as_caller(client, CURATOR, admin=True):
            made = client.post(
                "/memory/semantic",
                params={"promote": "corpus"},
                json={"collection": "decisions", "document_id": "doc-c", "text": "v1"},
            )
            gone = client.delete(URL["semantic"])
        assert (made.status_code, gone.status_code) == (200, 200)
        before, rows = stored(), audit_rows("doc-c")

        with as_caller(client, EDITOR, admin=True):
            put = client.put(URL["semantic"], json={"text": "edited"})

        assert put.status_code == 409, put.text
        assert put.json() == {"detail": SOFT_DELETED}
        assert stored() == before
        assert audit_rows("doc-c") == rows

    def test_the_request_metadata_does_not_decide(self, client: TestClient) -> None:
        """An UNSTAMPED corpus document is still written through, whatever
        fold-shaped keys the request body carries."""
        with as_caller(client, CURATOR, admin=True):
            made = client.post(
                "/memory/semantic",
                params={"promote": "corpus"},
                json={"collection": "decisions", "document_id": "doc-c", "text": "v1"},
            )
        assert made.status_code == 200, made.text
        forged = {"source": SOURCE, "chunk_total": 3, "fold_sha256": "f" * 64}

        with as_caller(client, EDITOR, admin=True):
            put = client.put(URL["semantic"], json={"text": "v2", "metadata": forged})

        assert put.status_code == 200, put.text
        assert put.json()["tier"] == "corpus"
        rows = stored()
        text, meta = rows[CORPUS]["doc-c"]
        assert (text, meta["tier"], meta["user_id"]) == ("v2", "corpus", EDITOR)
        assert rows[SHARED] == {}

    def test_an_unauthorized_caller_is_403_not_the_fold_refusal(
        self, client: TestClient
    ) -> None:
        """Authorization is answered before the document's state."""
        fold_corpus(client)
        target, before = chunk_ids()[0], stored()
        put = client.put(f"/memory/semantic/decisions/{target}", json={"text": "x"})
        assert put.status_code == 403
        assert put.json() == {
            "detail": UNAUTHORIZED.format(layer="semantic", key=f"decisions/{target}")
        }
        assert stored() == before

    def test_control_the_owner_still_updates_a_chunk_of_a_private_fold(
        self, client: TestClient
    ) -> None:
        """The refusal is for the CORPUS tier only (operator ruling). What a
        PUT does to the caller's OWN private fold is unchanged by this item."""
        from tests.memory_truncation_support import fold

        made = client.post(
            "/memory/episodic", json={"filename": SOURCE, "content": FOLD_TEXT}
        )
        assert made.status_code == 200, made.text
        assert fold(client, "episodic", SOURCE, "decisions").status_code == 200
        target = chunk_ids()[0]
        put = client.put(f"/memory/semantic/decisions/{target}", json={"text": "mine"})
        assert put.status_code == 200, put.text
        assert put.json()["tier"] == "private"
        assert stored()[CORPUS] == {}


Step = Callable[..., Awaitable[None]]


def curator_creates_after_the_authorization(layer: str) -> Any:
    """Inject the interleaving: the real ``authorize_write`` runs and returns,
    THEN the curator's corpus document appears at the same key, and only then
    does the route go on. No clock, no thread: one awaited step."""
    real: Step = m.authorize_write

    async def create() -> None:
        from audittrace import dependencies

        if layer == "semantic":
            col = await dependencies.container._instances[
                "chromadb"
            ].get_or_create_collection(name=CORPUS, embedding_function=None)
            await col.upsert(
                ids=[KEY[layer]],
                documents=["corpus-v1"],
                metadatas=[{"user_id": CURATOR, "tier": "corpus"}],
                embeddings=[STUB_VECTOR],
            )
        else:
            dependencies.container._instances[layer].add_document(
                "corpus-v1", file=KEY[layer], tier="corpus"
            )
        await manifest().record_create(
            layer=layer,
            key=f"decisions/{KEY[layer]}" if layer == "semantic" else KEY[layer],
            title="t",
            size_bytes=9,
            user_id=CURATOR,
            tier="corpus",
            caller_can_write_shared=True,
        )

    async def authorize_then_create(*args: Any, **kwargs: Any) -> None:
        await real(*args, **kwargs)
        await create()

    return patch.object(m, "authorize_write", authorize_then_create)


@pytest.mark.parametrize("layer", LAYERS)
class TestCorpusWriteIsAuthorizedAgainstTheTierItWrites:
    def test_a_corpus_row_created_after_the_authorization_is_never_written(
        self, client: TestClient, layer: str
    ) -> None:
        key = f"decisions/{KEY[layer]}" if layer == "semantic" else KEY[layer]

        with curator_creates_after_the_authorization(layer):
            put = client.put(URL[layer], json={BODY[layer]: "user-v2"})

        assert put.status_code == 403, put.text
        assert put.json() == {"detail": UNAUTHORIZED.format(layer=layer, key=key)}
        # Nothing of the caller's landed anywhere: not in the corpus, and not
        # as a private copy that would shadow it for the caller.
        after = state(client, layer)
        assert after["content"] == "corpus-v1"
        assert (after["content_tier"], after["manifest_tier"]) == ("corpus", "corpus")
        assert after["manifest_modified_by"] == CURATOR
        assert after["private_chunks"] == {}
        if layer == "semantic":
            row = ("corpus-v1", {"user_id": CURATOR, "tier": "corpus"})
            assert stored() == {SHARED: {}, CORPUS: {KEY[layer]: row}}
        assert audit_rows(KEY[layer]) == [("read", "corpus")]

    def test_control_the_same_interleaving_with_the_authority_is_the_ruled_answer(
        self, client: TestClient, layer: str
    ) -> None:
        """An admin in the same interleaving gets what ADDENDUM-1 ruled for the
        layer: write-through on semantic, the file-layer refusal elsewhere."""
        with (
            as_caller(client, USER, admin=True),
            curator_creates_after_the_authorization(layer),
        ):
            put = client.put(URL[layer], json={BODY[layer]: "admin-v2"})
        expected = "admin-v2" if layer == "semantic" else "corpus-v1"
        assert put.status_code == (200 if layer == "semantic" else 409), put.text
        assert state(client, layer)["content"] == expected


PRIVATE_SOURCE = "private-plan.md"
STORED_ELSEWHERE_REFUSAL = (
    "doc-c is a corpus document stored where a PUT cannot replace it. "
    "Not supported: updating it in place."
)


class TestTheStoredRowsTheRefusalReads:
    """Gate r3 C1 (S1, S2): the two branches of the two-store predicate that
    the ratified cases left without a falsifier. Appended; the 40 cases above
    are the ratified file, unchanged."""

    def test_another_users_private_fold_chunk_is_not_a_corpus_fold_row(
        self, client: TestClient
    ) -> None:
        """S1 (cross-user disclosure). An admin promotes a document whose id is
        another user's PRIVATE chunk id (``<owner>__<chunk>``; nothing rejects
        ``__`` in a document id). The ``<c>_v2`` row at that bare id belongs to
        its owner: it carries the owner's ``source`` but it is not a corpus
        row, so it must neither refuse the PUT nor put the owner's filename in
        the answer. Without the shared-row filter the PUT is a 409 that names
        the private source and the corpus document is not updated."""
        from tests.memory_truncation_support import fold

        made = client.post(
            "/memory/episodic", json={"filename": PRIVATE_SOURCE, "content": "plan"}
        )
        assert made.status_code == 200, made.text
        assert fold(client, "episodic", PRIVATE_SOURCE, "decisions").status_code == 200
        (private_id,) = stored()[SHARED]
        assert private_id.startswith(f"{USER}__")
        own_chunk = f"/memory/semantic/decisions/{private_id.split('__', 1)[1]}"
        owner_read = client.get(own_chunk)
        assert (owner_read.status_code, owner_read.json()["content"]) == (200, "plan")
        owner_before = stored()[SHARED]
        assert owner_before[private_id][1]["source"] == PRIVATE_SOURCE
        with as_caller(client, CURATOR, admin=True):
            promoted = client.post(
                "/memory/semantic",
                params={"promote": "corpus"},
                json={
                    "collection": "decisions",
                    "document_id": private_id,
                    "text": "corpus-v1",
                },
            )
        assert promoted.status_code == 200, promoted.text

        with as_caller(client, EDITOR, admin=True):
            put = client.put(
                f"/memory/semantic/decisions/{private_id}", json={"text": "edited"}
            )

        assert put.status_code == 200, put.text
        assert put.json()["tier"] == "corpus"
        assert PRIVATE_SOURCE not in put.text
        rows = stored()
        assert rows[SHARED] == owner_before
        assert rows[CORPUS][private_id][0] == "edited"
        # The owner still reads its own chunk through the route, unchanged.
        owner_after = client.get(own_chunk)
        assert (owner_after.status_code, owner_after.json()["content"]) == (200, "plan")

    def test_a_corpus_row_readers_get_that_no_fold_wrote_is_refused_whole(
        self, client: TestClient
    ) -> None:
        """S2 (the identity pin). A corpus-tier row with no fold key at the bare
        id in ``<c>_v2`` is the row every reader gets, so a write-through to
        ``<c>_corpus_v2`` would be an orphan behind it: 409 before any write,
        both collections unchanged. The text does not advise a re-create (a
        create writes the same unread place): operator ruling of 2026-11-21."""
        with as_caller(client, CURATOR, admin=True):
            made = client.post(
                "/memory/semantic",
                params={"promote": "corpus"},
                json={"collection": "decisions", "document_id": "doc-c", "text": "v1"},
            )
        assert made.status_code == 200, made.text
        rewrite(
            SHARED, {"doc-c": ("shared-v1", {"tier": "corpus", "user_id": CURATOR})}
        )
        before, rows = stored(), audit_rows("doc-c")
        assert before[CORPUS]["doc-c"][0] == "v1"

        with as_caller(client, EDITOR, admin=True):
            put = client.put(URL["semantic"], json={"text": "edited"})

        assert put.status_code == 409, put.text
        assert put.json() == {"detail": STORED_ELSEWHERE_REFUSAL}
        assert stored() == before
        assert audit_rows("doc-c") == rows

    def test_a_legacy_private_row_at_a_corpus_id_is_not_a_corpus_fold_row(
        self, client: TestClient
    ) -> None:
        """B1 of review r1 (the legacy form of S1). A private fold chunk written
        before LC-9 sits at the bare id in ``<c>_v2`` with its owner's ``user_id``
        and ``source`` and NO ``tier`` and NO ``id_form``: ``new_form_owner``
        returns ``None`` for it, so only the ``tier == "corpus"`` condition of the
        shared-row filter keeps it out. Dropped, the editor's PUT is a 409 that
        names the owner's private source and the corpus document is not updated."""
        with as_caller(client, CURATOR, admin=True):
            made = client.post(
                "/memory/semantic",
                params={"promote": "corpus"},
                json={"collection": "decisions", "document_id": "doc-c", "text": "v1"},
            )
        assert made.status_code == 200, made.text
        legacy = {"user_id": USER, "source": PRIVATE_SOURCE, "chunk_index": 0}
        rewrite(SHARED, {"doc-c": ("owner text", legacy)})
        owner_before = stored()[SHARED]
        read_before = client.get(URL["semantic"])

        with as_caller(client, EDITOR, admin=True):
            put = client.put(URL["semantic"], json={"text": "edited"})

        assert put.status_code == 200, put.text
        assert put.json()["tier"] == "corpus"
        assert PRIVATE_SOURCE not in put.text
        rows = stored()
        assert rows[SHARED] == owner_before
        assert rows[CORPUS]["doc-c"][0] == "edited"
        read_after = client.get(URL["semantic"])
        assert (read_after.status_code, read_after.json()["content"]) == (
            read_before.status_code,
            read_before.json()["content"],
        )
