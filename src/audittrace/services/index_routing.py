"""Deterministic collection routing for the auto-index pipeline.

SPEC #387 Phase 1 (WU-1 + WU-4) — single source of truth for "given an
object key, which ChromaDB collection should it land in". Both the outbox
enqueue in ``ScanVerdictConsumer`` (WU-1, and ``IndexJanitor``'s re-drive)
and the manual ``POST /memory/index?file=`` route (WU-4, GAP-2 closure)
call :func:`collection_for_key` so the two paths can never independently
drift on the routing decision.

GAP-2 (SPEC #387): the default ``/memory/index`` collections
(``decisions``, ``skills``, ``semantic``) are ``.md``-only —
``ai_research_papers`` is the only collection whose indexer
(``_index_pdf_objects``) accepts PDFs. A caller that doesn't know this
routes a promoted PDF into a ``.md``-only collection and gets a silent
0-chunk no-op (bulk mode) or a 400/422 (single-file mode without an
explicit ``?collections=``). This module is the fix: routing by content
type, not by caller intent.
"""

from __future__ import annotations

# Closed-set: today only PDFs auto-route to the opt-in ai_research_papers
# collection (ADR-056) — the only indexer that accepts non-``.md`` content.
# Everything else falls back to `semantic`, the general `.md` collection fed
# by both the episodic and procedural `.md` indexers (`_index_md_objects`).
AI_RESEARCH_PAPERS_COLLECTION = "ai_research_papers"
DEFAULT_MD_COLLECTION = "semantic"
_PDF_SUFFIX = ".pdf"
_S3_SCHEME_PREFIX = "s3://"


def collection_for_key(key: str) -> str:
    """Return the deterministic ChromaDB collection name for *key*.

    PDF objects (``.pdf`` suffix, case-insensitive) route to
    ``ai_research_papers``; everything else routes to ``semantic``. Works
    on either a bucket-relative key (``episodic/papers/<id>/<file>.pdf``)
    or a full ``s3://bucket/...`` manifest URI — only the suffix matters.
    """
    return (
        AI_RESEARCH_PAPERS_COLLECTION
        if key.lower().endswith(_PDF_SUFFIX)
        else DEFAULT_MD_COLLECTION
    )


def bare_key_from_uri(uri: str) -> str:
    """Strip a ``s3://<bucket>/`` prefix, returning the bucket-relative key.

    ``ScanVerdictConsumer._apply_verdict`` re-points ``MemoryItem.key`` to
    the full ``s3://<bucket>/episodic/papers/<scan_id>/<filename>`` URI on
    a clean verdict, but ``minio_client.get_object(bucket, key)`` (and
    every ``_index_*`` indexer) expects the bucket-relative key. This is
    the single place that translates between the two forms — used by
    ``IndexJanitor`` when it re-derives an ``IndexRequestEnvelope`` from a
    stuck manifest row.

    Returns *uri* unchanged if it does not carry the ``s3://`` scheme
    (defensive; every manifest row this pipeline promotes always does).
    """
    if not uri.startswith(_S3_SCHEME_PREFIX):
        return uri
    without_scheme = uri[len(_S3_SCHEME_PREFIX) :]
    _bucket, _sep, rest = without_scheme.partition("/")
    return rest
