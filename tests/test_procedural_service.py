"""Tests for ProceduralService — Layer 2 of the 4-layer memory architecture (ADR-018).

The service is **always S3-backed** in production (MinIO) — there is no
filesystem implementation. Tests here exercise ``S3ProceduralService`` against
a fake MinIO client and ``MockProceduralService`` directly. See
``feedback_storage_always_s3``.
"""

from __future__ import annotations

from typing import Any

import pytest
from audittrace_object_storage import ObjectNotFoundError

from audittrace.services.procedural import (
    MockProceduralService,
    ProceduralService,
    S3ProceduralService,
)

# ── Fake MinIO client ────────────────────────────────────────────────────────
#
# ADR-006: fakes raise ObjectNotFoundError (shared package) rather than
# the old minio-shaped S3Error("NoSuchKey", ...). Matches the
# post-ADR-006 contract that the services catch.


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
    def __init__(self, objects: dict[str, bytes]) -> None:
        self._objects = dict(objects)

    def list_objects(
        self, bucket: str, prefix: str = "", **kwargs: Any
    ) -> list[_FakeObject]:
        del bucket, kwargs
        return [_FakeObject(k) for k in self._objects if k.startswith(prefix)]

    def get_object(self, bucket: str, key: str) -> _FakeResponse:
        del bucket
        if key not in self._objects:
            raise ObjectNotFoundError(f"Object does not exist: {key}")
        return _FakeResponse(self._objects[key])

    def put_object(self, bucket: str, key: str, body: Any, length: int) -> None:
        del bucket, length
        self._objects[key] = body.read()

    def stat_object(self, bucket: str, key: str) -> object:
        del bucket
        if key not in self._objects:
            raise ObjectNotFoundError(f"Object does not exist: {key}")
        return object()

    def remove_object(self, bucket: str, key: str) -> None:
        del bucket
        self._objects.pop(key, None)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_skill_objects() -> dict[str, bytes]:
    """Three sample SKILL-*.md objects under the ``procedural/`` prefix."""
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
        minio_client=_FakeMinio(fake_skill_objects),
        bucket="memory-shared",
        prefix="procedural/",
    )


# ── S3ProceduralService tests ────────────────────────────────────────────────


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

    async def test_load_skips_non_skill_keys(self, user_context):
        client = _FakeMinio(
            {
                "procedural/README.md": b"# readme\n",
                "procedural/SKILL-X.md": b"# X\n\nbody\n",
            }
        )
        service = S3ProceduralService(client, bucket="b", prefix="procedural/")
        files = [d.metadata["file"] for d in await service.load(user_context)]
        assert files == ["SKILL-X.md"]

    async def test_load_handles_empty_bucket(self, user_context):
        service = S3ProceduralService(_FakeMinio({}), bucket="b", prefix="procedural/")
        assert await service.load(user_context) == []

    async def test_load_handles_client_exception(self, user_context):
        class _Broken:
            def list_objects(self, *a: Any, **kw: Any) -> list[_FakeObject]:
                raise RuntimeError("connection refused")

        service = S3ProceduralService(_Broken(), bucket="b", prefix="procedural/")
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
        service = S3ProceduralService(
            _FakeMinio(objs), bucket="b", prefix="procedural/"
        )
        results = await service.search(user_context, "cloud migration patterns")
        assert len(results) == 4

    async def test_search_short_query_returns_empty(
        self, s3_procedural: S3ProceduralService, user_context
    ):
        assert await s3_procedural.search(user_context, "hi a") == []

    async def test_search_matches_content_beyond_first_200_chars(self, user_context):
        """Regression: keywords deep in the file must still match."""
        filler = "lorem ipsum " * 25
        client = _FakeMinio(
            {
                "procedural/SKILL-IAM.md": (
                    f"# IAM Skill\n\n{filler}\n\nDeep content with quantum keyword.\n"
                ).encode()
            }
        )
        service = S3ProceduralService(client, bucket="b", prefix="procedural/")
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

        service = S3ProceduralService(_Broken(), bucket="b", prefix="procedural/")
        assert await service.read(user_context, "SKILL-X.md") is None

    async def test_read_returns_full_untruncated_content(self, user_context):
        big = ("# IAM Skill\n\n" + ("body line.\n" * 5000)).encode()
        client = _FakeMinio({"procedural/SKILL-IAM.md": big})
        service = S3ProceduralService(client, bucket="b", prefix="procedural/")
        doc = await service.read(user_context, "SKILL-IAM.md")
        assert doc is not None
        assert len(doc.page_content) == len(big.decode())
        assert len(doc.page_content) > 5000


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


# ── write / delete / invalidate_cache (PR A — CRUD backoffice) ──────────────


class TestS3ProceduralServiceWriteDelete:
    async def test_write_creates_and_invalidates_cache(self, user_context) -> None:
        client = _FakeMinio({})
        service = S3ProceduralService(client, bucket="b", prefix="procedural/")
        await service.load(user_context)  # warm
        doc = await service.write(user_context, "SKILL-NEW.md", "# NEW\n\nbody\n")
        assert doc.metadata["skill"] == "NEW"
        assert "procedural/SKILL-NEW.md" in client._objects
        assert any(
            d.metadata["file"] == "SKILL-NEW.md"
            for d in await service.load(user_context)
        )

    async def test_write_replaces_existing(self, user_context) -> None:
        client = _FakeMinio({"procedural/SKILL-X.md": b"# v1\n"})
        service = S3ProceduralService(client, bucket="b", prefix="procedural/")
        await service.write(user_context, "SKILL-X.md", "# v2\n")
        doc = await service.read(user_context, "SKILL-X.md")
        assert doc is not None and doc.page_content == "# v2\n"

    async def test_write_rejects_invalid_filename(self, user_context) -> None:
        service = S3ProceduralService(_FakeMinio({}), bucket="b", prefix="procedural/")
        with pytest.raises(ValueError):
            await service.write(user_context, "../escape.md", "x")

    async def test_delete_existing(self, user_context) -> None:
        client = _FakeMinio({"procedural/SKILL-bye.md": b"# bye\n"})
        service = S3ProceduralService(client, bucket="b", prefix="procedural/")
        await service.load(user_context)
        assert await service.delete(user_context, "SKILL-bye.md") is True
        assert "procedural/SKILL-bye.md" not in client._objects
        assert await service.read(user_context, "SKILL-bye.md") is None

    async def test_delete_missing_returns_false(self, user_context) -> None:
        service = S3ProceduralService(_FakeMinio({}), bucket="b", prefix="procedural/")
        assert await service.delete(user_context, "never.md") is False

    async def test_invalidate_cache_explicit(self, user_context) -> None:
        client = _FakeMinio({"procedural/SKILL-c.md": b"# c\n"})
        service = S3ProceduralService(client, bucket="b", prefix="procedural/")
        await service.load(user_context)
        client._objects["procedural/SKILL-side.md"] = b"# side\n"
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
# caller and arbitrary object keys in the shared ``memory-shared`` bucket. The
# tests below pin the guard on the write/delete surface (read was already
# covered) and pin the "keep scanning" side of the per-document match loops,
# where an off-by-one would clobber or delete the wrong skill.


class TestFilenameValidationOnWriteDelete:
    """Empty / non-string / traversal filenames must never reach storage."""

    async def test_empty_filename_is_rejected_everywhere(self, user_context) -> None:
        """An empty ``file`` must not resolve to the prefix directory itself.

        ``key = f"{self._prefix}{file}"`` with an empty ``file`` produces
        ``"procedural/"`` — a valid object key. Without the emptiness check a
        delete of ``""`` would target that key, and a read of ``""`` would
        return whatever sits there. The guard has to fire before the key is
        built.
        """
        service = MockProceduralService()
        service.add_document("body", skill="IAM", file="SKILL-IAM.md")

        assert await service.read(user_context, "") is None
        assert await service.delete(user_context, "") is False
        with pytest.raises(ValueError):
            await service.write(user_context, "", "payload")
        # The pre-existing skill is untouched by any of the three attempts.
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

        # Nothing was appended by the rejected writes.
        assert await service.load(user_context) == []

    async def test_s3_delete_rejects_traversal_before_touching_storage(
        self, user_context
    ) -> None:
        """The S3 delete guard must short-circuit *before* the MinIO call.

        ``key = f"{self._prefix}{file}"`` normalises nothing, so
        ``"../secrets.md"`` would resolve to ``procedural/../secrets.md`` —
        an object outside the procedural prefix in the shared bucket. Asserting
        the client was never called (rather than only that the return value is
        ``False``) is what pins the short-circuit.
        """
        calls: list[str] = []

        class _RecordingMinio(_FakeMinio):
            def stat_object(self, bucket: str, key: str) -> object:
                calls.append(f"stat:{key}")
                return super().stat_object(bucket, key)

            def remove_object(self, bucket: str, key: str) -> None:
                calls.append(f"remove:{key}")
                super().remove_object(bucket, key)

        client = _RecordingMinio({"procedural/SKILL-IAM.md": b"# IAM\n"})
        service = S3ProceduralService(
            client, bucket="memory-shared", prefix="procedural/"
        )

        assert await service.delete(user_context, "../secrets.md") is False
        assert await service.delete(user_context, "SKILL-IAM") is False

        # Decisive: the object store was never asked to do anything.
        assert calls == []


class TestMockMatchLoops:
    """The per-document scans must act on the named skill and nothing else."""

    async def test_write_updates_only_the_named_skill(self, user_context) -> None:
        """Overwriting one skill must leave its siblings byte-identical.

        The write path scans ``self._documents`` for a filename match and
        replaces in place. If the scan stopped at the first entry rather than
        continuing past non-matches, writing to the second or third skill
        would silently overwrite the first — a shared-content corruption that
        every user of the procedural layer would then read back.
        """
        service = MockProceduralService()
        service.add_document("iam body", skill="IAM", file="SKILL-IAM.md")
        service.add_document("arch body", skill="ARCH", file="SKILL-ARCH.md")
        service.add_document("cli body", skill="CLI", file="SKILL-CLI.md")

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
        service.add_document("iam body", skill="IAM", file="SKILL-IAM.md")
        service.add_document("arch body", skill="ARCH", file="SKILL-ARCH.md")

        assert await service.delete(user_context, "SKILL-NOPE.md") is False

        remaining = {d.metadata["file"] for d in await service.load(user_context)}
        assert remaining == {"SKILL-IAM.md", "SKILL-ARCH.md"}

    async def test_delete_removes_only_the_named_skill(self, user_context) -> None:
        """Deleting the second of three skills must leave the other two.

        Same off-by-one risk as the write scan, but destructive: the index
        used for ``pop`` has to be the index of the *matching* document.
        """
        service = MockProceduralService()
        service.add_document("iam body", skill="IAM", file="SKILL-IAM.md")
        service.add_document("arch body", skill="ARCH", file="SKILL-ARCH.md")
        service.add_document("cli body", skill="CLI", file="SKILL-CLI.md")

        assert await service.delete(user_context, "SKILL-ARCH.md") is True

        remaining = {d.metadata["file"] for d in await service.load(user_context)}
        assert remaining == {"SKILL-IAM.md", "SKILL-CLI.md"}
