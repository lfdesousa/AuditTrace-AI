"""Helpers of the memory write routes that other route modules share.

They lived in ``routes.memory`` and were imported back from inside functions by
``memory_layer_writes`` and ``memory_fold_tail`` (a cycle). Here they are leaf
functions with public names; ``routes.memory`` keeps its underscore names as
aliases, so every existing caller and test is unchanged.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from audittrace.identity import UserContext
from audittrace.routes.memory_semantic_writes import concurrent_write_409
from audittrace.services.memory_chunk_ids import STAMP_FOLD, STAMP_TOTAL
from audittrace.services.memory_manifest import (
    ConcurrentManifestWriteError,
    ManifestAuthorizationError,
    ManifestEntry,
)


def semantic_key(collection: str, document_id: str) -> str:
    """Manifest lookup key for a semantic doc. ``<collection>/<doc_id>``."""
    return f"{collection}/{document_id}"


def has_corpus_scope(user: UserContext, collection: str, action: str) -> bool:
    """``True`` iff *user* holds ``memory:corpus:<collection>:<action>``
    (or an admin bypass). Boolean twin of ``_require_corpus_scope`` for
    call sites that need to silently FILTER (list endpoints) rather than
    reject the whole request (single-doc read/write)."""
    required = f"memory:corpus:{collection}:{action}"
    return user.is_admin or "audittrace:admin" in user.scopes or required in user.scopes


def corpus_read_cleared(user: UserContext, collection: str | None) -> bool:
    """``True`` iff the semantic list's corpus gate can admit a corpus-tier row
    for *user*: an admin bypass, the granular read scope of *collection*, or,
    for a list over every collection, the read scope of at least one."""
    if collection is not None:
        return has_corpus_scope(user, collection, "read")
    return (
        user.is_admin
        or "audittrace:admin" in user.scopes
        or any(
            scope.startswith("memory:corpus:") and scope.endswith(":read")
            for scope in user.scopes
        )
    )


def corpus_write_authorized(user: UserContext, collection: str | None) -> bool:
    """``True`` iff *user* may overwrite/demote an EXISTING **corpus**-tier
    manifest row — the ``caller_can_write_shared`` authorization
    ``MemoryManifestService.record_create``/``record_update`` require to
    touch such a row (SPEC security-memory-manifest-tier-authz,
    2026-08-30).

    ``collection`` is one of the three ADR-062 §4 corpus-scoped ChromaDB
    collections (``decisions``/``skills``/``semantic``) for ``/memory/
    semantic`` writes — delegates to :func:`has_corpus_scope` for the exact
    per-collection grant (or admin). ``None`` is passed for the episodic/
    procedural layers, which have NO ``memory:corpus:<layer>:write`` scope
    declared at all (the closed scope set is exactly the three collections
    above); for those layers only an admin (``audittrace:admin`` or
    ``is_admin``) can ever satisfy this — matching that no ordinary caller
    can hold a shared-write grant that does not exist.
    """
    if collection is None:
        return user.is_admin or "audittrace:admin" in user.scopes
    return has_corpus_scope(user, collection, "write")


async def authorized_update_tier(
    manifest: Any, user: UserContext, layer: str, key: str
) -> str:
    """The tier of the row a ``PUT`` resolves to, which is the tier it writes
    (``private`` when the caller can see no row), so the content store, the
    manifest row and the audit row name the same one. Call it AFTER
    ``authorize_write``. Refused BEFORE any content write when that tier is
    ``corpus`` and either the caller
    lacks the shared-write authority for it (403; same predicate and text as
    ``authorize_write``) or the corpus row is soft-deleted (409; the text
    ``record_update`` gives after the write). Both checks are on the ONE row
    this call resolved and on the VALUE the content write is given, so no read
    between them and the write can separate them."""
    # ``authorize_write`` decided on ``resolve_write``; this is ``resolve_read``
    # (the caller's own row first, then a corpus row). The two agree on the
    # tier in every state one request can see alone; they differ when a row is
    # created BETWEEN them, and on a tombstoned own legacy row (``new`` vs
    # ``legacy_own``, both private). So the tier resolved here is never trusted
    # for a corpus write without the two checks below.
    entry: ManifestEntry | None = await manifest.resolve_read(layer, key, user)
    if entry is None or entry.tier != "corpus":
        return entry.tier if entry is not None else "private"
    collection = key.split("/", 1)[0] if layer == "semantic" else None
    if not corpus_write_authorized(user, collection):
        raise manifest_authorization_as_403(
            ManifestAuthorizationError(
                "caller lacks shared-write authorization to write over an "
                f"existing corpus-tier target layer={layer!r} key={key!r}"
            )
        )
    if entry.deleted_at_ms is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"manifest row for layer={layer!r} key={key!r} is soft-deleted; "
                "use record_create to revive"
            ),
        )
    return "corpus"


# Keys only a fold writes into a chunk's STORED metadata: the stamp of
# ``fold_stamp`` and the ``source`` every fold (stamped or older, .md or PDF)
# records. Read from the store, never from a request.
FOLD_KEYS = (STAMP_TOTAL, STAMP_FOLD, "source")


def corpus_update_refusal(
    document_id: str,
    shared_row: dict[str, Any] | None,
    corpus_row: dict[str, Any] | None,
) -> str | None:
    """Why a ``PUT`` may not write through to corpus document *document_id*,
    or ``None`` when it may. *shared_row* is the corpus-tier row a fold stores
    at the bare id in the shared-with-private collection (every reader
    resolves it first); *corpus_row* the row in the corpus collection, the one
    place a corpus ``upsert`` writes. Write-through is right only when the row
    readers get IS the row the write replaces, and no fold wrote it."""
    for row in (shared_row, corpus_row):
        if row is not None and any(key in row for key in FOLD_KEYS):
            return (
                f"{document_id} is one chunk of the folded corpus document "
                f"{row.get('source')}. Not supported: updating one chunk of a "
                "folded corpus document; re-fold the source."
            )
    if shared_row is not None:
        return (
            f"{document_id} is a corpus document stored where a PUT cannot "
            "replace it. Not supported: updating it in place."
        )
    if corpus_row is None:
        return (
            f"{document_id} is a corpus document with no content a PUT can "
            "replace. Not supported: updating it in place; re-create it."
        )
    return None


async def refuse_update_of_corpus_fold_chunk(
    service: Any, collection: str, document_id: str, tier: str
) -> None:
    """409 BEFORE any write (ADDENDUM-2). No ``except``: an unreadable store
    is a 5xx, never a write-through."""
    if tier != "corpus":
        return
    shared_row, corpus_row = await service.stored_corpus_rows(collection, document_id)
    refusal = corpus_update_refusal(document_id, shared_row, corpus_row)
    if refusal is not None:
        raise HTTPException(status_code=409, detail=refusal)


def refuse_corpus_update_of_file_layer(layer: str, filename: str, tier: str) -> None:
    """409 BEFORE any content write: the episodic and procedural services have
    no corpus write, so a ``PUT`` over a corpus-tier file stored a private copy
    that shadowed it while the corpus manifest row was marked modified."""
    if tier == "corpus":
        raise HTTPException(
            status_code=409,
            detail=(
                f"{filename} is a corpus-tier {layer} document. Not supported: "
                f"updating a corpus document of the {layer} layer."
            ),
        )


def manifest_authorization_as_403(exc: ManifestAuthorizationError) -> HTTPException:
    """Map :class:`ManifestAuthorizationError` to the route-layer HTTP 403
    every other authorization failure in the memory routes uses — never let it
    fall through to the generic 500 handler (SPEC security-memory-
    manifest-tier-authz, 2026-08-30, invariant #4: fail-closed, RAISE, not
    a silent overwrite AND not an opaque 500)."""
    return HTTPException(status_code=403, detail=str(exc))


def concurrent_write_as_409(exc: ConcurrentManifestWriteError) -> HTTPException:
    """A lost ``UNIQUE (layer, key)`` race is a retryable 409, never a 500
    (LC-9 WP1 review A2); the body lives in ``memory_semantic_writes``."""
    return concurrent_write_409(exc)


async def legacy_copy_forward_source(
    manifest: Any, user: UserContext, layer: str, filename: str
) -> str | None:
    """The stored key of the caller's OWN live legacy bare-key private row
    that the coming create/update of *filename* will copy forward, else
    ``None`` (LC-9 WP2a, ADDENDUM-1 S2: the audit trail records the
    copy-forward). Decided through ``resolve_read``: the caller's owner key
    wins; the legacy row counts only when it is the caller's own, private and
    live. Another user's row is invisible to ``resolve_read``, so it can
    never be named here."""
    prior: ManifestEntry | None = await manifest.resolve_read(layer, filename, user)
    if (
        prior is not None
        and prior.tier == "private"
        and prior.deleted_at_ms is None
        and prior.created_by_user_id == user.user_id
        and prior.storage_key == filename
    ):
        return prior.storage_key
    return None
