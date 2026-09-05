"""Tests for ``POST /memory/promote`` — WU-4 of the Sovereign-Attach
EPIC ("keep this": promote a caller's session document into a durable
memory layer).

Non-vacuous guards, each with its own test class (ratified spec
2026-09-05-SPEC-wu4-promote-session-to-durable.md):

* ``TestPromoteScopeGate`` — a ``memory:session:write``-only token 403s;
  the durable ``memory:<target_layer>:write`` scope is required. Neuter
  ``_require_durable_write_scope`` and
  ``test_session_only_token_cannot_promote`` goes RED.
* ``TestPromoteTargetLayerValidation`` — ``target_layer`` is constrained
  to the durable set (episodic/semantic); session/conversational/unknown
  are rejected 422.
* ``TestPromoteOwnership`` — a caller cannot promote another user's (or a
  nonexistent) session doc: 404, never a different status code that
  would disclose existence.
* ``TestPromoteProvenance`` — ``promoted_by`` is ALWAYS the token's
  ``sub``, never a caller-supplied field; the durable content itself
  carries the provenance stamp.
* ``TestPromoteCopyNotMove`` — the session doc still exists after a
  successful promote.
* ``TestPromoteSemanticTarget`` — the semantic durable path (default +
  custom collection).
* ``TestPromoteAuditFailsClosed`` — an audit-emit failure is a 500, never
  a silently-unaudited 200 (mirrors ``TestMemoryAuditWriteFailsClosed``
  in ``tests/test_memory_routes.py``).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ── auth helpers ─────────────────────────────────────────────────────────────


class _Auth:
    """Small helper bundling the three patches + their return values so
    every test doesn't repeat the same six lines."""

    def __init__(self, sub: str, scope: str) -> None:
        self.sub = sub
        self.scope = scope

    def __enter__(self):
        self._patches = [
            patch("audittrace.auth.get_settings"),
            patch("audittrace.auth._get_jwks_keys"),
            patch("audittrace.auth._decode_jwt_with_allowed_issuers"),
        ]
        mocks = [p.__enter__() for p in self._patches]
        mock_settings, mock_jwks, mock_decode = mocks
        mock_settings.return_value = MagicMock(auth_enabled=True, auth_required=True)
        mock_jwks.return_value = ["fake-key"]
        mock_decode.return_value = {"sub": self.sub, "scope": self.scope}
        return self

    def __exit__(self, *exc: Any) -> None:
        for p in reversed(self._patches):
            p.__exit__(*exc)


def _upload_session_doc(
    client: TestClient, *, sub: str, filename: str, content: bytes = b"scratch note"
) -> None:
    """Seed a session-layer document via the real (WU-1) upload route, as
    *sub*, so promote tests exercise the real read_own ownership path
    rather than reaching into service internals directly."""
    with (
        _Auth(sub, "memory:session:write"),
        patch(
            "audittrace.routes.memory._get_minio_client",
            return_value=MagicMock(),
        ),
    ):
        response = client.post(
            "/memory/upload",
            params={"layer": "session"},
            files={"file": (filename, content, "text/plain")},
            headers={"Authorization": "Bearer session-token"},
        )
    assert response.status_code == 200, response.text


def _promote(
    client: TestClient,
    *,
    sub: str,
    scope: str,
    payload: dict[str, Any],
) -> Any:
    with _Auth(sub, scope):
        return client.post(
            "/memory/promote",
            json=payload,
            headers={"Authorization": "Bearer promote-token"},
        )


# ── scope gate ───────────────────────────────────────────────────────────────


class TestPromoteScopeGate:
    """Deliverable: durable scope REQUIRED, session scope INSUFFICIENT."""

    def test_session_only_token_cannot_promote(self, client: TestClient) -> None:
        """A ``memory:session:write``-only token — everything WU-1/2/3
        grant by default — MUST 403 at the promote choke. Neutering
        ``_require_durable_write_scope`` to also accept the session
        scope turns this RED."""
        _upload_session_doc(client, sub="alice", filename="note.txt")
        response = _promote(
            client,
            sub="alice",
            scope="memory:session:write",
            payload={"filename": "note.txt"},
        )
        assert response.status_code == 403
        assert "memory:episodic:write" in response.json()["detail"]

    def test_no_scope_token_cannot_promote(self, client: TestClient) -> None:
        response = _promote(
            client,
            sub="alice",
            scope="audittrace:query",
            payload={"filename": "note.txt"},
        )
        assert response.status_code == 403

    def test_episodic_scope_can_promote_to_episodic(self, client: TestClient) -> None:
        """The positive case: holding the DURABLE scope the target
        requires succeeds."""
        _upload_session_doc(client, sub="alice", filename="note.txt")
        response = _promote(
            client,
            sub="alice",
            scope="memory:episodic:write",
            payload={"filename": "note.txt"},
        )
        assert response.status_code == 200
        assert response.json()["target_layer"] == "episodic"

    def test_semantic_scope_cannot_promote_to_episodic(
        self, client: TestClient
    ) -> None:
        """Cross-layer denial mirrors ``_require_layer_write``'s own
        discipline: holding the WRONG durable scope is still refused."""
        _upload_session_doc(client, sub="alice", filename="note.txt")
        response = _promote(
            client,
            sub="alice",
            scope="memory:semantic:write",
            payload={"filename": "note.txt", "target_layer": "episodic"},
        )
        assert response.status_code == 403
        assert "memory:episodic:write" in response.json()["detail"]

    def test_admin_scope_bypasses_gate(self, client: TestClient) -> None:
        _upload_session_doc(client, sub="alice", filename="note.txt")
        response = _promote(
            client,
            sub="alice",
            scope="audittrace:admin",
            payload={"filename": "note.txt"},
        )
        assert response.status_code == 200


# ── target_layer validation ──────────────────────────────────────────────────


class TestPromoteTargetLayerValidation:
    def test_target_layer_session_rejected_422(self, client: TestClient) -> None:
        _upload_session_doc(client, sub="alice", filename="note.txt")
        response = _promote(
            client,
            sub="alice",
            scope="memory:session:write memory:episodic:write memory:semantic:write",
            payload={"filename": "note.txt", "target_layer": "session"},
        )
        assert response.status_code == 422

    def test_target_layer_conversational_rejected_422(self, client: TestClient) -> None:
        response = _promote(
            client,
            sub="alice",
            scope="audittrace:admin",
            payload={"filename": "note.txt", "target_layer": "conversational"},
        )
        assert response.status_code == 422

    def test_target_layer_unknown_value_rejected_422(self, client: TestClient) -> None:
        response = _promote(
            client,
            sub="alice",
            scope="audittrace:admin",
            payload={"filename": "note.txt", "target_layer": "not-a-real-layer"},
        )
        assert response.status_code == 422

    def test_target_layer_procedural_rejected_422(self, client: TestClient) -> None:
        """The durable set is exactly episodic/semantic — NOT procedural,
        even though procedural is itself a durable, S3-backed layer for
        other routes. The EPIC decision text names only episodic/semantic."""
        response = _promote(
            client,
            sub="alice",
            scope="audittrace:admin",
            payload={"filename": "note.txt", "target_layer": "procedural"},
        )
        assert response.status_code == 422

    def test_default_target_layer_is_episodic(self, client: TestClient) -> None:
        """Omitting ``target_layer`` entirely defaults to episodic."""
        _upload_session_doc(client, sub="alice", filename="default-target.txt")
        response = _promote(
            client,
            sub="alice",
            scope="memory:episodic:write",
            payload={"filename": "default-target.txt"},
        )
        assert response.status_code == 200
        assert response.json()["target_layer"] == "episodic"


# ── ownership (RLS-equivalent) ───────────────────────────────────────────────


class TestPromoteOwnership:
    """Deliverable: a caller cannot promote another user's session doc —
    read_own enforces this (feedback_unit_tests_miss_rls: the session
    layer's isolation is a service-layer explicit filter, proven the
    same way WU-1's own acceptance test (d) proves it)."""

    def test_promote_nonexistent_filename_404(self, client: TestClient) -> None:
        response = _promote(
            client,
            sub="alice",
            scope="memory:episodic:write",
            payload={"filename": "never-uploaded.txt"},
        )
        assert response.status_code == 404

    def test_cross_user_promote_404(self, client: TestClient) -> None:
        """Alice's session doc is invisible to Bob's promote attempt —
        neutering the ownership/isolation check (e.g. dropping
        ``read_own``'s user_id filter) makes this go RED (200 instead
        of 404)."""
        _upload_session_doc(client, sub="alice", filename="alices-note.txt")
        response = _promote(
            client,
            sub="bob",
            scope="memory:episodic:write",
            payload={"filename": "alices-note.txt"},
        )
        assert response.status_code == 404

    def test_owner_can_promote_own_doc(self, client: TestClient) -> None:
        """Positive control for the two 404 tests above: the SAME
        filename, promoted by its OWNER, succeeds."""
        _upload_session_doc(client, sub="alice", filename="alices-note.txt")
        response = _promote(
            client,
            sub="alice",
            scope="memory:episodic:write",
            payload={"filename": "alices-note.txt"},
        )
        assert response.status_code == 200


# ── provenance ────────────────────────────────────────────────────────────────


class TestPromoteProvenance:
    """Deliverable: ``promoted_by`` is TOKEN-derived, never a caller-
    supplied field (feedback_never_trust_caller_metadata_for_security_fields)."""

    def test_provenance_from_token_not_caller_field(self, client: TestClient) -> None:
        _upload_session_doc(client, sub="alice", filename="note.txt")
        response = _promote(
            client,
            sub="alice",
            scope="memory:episodic:write",
            payload={
                "filename": "note.txt",
                # An attacker-controlled attempt to forge attribution —
                # neither of these body fields is ever read by the
                # implementation for identity purposes.
                "promoted_by": "mallory",
                "user_id": "mallory",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["promoted_by"] == "alice"
        assert body["promoted_by"] != "mallory"

    def test_provenance_stamped_on_durable_content(self, client: TestClient) -> None:
        """The durable document's own content carries the provenance
        block — read it back through the (mock) episodic service."""
        from audittrace.dependencies import get_episodic_service
        from audittrace.identity import sentinel_user_context

        _upload_session_doc(client, sub="alice", filename="stamped.txt")
        response = _promote(
            client,
            sub="alice",
            scope="memory:episodic:write",
            payload={"filename": "stamped.txt"},
        )
        assert response.status_code == 200
        key = response.json()["key"]
        assert key == "stamped.txt.md"

        async def _read() -> Any:
            ctx = sentinel_user_context()
            ctx = replace(ctx, user_id="alice")
            return await get_episodic_service().read(ctx, key)

        doc = asyncio.run(_read())
        assert doc is not None
        assert "promoted_from: alice/session/stamped.txt" in doc.page_content
        assert "promoted_by: alice" in doc.page_content
        assert "scratch note" in doc.page_content


# ── copy, not move ────────────────────────────────────────────────────────────


class TestPromoteCopyNotMove:
    def test_session_doc_still_exists_after_promote(self, client: TestClient) -> None:
        """Neutering promote to also delete/GC the session row would
        make this go RED (``read_own`` would return ``None``)."""
        from audittrace.dependencies import get_session_memory_service
        from audittrace.identity import sentinel_user_context

        _upload_session_doc(client, sub="alice", filename="keepalive.txt")
        response = _promote(
            client,
            sub="alice",
            scope="memory:episodic:write",
            payload={"filename": "keepalive.txt"},
        )
        assert response.status_code == 200

        async def _read_own() -> Any:
            ctx = sentinel_user_context()
            ctx = replace(ctx, user_id="alice")
            return await get_session_memory_service().read_own(ctx, "keepalive.txt")

        doc = asyncio.run(_read_own())
        assert doc is not None
        assert doc.page_content == "scratch note"


# ── semantic durable target ──────────────────────────────────────────────────


class TestPromoteSemanticTarget:
    def test_promote_to_semantic_default_collection(self, client: TestClient) -> None:
        _upload_session_doc(client, sub="alice", filename="sem-note.txt")
        response = _promote(
            client,
            sub="alice",
            scope="memory:semantic:write",
            payload={"filename": "sem-note.txt", "target_layer": "semantic"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["target_layer"] == "semantic"
        assert body["key"] == "semantic/sem-note.txt"

    def test_promote_to_semantic_custom_collection(self, client: TestClient) -> None:
        _upload_session_doc(client, sub="alice", filename="sem-custom.txt")
        response = _promote(
            client,
            sub="alice",
            scope="memory:semantic:write",
            payload={
                "filename": "sem-custom.txt",
                "target_layer": "semantic",
                "collection": "decisions",
            },
        )
        assert response.status_code == 200
        assert response.json()["key"] == "decisions/sem-custom.txt"

    def test_semantic_document_readable_after_promote(self, client: TestClient) -> None:
        from audittrace.dependencies import get_semantic_service
        from audittrace.identity import sentinel_user_context

        _upload_session_doc(client, sub="alice", filename="sem-read.txt")
        response = _promote(
            client,
            sub="alice",
            scope="memory:semantic:write",
            payload={"filename": "sem-read.txt", "target_layer": "semantic"},
        )
        assert response.status_code == 200

        async def _read() -> Any:
            ctx = sentinel_user_context()
            ctx = replace(ctx, user_id="alice")
            return await get_semantic_service().get_document(
                ctx, "semantic", "sem-read.txt"
            )

        doc = asyncio.run(_read())
        assert doc is not None
        assert "promoted_from: alice/session/sem-read.txt" in doc.page_content


# ── missing filename ──────────────────────────────────────────────────────────


class TestPromoteMissingFilename:
    def test_missing_filename_400(self, client: TestClient) -> None:
        response = _promote(
            client,
            sub="alice",
            scope="audittrace:admin",
            payload={},
        )
        assert response.status_code == 400


# ── audit fails closed ───────────────────────────────────────────────────────


class TestPromoteAuditFailsClosed:
    """Mirrors ``TestMemoryAuditWriteFailsClosed`` in
    ``tests/test_memory_routes.py`` — an audit-emit failure must fail the
    request closed, never silently succeed unaudited."""

    def test_promote_returns_500_when_audit_emit_fails(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from audittrace.routes import memory_promote as mp

        async def _boom(**_kwargs: Any) -> None:
            raise RuntimeError("audit store unavailable")

        _upload_session_doc(client, sub="alice", filename="audit-fc.txt")
        monkeypatch.setattr(mp, "emit_memory_audit_event", _boom)
        response = _promote(
            client,
            sub="alice",
            scope="memory:episodic:write",
            payload={"filename": "audit-fc.txt"},
        )
        assert response.status_code == 500


# ── manifest pre-write authorization choke ────────────────────────────────────


class TestPromoteManifestAuthorizationChoke:
    """The ``authorize_write`` pre-write choke (SPEC security-memory-
    write-authorization-choke, 2026-08-30) applies to promote exactly
    like every other durable write entry point: promoting OVER an
    existing CORPUS-tier row without shared-write authorization is
    refused 403 — no new hole opened for this WU."""

    @staticmethod
    def _seed_corpus_episodic_row(filename: str) -> None:
        from audittrace.dependencies import get_memory_manifest_service

        asyncio.run(
            get_memory_manifest_service().record_create(
                "episodic",
                filename,
                "Shared ADR",
                10,
                "curator",
                tier="corpus",
            )
        )

    @staticmethod
    def _seed_corpus_semantic_row(collection: str, document_id: str) -> None:
        from audittrace.dependencies import get_memory_manifest_service

        key = f"{collection}/{document_id}"
        asyncio.run(
            get_memory_manifest_service().record_create(
                "semantic",
                key,
                "Shared doc",
                10,
                "curator",
                tier="corpus",
            )
        )

    def test_promote_over_existing_corpus_episodic_row_denied(
        self, client: TestClient
    ) -> None:
        self._seed_corpus_episodic_row("collide.txt.md")
        _upload_session_doc(client, sub="alice", filename="collide.txt")
        response = _promote(
            client,
            sub="alice",
            scope="memory:episodic:write",
            payload={"filename": "collide.txt"},
        )
        assert response.status_code == 403

    def test_promote_over_existing_corpus_semantic_row_denied(
        self, client: TestClient
    ) -> None:
        self._seed_corpus_semantic_row("semantic", "collide-sem.txt")
        _upload_session_doc(client, sub="alice", filename="collide-sem.txt")
        response = _promote(
            client,
            sub="alice",
            scope="memory:semantic:write",
            payload={"filename": "collide-sem.txt", "target_layer": "semantic"},
        )
        assert response.status_code == 403

    def test_admin_can_promote_over_existing_corpus_row(
        self, client: TestClient
    ) -> None:
        """Positive control: an admin (shared-write authorized) CAN
        overwrite the corpus row — proves the 403s above are genuinely
        about authorization, not a blanket "any existing row" refusal."""
        self._seed_corpus_episodic_row("collide-admin.txt.md")
        _upload_session_doc(client, sub="alice", filename="collide-admin.txt")
        response = _promote(
            client,
            sub="alice",
            scope="audittrace:admin",
            payload={"filename": "collide-admin.txt"},
        )
        assert response.status_code == 200


# ── durable write-primitive failures ─────────────────────────────────────────


class TestPromoteWritePrimitiveFailures:
    """The episodic/semantic write-primitive failure branches — mirrors
    ``create_episodic``/``create_semantic``'s own 400/502 mapping."""

    def test_episodic_write_value_error_maps_to_400(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from audittrace.services.episodic import MockEpisodicService

        async def _raise_value_error(*_a: Any, **_kw: Any) -> Any:
            raise ValueError("invalid filename")

        _upload_session_doc(client, sub="alice", filename="badwrite.txt")
        monkeypatch.setattr(MockEpisodicService, "write", _raise_value_error)
        response = _promote(
            client,
            sub="alice",
            scope="memory:episodic:write",
            payload={"filename": "badwrite.txt"},
        )
        assert response.status_code == 400

    def test_episodic_write_runtime_error_maps_to_502(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from audittrace.services.episodic import MockEpisodicService

        async def _raise_runtime_error(*_a: Any, **_kw: Any) -> Any:
            raise RuntimeError("backend unavailable")

        _upload_session_doc(client, sub="alice", filename="badwrite2.txt")
        monkeypatch.setattr(MockEpisodicService, "write", _raise_runtime_error)
        response = _promote(
            client,
            sub="alice",
            scope="memory:episodic:write",
            payload={"filename": "badwrite2.txt"},
        )
        assert response.status_code == 502

    def test_semantic_upsert_failure_maps_to_502(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from audittrace.services.semantic import MockSemanticService

        async def _raise(*_a: Any, **_kw: Any) -> Any:
            raise RuntimeError("chroma unavailable")

        _upload_session_doc(client, sub="alice", filename="badsem.txt")
        monkeypatch.setattr(MockSemanticService, "upsert", _raise)
        response = _promote(
            client,
            sub="alice",
            scope="memory:semantic:write",
            payload={"filename": "badsem.txt", "target_layer": "semantic"},
        )
        assert response.status_code == 502

    def test_semantic_collection_must_be_a_string(self, client: TestClient) -> None:
        _upload_session_doc(client, sub="alice", filename="badcol.txt")
        response = _promote(
            client,
            sub="alice",
            scope="memory:semantic:write",
            payload={
                "filename": "badcol.txt",
                "target_layer": "semantic",
                "collection": 123,
            },
        )
        assert response.status_code == 400


# ── optional title field ──────────────────────────────────────────────────────


class TestPromoteTitleField:
    def test_explicit_title_is_used_on_the_manifest_row(
        self, client: TestClient
    ) -> None:
        from audittrace.dependencies import get_memory_manifest_service

        _upload_session_doc(client, sub="alice", filename="titled.txt")
        response = _promote(
            client,
            sub="alice",
            scope="memory:episodic:write",
            payload={"filename": "titled.txt", "title": "My Custom Title"},
        )
        assert response.status_code == 200
        key = response.json()["key"]

        entry = asyncio.run(get_memory_manifest_service().get("episodic", key))
        assert entry is not None
        assert entry.title == "My Custom Title"


# ── record_create's own (second) authorization check ─────────────────────────


class TestPromoteRecordCreateAuthorizationNotSwallowed:
    """SPEC security-memory-write-authorization-choke (2026-08-30) — the
    manifest's OWN ``record_create`` guard is a SECOND, independent
    check (belt-and-suspenders alongside the pre-write
    ``authorize_write`` choke). Mirrors
    ``TestManifestWriteChokeSupersedes*`` in ``tests/test_memory_routes.py``:
    ``ManifestAuthorizationError`` raised from ``record_create`` itself
    must propagate as 403, never be swallowed."""

    def test_episodic_record_create_authorization_error_is_403(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from audittrace.services.memory_manifest import (
            ManifestAuthorizationError,
            MockMemoryManifestService,
        )

        async def _deny(*_a: Any, **_kw: Any) -> Any:
            raise ManifestAuthorizationError("denied by record_create")

        _upload_session_doc(client, sub="alice", filename="rc-deny.txt")
        monkeypatch.setattr(MockMemoryManifestService, "record_create", _deny)
        response = _promote(
            client,
            sub="alice",
            scope="memory:episodic:write",
            payload={"filename": "rc-deny.txt"},
        )
        assert response.status_code == 403

    def test_semantic_record_create_authorization_error_is_403(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from audittrace.services.memory_manifest import (
            ManifestAuthorizationError,
            MockMemoryManifestService,
        )

        async def _deny(*_a: Any, **_kw: Any) -> Any:
            raise ManifestAuthorizationError("denied by record_create")

        _upload_session_doc(client, sub="alice", filename="rc-deny-sem.txt")
        monkeypatch.setattr(MockMemoryManifestService, "record_create", _deny)
        response = _promote(
            client,
            sub="alice",
            scope="memory:semantic:write",
            payload={"filename": "rc-deny-sem.txt", "target_layer": "semantic"},
        )
        assert response.status_code == 403
