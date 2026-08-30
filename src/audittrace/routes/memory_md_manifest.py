"""Manifest write for the ``.md`` (decisions/skills/semantic) index path.

ADR-059 WU-1c (2026-08-08) — the durable fix for the live v1.20.3
fleet-recall discovery-cap bug (SPEC-wu1c-fleet-recall-discovery-cap).
Mirrors ``routes/memory_pdf/manifest.py::_flush_pdf_manifest`` (ADR-056's
manifest-write seam) but for the ``.md`` fold path
(``routes/memory.py::_index_md_objects``), placed in its own sibling
module per PYTHON-ENGINEERING §11 (``routes/memory.py`` is already past
the 2000-LOC "stop adding, new work goes in a sibling module" threshold —
the same reasoning that put the PDF pipeline in ``routes/memory_pdf/``).

**The bug this closes.** Before this fix, ``_index_md_objects`` wrote
ChromaDB rows only — no manifest row — so every folded decision/skill fell
to the capped (``_SEMANTIC_DISCOVERY_LIMIT = 200``), non-recency-ordered
``col.get()`` discovery scan (``_discover_rows_for_caller`` /
``_merge_semantic_with_chroma`` in ``routes/memory.py``). ChromaDB doc ids
are content hashes, not timestamps, so ``.get()`` order does not correlate
with recency. On the single-tenant laptop the fleet identity owns close to
every row in ``decisions_v2``, so even the WU-1 owner-scoped
``where={"user_id": ...}`` scan hit the same 200-row cap — a dominant
owner's own freshest fold could sort outside the capped window and never
surface via ``GET /memory/semantic`` (what
``scripts/deploy/memory.py::recall_deploy_lessons`` reads).

**The fix.** Thread a manifest row per folded chunk here. ``list_semantic``
reads manifest rows via ``MemoryManifestService.list_for_layer`` — a plain,
UNCAPPED SQL scan ordered by ``created_at_ms`` — so a caller's newest fold
is always inside the listing regardless of how many rows the physical
ChromaDB collection or the caller owns. The raw ``.get()`` discovery scan
in ``_merge_semantic_with_chroma`` is unchanged and continues to run, but
is now effectively a fallback for legacy pre-fix rows that were never
manifest-tracked: any row a manifest entry already covers is skipped there
via the existing ``known_keys`` dedup.

Best-effort for ordinary (e.g. Postgres connectivity) failures: a warning
is logged and the index call does not fail — the ChromaDB chunk has
already landed, and audit-trail resiliency (surface it, don't 500) takes
precedence over strict manifest consistency. **NOT** best-effort for
:class:`~audittrace.services.memory_manifest.ManifestAuthorizationError`
(SPEC security-memory-write-authorization-choke, 2026-08-30) — see this
module's ``_flush_md_manifest`` docstring for why that one specific
failure must propagate instead of being swallowed.
"""

from __future__ import annotations

import logging
from typing import Any

from audittrace.services.memory_manifest import ManifestAuthorizationError

logger = logging.getLogger(__name__)


async def _flush_md_manifest(
    manifest_service: Any | None,
    *,
    keys: list[str],
    filename: str,
    sizes_bytes: list[int],
    user_id: str,
    tier: str,
    caller_can_write_shared: bool = False,
) -> None:
    """Best-effort manifest row per folded ``.md`` chunk (ADR-059 WU-1c).

    One manifest row per ``(collection, chunk_id)`` key in *keys* — the
    SAME granularity ``create_semantic``/``read_semantic`` already use for
    the manifest ``key`` shape (``<collection>/<document_id>``, built by
    ``routes/memory.py::_semantic_key``), and the same granularity
    ``_merge_semantic_with_chroma``'s discovery loop keys a ChromaDB row
    on (one row per chunk). Tracking every chunk — not just a file's first
    — keeps every ChromaDB row a manifest entry can cover, so a
    multi-chunk file's later chunks don't silently fall back to the capped
    discovery scan while its first chunk is durably listed.

    Skips silently when *manifest_service* is ``None`` (defensive only —
    every real call site passes one; kept for symmetry with
    ``_flush_pdf_manifest`` so a future caller that omits it fails soft,
    not hard). *keys* and *sizes_bytes* must be the same length — one
    entry per chunk, in the same order (both derived from the same
    ``chunks`` list at the call site).

    *caller_can_write_shared* — forwarded to ``record_create`` as its
    fail-closed (default ``False``) shared-write authorization (the
    manifest-layer DEFENSE-IN-DEPTH guard).

    **``ManifestAuthorizationError`` is NOT swallowed** (SPEC security-
    memory-write-authorization-choke, 2026-08-30 — SUPERSEDES the prior
    behavior, where it was). Three independent reviews proved that a
    silent WARNING + implicit-200 here is itself the vulnerability: the
    ChromaDB chunk this manifest row is supposed to audit had ALREADY
    landed by the time this function runs (see the call site,
    ``routes/memory.py::_index_md_objects``), so on the primary
    ``authorize_write`` pre-write choke this specific error should never
    actually reach here on an unauthorized request — but if it ever does
    (a missed call site, a future refactor), it MUST surface loudly as a
    403, not a swallowed warning masking a poisoned write. Every OTHER
    exception (e.g. a genuine Postgres outage) is still caught and
    logged — the ChromaDB write already landed and is not rolled back
    either way, so a non-authorization manifest failure staying
    best-effort is unchanged.
    """
    if manifest_service is None:
        return
    for key, size_bytes in zip(keys, sizes_bytes, strict=True):
        try:
            await manifest_service.record_create(
                layer="semantic",
                key=key,
                title=filename,
                size_bytes=size_bytes,
                user_id=user_id,
                tier=tier,
                caller_can_write_shared=caller_can_write_shared,
            )
        except ManifestAuthorizationError:
            raise
        except Exception as exc:
            logger.warning(
                "Failed to write .md manifest row for semantic/%s: %s",
                key,
                exc,
            )
