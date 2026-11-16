"""The one write sequence of the file layers (episodic, procedural).

``POST /memory/<layer>`` and ``POST /memory/upload`` both store a text document
in the caller's private tier. They share this sequence so that every stored
object has the same pre-write authorization, the same manifest row (with the
stored size) and the same write audit, whichever route carried it.
"""

from __future__ import annotations

from typing import Any

from audittrace.identity import UserContext
from audittrace.routes.memory_write_support import (
    concurrent_write_as_409,
    corpus_write_authorized,
    legacy_copy_forward_source,
    manifest_authorization_as_403,
)
from audittrace.services.memory_manifest import (
    ConcurrentManifestWriteError,
    ManifestAuthorizationError,
    ManifestEntry,
    authorize_write,
)


async def authorize_layer_write(
    manifest: Any, user: UserContext, layer: str, filename: str, tier: str
) -> None:
    """The pre-write choke, as an HTTP 403. Call it before the content write."""
    try:
        await authorize_write(manifest, user, layer, filename, requested_tier=tier)
    except ManifestAuthorizationError as exc:
        raise manifest_authorization_as_403(exc) from exc


async def record_layer_write(
    manifest: Any,
    user: UserContext,
    layer: str,
    filename: str,
    content: str,
    *,
    title: str | None,
    tier: str,
) -> ManifestEntry:
    """Manifest row (409 on a lost race, 403 on a refused one) and the write
    audit (500, fail-closed) for a document the layer service just stored."""
    # TODO(MEMORY-SERVER-SILENT-TRUNCATION B5): the two audit emitters are the
    # only helpers still imported back from ``routes.memory``, lazily (a cycle:
    # that module imports this one). ``_emit_write_audit`` reads the name
    # ``emit_memory_audit_event`` from ``routes.memory``'s namespace, and five
    # existing tests patch it THERE (test_lc9_wp2a_routes, test_lc9_wp2b_pg,
    # test_lc9_wp2b_semantic_routes, test_lc9_wp3_chroma_ids); moving the
    # emitters means re-pointing those patches, a change to tests that this
    # item has no other reason to touch.
    from audittrace.routes.memory import (  # noqa: PLC0415 - lazy: routes.memory imports this module
        _emit_write_audit,
        _emit_write_audits,
    )

    copied_from = await legacy_copy_forward_source(manifest, user, layer, filename)
    try:
        entry: ManifestEntry = await manifest.record_create(
            layer=layer,
            key=filename,
            title=title,
            size_bytes=len(content.encode("utf-8")),
            user_id=user.user_id,
            tier=tier,
            caller_can_write_shared=corpus_write_authorized(user, None),
        )
    except ConcurrentManifestWriteError as exc:
        # The content write already happened: it is audited even though the
        # manifest row went to the concurrent writer.
        await _emit_write_audit(
            user=user,
            op="write",
            layer=layer,
            key=filename,
            detail_extra={"manifest": "concurrent_write"},
        )
        raise concurrent_write_as_409(exc) from exc
    except ManifestAuthorizationError as exc:
        raise manifest_authorization_as_403(exc) from exc
    await _emit_write_audits(
        user=user, layer=layer, filename=filename, copied_from=copied_from
    )
    return entry
