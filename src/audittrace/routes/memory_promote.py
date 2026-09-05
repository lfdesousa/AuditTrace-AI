"""``POST /memory/promote`` business logic — WU-4 of the Sovereign-Attach
EPIC ("keep this": promote a caller's ephemeral session document into a
durable memory layer).

Lives in a SIBLING module to ``routes/memory.py`` rather than inline in
that file: ``routes/memory.py`` is already >3000 LOC (its own docstring
records the PDF-pipeline split that happened for exactly this reason —
PYTHON-ENGINEERING skill §11, "module LOC > 2000 -> stop adding, new work
goes in a sibling module"). ``routes/memory.py`` still registers the
``@router.post("/promote")`` FastAPI route itself (the spec names that
file explicitly), but the route body is a two-line delegation to
:func:`promote_session_to_durable` here — mirrors the established
``routes/memory_pdf`` / ``routes/memory_upload`` split precedent.

The end-to-end posture (ratified spec
``2026-09-05-SPEC-wu4-promote-session-to-durable.md``):

    BFF (separate RFC 8693 exchange for ONLY memory:<target>:write)
      -> orchestrator POST /memory/promote
           -> gate: memory:<target_layer>:write REQUIRED (a
              memory:session:write-only token 403s HERE)
           -> SessionMemoryService.read_own (RLS/ownership -> 404 if not
              the caller's own doc)
           -> COPY content into target_layer via the existing durable
              write primitives (EpisodicService.write /
              ChromaSemanticService.upsert) — the SAME
              authorize_write() pre-write choke every other durable
              write entry point uses (SPEC security-memory-write-
              authorization-choke, 2026-08-30)
           -> stamp provenance from the TOKEN (never a caller-supplied
              field — feedback_never_trust_caller_metadata_for_security_fields)
           -> audit (user_id + session_id + trace_id -
              feedback_traceability_requirement)
           -> return the new durable key

COPY, not move: the session row is never touched here (no delete/GC —
that is WU-6's job). Tier is always ``"private"`` — promote has no
corpus-tier concept; ``authorize_write``'s own pre-write choke still
applies unconditionally, so promoting OVER an existing CORPUS-tier row
at the same durable key stays refused exactly as it is on every other
write entry point (no new hole opened here).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import HTTPException
from langchain_core.documents import Document

from audittrace.dependencies import (
    get_episodic_service,
    get_memory_manifest_service,
    get_semantic_service,
    get_session_memory_service,
)
from audittrace.identity import UserContext
from audittrace.services.memory_audit import emit_memory_audit_event
from audittrace.services.memory_manifest import (
    ManifestAuthorizationError,
    authorize_write,
)

logger = logging.getLogger(__name__)

# The allowed PROMOTE TARGETS — the durable set the ratified spec names
# (episodic/semantic). Deliberately excludes "session" (the source layer,
# never a valid destination), "procedural" (not named by the spec — the
# EPIC decision text is explicit: "episodic/semantic"), and
# "conversational" (not a content-write layer at all). Any value outside
# this frozenset — including an unknown string — is rejected 422, never
# silently coerced to the default.
_DURABLE_PROMOTE_LAYERS: frozenset[str] = frozenset({"episodic", "semantic"})
_DEFAULT_PROMOTE_TARGET_LAYER = "episodic"

# The default ChromaDB collection a ``target_layer="semantic"`` promote
# lands in when the caller doesn't name one explicitly. "semantic" is
# already one of the three ADR-062 §4 corpus-scoped collections
# (alongside "decisions"/"skills") — reusing its name for the default
# private-tier promote target avoids inventing a fourth collection name
# nothing else in the codebase knows about.
_DEFAULT_PROMOTE_COLLECTION = "semantic"


def _validate_target_layer(raw_target_layer: Any) -> str:
    """Resolve + validate the caller's requested promote destination.

    Falsifiable: neuter this to accept ``"session"``/``"conversational"``
    (or drop the frozenset check entirely) and
    ``test_target_layer_session_rejected_422`` /
    ``test_target_layer_conversational_rejected_422`` go RED.
    """
    resolved = (
        raw_target_layer
        if raw_target_layer is not None
        else _DEFAULT_PROMOTE_TARGET_LAYER
    )
    if not isinstance(resolved, str) or resolved not in _DURABLE_PROMOTE_LAYERS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"target_layer must be one of {sorted(_DURABLE_PROMOTE_LAYERS)} "
                f"(got {resolved!r}); session/conversational are not durable "
                "promote targets"
            ),
        )
    return resolved


def _require_durable_write_scope(user: UserContext, target_layer: str) -> None:
    """Raise 403 unless *user* holds ``memory:<target_layer>:write`` (or
    ``audittrace:admin``/admin).

    Mirrors ``routes.memory._require_layer_write``'s shape exactly, but
    takes a plain ``target_layer`` string rather than the ``MemoryLayer``
    enum (``target_layer`` here is always a member of
    :data:`_DURABLE_PROMOTE_LAYERS`, resolved by :func:`_validate_target_layer`
    before this is ever called — never re-derived from a request field
    after this point).

    THE frozen invariant this enforces: a ``memory:session:write``-only
    token — the ONLY scope WU-1/2/3 ever grant by default — MUST 403
    here. Only a SEPARATE RFC 8693 exchange for the durable scope (never
    re-using the session-scoped token) satisfies this gate. Falsifiable:
    neuter this to also accept ``memory:session:write`` and
    ``test_session_only_token_cannot_promote`` goes RED.
    """
    required = f"memory:{target_layer}:write"
    if user.is_admin or "audittrace:admin" in user.scopes or required in user.scopes:
        return
    raise HTTPException(
        status_code=403,
        detail=f"Required scope: {required} (or audittrace:admin)",
    )


def _semantic_key(collection: str, document_id: str) -> str:
    """Manifest lookup key for a semantic doc: ``<collection>/<doc_id>``.

    Duplicated (not imported) from ``routes.memory._semantic_key`` — that
    module already imports FROM this one at module-load time (the thin
    ``/promote`` route delegates here), so a module-level import back
    would deadlock the import graph; this is a one-line pure function,
    the same duplication precedent ``services/session_memory.py`` and
    ``services/episodic.py`` already each carry their own filename
    validator rather than share one.
    """
    return f"{collection}/{document_id}"


def _namespaced_semantic_document_id(user_id: str, filename: str) -> str:
    """Per-user-namespaced ChromaDB ``document_id`` for a semantic
    promote — ``<user_id>/<filename>``.

    **Why this exists (independent-review finding, pass 1 — cross-user
    hijack).** The semantic layer's private-tier physical ChromaDB
    collection is SHARED across every private writer (unlike episodic/
    procedural, whose private content lives under a per-user S3 object
    prefix); a bare ``document_id == filename`` let two users who
    independently promoted a same-named file silently overwrite the
    SAME row. Baking the TOKEN-derived ``user_id`` (never a caller-
    supplied field — the caller never gets to choose this value; it is
    always ``user.user_id`` from :func:`~audittrace.auth.require_user`)
    into the id itself makes that collision structurally impossible —
    two different ``user_id`` values always produce two different
    ``document_id`` values, regardless of how many users pick the exact
    same filename. Falsifiable: revert to the raw *filename* and
    ``tests/test_memory_promote_route.py::
    TestPromoteSemanticCrossUserIsolation`` goes RED.
    """
    return f"{user_id}/{filename}"


def _durable_episodic_filename(session_filename: str) -> str:
    """Derive an episodic-safe (``.md``) filename from a session filename
    that may carry any extension — session uploads aren't restricted to
    ``.md`` (see ``services/session_memory.py``'s module docstring), but
    ``EpisodicService.write`` rejects anything that isn't. Appends
    ``.md`` only when not already present, so promoting an
    already-``.md`` session note round-trips its exact name."""
    return (
        session_filename
        if session_filename.endswith(".md")
        else f"{session_filename}.md"
    )


def _provenance_header(
    *, session_key: str, promoted_at_ms: int, promoted_by: str
) -> str:
    """The provenance block stamped onto every promoted document's own
    content — ``promoted_from``/``promoted_at``/``promoted_by`` are
    TOKEN-DERIVED (``promoted_by``) or SERVER-COMPUTED (the other two),
    never a caller-supplied field
    (feedback_never_trust_caller_metadata_for_security_fields). Rendered
    as HTML comments so it survives both the episodic ``.md`` render path
    and a plain-text semantic read without corrupting either."""
    return (
        f"<!-- promoted_from: {session_key} -->\n"
        f"<!-- promoted_at_ms: {promoted_at_ms} -->\n"
        f"<!-- promoted_by: {promoted_by} -->\n"
    )


async def _emit_promote_audit(
    *,
    user: UserContext,
    layer: str,
    collection: str | None,
    key: str,
    detail_extra: dict[str, Any],
) -> None:
    """Fail-closed audit-row emission for the promote write — mirrors
    ``routes.memory._emit_write_audit``'s wrapper shape exactly (an
    audit-store failure becomes an explicit 500, never a silently
    unaudited 200 — feedback_traceability_requirement). Duplicated
    rather than imported for the same "avoid a circular import back into
    routes.memory" reason as :func:`_semantic_key` above."""
    try:
        await emit_memory_audit_event(
            user=user,
            op="write",
            layer=layer,
            collection=collection,
            key=key,
            detail_extra=detail_extra,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                f"memory audit-write failed — promote not confirmed audited: {exc}"
            ),
        ) from exc


def _raise_manifest_authorization_as_403(
    exc: ManifestAuthorizationError,
) -> HTTPException:
    """Mirrors ``routes.memory._raise_manifest_authorization_as_403`` —
    never let a manifest-authorization failure fall through to a generic
    500; it is always a 403 (fail-closed, per SPEC security-memory-
    manifest-tier-authz)."""
    return HTTPException(status_code=403, detail=str(exc))


async def _promote_to_episodic(
    user: UserContext, filename: str, stamped_content: str, title: str
) -> str:
    """Copy *stamped_content* into the caller's private-tier episodic
    layer, keyed by a ``.md``-safe filename derived from *filename*.
    Returns the durable key (the episodic filename). Raises
    ``HTTPException`` (400/403/502) on any failure — never a silent
    partial write."""
    durable_filename = _durable_episodic_filename(filename)
    manifest = get_memory_manifest_service()
    # SPEC security-memory-write-authorization-choke (2026-08-30) — the
    # PRE-WRITE choke, checked before any content lands. Always
    # requested_tier="private": promote has no corpus concept, so an
    # existing CORPUS-tier row at this key stays refused exactly as it
    # would on /memory/episodic's own POST.
    try:
        await authorize_write(
            manifest, user, "episodic", durable_filename, requested_tier="private"
        )
    except ManifestAuthorizationError as exc:
        raise _raise_manifest_authorization_as_403(exc) from exc
    service = get_episodic_service()
    try:
        await service.write(user, durable_filename, stamped_content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.error("promote-to-episodic write failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    # Lazy import: routes.memory._corpus_write_authorized decides whether
    # THIS caller may claim shared-write authorship on record_create —
    # same lazy-import discipline authorize_write() itself already uses
    # for the identical reason (see that function's docstring).
    from audittrace.routes.memory import _corpus_write_authorized  # noqa: PLC0415

    try:
        await manifest.record_create(
            layer="episodic",
            key=durable_filename,
            title=title,
            size_bytes=len(stamped_content.encode("utf-8")),
            user_id=user.user_id,
            tier="private",
            caller_can_write_shared=_corpus_write_authorized(user, None),
        )
    except ManifestAuthorizationError as exc:
        raise _raise_manifest_authorization_as_403(exc) from exc
    return durable_filename


async def _promote_to_semantic(
    user: UserContext,
    filename: str,
    stamped_content: str,
    title: str,
    collection: Any,
    extra_metadata: dict[str, Any],
) -> tuple[str, str]:
    """Copy *stamped_content* into the caller's private-tier semantic
    collection (default :data:`_DEFAULT_PROMOTE_COLLECTION`), keyed by a
    PER-USER-NAMESPACED ``document_id``. Returns ``(durable_key,
    collection)`` — the resolved collection name is returned explicitly
    (not re-derived by splitting ``durable_key``) because the
    per-user-namespaced ``document_id`` itself now contains a ``/``,
    so a naive ``durable_key.rsplit("/", 1)`` would silently mis-parse
    the collection name once the namespacing fix below landed.

    **Cross-user collision fix (independent-review finding, pass 1).**
    Unlike episodic/procedural — whose PRIVATE-tier content is isolated
    by a per-user S3 object prefix (``{private_bucket}/{jwt.sub}/
    episodic/...``, entirely separate from the manifest-row collision
    ADR-062 §B4 already documents as a follow-up) — the semantic layer's
    private-tier ChromaDB physical collection
    (``ChromaSemanticService._physical``) is ONE SHARED collection for
    every private writer; there is no per-user storage-path mechanism at
    all. A ``document_id`` equal to the raw session filename therefore
    let two users who independently promoted a same-named file
    (e.g. both promoting ``shared.md``) silently overwrite the SAME
    ChromaDB row — reproduced live against the real ``/memory/promote``
    + ``/memory/semantic`` endpoints. :func:`_namespaced_semantic_document_id`
    bakes the TOKEN-derived ``user.user_id`` (never a caller-supplied
    field — feedback_never_trust_caller_metadata_for_security_fields)
    into the ``document_id`` itself, mirroring the INTENT of episodic's
    per-user S3 prefix (a stable per-user segment baked into the storage
    key) even though the exact mechanism differs, since ChromaDB has no
    separate storage-path concept to isolate on. This does NOT touch the
    broader, pre-existing ``authorize_write`` corpus-collision primitive
    gap (ADR-062 §B4) — that stays the documented separate follow-up;
    this closes the specific hijack this promote path introduced.
    """
    if collection is None:
        collection = _DEFAULT_PROMOTE_COLLECTION
    if not isinstance(collection, str) or not collection:
        raise HTTPException(status_code=400, detail="collection must be a string")
    document_id = _namespaced_semantic_document_id(user.user_id, filename)
    manifest = get_memory_manifest_service()
    durable_key = _semantic_key(collection, document_id)
    try:
        await authorize_write(
            manifest, user, "semantic", durable_key, requested_tier="private"
        )
    except ManifestAuthorizationError as exc:
        raise _raise_manifest_authorization_as_403(exc) from exc
    service = get_semantic_service()
    try:
        await service.upsert(
            user,
            collection,
            document_id,
            stamped_content,
            extra_metadata,
            tier="private",
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    from audittrace.routes.memory import _corpus_write_authorized  # noqa: PLC0415

    try:
        await manifest.record_create(
            layer="semantic",
            key=durable_key,
            title=title,
            size_bytes=len(stamped_content.encode("utf-8")),
            user_id=user.user_id,
            tier="private",
            caller_can_write_shared=_corpus_write_authorized(user, collection),
        )
    except ManifestAuthorizationError as exc:
        raise _raise_manifest_authorization_as_403(exc) from exc
    return durable_key, collection


async def promote_session_to_durable(
    payload: dict[str, Any], user: UserContext
) -> dict[str, Any]:
    """Promote (COPY, never move) the caller's OWN ephemeral session
    document into a durable memory layer.

    ``payload`` shape: ``{"filename": <required str>, "target_layer":
    <optional str, default "episodic">, "collection": <optional str,
    only meaningful when target_layer=="semantic">}``.

    Steps (fail-closed at every stage — see the module docstring for the
    full posture):

    1. Validate ``target_layer`` (422 for session/conversational/unknown).
    2. Scope gate: ``memory:<target_layer>:write`` required (403).
    3. ``SessionMemoryService.read_own`` — 404 if the caller doesn't own
       (or never wrote) a session doc by that filename. Never leaks
       another user's document via a different status code.
    4. Copy the content into the durable layer (stamping provenance from
       the TOKEN, never the payload) via the existing durable write
       primitives.
    5. Emit an audit row (fail-closed).
    6. Return the new durable key.

    The session row is NEVER touched (no delete, no GC) — asserted by
    the caller's own follow-up ``read_own`` call in the test suite
    (copy-not-move).
    """
    filename = payload.get("filename")
    if not isinstance(filename, str) or not filename:
        raise HTTPException(status_code=400, detail="filename is required")

    target_layer = _validate_target_layer(payload.get("target_layer"))
    _require_durable_write_scope(user, target_layer)

    session_service = get_session_memory_service()
    doc: Document | None = await session_service.read_own(user, filename)
    if doc is None:
        raise HTTPException(status_code=404, detail="session document not found")

    # Token-derived, NEVER a caller-supplied field
    # (feedback_never_trust_caller_metadata_for_security_fields) — even
    # though nothing in this payload currently names a "promoted_by"
    # field, the value is computed from `user.user_id` unconditionally
    # so a future caller-supplied field can never silently override it.
    promoted_by = user.user_id
    promoted_at_ms = int(time.time() * 1000)
    session_key = f"{user.user_id}/session/{filename}"
    stamped_content = (
        _provenance_header(
            session_key=session_key,
            promoted_at_ms=promoted_at_ms,
            promoted_by=promoted_by,
        )
        + doc.page_content
    )
    title = payload.get("title")
    if not isinstance(title, str) or not title:
        title = filename

    if target_layer == "episodic":
        durable_key = await _promote_to_episodic(user, filename, stamped_content, title)
        collection: str | None = None
    else:
        collection_value = payload.get("collection")
        durable_key, collection = await _promote_to_semantic(
            user,
            filename,
            stamped_content,
            title,
            collection_value,
            {
                "promoted_from": session_key,
                "promoted_at_ms": promoted_at_ms,
                "promoted_by": promoted_by,
            },
        )

    await _emit_promote_audit(
        user=user,
        layer=target_layer,
        collection=collection,
        key=durable_key,
        detail_extra={
            "tier": "private",
            "promoted_from": session_key,
            "promoted_at_ms": promoted_at_ms,
            "promoted_by": promoted_by,
        },
    )

    return {
        "status": "promoted",
        "target_layer": target_layer,
        "key": durable_key,
        "promoted_from": session_key,
        "promoted_at_ms": promoted_at_ms,
        "promoted_by": promoted_by,
    }
