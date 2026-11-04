"""LC-9 WP2b -- HTTP status mapping shared by the semantic write sites.

SECURITY (LC-9, cross-user private-row isolation; ADDENDUM-1 S1/S2, WP2a
review C3). Every semantic write -- ``POST/PUT/DELETE /memory/semantic``, the
``/memory/promote`` semantic target and the MCP ``write_*`` tools -- builds its
LOGICAL manifest key through :func:`audittrace.services.memory_keys.
semantic_logical_key` and then touches the manifest. Two failures of that path
are CLIENT-VISIBLE contract, not server faults:

* the key is longer than :data:`~audittrace.services.memory_keys.
  LOGICAL_KEY_MAX` -> **400** (a 219+ char key cannot be stored under an owner
  prefix; before LC-9 it surfaced as a 500 ``StringDataRightTruncation``);
* a concurrent same-item write won the ``UNIQUE (layer, key)`` race
  (:class:`~audittrace.services.memory_keys.ConcurrentManifestWriteError`) ->
  **409** with ``Retry-After: 1`` (nothing was written; a retry converges).

Only SQLSTATE ``23505`` is softened to a 409 (see
``services.memory_manifest._is_unique_violation``); the 034 CHECK (``23514``)
stays an ``IntegrityError``. A ``ManifestAuthorizationError`` is NOT mapped
here: each route already maps it to 403 and the MCP tools to an ``{"error"}``
result, and that mapping must stay visible at the call site.

Falsifiability: ``tests/test_lc9_wp2b_semantic_routes.py`` has one test per
site; removing this context manager from a site (or making the mapper return
500, or dropping ``Retry-After``) turns that site's 400/409 case RED.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from typing import Any

from fastapi import HTTPException

from audittrace.identity import UserContext
from audittrace.services.memory_keys import (
    ConcurrentManifestWriteError,
    LogicalKeyTooLongError,
)

logger = logging.getLogger(__name__)


def concurrent_write_409(exc: ConcurrentManifestWriteError) -> HTTPException:
    """The retryable 409 for a lost ``UNIQUE (layer, key)`` race."""
    return HTTPException(status_code=409, detail=f"{exc}", headers={"Retry-After": "1"})


@contextlib.contextmanager
def semantic_write_errors_as_http() -> Iterator[None]:
    """Map the two client-visible manifest-write failures to 400 / 409."""
    try:
        yield
    except LogicalKeyTooLongError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConcurrentManifestWriteError as exc:
        raise concurrent_write_409(exc) from exc


async def require_visible_row_or_404(
    manifest: Any, user: UserContext, key: str
) -> None:
    """404 BEFORE any content write when the caller can see no manifest row.

    ``PUT`` updates an existing item. Another user's private row is invisible
    to :meth:`resolve_read` (it is ``None``, exactly like an absent key), so a
    foreign or absent key is the same 404 and the Chroma upsert that follows
    never runs -- B's ``PUT`` can no longer overwrite A's vector before the
    manifest says "no row". The text matches the ``record_update`` ``LookupError``
    path it precedes."""
    if await manifest.resolve_read("semantic", key, user) is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "semantic doc has no manifest row: "
                f"no manifest row for layer='semantic' key={key!r}"
            ),
        )


async def drop_copied_forward_chunk(
    service: Any,
    user: UserContext,
    collection: str,
    document_id: str,
    copied_from: str | None,
) -> None:
    """Copy-forward step 3 (LC-9 WP3, ADDENDUM-1 S2): after the manifest has
    tombstoned the caller's own legacy row (*copied_from* names it), delete the
    caller's legacy ChromaDB chunk at the bare *document_id*.

    Runs AFTER the manifest transaction, so a failure here never undoes it: the
    legacy chunk is already hidden from recall by the tombstone (reader #5), and
    the owner-keyed chunk written in step 1 answers every read. The failure is
    logged and the write still succeeds; the owner check inside
    ``drop_legacy_documents`` means another user's chunk at that bare id is never
    deleted. A no-op when nothing was copied forward."""
    if not copied_from:
        return
    try:
        await service.drop_legacy_documents(user, collection, [document_id])
    except Exception:
        logger.warning(
            "copy-forward step 3 failed collection=%s document_id=%s; the legacy "
            "chunk stays hidden by its tombstone",
            collection,
            document_id,
            exc_info=True,
        )
