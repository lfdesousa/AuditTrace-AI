"""Tests for ProceduralService — Layer 2 of the 4-layer memory architecture (ADR-018).

The service is **always S3-backed** in production (MinIO) — there is no
filesystem implementation. Tests here exercise ``S3ProceduralService`` against
a fake, bucket-aware MinIO client and ``MockProceduralService`` directly. See
``feedback_storage_always_s3``.

ADR-062 Phase B (WU-B2): the layer is dual-tier — a caller's own writes land
in the PRIVATE tier (``private_bucket/{user_id}/procedural/``); the
pre-existing ``memory-shared`` content is the CORPUS tier (shared-read). The
``TestDualTier*`` classes below are the isolation-safety-bar falsifiable
tests: a second user must never see another user's private writes, and every
caller must still see the corpus.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from audittrace_object_storage import ObjectNotFoundError

from audittrace.services.procedural import (
    MockProceduralService,
    ProceduralService,
    S3ProceduralService,
    UserScopedProceduralService,
)

# ── Fake, bucket-aware MinIO client ──────────────────────────────────────────
#
# ADR-006: fakes raise ObjectNotFoundError (shared package) rather than the
# old minio-shaped S3Error("NoSuchKey", ...).
#
# ADR-062 Phase B: real MinIO partitions objects by BUCKET — a key in
# ``memory-private`` is invisible to a client reading ``memory-shared``, even
# if the key string is identical. The fake is a real ``{bucket: {key: bytes}}``
# two-level store so the private/corpus boundary is faithfully simulated.


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

    def read(self) -> bytes:
        return self._content

    def close(self) -> None:
        return None

    def release_conn(self) -> None:
        return None

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
        self.release_conn()


class _FakeMinio:
    """Bucket-partitioned MinIO-client double. ``buckets`` is
    ``{bucket_name: {key: content_bytes}}``. A key written to one bucket is
    invisible from another — the same isolation real MinIO gives two
    distinct buckets."""

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
        return object()

    def remove_object(self, bucket: str, key: str) -> None:
        self._objects(bucket).pop(key, None)


# ── Fixtures ─────────────────────────────────────────────────────────────────

_SHARED = "memory-shared"
_PRIVATE = "memory-private"


@pytest.fixture
def fake_skill_objects() -> dict[str, bytes]:
    """Three sample SKILL-*.md objects under the ``procedural/`` prefix (corpus)."""
    return {
        "procedural/SKILL-IAM.md": (
            b"# IAM Skill\n\nOAuth2, OIDC, JWT validation, BFF pattern.\n"
        ),
        "procedural/SKILL-ARCHITECTURE.md": (
            b"# Architecture Skill\n\nC4 model, Structurizr DSL, EIP patterns.\n"
        ),
        "procedural/SKILL-memory-commands.md": (
            b"# Memory Commands\n\nCLI commands for memory indexing and query.\n"
        ),
    }


@pytest.fixture
def s3_procedural(fake_skill_objects: dict[str, bytes]) -> S3ProceduralService:
    return S3ProceduralService(
        minio_client=_FakeMinio({_SHARED: fake_skill_objects}),
        bucket=_SHARED,
        private_bucket=_PRIVATE,
        prefix="procedural/",
    )


def _service(
    corpus: dict[str, bytes] | None = None,
    private: dict[str, bytes] | None = None,
) -> S3ProceduralService:
    client = _FakeMinio({_SHARED: corpus or {}, _PRIVATE: private or {}})
    return S3ProceduralService(
        client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="procedural/"
    )


def _user(user_id: str) -> Any:
    from audittrace.identity import UserContext

    return UserContext(
        user_id=user_id,
        username=user_id,
        agent_type="opencode",
        scopes=("memory:procedural:read", "memory:procedural:write"),
        is_admin=False,
    )


# ── S3ProceduralService tests (corpus-only, pre-existing coverage) ──────────


class TestS3ProceduralService:
    async def test_load_returns_all_skills(
        self, s3_procedural: S3ProceduralService, user_context
    ):
        docs = await s3_procedural.load(user_context)
        assert len(docs) == 3

    async def test_load_extracts_skill_name(
        self, s3_procedural: S3ProceduralService, user_context
    ):
        docs = await s3_procedural.load(user_context)
        skills = [d.metadata["skill"] for d in docs]
        assert "IAM" in skills
        assert "ARCHITECTURE" in skills
        assert "memory-commands" in skills

    async def test_load_sets_metadata(
        self, s3_procedural: S3ProceduralService, user_context
    ):
        docs = await s3_procedural.load(user_context)
        for d in docs:
            assert d.metadata["source"] == "procedural"
            assert d.metadata["file"].startswith("SKILL-")
            assert d.metadata["tier"] == "corpus"

    async def test_load_includes_all_md_objects(self, user_context):
        """Backlog #15 (R1): every ``.md`` object is enumerated, not just
        SKILL-*.md — parity with the episodic fix."""
        service = _service(
            corpus={
                "procedural/README.md": b"# readme\n",
                "procedural/runbook-notes.md": b"# notes\n\nbody\n",
                "procedural/SKILL-X.md": b"# X\n\nbody\n",
            }
        )
        files = {d.metadata["file"] for d in await service.load(user_context)}
        assert files == {"README.md", "runbook-notes.md", "SKILL-X.md"}

    async def test_load_still_skips_non_md_objects(self, user_context):
        service = _service(
            corpus={
                "procedural/SKILL-X.md": b"# X\n\nbody\n",
                "procedural/asset.bin": b"\x00\x01",
            }
        )
        files = {d.metadata["file"] for d in await service.load(user_context)}
        assert files == {"SKILL-X.md"}

    async def test_load_handles_empty_bucket(self, user_context):
        assert await _service().load(user_context) == []

    async def test_load_handles_client_exception(self, user_context):
        class _Broken:
            def list_objects(self, *a: Any, **kw: Any) -> list[_FakeObject]:
                raise RuntimeError("connection refused")

        service = S3ProceduralService(
            _Broken(), bucket=_SHARED, private_bucket=_PRIVATE, prefix="procedural/"
        )
        assert await service.load(user_context) == []

    async def test_search_filters_by_query(
        self, s3_procedural: S3ProceduralService, user_context
    ):
        results = await s3_procedural.search(user_context, "OAuth2 validation")
        assert len(results) >= 1
        assert any("IAM" in d.metadata["skill"] for d in results)

    async def test_search_matches_skill_name(
        self, s3_procedural: S3ProceduralService, user_context
    ):
        results = await s3_procedural.search(user_context, "architecture patterns")
        assert any("ARCHITECTURE" in d.metadata["skill"] for d in results)

    async def test_search_no_match_returns_empty(
        self, s3_procedural: S3ProceduralService, user_context
    ):
        assert await s3_procedural.search(user_context, "quantum physics") == []

    async def test_search_no_arbitrary_cap(self, user_context):
        """If 4 skills match, all 4 should be returned."""
        objs = {
            f"procedural/SKILL-CLOUD-{n}.md": (
                f"# CLOUD-{n} Skill\n\nCloud architecture and cloud migration.\n"
            ).encode()
            for n in ("STRATEGY", "APP-PATTERNS", "SECURITY", "MIGRATION")
        }
        service = _service(corpus=objs)
        results = await service.search(user_context, "cloud migration patterns")
        assert len(results) == 4

    async def test_search_short_query_returns_empty(
        self, s3_procedural: S3ProceduralService, user_context
    ):
        assert await s3_procedural.search(user_context, "hi a") == []

    async def test_search_matches_content_beyond_first_200_chars(self, user_context):
        """Regression: keywords deep in the file must still match."""
        filler = "lorem ipsum " * 25
        service = _service(
            corpus={
                "procedural/SKILL-IAM.md": (
                    f"# IAM Skill\n\n{filler}\n\nDeep content with quantum keyword.\n"
                ).encode()
            }
        )
        results = await service.search(user_context, "quantum")
        assert len(results) == 1
        assert results[0].metadata["skill"] == "IAM"

    async def test_as_context_returns_formatted_string(
        self, s3_procedural: S3ProceduralService, user_context
    ):
        ctx = await s3_procedural.as_context(user_context, "memory commands")
        assert "Relevant Skills" in ctx
        assert "memory-commands" in ctx

    async def test_as_context_empty_when_no_match(
        self, s3_procedural: S3ProceduralService, user_context
    ):
        assert await s3_procedural.as_context(user_context, "quantum") == ""


class TestS3ProceduralServiceRead:
    """``read(file)`` — full-content fetch by exact filename (Phase A.1)."""

    async def test_read_existing_file_returns_full_content(
        self,
        s3_procedural: S3ProceduralService,
        fake_skill_objects: dict[str, bytes],
        user_context,
    ):
        doc = await s3_procedural.read(user_context, "SKILL-IAM.md")
        assert doc is not None
        expected = fake_skill_objects["procedural/SKILL-IAM.md"].decode("utf-8")
        assert doc.page_content == expected
        assert doc.metadata["file"] == "SKILL-IAM.md"
        assert doc.metadata["skill"] == "IAM"
        assert doc.metadata["source"] == "procedural"
        assert doc.metadata["tier"] == "corpus"

    async def test_read_missing_file_returns_none(
        self, s3_procedural: S3ProceduralService, user_context
    ):
        assert await s3_procedural.read(user_context, "SKILL-NOPE.md") is None

    async def test_read_rejects_path_traversal(
        self, s3_procedural: S3ProceduralService, user_context
    ):
        for bad in ["../passwd.md", "subdir/SKILL-IAM.md", "..\\win.md"]:
            assert await s3_procedural.read(user_context, bad) is None

    async def test_read_rejects_non_md(
        self, s3_procedural: S3ProceduralService, user_context
    ):
        assert await s3_procedural.read(user_context, "SKILL-IAM") is None
        assert await s3_procedural.read(user_context, "SKILL-IAM.txt") is None

    async def test_read_handles_unexpected_exception(self, user_context):
        class _Broken:
            def get_object(self, *a: Any, **kw: Any) -> _FakeResponse:
                raise RuntimeError("connection reset")

        service = S3ProceduralService(
            _Broken(), bucket=_SHARED, private_bucket=_PRIVATE, prefix="procedural/"
        )
        assert await service.read(user_context, "SKILL-X.md") is None

    async def test_read_returns_full_untruncated_content(self, user_context):
        big = ("# IAM Skill\n\n" + ("body line.\n" * 5000)).encode()
        service = _service(corpus={"procedural/SKILL-IAM.md": big})
        doc = await service.read(user_context, "SKILL-IAM.md")
        assert doc is not None
        assert len(doc.page_content) == len(big.decode())
        assert len(doc.page_content) > 5000


# ── ADR-062 Phase B — dual-tier isolation-safety-bar tests ──────────────────


class TestDualTierWrite:
    async def test_write_lands_in_private_bucket_only(self, user_context) -> None:
        """WU-B2 falsifiable: write() must NOT touch the corpus bucket."""
        client = _FakeMinio({_SHARED: {}, _PRIVATE: {}})
        alice = _user("user-alice")
        service = S3ProceduralService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="procedural/"
        )
        await service.write(alice, "SKILL-notes.md", "# notes\n")
        assert "user-alice/procedural/SKILL-notes.md" in client._buckets[_PRIVATE]
        assert client._buckets.get(_SHARED, {}) == {}

    async def test_write_stamps_tier_private(self, user_context) -> None:
        service = _service()
        doc = await service.write(user_context, "SKILL-notes.md", "# notes\n")
        assert doc.metadata["tier"] == "private"

    async def test_write_never_shadows_another_users_key(self, user_context) -> None:
        client = _FakeMinio({_SHARED: {}, _PRIVATE: {}})
        service = S3ProceduralService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="procedural/"
        )
        alice, bob = _user("user-alice"), _user("user-bob")
        await service.write(alice, "SKILL-notes.md", "# alice's notes\n")
        await service.write(bob, "SKILL-notes.md", "# bob's notes\n")
        private = client._buckets[_PRIVATE]
        assert private["user-alice/procedural/SKILL-notes.md"] == b"# alice's notes\n"
        assert private["user-bob/procedural/SKILL-notes.md"] == b"# bob's notes\n"


class TestDualTierListAndSearchIsolation:
    """The ADR-062 §3 isolation-safety-bar, falsified end to end: user A
    writes private content; user B's ``load``/``search`` must NOT return it,
    but BOTH must still see the corpus, tagged ``tier=corpus``."""

    async def test_private_write_not_visible_to_other_user_via_load(
        self, user_context
    ) -> None:
        client = _FakeMinio(
            {
                _SHARED: {
                    "procedural/SKILL-seed.md": b"# Seed skill\n\nCorpus content.\n"
                }
            }
        )
        service = S3ProceduralService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="procedural/"
        )
        alice, bob = _user("user-alice"), _user("user-bob")

        await service.write(
            alice, "SKILL-alice-secret.md", "# Alice's private skill note\n"
        )

        alice_files = {d.metadata["file"] for d in await service.load(alice)}
        bob_files = {d.metadata["file"] for d in await service.load(bob)}

        assert "SKILL-alice-secret.md" in alice_files
        assert "SKILL-alice-secret.md" not in bob_files  # isolation-safety-bar
        assert "SKILL-seed.md" in alice_files
        assert "SKILL-seed.md" in bob_files

    async def test_corpus_items_are_tagged_tier_corpus_for_every_caller(
        self, user_context
    ) -> None:
        client = _FakeMinio(
            {
                _SHARED: {
                    "procedural/SKILL-seed.md": b"# Seed skill\n\nCorpus content.\n"
                }
            }
        )
        service = S3ProceduralService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="procedural/"
        )
        bob = _user("user-bob")
        docs = await service.load(bob)
        seed = next(d for d in docs if d.metadata["file"] == "SKILL-seed.md")
        assert seed.metadata["tier"] == "corpus"

    async def test_private_write_tagged_tier_private_for_owner(
        self, user_context
    ) -> None:
        client = _FakeMinio({_SHARED: {}})
        service = S3ProceduralService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="procedural/"
        )
        alice = _user("user-alice")
        await service.write(alice, "SKILL-alice-secret.md", "# secret\n")
        docs = await service.load(alice)
        mine = next(d for d in docs if d.metadata["file"] == "SKILL-alice-secret.md")
        assert mine.metadata["tier"] == "private"

    async def test_private_write_not_found_by_other_user_via_search(
        self, user_context
    ) -> None:
        client = _FakeMinio({_SHARED: {}})
        service = S3ProceduralService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="procedural/"
        )
        alice, bob = _user("user-alice"), _user("user-bob")
        await service.write(
            alice,
            "SKILL-alice-cache-tuning.md",
            "# Cache tuning notes\n\ncache cache\n",
        )
        alice_hits = await service.search(alice, "cache tuning")
        bob_hits = await service.search(bob, "cache tuning")
        assert any(
            d.metadata["file"] == "SKILL-alice-cache-tuning.md" for d in alice_hits
        )
        assert not any(
            d.metadata["file"] == "SKILL-alice-cache-tuning.md" for d in bob_hits
        )

    async def test_private_shadows_same_named_corpus_object(self, user_context) -> None:
        """A private object with the SAME filename as a corpus object wins
        the merge (ADR-062 §3: "private shadows corpus")."""
        client = _FakeMinio(
            {_SHARED: {"procedural/SKILL-IAM.md": b"# IAM\n\ncorpus version\n"}}
        )
        service = S3ProceduralService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="procedural/"
        )
        alice = _user("user-alice")
        await service.write(alice, "SKILL-IAM.md", "# IAM\n\nalice's private version\n")
        docs = await service.load(alice)
        matches = [d for d in docs if d.metadata["file"] == "SKILL-IAM.md"]
        assert len(matches) == 1  # deduped, not duplicated
        assert matches[0].metadata["tier"] == "private"
        assert "alice's private version" in matches[0].page_content


class TestDualTierRead:
    async def test_read_tries_private_first(self, user_context) -> None:
        client = _FakeMinio(
            {
                _SHARED: {"procedural/SKILL-IAM.md": b"# IAM\n\ncorpus\n"},
                _PRIVATE: {
                    "user-alice/procedural/SKILL-IAM.md": (
                        b"# IAM\n\nprivate override\n"
                    )
                },
            }
        )
        service = S3ProceduralService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="procedural/"
        )
        doc = await service.read(_user("user-alice"), "SKILL-IAM.md")
        assert doc is not None
        assert "private override" in doc.page_content
        assert doc.metadata["tier"] == "private"

    async def test_read_falls_back_to_corpus_when_no_private_object(
        self, user_context
    ) -> None:
        client = _FakeMinio(
            {_SHARED: {"procedural/SKILL-IAM.md": b"# IAM\n\ncorpus\n"}, _PRIVATE: {}}
        )
        service = S3ProceduralService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="procedural/"
        )
        doc = await service.read(_user("user-bob"), "SKILL-IAM.md")
        assert doc is not None
        assert doc.metadata["tier"] == "corpus"

    async def test_read_by_filename_never_leaks_another_users_private_object(
        self, user_context
    ) -> None:
        client = _FakeMinio({_SHARED: {}, _PRIVATE: {}})
        service = S3ProceduralService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="procedural/"
        )
        alice, bob = _user("user-alice"), _user("user-bob")
        await service.write(alice, "SKILL-alice-secret.md", "# secret\n")
        assert await service.read(bob, "SKILL-alice-secret.md") is None
        assert await service.read(alice, "SKILL-alice-secret.md") is not None


class TestDualTierDelete:
    async def test_delete_only_removes_private_object_never_corpus(
        self, user_context
    ) -> None:
        client = _FakeMinio(
            {
                _SHARED: {"procedural/SKILL-IAM.md": b"# IAM\n\ncorpus\n"},
                _PRIVATE: {"user-alice/procedural/SKILL-IAM.md": b"# IAM\n\nprivate\n"},
            }
        )
        service = S3ProceduralService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="procedural/"
        )
        alice = _user("user-alice")
        deleted = await service.delete(alice, "SKILL-IAM.md")
        assert deleted is True
        assert "user-alice/procedural/SKILL-IAM.md" not in client._buckets[_PRIVATE]
        assert "procedural/SKILL-IAM.md" in client._buckets[_SHARED]
        doc = await service.read(alice, "SKILL-IAM.md")
        assert doc is not None
        assert doc.metadata["tier"] == "corpus"

    async def test_delete_cannot_remove_another_users_private_object(
        self, user_context
    ) -> None:
        client = _FakeMinio(
            {_PRIVATE: {"user-alice/procedural/SKILL-notes.md": b"# alice's notes\n"}}
        )
        service = S3ProceduralService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="procedural/"
        )
        bob = _user("user-bob")
        deleted = await service.delete(bob, "SKILL-notes.md")
        assert deleted is False
        assert "user-alice/procedural/SKILL-notes.md" in client._buckets[_PRIVATE]


class TestDualTierCacheIsolation:
    """The Redis-cache leak WU-B1 exists to close, exercised through the
    REAL service by neutering the user_id dimension in the key it consults.

    NEUTER: monkeypatch ``layer_list_cache_key_private`` to ignore
    ``user_id`` → Bob's ``load()`` picks up Alice's cached private write →
    RED (leak proven). RESTORE: undo the monkeypatch → GREEN (isolated).
    """

    async def test_neutered_user_id_dimension_leaks_then_restored_key_isolates(
        self, user_context, monkeypatch
    ) -> None:
        import audittrace.services.procedural as procedural_module
        from audittrace.services.layer_cache import InMemoryLayerCacheStore

        shared_cache = InMemoryLayerCacheStore()
        client = _FakeMinio({_SHARED: {}, _PRIVATE: {}})
        service = S3ProceduralService(
            client,
            bucket=_SHARED,
            private_bucket=_PRIVATE,
            prefix="procedural/",
            cache=shared_cache,
        )
        alice, bob = _user("user-alice"), _user("user-bob")

        monkeypatch.setattr(
            procedural_module,
            "layer_list_cache_key_private",
            lambda layer, user_id: "audittrace:layer-cache:procedural:private:list",
        )
        await service.write(alice, "SKILL-alice-secret.md", "# secret\n")
        await service.load(alice)  # warms the (now user_id-agnostic) key
        bob_files_neutered = {d.metadata["file"] for d in await service.load(bob)}
        assert "SKILL-alice-secret.md" in bob_files_neutered  # RED — leak proven

        monkeypatch.undo()
        clean_cache = InMemoryLayerCacheStore()
        clean_client = _FakeMinio({_SHARED: {}, _PRIVATE: {}})
        clean_service = S3ProceduralService(
            clean_client,
            bucket=_SHARED,
            private_bucket=_PRIVATE,
            prefix="procedural/",
            cache=clean_cache,
        )
        await clean_service.write(alice, "SKILL-alice-secret.md", "# secret\n")
        bob_files_restored = {d.metadata["file"] for d in await clean_service.load(bob)}
        assert "SKILL-alice-secret.md" not in bob_files_restored  # GREEN


# ── MockProceduralService tests ──────────────────────────────────────────────


class TestMockProceduralService:
    async def test_mock_starts_empty(self, user_context):
        service = MockProceduralService()
        assert await service.load(user_context) == []

    async def test_mock_add_and_load(self, user_context):
        service = MockProceduralService()
        service.add_document("OAuth2 patterns", skill="IAM", file="SKILL-IAM.md")
        docs = await service.load(user_context)
        assert len(docs) == 1
        assert docs[0].metadata["skill"] == "IAM"
        assert docs[0].metadata["tier"] == "corpus"

    async def test_mock_search_filters(self, user_context):
        service = MockProceduralService()
        service.add_document("OAuth2 JWT", skill="IAM", file="SKILL-IAM.md")
        service.add_document("C4 model", skill="ARCHITECTURE", file="SKILL-ARCH.md")
        results = await service.search(user_context, "OAuth2")
        assert len(results) == 1

    async def test_mock_reset(self, user_context):
        service = MockProceduralService()
        service.add_document("test", skill="T", file="T.md")
        service.reset()
        assert await service.load(user_context) == []

    def test_abstract_interface(self):
        assert isinstance(MockProceduralService(), ProceduralService)

    async def test_mock_search_short_query_returns_empty(self, user_context):
        service = MockProceduralService()
        service.add_document("body", skill="X", file="SKILL-X.md")
        assert await service.search(user_context, "hi a") == []

    async def test_mock_as_context_renders_matched(self, user_context):
        service = MockProceduralService()
        service.add_document(
            "OAuth2 implementation patterns", skill="IAM", file="SKILL-IAM.md"
        )
        out = await service.as_context(user_context, "OAuth2")
        assert "## Relevant Skills" in out
        assert "IAM" in out
        assert "SKILL-IAM.md" in out

    async def test_mock_as_context_no_match_returns_empty_string(self, user_context):
        service = MockProceduralService()
        service.add_document("body", skill="X", file="SKILL-X.md")
        assert await service.as_context(user_context, "nothing-matches") == ""

    async def test_mock_read_returns_matching_document(self, user_context):
        service = MockProceduralService()
        service.add_document("body", skill="IAM", file="SKILL-IAM.md")
        doc = await service.read(user_context, "SKILL-IAM.md")
        assert doc is not None
        assert doc.page_content == "body"

    async def test_mock_read_returns_none_when_missing(self, user_context):
        service = MockProceduralService()
        service.add_document("body", skill="IAM", file="SKILL-IAM.md")
        assert await service.read(user_context, "SKILL-NOPE.md") is None

    async def test_mock_read_rejects_path_traversal(self, user_context):
        service = MockProceduralService()
        service.add_document("body", skill="IAM", file="SKILL-IAM.md")
        assert await service.read(user_context, "../passwd.md") is None

    def test_mock_add_document_private_requires_user_id(self) -> None:
        service = MockProceduralService()
        with pytest.raises(ValueError, match="user_id"):
            service.add_document("secret", tier="private")


class TestMockProceduralServiceDualTierIsolation:
    async def test_private_document_not_visible_to_other_user(
        self, user_context
    ) -> None:
        service = MockProceduralService()
        service.add_document("corpus item", skill="Seed", file="SKILL-seed.md")
        service.add_document(
            "alice's private skill",
            skill="Alice",
            file="SKILL-alice.md",
            tier="private",
            user_id="user-alice",
        )
        alice = _user("user-alice")
        bob = _user("user-bob")

        alice_files = {d.metadata["file"] for d in await service.load(alice)}
        bob_files = {d.metadata["file"] for d in await service.load(bob)}

        assert "SKILL-alice.md" in alice_files
        assert "SKILL-alice.md" not in bob_files
        assert "SKILL-seed.md" in alice_files
        assert "SKILL-seed.md" in bob_files

    async def test_mock_read_never_leaks_across_users(self, user_context) -> None:
        service = MockProceduralService()
        service.add_document(
            "secret", file="SKILL-secret.md", tier="private", user_id="user-alice"
        )
        assert await service.read(_user("user-bob"), "SKILL-secret.md") is None
        assert await service.read(_user("user-alice"), "SKILL-secret.md") is not None


# ── write / delete / invalidate_cache (PR A — CRUD backoffice) ──────────────


class TestS3ProceduralServiceWriteDelete:
    async def test_write_creates_and_invalidates_cache(self, user_context) -> None:
        service = _service()
        await service.load(user_context)  # warm
        doc = await service.write(user_context, "SKILL-NEW.md", "# NEW\n\nbody\n")
        assert doc.metadata["skill"] == "NEW"
        key = f"{user_context.user_id}/procedural/SKILL-NEW.md"
        assert key in service._client._buckets[_PRIVATE]
        assert any(
            d.metadata["file"] == "SKILL-NEW.md"
            for d in await service.load(user_context)
        )

    async def test_write_replaces_existing(self, user_context) -> None:
        key = f"{user_context.user_id}/procedural/SKILL-X.md"
        service = _service(private={key: b"# v1\n"})
        await service.write(user_context, "SKILL-X.md", "# v2\n")
        doc = await service.read(user_context, "SKILL-X.md")
        assert doc is not None and doc.page_content == "# v2\n"

    async def test_write_rejects_invalid_filename(self, user_context) -> None:
        service = _service()
        with pytest.raises(ValueError):
            await service.write(user_context, "../escape.md", "x")

    async def test_delete_existing(self, user_context) -> None:
        key = f"{user_context.user_id}/procedural/SKILL-bye.md"
        service = _service(private={key: b"# bye\n"})
        await service.load(user_context)
        assert await service.delete(user_context, "SKILL-bye.md") is True
        assert key not in service._client._buckets[_PRIVATE]
        assert await service.read(user_context, "SKILL-bye.md") is None

    async def test_delete_missing_returns_false(self, user_context) -> None:
        assert await _service().delete(user_context, "never.md") is False

    async def test_invalidate_cache_explicit_covers_corpus_tier(
        self, user_context
    ) -> None:
        client = _FakeMinio({_SHARED: {"procedural/SKILL-c.md": b"# c\n"}})
        service = S3ProceduralService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="procedural/"
        )
        await service.load(user_context)
        client._buckets[_SHARED]["procedural/SKILL-side.md"] = b"# side\n"
        before = {d.metadata["file"] for d in await service.load(user_context)}
        assert "SKILL-side.md" not in before
        service.invalidate_cache()
        after = {d.metadata["file"] for d in await service.load(user_context)}
        assert "SKILL-side.md" in after


class TestMockProceduralServiceWriteDelete:
    async def test_write_then_read(self, user_context) -> None:
        service = MockProceduralService()
        doc = await service.write(user_context, "SKILL-foo.md", "# Foo\n")
        assert doc.metadata["skill"] == "foo"
        assert (
            await service.read(user_context, "SKILL-foo.md")
        ).page_content == "# Foo\n"

    async def test_delete_existing(self, user_context) -> None:
        service = MockProceduralService()
        await service.write(user_context, "SKILL-x.md", "x")
        assert await service.delete(user_context, "SKILL-x.md") is True
        assert await service.read(user_context, "SKILL-x.md") is None

    def test_invalidate_cache_no_op(self, user_context) -> None:
        service = MockProceduralService()
        service.invalidate_cache()  # no exception


# ── Filename-validation branch hardening (#364) ─────────────────────────────
#
# ``_validate_filename`` is the only thing standing between a CRUD-backoffice
# caller and arbitrary object keys in the private bucket. The tests below pin
# the guard on the write/delete surface (read was already covered) and pin
# the "keep scanning" side of the per-document match loops, where an
# off-by-one would clobber or delete the wrong skill.


class TestFilenameValidationOnWriteDelete:
    """Empty / non-string / traversal filenames must never reach storage."""

    async def test_empty_filename_is_rejected_everywhere(self, user_context) -> None:
        """An empty ``file`` must not resolve to the prefix directory itself.

        ``key = f"{user_id}/{self._prefix}{file}"`` with an empty ``file``
        produces the user's prefix directory itself — a valid object key.
        Without the emptiness check a delete of ``""`` would target that
        key, and a read of ``""`` would return whatever sits there. The
        guard has to fire before the key is built.
        """
        service = MockProceduralService()
        service.add_document("body", skill="IAM", file="SKILL-IAM.md")

        assert await service.read(user_context, "") is None
        assert await service.delete(user_context, "") is False
        with pytest.raises(ValueError):
            await service.write(user_context, "", "payload")
        # The pre-existing corpus skill is untouched by any of the attempts.
        assert len(await service.load(user_context)) == 1

    async def test_non_string_filename_is_rejected(self, user_context) -> None:
        """A JSON body with ``"file": null`` arrives as ``None``, not a string.

        ``None.endswith(".md")`` raises ``AttributeError``, which would surface
        as a 500 from the CRUD route instead of a clean rejection. The
        isinstance half of the guard is what turns that into a normal
        "invalid filename" answer.
        """
        service = MockProceduralService()

        assert await service.read(user_context, None) is None  # type: ignore[arg-type]
        assert await service.delete(user_context, None) is False  # type: ignore[arg-type]

    async def test_mock_write_rejects_traversal_filename(self, user_context) -> None:
        """The mock must reject exactly what the S3 implementation rejects.

        ``MockProceduralService`` is what most unit tests run against. If it
        accepted ``../`` filenames the CRUD tests would pass while the real
        S3 path rejected them, and the traversal defence would only ever be
        exercised in production.
        """
        service = MockProceduralService()

        with pytest.raises(ValueError, match="invalid filename"):
            await service.write(user_context, "../../etc/passwd.md", "pwned")
        with pytest.raises(ValueError, match="invalid filename"):
            await service.write(user_context, "SKILL-notes.txt", "wrong suffix")

        # Nothing was appended by the rejected writes (private tier stays empty).
        assert await service.load(user_context) == []

    async def test_s3_delete_rejects_traversal_before_touching_storage(
        self, user_context
    ) -> None:
        """The S3 delete guard must short-circuit *before* the MinIO call.

        ``key = f"{user_id}/{self._prefix}{file}"`` normalises nothing, so
        ``"../secrets.md"`` would resolve outside the intended prefix.
        Asserting the client was never called (rather than only that the
        return value is ``False``) is what pins the short-circuit.
        """
        calls: list[str] = []

        class _RecordingMinio(_FakeMinio):
            def stat_object(self, bucket: str, key: str) -> object:
                calls.append(f"stat:{bucket}:{key}")
                return super().stat_object(bucket, key)

            def remove_object(self, bucket: str, key: str) -> None:
                calls.append(f"remove:{bucket}:{key}")
                super().remove_object(bucket, key)

        client = _RecordingMinio({_SHARED: {"procedural/SKILL-IAM.md": b"# IAM\n"}})
        service = S3ProceduralService(
            client, bucket=_SHARED, private_bucket=_PRIVATE, prefix="procedural/"
        )

        assert await service.delete(user_context, "../secrets.md") is False
        assert await service.delete(user_context, "SKILL-IAM") is False

        # Decisive: the object store was never asked to do anything.
        assert calls == []


class TestMockMatchLoops:
    """The per-document scans must act on the named skill and nothing else.

    ADR-062 Phase B: ``write``/``delete`` now target the PRIVATE tier only,
    so these scans are seeded via ``write()`` (private) rather than
    ``add_document(..., tier="corpus")`` — matching what the CRUD route
    actually does.
    """

    async def test_write_updates_only_the_named_skill(self, user_context) -> None:
        """Overwriting one skill must leave its siblings byte-identical.

        The write path scans the caller's private documents for a filename
        match and replaces in place. If the scan stopped at the first entry
        rather than continuing past non-matches, writing to the second or
        third skill would silently overwrite the first.
        """
        service = MockProceduralService()
        await service.write(user_context, "SKILL-IAM.md", "iam body")
        await service.write(user_context, "SKILL-ARCH.md", "arch body")
        await service.write(user_context, "SKILL-CLI.md", "cli body")

        updated = await service.write(user_context, "SKILL-CLI.md", "cli body v2")

        assert updated.page_content == "cli body v2"
        assert updated.metadata["skill"] == "CLI"
        # No new document was appended — this was an in-place replace.
        docs = await service.load(user_context)
        assert len(docs) == 3
        by_file = {d.metadata["file"]: d.page_content for d in docs}
        assert by_file["SKILL-IAM.md"] == "iam body"
        assert by_file["SKILL-ARCH.md"] == "arch body"
        assert by_file["SKILL-CLI.md"] == "cli body v2"

    async def test_read_scans_past_non_matching_documents(self, user_context) -> None:
        """``read`` must find a skill that is not the first one stored.

        Recall and the CRUD backoffice both read by exact filename against a
        multi-skill store; a scan that only ever inspected the head of the
        list would return the wrong skill's content to the LLM.
        """
        service = MockProceduralService()
        service.add_document("iam body", skill="IAM", file="SKILL-IAM.md")
        service.add_document("arch body", skill="ARCH", file="SKILL-ARCH.md")

        doc = await service.read(user_context, "SKILL-ARCH.md")

        assert doc is not None
        assert doc.page_content == "arch body"
        assert doc.metadata["file"] == "SKILL-ARCH.md"

    async def test_delete_missing_skill_is_a_no_op(self, user_context) -> None:
        """Deleting an absent skill must report False and remove nothing.

        The route maps the boolean onto 404-vs-200. If the scan fell off the
        end and removed the last-inspected entry (or returned True), a delete
        of a typo'd filename would destroy an unrelated skill and report
        success.
        """
        service = MockProceduralService()
        await service.write(user_context, "SKILL-IAM.md", "iam body")
        await service.write(user_context, "SKILL-ARCH.md", "arch body")

        assert await service.delete(user_context, "SKILL-NOPE.md") is False

        remaining = {d.metadata["file"] for d in await service.load(user_context)}
        assert remaining == {"SKILL-IAM.md", "SKILL-ARCH.md"}

    async def test_delete_removes_only_the_named_skill(self, user_context) -> None:
        """Deleting the second of three skills must leave the other two.

        Same off-by-one risk as the write scan, but destructive: the index
        used for ``pop`` has to be the index of the *matching* document.
        """
        service = MockProceduralService()
        await service.write(user_context, "SKILL-IAM.md", "iam body")
        await service.write(user_context, "SKILL-ARCH.md", "arch body")
        await service.write(user_context, "SKILL-CLI.md", "cli body")

        assert await service.delete(user_context, "SKILL-ARCH.md") is True

        remaining = {d.metadata["file"] for d in await service.load(user_context)}
        assert remaining == {"SKILL-IAM.md", "SKILL-CLI.md"}


# ── UserScopedProceduralService (ADR-062 Phase B, WU-B1) ─────────────────────


class TestUserScopedProceduralService:
    """Mirrors ``TestUserScopedSemanticService`` (semantic.py precedent):
    the bound identity wins over any per-call ``user_context`` — true
    isolation by construction at the ``get_context_builder()`` DI seam."""

    async def test_bound_identity_overrides_call_time_argument(
        self, user_context
    ) -> None:
        inner = MockProceduralService()
        inner.add_document(
            "alice's skill",
            file="SKILL-alice.md",
            tier="private",
            user_id="user-alice",
        )
        alice = _user("user-alice")
        wrapper = UserScopedProceduralService(inner=inner, user_context=alice)

        bogus = replace(user_context, user_id="someone-else", is_admin=True)
        docs = await wrapper.load(bogus)
        assert any(d.metadata["file"] == "SKILL-alice.md" for d in docs)

    async def test_two_wrappers_stay_isolated(self, user_context) -> None:
        inner = MockProceduralService()
        alice, bob = _user("user-alice"), _user("user-bob")
        wrapper_a = UserScopedProceduralService(inner=inner, user_context=alice)
        wrapper_b = UserScopedProceduralService(inner=inner, user_context=bob)

        await wrapper_a.write(alice, "SKILL-notes.md", "alice's content")
        alice_doc = await wrapper_a.read(alice, "SKILL-notes.md")
        bob_doc = await wrapper_b.read(bob, "SKILL-notes.md")

        assert alice_doc is not None
        assert bob_doc is None

    async def test_search_and_as_context_and_delete_delegate_to_bound_user(
        self, user_context
    ) -> None:
        inner = MockProceduralService()
        alice = _user("user-alice")
        wrapper = UserScopedProceduralService(inner=inner, user_context=alice)

        await wrapper.write(alice, "SKILL-cache.md", "cache tuning details")
        hits = await wrapper.search(alice, "cache tuning")
        assert any(d.metadata["file"] == "SKILL-cache.md" for d in hits)

        ctx = await wrapper.as_context(alice, "cache tuning")
        assert "SKILL-cache.md" in ctx or "cache" in ctx.lower()

        deleted = await wrapper.delete(alice, "SKILL-cache.md")
        assert deleted is True
        assert await wrapper.read(alice, "SKILL-cache.md") is None

    def test_invalidate_cache_delegates_to_inner(self) -> None:
        class _Spy(MockProceduralService):
            def __init__(self) -> None:
                super().__init__()
                self.invalidated = False

            def invalidate_cache(self) -> None:
                self.invalidated = True

        inner = _Spy()
        wrapper = UserScopedProceduralService(
            inner=inner, user_context=_user("user-alice")
        )
        wrapper.invalidate_cache()
        assert inner.invalidated is True
