"""LC-9 WP2b -- the SEMANTIC routes, the promote-to-semantic target and the MCP
``write_*`` tools refuse cross-user private-row access, copy forward a caller's
own legacy row (manifest side + audit), and map the logical-key cap and the
concurrent-write race to clean statuses.

Everything goes through the REAL HTTP routes (``TestClient(create_app())``, two
subs through the repo's JWT-patch pattern, the REAL ``ChromaSemanticService`` on
the in-repo fake ChromaDB client) and runs the manifest half on BOTH backends
(the Mock twin and ``MemoryManifestService`` over aiosqlite).

RT -> class map (spec ``2026-10-26-SPEC-lc9-cross-user-private-row-isolation``
+ ADDENDUM-1 + brief ``2026-10-28-BRIEF-lc9-wp2b-semantic-mcp``):

* RT-1 / RT-12 / RT-25  ``TestTwoUsersSameDocument``
* RT-2                  ``TestForeignDeleteIsNotFound``
* RT-5a / RT-5b         ``TestCuratorCorpusAtAnOwnersKey``
* RT-10                 ``TestLegacyRows``
* RT-21                 ``TestCollectionFilteredList``
* S2 audit              ``TestCopyForwardAudit`` (REST, promote, MCP)
* RT-26a                ``TestLogicalKeyCapIs400`` (five sites)
* A2                    ``TestConcurrentWriteIs409`` (five sites)
* dead ternary          ``TestEpisodicProceduralCreateRejectsCorpusFirst``
* RT-22 and the real-postgres races: ``tests/test_lc9_wp2b_pg.py``

SCOPE NOTE (brief, "NOT in WP2b"): the ChromaDB physical id space is WP3's.
Until WP3 lands, two users writing the same ``document_id`` still share one
Chroma id, so "A reads own TEXT after B wrote the same id" (the content half of
RT-1) is a WP3 case (RT-7/8/11/19/23). WP2b owns the MANIFEST side: B's write
makes B's OWN row and A's row is untouched.

Neuters (each ALONE, ``PYTHONDONTWRITEBYTECODE=1``, restored with ``cmp``) are
listed in the build record's RT -> test -> neuter map.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from audittrace import dependencies
from audittrace.db.models import MemoryItem
from audittrace.identity import UserContext
from audittrace.services.memory_keys import (
    LOGICAL_KEY_MAX,
    ConcurrentManifestWriteError,
)
from audittrace.services.memory_manifest import (
    MemoryManifestService,
)

A, B, CURATOR = "user-a", "user-b", "curator-1"
BACKENDS = ("mock", "sql")
BASE = "memory:semantic:write memory:semantic:read"
CORPUS = " memory:corpus:decisions:write memory:corpus:decisions:read"
REFUSAL = "tier conflict: semantic key 'decisions/x' is not available for this write"


class _Auth:
    """Three patches bundled: a valid JWT for *sub* holding *scope*."""

    def __init__(self, sub: str, scope: str) -> None:
        self.sub, self.scope = sub, scope

    def __enter__(self) -> _Auth:
        self._patches = [
            patch("audittrace.auth.get_settings"),
            patch("audittrace.auth._get_jwks_keys"),
            patch("audittrace.auth._decode_jwt_with_allowed_issuers"),
        ]
        settings, jwks, decode = (p.__enter__() for p in self._patches)
        settings.return_value = MagicMock(auth_enabled=True, auth_required=True)
        jwks.return_value = ["fake-key"]
        decode.return_value = {"sub": self.sub, "scope": self.scope}
        return self

    def __exit__(self, *exc: Any) -> None:
        for p in reversed(self._patches):
            p.__exit__(*exc)


def _ctx(sub: str) -> UserContext:
    return UserContext(
        user_id=sub, username=sub, agent_type="test", scopes=(), is_admin=False
    )


class Rig:
    """A TestClient plus the manifest and the real semantic service behind it."""

    def __init__(self, client: TestClient, container: Any, kind: str) -> None:
        self.client, self.kind = client, kind
        self.manifest = container._instances["memory_manifest"]
        self.semantic = container._instances["semantic"]

    def call(
        self, method: str, url: str, sub: str, scope: str = BASE, **kw: Any
    ) -> Any:
        with _Auth(sub, scope):
            return self.client.request(
                method, url, headers={"Authorization": "Bearer t"}, **kw
            )

    def create(
        self,
        sub: str,
        doc: str,
        text: str = "body",
        *,
        collection: str = "decisions",
        scope: str = BASE,
        params: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Any:
        body = {"collection": collection, "document_id": doc, "text": text}
        body.update(extra or {})
        return self.call(
            "POST", "/memory/semantic", sub, scope, json=body, params=params
        )

    def update(
        self, sub: str, doc: str, text: str = "upd", *, collection: str = "decisions"
    ) -> Any:
        return self.call(
            "PUT",
            f"/memory/semantic/{collection}/{doc}",
            sub,
            json={"text": text},
        )

    def delete(self, sub: str, doc: str, *, collection: str = "decisions") -> Any:
        return self.call("DELETE", f"/memory/semantic/{collection}/{doc}", sub)

    def get(self, sub: str, doc: str, *, collection: str = "decisions") -> Any:
        return self.call("GET", f"/memory/semantic/{collection}/{doc}", sub)

    def listing(self, sub: str, **params: Any) -> list[dict[str, Any]]:
        r = self.call("GET", "/memory/semantic", sub, params=params)
        assert r.status_code == 200, r.text
        return r.json()["items"]

    def rows(self) -> dict[str, Any]:
        """Every semantic manifest row (tombstones included) by STORED key."""
        entries = asyncio.run(
            self.manifest.list_for_layer("semantic", include_deleted=True)
        )
        return {e.storage_key: e for e in entries}

    def seed_row(self, stored_key: str, owner: str, tier: str = "private") -> None:
        """A raw manifest row at an arbitrary STORED key (no Chroma chunk)."""
        now = int(time.time() * 1000)
        cols = dict(
            layer="semantic",
            key=stored_key,
            title="t",
            size_bytes=1,
            created_at_ms=now,
            modified_at_ms=now,
            created_by_user_id=owner,
            modified_by_user_id=owner,
            tier=tier,
        )
        if self.kind == "mock":
            self.manifest._rows[("semantic", stored_key)] = {
                "id": str(uuid.uuid4()),
                **cols,
                "deleted_at_ms": None,
                "deleted_by_user_id": None,
            }
            return

        async def _add() -> None:
            async with self.manifest._session_factory() as s:
                s.add(MemoryItem(**cols))
                await s.commit()

        asyncio.run(_add())

    def seed_legacy(self, key_doc: str, owner: str, *, collection: str = "decisions"):
        """A pre-LC-9 bare-key private row (+ the owner's Chroma chunk)."""
        key = f"{collection}/{key_doc}"
        now = int(time.time() * 1000) - 5000
        if self.kind == "mock":
            self.manifest._rows[("semantic", key)] = {
                "id": str(uuid.uuid4()),
                "layer": "semantic",
                "key": key,
                "title": "legacy",
                "size_bytes": 3,
                "created_at_ms": now,
                "modified_at_ms": now,
                "created_by_user_id": owner,
                "modified_by_user_id": owner,
                "deleted_at_ms": None,
                "deleted_by_user_id": None,
                "tier": "private",
            }
        else:

            async def _add() -> None:
                async with self.manifest._session_factory() as s:
                    s.add(
                        MemoryItem(
                            layer="semantic",
                            key=key,
                            title="legacy",
                            size_bytes=3,
                            created_at_ms=now,
                            modified_at_ms=now,
                            created_by_user_id=owner,
                            modified_by_user_id=owner,
                            tier="private",
                        )
                    )
                    await s.commit()

            asyncio.run(_add())
        asyncio.run(
            self.semantic.upsert(
                _ctx(owner), collection, key_doc, "legacy body", {}, tier="private"
            )
        )
        return now


@pytest.fixture(params=BACKENDS)
def rig(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Rig]:
    from audittrace.db.factory import MockChromaDBFactory
    from audittrace.services.semantic import ChromaSemanticService

    monkeypatch.setattr(
        "audittrace.services.semantic.embed_via_nomic",
        AsyncMock(side_effect=lambda texts, **_: [[0.1, 0.2, 0.3] for _ in texts]),
    )
    previous = dependencies.container
    container = dependencies.create_test_container()
    if request.param == "sql":
        from audittrace.db.postgres import InMemoryPostgresFactory

        container._instances["memory_manifest"] = MemoryManifestService(
            InMemoryPostgresFactory().get_session_factory()
        )
    client = asyncio.run(MockChromaDBFactory().get_client())
    container._instances["semantic"] = ChromaSemanticService(
        client=client, default_collections=["decisions", "skills", "semantic"]
    )
    dependencies.container = container
    from audittrace.server import create_app

    with TestClient(create_app()) as c:
        yield Rig(c, container, request.param)
    dependencies.container = previous


@pytest.fixture
def audits() -> Iterator[AsyncMock]:
    """Capture every audit event the routes emit (memory + promote + MCP)."""
    mock = AsyncMock()
    with (
        patch("audittrace.routes.memory.emit_memory_audit_event", mock),
        patch("audittrace.routes.memory_promote.emit_memory_audit_event", mock),
        patch("audittrace.tools.mcp_write_handlers.emit_memory_audit_event", mock),
    ):
        yield mock


def _ops(audits: AsyncMock) -> list[tuple[str, Any]]:
    return [(c.kwargs["op"], c.kwargs.get("detail_extra")) for c in audits.mock_calls]


def _conflict() -> ConcurrentManifestWriteError:
    return ConcurrentManifestWriteError("concurrent write; retry")


# ── RT-1 / RT-12 / RT-25 ────────────────────────────────────────────────────


class TestTwoUsersSameDocument:
    """RT-1 (manifest half): B writing A's semantic id makes B's OWN row; A's
    row is untouched. RT-12: the response ``key`` is the logical key sent."""

    def test_b_create_makes_a_second_row_a_untouched(self, rig: Rig) -> None:
        ra = rig.create(A, "x", "A-body")
        before = rig.rows()[f"{A}/decisions/x"]
        rb = rig.create(B, "x", "B-body")
        assert ra.status_code == rb.status_code == 200, (ra.text, rb.text)
        rows = rig.rows()
        assert set(rows) == {f"{A}/decisions/x", f"{B}/decisions/x"}
        a_row = rows[f"{A}/decisions/x"]
        assert a_row.modified_by_user_id == A  # RT-1: never B
        assert a_row.modified_at_ms == before.modified_at_ms
        assert a_row.size_bytes == len("A-body")
        assert rows[f"{B}/decisions/x"].created_by_user_id == B

    def test_create_response_key_is_the_logical_key(self, rig: Rig) -> None:
        """RT-12. Neuter: ``from_row`` / the Mock ``_to_entry`` returns the
        stored key -> RED."""
        ra, rb = rig.create(A, "x"), rig.create(B, "x")
        assert ra.json()["key"] == rb.json()["key"] == "decisions/x"
        assert not ra.json()["key"].startswith(f"{A}/")
        put = rig.update(A, "x")
        assert put.json()["key"] == "decisions/x"

    def test_b_update_never_reaches_a_row(self, rig: Rig) -> None:
        rig.create(A, "x", "A-body")
        before = rig.rows()[f"{A}/decisions/x"]
        rb = rig.update(B, "x", "B-hijack")
        assert rb.status_code == 404  # B has no row of its own: as if absent
        assert set(rig.rows()) == {f"{A}/decisions/x"}
        assert rig.rows()[f"{A}/decisions/x"].modified_at_ms == before.modified_at_ms
        # the 404 came BEFORE any Chroma write: A still reads its own text
        ga = rig.get(A, "x")
        assert ga.status_code == 200 and ga.json()["content"] == "A-body"
        assert ga.json()["manifest"]["modified_by_user_id"] == A

    def test_b_update_does_not_write_chroma_at_all(self, rig: Rig) -> None:
        rig.create(A, "x")
        with patch.object(rig.semantic, "upsert", AsyncMock()) as up:
            assert rig.update(B, "x").status_code == 404
        up.assert_not_called()

    def test_body_and_metadata_user_id_cannot_pick_the_prefix(self, rig: Rig) -> None:
        """RT-25. Neuter: derive the prefix / ``user_id=`` from the body or
        metadata -> RED (the row would land under ``user-a/``)."""
        r = rig.create(
            B,
            "x",
            extra={
                "user_id": A,
                "created_by_user_id": A,
                "metadata": {"user_id": A, "tier": "corpus", "k": "v"},
            },
        )
        assert r.status_code == 200, r.text
        rows = rig.rows()
        assert set(rows) == {f"{B}/decisions/x"}
        assert rows[f"{B}/decisions/x"].created_by_user_id == B
        assert rows[f"{B}/decisions/x"].tier == "private"
        meta = rig.get(B, "x").json()["metadata"]
        assert meta["user_id"] == B and meta["tier"] == "private" and meta["k"] == "v"

    def test_put_body_user_id_cannot_pick_the_prefix(self, rig: Rig) -> None:
        rig.create(B, "x")
        r = rig.call(
            "PUT",
            "/memory/semantic/decisions/x",
            B,
            json={"text": "t2", "user_id": A, "metadata": {"user_id": A}},
        )
        assert r.status_code == 200
        assert set(rig.rows()) == {f"{B}/decisions/x"}


# ── RT-2 ────────────────────────────────────────────────────────────────────


class TestForeignDeleteIsNotFound:
    def test_b_delete_404_a_keeps_item(self, rig: Rig) -> None:
        rig.create(A, "x")
        assert rig.delete(B, "x").status_code == 404
        row = rig.rows()[f"{A}/decisions/x"]
        assert row.deleted_at_ms is None and row.deleted_by_user_id is None
        assert [i["key"] for i in rig.listing(A, collection="decisions")] == [
            "decisions/x"
        ]

    def test_b_delete_of_a_legacy_key_404_legacy_untouched(self, rig: Rig) -> None:
        rig.seed_legacy("x", A)
        assert rig.delete(B, "x").status_code == 404
        assert rig.rows()["decisions/x"].deleted_at_ms is None

    def test_owner_delete_works(self, rig: Rig) -> None:
        rig.create(A, "x")
        assert rig.delete(A, "x").status_code == 200
        assert rig.rows()[f"{A}/decisions/x"].deleted_by_user_id == A


# ── RT-5a / RT-5b ───────────────────────────────────────────────────────────


class TestCuratorCorpusAtAnOwnersKey:
    def test_5a_new_form_row_a_corpus_row_beside_it_a_next_write_200(
        self, rig: Rig
    ) -> None:
        """RT-5a. Neuter: allow the tier re-stamp (a corpus write over a
        private row proceeds) -> RED (A's row would flip to corpus / A 403)."""
        assert rig.create(A, "x", "A-body").status_code == 200
        r = rig.create(
            CURATOR, "x", "curated", scope=BASE + CORPUS, params={"promote": "corpus"}
        )
        # the curator's corpus row lives at the BARE key; A's stays private
        assert r.status_code == 200, r.text
        rows = rig.rows()
        assert set(rows) == {f"{A}/decisions/x", "decisions/x"}
        assert rows[f"{A}/decisions/x"].tier == "private"
        assert rows["decisions/x"].tier == "corpus"
        assert rig.update(A, "x", "A-again").status_code == 200  # not locked out

    def test_5b_legacy_row_curator_403_one_text_a_untouched_a_copies_forward(
        self, rig: Rig
    ) -> None:
        rig.seed_legacy("x", A)
        r = rig.create(
            CURATOR, "x", "curated", scope=BASE + CORPUS, params={"promote": "corpus"}
        )
        assert r.status_code == 403
        assert r.json()["detail"] == REFUSAL  # ONE text, no owner named
        assert A not in r.text
        legacy = rig.rows()["decisions/x"]
        assert legacy.tier == "private" and legacy.deleted_at_ms is None
        assert legacy.modified_by_user_id == A
        # A's next write copies forward and works
        assert rig.update(A, "x", "A-next").status_code == 200
        rows = rig.rows()
        assert rows["decisions/x"].deleted_by_user_id == A
        assert rows[f"{A}/decisions/x"].deleted_at_ms is None


class TestForeignRowAtTheTargetedKey:
    """The fail-closed backstop: a private row at the key B's write TARGETS
    (``user-b/decisions/x``) that B did not create is refused 403 with the one
    text -- unreachable on Postgres after 034 (CHECK), so it runs on Mock and
    aiosqlite. Neuter: treat the ``foreign`` outcome as ``own`` -> B's POST
    overwrites A's row (RED)."""

    def test_b_post_is_refused_a_row_untouched(self, rig: Rig) -> None:
        rig.seed_row(f"{B}/decisions/x", A)
        r = rig.create(B, "x")
        assert r.status_code == 403
        assert r.json()["detail"] == REFUSAL and A not in r.text
        row = rig.rows()[f"{B}/decisions/x"]
        assert row.created_by_user_id == A and row.modified_by_user_id == A


# ── RT-10 ───────────────────────────────────────────────────────────────────


class TestLegacyRows:
    """RT-10: legacy bare rows seeded for A (manifest + Chroma)."""

    def test_a_get_200_b_get_404(self, rig: Rig) -> None:
        """Neuter: the dual-lookup owner equality accepts any legacy owner ->
        B's GET returns A's manifest (RED)."""
        rig.seed_legacy("x", A)
        ra, rb = rig.get(A, "x"), rig.get(B, "x")
        assert ra.status_code == 200 and ra.json()["content"] == "legacy body"
        assert ra.json()["manifest"]["created_by_user_id"] == A
        assert rb.status_code == 404

    def test_b_put_never_copies_a_row(self, rig: Rig) -> None:
        """Neuter: treat the ``foreign`` outcome as ``own`` -> B's PUT copies
        A's legacy row (RED)."""
        rig.seed_legacy("x", A)
        rb = rig.update(B, "x", "B-hijack")
        assert rb.status_code == 404
        rows = rig.rows()
        assert set(rows) == {"decisions/x"}
        assert rows["decisions/x"].deleted_at_ms is None
        assert rows["decisions/x"].modified_by_user_id == A
        # nothing of B's reached Chroma either: A still reads its own body
        assert rig.get(A, "x").json()["content"] == "legacy body"

    def test_b_put_over_legacy_equals_b_put_over_absent(self, rig: Rig) -> None:
        rig.seed_legacy("x", A)
        over_legacy, over_absent = rig.update(B, "x"), rig.update(B, "nothing")
        assert over_legacy.status_code == over_absent.status_code == 404
        assert (
            over_legacy.json()["detail"].replace("decisions/x", "decisions/nothing")
            == over_absent.json()["detail"]
        )

    def test_a_put_copies_forward(self, rig: Rig) -> None:
        created = rig.seed_legacy("x", A)
        r = rig.update(A, "x", "A-new")
        assert r.status_code == 200 and r.json()["key"] == "decisions/x"
        rows = rig.rows()
        assert rows["decisions/x"].deleted_by_user_id == A  # tombstoned by owner
        new = rows[f"{A}/decisions/x"]
        assert new.created_at_ms == created  # history continuous
        assert new.deleted_at_ms is None
        got = rig.get(A, "x")
        assert got.status_code == 200 and got.json()["content"] == "A-new"

    def test_a_post_copies_forward_too(self, rig: Rig) -> None:
        created = rig.seed_legacy("x", A)
        assert rig.create(A, "x", "A-new").status_code == 200
        rows = rig.rows()
        assert rows["decisions/x"].deleted_by_user_id == A
        assert rows[f"{A}/decisions/x"].created_at_ms == created

    def test_b_post_over_a_legacy_key_is_a_new_row_a_legacy_intact(
        self, rig: Rig
    ) -> None:
        rig.seed_legacy("x", A)
        assert rig.create(B, "x").status_code == 200
        rows = rig.rows()
        assert set(rows) == {"decisions/x", f"{B}/decisions/x"}
        assert rows["decisions/x"].deleted_at_ms is None
        assert rows["decisions/x"].modified_by_user_id == A


# ── RT-21 ───────────────────────────────────────────────────────────────────


class TestCollectionFilteredList:
    """RT-21 / reader #9: ``list_semantic``'s ``startswith(f"{collection}/")``
    filter must keep new owner-prefixed private rows. Neuter: ``from_row`` /
    ``_to_entry`` returns the stored key -> the A-prefixed key no longer starts
    with ``decisions/`` and the row drops out (RED)."""

    def test_a_new_row_is_listed_once_under_its_logical_key(self, rig: Rig) -> None:
        rig.create(A, "x")
        items = rig.listing(A, collection="decisions")
        assert [i["key"] for i in items] == ["decisions/x"]

    def test_other_users_row_is_not_listed(self, rig: Rig) -> None:
        rig.create(A, "x")
        assert rig.listing(B, collection="decisions") == []

    def test_legacy_row_and_after_copy_forward_one_live_item(self, rig: Rig) -> None:
        rig.seed_legacy("x", A)
        assert [i["key"] for i in rig.listing(A, collection="decisions")] == [
            "decisions/x"
        ]
        assert rig.update(A, "x").status_code == 200
        assert [i["key"] for i in rig.listing(A, collection="decisions")] == [
            "decisions/x"
        ]

    def test_other_collection_filter_excludes_it(self, rig: Rig) -> None:
        rig.create(A, "x")
        assert rig.listing(A, collection="skills") == []


# ── S2 audit: REST, promote, MCP ────────────────────────────────────────────


class TestCopyForwardAudit:
    """ADDENDUM-1 S2 (the audit half WP2b owns): one ``_emit_write_audit`` per
    manifest mutation -- the write with ``copied_from``, the legacy tombstone
    with ``reason=copy_forward``; a plain write has neither."""

    def test_put_over_own_legacy_audits_both_mutations(
        self, rig: Rig, audits: AsyncMock
    ) -> None:
        rig.seed_legacy("x", A)
        assert rig.update(A, "x").status_code == 200
        assert _ops(audits) == [
            ("write", {"copied_from": "decisions/x"}),
            ("delete", {"reason": "copy_forward"}),
        ]
        assert {c.kwargs["collection"] for c in audits.mock_calls} == {"decisions"}
        assert {c.kwargs["key"] for c in audits.mock_calls} == {"x"}

    def test_post_over_own_legacy_audits_both_mutations(
        self, rig: Rig, audits: AsyncMock
    ) -> None:
        rig.seed_legacy("x", A)
        assert rig.create(A, "x").status_code == 200
        assert [op for op, _ in _ops(audits)] == ["write", "delete"]

    def test_plain_create_and_second_write_have_no_copy_audit(
        self, rig: Rig, audits: AsyncMock
    ) -> None:
        assert rig.create(A, "fresh").status_code == 200
        assert rig.update(A, "fresh").status_code == 200
        assert _ops(audits) == [("write", None), ("write", None)]

    def test_b_never_triggers_a_copy_forward_audit(
        self, rig: Rig, audits: AsyncMock
    ) -> None:
        rig.seed_legacy("x", A)
        assert rig.create(B, "x").status_code == 200
        assert _ops(audits) == [("write", None)]

    def test_audit_failure_is_a_500_not_a_silent_200(
        self, rig: Rig, audits: AsyncMock
    ) -> None:
        rig.seed_legacy("x", A)
        audits.side_effect = RuntimeError("audit store down")
        assert rig.update(A, "x").status_code == 500


# ── promote to semantic ─────────────────────────────────────────────────────


class TestPromoteToSemantic:
    def _upload(self, rig: Rig, sub: str, name: str) -> None:
        with (
            _Auth(sub, "memory:session:write"),
            patch("audittrace.routes.memory._get_minio_client", MagicMock()),
        ):
            r = rig.client.post(
                "/memory/upload",
                params={"layer": "session"},
                files={"file": (name, b"scratch", "text/plain")},
                headers={"Authorization": "Bearer t"},
            )
        assert r.status_code == 200, r.text

    def _promote(self, rig: Rig, sub: str, name: str) -> Any:
        with _Auth(sub, "memory:semantic:write memory:session:write"):
            return rig.client.post(
                "/memory/promote",
                json={"filename": name, "target_layer": "semantic"},
                headers={"Authorization": "Bearer t"},
            )

    def test_two_users_two_disjoint_rows(self, rig: Rig) -> None:
        for sub in (A, B):
            self._upload(rig, sub, "n.md")
            assert self._promote(rig, sub, "n.md").status_code == 200
        assert set(rig.rows()) == {
            f"{A}/semantic/{A}__n.md",
            f"{B}/semantic/{B}__n.md",
        }

    def test_promote_over_own_legacy_copies_forward_and_audits(
        self, rig: Rig, audits: AsyncMock
    ) -> None:
        created = rig.seed_legacy(f"{A}__n.md", A, collection="semantic")
        self._upload(rig, A, "n.md")
        r = self._promote(rig, A, "n.md")
        assert r.status_code == 200, r.text
        assert r.json()["key"] == f"semantic/{A}__n.md"
        rows = rig.rows()
        assert rows[f"semantic/{A}__n.md"].deleted_by_user_id == A
        assert rows[f"{A}/semantic/{A}__n.md"].created_at_ms == created
        ops = _ops(audits)
        assert ("delete", {"reason": "copy_forward"}) in ops
        assert any(
            op == "write" and (d or {}).get("copied_from") == f"semantic/{A}__n.md"
            for op, d in ops
        )

    def test_promote_key_over_cap_is_400(self, rig: Rig) -> None:
        # "semantic/" + "user-a__" + name must exceed LOGICAL_KEY_MAX
        name = "n" * (LOGICAL_KEY_MAX - len(f"semantic/{A}__") - 3 + 1) + ".md"
        assert len(f"semantic/{A}__{name}") == LOGICAL_KEY_MAX + 1
        self._upload(rig, A, name)
        r = self._promote(rig, A, name)
        assert r.status_code == 400
        assert str(LOGICAL_KEY_MAX) in r.json()["detail"]
        assert rig.rows() == {}

    def test_promote_concurrent_write_is_409(self, rig: Rig) -> None:
        self._upload(rig, A, "n.md")
        with patch.object(
            rig.manifest, "record_create", AsyncMock(side_effect=_conflict())
        ):
            r = self._promote(rig, A, "n.md")
        assert r.status_code == 409
        assert r.headers["Retry-After"] == "1"


# ── RT-26a: the cap at the five sites ───────────────────────────────────────


def _doc(n: int) -> str:
    """A document id whose logical key ``decisions/<id>`` is *n* chars long."""
    return "d" * (n - len("decisions/"))


class TestLogicalKeyCapIs400:
    over = _doc(LOGICAL_KEY_MAX + 1)
    at_cap = _doc(LOGICAL_KEY_MAX)

    def test_lengths(self) -> None:
        assert len(f"decisions/{self.over}") == LOGICAL_KEY_MAX + 1
        assert len(f"decisions/{self.at_cap}") == LOGICAL_KEY_MAX

    def test_create_219_400_218_200(self, rig: Rig) -> None:
        """Site #8. Neuter: remove the 400 mapper from ``create_semantic`` ->
        a 500 (RED)."""
        r = rig.create(A, self.over)
        assert r.status_code == 400
        assert r.json()["detail"] == f"key longer than {LOGICAL_KEY_MAX} characters"
        assert rig.create(A, self.at_cap).status_code == 200
        assert list(rig.rows()) == [f"{A}/decisions/{self.at_cap}"]

    def test_update_219_is_400(self, rig: Rig) -> None:
        """Site #9. Neuter: remove the 400 mapper from ``update_semantic``."""
        assert rig.update(A, self.over).status_code == 400

    def test_delete_219_is_400(self, rig: Rig) -> None:
        """Site #10. Neuter: remove the 400 mapper from ``delete_semantic``."""
        assert rig.delete(A, self.over).status_code == 400

    def test_corpus_tier_key_is_capped_too(self, rig: Rig) -> None:
        """A corpus key is stored BARE, so ``physical_key`` never sees it: the
        cap must come from ``semantic_logical_key``. Neuter: drop the cap from
        ``semantic_logical_key`` -> a 500 (String(255) overflow) or a 200."""
        over = "d" * 300
        r = rig.create(CURATOR, over, scope=BASE + CORPUS, params={"promote": "corpus"})
        assert r.status_code == 400

    def test_36_char_sub_at_the_cap_is_200_and_stores_255(self, rig: Rig) -> None:
        sub = "a" * 36
        assert rig.create(sub, self.at_cap).status_code == 200
        assert len(next(iter(rig.rows()))) == 255


class TestMcpCapAndRace:
    """Sites #14: the MCP ``write_decision`` surfaces the cap and the race as a
    tool-level ``{"error"}`` (``isError: true``), the shape every other known-bad
    input already uses; no JSON-RPC error code and no schema change."""

    def _call(
        self, rig: Rig, sub: str, doc: str, text: str = "t", tool="write_decision"
    ) -> Any:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool,
                "arguments": {"document_id": doc, "text": text},
            },
        }
        return rig.call(
            "POST", "/mcp", sub, "memory:decisions:write memory:skills:write", json=body
        )

    @pytest.fixture(autouse=True)
    def _registries(self) -> Iterator[None]:
        import importlib

        import audittrace.tools.memory_handlers as mh
        from audittrace.tools import mcp_write_handlers as wh
        from audittrace.tools import reset_registry_for_tests
        from audittrace.tools.mcp_write_registry import (
            reset_mcp_write_registry_for_tests,
        )

        reset_registry_for_tests()
        importlib.reload(mh)
        reset_mcp_write_registry_for_tests()
        importlib.reload(wh)
        yield
        reset_registry_for_tests()
        reset_mcp_write_registry_for_tests()

    def test_ok_write_makes_an_owner_keyed_row(self, rig: Rig) -> None:
        r = self._call(rig, A, "x")
        assert r.status_code == 200 and r.json()["result"]["isError"] is False
        assert set(rig.rows()) == {f"{A}/decisions/x"}

    def test_two_users_same_document_two_rows(self, rig: Rig) -> None:
        assert self._call(rig, A, "x").json()["result"]["isError"] is False
        assert self._call(rig, B, "x").json()["result"]["isError"] is False
        assert set(rig.rows()) == {f"{A}/decisions/x", f"{B}/decisions/x"}

    def test_cap_over_is_a_tool_error_at_cap_is_ok(self, rig: Rig) -> None:
        """Site #14 (400). Neuter: drop the ``LogicalKeyTooLongError`` mapper in
        ``_write_semantic_document`` -> the error propagates (bridge wraps it as
        a class-name error with NO cap text, RED)."""
        bad = self._call(rig, A, TestLogicalKeyCapIs400.over).json()["result"]
        assert bad["isError"] is True
        assert f"key longer than {LOGICAL_KEY_MAX} characters" in str(bad["content"])
        assert rig.rows() == {}
        ok = self._call(rig, A, TestLogicalKeyCapIs400.at_cap).json()["result"]
        assert ok["isError"] is False

    def test_race_is_a_retryable_tool_error(self, rig: Rig) -> None:
        """Site #14 (409). Neuter: drop the ``ConcurrentManifestWriteError``
        mapper -> the bridge reports the bare class name, no 'retry' (RED)."""
        with patch.object(
            rig.manifest, "record_create", AsyncMock(side_effect=_conflict())
        ):
            res = self._call(rig, A, "x").json()["result"]
        assert res["isError"] is True
        assert "retry" in str(res["content"])
        assert "ConcurrentManifestWriteError" not in str(res["content"])

    def test_copy_forward_audits_both_mutations(
        self, rig: Rig, audits: AsyncMock
    ) -> None:
        rig.seed_legacy("x", A)
        assert self._call(rig, A, "x").json()["result"]["isError"] is False
        assert _ops(audits) == [
            ("write", {"copied_from": "decisions/x"}),
            ("delete", {"reason": "copy_forward"}),
        ]
        assert rig.rows()["decisions/x"].deleted_by_user_id == A

    def test_audit_failure_after_copy_forward_is_reported(
        self, rig: Rig, audits: AsyncMock
    ) -> None:
        rig.seed_legacy("x", A)
        audits.side_effect = RuntimeError("audit down")
        res = self._call(rig, A, "x").json()["result"]
        assert res["isError"] is True
        assert "audit" in str(res["content"])


# ── A2: the race at the five sites ──────────────────────────────────────────


class TestConcurrentWriteIs409:
    def _assert_409(self, r: Any) -> None:
        assert r.status_code == 409
        assert r.headers["Retry-After"] == "1"
        assert "retry" in r.json()["detail"]

    def test_create(self, rig: Rig) -> None:
        """Site #8. Neuter: remove the 409 mapper (or make it return 500, or
        drop ``Retry-After``) -> RED."""
        with patch.object(
            rig.manifest, "record_create", AsyncMock(side_effect=_conflict())
        ):
            self._assert_409(rig.create(A, "x"))

    def test_update(self, rig: Rig) -> None:
        """Site #9."""
        rig.create(A, "x")
        with patch.object(
            rig.manifest, "record_update", AsyncMock(side_effect=_conflict())
        ):
            self._assert_409(rig.update(A, "x"))

    def test_delete(self, rig: Rig) -> None:
        """Site #10. ``record_delete`` cannot lose the race today (it never
        inserts); the route contract maps it anyway."""
        rig.create(A, "x")
        with patch.object(
            rig.manifest, "record_delete", AsyncMock(side_effect=_conflict())
        ):
            self._assert_409(rig.delete(A, "x"))

    def test_409_is_not_a_500_for_the_authorization_mapping(self, rig: Rig) -> None:
        """Only the race is softened: an authorization refusal stays 403."""
        rig.seed_legacy("x", A)
        r = rig.create(CURATOR, "x", scope=BASE + CORPUS, params={"promote": "corpus"})
        assert r.status_code == 403


# ── the dead ternary (episodic / procedural create) ─────────────────────────


@pytest.mark.parametrize("layer", ["episodic", "procedural"])
class TestEpisodicProceduralCreateRejectsCorpusFirst:
    """WP2a left ``... if tier == "private" else None`` on the create-probe.
    ``_reject_corpus_promotion_for`` already 400s ``tier=corpus`` before any
    manifest call, so ``tier`` is always ``"private"`` there and the ternary was
    dead. Proof: corpus is refused with no manifest touch; a private create
    copy-forwards (the probe runs unconditionally)."""

    def _post(self, rig: Rig, layer: str, **extra: Any) -> Any:
        return rig.call(
            "POST",
            f"/memory/{layer}",
            A,
            f"memory:{layer}:write memory:{layer}:read",
            json={"filename": "n.md", "content": "c", **extra},
        )

    def test_corpus_is_400_before_any_manifest_call(self, rig: Rig, layer: str) -> None:
        with (
            patch.object(rig.manifest, "record_create", AsyncMock()) as rc,
            patch.object(rig.manifest, "resolve_read", AsyncMock()) as rr,
        ):
            r = self._post(rig, layer, tier="corpus")
        assert r.status_code == 400
        rc.assert_not_called()
        rr.assert_not_called()

    def test_private_create_runs_the_probe(self, rig: Rig, layer: str) -> None:
        real = rig.manifest.resolve_read
        with patch.object(rig.manifest, "resolve_read", AsyncMock(wraps=real)) as probe:
            assert self._post(rig, layer).status_code == 200
        assert probe.await_count >= 1
