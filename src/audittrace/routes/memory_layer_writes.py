"""The one write sequence of the file layers (episodic, procedural).

``POST /memory/<layer>`` and ``POST /memory/upload`` both store a text document
in the caller's private tier. They share this sequence so that every stored
object has the same pre-write authorization, the same manifest row (with the
stored size) and the same write audit, whichever route carried it.
"""

from __future__ import annotations

from typing import Any

from audittrace.identity import UserContext
from audittrace.routes.memory_write_audit import emit_write_audit, emit_write_audits
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
    audit (500, fail-closed) for a document the layer service just stored.

    The content write has ALREADY happened when the row is recorded, so both
    refusals leave the object stored. A lost race (409) is audited, because
    the write took effect. A row refused by the manifest (403: the pre-write
    authorization passed, then a concurrent change, e.g. a corpus row under the
    same name, made the row unauthorizable) is NOT audited as a write: the
    caller is told 403 and the stored object is what the next authorized write
    or the operator reconciles."""
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
        # The row records the storage write that landed, not the HTTP answer:
        # the caller gets 409; the row carries ``manifest: concurrent_write``.
        await emit_write_audit(
            user=user,
            op="write",
            layer=layer,
            key=filename,
            tier=tier,
            detail_extra={"manifest": "concurrent_write"},
        )
        raise concurrent_write_as_409(exc) from exc
    except ManifestAuthorizationError as exc:
        raise manifest_authorization_as_403(exc) from exc
    await emit_write_audits(
        user=user,
        layer=layer,
        filename=filename,
        copied_from=copied_from,
        tier=entry.tier,
    )
    return entry
