"""Tests for EpisodicService — Layer 1 of the 4-layer memory architecture (ADR-018).

The service is **always S3-backed** in production (MinIO) — there is no
filesystem implementation. Tests here exercise ``S3EpisodicService`` against a
fake, bucket-aware MinIO client and ``MockEpisodicService`` directly. See
``feedback_storage_always_s3``.

Phase 2 (DESIGN §15): every service method takes ``user_context`` as the first
positional argument.

ADR-062 Phase B (WU-B2): the layer is dual-tier — a caller's own writes land
in the PRIVATE tier (``private_bucket/{user_id}/episodic/``); the pre-existing
``memory-shared`` content is the CORPUS tier (shared-read). ``TestDualTier*``
classes below are the isolation-safety-bar falsifiable tests: a second user
must never see another user's private writes, and every caller must still see
the corpus.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from audittrace_object_storage import ObjectNotFoundError

from audittrace.services.episodic import (
    EpisodicService,
    MockEpisodicService,
    S3EpisodicService,
    UserScopedEpisodicService,
)

# ── Fake, bucket-aware MinIO client ──────────────────────────────────────────
#
# ADR-006 migration note: the four services now catch
# :class:`ObjectNotFoundError` (from the shared package), NOT the
# old minio-shaped ``S3Error`` with ``.code == "NoSuchKey"``. The
# fakes below raise ``ObjectNotFoundError`` to match the post-ADR-006
# contract.
#
# ADR-062 Phase B: real MinIO partitions objects by BUCKET — a key in
# ``memory-private`` is invisible to a client reading ``memory-shared``, even
# if the key string is identical. The old fake ignored ``bucket`` entirely
# (single global dict); that can no longer stand in for the private/corpus
# boundary the dual-tier logic depends on, so the fake is now a real
# ``{bucket: {key: bytes}}`` two-level store.


class _FakeObject:
    def __init__(self, object_name: str) -> None:
        self.object_name = object_name


class _FakeResponse:
    """MinIO ``get_object`` response double.

    Implements the context-manager protocol because the production
    code uses ``with client.get_object(...) as response:`` for
    deterministic cleanup (PYTHON-ENGINEERING skill §1).
    """

    def __init__(self, content: bytes) -> None:
        self._content = content
        self.closed = False
        self.released = False

    def read(self) -> bytes:
        return self._content

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
        self.release_conn()


class _FakeMinio:
    """Bucket-partitioned MinIO-client double covering ``list_objects`` +
    ``get_object`` + ``put_object`` + ``remove_object`` + ``stat_object``.

    ``buckets`` is ``{bucket_name: {key: content_bytes}}``. A key written to
    one bucket is invisible from another — the same isolation real MinIO
    gives two distinct buckets.
    """

    def __init__(self, buckets: dict[str, dict[str, bytes]] | None = None) -> None:
        self._buckets: dict[str, dict[str, bytes]] = {
            b: dict(objs) for b, objs in (buckets or {}).items()
        }

    def _objects(self, bucket: str) -> dict[str, bytes]:
        return self._buckets.setdefault(bucket, {})

    def list_objects(
        self, bucket: str, prefix: str = "", **kwargs: Any
    ) -> list[_FakeObject]:
        del kwargs
        return [_FakeObject(k) for k in self._objects(bucket) if k.startswith(prefix)]

    def get_object(self, bucket: str, key: str) -> _FakeResponse:
        objs = self._objects(bucket)
        if key not in objs:
            raise ObjectNotFoundError(f"Object does not exist: {bucket}/{key}")
        return _FakeResponse(objs[key])

    def put_object(self, bucket: str, key: str, body: Any, length: int) -> None:
        del length
        self._objects(bucket)[key] = body.read()

    def stat_object(self, bucket: str, key: str) -> object:
        if key not in self._objects(bucket):
            raise ObjectNotFoundError(f"Object does not exist: {bucket}/{key}")
        return object()  # opaque "exists" sentinel

    def remove_object(self, bucket: str, key: str) -> None:
        self._objects(bucket).pop(key, None)


# ── Fixtures ─────────────────────────────────────────────────────────────────

_SHARED = "memory-shared"
_PRIVATE = "memory-private"


@pytest.fixture
def fake_bucket_objects() -> dict[str, bytes]:
    """Three sample ADR-*.md objects under the ``episodic/`` prefix (corpus)."""
    return {
        "episodic/ADR-001-use-rocm.md": (
            b"# ADR-001: Use ROCm for GPU Acceleration\n\n"
            b"Date: 2026-03-01\n\n## Status\n\nAccepted\n\n"
            b"## Context\n\nThe workstation uses AMD GPU requiring ROCm.\n"
        ),
        "episodic/ADR-009-kv-cache-compression.md": (
            b"# ADR-009: KV Cache Compression\n\n"
            b"## Decision\n\nUse q4_0 cache compression to reduce to 4 GB.\n"
        ),
        "episodic/ADR-016-bandwidth-optimisation.md": (
            b"# ADR-016: Memory Bus Bandwidth Optimisation\n\n"
            b"## Decision\n\nReduce context to 65k.\n"
        ),
    }


@pytest.fixture
def s3_episodic(fake_bucket_objects: dict[str, bytes]) -> S3EpisodicService:
    return S3EpisodicService(
        minio_client=_FakeMinio({_SHARED: fake_bucket_objects}),
        bucket=_SHARED,
        private_bucket=_PRIVATE,
        prefix="episodic/",
    )


def _service(
    corpus: dict[str, bytes] | None = None,
    private: dict[str, bytes] | None = None,
) -> S3EpisodicService:
    client = _FakeMinio({_SHARED: corpus or {}, _PRIVATE: private or {}})
    return S3EpisodicService(
        client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="episodic/"
    )


# ── S3EpisodicService tests (corpus-only, pre-existing coverage) ────────────


class TestS3EpisodicService:
    async def test_load_returns_all_adrs(
        self, s3_episodic: S3EpisodicService, user_context
    ):
        docs = await s3_episodic.load(user_context)
        assert len(docs) == 3

    async def test_load_extracts_title_from_heading(
        self, s3_episodic: S3EpisodicService, user_context
    ):
        docs = await s3_episodic.load(user_context)
        titles = [d.metadata["title"] for d in docs]
        assert "ADR-001: Use ROCm for GPU Acceleration" in titles
        assert "ADR-009: KV Cache Compression" in titles

    async def test_load_sets_metadata(
        self, s3_episodic: S3EpisodicService, user_context
    ):
        docs = await s3_episodic.load(user_context)
        for d in docs:
            assert d.metadata["source"] == "episodic"
            assert d.metadata["file"].startswith("ADR-")
            assert d.metadata["file"].endswith(".md")
            assert d.metadata["tier"] == "corpus"

    async def test_load_includes_all_md_objects(self, user_context):
        """Backlog #15 (R1): every ``.md`` object is enumerated, not just
        ADR-*.md. A non-ADR ``decision-*.md`` / ``README.md`` uploaded via
        /memory/upload MUST surface — that was the blind spot."""
        service = _service(
            corpus={
                "episodic/README.md": b"# Just a readme\n",
                "episodic/decision-2026-07-24-x.md": b"# Decision\n\nbody\n",
                "episodic/ADR-001-x.md": b"# ADR-001\n\nbody\n",
            }
        )
        docs = await service.load(user_context)
        files = {d.metadata["file"] for d in docs}
        assert files == {"README.md", "decision-2026-07-24-x.md", "ADR-001-x.md"}

    async def test_load_still_skips_non_md_objects(self, user_context):
        """Non-``.md`` objects (e.g. papers/*.pdf) are NOT loaded as docs —
        the unified rule is '.md under the prefix'."""
        service = _service(
            corpus={
                "episodic/ADR-001-x.md": b"# ADR-001\n\nbody\n",
                "episodic/papers/foo.pdf": b"%PDF-1.7\n",
            }
        )
        files = {d.metadata["file"] for d in await service.load(user_context)}
        assert files == {"ADR-001-x.md"}

    async def test_load_handles_empty_bucket(self, user_context):
        assert await _service().load(user_context) == []

    async def test_load_handles_client_exception(self, user_context):
        """An unexpected client error logs + returns []. No exception bubbles."""

        class _Broken:
            def list_objects(self, *a: Any, **kw: Any) -> list[_FakeObject]:
                raise RuntimeError("connection refused")

        service = S3EpisodicService(
            _Broken(), bucket=_SHARED, private_bucket=_PRIVATE, prefix="episodic/"
        )
        assert await service.load(user_context) == []

    async def test_search_filters_by_query(
        self, s3_episodic: S3EpisodicService, user_context
    ):
        results = await s3_episodic.search(user_context, "cache compression")
        assert len(results) >= 1
        titles = [d.metadata["title"] for d in results]
        assert any("Cache" in t for t in titles)

    async def test_search_no_match_returns_empty(
        self, s3_episodic: S3EpisodicService, user_context
    ):
        assert await s3_episodic.search(user_context, "quantum entanglement") == []

    async def test_search_no_arbitrary_cap(self, user_context):
        """If 5 ADRs match, all 5 should be returned — no cap."""
        objs = {
            f"episodic/ADR-{i:03d}-server-config-{i}.md": (
                f"# ADR-{i:03d}: Server Config Part {i}\n\n"
                f"## Decision\n\nApply server setting {i}.\n"
            ).encode()
            for i in range(1, 6)
        }
        service = _service(corpus=objs)
        results = await service.search(user_context, "server configuration")
        assert len(results) == 5

    async def test_search_short_query_returns_empty(
        self, s3_episodic: S3EpisodicService, user_context
    ):
        """Short keywords (≤3 chars) yield nothing — avoids spam matches."""
        assert await s3_episodic.search(user_context, "hi a") == []

    async def test_as_context_returns_formatted_string(
        self, s3_episodic: S3EpisodicService, user_context
    ):
        ctx = await s3_episodic.as_context(user_context, "cache")
        assert "Architecture Decisions" in ctx
        assert "KV Cache" in ctx

    async def test_as_context_empty_when_no_match(
        self, s3_episodic: S3EpisodicService, user_context
    ):
        assert await s3_episodic.as_context(user_context, "quantum entanglement") == ""

    async def test_load_handles_adr_with_no_h1_header(self, user_context):
        """An ADR file without a `# ` H1 line still loads — title is the stem."""
        service = _service(
            corpus={"episodic/ADR-100-no-header.md": b"Just body text, no H1 line.\n"}
        )
        docs = await service.load(user_context)
        assert len(docs) == 1
        assert docs[0].metadata["title"] == "ADR-100-no-header"


class TestS3EpisodicServiceRead:
    """``read(file)`` — full-content fetch by exact filename (Phase A.1)."""

    async def test_read_existing_file_returns_full_content(
        self,
        s3_episodic: S3EpisodicService,
        fake_bucket_objects: dict[str, bytes],
        user_context,
    ):
        doc = await s3_episodic.read(user_context, "ADR-009-kv-cache-compression.md")
        assert doc is not None
        expected = fake_bucket_objects[
            "episodic/ADR-009-kv-cache-compression.md"
        ].decode("utf-8")
        assert doc.page_content == expected
        assert doc.metadata["file"] == "ADR-009-kv-cache-compression.md"
        assert doc.metadata["title"] == "ADR-009: KV Cache Compression"
        assert doc.metadata["source"] == "episodic"
        assert doc.metadata["tier"] == "corpus"

    async def test_read_missing_file_returns_none(
        self, s3_episodic: S3EpisodicService, user_context
    ):
        assert await s3_episodic.read(user_context, "ADR-999-nope.md") is None

    async def test_read_rejects_path_traversal(
        self, s3_episodic: S3EpisodicService, user_context
    ):
        for bad in [
            "../etc/passwd.md",
            "ADR-001/../../secret.md",
            "subdir/ADR-001.md",
            "..\\windows.md",
        ]:
            assert await s3_episodic.read(user_context, bad) is None

    async def test_read_rejects_non_md(
        self, s3_episodic: S3EpisodicService, user_context
    ):
        assert await s3_episodic.read(user_context, "ADR-001") is None
        assert await s3_episodic.read(user_context, "ADR-001.txt") is None

    async def test_read_rejects_empty_or_non_string(
        self, s3_episodic: S3EpisodicService, user_context
    ):
        assert await s3_episodic.read(user_context, "") is None
        assert await s3_episodic.read(user_context, None) is None  # type: ignore[arg-type]

    async def test_read_handles_unexpected_exception(self, user_context):
        """Non-NoSuchKey errors log + return None — caller never sees a raise."""

        class _Broken:
            def get_object(self, *a: Any, **kw: Any) -> _FakeResponse:
                raise RuntimeError("connection reset")

        service = S3EpisodicService(
            _Broken(), bucket=_SHARED, private_bucket=_PRIVATE, prefix="episodic/"
        )
        assert await service.read(user_context, "ADR-001.md") is None

    async def test_read_returns_full_untruncated_content(self, user_context):
        """Regression for the ADR-025 bug: full content, no 400-char limit."""
        big = ("# ADR-025\n\n" + ("body line.\n" * 5000)).encode()
        service = _service(corpus={"episodic/ADR-025.md": big})
        doc = await service.read(user_context, "ADR-025.md")
        assert doc is not None
        assert len(doc.page_content) == len(big.decode())
        assert len(doc.page_content) > 5000  # well over the old 400-char cap


# ── ADR-062 Phase B — dual-tier isolation-safety-bar tests ──────────────────


def _user(user_id: str) -> Any:
    """Build a distinct UserContext from the shared admin-sentinel fixture
    value at call sites that need two *different*, non-admin identities."""
    from audittrace.identity import UserContext

    return UserContext(
        user_id=user_id,
        username=user_id,
        agent_type="opencode",
        scopes=("memory:episodic:read", "memory:episodic:write"),
        is_admin=False,
    )


class TestDualTierWrite:
    async def test_write_lands_in_private_bucket_only(self, user_context) -> None:
        """WU-B2 falsifiable: write() must NOT touch the corpus bucket."""
        client = _FakeMinio({_SHARED: {}, _PRIVATE: {}})
        alice = _user("user-alice")
        service = S3EpisodicService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="episodic/"
        )
        await service.write(alice, "note.md", "# note\n")
        assert "user-alice/episodic/note.md" in client._buckets[_PRIVATE]
        assert client._buckets.get(_SHARED, {}) == {}

    async def test_write_stamps_tier_private(self, user_context) -> None:
        service = _service()
        doc = await service.write(user_context, "note.md", "# note\n")
        assert doc.metadata["tier"] == "private"

    async def test_write_never_shadows_another_users_key(self, user_context) -> None:
        """Two users writing the SAME filename land at DIFFERENT S3 keys."""
        client = _FakeMinio({_SHARED: {}, _PRIVATE: {}})
        service = S3EpisodicService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="episodic/"
        )
        alice, bob = _user("user-alice"), _user("user-bob")
        await service.write(alice, "note.md", "# alice's note\n")
        await service.write(bob, "note.md", "# bob's note\n")
        private = client._buckets[_PRIVATE]
        assert private["user-alice/episodic/note.md"] == b"# alice's note\n"
        assert private["user-bob/episodic/note.md"] == b"# bob's note\n"


class TestDualTierListAndSearchIsolation:
    """The ADR-062 §3 isolation-safety-bar, falsified end to end: user A
    writes private content; user B's ``load``/``search`` must NOT return it,
    but BOTH must still see the corpus, tagged ``tier=corpus``."""

    async def test_private_write_not_visible_to_other_user_via_load(
        self, user_context
    ) -> None:
        client = _FakeMinio(
            {_SHARED: {"episodic/ADR-seed.md": b"# Seed ADR\n\nCorpus content.\n"}}
        )
        service = S3EpisodicService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="episodic/"
        )
        alice, bob = _user("user-alice"), _user("user-bob")

        await service.write(alice, "alice-secret.md", "# Alice's private note\n")

        alice_files = {d.metadata["file"] for d in await service.load(alice)}
        bob_files = {d.metadata["file"] for d in await service.load(bob)}

        assert "alice-secret.md" in alice_files
        assert "alice-secret.md" not in bob_files  # THE isolation-safety-bar
        # Both still see the corpus.
        assert "ADR-seed.md" in alice_files
        assert "ADR-seed.md" in bob_files

    async def test_corpus_items_are_tagged_tier_corpus_for_every_caller(
        self, user_context
    ) -> None:
        client = _FakeMinio(
            {_SHARED: {"episodic/ADR-seed.md": b"# Seed ADR\n\nCorpus content.\n"}}
        )
        service = S3EpisodicService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="episodic/"
        )
        bob = _user("user-bob")
        docs = await service.load(bob)
        seed = next(d for d in docs if d.metadata["file"] == "ADR-seed.md")
        assert seed.metadata["tier"] == "corpus"

    async def test_private_write_tagged_tier_private_for_owner(
        self, user_context
    ) -> None:
        client = _FakeMinio({_SHARED: {}})
        service = S3EpisodicService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="episodic/"
        )
        alice = _user("user-alice")
        await service.write(alice, "alice-secret.md", "# secret\n")
        docs = await service.load(alice)
        mine = next(d for d in docs if d.metadata["file"] == "alice-secret.md")
        assert mine.metadata["tier"] == "private"

    async def test_private_write_not_found_by_other_user_via_search(
        self, user_context
    ) -> None:
        client = _FakeMinio({_SHARED: {}})
        service = S3EpisodicService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="episodic/"
        )
        alice, bob = _user("user-alice"), _user("user-bob")
        await service.write(
            alice, "alice-cache-notes.md", "# Cache tuning notes\n\ncache cache\n"
        )
        alice_hits = await service.search(alice, "cache tuning")
        bob_hits = await service.search(bob, "cache tuning")
        assert any(d.metadata["file"] == "alice-cache-notes.md" for d in alice_hits)
        assert not any(d.metadata["file"] == "alice-cache-notes.md" for d in bob_hits)

    async def test_private_shadows_same_named_corpus_object(self, user_context) -> None:
        """A private object with the SAME filename as a corpus object wins
        the merge (ADR-062 §3: "private shadows corpus")."""
        client = _FakeMinio(
            {_SHARED: {"episodic/ADR-1.md": b"# ADR-1\n\ncorpus version\n"}}
        )
        service = S3EpisodicService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="episodic/"
        )
        alice = _user("user-alice")
        await service.write(alice, "ADR-1.md", "# ADR-1\n\nalice's private version\n")
        docs = await service.load(alice)
        matches = [d for d in docs if d.metadata["file"] == "ADR-1.md"]
        assert len(matches) == 1  # deduped, not duplicated
        assert matches[0].metadata["tier"] == "private"
        assert "alice's private version" in matches[0].page_content


class TestDualTierRead:
    async def test_read_tries_private_first(self, user_context) -> None:
        client = _FakeMinio(
            {
                _SHARED: {"episodic/ADR-1.md": b"# ADR-1\n\ncorpus\n"},
                _PRIVATE: {
                    "user-alice/episodic/ADR-1.md": b"# ADR-1\n\nprivate override\n"
                },
            }
        )
        service = S3EpisodicService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="episodic/"
        )
        doc = await service.read(_user("user-alice"), "ADR-1.md")
        assert doc is not None
        assert "private override" in doc.page_content
        assert doc.metadata["tier"] == "private"

    async def test_read_falls_back_to_corpus_when_no_private_object(
        self, user_context
    ) -> None:
        client = _FakeMinio(
            {_SHARED: {"episodic/ADR-1.md": b"# ADR-1\n\ncorpus\n"}, _PRIVATE: {}}
        )
        service = S3EpisodicService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="episodic/"
        )
        doc = await service.read(_user("user-bob"), "ADR-1.md")
        assert doc is not None
        assert doc.metadata["tier"] == "corpus"

    async def test_read_by_filename_never_leaks_another_users_private_object(
        self, user_context
    ) -> None:
        client = _FakeMinio({_SHARED: {}, _PRIVATE: {}})
        service = S3EpisodicService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="episodic/"
        )
        alice, bob = _user("user-alice"), _user("user-bob")
        await service.write(alice, "alice-secret.md", "# secret\n")
        # Bob asks for the exact filename by name — must 404 (None), never leak.
        assert await service.read(bob, "alice-secret.md") is None
        assert await service.read(alice, "alice-secret.md") is not None


class TestDualTierDelete:
    async def test_delete_only_removes_private_object_never_corpus(
        self, user_context
    ) -> None:
        client = _FakeMinio(
            {
                _SHARED: {"episodic/ADR-1.md": b"# ADR-1\n\ncorpus\n"},
                _PRIVATE: {"user-alice/episodic/ADR-1.md": b"# ADR-1\n\nprivate\n"},
            }
        )
        service = S3EpisodicService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="episodic/"
        )
        alice = _user("user-alice")
        deleted = await service.delete(alice, "ADR-1.md")
        assert deleted is True
        assert "user-alice/episodic/ADR-1.md" not in client._buckets[_PRIVATE]
        # Corpus untouched.
        assert "episodic/ADR-1.md" in client._buckets[_SHARED]
        # Reading again now falls back to corpus.
        doc = await service.read(alice, "ADR-1.md")
        assert doc is not None
        assert doc.metadata["tier"] == "corpus"

    async def test_delete_cannot_remove_another_users_private_object(
        self, user_context
    ) -> None:
        client = _FakeMinio(
            {_PRIVATE: {"user-alice/episodic/note.md": b"# alice's note\n"}}
        )
        service = S3EpisodicService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="episodic/"
        )
        bob = _user("user-bob")
        deleted = await service.delete(bob, "note.md")
        assert deleted is False  # Bob has no such key — nothing to delete
        assert "user-alice/episodic/note.md" in client._buckets[_PRIVATE]


class TestDualTierCacheIsolation:
    """The Redis-cache leak WU-B1 exists to close, exercised through the
    REAL service by neutering the user_id dimension in the key it consults
    (not just the key-scheme unit tests in ``test_layer_cache.py``).

    NEUTER: monkeypatch ``layer_list_cache_key_private`` to ignore
    ``user_id`` (the exact regression this key exists to prevent) → Bob's
    ``load()`` picks up Alice's cached private write → RED (leak proven).
    RESTORE: undo the monkeypatch → same scenario, same service instance →
    GREEN (isolated again). Proves the isolation is load-bearing, not
    vacuously true.
    """

    async def test_neutered_user_id_dimension_leaks_then_restored_key_isolates(
        self, user_context, monkeypatch
    ) -> None:
        import audittrace.services.episodic as episodic_module
        from audittrace.services.layer_cache import InMemoryLayerCacheStore

        shared_cache = InMemoryLayerCacheStore()
        client = _FakeMinio({_SHARED: {}, _PRIVATE: {}})
        service = S3EpisodicService(
            client,
            bucket=_SHARED,
            private_bucket=_PRIVATE,
            prefix="episodic/",
            cache=shared_cache,
        )
        alice, bob = _user("user-alice"), _user("user-bob")

        # NEUTER — private cache key collapses to a single fleet-wide key
        # (drops the user_id dimension), the exact bug the real key
        # prevents.
        monkeypatch.setattr(
            episodic_module,
            "layer_list_cache_key_private",
            lambda layer, user_id: "audittrace:layer-cache:episodic:private:list",
        )
        await service.write(alice, "alice-secret.md", "# secret\n")
        # Warm the (buggy, now user_id-agnostic) private cache with Alice's
        # own listing — this is the read a normal "list my private items"
        # call would make right after the write.
        await service.load(alice)
        # Bob's private listing now collapses onto the SAME cache key and
        # gets served Alice's cached rows — the leak.
        bob_files_neutered = {d.metadata["file"] for d in await service.load(bob)}
        assert "alice-secret.md" in bob_files_neutered  # RED — leak proven

        # RESTORE — undo the monkeypatch, fresh service + cache so no
        # neutered-state cache entries linger.
        monkeypatch.undo()
        clean_cache = InMemoryLayerCacheStore()
        clean_client = _FakeMinio({_SHARED: {}, _PRIVATE: {}})
        clean_service = S3EpisodicService(
            clean_client,
            bucket=_SHARED,
            private_bucket=_PRIVATE,
            prefix="episodic/",
            cache=clean_cache,
        )
        await clean_service.write(alice, "alice-secret.md", "# secret\n")
        bob_files_restored = {d.metadata["file"] for d in await clean_service.load(bob)}
        assert "alice-secret.md" not in bob_files_restored  # GREEN — isolated


# ── MockEpisodicService tests ────────────────────────────────────────────────


class TestMockEpisodicService:
    async def test_mock_starts_empty(self, user_context):
        service = MockEpisodicService()
        assert await service.load(user_context) == []
        assert await service.search(user_context, "anything") == []

    async def test_mock_add_and_load(self, user_context):
        service = MockEpisodicService()
        service.add_document(
            "ADR content about cache", title="ADR-009", file="ADR-009.md"
        )
        docs = await service.load(user_context)
        assert len(docs) == 1
        assert docs[0].metadata["title"] == "ADR-009"
        assert docs[0].metadata["tier"] == "corpus"

    async def test_mock_search_filters(self, user_context):
        service = MockEpisodicService()
        service.add_document("KV cache compression", title="ADR-009", file="ADR-009.md")
        service.add_document("ROCm GPU setup", title="ADR-001", file="ADR-001.md")
        results = await service.search(user_context, "cache")
        assert len(results) == 1
        assert results[0].metadata["title"] == "ADR-009"

    async def test_mock_reset(self, user_context):
        service = MockEpisodicService()
        service.add_document("test", title="T", file="T.md")
        service.reset()
        assert await service.load(user_context) == []

    def test_abstract_interface(self):
        """Verify MockEpisodicService is a valid EpisodicService."""
        service = MockEpisodicService()
        assert isinstance(service, EpisodicService)

    async def test_mock_as_context_renders_matched(self, user_context):
        """as_context with results renders the section header + content slice."""
        service = MockEpisodicService()
        service.add_document(
            "Detailed body about KV cache compression",
            title="ADR-009",
            file="ADR-009.md",
        )
        out = await service.as_context(user_context, "compression")
        assert "## Architecture Decisions" in out
        assert "ADR-009" in out
        assert "compression" in out

    async def test_mock_search_short_query_returns_empty(self, user_context):
        """Queries with no keywords > 3 chars must return [] (no spam matches)."""
        service = MockEpisodicService()
        service.add_document("anything", title="T", file="T.md")
        assert await service.search(user_context, "hi a") == []

    async def test_mock_as_context_no_match_returns_empty_string(self, user_context):
        """as_context returns "" when search yields nothing."""
        service = MockEpisodicService()
        service.add_document("body", title="T", file="T.md")
        assert await service.as_context(user_context, "nothing-matches-here") == ""

    async def test_mock_read_returns_matching_document(self, user_context):
        service = MockEpisodicService()
        service.add_document("contents", title="ADR-007", file="ADR-007.md")
        doc = await service.read(user_context, "ADR-007.md")
        assert doc is not None
        assert doc.page_content == "contents"

    async def test_mock_read_returns_none_when_missing(self, user_context):
        service = MockEpisodicService()
        service.add_document("contents", title="ADR-007", file="ADR-007.md")
        assert await service.read(user_context, "ADR-999.md") is None

    async def test_mock_read_rejects_path_traversal(self, user_context):
        service = MockEpisodicService()
        service.add_document("contents", title="ADR-007", file="ADR-007.md")
        assert await service.read(user_context, "../etc/passwd.md") is None

    def test_mock_add_document_private_requires_user_id(self) -> None:
        service = MockEpisodicService()
        with pytest.raises(ValueError, match="user_id"):
            service.add_document("secret", tier="private")


class TestMockEpisodicServiceDualTierIsolation:
    """Same isolation-safety-bar as the S3 fake, exercised against the mock
    (routes/tool tests use the mock via ``create_test_container``)."""

    async def test_private_document_not_visible_to_other_user(
        self, user_context
    ) -> None:
        service = MockEpisodicService()
        service.add_document("corpus item", title="Seed", file="seed.md")
        service.add_document(
            "alice's private note",
            title="Alice",
            file="alice.md",
            tier="private",
            user_id="user-alice",
        )
        alice = _user("user-alice")
        bob = _user("user-bob")

        alice_files = {d.metadata["file"] for d in await service.load(alice)}
        bob_files = {d.metadata["file"] for d in await service.load(bob)}

        assert "alice.md" in alice_files
        assert "alice.md" not in bob_files
        assert "seed.md" in alice_files
        assert "seed.md" in bob_files

    async def test_mock_read_never_leaks_across_users(self, user_context) -> None:
        service = MockEpisodicService()
        service.add_document(
            "secret", file="secret.md", tier="private", user_id="user-alice"
        )
        assert await service.read(_user("user-bob"), "secret.md") is None
        assert await service.read(_user("user-alice"), "secret.md") is not None


# ── write / delete / invalidate_cache (PR A — CRUD backoffice) ──────────────


class TestS3EpisodicServiceWriteDelete:
    """write() and delete() round-trip through the fake MinIO and
    invalidate the private-tier cache so subsequent load()/read() see
    the change."""

    async def test_write_creates_object_and_invalidates_cache(
        self, user_context
    ) -> None:
        service = _service()
        # Warm the cache
        assert await service.load(user_context) == []
        # Write
        doc = await service.write(user_context, "ADR-100.md", "# ADR-100\n\nbody\n")
        assert doc.metadata["file"] == "ADR-100.md"
        assert doc.metadata["title"] == "ADR-100"
        # Object landed in the private bucket under the user prefix.
        key = f"{user_context.user_id}/episodic/ADR-100.md"
        assert key in service._client._buckets[_PRIVATE]
        # Cache was invalidated → next load() sees the new doc
        assert any(
            d.metadata["file"] == "ADR-100.md" for d in await service.load(user_context)
        )

    async def test_write_replaces_existing(self, user_context) -> None:
        key = f"{user_context.user_id}/episodic/ADR-x.md"
        service = _service(private={key: b"# v1\n"})
        await service.write(user_context, "ADR-x.md", "# v2\n")
        # Re-fetch
        doc = await service.read(user_context, "ADR-x.md")
        assert doc is not None
        assert doc.page_content == "# v2\n"

    async def test_write_rejects_invalid_filename(self, user_context) -> None:
        service = _service()
        with pytest.raises(ValueError):
            await service.write(user_context, "../escape.md", "x")

    async def test_delete_removes_existing(self, user_context) -> None:
        key = f"{user_context.user_id}/episodic/ADR-d.md"
        service = _service(private={key: b"# bye\n"})
        # Warm cache
        await service.load(user_context)
        deleted = await service.delete(user_context, "ADR-d.md")
        assert deleted is True
        assert key not in service._client._buckets[_PRIVATE]
        # Cache invalidated → not found
        assert await service.read(user_context, "ADR-d.md") is None

    async def test_delete_missing_returns_false(self, user_context) -> None:
        assert await _service().delete(user_context, "never.md") is False

    async def test_delete_rejects_invalid_filename(self, user_context) -> None:
        assert await _service().delete(user_context, "../escape.md") is False

    async def test_invalidate_cache_explicit_covers_corpus_tier(
        self, user_context
    ) -> None:
        """The no-arg ``invalidate_cache()`` is the CORPUS-side hook — it
        must surface a newly-appeared corpus object, same as before Phase B."""
        client = _FakeMinio({_SHARED: {"episodic/ADR-c.md": b"# c\n"}})
        service = S3EpisodicService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="episodic/"
        )
        await service.load(user_context)  # warm
        # Backdoor an extra object straight into the corpus bucket.
        client._buckets[_SHARED]["episodic/ADR-side.md"] = b"# side\n"
        # Without invalidation, cache hides it.
        files_before = {d.metadata["file"] for d in await service.load(user_context)}
        assert "ADR-side.md" not in files_before
        service.invalidate_cache()
        files_after = {d.metadata["file"] for d in await service.load(user_context)}
        assert "ADR-side.md" in files_after


class _FakeObjectWithMeta:
    """List-object double carrying size + last_modified (backlog #15, R4)."""

    def __init__(self, object_name: str, size: int, last_modified: Any) -> None:
        self.object_name = object_name
        self.size = size
        self.last_modified = last_modified


class _FakeMinioWithMeta(_FakeMinio):
    """As :class:`_FakeMinio` but ``list_objects`` yields last_modified."""

    def __init__(
        self, buckets: dict[str, dict[str, tuple[bytes, Any]]] | None = None
    ) -> None:
        self._rich: dict[str, dict[str, tuple[bytes, Any]]] = {
            b: dict(objs) for b, objs in (buckets or {}).items()
        }
        super().__init__(
            {
                bucket: {k: v[0] for k, v in objs.items()}
                for bucket, objs in self._rich.items()
            }
        )

    def list_objects(self, bucket: str, prefix: str = "", **kwargs: Any):
        del kwargs
        return [
            _FakeObjectWithMeta(k, len(v[0]), v[1])
            for k, v in self._rich.get(bucket, {}).items()
            if k.startswith(prefix)
        ]


class TestS3EpisodicServiceCacheAndTimestamps:
    """Backlog #15 — shared cache (R2), real timestamps (R4), three-view (R6b)."""

    async def test_last_modified_flows_into_document_metadata(self, user_context):
        from datetime import UTC, datetime

        lm = datetime(2026, 7, 24, 9, 30, 0, tzinfo=UTC)
        client = _FakeMinioWithMeta(
            {
                _SHARED: {
                    "episodic/decision-2026-07-24.md": (b"# Decision\n\nbody\n", lm)
                }
            }
        )
        service = S3EpisodicService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="episodic/"
        )
        docs = await service.load(user_context)
        assert len(docs) == 1
        assert docs[0].metadata["last_modified_ms"] == int(lm.timestamp() * 1000)

    async def test_injected_shared_store_is_used_and_invalidation_surfaces_object(
        self, user_context
    ):
        """The list path (load) consults the SHARED corpus store; an
        invalidation makes a newly-written corpus object appear (R6d). Two
        services over one store simulate two replicas (R2/Defect B)."""
        from audittrace.services.layer_cache import InMemoryLayerCacheStore

        shared = InMemoryLayerCacheStore()
        client = _FakeMinio({_SHARED: {"episodic/ADR-c.md": b"# c\n"}})
        pod_a = S3EpisodicService(
            client,
            bucket=_SHARED,
            private_bucket=_PRIVATE,
            prefix="episodic/",
            cache=shared,
        )
        pod_b = S3EpisodicService(
            client,
            bucket=_SHARED,
            private_bucket=_PRIVATE,
            prefix="episodic/",
            cache=shared,
        )

        # pod A warms the shared cache
        assert {d.metadata["file"] for d in await pod_a.load(user_context)} == {
            "ADR-c.md"
        }
        # A non-ADR object lands in S3 directly (as /memory/upload would do)
        client._buckets[_SHARED]["episodic/decision-new.md"] = b"# decision\n"
        # pod B still serves the shared cached listing (no re-read yet)
        assert "decision-new.md" not in {
            d.metadata["file"] for d in await pod_b.load(user_context)
        }
        # The write path invalidates the shared cache → fleet-wide
        pod_a.invalidate_cache()
        assert "decision-new.md" in {
            d.metadata["file"] for d in await pod_b.load(user_context)
        }

    async def test_three_view_consistency_for_non_adr_md(self, user_context):
        """R6b: an object present in S3 is enumerable by the list (load),
        fetchable by read(), AND seen by the /memory/index prefix walk — the
        three views agree on the SAME set, including non-ADR ``.md``."""
        from audittrace.routes.memory import _list_objects_from_minio

        client = _FakeMinio(
            {
                _SHARED: {
                    "episodic/ADR-1.md": b"# ADR-1\n\nbody\n",
                    "episodic/decision-2026-07-24.md": b"# Decision\n\nbody\n",
                }
            }
        )
        service = S3EpisodicService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="episodic/"
        )

        load_files = {d.metadata["file"] for d in await service.load(user_context)}
        index_files = {
            o["filename"]
            for o in _list_objects_from_minio(client, _SHARED, "episodic/")
            if o["filename"].endswith(".md")
        }
        read_doc = await service.read(user_context, "decision-2026-07-24.md")

        assert load_files == index_files == {"ADR-1.md", "decision-2026-07-24.md"}
        assert read_doc is not None  # read-by-key sees the same non-ADR object


class TestMockEpisodicServiceWriteDelete:
    """Mock variants — used by route tests + the in-process pytest stack."""

    async def test_write_then_read(self, user_context) -> None:
        service = MockEpisodicService()
        doc = await service.write(user_context, "ADR-7.md", "# Seven\n")
        assert doc.metadata["title"] == "Seven"
        # Read back
        assert (
            await service.read(user_context, "ADR-7.md")
        ).page_content == "# Seven\n"

    async def test_write_replaces(self, user_context) -> None:
        service = MockEpisodicService()
        await service.write(user_context, "ADR-7.md", "v1")
        await service.write(user_context, "ADR-7.md", "v2")
        assert len(await service.load(user_context)) == 1
        assert (await service.read(user_context, "ADR-7.md")).page_content == "v2"

    async def test_delete_existing_returns_true(self, user_context) -> None:
        service = MockEpisodicService()
        await service.write(user_context, "ADR-7.md", "x")
        assert await service.delete(user_context, "ADR-7.md") is True
        assert await service.read(user_context, "ADR-7.md") is None

    async def test_delete_missing_returns_false(self, user_context) -> None:
        service = MockEpisodicService()
        assert await service.delete(user_context, "never.md") is False

    def test_invalidate_cache_is_no_op(self, user_context) -> None:
        # Mock has no cache; calling should not error.
        service = MockEpisodicService()
        service.invalidate_cache()
        service.invalidate_cache()  # idempotent


# ── UserScopedEpisodicService (ADR-062 Phase B, WU-B1) ───────────────────────


class TestUserScopedEpisodicService:
    """Mirrors ``TestUserScopedSemanticService`` (semantic.py precedent):
    the bound identity wins over any per-call ``user_context`` — true
    isolation by construction at the ``get_context_builder()`` DI seam."""

    async def test_bound_identity_overrides_call_time_argument(
        self, user_context
    ) -> None:
        inner = MockEpisodicService()
        inner.add_document(
            "alice's note",
            file="alice.md",
            tier="private",
            user_id="user-alice",
        )
        alice = _user("user-alice")
        wrapper = UserScopedEpisodicService(inner=inner, user_context=alice)

        # Call-time argument is an entirely different (bogus admin) context —
        # the wrapper must ignore it and use the bound Alice identity.
        bogus = replace(user_context, user_id="someone-else", is_admin=True)
        docs = await wrapper.load(bogus)
        assert any(d.metadata["file"] == "alice.md" for d in docs)

    async def test_two_wrappers_stay_isolated(self, user_context) -> None:
        inner = MockEpisodicService()
        alice, bob = _user("user-alice"), _user("user-bob")
        wrapper_a = UserScopedEpisodicService(inner=inner, user_context=alice)
        wrapper_b = UserScopedEpisodicService(inner=inner, user_context=bob)

        await wrapper_a.write(alice, "note.md", "alice's content")
        alice_doc = await wrapper_a.read(alice, "note.md")
        bob_doc = await wrapper_b.read(bob, "note.md")

        assert alice_doc is not None
        assert bob_doc is None  # never leaks across the two wrappers

    async def test_search_and_as_context_and_delete_delegate_to_bound_user(
        self, user_context
    ) -> None:
        inner = MockEpisodicService()
        alice = _user("user-alice")
        wrapper = UserScopedEpisodicService(inner=inner, user_context=alice)

        await wrapper.write(alice, "cache-notes.md", "cache tuning details")
        hits = await wrapper.search(alice, "cache tuning")
        assert any(d.metadata["file"] == "cache-notes.md" for d in hits)

        ctx = await wrapper.as_context(alice, "cache tuning")
        assert "cache-notes.md" in ctx or "cache tuning" in ctx.lower()

        deleted = await wrapper.delete(alice, "cache-notes.md")
        assert deleted is True
        assert await wrapper.read(alice, "cache-notes.md") is None

    def test_invalidate_cache_delegates_to_inner(self) -> None:
        class _Spy(MockEpisodicService):
            def __init__(self) -> None:
                super().__init__()
                self.invalidated = False

            def invalidate_cache(self) -> None:
                self.invalidated = True

        inner = _Spy()
        wrapper = UserScopedEpisodicService(
            inner=inner, user_context=_user("user-alice")
        )
        wrapper.invalidate_cache()
        assert inner.invalidated is True
