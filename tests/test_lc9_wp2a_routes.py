"""LC-9 WP2a -- the episodic and procedural ROUTES refuse cross-user private-row
access, copy forward a caller's own legacy row, and map the logical-key cap and
the concurrent-write race to clean statuses.

Everything here goes through the REAL HTTP routes (``TestClient(create_app())``,
two subs through the repo's JWT-patch pattern) and runs the manifest half on
BOTH backends (the Mock twin and ``MemoryManifestService`` over aiosqlite).

RT -> class map (spec ``2026-10-26-SPEC-lc9-cross-user-private-row-isolation``
+ ADDENDUM-1 + brief ``2026-10-27-BRIEF-lc9-wp2a``):

* RT-3 / RT-6   ``TestTwoUsersSameFilename`` (episodic / procedural create+update)
* RT-4 / RT-6   ``TestForeignDeleteIsNotFound`` (episodic / procedural delete)
* RT-9          ``TestPromoteToEpisodic`` (durable name = another user's file)
* RT-20 / RT-20b ``TestListOneItemPerDocument`` (episodic / procedural; reader #7)
* RT-26a        ``TestLogicalKeyCapIs400`` (219 -> 400, 218 -> 200, every route)
* S2 copy-forward ``TestCopyForwardAtTheRoute`` (+ audit rows)
* A2            ``TestConcurrentWriteIs409``
* A1            ``TestPdfMetadataForeignPrivateRow`` (service level)

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
from audittrace.services.memory_keys import (
    LOGICAL_KEY_MAX,
    ConcurrentManifestWriteError,
    CrossOwnerWriteError,
)
from audittrace.services.memory_manifest import (
    MemoryManifestService,
    MockMemoryManifestService,
    _is_unique_violation,
)

A, B = "user-a", "user-b"
LAYERS = ("episodic", "procedural")
BACKENDS = ("mock", "sql")


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


def _scope(layer: str) -> str:
    return f"memory:{layer}:write memory:{layer}:read"


class Rig:
    """A TestClient plus the manifest/service doubles behind it."""

    def __init__(self, client: TestClient, manifest: Any, kind: str) -> None:
        self.client, self.manifest, self.kind = client, manifest, kind

    def call(self, method: str, url: str, sub: str, layer: str, **kw: Any) -> Any:
        with _Auth(sub, _scope(layer)):
            return self.client.request(
                method, url, headers={"Authorization": "Bearer t"}, **kw
            )

    def create(self, sub: str, layer: str, filename: str, content: str = "c") -> Any:
        return self.call(
            "POST",
            f"/memory/{layer}",
            sub,
            layer,
            json={"filename": filename, "content": content, "title": filename},
        )

    def update(self, sub: str, layer: str, filename: str, content: str = "u") -> Any:
        return self.call(
            "PUT",
            f"/memory/{layer}/{filename}",
            sub,
            layer,
            json={"content": content},
        )

    def delete(self, sub: str, layer: str, filename: str) -> Any:
        return self.call("DELETE", f"/memory/{layer}/{filename}", sub, layer)

    def rows(self, layer: str) -> dict[str, Any]:
        """Every manifest row (tombstones included) by STORED key."""
        entries = asyncio.run(self.manifest.list_for_layer(layer, include_deleted=True))
        return {e.storage_key: e for e in entries}

    def seed_legacy(
        self, layer: str, key: str, owner: str, *, with_doc: bool = True
    ) -> int:
        """A pre-LC-9 bare-key private row (+ the owner's S3 object)."""
        now = int(time.time() * 1000) - 5000
        if self.kind == "mock":
            self.manifest._rows[(layer, key)] = {
                "id": str(uuid.uuid4()),
                "layer": layer,
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
                            layer=layer,
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
        if with_doc:
            svc = (
                dependencies.get_episodic_service()
                if layer == "episodic"
                else dependencies.get_procedural_service()
            )
            svc.add_document(  # type: ignore[attr-defined]
                "legacy body",
                file=key,
                tier="private",
                user_id=owner,
            )
        return now


@pytest.fixture(params=BACKENDS)
def rig(request: pytest.FixtureRequest) -> Iterator[Rig]:
    container = dependencies.create_test_container()
    if request.param == "sql":
        from audittrace.db.postgres import InMemoryPostgresFactory

        container._instances["memory_manifest"] = MemoryManifestService(
            InMemoryPostgresFactory().get_session_factory()
        )
    dependencies.container = container
    from audittrace.server import create_app

    with TestClient(create_app()) as c:
        yield Rig(c, container._instances["memory_manifest"], request.param)


@pytest.fixture
def audits() -> Iterator[AsyncMock]:
    """Capture every audit event the routes emit (memory + promote)."""
    mock = AsyncMock()
    with (
        patch("audittrace.routes.memory.emit_memory_audit_event", mock),
        patch("audittrace.routes.memory_promote.emit_memory_audit_event", mock),
    ):
        yield mock


# ── RT-3 / RT-6 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("layer", LAYERS)
class TestTwoUsersSameFilename:
    """RT-3 (episodic) / RT-6 (procedural): B writing A's filename makes B's
    OWN row; A's row is untouched; the response key is the logical key."""

    def test_b_create_makes_a_second_row_a_untouched(
        self, rig: Rig, layer: str
    ) -> None:
        ra = rig.create(A, layer, "note.md", "A-body")
        before = rig.rows(layer)[f"{A}/note.md"]
        rb = rig.create(B, layer, "note.md", "B-body")
        assert ra.status_code == rb.status_code == 200
        assert ra.json()["key"] == rb.json()["key"] == "note.md"
        rows = rig.rows(layer)
        assert set(rows) == {f"{A}/note.md", f"{B}/note.md"}
        a_row = rows[f"{A}/note.md"]
        assert a_row.modified_by_user_id == A
        assert a_row.modified_at_ms == before.modified_at_ms
        assert a_row.size_bytes == len("A-body")
        assert rows[f"{B}/note.md"].created_by_user_id == B

    def test_b_update_never_reaches_a_row(self, rig: Rig, layer: str) -> None:
        rig.create(A, layer, "note.md", "A-body")
        before = rig.rows(layer)[f"{A}/note.md"]
        rb = rig.update(B, layer, "note.md", "B-hijack")
        # B has no row of its own: same 404 as an absent key, A untouched.
        assert rb.status_code == 404
        after = rig.rows(layer)
        assert set(after) == {f"{A}/note.md"}
        assert after[f"{A}/note.md"].modified_by_user_id == A
        assert after[f"{A}/note.md"].modified_at_ms == before.modified_at_ms

    def test_foreign_legacy_row_is_indistinguishable_from_absent(
        self, rig: Rig, layer: str
    ) -> None:
        rig.seed_legacy(layer, "legacy.md", A)
        over_legacy = rig.update(B, layer, "legacy.md")
        over_absent = rig.update("user-c", layer, "legacy.md")
        assert over_legacy.status_code == over_absent.status_code == 404
        assert over_legacy.json() == over_absent.json()
        legacy = rig.rows(layer)["legacy.md"]
        assert legacy.deleted_at_ms is None and legacy.modified_by_user_id == A


# ── RT-4 / RT-6 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("layer", LAYERS)
class TestForeignDeleteIsNotFound:
    """RT-4 (episodic) / RT-6 (procedural): B deleting A's filename is a 404;
    A still has the item, live, in A's list."""

    def test_b_delete_404_a_keeps_item(self, rig: Rig, layer: str) -> None:
        rig.create(A, layer, "note.md")
        rb = rig.delete(B, layer, "note.md")
        assert rb.status_code == 404
        row = rig.rows(layer)[f"{A}/note.md"]
        assert row.deleted_at_ms is None and row.deleted_by_user_id is None
        listing = rig.call("GET", f"/memory/{layer}", A, layer).json()
        assert [i["key"] for i in listing["items"]] == ["note.md"]

    def test_b_delete_of_a_legacy_key_404_legacy_untouched(
        self, rig: Rig, layer: str
    ) -> None:
        rig.seed_legacy(layer, "legacy.md", A)
        assert rig.delete(B, layer, "legacy.md").status_code == 404
        assert rig.rows(layer)["legacy.md"].deleted_at_ms is None

    def test_owner_delete_works(self, rig: Rig, layer: str) -> None:
        rig.create(A, layer, "note.md")
        assert rig.delete(A, layer, "note.md").status_code == 200
        assert rig.rows(layer)[f"{A}/note.md"].deleted_by_user_id == A


# ── RT-9 ────────────────────────────────────────────────────────────────────


class TestPromoteToEpisodic:
    """RT-9: promoting a session doc whose durable name equals another user's
    episodic file makes the promoter's OWN row; A's row is untouched."""

    def _upload_session(self, rig: Rig, sub: str, name: str) -> None:
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
        with _Auth(sub, "memory:episodic:write memory:session:write"):
            return rig.client.post(
                "/memory/promote",
                json={"filename": name},
                headers={"Authorization": "Bearer t"},
            )

    def test_disjoint_rows(self, rig: Rig) -> None:
        rig.create(A, "episodic", "note.md", "A-body")
        before = rig.rows("episodic")[f"{A}/note.md"]
        self._upload_session(rig, B, "note.md")
        r = self._promote(rig, B, "note.md")
        assert r.status_code == 200, r.text
        assert r.json()["key"] == "note.md"
        rows = rig.rows("episodic")
        assert set(rows) == {f"{A}/note.md", f"{B}/note.md"}
        assert rows[f"{A}/note.md"].modified_at_ms == before.modified_at_ms
        assert rows[f"{A}/note.md"].modified_by_user_id == A

    def test_promote_over_own_legacy_copies_forward_and_audits(
        self, rig: Rig, audits: AsyncMock
    ) -> None:
        created = rig.seed_legacy("episodic", "note.md", A)
        self._upload_session(rig, A, "note.md")
        r = self._promote(rig, A, "note.md")
        assert r.status_code == 200, r.text
        rows = rig.rows("episodic")
        assert rows["note.md"].deleted_by_user_id == A
        assert rows[f"{A}/note.md"].created_at_ms == created
        calls = [
            (c.kwargs["op"], c.kwargs.get("detail_extra")) for c in audits.mock_calls
        ]
        assert ("delete", {"reason": "copy_forward"}) in calls
        assert any(
            op == "write" and (d or {}).get("copied_from") == "note.md"
            for op, d in calls
        )

    def test_promote_name_over_cap_is_400(self, rig: Rig) -> None:
        name = "n" * (LOGICAL_KEY_MAX - 3) + "x.md"  # 219 chars
        assert len(name) == LOGICAL_KEY_MAX + 1
        self._upload_session(rig, A, name)
        r = self._promote(rig, A, name)
        assert r.status_code == 400
        assert str(LOGICAL_KEY_MAX) in r.json()["detail"]
        assert rig.rows("episodic") == {}

    def test_promote_concurrent_write_is_409(self, rig: Rig) -> None:
        self._upload_session(rig, A, "note.md")
        with patch.object(
            rig.manifest, "record_create", AsyncMock(side_effect=_conflict())
        ):
            r = self._promote(rig, A, "note.md")
        assert r.status_code == 409
        assert r.headers["Retry-After"] == "1"


def _conflict() -> ConcurrentManifestWriteError:
    return ConcurrentManifestWriteError("concurrent write; retry")


# ── RT-20 / RT-20b (reader #7) ──────────────────────────────────────────────


@pytest.mark.parametrize("layer", LAYERS)
class TestListOneItemPerDocument:
    """RT-20 (episodic) / RT-20b (procedural): the manifest row (stored key
    ``{owner}/note.md``) and the S3 object (leaf ``note.md``) are ONE item.
    Neuter: ``_merge_layer_items_with_s3`` dedups on the STORED key -> the
    document appears twice (once as the row, once as 'discovered')."""

    def _keys(self, rig: Rig, layer: str, sub: str, **params: Any) -> list[str]:
        r = rig.call("GET", f"/memory/{layer}", sub, layer, params=params)
        assert r.status_code == 200
        return [i["key"] for i in r.json()["items"]]

    def test_new_form_row_plus_its_s3_object(self, rig: Rig, layer: str) -> None:
        rig.create(A, layer, "note.md")
        assert self._keys(rig, layer, A) == ["note.md"]

    def test_legacy_row_plus_its_s3_object(self, rig: Rig, layer: str) -> None:
        rig.seed_legacy(layer, "note.md", A)
        assert self._keys(rig, layer, A) == ["note.md"]

    def test_after_copy_forward_one_live_item(self, rig: Rig, layer: str) -> None:
        rig.seed_legacy(layer, "note.md", A)
        assert rig.update(A, layer, "note.md").status_code == 200
        assert self._keys(rig, layer, A) == ["note.md"]
        # include_deleted shows the tombstone + the new row under ONE logical key
        both = self._keys(rig, layer, A, include_deleted="true")
        assert sorted(both) == ["note.md", "note.md"]

    def test_other_users_item_is_not_listed(self, rig: Rig, layer: str) -> None:
        rig.create(A, layer, "note.md")
        assert self._keys(rig, layer, B) == []


# ── RT-26a ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("layer", LAYERS)
class TestLogicalKeyCapIs400:
    """RT-26a: 219 -> 400, 218 -> 200, at EVERY route of the slice."""

    over = "n" * (LOGICAL_KEY_MAX - 3) + "x.md"
    at_cap = "n" * (LOGICAL_KEY_MAX - 3) + ".md"

    def test_lengths(self, layer: str) -> None:
        assert len(self.over) == LOGICAL_KEY_MAX + 1
        assert len(self.at_cap) == LOGICAL_KEY_MAX

    def test_create_219_400_218_200(self, rig: Rig, layer: str) -> None:
        assert rig.create(A, layer, self.over).status_code == 400
        assert rig.create(A, layer, self.at_cap).status_code == 200
        assert list(rig.rows(layer)) == [f"{A}/{self.at_cap}"]

    def test_update_delete_read_219_are_400(self, rig: Rig, layer: str) -> None:
        assert rig.update(A, layer, self.over).status_code == 400
        assert rig.delete(A, layer, self.over).status_code == 400
        r = rig.call("GET", f"/memory/{layer}/{self.over}", A, layer)
        assert r.status_code == 400

    def test_400_names_the_cap_not_a_stack(self, rig: Rig, layer: str) -> None:
        d = rig.create(A, layer, self.over).json()["detail"]
        assert d == f"key longer than {LOGICAL_KEY_MAX} characters"

    def test_36_char_sub_at_the_cap_is_200(self, rig: Rig, layer: str) -> None:
        sub = "a" * 36
        assert rig.create(sub, layer, self.at_cap).status_code == 200
        assert len(next(iter(rig.rows(layer)))) == 255


# ── Copy-forward at the route (S2) ──────────────────────────────────────────


@pytest.mark.parametrize("layer", LAYERS)
class TestCopyForwardAtTheRoute:
    def test_owner_put_copies_forward_with_continuous_history(
        self, rig: Rig, layer: str, audits: AsyncMock
    ) -> None:
        created = rig.seed_legacy(layer, "note.md", A)
        r = rig.update(A, layer, "note.md", "new-body")
        assert r.status_code == 200
        assert r.json()["key"] == "note.md"
        rows = rig.rows(layer)
        assert rows["note.md"].deleted_by_user_id == A  # legacy tombstoned by owner
        assert rows[f"{A}/note.md"].created_at_ms == created  # history continuous
        assert rows[f"{A}/note.md"].deleted_at_ms is None
        ops = [
            (c.kwargs["op"], c.kwargs.get("detail_extra")) for c in audits.mock_calls
        ]
        assert ops == [
            ("write", {"copied_from": "note.md"}),
            ("delete", {"reason": "copy_forward"}),
        ]

    def test_owner_post_copies_forward_too(
        self, rig: Rig, layer: str, audits: AsyncMock
    ) -> None:
        created = rig.seed_legacy(layer, "note.md", A)
        assert rig.create(A, layer, "note.md").status_code == 200
        rows = rig.rows(layer)
        assert rows["note.md"].deleted_by_user_id == A
        assert rows[f"{A}/note.md"].created_at_ms == created
        assert [c.kwargs["op"] for c in audits.mock_calls] == ["write", "delete"]

    def test_second_write_is_a_plain_update_no_copy_audit(
        self, rig: Rig, layer: str, audits: AsyncMock
    ) -> None:
        rig.seed_legacy(layer, "note.md", A)
        rig.update(A, layer, "note.md")
        audits.reset_mock()
        assert rig.update(A, layer, "note.md", "again").status_code == 200
        assert [
            (c.kwargs["op"], c.kwargs.get("detail_extra")) for c in audits.mock_calls
        ] == [("write", None)]

    def test_plain_create_has_no_copy_forward_audit(
        self, rig: Rig, layer: str, audits: AsyncMock
    ) -> None:
        assert rig.create(A, layer, "fresh.md").status_code == 200
        assert [
            (c.kwargs["op"], c.kwargs.get("detail_extra")) for c in audits.mock_calls
        ] == [("write", None)]

    def test_b_never_triggers_a_copy_forward_audit(
        self, rig: Rig, layer: str, audits: AsyncMock
    ) -> None:
        rig.seed_legacy(layer, "note.md", A)
        assert rig.create(B, layer, "note.md").status_code == 200
        assert [
            (c.kwargs["op"], c.kwargs.get("detail_extra")) for c in audits.mock_calls
        ] == [("write", None)]
        assert rig.rows(layer)["note.md"].deleted_at_ms is None  # A's legacy intact

    def test_audit_failure_is_a_500_not_a_silent_200(
        self, rig: Rig, layer: str, audits: AsyncMock
    ) -> None:
        rig.seed_legacy(layer, "note.md", A)
        audits.side_effect = RuntimeError("audit store down")
        assert rig.update(A, layer, "note.md").status_code == 500


class TestLegacySourceProbe:
    """``_legacy_copy_forward_source`` names a source ONLY for the caller's own
    live private bare-key row. Each clause is independently load-bearing
    (neuter each -> the matching case flips)."""

    @staticmethod
    def _entry(**over: Any) -> Any:
        from audittrace.services.memory_manifest import ManifestEntry

        base: dict[str, Any] = dict(
            id="i",
            layer="episodic",
            key="note.md",
            title=None,
            size_bytes=1,
            created_at_ms=1,
            modified_at_ms=1,
            created_by_user_id=A,
            modified_by_user_id=A,
            deleted_at_ms=None,
            deleted_by_user_id=None,
            tier="private",
            storage_key="note.md",
        )
        base.update(over)
        return ManifestEntry(**base)

    async def _probe(self, entry: Any) -> str | None:
        from audittrace.identity import UserContext
        from audittrace.routes.memory import _legacy_copy_forward_source

        fake = MagicMock()
        fake.resolve_read = AsyncMock(return_value=entry)
        user = UserContext(
            user_id=A, username=A, agent_type="t", scopes=(), is_admin=False
        )
        return await _legacy_copy_forward_source(fake, user, "episodic", "note.md")

    async def test_own_live_legacy_row_is_a_source(self) -> None:
        assert await self._probe(self._entry()) == "note.md"

    @pytest.mark.parametrize(
        "over",
        [
            {"created_by_user_id": B},  # a foreign row (defence in depth)
            {"tier": "corpus"},
            {"deleted_at_ms": 5},  # a tombstoned legacy row is not copied
            {"storage_key": f"{A}/note.md"},  # already owner-keyed
        ],
    )
    async def test_everything_else_is_not(self, over: dict[str, Any]) -> None:
        assert await self._probe(self._entry(**over)) is None

    async def test_absent_row_is_not(self) -> None:
        assert await self._probe(None) is None


# ── A2: the concurrent write race ───────────────────────────────────────────


@pytest.mark.parametrize("layer", LAYERS)
class TestConcurrentWriteIs409:
    @pytest.mark.parametrize("verb", ["create", "update"])
    def test_route_maps_the_conflict_to_409_retry_after(
        self, rig: Rig, layer: str, verb: str
    ) -> None:
        method = "record_create" if verb == "create" else "record_update"
        rig.seed_legacy(layer, "note.md", A)
        with patch.object(rig.manifest, method, AsyncMock(side_effect=_conflict())):
            r = getattr(rig, verb)(A, layer, "note.md")
        assert r.status_code == 409
        assert r.headers["Retry-After"] == "1"
        assert "retry" in r.json()["detail"]


class TestLostRaceAtTheService:
    """The REAL race on the SQL service: a rival owner-keyed row appears
    between the resolve and the commit -> ConcurrentManifestWriteError (not an
    IntegrityError), nothing written, and a retry converges."""

    @staticmethod
    async def _svc() -> tuple[MemoryManifestService, Any]:
        from audittrace.db.postgres import InMemoryPostgresFactory

        factory = InMemoryPostgresFactory()
        svc = MemoryManifestService(factory.get_session_factory())
        return svc, factory.get_session_factory()

    @pytest.mark.parametrize("verb", ["create", "update"])
    async def test_rival_copy_forward_wins(self, verb: str) -> None:
        svc, sf = await self._svc()
        await _seed_sql_legacy(sf, "episodic", "note.md", A)
        real = svc._resolve_write_in if verb == "create" else svc._read_row

        async def _stale(*args: Any, **kw: Any) -> Any:
            out = await real(*args, **kw)
            async with sf() as s:  # the rival lands AFTER our resolve
                s.add(_row("episodic", f"{A}/note.md", A))
                await s.commit()
            return out

        attr = "_resolve_write_in" if verb == "create" else "_read_row"
        with patch.object(svc, attr, _stale):
            with pytest.raises(ConcurrentManifestWriteError):
                if verb == "create":
                    await svc.record_create("episodic", "note.md", "t", 1, A)
                else:
                    await svc.record_update("episodic", "note.md", 1, A)
        # nothing of ours was written: the legacy row is still live
        legacy = await svc.get("episodic", "note.md")
        assert legacy is not None and legacy.deleted_at_ms is None
        # the retry converges on the rival's row
        retry = await svc.record_update("episodic", "note.md", 2, A)
        assert retry.storage_key == f"{A}/note.md" and retry.size_bytes == 2

    def test_only_unique_violations_are_conflicts(self) -> None:
        from sqlalchemy.exc import IntegrityError

        def _exc(orig: Any) -> IntegrityError:
            return IntegrityError("stmt", {}, orig)

        class _PgError(Exception):
            def __init__(self, state: str) -> None:
                self.sqlstate = state

        assert _is_unique_violation(_exc(_PgError("23505")))
        assert not _is_unique_violation(_exc(_PgError("23514")))  # the 034 CHECK
        assert not _is_unique_violation(_exc(_PgError("23502")))  # NOT NULL
        assert _is_unique_violation(
            _exc(Exception("UNIQUE constraint failed: memory_items.layer"))
        )
        assert not _is_unique_violation(_exc(Exception("CHECK constraint failed")))

    async def test_a_check_violation_stays_an_integrity_error(self) -> None:
        """Fail-closed: a non-UNIQUE IntegrityError is never softened to 409."""
        from sqlalchemy.exc import IntegrityError

        svc, sf = await self._svc()
        boom = IntegrityError("stmt", {}, Exception("CHECK constraint failed"))
        with (
            patch.object(sf().__class__, "commit", AsyncMock(side_effect=boom)),
            pytest.raises(IntegrityError),
        ):
            await svc.record_create("episodic", "x.md", "t", 1, A)


def _row(layer: str, key: str, owner: str, **kw: Any) -> MemoryItem:
    now = int(time.time() * 1000)
    return MemoryItem(
        layer=layer,
        key=key,
        title="t",
        size_bytes=1,
        created_at_ms=now,
        modified_at_ms=now,
        created_by_user_id=owner,
        modified_by_user_id=owner,
        tier=kw.pop("tier", "private"),
        **kw,
    )


async def _seed_sql_legacy(sf: Any, layer: str, key: str, owner: str) -> None:
    async with sf() as s:
        s.add(_row(layer, key, owner))
        await s.commit()


# ── A1: upsert_pdf_metadata on a foreign private row ────────────────────────


def _pdf_kwargs() -> dict[str, Any]:
    return dict(
        size_bytes=1,
        page_count=1,
        signature_status="none",
        ocr_coverage_pct=None,
        attachment_count=0,
        form_field_count=0,
        extraction_warnings=[],
        document_sha256="0" * 64,
    )


@pytest.fixture(params=BACKENDS)
async def pdf_env(request: pytest.FixtureRequest) -> tuple[Any, Any]:
    """(service, seed(key, owner, tier))."""
    if request.param == "mock":
        svc = MockMemoryManifestService()

        async def seed(key: str, owner: str, tier: str) -> None:
            svc._rows[("episodic", key)] = {
                "id": str(uuid.uuid4()),
                "layer": "episodic",
                "key": key,
                "title": None,
                "size_bytes": 1,
                "created_at_ms": 1,
                "modified_at_ms": 1,
                "created_by_user_id": owner,
                "modified_by_user_id": owner,
                "deleted_at_ms": None,
                "deleted_by_user_id": None,
                "tier": tier,
            }

        return svc, seed
    from audittrace.db.postgres import InMemoryPostgresFactory

    sf = InMemoryPostgresFactory().get_session_factory()
    sql = MemoryManifestService(sf)

    async def seed_sql(key: str, owner: str, tier: str) -> None:
        async with sf() as s:
            s.add(_row("episodic", key, owner, tier=tier))
            await s.commit()

    return sql, seed_sql


class TestPdfMetadataForeignPrivateRow:
    """A1: ``upsert_pdf_metadata`` refuses another user's PRIVATE row at the
    service, no admin/curator override, no mutation. (Before: only the 034
    CHECK on Postgres refused it.) Neuter: remove ``_pdf_private_row_foreign``
    -> RED."""

    async def test_foreign_legacy_private_row_refused(
        self, pdf_env: tuple[Any, Any]
    ) -> None:
        svc, seed = pdf_env
        await seed("scan.pdf", A, "private")
        for shared in (False, True):  # a curator's shared-write does not override
            with pytest.raises(CrossOwnerWriteError) as ei:
                await svc.upsert_pdf_metadata(
                    "episodic",
                    "scan.pdf",
                    user_id=B,
                    caller_can_write_shared=shared,
                    **_pdf_kwargs(),
                )
            assert A not in str(ei.value)  # names no owner
        row = await svc.get("episodic", "scan.pdf")
        assert row.modified_by_user_id == A and row.page_count is None

    async def test_own_private_row_refresh_still_works(
        self, pdf_env: tuple[Any, Any]
    ) -> None:
        svc, seed = pdf_env
        await seed("scan.pdf", A, "private")
        out = await svc.upsert_pdf_metadata(
            "episodic", "scan.pdf", user_id=A, **_pdf_kwargs()
        )
        assert out.page_count == 1

    async def test_corpus_row_semantics_unchanged(
        self, pdf_env: tuple[Any, Any]
    ) -> None:
        svc, seed = pdf_env
        await seed("scan.pdf", A, "corpus")
        out = await svc.upsert_pdf_metadata(
            "episodic",
            "scan.pdf",
            user_id=B,
            caller_can_write_shared=True,
            **_pdf_kwargs(),
        )
        assert out.modified_by_user_id == B
