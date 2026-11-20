"""The chunks an earlier, longer fold of a file left past the end of a new one.

A fold writes chunks ``0..n-1``. If an earlier fold of the same file wrote more,
its chunks at index ``n`` and above stay in ChromaDB and in the manifest: the
old text stays readable and recallable. The fold finds that tail and authorizes
its removal BEFORE it writes anything, and removes it after its own chunks land.

A fold that yields NO chunk (a blank document) has ``n = 0``: the "tail" is every
chunk of the file, and it is removed the same way (gate r3 S14). Without that,
the previous fold would stay readable, ``verified``, while the stored object is
blank.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
from typing import Any

from fastapi import HTTPException

from audittrace.identity import UserContext
from audittrace.routes.memory_whole_document import (
    CHUNK_READ_WINDOW,
    document_chunk_ids,
)
from audittrace.routes.memory_write_support import semantic_key
from audittrace.services.memory_audit import emit_memory_audit_event
from audittrace.services.memory_keys import private_physical_id
from audittrace.services.memory_manifest import authorize_write

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True, slots=True)
class StaleTail:
    """The rows an earlier fold of *filename* left at index >= the new chunk
    count: ``(logical id, ChromaDB id)`` each, and the work to remove them."""

    col_name: str
    filename: str
    rows: tuple[tuple[str, str], ...]

    @property
    def keys(self) -> list[str]:
        """The manifest keys of the tail, one per logical chunk."""
        return sorted({semantic_key(self.col_name, lid) for lid, _ in self.rows})

    async def authorize(
        self, manifest_service: Any | None, user: UserContext, tier: str
    ) -> None:
        """The pre-write choke for the tail, exactly as for the chunks the fold
        writes. Raises ``ManifestAuthorizationError`` before anything is
        written."""
        if manifest_service is None:
            return
        for key in self.keys:
            await authorize_write(
                manifest_service, user, "semantic", key, requested_tier=tier
            )

    async def drop(
        self,
        collection: Any,
        manifest_service: Any | None,
        user: UserContext,
        tier: str,
        *,
        caller_can_write_shared: bool,
    ) -> int:
        """Delete the tail rows, tombstone their manifest rows, and write ONE
        audit event for the fold, recording the *tier* the fold ran under (the
        same one its own write audit records). Returns how many chunks were
        removed."""
        if not self.rows:
            return 0
        if manifest_service is not None:
            for key in self.keys:
                with contextlib.suppress(LookupError):
                    await manifest_service.record_delete(
                        "semantic",
                        key,
                        user.user_id,
                        caller_can_write_shared=caller_can_write_shared,
                    )
        await collection.delete(ids=[pid for _, pid in self.rows])
        removed = len({lid for lid, _ in self.rows})
        try:
            await emit_memory_audit_event(
                user=user,
                op="delete",
                layer="semantic",
                collection=self.col_name,
                key=self.filename,
                tier=tier,
                detail_extra={"reason": "refold_stale_tail", "removed": removed},
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=(
                    f"memory audit-write failed — mutation not confirmed audited: {exc}"
                ),
            ) from exc
        logger.info("fold removed %d stale tail chunks of %s", removed, self.filename)
        return removed


async def find_stale_tail(
    collection: Any,
    user: UserContext,
    tier: str,
    col_name: str,
    filename: str,
    *,
    kept: int,
) -> StaleTail:
    """The :class:`StaleTail` of *filename*: every chunk at index >= *kept* that
    this caller's fold may remove: its own new-form rows, and (private tier) its
    own rows still stored at the bare pre-LC-9 id. The tail is contiguous from
    *kept*, so the walk ends at the first empty window."""
    stale: list[tuple[str, str]] = []
    start = kept
    while True:
        logical = document_chunk_ids(
            col_name, filename, start, start + CHUNK_READ_WINDOW
        )
        found: list[tuple[str, str]] = []
        if tier == "corpus":
            present = set((await collection.get(ids=logical, include=[]))["ids"])
            found = [(lid, lid) for lid in logical if lid in present]
        else:
            owned = [private_physical_id(user, lid) for lid in logical]
            present = set((await collection.get(ids=owned, include=[]))["ids"])
            found = [
                (lid, pid)
                for lid, pid in zip(logical, owned, strict=True)
                if pid in present
            ]
            legacy = await collection.get(ids=logical, include=["metadatas"])
            for lid, meta in zip(legacy["ids"], legacy["metadatas"], strict=True):
                if (meta or {}).get("user_id") == user.user_id and (meta or {}).get(
                    "tier"
                ) == "private":
                    found.append((lid, lid))
        if not found:
            return StaleTail(col_name, filename, tuple(stale))
        stale.extend(found)
        start += CHUNK_READ_WINDOW
