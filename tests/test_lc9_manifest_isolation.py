"""LC-9 WP1 -- the manifest service layer refuses cross-user private-row
access, on BOTH backends (the Mock twin and the SQL ``MemoryManifestService``
over aiosqlite). One parametrised fixture runs every case twice, so the two
cannot drift (Mock parity).

RT map (spec ``2026-10-26-SPEC-lc9-cross-user-private-row-isolation`` +
ADDENDUM-1): RT-1..4 (service shape) ``TestTwoUsersSameLogicalKey``; RT-5b
``TestCorpusWriteOverPrivate``; RT-15 ``TestLegacyRowIsInvisibleToOthers``;
RT-16 ``TestCorpusWriteOverPrivate``; RT-17 ``TestCopyForward``; RT-24
``TestNoAdminOrCuratorOverride``; RT-26b ``TestInvalidSub``; RT-12
``TestEntriesCarryTheLogicalKey``; the C3 readers #3/#4
``TestIndexStatusLogicalKey`` / ``TestTombstoneCandidates``; S1 cap
``TestLogicalKeyCap``; the rewritten defect pin
``TestRewrittenDefectPin``.

Neuters (each alone, ``PYTHONDONTWRITEBYTECODE=1``, restored with ``cmp``):

* ``physical_key`` returns ``logical``                     -> TestTwoUsersSameLogicalKey
* ``resolve_write`` legacy arm without ``created_by==owner`` -> TestLegacyRowIsInvisibleToOthers
* ``resolve_write`` corpus arm lets a private row through    -> TestCorpusWriteOverPrivate
* foreign / tier_conflict treated as ``own``                 -> TestForeignBackstop
* ``resolve_read`` drops owner equality on the legacy arm    -> TestNoAdminOrCuratorOverride
* ``ManifestEntry.from_row`` / ``_to_entry`` key = stored    -> TestEntriesCarryTheLogicalKey
* ``record_create`` re-stamps / updates the corpus row       -> TestPrivateWriteBesideCorpus
* copy-forward skips the tombstone / created_at              -> TestCopyForward
* ``authorize_write`` skips the resolver                     -> TestAuthorizeWriteIsThePreWriteChoke
* ``deleted_key_candidates`` drops the owner key             -> TestTombstoneCandidates
* ``check_logical`` cap removed                              -> TestLogicalKeyCap
* ``check_sub`` rule removed                                 -> TestInvalidSub
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select

from audittrace.db.models import MemoryItem
from audittrace.identity import UserContext
from audittrace.services.memory_keys import (
    CrossOwnerWriteError,
    InvalidPrivateSubjectError,
    LogicalKeyTooLongError,
    deleted_key_candidates,
    tier_conflict_message,
)
from audittrace.services.memory_manifest import (
    ManifestAuthorizationError,
    MemoryManifestService,
    MockMemoryManifestService,
    authorize_write,
)


def _user(
    sub: str, *, is_admin: bool = False, scopes: tuple[str, ...] = ()
) -> UserContext:
    return UserContext(
        user_id=sub, username=sub, agent_type="test", scopes=scopes, is_admin=is_admin
    )


A, B, CURATOR, ADMIN = "user-a", "user-b", "curator", "ops-admin"


@dataclass
class Env:
    svc: Any
    kind: str

    async def seed(
        self,
        layer: str,
        key: str,
        owner: str,
        *,
        tier: str = "private",
        deleted: bool = False,
        created_at_ms: int | None = None,
        title: str | None = None,
    ) -> None:
        """Insert a row AT the exact stored ``key`` (legacy bare rows, or a
        foreign row at someone's owner key), bypassing the resolver."""
        now = created_at_ms or int(time.time() * 1000)
        if self.kind == "mock":
            self.svc._rows[(layer, key)] = {
                "id": str(uuid.uuid4()),
                "layer": layer,
                "key": key,
                "title": title,
                "size_bytes": 1,
                "created_at_ms": now,
                "modified_at_ms": now,
                "created_by_user_id": owner,
                "modified_by_user_id": owner,
                "deleted_at_ms": now if deleted else None,
                "deleted_by_user_id": owner if deleted else None,
                "tier": tier,
            }
            return
        async with self.svc._session_factory() as session:
            session.add(
                MemoryItem(
                    layer=layer,
                    key=key,
                    title=title,
                    size_bytes=1,
                    created_at_ms=now,
                    modified_at_ms=now,
                    created_by_user_id=owner,
                    modified_by_user_id=owner,
                    deleted_at_ms=now if deleted else None,
                    deleted_by_user_id=owner if deleted else None,
                    tier=tier,
                )
            )
            await session.commit()

    async def scanned_clean(self, layer: str, stored_key: str) -> None:
        """Put the row AT ``stored_key`` in the index outbox: scanned clean, not indexed."""
        if self.kind == "mock":
            self.svc.set_index_state(layer, stored_key, scan_status="scanned_clean")
            return
        async with self.svc._session_factory() as session:
            for row in await session.scalars(
                select(MemoryItem).filter_by(layer=layer, key=stored_key)
            ):
                row.scan_status = "scanned_clean"
            await session.commit()
        assert (await self.raw(layer, stored_key)) is not None

    async def raw(self, layer: str, stored_key: str) -> Any:
        return await self.svc.get(layer, stored_key)

    async def all_stored_keys(self, layer: str) -> set[str]:
        return {
            e.storage_key
            for e in await self.svc.list_for_layer(layer, include_deleted=True)
        }


@pytest_asyncio.fixture(params=["mock", "sql"])
async def env(request: pytest.FixtureRequest) -> Env:
    if request.param == "mock":
        return Env(MockMemoryManifestService(), "mock")
    from audittrace.db.postgres import InMemoryPostgresFactory

    factory = InMemoryPostgresFactory()
    await factory.create_schema()
    return Env(MemoryManifestService(factory.get_session_factory()), "sql")


class TestTwoUsersSameLogicalKey:
    """RT-1..4, service shape: B's write/delete on A's logical key lands on
    B's own row and never touches A's."""

    async def test_b_create_does_not_touch_a(self, env: Env) -> None:
        a = await env.svc.record_create("episodic", "note.md", "A's", 5, A)
        b = await env.svc.record_create("episodic", "note.md", "B's", 9, B)
        assert a.id != b.id
        assert (a.storage_key, b.storage_key) == (f"{A}/note.md", f"{B}/note.md")
        a_after = await env.raw("episodic", f"{A}/note.md")
        assert a_after.title == "A's"
        assert a_after.modified_by_user_id == A
        assert a_after.size_bytes == 5

    async def test_b_update_on_a_key_is_not_found_and_a_is_untouched(
        self, env: Env
    ) -> None:
        await env.svc.record_create("episodic", "note.md", "A's", 5, A)
        with pytest.raises(LookupError):
            await env.svc.record_update("episodic", "note.md", 99, B, title="pwned")
        a_after = await env.raw("episodic", f"{A}/note.md")
        assert a_after.title == "A's" and a_after.modified_by_user_id == A

    async def test_b_delete_on_a_key_is_not_found_and_a_stays_live(
        self, env: Env
    ) -> None:
        await env.svc.record_create("episodic", "note.md", "A's", 5, A)
        with pytest.raises(LookupError):
            await env.svc.record_delete("episodic", "note.md", B)
        a_after = await env.raw("episodic", f"{A}/note.md")
        assert a_after.deleted_at_ms is None and a_after.deleted_by_user_id is None
        listed = await env.svc.list_for_layer("episodic", caller=_user(A))
        assert [e.key for e in listed] == ["note.md"]

    async def test_each_user_deletes_only_their_own(self, env: Env) -> None:
        await env.svc.record_create("episodic", "note.md", None, 1, A)
        await env.svc.record_create("episodic", "note.md", None, 1, B)
        await env.svc.record_delete("episodic", "note.md", B)
        assert (await env.raw("episodic", f"{A}/note.md")).deleted_at_ms is None
        assert (await env.raw("episodic", f"{B}/note.md")).deleted_at_ms is not None

    async def test_owner_rewrite_is_idempotent_and_revives(self, env: Env) -> None:
        first = await env.svc.record_create("episodic", "note.md", "v1", 1, A)
        await env.svc.record_delete("episodic", "note.md", A)
        again = await env.svc.record_create("episodic", "note.md", "v2", 2, A)
        assert again.id == first.id and again.deleted_at_ms is None
        assert again.title == "v2"
        twice = await env.svc.record_create("episodic", "note.md", "v3", 3, A)
        assert twice.id == first.id

    async def test_owner_update_and_delete_work(self, env: Env) -> None:
        await env.svc.record_create("episodic", "note.md", "v1", 1, A)
        upd = await env.svc.record_update("episodic", "note.md", 7, A, title="v2")
        assert upd.title == "v2" and upd.size_bytes == 7 and upd.key == "note.md"
        gone = await env.svc.record_delete("episodic", "note.md", A)
        assert gone.deleted_by_user_id == A
        again = await env.svc.record_delete("episodic", "note.md", A)
        assert again.deleted_at_ms == gone.deleted_at_ms


class TestLegacyRowIsInvisibleToOthers:
    """RT-15 (service level): B writing over A's LEGACY bare row behaves exactly
    like B writing an absent key. No oracle, A's row unchanged."""

    async def test_b_over_a_legacy_equals_b_over_absent(self, env: Env) -> None:
        await env.seed("episodic", "k1.md", A, title="A legacy")
        over_legacy = await env.svc.record_create("episodic", "k1.md", "B", 3, B)
        over_absent = await env.svc.record_create("episodic", "k9.md", "B", 3, B)
        assert over_legacy.storage_key == f"{B}/k1.md"
        assert over_absent.storage_key == f"{B}/k9.md"
        assert (over_legacy.tier, over_legacy.created_by_user_id) == (
            over_absent.tier,
            over_absent.created_by_user_id,
        )
        legacy = await env.raw("episodic", "k1.md")
        assert legacy.title == "A legacy"
        assert legacy.deleted_at_ms is None and legacy.modified_by_user_id == A

    async def test_b_update_and_delete_find_nothing(self, env: Env) -> None:
        await env.seed("episodic", "k1.md", A)
        with pytest.raises(LookupError):
            await env.svc.record_update("episodic", "k1.md", 1, B)
        with pytest.raises(LookupError):
            await env.svc.record_delete("episodic", "k1.md", B)
        assert (await env.raw("episodic", "k1.md")).deleted_at_ms is None

    async def test_dual_lookup_read_owner_equality(self, env: Env) -> None:
        await env.seed("episodic", "k1.md", A)
        mine = await env.svc.resolve_read("episodic", "k1.md", _user(A))
        theirs = await env.svc.resolve_read("episodic", "k1.md", _user(B))
        assert mine is not None and mine.key == "k1.md"
        assert theirs is None

    async def test_read_prefers_the_owner_key_over_own_legacy(self, env: Env) -> None:
        await env.seed("episodic", "k1.md", A, title="legacy")
        await env.seed("episodic", f"{A}/k1.md", A, title="new")
        got = await env.svc.resolve_read("episodic", "k1.md", _user(A))
        assert got is not None and got.title == "new"

    async def test_corpus_row_readable_by_anyone(self, env: Env) -> None:
        await env.seed("episodic", "shared.md", CURATOR, tier="corpus")
        got = await env.svc.resolve_read("episodic", "shared.md", _user(B))
        assert got is not None and got.tier == "corpus"

    async def test_read_never_raises_on_a_bad_sub_or_a_long_key(self, env: Env) -> None:
        assert await env.svc.resolve_read("episodic", "k", _user("a/b")) is None
        assert await env.svc.resolve_read("episodic", "k" * 400, _user(A)) is None


class TestCorpusWriteOverPrivate:
    """RT-5b / RT-16: a corpus write at a bare key held by ANY private row (any
    owner, live or tombstoned, the caller's own included) is refused with ONE
    owner-free text and changes nothing."""

    @pytest.mark.parametrize(
        ("owner", "deleted"),
        [(A, False), (CURATOR, False), (A, True)],
        ids=["other-live", "own-live", "other-tombstoned"],
    )
    async def test_refused_with_one_text(
        self, env: Env, owner: str, deleted: bool
    ) -> None:
        await env.seed("semantic", "decisions/k.md", owner, deleted=deleted)
        before = await env.raw("semantic", "decisions/k.md")
        with pytest.raises(CrossOwnerWriteError) as exc:
            await env.svc.record_create(
                "semantic",
                "decisions/k.md",
                "corpus",
                1,
                CURATOR,
                tier="corpus",
                caller_can_write_shared=True,
            )
        assert str(exc.value) == tier_conflict_message("semantic", "decisions/k.md")
        assert owner not in str(exc.value)
        after = await env.raw("semantic", "decisions/k.md")
        assert after == before

    async def test_cross_owner_error_is_a_403_family_error(self, env: Env) -> None:
        await env.seed("semantic", "decisions/k.md", A)
        with pytest.raises(ManifestAuthorizationError):
            await env.svc.record_create(
                "semantic", "decisions/k.md", None, 1, CURATOR, tier="corpus"
            )

    async def test_new_form_private_row_does_not_block_a_corpus_row(
        self, env: Env
    ) -> None:
        """RT-5a: A's row is new-form (``user-a/...``), so the curator's corpus
        row at the bare key is a distinct row and A's stays private."""
        await env.svc.record_create("semantic", "decisions/k4.md", "A", 1, A)
        corpus = await env.svc.record_create(
            "semantic",
            "decisions/k4.md",
            "C",
            1,
            CURATOR,
            tier="corpus",
            caller_can_write_shared=True,
        )
        assert corpus.tier == "corpus" and corpus.storage_key == "decisions/k4.md"
        mine = await env.raw("semantic", f"{A}/decisions/k4.md")
        assert mine.tier == "private" and mine.title == "A"
        again = await env.svc.record_create("semantic", "decisions/k4.md", "A2", 2, A)
        assert again.id == mine.id and again.tier == "private"


class TestPrivateWriteBesideCorpus:
    """The silent tier re-stamp is gone: a private write next to a corpus row
    never changes the corpus row, whatever the caller's authority."""

    async def test_unauthorized_private_write_over_corpus_row_is_refused(
        self, env: Env
    ) -> None:
        await env.seed("semantic", "decisions/s.md", CURATOR, tier="corpus", title="S")
        with pytest.raises(ManifestAuthorizationError):
            await env.svc.record_create("semantic", "decisions/s.md", "pwn", 9, B)
        row = await env.raw("semantic", "decisions/s.md")
        assert row.title == "S" and row.tier == "corpus"
        assert row.modified_by_user_id == CURATOR

    async def test_authorized_private_write_creates_own_row_and_leaves_corpus(
        self, env: Env
    ) -> None:
        await env.seed("semantic", "decisions/s.md", CURATOR, tier="corpus", title="S")
        mine = await env.svc.record_create(
            "semantic",
            "decisions/s.md",
            "mine",
            9,
            ADMIN,
            tier="private",
            caller_can_write_shared=True,
        )
        assert mine.tier == "private" and mine.storage_key == f"{ADMIN}/decisions/s.md"
        row = await env.raw("semantic", "decisions/s.md")
        assert row.tier == "corpus" and row.title == "S"
        assert row.created_by_user_id == CURATOR

    async def test_corpus_update_and_delete_by_authorized_curator(
        self, env: Env
    ) -> None:
        await env.seed("semantic", "decisions/s.md", CURATOR, tier="corpus", title="S")
        upd = await env.svc.record_update(
            "semantic", "decisions/s.md", 5, CURATOR, caller_can_write_shared=True
        )
        assert upd.tier == "corpus" and upd.size_bytes == 5
        gone = await env.svc.record_delete(
            "semantic", "decisions/s.md", CURATOR, caller_can_write_shared=True
        )
        assert gone.deleted_at_ms is not None and gone.tier == "corpus"

    async def test_unauthorized_corpus_update_and_delete_refused(
        self, env: Env
    ) -> None:
        await env.seed("semantic", "decisions/s.md", CURATOR, tier="corpus", title="S")
        with pytest.raises(ManifestAuthorizationError):
            await env.svc.record_update("semantic", "decisions/s.md", 5, B)
        with pytest.raises(ManifestAuthorizationError):
            await env.svc.record_delete("semantic", "decisions/s.md", B)
        row = await env.raw("semantic", "decisions/s.md")
        assert row.deleted_at_ms is None and row.size_bytes == 1

    async def test_corpus_create_over_corpus_row_authorized_and_not(
        self, env: Env
    ) -> None:
        await env.seed("semantic", "decisions/s.md", CURATOR, tier="corpus", title="S")
        with pytest.raises(ManifestAuthorizationError):
            await env.svc.record_create(
                "semantic", "decisions/s.md", "x", 1, B, tier="corpus"
            )
        ok = await env.svc.record_create(
            "semantic",
            "decisions/s.md",
            "T",
            1,
            "curator-2",
            tier="corpus",
            caller_can_write_shared=True,
        )
        assert ok.title == "T" and ok.created_by_user_id == CURATOR


class TestForeignBackstop:
    """A private row at the caller's OWN target key that belongs to someone
    else (unreachable on Postgres after 034: the CHECK refuses the INSERT)."""

    async def test_foreign_row_at_target_refused_with_the_one_text(
        self, env: Env
    ) -> None:
        await env.seed("episodic", f"{B}/k.md", A)  # B's key, A's row
        before = await env.raw("episodic", f"{B}/k.md")
        with pytest.raises(CrossOwnerWriteError) as exc:
            await env.svc.record_create("episodic", "k.md", "x", 1, B)
        assert str(exc.value) == tier_conflict_message("episodic", "k.md")
        assert await env.raw("episodic", f"{B}/k.md") == before

    async def test_corpus_row_at_target_is_a_tier_conflict(self, env: Env) -> None:
        await env.seed("episodic", f"{B}/k.md", B, tier="corpus")
        with pytest.raises(CrossOwnerWriteError):
            await env.svc.record_create("episodic", "k.md", "x", 1, B)


class TestCopyForward:
    """RT-17: the owner's write over their OWN legacy row copy-forwards."""

    async def test_create_copies_forward_and_tombstones_legacy(self, env: Env) -> None:
        await env.seed("episodic", "k5.md", A, created_at_ms=1000, title="old")
        new = await env.svc.record_create("episodic", "k5.md", "new", 2, A)
        assert new.storage_key == f"{A}/k5.md" and new.key == "k5.md"
        assert new.created_at_ms == 1000  # audit history stays continuous
        legacy = await env.raw("episodic", "k5.md")
        assert legacy.deleted_at_ms is not None and legacy.deleted_by_user_id == A
        assert legacy.title == "old"

    async def test_update_copies_forward(self, env: Env) -> None:
        await env.seed("episodic", "k5.md", A, created_at_ms=1000, title="old")
        new = await env.svc.record_update("episodic", "k5.md", 4, A, title=None)
        assert new.storage_key == f"{A}/k5.md"
        assert new.created_at_ms == 1000 and new.title == "old" and new.size_bytes == 4
        assert (await env.raw("episodic", "k5.md")).deleted_at_ms is not None
        assert (await env.raw("episodic", f"{A}/k5.md")).deleted_at_ms is None

    async def test_delete_tombstones_the_legacy_row_in_place(self, env: Env) -> None:
        await env.seed("episodic", "k5.md", A)
        gone = await env.svc.record_delete("episodic", "k5.md", A)
        assert gone.deleted_by_user_id == A and gone.storage_key == "k5.md"

    async def test_update_of_a_tombstoned_legacy_row_is_refused(self, env: Env) -> None:
        await env.seed("episodic", "k5.md", A, deleted=True)
        with pytest.raises(RuntimeError, match="soft-deleted"):
            await env.svc.record_update("episodic", "k5.md", 1, A)

    async def test_create_over_a_tombstoned_own_legacy_is_a_plain_new_row(
        self, env: Env
    ) -> None:
        await env.seed("episodic", "k5.md", A, deleted=True, created_at_ms=1000)
        new = await env.svc.record_create("episodic", "k5.md", "n", 1, A)
        assert new.created_at_ms != 1000
        assert new.storage_key == f"{A}/k5.md"

    async def test_a_foreign_legacy_row_is_never_copied_forward(self, env: Env) -> None:
        await env.seed("episodic", "k6.md", A, title="A legacy")
        new = await env.svc.record_create("episodic", "k6.md", "B", 1, B)
        assert new.created_by_user_id == B and new.title == "B"
        assert (await env.raw("episodic", "k6.md")).title == "A legacy"


class TestNoAdminOrCuratorOverride:
    """RT-24 / O-3: an admin or a curator holding corpus-write authority gets
    NO access to another user's private row, new-form or legacy."""

    @pytest.mark.parametrize("legacy", [False, True], ids=["new-form", "legacy"])
    async def test_admin_update_and_delete_cannot_reach_a(
        self, env: Env, legacy: bool
    ) -> None:
        if legacy:
            await env.seed("semantic", "decisions/a.md", A, title="A")
            stored = "decisions/a.md"
        else:
            await env.svc.record_create("semantic", "decisions/a.md", "A", 1, A)
            stored = f"{A}/decisions/a.md"
        for who in (ADMIN, CURATOR):
            with pytest.raises(LookupError):
                await env.svc.record_update(
                    "semantic",
                    "decisions/a.md",
                    9,
                    who,
                    title="pwn",
                    caller_can_write_shared=True,
                )
            with pytest.raises(LookupError):
                await env.svc.record_delete(
                    "semantic", "decisions/a.md", who, caller_can_write_shared=True
                )
        row = await env.raw("semantic", stored)
        assert row.title == "A" and row.deleted_at_ms is None
        assert row.modified_by_user_id == A

    async def test_admin_authorize_write_lands_on_the_admins_own_key(
        self, env: Env
    ) -> None:
        await env.svc.record_create("semantic", "decisions/a.md", "A", 1, A)
        admin = _user(ADMIN, is_admin=True)
        await authorize_write(env.svc, admin, "semantic", "decisions/a.md")
        mine = await env.svc.record_create(
            "semantic",
            "decisions/a.md",
            "admin",
            1,
            ADMIN,
            caller_can_write_shared=True,
        )
        assert mine.storage_key == f"{ADMIN}/decisions/a.md"
        assert (await env.raw("semantic", f"{A}/decisions/a.md")).title == "A"

    @pytest.mark.parametrize("legacy", [False, True], ids=["new-form", "legacy"])
    async def test_admin_authorize_write_refuses_foreign_private_row(
        self, env: Env, legacy: bool
    ) -> None:
        """F1: no admin short-circuit inside ``authorize_write``. An admin
        holding the corpus-write scope still gets the owner-free refusal.

        legacy: corpus write over A's bare-key private row.
        new-form: a corpus write cannot collide with an owner-keyed row by
        design (RT-5a: distinct rows), so the new-form refusal is the private
        write whose target ``{admin}/{key}`` is held by a foreign row.
        """
        if legacy:
            await env.seed("semantic", "decisions/a.md", A, title="A")
            tier = "corpus"
        else:
            await env.seed("semantic", f"{ADMIN}/decisions/a.md", A, title="A")
            tier = "private"
        before = await env.all_stored_keys("semantic")
        admin = _user(ADMIN, is_admin=True, scopes=("memory:corpus:decisions:write",))
        with pytest.raises(CrossOwnerWriteError) as exc:
            await authorize_write(
                env.svc, admin, "semantic", "decisions/a.md", requested_tier=tier
            )
        assert str(exc.value) == tier_conflict_message("semantic", "decisions/a.md")
        assert A not in str(exc.value) and ADMIN not in str(exc.value)
        assert await env.all_stored_keys("semantic") == before

    async def test_admin_read_of_a_legacy_row_is_not_found(self, env: Env) -> None:
        await env.seed("semantic", "decisions/a.md", A)
        assert (
            await env.svc.resolve_read(
                "semantic", "decisions/a.md", _user(ADMIN, is_admin=True)
            )
            is None
        )


class TestAuthorizeWriteIsThePreWriteChoke:
    """Gate advisory: the refusal runs at ``authorize_write``, before any
    content write or manifest mutation."""

    async def test_corpus_write_over_private_row_refused_with_no_mutation(
        self, env: Env
    ) -> None:
        await env.seed("semantic", "decisions/k.md", A)
        before = await env.all_stored_keys("semantic")
        curator = _user(CURATOR, scopes=("memory:corpus:decisions:write",))
        with pytest.raises(CrossOwnerWriteError) as exc:
            await authorize_write(
                env.svc,
                curator,
                "semantic",
                "decisions/k.md",
                requested_tier="corpus",
            )
        assert str(exc.value) == tier_conflict_message("semantic", "decisions/k.md")
        assert await env.all_stored_keys("semantic") == before

    async def test_private_write_over_another_users_legacy_key_passes(
        self, env: Env
    ) -> None:
        await env.seed("semantic", "decisions/k.md", A)
        await authorize_write(env.svc, _user(B), "semantic", "decisions/k.md")

    async def test_private_write_beside_corpus_row_unauthorized_refused(
        self, env: Env
    ) -> None:
        await env.seed("semantic", "decisions/s.md", CURATOR, tier="corpus")
        with pytest.raises(ManifestAuthorizationError):
            await authorize_write(env.svc, _user(B), "semantic", "decisions/s.md")

    async def test_foreign_row_at_target_refused(self, env: Env) -> None:
        await env.seed("episodic", f"{B}/k.md", A)
        with pytest.raises(CrossOwnerWriteError):
            await authorize_write(env.svc, _user(B), "episodic", "k.md")

    async def test_authorize_then_record_agree(self, env: Env) -> None:
        user = _user(B)
        await authorize_write(env.svc, user, "episodic", "fresh.md")
        entry = await env.svc.record_create("episodic", "fresh.md", None, 1, B)
        assert entry.storage_key == f"{B}/fresh.md"
        await authorize_write(env.svc, user, "episodic", "fresh.md")  # now `own`


class TestRewrittenDefectPin:
    """Rewrite of ``tests/test_services_memory_manifest.py::
    TestAuthorizeWrite::test_existing_private_row_unaffected_by_guard``, which
    pinned the defect (bob's write "allowed" over alice's private row because
    it landed on alice's row). Now bob's resolved target is HIS OWN storage
    key and alice's row is unchanged afterwards."""

    async def test_bobs_target_is_his_own_key_and_alices_row_is_unchanged(
        self, env: Env
    ) -> None:
        alice_row = await env.svc.record_create(
            "episodic", "own.md", "Mine", 10, "alice", tier="private"
        )
        await authorize_write(env.svc, _user("bob"), "episodic", "own.md")
        bob_row = await env.svc.record_create("episodic", "own.md", "Bob", 1, "bob")
        assert bob_row.storage_key == "bob/own.md"
        assert bob_row.id != alice_row.id
        assert await env.raw("episodic", "alice/own.md") == alice_row


class TestEntriesCarryTheLogicalKey:
    """RT-12 + the C3 pin: no entry any service method returns carries the
    owner prefix for a private row."""

    async def test_every_returned_entry_is_logical(self, env: Env) -> None:
        created = await env.svc.record_create("semantic", "decisions/x.md", "t", 1, A)
        updated = await env.svc.record_update("semantic", "decisions/x.md", 2, A)
        read = await env.svc.resolve_read("semantic", "decisions/x.md", _user(A))
        listed = await env.svc.list_for_layer("semantic", caller=_user(A))
        deleted = await env.svc.record_delete("semantic", "decisions/x.md", A)
        entries = [created, updated, read, deleted, *listed]
        assert len(entries) == 5
        for entry in entries:
            assert entry is not None
            assert entry.key == "decisions/x.md"
            assert not entry.key.startswith(f"{A}/")
            assert entry.storage_key == f"{A}/decisions/x.md"
            assert "storage_key" not in entry.to_dict()
            assert entry.to_dict()["key"] == "decisions/x.md"

    async def test_create_response_key_equals_the_key_sent(self, env: Env) -> None:
        entry = await env.svc.record_create("episodic", "a/b.md", None, 1, A)
        assert entry.key == "a/b.md"

    async def test_legacy_and_corpus_rows_keep_their_stored_key(self, env: Env) -> None:
        await env.seed("semantic", "decisions/legacy.md", A)
        await env.seed("semantic", "decisions/corp.md", CURATOR, tier="corpus")
        keys = {e.key: e.storage_key for e in await env.svc.list_for_layer("semantic")}
        assert keys == {
            "decisions/legacy.md": "decisions/legacy.md",
            "decisions/corp.md": "decisions/corp.md",
        }


class TestIndexStatusLogicalKey:
    """C3 reader #3: ``index_status_summary`` shows LOGICAL keys to the model."""

    async def test_matched_key_is_logical(self, env: Env) -> None:
        await env.svc.record_create("semantic", "decisions/adr-77.md", None, 1, A)
        await env.scanned_clean("semantic", f"{A}/decisions/adr-77.md")
        summary = await env.svc.index_status_summary(_user(A), ["adr-77"])
        assert [m.key for m in summary.matched] == ["decisions/adr-77.md"]

    async def test_other_users_new_form_row_is_not_counted(self, env: Env) -> None:
        await env.svc.record_create("semantic", "decisions/adr-77.md", None, 1, A)
        await env.scanned_clean("semantic", f"{A}/decisions/adr-77.md")
        summary = await env.svc.index_status_summary(_user(B), ["adr-77"])
        assert summary.matched == [] and summary.pending == 0


class TestTombstoneCandidates:
    """C3 reader #4: the soft-delete lookup finds a NEW-form tombstone when
    given ``deleted_key_candidates`` (one candidate per row: owner-keyed for a stamped chunk, bare for a legacy one)."""

    async def test_new_form_tombstone_found_through_candidates(self, env: Env) -> None:
        await env.svc.record_create("semantic", "decisions/d1", None, 1, A)
        await env.svc.record_delete("semantic", "decisions/d1", A)
        candidates = deleted_key_candidates("decisions", f"{A}__d1", A)
        assert await env.svc.get_deleted_keys("semantic", candidates) == {
            f"{A}/decisions/d1"
        }

    async def test_live_new_form_row_not_returned(self, env: Env) -> None:
        await env.svc.record_create("semantic", "decisions/d1", None, 1, A)
        candidates = deleted_key_candidates("decisions", f"{A}__d1", A)
        assert await env.svc.get_deleted_keys("semantic", candidates) == set()

    async def test_legacy_tombstone_still_found(self, env: Env) -> None:
        await env.seed("semantic", "decisions/d2", A, deleted=True)
        candidates = deleted_key_candidates("decisions", "d2", None)
        assert await env.svc.get_deleted_keys("semantic", candidates) == {
            "decisions/d2"
        }


class TestLogicalKeyCap:
    """S1: a 219-char logical key is refused (->400), 218 is accepted even
    with a 36-char sub, and never reaches the DB as a 500."""

    async def test_218_with_a_36_char_sub_is_accepted(self, env: Env) -> None:
        sub = "s" * 36
        entry = await env.svc.record_create("episodic", "k" * 218, None, 1, sub)
        assert len(entry.storage_key) == 255 and entry.key == "k" * 218

    async def test_219_is_refused_before_any_write(self, env: Env) -> None:
        with pytest.raises(LogicalKeyTooLongError):
            await env.svc.record_create("episodic", "k" * 219, None, 1, A)
        assert await env.all_stored_keys("episodic") == set()

    async def test_authorize_write_refuses_219(self, env: Env) -> None:
        with pytest.raises(LogicalKeyTooLongError):
            await authorize_write(env.svc, _user(A), "episodic", "k" * 219)


class TestInvalidSub:
    """RT-26b: a sub that would make private prefixes non-injective is refused
    (403 family), never a 500, and nothing is written."""

    @pytest.mark.parametrize("sub", ["a_", "a/b", "a__b"])
    async def test_record_create_refused(self, env: Env, sub: str) -> None:
        with pytest.raises(InvalidPrivateSubjectError):
            await env.svc.record_create("episodic", "k.md", None, 1, sub)
        assert await env.all_stored_keys("episodic") == set()

    @pytest.mark.parametrize("sub", ["a_", "a/b", "a__b"])
    async def test_authorize_write_refused(self, env: Env, sub: str) -> None:
        with pytest.raises(ManifestAuthorizationError):
            await authorize_write(env.svc, _user(sub), "episodic", "k.md")

    async def test_corpus_write_does_not_need_a_valid_sub(self, env: Env) -> None:
        entry = await env.svc.record_create(
            "episodic", "k.md", None, 1, "a__b", tier="corpus"
        )
        assert entry.storage_key == "k.md"
