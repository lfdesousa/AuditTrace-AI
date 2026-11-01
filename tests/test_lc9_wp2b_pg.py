"""LC-9 WP2b on REAL Postgres 16 (migration 034 applied, asyncpg driver), through
the REAL HTTP routes (``httpx.ASGITransport`` over ``create_app()``, so the app,
the manifest and the test share ONE event loop) and the MCP entry.

Reuses the throwaway-container scaffolding of ``test_lc9_migration_034_pg`` (one
postgres:18.3 container per session; Docker is available in the gate container).

* RT-22: fault-inject after copy-forward step 1 (the Chroma upsert, before the
  manifest transaction) and after step 2 (the committed manifest transaction,
  before the audit): no half state -- the caller reads its own item, the list has
  ONE item, the other user gets 404, and the retry converges.
* The copy-forward is ONE transaction: failing the first commit leaves both rows
  untouched; the second commit never happens (a split copy-forward would leave
  a new row with no tombstone).
* The REAL race: two simultaneous owner writes over one legacy row -> one winner,
  the loser a 409 (REST) / a retryable tool error (MCP), never a 500.
* The cap against the real ``String(255)`` column: 219 -> 400, 218 with a
  36-char sub -> 200 and a 255-char stored key that passes the 034 CHECK.
* RT-1 / RT-2 / RT-5b / RT-10 manifest halves with the 034 CHECK enforced.

Neuters: copy-forward split into two commits -> the ``commit_fault`` n=2 case
RED; ``_commit_or_conflict`` returning a bare commit -> the race tests RED (a
raw ``IntegrityError``, 500); the 409 mapper removed from a site -> that site's
race test RED.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audittrace import dependencies
from audittrace.identity import UserContext
from audittrace.services.memory_keys import LOGICAL_KEY_MAX
from audittrace.services.memory_manifest import MemoryManifestService
from tests.test_lc9_migration_034_pg import _ADMIN_URL, Schema, pg
from tests.test_lc9_wp2b_semantic_routes import BASE, CORPUS, REFUSAL, _Auth

A, B, CURATOR = "user-a", "user-b", "curator-1"
MCP_SCOPE = "memory:decisions:write memory:skills:write"


class _CrashError(Exception):
    """A fault injected between copy-forward steps. Deliberately NOT a
    ``RuntimeError`` (the update route maps that to its soft-deleted 409)."""


def _ctx(sub: str) -> UserContext:
    return UserContext(
        user_id=sub, username=sub, agent_type="test", scopes=(), is_admin=False
    )


@pytest.fixture
def pgs() -> Iterator[Schema]:
    assert _ADMIN_URL is not None
    s = Schema(_ADMIN_URL)
    try:
        yield s
    finally:
        s.drop()


@pytest.fixture(autouse=True)
def _mcp_registries() -> Iterator[None]:
    import audittrace.tools.memory_handlers as mh
    from audittrace.tools import mcp_write_handlers as wh
    from audittrace.tools import reset_registry_for_tests
    from audittrace.tools.mcp_write_registry import reset_mcp_write_registry_for_tests

    reset_registry_for_tests()
    importlib.reload(mh)
    reset_mcp_write_registry_for_tests()
    importlib.reload(wh)
    yield
    reset_registry_for_tests()
    reset_mcp_write_registry_for_tests()


class Env:
    def __init__(self, client: httpx.AsyncClient, pgs: Schema, svc: Any, sem: Any):
        self.client, self.pgs, self.svc, self.sem = client, pgs, svc, sem
        self._pinned = False

    @contextlib.contextmanager
    def auth(self, sub: str, scope: str) -> Iterator[None]:
        """ONE auth patch around concurrent requests. Overlapping per-request
        ``_Auth`` contexts would restore each other's mock on exit and leak a
        patched ``auth.get_settings`` into every later test."""
        with _Auth(sub, scope):
            self._pinned = True
            try:
                yield
            finally:
                self._pinned = False

    async def call(
        self, method: str, url: str, sub: str, scope: str = BASE, **kw: Any
    ) -> httpx.Response:
        headers = {"Authorization": "Bearer t"}
        if self._pinned:
            return await self.client.request(method, url, headers=headers, **kw)
        with _Auth(sub, scope):
            return await self.client.request(method, url, headers=headers, **kw)

    async def create(self, sub: str, doc: str, text: str = "body", **kw: Any) -> Any:
        scope = kw.pop("scope", BASE)
        params = kw.pop("params", None)
        return await self.call(
            "POST",
            "/memory/semantic",
            sub,
            scope,
            params=params,
            json={"collection": "decisions", "document_id": doc, "text": text},
        )

    async def put(self, sub: str, doc: str, text: str = "upd") -> Any:
        return await self.call(
            "PUT", f"/memory/semantic/decisions/{doc}", sub, json={"text": text}
        )

    async def delete(self, sub: str, doc: str) -> Any:
        return await self.call("DELETE", f"/memory/semantic/decisions/{doc}", sub)

    async def get(self, sub: str, doc: str) -> Any:
        return await self.call("GET", f"/memory/semantic/decisions/{doc}", sub)

    async def keys(self, sub: str) -> list[str]:
        r = await self.call(
            "GET", "/memory/semantic", sub, params={"collection": "decisions"}
        )
        assert r.status_code == 200, r.text
        return [i["key"] for i in r.json()["items"]]

    async def mcp(self, sub: str, doc: str, text: str = "t") -> dict[str, Any]:
        r = await self.call(
            "POST",
            "/mcp",
            sub,
            MCP_SCOPE,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "write_decision",
                    "arguments": {"document_id": doc, "text": text},
                },
            },
        )
        assert r.status_code == 200, r.text
        return r.json()["result"]


@contextlib.asynccontextmanager
async def _env(
    pgs: Schema,
    monkeypatch: pytest.MonkeyPatch,
    *,
    legacy: tuple[tuple[str, str], ...] = (),
) -> AsyncIterator[Env]:
    """The app wired to the real-PG manifest + the real Chroma service.
    ``legacy`` = ((document_id, owner), ...) seeded BEFORE migration 034, as a
    pre-LC-9 bare-key private row plus the owner's Chroma chunk."""
    from audittrace.db.factory import MockChromaDBFactory
    from audittrace.server import create_app
    from audittrace.services.semantic import ChromaSemanticService

    monkeypatch.setattr(
        "audittrace.services.semantic.embed_via_nomic",
        AsyncMock(side_effect=lambda texts, **_: [[0.1, 0.2, 0.3] for _ in texts]),
    )
    for doc, owner in legacy:
        pgs.insert(layer="semantic", key=f"decisions/{doc}", created_by_user_id=owner)
    pgs.migrate("upgrade")
    svc = MemoryManifestService(async_sessionmaker(pgs.async_engine))
    sem = ChromaSemanticService(
        client=await MockChromaDBFactory().get_client(),
        default_collections=["decisions", "skills", "semantic"],
    )
    for doc, owner in legacy:
        await sem.upsert(
            _ctx(owner), "decisions", doc, "legacy body", {}, tier="private"
        )
    previous = dependencies.container
    container = dependencies.create_test_container()
    container._instances["memory_manifest"] = svc
    container._instances["semantic"] = sem
    dependencies.container = container
    app = create_app()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    audits = AsyncMock()
    try:
        with (
            patch("audittrace.routes.memory.emit_memory_audit_event", audits),
            patch(
                "audittrace.tools.mcp_write_handlers.emit_memory_audit_event", audits
            ),
        ):
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                yield Env(client, pgs, svc, sem)
    finally:
        dependencies.container = previous
        await pgs.async_engine.dispose()


def _live(pgs: Schema, key: str) -> bool:
    r = pgs.row(key)
    return r is not None and r.deleted_at_ms is None


# ── RT-22 ────────────────────────────────────────────────────────────────────


@pg
class TestRt22FaultInjection:
    async def test_fault_after_step_1_no_half_state(
        self, pgs: Schema, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Chroma upsert (step 1) ran, the manifest transaction (step 2)
        never did: the legacy row still answers, nothing half-written, and the
        retry converges."""
        async with _env(pgs, monkeypatch, legacy=(("x", A),)) as env:
            with patch.object(
                MemoryManifestService,
                "record_update",
                AsyncMock(side_effect=_CrashError("crash between step 1 and 2")),
            ):
                r = await env.put(A, "x", "A-new")
            assert r.status_code == 500
            # no half state: legacy live, NO owner-keyed row
            assert _live(pgs, "decisions/x") and pgs.row(f"{A}/decisions/x") is None
            ga = await env.get(A, "x")
            assert (
                ga.status_code == 200 and ga.json()["manifest"]["key"] == "decisions/x"
            )
            assert await env.keys(A) == ["decisions/x"]  # ONE item
            assert (await env.get(B, "x")).status_code == 404
            # the retry converges: owner row + legacy tombstone, one item
            ok = await env.put(A, "x", "A-new")
            assert ok.status_code == 200
            assert _live(pgs, f"{A}/decisions/x")
            assert pgs.row("decisions/x").deleted_by_user_id == A
            assert await env.keys(A) == ["decisions/x"]
            assert (await env.get(A, "x")).json()["content"] == "A-new"

    async def test_fault_after_step_2_no_half_state(
        self, pgs: Schema, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The manifest transaction committed (step 2); the audit failed after
        it: the state is complete (owner row + tombstone), the caller reads
        its own item, the list has ONE item, and the retry is a plain update."""
        async with _env(pgs, monkeypatch, legacy=(("x", A),)) as env:
            with patch(
                "audittrace.routes.memory.emit_memory_audit_event",
                AsyncMock(side_effect=RuntimeError("audit store down")),
            ):
                r = await env.put(A, "x", "A-new")
            assert r.status_code == 500
            assert _live(pgs, f"{A}/decisions/x")
            assert pgs.row("decisions/x").deleted_by_user_id == A
            ga = await env.get(A, "x")
            assert ga.status_code == 200 and ga.json()["content"] == "A-new"
            assert await env.keys(A) == ["decisions/x"]  # ONE live item
            assert (await env.get(B, "x")).status_code == 404
            ok = await env.put(A, "x", "A-again")  # plain `own` update now
            assert ok.status_code == 200
            assert await env.keys(A) == ["decisions/x"]
            assert (await env.get(A, "x")).json()["content"] == "A-again"

    @pytest.mark.parametrize("fail_commit", [1, 2])
    async def test_copy_forward_is_one_transaction(
        self, pgs: Schema, monkeypatch: pytest.MonkeyPatch, fail_commit: int
    ) -> None:
        """The invariant 'owner row exists <=> legacy tombstoned' holds whichever
        manifest commit is made to fail. ONE transaction: commit #1 failing
        rolls both back; commit #2 never happens. Neuter: split the copy-forward
        into two commits -> the n=2 case leaves a new row with no tombstone."""
        real_commit = AsyncSession.commit
        calls = {"n": 0}

        async def _failing(self: AsyncSession) -> None:
            calls["n"] += 1
            if calls["n"] == fail_commit:
                raise _CrashError(f"commit #{fail_commit} failed")
            await real_commit(self)

        async with _env(pgs, monkeypatch, legacy=(("x", A),)) as env:
            with patch.object(AsyncSession, "commit", _failing):
                r = await env.put(A, "x", "A-new")
            owner_row = pgs.row(f"{A}/decisions/x") is not None
            tombstoned = not _live(pgs, "decisions/x")
            assert owner_row == tombstoned, "half state: row and tombstone disagree"
            assert r.status_code == (500 if fail_commit == 1 else 200)
            assert (await env.get(A, "x")).status_code == 200
            assert await env.keys(A) == ["decisions/x"]


# ── the REAL race ────────────────────────────────────────────────────────────


@pg
class TestRealRace:
    @staticmethod
    def _both_resolve_before_either_commits(svc: MemoryManifestService, method: str):
        """Hold each writer after it resolved the legacy row until BOTH did."""
        barrier = asyncio.Barrier(2)
        real_resolve = getattr(svc, method)
        in_write: Any = None
        import contextvars

        in_write = contextvars.ContextVar("in_write", default=False)
        real_write = svc.record_update if method == "_read_row" else svc.record_create

        async def _wrapped_write(*a: Any, **k: Any) -> Any:
            in_write.set(True)
            return await real_write(*a, **k)

        async def _resolve(*a: Any, **k: Any) -> Any:
            out = await real_resolve(*a, **k)
            if in_write.get():
                await barrier.wait()
            return out

        return _resolve, _wrapped_write

    async def test_two_simultaneous_puts_one_winner_one_409(
        self, pgs: Schema, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Site #9 on the real unique index. Neuter: ``_commit_or_conflict``
        -> a bare commit makes the loser a raw IntegrityError (500)."""
        async with _env(pgs, monkeypatch, legacy=(("x", A),)) as env:
            resolve, write = self._both_resolve_before_either_commits(
                env.svc, "_read_row"
            )
            with (
                patch.object(env.svc, "_read_row", resolve),
                patch.object(env.svc, "record_update", write),
            ):
                with env.auth(A, BASE + " " + MCP_SCOPE):
                    r1, r2 = await asyncio.gather(
                        env.put(A, "x", "1"), env.put(A, "x", "2")
                    )
            codes = sorted((r1.status_code, r2.status_code))
            assert codes == [200, 409], (r1.text, r2.text)
            loser = r1 if r1.status_code == 409 else r2
            assert loser.headers["Retry-After"] == "1"
            assert _live(pgs, f"{A}/decisions/x")
            assert pgs.row("decisions/x").deleted_by_user_id == A
            retry = await env.put(A, "x", "3")  # converges on the winner's row
            assert retry.status_code == 200
            assert await env.keys(A) == ["decisions/x"]

    async def test_two_simultaneous_posts_one_winner_one_409(
        self, pgs: Schema, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Site #8."""
        async with _env(pgs, monkeypatch, legacy=(("x", A),)) as env:
            resolve, write = self._both_resolve_before_either_commits(
                env.svc, "_resolve_write_in"
            )
            with (
                patch.object(env.svc, "_resolve_write_in", resolve),
                patch.object(env.svc, "record_create", write),
            ):
                with env.auth(A, BASE + " " + MCP_SCOPE):
                    r1, r2 = await asyncio.gather(
                        env.create(A, "x", "1"), env.create(A, "x", "2")
                    )
            assert sorted((r1.status_code, r2.status_code)) == [200, 409]
            assert await env.keys(A) == ["decisions/x"]

    async def test_two_simultaneous_mcp_writes_one_winner_one_retry_error(
        self, pgs: Schema, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Site #14. Neuter: drop the ``ConcurrentManifestWriteError`` mapper in
        ``_write_semantic_document`` -> the loser's error is the bare class name,
        no 'retry' text."""
        async with _env(pgs, monkeypatch, legacy=(("x", A),)) as env:
            resolve, write = self._both_resolve_before_either_commits(
                env.svc, "_resolve_write_in"
            )
            with (
                patch.object(env.svc, "_resolve_write_in", resolve),
                patch.object(env.svc, "record_create", write),
            ):
                with env.auth(A, BASE + " " + MCP_SCOPE):
                    r1, r2 = await asyncio.gather(
                        env.mcp(A, "x", "1"), env.mcp(A, "x", "2")
                    )
            errs = [r for r in (r1, r2) if r["isError"]]
            oks = [r for r in (r1, r2) if not r["isError"]]
            assert len(errs) == 1 and len(oks) == 1, (r1, r2)
            assert "retry" in str(errs[0]["content"])
            assert _live(pgs, f"{A}/decisions/x")
            assert pgs.row("decisions/x").deleted_by_user_id == A


# ── cap on the real column ───────────────────────────────────────────────────


@pg
class TestCapOnRealColumn:
    over = "d" * (LOGICAL_KEY_MAX + 1 - len("decisions/"))
    at_cap = "d" * (LOGICAL_KEY_MAX - len("decisions/"))

    async def test_219_is_400_not_a_stringdatatruncation_500(
        self, pgs: Schema, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with _env(pgs, monkeypatch) as env:
            assert (await env.create(A, self.over)).status_code == 400
            assert (await env.put(A, self.over)).status_code == 400
            assert (await env.delete(A, self.over)).status_code == 400

    async def test_218_with_a_36_char_sub_stores_255_and_passes_the_check(
        self, pgs: Schema, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sub = "a" * 36
        async with _env(pgs, monkeypatch) as env:
            r = await env.create(sub, self.at_cap)
            assert r.status_code == 200, r.text
            assert r.json()["key"] == f"decisions/{self.at_cap}"
            row = pgs.row(f"{sub}/decisions/{self.at_cap}")
            assert row is not None and len(row.key) == 255


# ── RT-1 / RT-2 / RT-5b / RT-10 with the 034 CHECK enforced ──────────────────


@pg
class TestIsolationOnPostgres:
    async def test_rt1_rt12_two_users_same_id_two_rows(
        self, pgs: Schema, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with _env(pgs, monkeypatch) as env:
            ra, rb = await env.create(A, "x", "A"), await env.create(B, "x", "B")
            assert ra.status_code == rb.status_code == 200
            assert ra.json()["key"] == rb.json()["key"] == "decisions/x"
            assert _live(pgs, f"{A}/decisions/x") and _live(pgs, f"{B}/decisions/x")
            assert pgs.row(f"{A}/decisions/x").modified_by_user_id == A

    async def test_rt2_foreign_delete_is_404_a_untouched(
        self, pgs: Schema, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with _env(pgs, monkeypatch) as env:
            await env.create(A, "x")
            assert (await env.delete(B, "x")).status_code == 404
            assert _live(pgs, f"{A}/decisions/x")

    async def test_rt5b_curator_over_legacy_403_one_text_a_copies_forward(
        self, pgs: Schema, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with _env(pgs, monkeypatch, legacy=(("x", A),)) as env:
            r = await env.create(
                CURATOR, "x", scope=BASE + CORPUS, params={"promote": "corpus"}
            )
            assert r.status_code == 403 and r.json()["detail"] == REFUSAL
            assert A not in r.text and _live(pgs, "decisions/x")
            assert (await env.put(A, "x", "next")).status_code == 200
            assert _live(pgs, f"{A}/decisions/x")

    async def test_rt10_legacy_a_get_200_b_get_404_b_put_never_copies(
        self, pgs: Schema, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with _env(pgs, monkeypatch, legacy=(("x", A),)) as env:
            assert (await env.get(A, "x")).status_code == 200
            assert (await env.get(B, "x")).status_code == 404
            assert (await env.put(B, "x")).status_code == 404
            assert pgs.row(f"{B}/decisions/x") is None and _live(pgs, "decisions/x")
            assert (await env.put(A, "x")).status_code == 200
            assert pgs.row("decisions/x").deleted_by_user_id == A
