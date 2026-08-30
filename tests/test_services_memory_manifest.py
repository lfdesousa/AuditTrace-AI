"""Unit tests for ``MemoryManifestService`` + ``MockMemoryManifestService``.

Covers the contract documented in
``src/audittrace/services/memory_manifest.py`` — record_create,
record_update, record_delete, list_for_layer, get + the
``ManifestEntry`` dataclass round-trip.

Tests run against the Mock implementation. The real Postgres-backed
``MemoryManifestService`` is exercised via the ``test_memory_routes.py``
integration tests through the in-memory PostgresFactory.
"""

from __future__ import annotations

import time

import pytest
import pytest_asyncio
from sqlalchemy import update

from audittrace.db.models import MemoryItem
from audittrace.identity import UserContext
from audittrace.services.memory_manifest import (
    IndexStatusSummary,
    ManifestAuthorizationError,
    ManifestEntry,
    MatchedUnindexed,
    MemoryManifestService,
    MockMemoryManifestService,
    _now_ms,
    _validate_layer,
    authorize_write,
)


@pytest.fixture
def manifest() -> MockMemoryManifestService:
    return MockMemoryManifestService()


def _user(
    user_id: str, *, is_admin: bool = False, scopes: tuple[str, ...] = ()
) -> UserContext:
    """Minimal non-admin UserContext for the WU-B4 caller-predicate tests."""
    return UserContext(
        user_id=user_id,
        username=user_id,
        agent_type="test",
        scopes=scopes,
        is_admin=is_admin,
    )


class TestNowMs:
    def test_returns_milliseconds_not_seconds(self) -> None:
        now = _now_ms()
        # > 1e12 means we're in millis (we'd be in seconds territory if
        # this returned 1.7e9).
        assert now > 10**12
        # Plausibly current.
        assert abs(now - int(time.time() * 1000)) < 1000


class TestValidateLayer:
    def test_accepts_valid(self) -> None:
        for layer in ("episodic", "procedural", "semantic"):
            _validate_layer(layer)  # no raise

    def test_rejects_invalid(self) -> None:
        for bad in ("conversational", "EPISODIC", "", "anything"):
            with pytest.raises(ValueError, match="Invalid memory layer"):
                _validate_layer(bad)


class TestAuthorizeWrite:
    """SPEC security-memory-write-authorization-choke (2026-08-30) — the
    PRIMARY pre-write choke, unit-tested directly (independent of any
    route/pipeline call site) so every branch is exercised in isolation."""

    async def test_new_private_item_passes(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await authorize_write(manifest, _user("writer"), "semantic", "decisions/new")

    async def test_new_item_requested_corpus_unauthorized_raises(
        self, manifest: MockMemoryManifestService
    ) -> None:
        with pytest.raises(ManifestAuthorizationError):
            await authorize_write(
                manifest,
                _user("writer", scopes=("memory:semantic:write",)),
                "semantic",
                "decisions/new-corpus",
                requested_tier="corpus",
            )

    async def test_new_item_requested_corpus_authorized_passes(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await authorize_write(
            manifest,
            _user("curator", scopes=("memory:corpus:decisions:write",)),
            "semantic",
            "decisions/new-corpus-ok",
            requested_tier="corpus",
        )

    async def test_existing_corpus_row_unauthorized_raises(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create(
            "semantic", "decisions/shared", "Shared", 10, "curator", tier="corpus"
        )
        with pytest.raises(ManifestAuthorizationError):
            await authorize_write(
                manifest,
                _user("attacker", scopes=("memory:semantic:write",)),
                "semantic",
                "decisions/shared",
            )

    async def test_existing_corpus_row_authorized_passes(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create(
            "semantic", "decisions/shared2", "Shared", 10, "curator", tier="corpus"
        )
        await authorize_write(
            manifest,
            _user("curator2", scopes=("memory:corpus:decisions:write",)),
            "semantic",
            "decisions/shared2",
        )

    async def test_admin_always_passes(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create(
            "semantic", "decisions/shared3", "Shared", 10, "curator", tier="corpus"
        )
        await authorize_write(
            manifest, _user("ops", is_admin=True), "semantic", "decisions/shared3"
        )

    async def test_existing_private_row_unaffected_by_guard(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create(
            "episodic", "own.md", "Mine", 10, "alice", tier="private"
        )
        # bob has no scope at all — still allowed, since private-tier
        # cross-owner collisions are a separate, already-flagged follow-up.
        await authorize_write(manifest, _user("bob"), "episodic", "own.md")

    async def test_owner_exempt_true_lets_owner_touch_own_corpus_row(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create(
            "episodic", "shared.pdf", "Shared", 10, "alice", tier="corpus"
        )
        # No shared-write scope at all — owner_exempt is what saves it.
        await authorize_write(
            manifest, _user("alice"), "episodic", "shared.pdf", owner_exempt=True
        )

    async def test_owner_exempt_true_still_blocks_non_owner(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create(
            "episodic", "shared2.pdf", "Shared", 10, "alice", tier="corpus"
        )
        with pytest.raises(ManifestAuthorizationError):
            await authorize_write(
                manifest,
                _user("attacker"),
                "episodic",
                "shared2.pdf",
                owner_exempt=True,
            )


class TestRecordCreate:
    async def test_first_create_sets_created_modified_to_same(
        self, manifest: MockMemoryManifestService
    ) -> None:
        entry = await manifest.record_create(
            "episodic", "ADR-x.md", "Title X", 100, "user-alice"
        )
        assert entry.layer == "episodic"
        assert entry.key == "ADR-x.md"
        assert entry.title == "Title X"
        assert entry.size_bytes == 100
        assert entry.created_at_ms == entry.modified_at_ms
        assert entry.created_by_user_id == "user-alice"
        assert entry.modified_by_user_id == "user-alice"
        assert entry.deleted_at_ms is None

    async def test_recreate_revives_soft_deleted(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create("episodic", "k.md", None, 1, "alice")
        await manifest.record_delete("episodic", "k.md", "alice")
        # Recreate
        revived = await manifest.record_create(
            "episodic", "k.md", "new title", 2, "bob"
        )
        assert revived.deleted_at_ms is None
        assert revived.deleted_by_user_id is None
        assert revived.title == "new title"
        assert revived.size_bytes == 2
        assert revived.modified_by_user_id == "bob"

    async def test_recreate_existing_live_row_overwrites(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create("episodic", "k.md", "v1", 10, "alice")
        again = await manifest.record_create("episodic", "k.md", "v2", 20, "bob")
        assert again.title == "v2"
        assert again.size_bytes == 20
        # Created_at preserved (the row was created by alice originally)
        # — Mock implementation keeps the original entry's id and adds
        # modifications, but doesn't preserve created_at across overwrite.
        # That's a divergence from the Postgres path which DOES preserve.
        # Documented for awareness.

    async def test_rejects_invalid_layer(
        self, manifest: MockMemoryManifestService
    ) -> None:
        with pytest.raises(ValueError):
            await manifest.record_create("conversational", "x", None, 0, "u")

    # ── ADR-062 Phase B (WU-B4/B5) — tier default + persistence ──────────

    async def test_tier_defaults_to_private(
        self, manifest: MockMemoryManifestService
    ) -> None:
        """D2: new writes default private — even a caller of the SERVICE
        (not just the route) that forgets to pass ``tier`` explicitly
        lands on the fail-closed (least-shared) side."""
        entry = await manifest.record_create("episodic", "new.md", None, 1, "alice")
        assert entry.tier == "private"

    async def test_tier_explicit_corpus_is_persisted(
        self, manifest: MockMemoryManifestService
    ) -> None:
        entry = await manifest.record_create(
            "semantic", "decisions/x", None, 1, "curator", tier="corpus"
        )
        assert entry.tier == "corpus"

    async def test_recreate_re_stamps_tier(
        self, manifest: MockMemoryManifestService
    ) -> None:
        """Recreating an existing row updates its tier too (same branch
        that already updates title/size_bytes on overwrite) — PROVIDED
        the caller is authorized for a shared write (SPEC security-
        memory-manifest-tier-authz, 2026-08-30: a tier change on an
        existing row now requires ``caller_can_write_shared``, same as
        an outright corpus overwrite)."""
        await manifest.record_create(
            "episodic", "k.md", None, 1, "alice", tier="private"
        )
        again = await manifest.record_create(
            "episodic",
            "k.md",
            None,
            1,
            "curator",
            tier="corpus",
            caller_can_write_shared=True,
        )
        assert again.tier == "corpus"

    async def test_recreate_unauthorized_tier_change_preserves_tier(
        self, manifest: MockMemoryManifestService
    ) -> None:
        """Without ``caller_can_write_shared``, requesting ``tier="corpus"``
        on an existing PRIVATE row does NOT promote it — the tier is
        preserved (the write itself still succeeds; only the tier field
        is protected) — defense-in-depth at the manifest choke, per the
        SPEC's "only set existing.tier = tier when authorized" design."""
        await manifest.record_create(
            "episodic", "k.md", None, 1, "alice", tier="private"
        )
        again = await manifest.record_create(
            "episodic", "k.md", "still mine", 2, "alice", tier="corpus"
        )
        assert again.tier == "private"
        assert again.title == "still mine"

    # ── SPEC security-memory-manifest-tier-authz (2026-08-30) ────────────
    # The M3-WU-D2-2 reviewer's cross-user corpus-hijack/hide finding:
    # create()-over-an-existing-corpus-row had NO authorization check at
    # all. FALSIFIABLE: neuter ``_tier_write_unauthorized`` (e.g. make it
    # always return ``False``) and
    # ``test_unauthorized_create_over_corpus_row_raises`` goes RED
    # (no exception, the row silently gets demoted/re-authored); restore
    # it and it goes GREEN.

    async def test_unauthorized_create_over_corpus_row_raises(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create(
            "semantic", "decisions/shared", "Shared", 10, "curator", tier="corpus"
        )
        with pytest.raises(ManifestAuthorizationError):
            await manifest.record_create(
                "semantic",
                "decisions/shared",
                "pwned",
                99,
                "attacker",
                tier="private",
            )
        # No tier change, no overwrite, no re-authorship.
        row = await manifest.get("semantic", "decisions/shared")
        assert row is not None
        assert row.tier == "corpus"
        assert row.title == "Shared"
        assert row.created_by_user_id == "curator"
        assert row.modified_by_user_id == "curator"

    async def test_authorized_create_over_corpus_row_succeeds(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create(
            "semantic", "decisions/shared", "Shared", 10, "curator", tier="corpus"
        )
        updated = await manifest.record_create(
            "semantic",
            "decisions/shared",
            "Updated",
            20,
            "curator-2",
            tier="corpus",
            caller_can_write_shared=True,
        )
        assert updated.tier == "corpus"
        assert updated.title == "Updated"
        assert updated.modified_by_user_id == "curator-2"
        # created_by_user_id is IMMUTABLE even on an authorized overwrite.
        assert updated.created_by_user_id == "curator"


class TestRecordUpdate:
    async def test_update_bumps_modified_only(
        self, manifest: MockMemoryManifestService
    ) -> None:
        e1 = await manifest.record_create("episodic", "k.md", "v1", 10, "alice")
        # Sleep just enough to guarantee a different millisecond.
        time.sleep(0.002)
        e2 = await manifest.record_update("episodic", "k.md", 20, "bob", title="v2")
        assert e2.created_at_ms == e1.created_at_ms
        assert e2.modified_at_ms > e1.modified_at_ms
        assert e2.created_by_user_id == "alice"
        assert e2.modified_by_user_id == "bob"
        assert e2.title == "v2"
        assert e2.size_bytes == 20

    async def test_update_title_none_preserves_existing(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create("episodic", "k.md", "stays", 1, "alice")
        e2 = await manifest.record_update("episodic", "k.md", 2, "bob", title=None)
        assert e2.title == "stays"

    async def test_update_missing_raises(
        self, manifest: MockMemoryManifestService
    ) -> None:
        with pytest.raises(LookupError):
            await manifest.record_update("episodic", "missing.md", 1, "u")

    async def test_update_soft_deleted_raises(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create("episodic", "k.md", None, 1, "alice")
        await manifest.record_delete("episodic", "k.md", "alice")
        with pytest.raises(RuntimeError, match="soft-deleted"):
            await manifest.record_update("episodic", "k.md", 2, "bob")

    # ── SPEC security-memory-manifest-tier-authz (2026-08-30) ────────────
    # ``record_update`` had the SAME missing-authz shape as
    # ``record_create``: it unconditionally overwrote ``title`` +
    # ``modified_by_user_id`` of any existing row, across tiers, with no
    # ownership/tier check. FALSIFIABLE: neuter
    # ``_tier_write_unauthorized`` and
    # ``test_unauthorized_update_over_corpus_row_raises`` goes RED
    # (the corpus row's title/modified_by silently changes); restore and
    # it goes GREEN.

    async def test_unauthorized_update_over_corpus_row_raises(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create(
            "semantic", "decisions/shared-u", "Shared", 10, "curator", tier="corpus"
        )
        with pytest.raises(ManifestAuthorizationError):
            await manifest.record_update(
                "semantic", "decisions/shared-u", 99, "attacker", title="pwned"
            )
        row = await manifest.get("semantic", "decisions/shared-u")
        assert row is not None
        assert row.tier == "corpus"
        assert row.title == "Shared"
        assert row.modified_by_user_id == "curator"
        assert row.size_bytes == 10

    async def test_authorized_update_over_corpus_row_succeeds(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create(
            "semantic", "decisions/shared-u2", "Shared", 10, "curator", tier="corpus"
        )
        updated = await manifest.record_update(
            "semantic",
            "decisions/shared-u2",
            20,
            "curator-2",
            title="Revised",
            caller_can_write_shared=True,
        )
        assert updated.title == "Revised"
        assert updated.modified_by_user_id == "curator-2"
        assert updated.created_by_user_id == "curator"


class TestRecordDelete:
    async def test_delete_sets_timestamp_and_user(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create("episodic", "k.md", None, 1, "alice")
        d = await manifest.record_delete("episodic", "k.md", "bob")
        assert d.deleted_at_ms is not None
        assert d.deleted_by_user_id == "bob"

    async def test_delete_already_deleted_is_idempotent(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create("episodic", "k.md", None, 1, "alice")
        d1 = await manifest.record_delete("episodic", "k.md", "bob")
        d2 = await manifest.record_delete("episodic", "k.md", "cleo")
        # Second call returns existing entry, doesn't update deleter.
        assert d2.deleted_at_ms == d1.deleted_at_ms
        assert d2.deleted_by_user_id == "bob"

    async def test_delete_missing_raises(
        self, manifest: MockMemoryManifestService
    ) -> None:
        with pytest.raises(LookupError):
            await manifest.record_delete("episodic", "missing.md", "u")

    # ── SPEC security-memory-manifest-tier-authz (2026-08-30) ────────────
    # The LAST of the four manifest mutation methods to gain the guard —
    # the M3-WU-D2-2 reviewer's third REJECT. FALSIFIABLE: neuter
    # ``_tier_write_unauthorized`` and
    # ``test_unauthorized_delete_over_corpus_row_raises`` goes RED (no
    # exception, the corpus row gets silently tombstoned); restore it and
    # it goes GREEN.

    async def test_unauthorized_delete_over_corpus_row_raises(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create(
            "semantic", "decisions/shared-d", "Shared", 10, "curator", tier="corpus"
        )
        with pytest.raises(ManifestAuthorizationError):
            await manifest.record_delete("semantic", "decisions/shared-d", "attacker")
        row = await manifest.get("semantic", "decisions/shared-d")
        assert row is not None
        assert row.tier == "corpus"
        assert row.deleted_at_ms is None
        assert row.deleted_by_user_id is None
        assert row.created_by_user_id == "curator"

    async def test_authorized_delete_over_corpus_row_succeeds(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create(
            "semantic", "decisions/shared-d2", "Shared", 10, "curator", tier="corpus"
        )
        d = await manifest.record_delete(
            "semantic", "decisions/shared-d2", "curator-2", caller_can_write_shared=True
        )
        assert d.deleted_at_ms is not None
        assert d.deleted_by_user_id == "curator-2"

    async def test_own_private_item_delete_unaffected_by_guard(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create(
            "semantic", "decisions/own-d", "Mine", 10, "alice", tier="private"
        )
        d = await manifest.record_delete("semantic", "decisions/own-d", "alice")
        assert d.deleted_at_ms is not None
        assert d.deleted_by_user_id == "alice"


class TestListForLayer:
    async def test_excludes_deleted_by_default(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create("episodic", "live.md", None, 1, "u")
        await manifest.record_create("episodic", "deleted.md", None, 1, "u")
        await manifest.record_delete("episodic", "deleted.md", "u")
        rows = await manifest.list_for_layer("episodic")
        keys = {r.key for r in rows}
        assert keys == {"live.md"}

    async def test_include_deleted_returns_all(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create("episodic", "live.md", None, 1, "u")
        await manifest.record_create("episodic", "deleted.md", None, 1, "u")
        await manifest.record_delete("episodic", "deleted.md", "u")
        rows = await manifest.list_for_layer("episodic", include_deleted=True)
        assert {r.key for r in rows} == {"live.md", "deleted.md"}

    async def test_layer_isolation(self, manifest: MockMemoryManifestService) -> None:
        await manifest.record_create("episodic", "a.md", None, 1, "u")
        await manifest.record_create("procedural", "b.md", None, 1, "u")
        await manifest.record_create("semantic", "c/d", None, 1, "u")
        assert {r.key for r in await manifest.list_for_layer("episodic")} == {"a.md"}
        assert {r.key for r in await manifest.list_for_layer("procedural")} == {"b.md"}
        assert {r.key for r in await manifest.list_for_layer("semantic")} == {"c/d"}

    async def test_ordered_by_modified_at_desc(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create("episodic", "first.md", None, 1, "u")
        time.sleep(0.002)
        await manifest.record_create("episodic", "second.md", None, 1, "u")
        time.sleep(0.002)
        await manifest.record_update("episodic", "first.md", 2, "u")
        rows = await manifest.list_for_layer("episodic")
        # `first.md` was modified most recently → ordered first.
        assert [r.key for r in rows] == ["first.md", "second.md"]


class TestListForLayerCallerPredicate:
    """ADR-062 Phase B (WU-B4) — ``list_for_layer(caller=...)`` owner-or-
    corpus isolation. FALSIFIABLE: neuter the predicate in
    ``MockMemoryManifestService.list_for_layer`` (e.g. hardcode the
    filter condition to ``True``) and
    ``test_non_admin_sees_own_and_corpus_not_others_private`` goes RED
    (bob's private row leaks to alice); restore it and it goes green.
    ``test_corpus_row_visible_to_everyone`` and
    ``test_admin_bypasses_the_predicate`` pin the two non-leak arms so a
    "fix" can't just hide everything.
    """

    async def test_caller_none_is_unfiltered(
        self, manifest: MockMemoryManifestService
    ) -> None:
        """Backward compatibility: omitting ``caller`` (the pre-WU-B4
        default) returns the full operator-global view, same as every
        pre-existing call site that doesn't pass it."""
        await manifest.record_create("episodic", "a.md", None, 1, "alice")
        await manifest.record_create("episodic", "b.md", None, 1, "bob")
        rows = await manifest.list_for_layer("episodic")
        assert {r.key for r in rows} == {"a.md", "b.md"}

    async def test_non_admin_sees_own_and_corpus_not_others_private(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create(
            "episodic", "alice-private.md", None, 1, "alice", tier="private"
        )
        await manifest.record_create(
            "episodic", "bob-private.md", None, 1, "bob", tier="private"
        )
        rows = await manifest.list_for_layer("episodic", caller=_user("alice"))
        keys = {r.key for r in rows}
        assert "alice-private.md" in keys
        assert "bob-private.md" not in keys, f"leaked bob's private row: {keys}"

    async def test_corpus_row_visible_to_everyone(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create(
            "episodic", "shared-adr.md", None, 1, "curator", tier="corpus"
        )
        rows = await manifest.list_for_layer("episodic", caller=_user("alice"))
        assert {r.key for r in rows} == {"shared-adr.md"}

    async def test_admin_bypasses_the_predicate(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create(
            "episodic", "alice-private.md", None, 1, "alice", tier="private"
        )
        await manifest.record_create(
            "episodic", "bob-private.md", None, 1, "bob", tier="private"
        )
        rows = await manifest.list_for_layer(
            "episodic", caller=_user("ops", is_admin=True)
        )
        assert {r.key for r in rows} == {"alice-private.md", "bob-private.md"}


# ── SPEC #374 WU-1: index_status_summary (Mock) ─────────────────────────────


class TestIndexStatusSummaryMock:
    """``MockMemoryManifestService.index_status_summary`` — SPEC #374 WU-1.

    FALSIFIABLE: neuter a count predicate (e.g. drop the
    ``index_failed_at_ms is None`` arm from the pending count) and
    ``test_counts_pending_and_dead_lettered_separately`` goes RED; neuter
    the token-match predicate (e.g. hardcode ``True``) and
    ``test_matched_names_only_the_matching_doc`` goes RED (an unrelated
    pending doc would wrongly appear); drop the tenancy filter (mirrors
    ``TestListForLayerCallerPredicate``) and
    ``test_non_admin_never_sees_another_users_private_counts`` goes RED.
    """

    async def test_counts_pending_and_dead_lettered_separately(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create(
            "semantic", "decisions/pending.md", None, 1, "alice", tier="corpus"
        )
        await manifest.record_create(
            "semantic", "decisions/dead.md", None, 1, "alice", tier="corpus"
        )
        manifest.set_index_state(
            "semantic",
            "decisions/dead.md",
            index_failed_at_ms=123,
            index_failure_code="pdf_corrupted_structure",
        )
        summary = await manifest.index_status_summary(_user("alice"), [])
        assert summary.pending == 1
        assert summary.dead_lettered == 1

    async def test_indexed_doc_counts_as_neither(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create(
            "semantic", "decisions/done.md", None, 1, "alice", tier="corpus"
        )
        manifest.set_index_state("semantic", "decisions/done.md", indexed_at_ms=999)
        summary = await manifest.index_status_summary(_user("alice"), [])
        assert summary.pending == 0
        assert summary.dead_lettered == 0

    async def test_soft_deleted_doc_excluded_from_counts(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create(
            "semantic", "decisions/gone.md", None, 1, "alice", tier="corpus"
        )
        # SPEC security-memory-manifest-tier-authz (2026-08-30): deleting a
        # corpus-tier row (even one's own) now requires
        # ``caller_can_write_shared=True`` — corpus rows are shared, not
        # owned, same strict model ``record_create``/``record_update`` use.
        await manifest.record_delete(
            "semantic", "decisions/gone.md", "alice", caller_can_write_shared=True
        )
        summary = await manifest.index_status_summary(_user("alice"), [])
        assert summary.pending == 0
        assert summary.dead_lettered == 0

    async def test_no_tokens_returns_counts_without_matching(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create(
            "semantic", "decisions/ADR-058.pdf", None, 1, "alice", tier="corpus"
        )
        manifest.set_index_state(
            "semantic",
            "decisions/ADR-058.pdf",
            index_failed_at_ms=1,
            index_failure_code="pdf_corrupted_structure",
        )
        summary = await manifest.index_status_summary(_user("alice"), [])
        assert summary.dead_lettered == 1
        assert summary.matched == []

    async def test_skip_counts_if_no_match_skips_counts(
        self, manifest: MockMemoryManifestService
    ) -> None:
        # real pending + dead-lettered rows present
        await manifest.record_create(
            "semantic", "decisions/pending.md", None, 1, "alice", tier="corpus"
        )
        await manifest.record_create(
            "semantic", "decisions/dead.md", None, 1, "alice", tier="corpus"
        )
        manifest.set_index_state(
            "semantic",
            "decisions/dead.md",
            index_failed_at_ms=1,
            index_failure_code="pdf_corrupted_structure",
        )
        # query token-matches NOTHING, hot-path short-circuit on
        summary = await manifest.index_status_summary(
            _user("alice"), ["zzznomatchxyz"], skip_counts_if_no_match=True
        )
        assert summary.matched == []
        # counts SKIPPED (uncomputed) → 0 despite the real pending(1)+dead(1) rows.
        # Neuter proof: drop the short-circuit and these become 1/1 → RED.
        assert summary.pending == 0
        assert summary.dead_lettered == 0

    async def test_matched_names_only_the_matching_doc(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create(
            "semantic",
            "decisions/ADR-058-outage.pdf",
            None,
            1,
            "alice",
            tier="corpus",
        )
        await manifest.record_create(
            "semantic", "decisions/unrelated.pdf", None, 1, "alice", tier="corpus"
        )
        manifest.set_index_state(
            "semantic",
            "decisions/ADR-058-outage.pdf",
            index_failed_at_ms=1,
            index_failure_code="pdf_corrupted_structure",
        )
        manifest.set_index_state("semantic", "decisions/unrelated.pdf")
        summary = await manifest.index_status_summary(_user("alice"), ["adr-058"])
        assert [m.key for m in summary.matched] == ["decisions/ADR-058-outage.pdf"]
        assert summary.matched[0].state == "dead_lettered"
        assert summary.matched[0].index_failure_code == "pdf_corrupted_structure"

    async def test_matched_caps_at_five(
        self, manifest: MockMemoryManifestService
    ) -> None:
        for i in range(7):
            key = f"decisions/adr-{i}.pdf"
            await manifest.record_create(
                "semantic", key, None, 1, "alice", tier="corpus"
            )
            manifest.set_index_state("semantic", key)
        summary = await manifest.index_status_summary(_user("alice"), ["adr"])
        assert len(summary.matched) == 5

    async def test_pending_and_dead_lettered_both_match(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create(
            "semantic", "decisions/adr-pending.pdf", None, 1, "alice", tier="corpus"
        )
        await manifest.record_create(
            "semantic", "decisions/adr-dead.pdf", None, 1, "alice", tier="corpus"
        )
        manifest.set_index_state("semantic", "decisions/adr-pending.pdf")
        manifest.set_index_state(
            "semantic",
            "decisions/adr-dead.pdf",
            index_failed_at_ms=1,
            index_failure_code="max_attempts_exceeded",
        )
        summary = await manifest.index_status_summary(_user("alice"), ["adr"])
        states = {m.key: m.state for m in summary.matched}
        assert states == {
            "decisions/adr-pending.pdf": "pending",
            "decisions/adr-dead.pdf": "dead_lettered",
        }

    async def test_non_admin_never_sees_another_users_private_counts(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create(
            "semantic", "decisions/bob-private.pdf", None, 1, "bob", tier="private"
        )
        manifest.set_index_state(
            "semantic",
            "decisions/bob-private.pdf",
            index_failed_at_ms=1,
            index_failure_code="pdf_corrupted_structure",
        )
        summary = await manifest.index_status_summary(_user("alice"), ["private"])
        assert summary.pending == 0
        assert summary.dead_lettered == 0
        assert summary.matched == []

    async def test_corpus_tier_doc_counted_for_every_caller(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create(
            "semantic", "decisions/shared.pdf", None, 1, "curator", tier="corpus"
        )
        summary = await manifest.index_status_summary(_user("alice"), [])
        assert summary.pending == 1

    async def test_admin_bypasses_tenancy_predicate(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create(
            "semantic", "decisions/bob-private.pdf", None, 1, "bob", tier="private"
        )
        summary = await manifest.index_status_summary(_user("ops", is_admin=True), [])
        assert summary.pending == 1

    async def test_set_index_state_missing_row_raises(
        self, manifest: MockMemoryManifestService
    ) -> None:
        with pytest.raises(LookupError):
            manifest.set_index_state("semantic", "never.pdf")

    def test_dataclasses_are_frozen_plain_data(self) -> None:
        """Round-trip the two SPEC #374 dataclasses directly — pins the
        field names/shape a serialiser (``tools.memory_handlers.
        _maybe_corpus_status``) depends on."""
        matched = MatchedUnindexed(
            key="decisions/ADR-058.pdf",
            state="dead_lettered",
            index_failure_code="pdf_corrupted_structure",
        )
        summary = IndexStatusSummary(pending=2, dead_lettered=4, matched=[matched])
        assert summary.pending == 2
        assert summary.dead_lettered == 4
        assert summary.matched[0].key == "decisions/ADR-058.pdf"
        assert summary.matched[0].state == "dead_lettered"
        assert summary.matched[0].index_failure_code == "pdf_corrupted_structure"
        with pytest.raises(AttributeError):
            matched.key = "mutated"  # type: ignore[misc]


# ── SPEC #374 WU-1: index_status_summary (real Postgres-backed) ────────────


class TestIndexStatusSummaryPostgres:
    """Same contract as ``TestIndexStatusSummaryMock``, exercised against
    the real SQL query construction (``pg_manifest``, async in-memory
    SQLite) — proves the ``ilike``/``LIMIT``/tenancy-``WHERE`` actually
    compile and run, not just the dict-filtering mock."""

    async def _stamp(
        self,
        pg_manifest: MemoryManifestService,
        layer: str,
        key: str,
        *,
        indexed_at_ms: int | None = None,
        index_failed_at_ms: int | None = None,
        index_failure_code: str | None = None,
    ) -> None:
        """Directly UPDATE the migration-019/020 columns ``record_create``
        never touches — mirrors how ``IndexWorker``/``ScanVerdictConsumer``
        stamp them in production, without pulling that machinery into a
        service-level unit test."""
        async with pg_manifest._session_factory() as session:
            await session.execute(
                update(MemoryItem)
                .where(MemoryItem.layer == layer, MemoryItem.key == key)
                .values(
                    indexed_at_ms=indexed_at_ms,
                    index_failed_at_ms=index_failed_at_ms,
                    index_failure_code=index_failure_code,
                )
            )
            await session.commit()

    async def test_counts_pending_and_dead_lettered_separately(
        self, pg_manifest: MemoryManifestService
    ) -> None:
        await pg_manifest.record_create(
            "semantic", "decisions/pending.md", None, 1, "alice", tier="corpus"
        )
        await pg_manifest.record_create(
            "semantic", "decisions/dead.md", None, 1, "alice", tier="corpus"
        )
        await self._stamp(
            pg_manifest,
            "semantic",
            "decisions/dead.md",
            index_failed_at_ms=123,
            index_failure_code="pdf_corrupted_structure",
        )
        summary = await pg_manifest.index_status_summary(_user("alice"), [])
        assert summary.pending == 1
        assert summary.dead_lettered == 1

    async def _seed_pending_and_dead(self, pg_manifest: MemoryManifestService) -> None:
        await pg_manifest.record_create(
            "semantic", "decisions/pending.md", None, 1, "alice", tier="corpus"
        )
        await pg_manifest.record_create(
            "semantic", "decisions/dead.md", None, 1, "alice", tier="corpus"
        )
        await self._stamp(
            pg_manifest,
            "semantic",
            "decisions/dead.md",
            index_failed_at_ms=1,
            index_failure_code="pdf_corrupted_structure",
        )

    async def test_skip_counts_if_no_match_returns_zero_counts(
        self, pg_manifest: MemoryManifestService
    ) -> None:
        await self._seed_pending_and_dead(pg_manifest)
        # query token-matches NOTHING, hot-path short-circuit on
        summary = await pg_manifest.index_status_summary(
            _user("alice"), ["zzznomatchxyz"], skip_counts_if_no_match=True
        )
        assert summary.matched == []
        # counts SKIPPED (the two COUNT queries never ran) → 0 despite the real
        # pending(1)+dead(1) rows. Neuter proof: remove the short-circuit → 1/1 → RED.
        assert summary.pending == 0
        assert summary.dead_lettered == 0

    async def test_default_computes_counts_even_without_match(
        self, pg_manifest: MemoryManifestService
    ) -> None:
        await self._seed_pending_and_dead(pg_manifest)
        # DEFAULT (skip_counts_if_no_match=False): the general contract is preserved —
        # counts stay honest even when nothing token-matches.
        summary = await pg_manifest.index_status_summary(
            _user("alice"), ["zzznomatchxyz"]
        )
        assert summary.matched == []
        assert summary.pending == 1
        assert summary.dead_lettered == 1

    async def test_matched_case_insensitive_on_key_and_title(
        self, pg_manifest: MemoryManifestService
    ) -> None:
        await pg_manifest.record_create(
            "semantic",
            "decisions/ADR-058-outage.pdf",
            "Outage report",
            1,
            "alice",
            tier="corpus",
        )
        await self._stamp(
            pg_manifest,
            "semantic",
            "decisions/ADR-058-outage.pdf",
            index_failed_at_ms=1,
            index_failure_code="pdf_corrupted_structure",
        )
        summary = await pg_manifest.index_status_summary(_user("alice"), ["adr-058"])
        assert [m.key for m in summary.matched] == ["decisions/ADR-058-outage.pdf"]
        assert summary.matched[0].state == "dead_lettered"
        assert summary.matched[0].index_failure_code == "pdf_corrupted_structure"

    async def test_matched_caps_at_five(
        self, pg_manifest: MemoryManifestService
    ) -> None:
        for i in range(7):
            key = f"decisions/adr-{i}.pdf"
            await pg_manifest.record_create(
                "semantic", key, None, 1, "alice", tier="corpus"
            )
            await self._stamp(pg_manifest, "semantic", key)
        summary = await pg_manifest.index_status_summary(_user("alice"), ["adr"])
        assert len(summary.matched) == 5

    async def test_non_admin_never_sees_another_users_private_row(
        self, pg_manifest: MemoryManifestService
    ) -> None:
        await pg_manifest.record_create(
            "semantic", "decisions/bob-private.pdf", None, 1, "bob", tier="private"
        )
        await self._stamp(
            pg_manifest,
            "semantic",
            "decisions/bob-private.pdf",
            index_failed_at_ms=1,
            index_failure_code="pdf_corrupted_structure",
        )
        summary = await pg_manifest.index_status_summary(_user("alice"), ["private"])
        assert summary.pending == 0
        assert summary.dead_lettered == 0
        assert summary.matched == []

    async def test_admin_bypasses_tenancy_predicate(
        self, pg_manifest: MemoryManifestService
    ) -> None:
        await pg_manifest.record_create(
            "semantic", "decisions/bob-private.pdf", None, 1, "bob", tier="private"
        )
        summary = await pg_manifest.index_status_summary(
            _user("ops", is_admin=True), []
        )
        assert summary.pending == 1

    async def test_soft_deleted_row_excluded(
        self, pg_manifest: MemoryManifestService
    ) -> None:
        await pg_manifest.record_create(
            "semantic", "decisions/gone.md", None, 1, "alice", tier="corpus"
        )
        # SPEC security-memory-manifest-tier-authz (2026-08-30): deleting a
        # corpus-tier row (even one's own) now requires
        # ``caller_can_write_shared=True``.
        await pg_manifest.record_delete(
            "semantic", "decisions/gone.md", "alice", caller_can_write_shared=True
        )
        summary = await pg_manifest.index_status_summary(_user("alice"), [])
        assert summary.pending == 0
        assert summary.dead_lettered == 0


class TestManifestEntryRoundTrip:
    async def test_to_dict_contains_all_fields(
        self, manifest: MockMemoryManifestService
    ) -> None:
        entry = await manifest.record_create("episodic", "k.md", "Title", 100, "user-x")
        d = entry.to_dict()
        for k in (
            "id",
            "layer",
            "key",
            "title",
            "size_bytes",
            "created_at_ms",
            "modified_at_ms",
            "created_by_user_id",
            "modified_by_user_id",
            "deleted_at_ms",
            "deleted_by_user_id",
        ):
            assert k in d, f"missing key: {k}"
        # Ensure it's a flat JSON-friendly dict (no nested objects).
        for v in d.values():
            assert v is None or isinstance(v, (str, int))

    async def test_frozen_dataclass(self, manifest: MockMemoryManifestService) -> None:
        entry = await manifest.record_create("episodic", "k.md", None, 1, "u")
        with pytest.raises(Exception):  # FrozenInstanceError
            entry.layer = "procedural"  # type: ignore[misc]

    async def test_immutable_via_get(self, manifest: MockMemoryManifestService) -> None:
        await manifest.record_create("episodic", "k.md", "t", 1, "u")
        e1 = await manifest.get("episodic", "k.md")
        assert e1 is not None
        # Updating doesn't mutate the previously-returned entry.
        await manifest.record_update("episodic", "k.md", 2, "u")
        assert e1.size_bytes == 1


class TestGet:
    async def test_returns_none_for_missing(
        self, manifest: MockMemoryManifestService
    ) -> None:
        assert await manifest.get("episodic", "never.md") is None

    async def test_returns_entry_for_existing(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create("episodic", "k.md", "t", 1, "u")
        e = await manifest.get("episodic", "k.md")
        assert e is not None and e.key == "k.md"

    async def test_returns_soft_deleted_too(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create("episodic", "k.md", None, 1, "u")
        await manifest.record_delete("episodic", "k.md", "u")
        e = await manifest.get("episodic", "k.md")
        assert e is not None
        assert e.deleted_at_ms is not None


class TestGetDeletedKeys:
    """ADR-062 §6 (WU-A5) — ``get_deleted_keys`` is what
    ``ChromaSemanticService`` consults to honour the manifest soft-delete
    tombstone at recall time (see ``test_semantic_service.py::
    TestSoftDeleteTombstoneFiltersRecall`` for the consumer-side proof)."""

    async def test_empty_keys_short_circuits(
        self, manifest: MockMemoryManifestService
    ) -> None:
        assert await manifest.get_deleted_keys("semantic", []) == set()

    async def test_live_key_not_returned(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create("semantic", "decisions/d1", None, 1, "u")
        assert await manifest.get_deleted_keys("semantic", ["decisions/d1"]) == set()

    async def test_deleted_key_is_returned(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create("semantic", "decisions/d1", None, 1, "u")
        await manifest.record_delete("semantic", "decisions/d1", "u")
        assert await manifest.get_deleted_keys("semantic", ["decisions/d1"]) == {
            "decisions/d1"
        }

    async def test_only_the_requested_keys_are_checked(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create("semantic", "decisions/d1", None, 1, "u")
        await manifest.record_delete("semantic", "decisions/d1", "u")
        await manifest.record_create("semantic", "decisions/d2", None, 1, "u")
        await manifest.record_delete("semantic", "decisions/d2", "u")
        # d2 is soft-deleted too, but wasn't asked about — must not leak in.
        assert await manifest.get_deleted_keys("semantic", ["decisions/d1"]) == {
            "decisions/d1"
        }

    async def test_other_layer_is_isolated(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.record_create("episodic", "decisions/d1", None, 1, "u")
        await manifest.record_delete("episodic", "decisions/d1", "u")
        # Same key string, different layer — must not match.
        assert await manifest.get_deleted_keys("semantic", ["decisions/d1"]) == set()


class TestManifestEntryFromRow:
    def test_from_row_with_real_orm_object(self) -> None:
        """`from_row` accepts duck-typed objects with the right attrs."""
        from types import SimpleNamespace

        row = SimpleNamespace(
            id="abc",
            layer="episodic",
            key="k.md",
            title=None,
            size_bytes=42,
            created_at_ms=1700000000000,
            modified_at_ms=1700000000000,
            created_by_user_id="u",
            modified_by_user_id="u",
            deleted_at_ms=None,
            deleted_by_user_id=None,
            # Tier-B PDF columns (ADR-050 #22) — None for non-PDF rows.
            page_count=None,
            signature_status=None,
            ocr_coverage_pct=None,
            attachment_count=None,
            form_field_count=None,
            extraction_warnings=None,
            document_sha256=None,
            # Tier-C PDF metadata columns (ADR-056 #10) — None for
            # non-PDF rows.
            pdf_title=None,
            pdf_author=None,
            pdf_creator=None,
            pdf_creation_date=None,
            # Tier-C PDF/A + LTV (ADR-056 #14 + #13).
            pdfa_part=None,
            pdfa_conformance=None,
            ltv_data=None,
            # ADR-062 Phase B (WU-B4, migration 018).
            tier="corpus",
        )
        e = ManifestEntry.from_row(row)
        assert e.id == "abc"
        assert e.size_bytes == 42
        assert e.page_count is None
        assert e.attachment_count is None
        assert e.tier == "corpus"


# ── Postgres-backed MemoryManifestService (uses InMemoryPostgresFactory) ─────


@pytest_asyncio.fixture
async def pg_manifest():
    """Real ``MemoryManifestService`` over an async in-memory SQLite DB.
    Schema is created via the factory's async ``create_schema()`` (no Alembic
    needed for unit tests; the production run does run migration 009)."""
    from audittrace.db.postgres import InMemoryPostgresFactory
    from audittrace.services.memory_manifest import MemoryManifestService

    factory = InMemoryPostgresFactory()
    await factory.create_schema()
    return MemoryManifestService(session_factory=factory.get_session_factory())


class TestPostgresMemoryManifestService:
    """End-to-end tests on the real Postgres-backed implementation. Mirrors
    the Mock test suite so the production code path is exercised."""

    async def test_create_then_get(self, pg_manifest) -> None:
        e = await pg_manifest.record_create(
            "episodic", "ADR-001.md", "Title", 100, "user-alice"
        )
        got = await pg_manifest.get("episodic", "ADR-001.md")
        assert got is not None
        assert got.id == e.id
        assert got.layer == "episodic"
        assert got.title == "Title"
        assert got.size_bytes == 100
        assert got.created_by_user_id == "user-alice"
        assert got.deleted_at_ms is None

    async def test_get_returns_none_for_missing(self, pg_manifest) -> None:
        assert await pg_manifest.get("episodic", "never.md") is None

    async def test_recreate_revives_soft_deleted(self, pg_manifest) -> None:
        await pg_manifest.record_create("procedural", "SKILL-x.md", None, 1, "alice")
        await pg_manifest.record_delete("procedural", "SKILL-x.md", "alice")
        # Pre-condition: row is soft-deleted
        deleted = await pg_manifest.get("procedural", "SKILL-x.md")
        assert deleted is not None and deleted.deleted_at_ms is not None
        # Recreate
        revived = await pg_manifest.record_create(
            "procedural", "SKILL-x.md", "new", 2, "bob"
        )
        assert revived.deleted_at_ms is None
        assert revived.deleted_by_user_id is None
        assert revived.title == "new"
        assert revived.modified_by_user_id == "bob"

    async def test_recreate_overwrites_live_row(self, pg_manifest) -> None:
        e1 = await pg_manifest.record_create(
            "semantic", "decisions/d-1", "v1", 10, "alice"
        )
        e2 = await pg_manifest.record_create(
            "semantic", "decisions/d-1", "v2", 20, "bob"
        )
        # Same row id (UNIQUE on (layer, key))
        assert e2.id == e1.id
        assert e2.title == "v2"
        assert e2.size_bytes == 20
        assert e2.modified_by_user_id == "bob"

    async def test_update_bumps_modified_only(self, pg_manifest) -> None:
        e1 = await pg_manifest.record_create("episodic", "k.md", "v1", 10, "alice")
        time.sleep(0.002)  # guarantee different ms
        e2 = await pg_manifest.record_update("episodic", "k.md", 20, "bob", title="v2")
        assert e2.id == e1.id
        assert e2.created_at_ms == e1.created_at_ms
        assert e2.modified_at_ms > e1.modified_at_ms
        assert e2.modified_by_user_id == "bob"
        assert e2.title == "v2"

    async def test_update_title_none_preserves_existing(self, pg_manifest) -> None:
        await pg_manifest.record_create("episodic", "k.md", "stays", 1, "alice")
        e2 = await pg_manifest.record_update("episodic", "k.md", 2, "bob", title=None)
        assert e2.title == "stays"

    async def test_update_missing_raises(self, pg_manifest) -> None:
        with pytest.raises(LookupError):
            await pg_manifest.record_update("episodic", "missing.md", 1, "u")

    async def test_update_soft_deleted_raises(self, pg_manifest) -> None:
        await pg_manifest.record_create("episodic", "k.md", None, 1, "alice")
        await pg_manifest.record_delete("episodic", "k.md", "alice")
        with pytest.raises(RuntimeError, match="soft-deleted"):
            await pg_manifest.record_update("episodic", "k.md", 2, "bob")

    # ── SPEC security-memory-manifest-tier-authz (2026-08-30) ────────────
    # Real Postgres-backed twin of the Mock guard tests above — proves the
    # PRODUCTION code path (not just the in-memory test double) raises.

    async def test_unauthorized_create_over_corpus_row_raises(
        self, pg_manifest
    ) -> None:
        await pg_manifest.record_create(
            "semantic", "decisions/shared", "Shared", 10, "curator", tier="corpus"
        )
        with pytest.raises(ManifestAuthorizationError):
            await pg_manifest.record_create(
                "semantic",
                "decisions/shared",
                "pwned",
                99,
                "attacker",
                tier="private",
            )
        row = await pg_manifest.get("semantic", "decisions/shared")
        assert row is not None
        assert row.tier == "corpus"
        assert row.title == "Shared"
        assert row.created_by_user_id == "curator"
        assert row.modified_by_user_id == "curator"

    async def test_authorized_create_over_corpus_row_succeeds(
        self, pg_manifest
    ) -> None:
        await pg_manifest.record_create(
            "semantic", "decisions/shared-ok", "Shared", 10, "curator", tier="corpus"
        )
        updated = await pg_manifest.record_create(
            "semantic",
            "decisions/shared-ok",
            "Updated",
            20,
            "curator-2",
            tier="corpus",
            caller_can_write_shared=True,
        )
        assert updated.tier == "corpus"
        assert updated.title == "Updated"
        assert updated.modified_by_user_id == "curator-2"
        assert updated.created_by_user_id == "curator"

    async def test_unauthorized_update_over_corpus_row_raises(
        self, pg_manifest
    ) -> None:
        await pg_manifest.record_create(
            "semantic", "decisions/shared-u", "Shared", 10, "curator", tier="corpus"
        )
        with pytest.raises(ManifestAuthorizationError):
            await pg_manifest.record_update(
                "semantic", "decisions/shared-u", 99, "attacker", title="pwned"
            )
        row = await pg_manifest.get("semantic", "decisions/shared-u")
        assert row is not None
        assert row.tier == "corpus"
        assert row.title == "Shared"
        assert row.modified_by_user_id == "curator"
        assert row.size_bytes == 10

    async def test_authorized_update_over_corpus_row_succeeds(
        self, pg_manifest
    ) -> None:
        await pg_manifest.record_create(
            "semantic", "decisions/shared-u2", "Shared", 10, "curator", tier="corpus"
        )
        updated = await pg_manifest.record_update(
            "semantic",
            "decisions/shared-u2",
            20,
            "curator-2",
            title="Revised",
            caller_can_write_shared=True,
        )
        assert updated.title == "Revised"
        assert updated.modified_by_user_id == "curator-2"
        assert updated.created_by_user_id == "curator"

    async def test_unauthorized_create_over_private_row_preserves_tier(
        self, pg_manifest
    ) -> None:
        """Defense-in-depth: an unauthorized create landing on an
        existing PRIVATE row still writes (title/size/modified_by),
        matching pre-fix behavior for the non-corpus case — but any
        requested tier change is ignored rather than silently honored."""
        await pg_manifest.record_create(
            "episodic", "k-priv.md", "v1", 10, "alice", tier="private"
        )
        again = await pg_manifest.record_create(
            "episodic", "k-priv.md", "v2", 20, "bob", tier="corpus"
        )
        assert again.title == "v2"
        assert again.modified_by_user_id == "bob"
        assert again.tier == "private"

    async def test_delete_sets_timestamp(self, pg_manifest) -> None:
        await pg_manifest.record_create("episodic", "k.md", None, 1, "alice")
        d = await pg_manifest.record_delete("episodic", "k.md", "bob")
        assert d.deleted_at_ms is not None
        assert d.deleted_by_user_id == "bob"

    async def test_delete_idempotent(self, pg_manifest) -> None:
        await pg_manifest.record_create("episodic", "k.md", None, 1, "alice")
        d1 = await pg_manifest.record_delete("episodic", "k.md", "bob")
        d2 = await pg_manifest.record_delete("episodic", "k.md", "cleo")
        # Returns existing entry; doesn't update deleter (lossy bob-was-here)
        assert d2.deleted_at_ms == d1.deleted_at_ms
        assert d2.deleted_by_user_id == "bob"

    async def test_delete_missing_raises(self, pg_manifest) -> None:
        with pytest.raises(LookupError):
            await pg_manifest.record_delete("episodic", "missing.md", "u")

    async def test_unauthorized_delete_over_corpus_row_raises(
        self, pg_manifest
    ) -> None:
        """Real Postgres-backed twin of the Mock guard test — proves the
        PRODUCTION code path (not just the in-memory test double) raises."""
        await pg_manifest.record_create(
            "semantic", "decisions/shared-d", "Shared", 10, "curator", tier="corpus"
        )
        with pytest.raises(ManifestAuthorizationError):
            await pg_manifest.record_delete(
                "semantic", "decisions/shared-d", "attacker"
            )
        row = await pg_manifest.get("semantic", "decisions/shared-d")
        assert row is not None
        assert row.tier == "corpus"
        assert row.deleted_at_ms is None
        assert row.deleted_by_user_id is None
        assert row.created_by_user_id == "curator"

    async def test_authorized_delete_over_corpus_row_succeeds(
        self, pg_manifest
    ) -> None:
        await pg_manifest.record_create(
            "semantic", "decisions/shared-d2", "Shared", 10, "curator", tier="corpus"
        )
        d = await pg_manifest.record_delete(
            "semantic", "decisions/shared-d2", "curator-2", caller_can_write_shared=True
        )
        assert d.deleted_at_ms is not None
        assert d.deleted_by_user_id == "curator-2"

    async def test_own_private_item_delete_unaffected_by_guard(
        self, pg_manifest
    ) -> None:
        await pg_manifest.record_create(
            "semantic", "decisions/own-d", "Mine", 10, "alice", tier="private"
        )
        d = await pg_manifest.record_delete("semantic", "decisions/own-d", "alice")
        assert d.deleted_at_ms is not None
        assert d.deleted_by_user_id == "alice"

    async def test_list_excludes_deleted_by_default(self, pg_manifest) -> None:
        await pg_manifest.record_create("episodic", "live.md", None, 1, "u")
        await pg_manifest.record_create("episodic", "deleted.md", None, 1, "u")
        await pg_manifest.record_delete("episodic", "deleted.md", "u")
        rows = await pg_manifest.list_for_layer("episodic")
        assert {r.key for r in rows} == {"live.md"}

    async def test_list_include_deleted(self, pg_manifest) -> None:
        await pg_manifest.record_create("episodic", "live.md", None, 1, "u")
        await pg_manifest.record_create("episodic", "deleted.md", None, 1, "u")
        await pg_manifest.record_delete("episodic", "deleted.md", "u")
        rows = await pg_manifest.list_for_layer("episodic", include_deleted=True)
        assert {r.key for r in rows} == {"live.md", "deleted.md"}

    async def test_list_layer_isolation(self, pg_manifest) -> None:
        await pg_manifest.record_create("episodic", "a.md", None, 1, "u")
        await pg_manifest.record_create("procedural", "b.md", None, 1, "u")
        await pg_manifest.record_create("semantic", "c/d", None, 1, "u")
        assert {r.key for r in await pg_manifest.list_for_layer("episodic")} == {"a.md"}
        assert {r.key for r in await pg_manifest.list_for_layer("procedural")} == {
            "b.md"
        }
        assert {r.key for r in await pg_manifest.list_for_layer("semantic")} == {"c/d"}

    async def test_list_ordered_by_modified_desc(self, pg_manifest) -> None:
        await pg_manifest.record_create("episodic", "first.md", None, 1, "u")
        time.sleep(0.002)
        await pg_manifest.record_create("episodic", "second.md", None, 1, "u")
        time.sleep(0.002)
        await pg_manifest.record_update("episodic", "first.md", 2, "u")
        rows = await pg_manifest.list_for_layer("episodic")
        assert [r.key for r in rows] == ["first.md", "second.md"]

    async def test_invalid_layer_raises(self, pg_manifest) -> None:
        with pytest.raises(ValueError):
            await pg_manifest.record_create("conversational", "x.md", None, 0, "u")
        with pytest.raises(ValueError):
            await pg_manifest.list_for_layer("not-a-layer")
        with pytest.raises(ValueError):
            await pg_manifest.get("not-a-layer", "k")

    async def test_get_deleted_keys_empty_short_circuits(self, pg_manifest) -> None:
        assert await pg_manifest.get_deleted_keys("semantic", []) == set()

    async def test_get_deleted_keys_returns_only_soft_deleted(
        self, pg_manifest
    ) -> None:
        await pg_manifest.record_create("semantic", "decisions/live", None, 1, "u")
        await pg_manifest.record_create("semantic", "decisions/gone", None, 1, "u")
        await pg_manifest.record_delete("semantic", "decisions/gone", "u")
        result = await pg_manifest.get_deleted_keys(
            "semantic", ["decisions/live", "decisions/gone", "decisions/never-existed"]
        )
        assert result == {"decisions/gone"}

    async def test_get_deleted_keys_invalid_layer_raises(self, pg_manifest) -> None:
        with pytest.raises(ValueError):
            await pg_manifest.get_deleted_keys("not-a-layer", ["k"])


# ── Telemetry-coverage regression test ──────────────────────────────────────


class TestTelemetryCoverage:
    """Per `feedback_traceability_requirement` + the user's mandatory
    telemetry directive (2026-05-03 evening): every new feature MUST be
    visible in OpenTelemetry traces. The chart's ``@log_call`` decorator
    is the project's standard way to emit a Tempo+Langfuse span around
    a service method. This test is a regression guard so a future
    refactor doesn't silently strip the decorator."""

    def test_manifest_service_methods_carry_log_call(self) -> None:
        from audittrace.services.memory_manifest import MemoryManifestService

        for method_name in (
            "record_create",
            "record_update",
            "record_delete",
            "list_for_layer",
            "get",
            "index_status_summary",
        ):
            method = getattr(MemoryManifestService, method_name)
            # The @log_call decorator wraps with a function that has
            # __wrapped__ pointing at the original. A naked method
            # would not have that attribute.
            assert hasattr(method, "__wrapped__"), (
                f"{method_name} is not @log_call-decorated — telemetry "
                f"coverage gap (per feedback_traceability_requirement). "
                f"Re-add the decorator so spans land in Tempo + Langfuse."
            )

    def test_episodic_write_methods_carry_log_call(self) -> None:
        from audittrace.services.episodic import (
            MockEpisodicService,
            S3EpisodicService,
        )

        for cls in (S3EpisodicService, MockEpisodicService):
            for method_name in ("write", "delete", "invalidate_cache"):
                method = getattr(cls, method_name)
                assert hasattr(method, "__wrapped__"), (
                    f"{cls.__name__}.{method_name} is not @log_call-decorated"
                )

    def test_procedural_write_methods_carry_log_call(self) -> None:
        from audittrace.services.procedural import (
            MockProceduralService,
            S3ProceduralService,
        )

        for cls in (S3ProceduralService, MockProceduralService):
            for method_name in ("write", "delete", "invalidate_cache"):
                method = getattr(cls, method_name)
                assert hasattr(method, "__wrapped__"), (
                    f"{cls.__name__}.{method_name} is not @log_call-decorated"
                )

    def test_semantic_crud_methods_carry_log_call(self) -> None:
        from audittrace.services.semantic import (
            ChromaSemanticService,
            MockSemanticService,
            UserScopedSemanticService,
        )

        for cls in (
            ChromaSemanticService,
            MockSemanticService,
            UserScopedSemanticService,
        ):
            for method_name in ("upsert", "delete_document", "get_document"):
                method = getattr(cls, method_name)
                assert hasattr(method, "__wrapped__"), (
                    f"{cls.__name__}.{method_name} is not @log_call-decorated"
                )


# ── Tier-B (ADR-050 #22): upsert_pdf_metadata coverage ──────────────────────


class TestMockUpsertPdfMetadata:
    """Tier-B item #22 — ``MockMemoryManifestService.upsert_pdf_metadata``
    creates rows when none exist + updates fields when one does, on
    both code paths."""

    async def test_first_call_creates_row_with_pdf_columns(
        self, manifest: MockMemoryManifestService
    ) -> None:
        entry = await manifest.upsert_pdf_metadata(
            "episodic",
            "main.pdf",
            user_id="user-1",
            size_bytes=12345,
            page_count=46,
            signature_status="signed_invalid",
            ocr_coverage_pct=12.5,
            attachment_count=2,
            form_field_count=0,
            extraction_warnings=[
                {"code": "no_text_layer", "page": 5},
                {"code": "ocr_low_confidence", "page": 7, "confidence": 0.42},
            ],
            document_sha256="a" * 64,
        )
        assert entry.layer == "episodic"
        assert entry.key == "main.pdf"
        assert entry.page_count == 46
        assert entry.signature_status == "signed_invalid"
        assert entry.ocr_coverage_pct == 12.5
        assert entry.attachment_count == 2
        assert entry.form_field_count == 0
        assert entry.document_sha256 == "a" * 64
        assert entry.extraction_warnings is not None
        assert len(entry.extraction_warnings) == 2
        assert entry.extraction_warnings[0]["code"] == "no_text_layer"

    async def test_subsequent_call_updates_fields_keeps_authorship(
        self, manifest: MockMemoryManifestService
    ) -> None:
        # First call as user-1
        await manifest.upsert_pdf_metadata(
            "episodic",
            "main.pdf",
            user_id="user-1",
            size_bytes=100,
            page_count=10,
            signature_status="signed_valid",
            ocr_coverage_pct=0.0,
            attachment_count=0,
            form_field_count=0,
            extraction_warnings=[],
            document_sha256="b" * 64,
        )
        # Second call as user-2 (AUTHORIZED — e.g. admin/corpus-scope
        # re-index) — bumps modified_*, keeps created_*. SPEC security-
        # memory-manifest-tier-authz (2026-08-30): every PDF row defaults
        # tier="corpus", so a cross-owner call now requires
        # ``caller_can_write_shared=True``; ownership stays immutable even
        # for an authorized overwrite.
        e2 = await manifest.upsert_pdf_metadata(
            "episodic",
            "main.pdf",
            user_id="user-2",
            size_bytes=200,
            page_count=20,
            signature_status="signed_valid",
            ocr_coverage_pct=50.0,
            attachment_count=1,
            form_field_count=3,
            extraction_warnings=[{"code": "attachment", "name": "x.xml"}],
            document_sha256="c" * 64,
            caller_can_write_shared=True,
        )
        assert e2.created_by_user_id == "user-1"  # preserved
        assert e2.modified_by_user_id == "user-2"  # bumped
        assert e2.page_count == 20  # updated
        assert e2.attachment_count == 1
        assert e2.form_field_count == 3
        assert e2.size_bytes == 200

    # ── SPEC security-memory-manifest-tier-authz (2026-08-30) ────────────
    # The sibling gap the M3-WU-D2-2 reviewer live-demonstrated:
    # ``upsert_pdf_metadata`` is the manifest-write choke for the PDF
    # ingestion pipeline, missed by the initial ``record_create``/
    # ``record_update`` fix. FALSIFIABLE: neuter
    # ``_pdf_metadata_write_unauthorized`` (e.g. make it always return
    # ``False``) and ``test_cross_owner_unauthorized_update_raises_and_row_unchanged``
    # goes RED (a different, unauthorized user's re-index silently
    # succeeds and reassigns ``modified_by_user_id``/overwrites
    # ``pdf_title``/``signature_status``/``document_sha256``); restore it
    # and it goes GREEN.

    async def test_cross_owner_unauthorized_update_raises_and_row_unchanged(
        self, manifest: MockMemoryManifestService
    ) -> None:
        await manifest.upsert_pdf_metadata(
            "episodic",
            "curator-shared.pdf",
            user_id="curator",
            size_bytes=100,
            page_count=10,
            signature_status="signed_valid",
            ocr_coverage_pct=0.0,
            attachment_count=0,
            form_field_count=0,
            extraction_warnings=[],
            document_sha256="b" * 64,
            pdf_title="Original Title",
        )
        with pytest.raises(ManifestAuthorizationError):
            await manifest.upsert_pdf_metadata(
                "episodic",
                "curator-shared.pdf",
                user_id="attacker",
                size_bytes=999,
                page_count=1,
                signature_status="signed_tampered",
                ocr_coverage_pct=99.0,
                attachment_count=9,
                form_field_count=9,
                extraction_warnings=[{"code": "pwned"}],
                document_sha256="f" * 64,
                pdf_title="pwned",
            )
        # No overwrite at all — tier, owner, and every PDF field intact.
        row = await manifest.get("episodic", "curator-shared.pdf")
        assert row is not None
        assert row.tier == "corpus"
        assert row.created_by_user_id == "curator"
        assert row.modified_by_user_id == "curator"
        assert row.pdf_title == "Original Title"
        assert row.signature_status == "signed_valid"
        assert row.document_sha256 == "b" * 64
        assert row.size_bytes == 100

    async def test_same_owner_reindex_succeeds_without_shared_write_scope(
        self, manifest: MockMemoryManifestService
    ) -> None:
        """A legitimate metadata re-index by the row's OWN creator must
        keep working even though every PDF row defaults tier="corpus" —
        this is the routine, documented idempotent ``/memory/index``
        re-run, not a cross-user overwrite."""
        await manifest.upsert_pdf_metadata(
            "episodic",
            "own.pdf",
            user_id="alice",
            size_bytes=100,
            page_count=10,
            signature_status="signed_valid",
            ocr_coverage_pct=0.0,
            attachment_count=0,
            form_field_count=0,
            extraction_warnings=[],
            document_sha256="a" * 64,
        )
        e2 = await manifest.upsert_pdf_metadata(
            "episodic",
            "own.pdf",
            user_id="alice",
            size_bytes=150,
            page_count=12,
            signature_status="signed_valid",
            ocr_coverage_pct=5.0,
            attachment_count=1,
            form_field_count=0,
            extraction_warnings=[],
            document_sha256="a" * 64,
        )
        assert e2.created_by_user_id == "alice"
        assert e2.modified_by_user_id == "alice"
        assert e2.page_count == 12
        assert e2.size_bytes == 150

    async def test_rejects_invalid_layer(
        self, manifest: MockMemoryManifestService
    ) -> None:
        with pytest.raises(ValueError):
            await manifest.upsert_pdf_metadata(
                "bogus-layer",
                "x.pdf",
                user_id="u",
                size_bytes=1,
                page_count=1,
                signature_status="check_skipped",
                ocr_coverage_pct=None,
                attachment_count=0,
                form_field_count=0,
                extraction_warnings=[],
                document_sha256=None,
            )

    async def test_warnings_round_trip_through_to_dict(
        self, manifest: MockMemoryManifestService
    ) -> None:
        warnings = [
            {"code": "encrypted", "page": None},
            {
                "code": "attachment",
                "name": "invoice.xml",
                "mime": "application/xml",
                "size": 1024,
                "sha256": "d" * 64,
                "minio_key": "episodic/main.pdf/attachments/invoice.xml",
            },
        ]
        entry = await manifest.upsert_pdf_metadata(
            "episodic",
            "main.pdf",
            user_id="u",
            size_bytes=1,
            page_count=1,
            signature_status="check_skipped",
            ocr_coverage_pct=None,
            attachment_count=1,
            form_field_count=0,
            extraction_warnings=warnings,
            document_sha256="e" * 64,
        )
        d = entry.to_dict()
        assert d["extraction_warnings"] == warnings
        assert d["page_count"] == 1
        assert d["attachment_count"] == 1


class TestPostgresUpsertPdfMetadata:
    """End-to-end tier-B #22 against the real Postgres-backed service.
    Mirrors the Mock suite so the production code path is exercised."""

    async def test_create_writes_pdf_columns(self, pg_manifest) -> None:
        entry = await pg_manifest.upsert_pdf_metadata(
            "episodic",
            "main.pdf",
            user_id="alice",
            size_bytes=12345,
            page_count=46,
            signature_status="signed_invalid",
            ocr_coverage_pct=12.5,
            attachment_count=2,
            form_field_count=0,
            extraction_warnings=[
                {"code": "no_text_layer", "page": 5},
            ],
            document_sha256="a" * 64,
        )
        assert entry.page_count == 46
        # Round-trip: fetch via get() and verify the same shape.
        got = await pg_manifest.get("episodic", "main.pdf")
        assert got is not None
        assert got.signature_status == "signed_invalid"
        assert got.attachment_count == 2
        assert got.extraction_warnings == [{"code": "no_text_layer", "page": 5}]

    async def test_update_preserves_created_by(self, pg_manifest) -> None:
        await pg_manifest.upsert_pdf_metadata(
            "episodic",
            "main.pdf",
            user_id="alice",
            size_bytes=100,
            page_count=10,
            signature_status="signed_valid",
            ocr_coverage_pct=0.0,
            attachment_count=0,
            form_field_count=0,
            extraction_warnings=[],
            document_sha256="b" * 64,
        )
        # SPEC security-memory-manifest-tier-authz (2026-08-30): every PDF
        # row defaults tier="corpus", so bob (not the owner) now needs
        # ``caller_can_write_shared=True`` (e.g. admin) to overwrite it.
        e2 = await pg_manifest.upsert_pdf_metadata(
            "episodic",
            "main.pdf",
            user_id="bob",
            size_bytes=200,
            page_count=20,
            signature_status="signed_valid",
            ocr_coverage_pct=50.0,
            attachment_count=1,
            form_field_count=2,
            extraction_warnings=[],
            document_sha256="c" * 64,
            caller_can_write_shared=True,
        )
        assert e2.created_by_user_id == "alice"
        assert e2.modified_by_user_id == "bob"
        assert e2.page_count == 20

    async def test_cross_owner_unauthorized_update_raises_and_row_unchanged(
        self, pg_manifest
    ) -> None:
        """Real Postgres-backed twin of the Mock guard test — proves the
        PRODUCTION code path (not just the in-memory test double) raises."""
        await pg_manifest.upsert_pdf_metadata(
            "episodic",
            "curator-shared.pdf",
            user_id="curator",
            size_bytes=100,
            page_count=10,
            signature_status="signed_valid",
            ocr_coverage_pct=0.0,
            attachment_count=0,
            form_field_count=0,
            extraction_warnings=[],
            document_sha256="b" * 64,
            pdf_title="Original Title",
        )
        with pytest.raises(ManifestAuthorizationError):
            await pg_manifest.upsert_pdf_metadata(
                "episodic",
                "curator-shared.pdf",
                user_id="attacker",
                size_bytes=999,
                page_count=1,
                signature_status="signed_tampered",
                ocr_coverage_pct=99.0,
                attachment_count=9,
                form_field_count=9,
                extraction_warnings=[{"code": "pwned"}],
                document_sha256="f" * 64,
                pdf_title="pwned",
            )
        row = await pg_manifest.get("episodic", "curator-shared.pdf")
        assert row is not None
        assert row.tier == "corpus"
        assert row.created_by_user_id == "curator"
        assert row.modified_by_user_id == "curator"
        assert row.pdf_title == "Original Title"
        assert row.signature_status == "signed_valid"
        assert row.document_sha256 == "b" * 64
        assert row.size_bytes == 100

    async def test_same_owner_reindex_succeeds_without_shared_write_scope(
        self, pg_manifest
    ) -> None:
        await pg_manifest.upsert_pdf_metadata(
            "episodic",
            "own.pdf",
            user_id="alice",
            size_bytes=100,
            page_count=10,
            signature_status="signed_valid",
            ocr_coverage_pct=0.0,
            attachment_count=0,
            form_field_count=0,
            extraction_warnings=[],
            document_sha256="a" * 64,
        )
        e2 = await pg_manifest.upsert_pdf_metadata(
            "episodic",
            "own.pdf",
            user_id="alice",
            size_bytes=150,
            page_count=12,
            signature_status="signed_valid",
            ocr_coverage_pct=5.0,
            attachment_count=1,
            form_field_count=0,
            extraction_warnings=[],
            document_sha256="a" * 64,
        )
        assert e2.created_by_user_id == "alice"
        assert e2.modified_by_user_id == "alice"
        assert e2.page_count == 12
        assert e2.size_bytes == 150

    async def test_update_after_crud_create_carries_over(self, pg_manifest) -> None:
        """Common flow: operator first POSTs to /memory/episodic
        (record_create), THEN runs /memory/index which writes PDF
        metadata. The second call must update — not duplicate — the
        same row."""
        await pg_manifest.record_create(
            "episodic", "main.pdf", "Main paper", 100, "alice"
        )
        e = await pg_manifest.upsert_pdf_metadata(
            "episodic",
            "main.pdf",
            user_id="indexer",
            size_bytes=200,
            page_count=46,
            signature_status="signed_invalid",
            ocr_coverage_pct=0.0,
            attachment_count=0,
            form_field_count=0,
            extraction_warnings=[],
            document_sha256="d" * 64,
        )
        # Same row — created_by stays as the original creator.
        assert e.created_by_user_id == "alice"
        assert e.title == "Main paper"  # preserved by upsert path
        assert e.page_count == 46  # populated by upsert
