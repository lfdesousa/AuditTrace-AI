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
