"""Memory ingestion routes — single-gateway upload and indexing (ADR-027).

These endpoints make the memory server the sole gateway for writing to
MinIO and ChromaDB.  No external caller talks to those backends directly.

``POST /memory/upload`` stores a file in MinIO under the episodic or
procedural prefix.  ``POST /memory/index`` reads all documents from MinIO,
chunks them, and pushes the chunks into ChromaDB collections.

Memory-layer CRUD backoffice (migration 009 + this PR):

* ``POST   /memory/<layer>``                                  (create)
* ``GET    /memory/<layer>``                                  (list)
* ``GET    /memory/<layer>/{file}``                           (read)
* ``PUT    /memory/<layer>/{file}``                           (update)
* ``DELETE /memory/<layer>/{file}``                           (soft-delete)

For ``layer`` ∈ {episodic, procedural} the path key is the filename
(e.g. ``ADR-007.md``). For ``layer`` = semantic the path key is
``<collection>/<document_id>``.

All write operations require the per-layer ``memory:<layer>:write``
Keycloak scope. Reads require ``memory:<layer>:read`` (already
existed for the recall_* tools). Authorship + sub-second timestamps
are tracked in the ``memory_items`` Postgres manifest table; the
content itself lives in S3 (episodic/procedural) or ChromaDB
(semantic).
"""

import asyncio
import contextlib
import hashlib
import logging
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Any, Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    Security,
    UploadFile,
)
from sqlalchemy import func, select

# ADR-006 — direct ``minio.Minio`` import removed; routes/memory.py
# now consumes the ABC-shaped provider from the DI container via
# :func:`_get_minio_client`.
from audittrace.auth import require_user, validate_jwt
from audittrace.config import get_settings
from audittrace.db.models import InteractionRecord as InteractionRow
from audittrace.db.models import SessionRecord as SessionRow
from audittrace.db.rls import current_user_id, set_rls_user_id
from audittrace.dependencies import (
    get_chromadb,
    get_episodic_service,
    get_memory_manifest_service,
    get_postgres_factory,
    get_procedural_service,
    get_semantic_service,
)
from audittrace.identity import UserContext
from audittrace.models import (
    ConversationalDetailResponse,
    ConversationalListResponse,
)

# ─── PDF helpers re-exported from memory_pdf/ sub-package (2026-05-09) ─
# Pre-refactor memory.py was 3263 LOC; the §11 (PYTHON-ENGINEERING SKILL)
# discipline says modules > 2000 LOC stop accepting new code. The PDF
# concerns moved to ``audittrace.routes.memory_pdf``; this file re-exports
# the public seam so existing imports
# (``from audittrace.routes.memory import _PDF_WARNING_CODES``) keep
# working unchanged.
from audittrace.routes import memory_pdf as _pdf  # noqa: E402
from audittrace.routes import memory_scan as _scan  # noqa: E402
from audittrace.services.embedder import embed_via_nomic
from audittrace.services.episodic import EpisodicService
from audittrace.services.layer_listing import list_layer_objects
from audittrace.services.memory_audit import (
    MemoryOp,
    emit_memory_audit_event,
    schedule_read_audit,
)
from audittrace.services.memory_manifest import ManifestEntry
from audittrace.services.pagination import sort_and_paginate as _core_sort_and_paginate
from audittrace.services.procedural import ProceduralService

_PDF_WARNING_CODES = _pdf._PDF_WARNING_CODES
_SIGNATURE_STATUS_CODES = _pdf._SIGNATURE_STATUS_CODES
_SCAN_STATUS_CODES = _scan._SCAN_STATUS_CODES
_EVENT_CLASS_VALUES = _scan._EVENT_CLASS_VALUES
_PDFA_PART_RE = _pdf._PDFA_PART_RE
_PDFA_CONFORMANCE_RE = _pdf._PDFA_CONFORMANCE_RE
_PEM_CERT_RE = _pdf._PEM_CERT_RE
_pem_bundle_to_cert_list = _pdf._pem_bundle_to_cert_list
_get_validation_context = _pdf._get_validation_context
_invalidate_validation_context = _pdf._invalidate_validation_context
_pdf_signature_status = _pdf._pdf_signature_status
_extract_pdf_metadata = _pdf._extract_pdf_metadata
_parse_pdf_date = _pdf._parse_pdf_date
_trim_pdf_metadata_string = _pdf._trim_pdf_metadata_string
_extract_pdfa_conformance = _pdf._extract_pdfa_conformance
_summarize_ltv = _pdf._summarize_ltv
_build_toc_index = _pdf._build_toc_index
_classify_pdf_extraction_error = _pdf._classify_pdf_extraction_error
_flush_pdf_manifest = _pdf._flush_pdf_manifest
_pdf_is_encrypted = _pdf._pdf_is_encrypted
_quarantine_pdf_attachments = _pdf._quarantine_pdf_attachments
_acroform_text_for_page = _pdf._acroform_text_for_page
_ocr_render_page = _pdf._ocr_render_page
_page_bbox = _pdf._page_bbox
_redaction_rects = _pdf._redaction_rects
_rects_intersect = _pdf._rects_intersect
_text_clipped_around_redactions = _pdf._text_clipped_around_redactions
_index_pdf_objects = _pdf._index_pdf_objects
_PDF_ANNOT_REDACT = _pdf._PDF_ANNOT_REDACT

logger = logging.getLogger(__name__)

router = APIRouter()

# Chunking parameters — must match scripts/index-chromadb.py
CHUNK_SIZE = 1500
CHUNK_OVERLAP = 200

# Per-file `collection.add` batch. Keeps the embedding model's working
# set bounded — important for the PDF path where a single 50MB paper
# can yield thousands of chunks. Chosen to match the legacy-mode batch
# in scripts/index-chromadb.py.
_INDEX_BATCH_SIZE = 50

# Allowed `?collections=` values. `ai_research_papers` is an opt-in
# target (PDFs only) and is intentionally NOT in the default list:
# routine /memory/index calls stay fast and the embedder doesn't have
# to chew through 50+ MB of papers each time. Operators rebuild it
# explicitly via `?collections=ai_research_papers` when papers change.
_KNOWN_COLLECTIONS = frozenset(
    {"decisions", "skills", "semantic", "ai_research_papers"}
)
_DEFAULT_COLLECTIONS = ("decisions", "skills", "semantic")


# Singleton ONNX embedder lives in ``audittrace.services.embedder`` —
# imported above — so routes + services share one model instance.
# See the embedder module docstring for the leak-fix rationale; see
# the PYTHON-ENGINEERING skill §2 for the singleton-with-lock pattern.


class MemoryLayer(StrEnum):
    """Valid memory layer targets for file upload."""

    episodic = "episodic"
    procedural = "procedural"


def _chunk_text(
    text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP
) -> list[str]:
    """Split text into overlapping chunks."""
    if len(text) <= chunk_size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk)
        start = end - overlap
    return chunks


def _doc_id(collection: str, source: str, chunk_idx: int) -> str:
    """Generate a deterministic document ID."""
    raw = f"{collection}:{source}:{chunk_idx}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _get_minio_client() -> Any:
    """Return the singleton object-storage provider from the DI container.

    Post-ADR-006: returns the ``S3ObjectStorageProvider`` (MinIO OR AWS
    S3 backend, transparently — selected by
    ``AUDITTRACE_OBJECT_STORAGE_BACKEND``). Wrapped in
    ``QuarantineDenyingObjectStorageClient`` so any quarantine/* GET
    fires the defense-in-depth denial.

    Keeps the legacy function name so existing call sites in this file
    (the routes layer) don't need rewiring within this PR.

    Falls back to a freshly-constructed provider if the DI container
    has not yet been initialised (e.g. test isolation) — the call site
    behaviour is preserved.
    """
    from audittrace.dependencies import _create_object_storage_provider, container

    cached = container._instances.get("object_storage")
    if cached is not None:
        return cached
    return _create_object_storage_provider(get_settings())


def _list_objects_from_minio(
    client: Any, bucket: str, prefix: str
) -> list[dict[str, str]]:
    """List all objects under *prefix* in *bucket*.

    Returns a list of ``{"key": ..., "filename": ...}`` dicts.

    Delegates to the shared
    :func:`~audittrace.services.layer_listing.list_layer_objects` helper so
    the ``/memory/index`` prefix walk and the list/``load()`` path enumerate
    through one code path and cannot diverge again (backlog #15, Defect A).
    The shared helper also drops directory-marker / ``None`` keys.

    The ABC's ``list_objects`` paginates AND recurses transparently
    (MinIO backend uses ``recursive=True``; boto3 paginator walks every
    page). Before ADR-006 this helper also passed ``recursive=True``
    explicitly to the minio-py client; that kwarg no longer exists on
    the ABC because the contract guarantees full-subtree walking.
    """
    return [
        {"key": obj["key"], "filename": obj["filename"]}
        for obj in list_layer_objects(client, bucket, prefix)
    ]


def _invalidate_layer_list_cache(layer: str) -> None:
    """Invalidate the shared listing cache for *layer* after a mutation.

    Backlog #15, R3: because the cache is shared (Redis) one invalidation is
    seen by all replicas, so this is the PRIMARY correctness mechanism that
    makes a just-written object appear in the list fleet-wide. Best-effort —
    the service's ``invalidate_cache`` already fails open, and the TTL is the
    safety net.
    """
    try:
        if layer == MemoryLayer.episodic.value:
            get_episodic_service().invalidate_cache()
        elif layer == MemoryLayer.procedural.value:
            get_procedural_service().invalidate_cache()
    except Exception as exc:  # never fail the request on a cache-invalidate miss
        logger.warning("layer-cache invalidate failed for %s (ignored): %s", layer, exc)


# ── POST /memory/upload ─────────────────────────────────────────────────────


def _require_layer_write(user: UserContext, layer: MemoryLayer) -> None:
    """Raise 403 unless *user* may write to *layer*.

    Per-layer write scope (``memory:<layer>:write``) is the natural
    grant for end-user UI uploads; ``audittrace:admin`` continues to
    bypass per-layer gating so operator bulk operations don't break.
    Empty-handed callers see the layer name in the error so they know
    which scope to request.
    """
    required = f"memory:{layer.value}:write"
    if user.is_admin or "audittrace:admin" in user.scopes or required in user.scopes:
        return
    raise HTTPException(
        status_code=403,
        detail=f"Required scope: {required} (or audittrace:admin)",
    )


def _require_admin(user: UserContext, action: str) -> None:
    """Raise 403 unless *user* has the operator-level scope.

    Bulk-rebuild and other destructive whole-collection operations
    keep an admin gate even after the per-layer redesign — they
    cross user boundaries by definition.
    """
    if user.is_admin or "audittrace:admin" in user.scopes:
        return
    raise HTTPException(
        status_code=403,
        detail=f"Required scope: audittrace:admin ({action})",
    )


# ── ADR-062 Phase B (WU-B5) — corpus-write promote gate ─────────────────────
#
# The granular ``memory:corpus:<collection>:{read,write}`` scopes are a
# CLOSED, drift-guarded set (``tests/test_chart_drift_guards.py::
# TestCorpusScopeGovernance``) declared for exactly the three ChromaDB
# recall collections (``decisions``/``skills``/``semantic`` — ADR-062 §4,
# WU-A2/A3). They do NOT exist for the episodic/procedural S3 layers —
# adding new entries to that frozenset is explicitly out of scope for this
# PR (it would fail the drift guard). Consequently the promote-to-corpus
# mechanism this section wires is meaningful ONLY for ``/memory/semantic``;
# episodic/procedural writes stay private-only (see
# ``_reject_corpus_promotion_for`` below).


def _has_corpus_scope(user: UserContext, collection: str, action: str) -> bool:
    """``True`` iff *user* holds ``memory:corpus:<collection>:<action>``
    (or an admin bypass). Boolean twin of ``_require_corpus_scope`` for
    call sites that need to silently FILTER (list endpoints) rather than
    reject the whole request (single-doc read/write)."""
    required = f"memory:corpus:{collection}:{action}"
    return user.is_admin or "audittrace:admin" in user.scopes or required in user.scopes


def _require_corpus_scope(user: UserContext, collection: str, action: str) -> None:
    """Raise 403 unless *user* holds ``memory:corpus:<collection>:<action>``.

    Mirrors ``_require_layer_write``'s shape (admin bypass +
    ``audittrace:admin`` bypass + exact-scope match). ``action`` is
    ``"read"`` or ``"write"``. Falsifiable: a token without the scope
    (and without admin) is refused; a token WITH the scope (or admin) is
    let through — see ``TestSemanticCorpusPromoteGate`` /
    ``TestSemanticCorpusReadGate`` in ``tests/test_memory_routes.py``.
    """
    if _has_corpus_scope(user, collection, action):
        return
    required = f"memory:corpus:{collection}:{action}"
    raise HTTPException(
        status_code=403,
        detail=f"Required scope: {required} (or audittrace:admin)",
    )


def _resolve_requested_tier(payload: dict[str, Any] | None, promote: str | None) -> str:
    """Resolve the caller's requested write tier from an optional JSON
    body ``"tier"`` field and/or an optional ``?promote=`` query param
    (ADR-062 Phase B, WU-B5 — "explicit tier=corpus / ?promote=corpus").

    Returns ``"private"`` (the D2 default — no explicit request, or an
    explicit request for ``"private"``) or ``"corpus"`` (either signal
    asking for it). Raises 400 on a garbage value or a body/query
    conflict (e.g. ``{"tier": "private"}`` with ``?promote=corpus``) so a
    caller never silently gets a different tier than the one they asked
    for.
    """
    body_tier = payload.get("tier") if payload is not None else None
    if body_tier is not None and promote is not None and body_tier != promote:
        raise HTTPException(
            status_code=400,
            detail=(
                f"conflicting tier request: body tier={body_tier!r} vs "
                f"?promote={promote!r}"
            ),
        )
    requested = body_tier if body_tier is not None else promote
    if requested is None or requested == "private":
        return "private"
    if requested == "corpus":
        return "corpus"
    raise HTTPException(
        status_code=400,
        detail=f"invalid tier {requested!r}; expected 'private' or 'corpus'",
    )


def _manifest_visible(entry: ManifestEntry, user: UserContext) -> bool:
    """ADR-062 Phase B (WU-B4) — owner-or-corpus predicate for a SINGLE
    manifest entry, used by the ``read_*`` routes to decide whether the
    manifest metadata block is safe to return alongside a resolved
    read. Same shape as ``MemoryManifestService.list_for_layer``'s
    per-row filter and ``ChromaSemanticService._tier_authorized``.

    Defense-in-depth against the pre-existing ``(layer, key)``
    global-uniqueness manifest model (migration 009 — "operator-global,
    not per-user"): a private-tier filename collision across two
    different callers shares ONE manifest row, so without this check a
    caller whose OWN content-read already resolved correctly (via the
    private-tier-first S3/Chroma lookup) could still see a stranger's
    ``created_by_user_id`` in the ``manifest`` block. This does not fix
    that collision (out of WU-B4/B5 scope — flagged as a known
    follow-up), it just stops the row's authorship metadata leaking
    when it isn't the caller's own."""
    if user.is_admin:
        return True
    return entry.created_by_user_id == user.user_id or entry.tier == "corpus"


_SEMANTIC_METADATA_SECURITY_KEYS = frozenset({"tier", "user_id"})


def _sanitize_semantic_metadata(metadata: Any) -> Any:
    """ADR-062 Phase B (WU-B5 review fix, 2026-08-04) — strip
    caller-supplied ``tier``/``user_id`` keys from a semantic-document
    ``metadata`` payload before it reaches the service layer.

    ``tier`` and ``user_id`` are TOKEN-DERIVED / route-authorized
    values (ADR-027 §1), never caller-supplied — see
    ``ChromaSemanticService.upsert``'s docstring for the exploit this
    closes: a writer holding only ``memory:semantic:write`` could
    smuggle ``metadata={"tier": "corpus"}`` past the top-level promote
    gate (``_resolve_requested_tier`` only ever inspected the top-level
    body/query signal, never nested ``metadata``), and the forged tag
    was then trusted downstream by ``_tier_authorized`` on every
    subsequent read. This strip is belt-and-suspenders on top of the
    service layer's own unconditional stamp — it protects any
    ``SemanticService`` implementation (including a future/alternate
    one) that hasn't been hardened the same way, and applies in BOTH
    ``create_semantic`` (POST) and ``update_semantic`` (PUT) — a scope
    gate on one write path is worthless if the sibling path reaches the
    same sink unguarded.

    Non-dict input (the existing 400 validation elsewhere handles a
    malformed ``metadata``) passes through unchanged."""
    if not isinstance(metadata, dict):
        return metadata
    return {
        k: v for k, v in metadata.items() if k not in _SEMANTIC_METADATA_SECURITY_KEYS
    }


def _reject_corpus_promotion_for(layer: str, tier: str) -> None:
    """400 (not 403) if *tier* asks for corpus on a layer that has no
    corpus-write scope declared (episodic/procedural — see the module
    note above). This is a "not supported", not a "not authorized"
    response: no scope, held by anyone, could ever satisfy this request,
    so a 403 would misleadingly suggest a missing grant would fix it."""
    if tier == "corpus":
        raise HTTPException(
            status_code=400,
            detail=(
                f"corpus promotion is not supported for the {layer!r} layer; "
                "only /memory/semantic collections support corpus promotion "
                "(ADR-062 Phase B, WU-B5)"
            ),
        )


async def _emit_write_audit(
    *,
    user: UserContext,
    op: MemoryOp,
    layer: str,
    collection: str | None = None,
    key: str | None = None,
    detail_extra: dict[str, Any] | None = None,
) -> None:
    """Fail-closed wrapper around :func:`emit_memory_audit_event` for every
    WRITE/DELETE call site (ADR-062 §5, WU-A4).

    See ``services/memory_audit.py``'s module docstring for the full
    read=fail-open / write=fail-closed rationale. This wrapper converts an
    audit-store failure into an explicit ``HTTPException(500)`` rather than
    letting a bare exception fall through to the app-wide catch-all
    handler — the caller gets a precise, on-brand error (matching every
    other 5xx in this file) instead of a generic unhandled-exception
    envelope, and the failure is deterministically visible to
    ``TestClient`` (Starlette's ``ServerErrorMiddleware`` always re-raises
    after an unhandled exception, which ``TestClient`` in turn re-raises
    into the caller's test — an explicit ``HTTPException`` is the
    established pattern this file already uses everywhere else a service
    call can fail, e.g. ``_require_admin``'s 403, the 502s below)."""
    try:
        await emit_memory_audit_event(
            user=user,
            op=op,
            layer=layer,
            collection=collection,
            key=key,
            detail_extra=detail_extra,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                f"memory audit-write failed — mutation not confirmed audited: {exc}"
            ),
        ) from exc


async def _write_layer_private(
    layer: MemoryLayer, user: UserContext, filename: str, content: str
) -> None:
    """Dispatch to the episodic/procedural service's PRIVATE-tier write
    (ADR-062 Phase B, WU-B5), normalizing its exceptions to the same
    400/502 shape every other CRUD route in this file uses.

    Shared by ``upload_memory_file``'s legacy non-PDF branch so a plain
    /memory/upload gets WU-B2's private-tier routing + cache
    invalidation + filename validation for free, instead of hand-rolling
    a raw S3 ``put_object`` that used to land unconditionally in the
    shared/corpus bucket regardless of who uploaded (the leak this WU
    closes — see ``upload_memory_file``'s docstring)."""
    service: EpisodicService | ProceduralService = (
        get_episodic_service()
        if layer == MemoryLayer.episodic
        else get_procedural_service()
    )
    try:
        await service.write(user, filename, content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.error("Object storage write failed: %s", exc)
        raise HTTPException(
            status_code=502, detail="Object storage write failed"
        ) from exc


@router.post("/upload")
async def upload_memory_file(
    # ``Request[Any]`` would satisfy pre-commit mypy 1.8 (which can't
    # see starlette's generic default), but FastAPI's Pydantic field
    # introspection rejects it at route registration. Bare ``Request``
    # is what every other route in this codebase uses; we silence the
    # pre-commit-only error class explicitly.
    request: Request,  # type: ignore[type-arg, unused-ignore]
    response: Response,
    file: UploadFile = File(...),
    layer: MemoryLayer = Query(...),
    filename: str | None = Query(None),
    promote: str | None = Query(
        None,
        description=(
            "Set to 'corpus' to request a corpus-tier write. Neither "
            "episodic nor procedural has a corpus-write scope declared "
            "(ADR-062 §4) — any corpus request here returns 400."
        ),
    ),
    # NOT auth-only: the real gate is the dynamic per-layer
    # ``_require_layer_write`` below. The static ``scopes=[]`` only keeps
    # OAuth2 declared in the OpenAPI spec (same pattern as /memory/index);
    # the scope can't be static because it depends on the ``layer`` param.
    _auth: dict[str, Any] = Security(validate_jwt, scopes=[]),
    user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    """Upload a file to MinIO under the specified memory layer.

    Two paths (Luis 2026-05-10, ADR-048 PR-B3):

    * **PDF uploads** (``content-type: application/pdf`` AND
      magic-byte sniff matches ``%PDF-``) take the quarantine flow:
      bytes land in ``quarantine/<user>/<scan_id>/<file>`` (not the
      requested ``layer`` prefix), a ``memory_items`` row is inserted
      with ``scan_status='pending_scan'``, and an AMQP scan-request is
      enqueued. The response is **HTTP 202** with a ``scan_id`` and
      poll URL. Promotion to ``episodic/papers/`` happens
      asynchronously in content-control once the verdict is clean. This
      branch is UNCHANGED by ADR-062 Phase B — the async quarantine →
      verdict → promotion pipeline is ADR-048's content-control
      concern, out of WU-B4/WU-B5's scope.

    * **Non-PDF uploads** (markdown skills, ADRs, plain text): ADR-062
      Phase B (WU-B5) — writes to the caller's PRIVATE tier via the
      episodic/procedural service's own ``write()`` (already wired to
      the private bucket by WU-B2), closing a leak where every non-PDF
      upload used to land unconditionally in the shared/corpus bucket
      regardless of who uploaded it. Neither layer has a declared
      corpus-write scope (ADR-062 §4's frozenset is exactly
      decisions/skills/semantic), so ``?promote=corpus`` is rejected
      with 400 rather than silently accepted or silently ignored.

    Authorization (per-layer): the caller's JWT must carry
    ``memory:<layer>:write`` matching the ``layer`` query parameter
    (or ``audittrace:admin``). A token with ``memory:procedural:write``
    cannot upload to ``layer=episodic`` and vice-versa. This is enforced
    by ``_require_layer_write`` on the first line below — the endpoint is
    **not** auth-only despite the static ``scopes=[]`` on the OAuth2
    declaration (which exists only so the spec lists the security scheme;
    the effective scope is dynamic in the ``layer`` parameter, so it
    cannot be declared statically — same contract as ``/memory/index``).
    """
    _require_layer_write(user, layer)
    settings = get_settings()
    target_filename = filename or file.filename or "unnamed"
    content = await file.read()
    minio_client = _get_minio_client()

    # ── ADR-048 PR-B3 dispatch ───────────────────────────────────
    from audittrace.routes.memory_upload.handler import (  # noqa: PLC0415
        handle_pdf_upload,
    )
    from audittrace.routes.memory_upload.quarantine import (  # noqa: PLC0415
        is_pdf_upload,
    )

    claimed_ct = file.content_type or ""
    if is_pdf_upload(claimed_content_type=claimed_ct, content=content):
        from audittrace.dependencies import (  # noqa: PLC0415
            get_postgres_factory,
        )

        scan_queue = getattr(request.app.state, "scan_queue", None)
        if scan_queue is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "scan pipeline disabled (AUDITTRACE_SCAN_PIPELINE_ENABLED=false)"
                ),
            )
        body = await handle_pdf_upload(
            settings=settings,
            minio_client=minio_client,
            session_factory=get_postgres_factory().get_session_factory(),
            queue=scan_queue,
            user=user,
            filename=target_filename,
            content=content,
            content_type=claimed_ct,
        )
        response.status_code = 202
        return body

    # ── Legacy synchronous path (markdown, etc.) ────────────────
    # ADR-062 Phase B (WU-B5): defaults to the caller's PRIVATE tier.
    # See the docstring above + `_write_layer_private` for the leak this
    # closes. ``?promote=corpus`` has no declared scope for these two
    # layers, so it is rejected outright (fail-closed) rather than
    # silently downgraded to private or silently ignored.
    tier = _resolve_requested_tier(None, promote)
    _reject_corpus_promotion_for(layer.value, tier)
    try:
        text_content = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail="non-PDF /memory/upload content must be UTF-8 text",
        ) from exc

    await _write_layer_private(layer, user, target_filename, text_content)

    private_bucket = (
        settings.aws_private_bucket
        if settings.object_storage_backend == "aws"
        else settings.minio_private_bucket
    )
    key = f"{user.user_id}/{layer.value}/{target_filename}"

    logger.info(
        "Uploaded %s (%d bytes) to s3://%s/%s (tier=private)",
        target_filename,
        len(content),
        private_bucket,
        key,
    )

    return {
        "status": "uploaded",
        "bucket": private_bucket,
        "key": key,
        "size_bytes": len(content),
        "tier": "private",
    }


# ── POST /memory/index ──────────────────────────────────────────────────────


def _physical_collection(name: str) -> str:
    """Physical ChromaDB collection name for the nomic (768-dim) generation
    (ADR-047). The ``_v2`` suffix keeps it disjoint from the legacy
    in-process (384-dim) collection, so re-indexing is non-destructive and
    incremental — mirrors ChromaSemanticService._physical."""
    return f"{name}_v2"


async def _upsert_in_batches(
    collection: Any,
    ids: list[str],
    documents: list[str],
    metadatas: list[dict[str, Any]],
) -> None:
    # async: the chroma client is async (AsyncHttpClient) — await the upsert.
    """``collection.upsert`` in fixed-size slices.

    Upsert (vs. add) so the single-file ?file= mode is idempotent —
    a client looping per file can re-run the same call without
    duplicate-id errors. Splitting per file into _INDEX_BATCH_SIZE
    chunks bounds the in-flight payload size to ChromaDB.

    ADR-047: chunk vectors are computed on the nomic server per batch and
    supplied explicitly, so ChromaDB never embeds client-side. Both the
    markdown and PDF index paths flow through here, so this one choke-point
    covers both.
    """
    settings = get_settings()
    for start in range(0, len(ids), _INDEX_BATCH_SIZE):
        end = min(start + _INDEX_BATCH_SIZE, len(ids))
        batch_docs = documents[start:end]
        await collection.upsert(
            ids=ids[start:end],
            documents=batch_docs,
            metadatas=metadatas[start:end],
            embeddings=await embed_via_nomic(
                batch_docs,
                embed_url=settings.embed_url,
                model=settings.embed_model,
            ),
        )


def _read_minio_object(client: Any, bucket: str, key: str) -> bytes | None:
    """Fetch an object's bytes; return None on read failure (logged).

    Uses a ``with`` block on the MinIO response so the underlying
    urllib3 connection is closed + released on every exit path
    (success, decode error, network blip). Replaces the prior
    try/finally + ``response.close() / release_conn()`` pair — same
    semantics, idiomatic Python resource cleanup.
    """
    try:
        with client.get_object(bucket, key) as response:
            return bytes(response.read())
    except Exception as exc:
        logger.warning("Failed to read %s: %s", key, exc)
        return None


async def _index_md_objects(
    collection: Any,
    minio_client: Any,
    bucket: str,
    objects: list[dict[str, str]],
    col_name: str,
    category: str,
    user_id: str,
    tier: str,
    outcomes: list[bool] | None = None,
) -> int:
    """Stream-index ``.md`` files into *collection*.

    Per-file: read → chunk → add → drop. Avoids holding the cross-corpus
    document list in memory.

    *outcomes* (#430 v3) — when supplied, every object in *objects*
    appends exactly one ``bool`` here: ``True`` once the object clears
    both the extension check AND the MinIO read (i.e. it was actually
    ACCEPTED for processing — independent of how many chunks the
    content happens to yield), ``False`` when the object is rejected
    on extension mismatch or the read fails. This is the per-object
    OUTCOME signal the ``/memory/index`` handler uses to distinguish a
    genuine routing/read rejection from a benign empty-content file —
    unconditionally available (unlike ``?details``), so the handler's
    loud-422 guard never depends on an opt-in query flag.
    """
    total = 0
    for obj in objects:
        if not obj["filename"].endswith(".md"):
            if outcomes is not None:
                outcomes.append(False)
            continue
        raw = await asyncio.to_thread(
            _read_minio_object, minio_client, bucket, obj["key"]
        )
        if raw is None:
            if outcomes is not None:
                outcomes.append(False)
            continue
        if outcomes is not None:
            outcomes.append(True)
        content = raw.decode("utf-8", errors="replace")
        chunks = _chunk_text(content)
        if not chunks:
            continue
        ids = [_doc_id(col_name, obj["filename"], i) for i in range(len(chunks))]
        metadatas: list[dict[str, Any]] = [
            {
                "source": obj["filename"],
                "category": category,
                "file_type": "md",
                "chunk": i,
                # ``user_id`` is the ONE ownership key across every writer
                # (this path and routes/memory_pdf/pipeline.py). It is the
                # key ``ChromaSemanticService.search`` filters on for
                # non-admin callers.
                #
                # Before 2026-07-21 this path set NO ownership key at all and
                # the PDF path set ``ingested_by_user_id``, so the filter
                # matched nothing and EVERY non-admin recall returned zero
                # results for EVERY collection. Nothing failed loudly: an
                # empty result set is indistinguishable from "no relevant
                # documents", so the model read it as "this does not exist"
                # (#374). Do not introduce a second ownership key.
                "user_id": user_id,
                # ADR-059 fleet-recall gap (WU-1, 2026-08-07): stamp the
                # ALREADY-resolved (`index_memory`'s token-derived, ADR-027
                # §1 tenancy) tier onto every chunk. Before this fix, no
                # `tier` key was ever written here — the backoffice
                # discovery-merge builder (`_merge_semantic_with_chroma`)
                # defaults an untagged row's surfaced tier to `"corpus"`
                # (its `meta.get("tier", "corpus")`), and `list_semantic`'s
                # `_filter_corpus_read_gate` then drops any corpus-tagged
                # item the caller lacks `memory:corpus:<collection>:read`
                # for. So a caller's OWN genuinely-private single-file fold
                # (e.g. the ADR-059 fleet helper's `log_deploy_record`) was
                # silently misclassified as corpus and gated out, even
                # though it was never meant to require a corpus scope at
                # all. This is metadata accuracy, not a scope/tenancy
                # decision — `tier` is the SAME value the caller's
                # ``POST /memory/upload``/``?file=`` key prefix already
                # resolved (private vs corpus), just not previously
                # propagated into ChromaDB.
                "tier": tier,
            }
            for i in range(len(chunks))
        ]
        if category == "procedural" and obj["filename"].startswith("SKILL-"):
            skill_name = obj["filename"].replace("SKILL-", "").replace(".md", "")
            for m in metadatas:
                m["skill"] = skill_name
        await _upsert_in_batches(collection, ids, chunks, metadatas)
        total += len(chunks)
    return total


# ── Tier-C PDF metadata + corrupted-file classification (ADR-056) ────────


@router.post("/index")
async def index_memory(
    collections: str | None = Query(None),
    file: str | None = Query(
        None,
        description=(
            "Optional MinIO object key (e.g. ``episodic/papers/foo.pdf``). "
            "When set, only this single object is indexed via idempotent "
            "upsert into the named collection — used by the per-file "
            "client loop pattern that keeps memory bounded for the PDF "
            "corpus. The collection is NOT delete-and-recreated in this "
            "mode; existing chunks for other files are preserved."
        ),
    ),
    details: bool = Query(
        False,
        description=(
            "Tier-C item #24 (ADR-056). When true, the response includes a "
            "``documents`` array carrying per-document outcomes — file, "
            "chunks written, signature_status, page_count, "
            "extraction_warnings, document_sha256, the ADR-056 #10 metadata "
            "fields (pdf_title, pdf_author, pdf_creator, pdf_creation_date), "
            "ok/error. Default ``false`` keeps the legacy response shape "
            "for backwards compatibility. Bulk indexes can produce large "
            "``documents`` arrays; use opt-in for clients that want the "
            "detail."
        ),
    ),
    dry_run: bool = Query(
        False,
        description=(
            "Tier-C item #23 (ADR-056). When true, the request walks every "
            "PDF through the full extraction + signature + metadata "
            "pipeline but does NOT upsert chunks into ChromaDB and does "
            "NOT write the manifest row. Useful for previewing what would "
            "happen — pairs naturally with ``details=true`` to surface "
            "the per-document outcome the writer would have produced. "
            "Default ``false`` performs the full write."
        ),
    ),
    _auth: dict[str, Any] = Security(validate_jwt, scopes=[]),
    user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    """Read documents from MinIO and push chunked embeddings to ChromaDB.

    Two modes:

    * **Bulk** (default — no ``file``): rebuilds each named collection
      via delete-and-recreate. ``collections`` is comma-separated;
      defaults to ``decisions,skills,semantic`` (all ``.md``-only).
    * **Single-file** (``?file=<key>``): idempotent upsert of one
      MinIO object into the named collection. Operators use this in
      a per-file client loop for the ``ai_research_papers`` collection
      so each request's working set stays within the request-handler's
      memory budget.

    The opt-in ``ai_research_papers`` collection extracts text per page
    from PDFs and is rebuilt only when explicitly named in
    *collections* — keeping routine /memory/index calls fast.

    Authorization:

    * Bulk mode (``?file`` absent) — destructive whole-collection
      delete-and-recreate; cross-user by design. Requires
      ``audittrace:admin``.
    * Single-file mode (``?file=<layer>/<key>``) — one-document
      idempotent upsert. Requires ``memory:<layer>:write`` matching
      the file's MinIO prefix (or ``audittrace:admin``). The empty
      static ``scopes=[]`` keeps OAuth2 declared in the OpenAPI
      spec; the prose contract lives here.
    """
    # ADR-062 Phase B (#426) — tier disambiguation anchored on the TOKEN
    # sub, never the caller-supplied key (ADR-027 §1: tenancy is
    # token-derived, NEVER caller-supplied). ``tier``/``layer_for_scope``
    # are set here and consumed further down for bucket selection, object
    # dispatch, cache invalidation, and the write-audit ``detail_extra``.
    # Bulk mode always stays "corpus" (admin cross-user shared rebuild;
    # ``layer_for_scope`` stays ``None`` — bulk indexes both layers).
    tier: str = "corpus"
    layer_for_scope: MemoryLayer | None = None
    if file is None:
        _require_admin(user, "bulk /memory/index rebuild")
    else:
        # A private-tier key from POST /memory/upload has shape
        # "{jwt.sub}/{layer}/{filename}" (memory.py:635). The legacy
        # corpus/shared key shape is "{layer}/{filename}". Testing the
        # TOKEN prefix first means a foreign sub can never take the
        # private branch: "{other_sub}/episodic/x.md" does not start
        # with this caller's token prefix, so it falls to the corpus
        # branch below, where "{other_sub}" is not a valid MemoryLayer
        # -> 400. Cross-user private read is impossible by construction.
        token_prefix = f"{user.user_id}/"
        try:
            if file.startswith(token_prefix):
                tier = "private"
                remainder = file[len(token_prefix) :]
                layer_str = remainder.split("/", 1)[0]
            else:
                tier = "corpus"
                layer_str = file.split("/", 1)[0]
            layer_for_scope = MemoryLayer(layer_str)
        except (KeyError, ValueError):
            raise HTTPException(
                status_code=400,
                detail=(
                    "?file= must start with a known layer prefix "
                    f"({sorted(layer.value for layer in MemoryLayer)!r})"
                ),
            ) from None
        _require_layer_write(user, layer_for_scope)
    target_collections = (
        [c.strip() for c in collections.split(",") if c.strip()]
        if collections
        else list(_DEFAULT_COLLECTIONS)
    )

    unknown = [c for c in target_collections if c not in _KNOWN_COLLECTIONS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown collection(s): {unknown!r}. "
                f"Valid: {sorted(_KNOWN_COLLECTIONS)!r}"
            ),
        )

    settings = get_settings()
    # ADR-006 — effective bucket switches between MinIO (default) and
    # AWS S3 based on AUDITTRACE_OBJECT_STORAGE_BACKEND. The minio
    # variable name in settings is kept for backwards compatibility.
    shared_bucket = (
        settings.aws_bucket
        if settings.object_storage_backend == "aws"
        else settings.minio_shared_bucket
    )
    # ADR-062 Phase B (#426) — private bucket mirrors the write side
    # (memory.py:630-634). Bulk mode never selects it (tier stays
    # "corpus" — admin cross-user shared-corpus rebuild); single-file
    # mode selects it only when the tier disambiguation above resolved
    # ``tier == "private"``.
    private_bucket = (
        settings.aws_private_bucket
        if settings.object_storage_backend == "aws"
        else settings.minio_private_bucket
    )
    bucket = private_bucket if tier == "private" else shared_bucket
    minio_client = _get_minio_client()
    chroma_client = get_chromadb()

    start = time.time()
    # One ingestion timestamp per /memory/index call — every chunk
    # written in this batch carries this value, so an auditor can
    # group "all chunks indexed during this request" by exact match.
    # Item #21: per-chunk reconstructibility.
    ingestion_ts_ms = int(start * 1000)
    results: dict[str, int] = {}
    total_chunks = 0
    # #430 v3 (REVIEW REJECT #2 2026-08-07 fix) — the discriminator for the
    # loud-422 guard below. v2 gated on ``objects_attempted == 0``, which
    # only measured "was the layer ROUTED to an indexer call" — it did NOT
    # measure whether the routed object was actually ACCEPTED, so a
    # single-file extension mismatch (routed to ``_index_md_objects`` but
    # rejected there on the ``.md`` suffix check) or a corrupted-PDF
    # exception (routed to ``_index_pdf_objects`` but flushed ``ok=False``)
    # both wrongly stayed 200. ``object_outcomes`` instead accumulates the
    # ACTUAL per-object outcome — one ``bool`` per object dispatched to
    # either indexer, appended by the indexer itself: ``True`` once the
    # object clears extension/read (md) or reaches the pipeline's
    # ``ok=True`` flush (pdf) — regardless of how many chunks it yields —
    # ``False`` on any rejection/skip/failure (extension mismatch, read
    # failure, bomb-defense reject, encryption, corrupted-PDF exception).
    # This is ALWAYS available (unlike ``details_log``, which is opt-in via
    # ``?details``) and answers the principled question "was the object
    # actually processed vs rejected/skipped" rather than "did processing
    # yield chunks" — a text-free/image-free PDF page is legitimately
    # processed (``ok=True``) with 0 chunks to embed (memory_pdf/pipeline.py
    # "truly empty page — benign, no warning"), so ``total_chunks == 0``
    # alone conflates that benign case with a genuine rejection.
    object_outcomes: list[bool] = []
    # Tier-C #24 (ADR-056) — when ?details=true, every PDF processed
    # appends one outcome row here for inclusion in the response.
    # Allocated unconditionally to keep the call sites uniform; only
    # surfaced in the response when ``details`` is true.
    details_log: list[dict[str, Any]] = []

    single_file_mode = file is not None
    episodic_objects: list[dict[str, str]] = []
    procedural_objects: list[dict[str, str]] = []
    if file is not None:
        # mypy: ``file`` is now narrowed to ``str``.
        if len(target_collections) != 1:
            raise HTTPException(
                status_code=400,
                detail=(
                    "?file= requires exactly one collection in ?collections= "
                    "(single-file mode is per-collection)."
                ),
            )
        # Dispatch on the RESOLVED layer (``layer_for_scope``), not on the
        # raw key's prefix (#426): a private key's first segment is the
        # token sub, not a layer, so a ``file.startswith("episodic/")``
        # test would silently miss every private-tier object. The FULL
        # key (incl. sub prefix for private keys) is kept as ``"key"`` so
        # ``minio_client.get_object(bucket, key)`` resolves either tier.
        single_obj: dict[str, str] = {
            "key": file,
            "filename": file.rsplit("/", 1)[-1],
        }
        if layer_for_scope == MemoryLayer.episodic:
            episodic_objects = [single_obj]
        elif layer_for_scope == MemoryLayer.procedural:
            procedural_objects = [single_obj]
        else:
            # Defensive only — unreachable while MemoryLayer has exactly
            # these two members (already validated above by the
            # ``MemoryLayer(layer_str)`` construction, which 400s on any
            # other value). Guards against a future MemoryLayer addition
            # being wired into the enum without updating this dispatch.
            raise HTTPException(  # pragma: no cover
                status_code=400,
                detail=(
                    "single-file /memory/index supports only "
                    "episodic/ and procedural/ layers"
                ),
            )
    else:
        episodic_objects = _list_objects_from_minio(minio_client, bucket, "episodic/")
        procedural_objects = _list_objects_from_minio(
            minio_client, bucket, "procedural/"
        )

    for col_name in target_collections:
        # Bulk mode: delete-and-recreate for idempotency. Single-file
        # mode: get-or-create only — preserves chunks for other files
        # in the collection so a per-file client loop builds up
        # cumulatively. ``contextlib.suppress`` swallows delete
        # failures so a fresh install with no prior collection
        # doesn't 500 (per ``feedback_use_context_managers``).
        # Tier-C #23 (ADR-056): dry-run preserves the existing
        # collection — no delete-and-recreate side-effect.
        # ADR-047: write to the physical _v2 (768-dim) collection; ChromaDB
        # never embeds client-side (embedding_function=None) — chunk vectors
        # are computed on the nomic server in _upsert_in_batches.
        physical_col = _physical_collection(col_name)
        if not single_file_mode and not dry_run:
            with contextlib.suppress(Exception):
                await chroma_client.delete_collection(physical_col)
        collection = await chroma_client.get_or_create_collection(
            name=physical_col,
            embedding_function=None,
        )

        chunk_count = 0
        if col_name in ("decisions", "semantic"):
            chunk_count += await _index_md_objects(
                collection,
                minio_client,
                bucket,
                episodic_objects,
                col_name,
                category="episodic",
                user_id=user.user_id,
                tier=tier,
                outcomes=object_outcomes,
            )
        if col_name in ("skills", "semantic"):
            chunk_count += await _index_md_objects(
                collection,
                minio_client,
                bucket,
                procedural_objects,
                col_name,
                category="procedural",
                user_id=user.user_id,
                tier=tier,
                outcomes=object_outcomes,
            )
        if col_name == "ai_research_papers":
            # Tier-B item #22 + tier-C items #23/#24 (ADR-056): thread
            # the manifest service AND the per-document details
            # accumulator AND the dry-run flag. The manifest service
            # writes Postgres rows (skipped under dry_run); the details
            # accumulator collects per-doc outcomes for the
            # ?details=true response shape. ``outcomes`` (#430 v3) is the
            # separate, ALWAYS-populated per-object ok/reject signal the
            # loud-422 guard below reads.
            manifest_service = get_memory_manifest_service()
            chunk_count += await _index_pdf_objects(
                collection,
                minio_client,
                bucket,
                episodic_objects,
                col_name,
                category="episodic",
                layer_prefix="episodic/",
                user_id=user.user_id,
                ingestion_ts_ms=ingestion_ts_ms,
                manifest_service=manifest_service,
                details_log=details_log,
                dry_run=dry_run,
                outcomes=object_outcomes,
            )
            chunk_count += await _index_pdf_objects(
                collection,
                minio_client,
                bucket,
                procedural_objects,
                col_name,
                category="procedural",
                layer_prefix="procedural/",
                user_id=user.user_id,
                ingestion_ts_ms=ingestion_ts_ms,
                manifest_service=manifest_service,
                details_log=details_log,
                dry_run=dry_run,
                outcomes=object_outcomes,
            )

        results[col_name] = chunk_count
        total_chunks += chunk_count
        logger.info("Indexed %s: %d chunks", col_name, chunk_count)

    # Backlog #15, R3: after a real (non-dry-run) index seed, invalidate the
    # shared listing caches so any objects the seed run touched are re-read
    # fresh on the next list. Skipped for dry-run (no side effects).
    if not dry_run:
        # #426: keyed off the RESOLVED layer (``layer_for_scope``), not the
        # raw key prefix — a private key's first segment is the token sub,
        # so ``file.startswith("episodic/")`` would never match and a
        # private-tier index would fail to self-heal the list cache.
        if file is None or layer_for_scope == MemoryLayer.episodic:
            _invalidate_layer_list_cache(MemoryLayer.episodic.value)
        if file is None or layer_for_scope == MemoryLayer.procedural:
            _invalidate_layer_list_cache(MemoryLayer.procedural.value)

    duration = time.time() - start
    response: dict[str, Any] = {
        "status": "dry_run" if dry_run else "indexed",
        "collections": results,
        "total_chunks": total_chunks,
        "duration_s": round(duration, 2),
    }
    if dry_run:
        response["dry_run"] = True
    if details:
        response["documents"] = details_log
    # ADR-062 §5 (WU-A4) — write path, audited inline + awaited (fail-
    # closed). One event per /memory/index call, naming every target
    # collection touched; single-file mode additionally names the key.
    await _emit_write_audit(
        user=user,
        op="write",
        layer="semantic",
        collection=",".join(target_collections),
        key=file,
        detail_extra={
            "mode": "single_file" if single_file_mode else "bulk",
            "dry_run": dry_run,
            "total_chunks": total_chunks,
            # ADR-062 Phase B (#426) — additive-only field (audit schema
            # frozen at 1.17.0): "private" or "corpus", per the tier
            # resolution above.
            "tier": tier,
        },
    )
    # #430 — a single-file index whose object was never actually accepted by
    # an indexer is a silent failure wearing a 200: the caller believes the
    # object landed and it was not. The write ATTEMPT is still audited above
    # (ADR-058 — the attempt, with ``total_chunks: 0``, is already in
    # ``detail_extra``) before this loud fail.
    #
    # v3 (REVIEW REJECT #2 2026-08-07): gate on ``any(object_outcomes)``, NOT
    # ``objects_attempted == 0`` (v2) or ``total_chunks == 0`` (v1). v2's
    # ``objects_attempted`` only proved the object's LAYER was routed to an
    # indexer call — it said nothing about whether the indexer then ACCEPTED
    # the object, so an extension mismatch (``.pdf`` key indexed into a
    # ``.md``-only collection, rejected inside ``_index_md_objects``) or a
    # corrupted-PDF parse exception (rejected inside ``_index_pdf_objects``,
    # flushed ``ok=False``) both wrongly stayed 200 under v2 — reopening
    # #430. ``object_outcomes`` is populated by the indexers themselves with
    # the ACTUAL per-object outcome (True = accepted/processed, independent
    # of resulting chunk count; False = rejected/skipped/failed), so
    # ``not any(object_outcomes)`` is true both when the object was routed
    # but rejected AND when it was never routed at all (empty list). A
    # text-free/image-free PDF page still appends True (the pipeline reaches
    # its ``ok=True`` flush even with 0 chunks written) — benign, stays 200.
    # Bulk mode is exempt (an admin whole-collection rebuild legitimately
    # attempts 0 objects when nothing matches) and dry-run is exempt (no
    # write intended) — both keep returning 200.
    if single_file_mode and not dry_run and not any(object_outcomes):
        raise HTTPException(
            status_code=422,
            detail=(
                f"single-file index of file={file!r} into "
                f"collections={target_collections!r} wrote 0 chunks — "
                "the object was NOT indexed (it was not accepted by any "
                "indexer for this collection). Plausible causes: the "
                "object's layer prefix does not match the target "
                "collection's ingestion routing (e.g. a procedural/ or "
                "episodic/ key indexed into a collection that only "
                "accepts the other layer), the object's file extension "
                "does not match what this collection ingests (e.g. a "
                ".pdf key indexed into a .md-only collection), the file "
                "could not be read from object storage, or the file "
                "could not be parsed (e.g. a corrupted/unreadable PDF)."
            ),
        )
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Memory-layer CRUD backoffice
# ─────────────────────────────────────────────────────────────────────────────
#
# Five endpoints per layer (POST / GET-list / GET-one / PUT / DELETE).
# The handlers follow the same pattern across episodic + procedural since
# both are S3-backed by filename. Semantic has a different shape because
# its key is `<collection>/<document_id>` and it has no cache.
#
# Authorship + timestamps + soft-delete state lives in the `memory_items`
# Postgres table via `MemoryManifestService`. The actual content lives in
# the storage backend (S3 for episodic/procedural, ChromaDB for semantic).
# Writes touch BOTH; reads return content + manifest metadata.
#
# Per-layer write scopes (`memory:<layer>:write`) gate every mutation;
# read scopes (`memory:<layer>:read`) already existed for the recall_*
# tools.


def _validate_filename_or_400(filename: str, layer: str) -> None:
    """Filename validation echoing the service-layer rules; surfaces 400
    instead of raising deeper. Layer-specific prefix check is advisory
    (we don't enforce it here so an operator could in principle upload
    a non-conforming filename)."""
    if not filename or not filename.endswith(".md"):
        raise HTTPException(
            status_code=400,
            detail=f"filename must end with .md (got {filename!r})",
        )
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(
            status_code=400,
            detail="filename must not contain path separators or '..'",
        )


# ── /memory/episodic ────────────────────────────────────────────────────────


@router.post("/episodic")
async def create_episodic(
    payload: dict[str, Any] = Body(..., description="{filename, content, title?}"),
    promote: str | None = Query(
        None,
        description=(
            "Set to 'corpus' to request a corpus-tier write. Episodic has no "
            "corpus-write scope declared (ADR-062 §4) — any corpus request "
            "here returns 400, not a partial/soft acceptance."
        ),
    ),
    _scope: dict[str, Any] = Security(validate_jwt, scopes=["memory:episodic:write"]),
    user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    """Create an ADR. Body: ``{filename, content, title?}``.

    Idempotent on the manifest: re-creating a soft-deleted item revives
    it (clearing ``deleted_at_ms``) and bumps ``modified_at_ms``.

    ADR-062 Phase B (WU-B5): every write lands in the caller's PRIVATE
    tier (``tier="private"``, owner=caller) — ``service.write`` already
    only ever targets the private bucket (WU-B2); this just stamps the
    manifest row to match. Episodic has no corpus-write scope in the
    ADR-062 §4 catalogue, so a ``tier=corpus``/``?promote=corpus``
    request is rejected (400) rather than silently downgraded.
    """
    filename = payload.get("filename")
    content = payload.get("content")
    if not isinstance(filename, str) or not isinstance(content, str):
        raise HTTPException(
            status_code=400,
            detail="filename and content are required string fields",
        )
    _validate_filename_or_400(filename, "episodic")
    tier = _resolve_requested_tier(payload, promote)
    _reject_corpus_promotion_for("episodic", tier)
    title = payload.get("title")
    service = get_episodic_service()
    manifest = get_memory_manifest_service()
    try:
        await service.write(user, filename, content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    entry: ManifestEntry = await manifest.record_create(
        layer="episodic",
        key=filename,
        title=title,
        size_bytes=len(content.encode("utf-8")),
        user_id=user.user_id,
        tier=tier,
    )
    # ADR-062 §5 (WU-A4) — write path, audited inline + awaited (fail-
    # closed: see services/memory_audit.py module docstring).
    await _emit_write_audit(user=user, op="write", layer="episodic", key=filename)
    return entry.to_dict()


# ── List ergonomics — sort / order / limit / offset (backlog #15, R5) ────────

# Sensible page-size default + hard cap so a single list request can never
# ask the server to serialise an unbounded set.
_LIST_DEFAULT_LIMIT = 100
_LIST_MAX_LIMIT = 500

# Query-param literals — FastAPI validates these (422 on anything else), so
# the sort/order values are a closed set and the sort key functions below
# never see an unexpected field.
ListSort = Literal["created_at", "modified_at", "key", "size"]
ListOrder = Literal["asc", "desc"]

_SORT_FIELD_MS = {"created_at": "created_at_ms", "modified_at": "modified_at_ms"}


def _sort_and_paginate(
    items: list[dict[str, Any]],
    *,
    sort: str,
    order: str,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    """Return ``(page, total)`` — a total-order-safe sorted, paginated slice.

    ``total`` is the full pre-limit count so a client can page. Sorting is
    total-order safe: timestamp/size keys coalesce ``None`` → ``0`` and the
    ``key`` sort coalesces to ``""``, so no comparison ever touches ``None``
    (backlog #15, R5). Ties break on the object ``key`` ascending regardless
    of ``order`` (stable secondary sort) so paging is deterministic.

    The sort/slice mechanic itself is the shared
    :func:`audittrace.services.pagination.sort_and_paginate` core (backlog
    #15 R2b / #375) — only the field-name-to-key-function mapping below is
    specific to this endpoint's manifest-row shape.
    """

    def _int_key(field: str) -> Callable[[dict[str, Any]], Any]:
        def key(item: dict[str, Any]) -> Any:
            value = item.get(field)
            return value if isinstance(value, int) else 0

        return key

    def _str_key(item: dict[str, Any]) -> Any:
        return str(item.get("key") or "")

    primary: Callable[[dict[str, Any]], Any]
    if sort == "key":
        primary = _str_key
    elif sort == "size":
        primary = _int_key("size_bytes")
    else:
        primary = _int_key(_SORT_FIELD_MS.get(sort, "created_at_ms"))

    return _core_sort_and_paginate(
        items,
        key_fn=primary,
        tiebreak_fn=_str_key,
        reverse=(order != "asc"),
        limit=limit,
        offset=offset,
    )


async def _merge_layer_items_with_s3(
    layer: str,
    visible_entries: list[ManifestEntry],
    user: UserContext,
) -> list[dict[str, Any]]:
    """Merge S3 objects into the manifest-row list so pre-PR-A items
    (uploaded via /memory/upload or seeded via index-chromadb) surface
    in the backoffice.

    The manifest table is operator-managed and only contains items
    created via the new POST endpoint. The Memory tab has to reflect
    *all* content for the layer, so we walk the S3 prefix and emit
    a "discovered" entry for any object not already in the manifest.
    Manifest rows take precedence — they carry authorship, soft-delete
    state, sub-second timestamps. Discovered entries carry only what
    the storage backend knows: filename, size, title-from-content.

    Excludes from S3 discovery any key that has a manifest row even
    if it's currently filtered out (e.g. soft-deleted with
    ``include_deleted=False``). Otherwise a soft-deleted item would
    resurrect on every list as "discovered" because the S3 object
    is still there — we'd be papering over the operator's delete
    intent.

    ADR-062 Phase B (WU-B4): ``visible_entries`` is already
    owner-or-corpus scoped by the caller (``list_episodic``/
    ``list_procedural`` pass ``caller=user`` to ``list_for_layer``).
    ``all_known`` (used only to build ``known_keys`` for the dedup
    check below) is scoped the same way — a key only visible via
    another caller's private manifest row is treated as "not yet
    known" here, but the discovery loop's own per-user
    ``service.load(user)`` call (already private∪corpus-scoped since
    WU-B2) never surfaces that other caller's content either, so no
    leak results: the key just correctly doesn't appear at all.
    """
    items: list[dict[str, Any]] = [e.to_dict() for e in visible_entries]

    manifest = get_memory_manifest_service()
    all_known: list[ManifestEntry] = await manifest.list_for_layer(
        layer, include_deleted=True, caller=user
    )
    known_keys = {e.key for e in all_known}

    service: Any
    if layer == "episodic":
        service = get_episodic_service()
    elif layer == "procedural":
        service = get_procedural_service()
    else:
        return items  # other layers don't have S3 backing

    try:
        docs = await service.load(user)
    except Exception as exc:
        logger.warning(
            "S3 backfill load failed for layer %s: %s — listing manifest only",
            layer,
            exc,
        )
        return items

    for doc in docs:
        filename = doc.metadata.get("file")
        if not filename or filename in known_keys:
            continue
        # R4 (backlog #15): discovered entries carry the S3 object's real
        # ``last_modified`` (epoch-ms) for both created/modified so the merged
        # set is uniformly sortable and never yields ``None`` timestamps.
        # Manifest rows still take precedence (authorship, sub-second times).
        last_modified_ms = doc.metadata.get("last_modified_ms")
        items.append(
            {
                "id": None,
                "layer": layer,
                "key": filename,
                "title": doc.metadata.get("title")
                or doc.metadata.get("skill")
                or filename,
                "size_bytes": len(doc.page_content.encode("utf-8")),
                "created_at_ms": last_modified_ms,
                "modified_at_ms": last_modified_ms,
                "created_by_user_id": None,
                "modified_by_user_id": None,
                "deleted_at_ms": None,
                "deleted_by_user_id": None,
                "discovered": True,
                # ADR-062 Phase B (WU-B4) — surface tier on discovered
                # rows too; `service.load(user)` (WU-B2) already stamps
                # `metadata["tier"]` on every returned Document.
                "tier": doc.metadata.get("tier", "corpus"),
            }
        )
    return items


@router.get("/episodic")
async def list_episodic(
    include_deleted: bool = Query(False),
    sort: ListSort = Query(
        "created_at",
        description="Sort field: created_at | modified_at | key | size.",
    ),
    order: ListOrder = Query(
        "desc", description="Sort direction. Default desc (most-recent-first)."
    ),
    limit: int = Query(
        _LIST_DEFAULT_LIMIT,
        ge=1,
        le=_LIST_MAX_LIMIT,
        description="Page size (max 500).",
    ),
    offset: int = Query(0, ge=0, description="Zero-based pagination offset."),
    _auth: dict[str, Any] = Security(validate_jwt, scopes=["memory:episodic:read"]),
    user: UserContext = Depends(require_user),
    *,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """List ADRs / decision records. Merges manifest rows with S3 objects so
    content uploaded via /memory/upload or seeded via index-chromadb surfaces
    alongside operator-created items.

    ``include_deleted=true`` returns soft-deleted manifest rows; it has no
    effect on discovered entries (they have no soft-delete state).
    ``sort``/``order``/``limit``/``offset`` page the merged set (backlog #15,
    R5); ``total`` is the full pre-limit count.

    ADR-062 Phase B (WU-B4): manifest-tracked rows are scoped to the
    caller's own private tier ∪ corpus (``list_for_layer(caller=user)``)
    — a non-admin caller never sees another user's private manifest
    row. No corpus-read scope gate applies to this layer (episodic has
    no declared ``memory:corpus:episodic:read`` — the ADR-062 §4
    frozenset covers only decisions/skills/semantic); corpus content
    stays visible to any ``memory:episodic:read`` holder, same as
    before this PR."""
    manifest = get_memory_manifest_service()
    entries: list[ManifestEntry] = await manifest.list_for_layer(
        "episodic", include_deleted=include_deleted, caller=user
    )
    items = await _merge_layer_items_with_s3("episodic", entries, user)
    page, total = _sort_and_paginate(
        items, sort=sort, order=order, limit=limit, offset=offset
    )
    # ADR-062 §5 (WU-A4) — read path, fail-open background emit (see
    # services/memory_audit.py module docstring).
    schedule_read_audit(background_tasks, user=user, op="list", layer="episodic")
    return {"items": page, "total": total, "limit": limit, "offset": offset}


@router.get("/episodic/{filename}")
async def read_episodic(
    filename: str,
    _auth: dict[str, Any] = Security(validate_jwt, scopes=["memory:episodic:read"]),
    user: UserContext = Depends(require_user),
    *,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """Read ADR content + manifest metadata.

    ADR-062 Phase B (WU-B4): the content itself was already resolved
    private-first-then-corpus by ``service.read`` (WU-B2), so a 404
    here means genuinely missing (not a cross-user disclosure). The
    manifest metadata block is additionally scoped by
    ``_manifest_visible`` — see its docstring for why."""
    _validate_filename_or_400(filename, "episodic")
    service = get_episodic_service()
    manifest = get_memory_manifest_service()
    doc = await service.read(user, filename)
    if doc is None:
        raise HTTPException(status_code=404, detail="ADR not found")
    entry: ManifestEntry | None = await manifest.get("episodic", filename)
    if entry is not None and not _manifest_visible(entry, user):
        entry = None
    schedule_read_audit(
        background_tasks, user=user, op="read", layer="episodic", key=filename
    )
    return {
        "content": doc.page_content,
        "metadata": doc.metadata,
        "manifest": entry.to_dict() if entry is not None else None,
    }


@router.put("/episodic/{filename}")
async def update_episodic(
    filename: str,
    payload: dict[str, Any] = Body(..., description="{content, title?}"),
    _scope: dict[str, Any] = Security(validate_jwt, scopes=["memory:episodic:write"]),
    user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    """Replace ADR content. The manifest's ``modified_at_ms`` /
    ``modified_by_user_id`` are updated; ``created_at_ms`` is preserved.
    Cannot be applied to a soft-deleted item — recreate via POST instead."""
    _validate_filename_or_400(filename, "episodic")
    content = payload.get("content")
    if not isinstance(content, str):
        raise HTTPException(
            status_code=400, detail="content is a required string field"
        )
    title = payload.get("title")
    service = get_episodic_service()
    manifest = get_memory_manifest_service()
    try:
        await service.write(user, filename, content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    try:
        entry: ManifestEntry = await manifest.record_update(
            layer="episodic",
            key=filename,
            size_bytes=len(content.encode("utf-8")),
            user_id=user.user_id,
            title=title,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=404, detail=f"ADR has no manifest row: {exc}"
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _emit_write_audit(user=user, op="write", layer="episodic", key=filename)
    return entry.to_dict()


@router.delete("/episodic/{filename}")
async def delete_episodic(
    filename: str,
    hard: bool = Query(
        False,
        description=(
            "If true, also remove the underlying S3 object. Requires "
            "audittrace:admin in addition to memory:episodic:write."
        ),
    ),
    _scope: dict[str, Any] = Security(validate_jwt, scopes=["memory:episodic:write"]),
    user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    """Soft-delete by default; ``?hard=true`` also purges the S3 object.

    ADR-062 §6 (WU-A5): the manifest soft-delete is authoritative for
    corpus content — ``?hard=true`` is NOT the default delete path (the
    ``hard`` query param defaults to ``False`` and additionally requires
    ``audittrace:admin``), and a hard purge is audited with
    ``detail_extra={"hard": True}`` same as every other delete."""
    _validate_filename_or_400(filename, "episodic")
    if hard and "audittrace:admin" not in user.scopes and not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="?hard=true requires audittrace:admin scope",
        )
    service = get_episodic_service()
    manifest = get_memory_manifest_service()
    try:
        entry: ManifestEntry = await manifest.record_delete(
            "episodic", filename, user.user_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if hard:
        try:
            await service.delete(user, filename)
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    await _emit_write_audit(
        user=user,
        op="delete",
        layer="episodic",
        key=filename,
        detail_extra={"hard": hard},
    )
    return entry.to_dict()


# ── /memory/procedural ──────────────────────────────────────────────────────


@router.post("/procedural")
async def create_procedural(
    payload: dict[str, Any] = Body(..., description="{filename, content, title?}"),
    promote: str | None = Query(
        None,
        description=(
            "Set to 'corpus' to request a corpus-tier write. Procedural has "
            "no corpus-write scope declared (ADR-062 §4) — any corpus "
            "request here returns 400."
        ),
    ),
    _scope: dict[str, Any] = Security(validate_jwt, scopes=["memory:procedural:write"]),
    user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    """Create a SKILL document. Body shape mirrors POST /memory/episodic.

    ADR-062 Phase B (WU-B5): default write is PRIVATE-tier, owner=caller;
    see ``create_episodic``'s docstring for the corpus-promotion rejection
    rationale (identical for procedural)."""
    filename = payload.get("filename")
    content = payload.get("content")
    if not isinstance(filename, str) or not isinstance(content, str):
        raise HTTPException(
            status_code=400,
            detail="filename and content are required string fields",
        )
    _validate_filename_or_400(filename, "procedural")
    tier = _resolve_requested_tier(payload, promote)
    _reject_corpus_promotion_for("procedural", tier)
    title = payload.get("title")
    service = get_procedural_service()
    manifest = get_memory_manifest_service()
    try:
        await service.write(user, filename, content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    entry: ManifestEntry = await manifest.record_create(
        layer="procedural",
        key=filename,
        title=title,
        size_bytes=len(content.encode("utf-8")),
        user_id=user.user_id,
        tier=tier,
    )
    await _emit_write_audit(user=user, op="write", layer="procedural", key=filename)
    return entry.to_dict()


@router.get("/procedural")
async def list_procedural(
    include_deleted: bool = Query(False),
    sort: ListSort = Query(
        "created_at",
        description="Sort field: created_at | modified_at | key | size.",
    ),
    order: ListOrder = Query(
        "desc", description="Sort direction. Default desc (most-recent-first)."
    ),
    limit: int = Query(
        _LIST_DEFAULT_LIMIT,
        ge=1,
        le=_LIST_MAX_LIMIT,
        description="Page size (max 500).",
    ),
    offset: int = Query(0, ge=0, description="Zero-based pagination offset."),
    _auth: dict[str, Any] = Security(validate_jwt, scopes=["memory:procedural:read"]),
    user: UserContext = Depends(require_user),
    *,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """List SKILLs. Same merge-with-S3 + sort/paginate semantics as
    `/memory/episodic` so all ``.md`` items appear alongside
    operator-created ones (backlog #15).

    ADR-062 Phase B (WU-B4): same owner-or-corpus manifest scoping as
    ``list_episodic`` — see its docstring."""
    manifest = get_memory_manifest_service()
    entries: list[ManifestEntry] = await manifest.list_for_layer(
        "procedural", include_deleted=include_deleted, caller=user
    )
    items = await _merge_layer_items_with_s3("procedural", entries, user)
    page, total = _sort_and_paginate(
        items, sort=sort, order=order, limit=limit, offset=offset
    )
    schedule_read_audit(background_tasks, user=user, op="list", layer="procedural")
    return {"items": page, "total": total, "limit": limit, "offset": offset}


@router.get("/procedural/{filename}")
async def read_procedural(
    filename: str,
    _auth: dict[str, Any] = Security(validate_jwt, scopes=["memory:procedural:read"]),
    user: UserContext = Depends(require_user),
    *,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """ADR-062 Phase B (WU-B4): same manifest-visibility scoping as
    ``read_episodic`` — see its docstring."""
    _validate_filename_or_400(filename, "procedural")
    service = get_procedural_service()
    manifest = get_memory_manifest_service()
    doc = await service.read(user, filename)
    if doc is None:
        raise HTTPException(status_code=404, detail="SKILL not found")
    entry: ManifestEntry | None = await manifest.get("procedural", filename)
    if entry is not None and not _manifest_visible(entry, user):
        entry = None
    schedule_read_audit(
        background_tasks, user=user, op="read", layer="procedural", key=filename
    )
    return {
        "content": doc.page_content,
        "metadata": doc.metadata,
        "manifest": entry.to_dict() if entry is not None else None,
    }


@router.put("/procedural/{filename}")
async def update_procedural(
    filename: str,
    payload: dict[str, Any] = Body(..., description="{content, title?}"),
    _scope: dict[str, Any] = Security(validate_jwt, scopes=["memory:procedural:write"]),
    user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    _validate_filename_or_400(filename, "procedural")
    content = payload.get("content")
    if not isinstance(content, str):
        raise HTTPException(
            status_code=400, detail="content is a required string field"
        )
    title = payload.get("title")
    service = get_procedural_service()
    manifest = get_memory_manifest_service()
    try:
        await service.write(user, filename, content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    try:
        entry: ManifestEntry = await manifest.record_update(
            layer="procedural",
            key=filename,
            size_bytes=len(content.encode("utf-8")),
            user_id=user.user_id,
            title=title,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=404, detail=f"SKILL has no manifest row: {exc}"
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _emit_write_audit(user=user, op="write", layer="procedural", key=filename)
    return entry.to_dict()


@router.delete("/procedural/{filename}")
async def delete_procedural(
    filename: str,
    hard: bool = Query(False),
    _scope: dict[str, Any] = Security(validate_jwt, scopes=["memory:procedural:write"]),
    user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    """Soft-delete by default; ``?hard=true`` also purges the S3 object
    (ADR-062 §6 — same discipline as ``delete_episodic``)."""
    _validate_filename_or_400(filename, "procedural")
    if hard and "audittrace:admin" not in user.scopes and not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="?hard=true requires audittrace:admin scope",
        )
    service = get_procedural_service()
    manifest = get_memory_manifest_service()
    try:
        entry: ManifestEntry = await manifest.record_delete(
            "procedural", filename, user.user_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if hard:
        try:
            await service.delete(user, filename)
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    await _emit_write_audit(
        user=user,
        op="delete",
        layer="procedural",
        key=filename,
        detail_extra={"hard": hard},
    )
    return entry.to_dict()


# ── /memory/semantic ────────────────────────────────────────────────────────
#
# Semantic items are keyed by `<collection>/<document_id>`. The full
# slash-separated key lives in the manifest's `key` column; the route
# splits it into path segments for the URL but recombines for the
# manifest lookup.


def _semantic_key(collection: str, document_id: str) -> str:
    """Manifest lookup key for a semantic doc. ``<collection>/<doc_id>``."""
    return f"{collection}/{document_id}"


@router.post("/semantic")
async def create_semantic(
    payload: dict[str, Any] = Body(
        ...,
        description="{collection, document_id, text, metadata?, title?, tier?}",
    ),
    promote: str | None = Query(
        None,
        description=(
            "Set to 'corpus' to write into the shared corpus collection "
            "instead of the caller's private tier. Requires "
            "memory:corpus:<collection>:write (or audittrace:admin)."
        ),
    ),
    _scope: dict[str, Any] = Security(validate_jwt, scopes=["memory:semantic:write"]),
    user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    """Upsert a semantic-layer document. The collection's embedding
    function (configured at chart install time) handles vectorisation.

    ADR-062 Phase B (WU-B5): default write lands in the caller's PRIVATE
    tier (``tier="private"``, owner=caller — ``ChromaSemanticService.
    upsert``'s own default). An explicit ``"tier": "corpus"`` body field
    or ``?promote=corpus`` query param routes the write to the shared
    corpus collection instead, but ONLY if the caller holds
    ``memory:corpus:<collection>:write`` (or ``audittrace:admin`` /
    admin) — this is the scope gate WU-B3 wired the write TARGET for but
    deliberately left unenforced (see ``ChromaSemanticService.upsert``'s
    docstring)."""
    collection = payload.get("collection")
    document_id = payload.get("document_id")
    text = payload.get("text")
    if not (
        isinstance(collection, str)
        and isinstance(document_id, str)
        and isinstance(text, str)
    ):
        raise HTTPException(
            status_code=400,
            detail="collection, document_id and text are required strings",
        )
    metadata = _sanitize_semantic_metadata(payload.get("metadata"))
    title = payload.get("title")
    tier = _resolve_requested_tier(payload, promote)
    if tier == "corpus":
        _require_corpus_scope(user, collection, "write")
    service = get_semantic_service()
    manifest = get_memory_manifest_service()
    try:
        await service.upsert(user, collection, document_id, text, metadata, tier=tier)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    entry: ManifestEntry = await manifest.record_create(
        layer="semantic",
        key=_semantic_key(collection, document_id),
        title=title,
        size_bytes=len(text.encode("utf-8")),
        user_id=user.user_id,
        tier=tier,
    )
    await _emit_write_audit(
        user=user,
        op="write",
        layer="semantic",
        collection=collection,
        key=document_id,
    )
    return entry.to_dict()


async def _discover_rows_for_caller(
    col: Any, user: UserContext
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """Fetch discovery candidate rows from one physical ChromaDB collection,
    scoped so a caller's OWN rows always survive ``_SEMANTIC_DISCOVERY_LIMIT``.

    ADR-059 fleet-recall gap (WU-1, 2026-08-07): the discovery scan used to
    be a single UNFILTERED ``col.get(limit=_SEMANTIC_DISCOVERY_LIMIT)`` —
    the owner-or-corpus predicate was applied only AFTER that raw slice was
    taken. On a physical collection with more rows than the cap (the normal
    case for a well-used collection like ``decisions_v2``), a caller's own
    freshly-written private row can sort anywhere in ChromaDB's internal
    `get()` order and simply never land inside the first `limit` rows — so
    `list_semantic` (and therefore the fleet helper
    ``scripts/deploy/memory.py::recall_deploy_lessons``, which reads this
    endpoint) reported "0 lessons" for a caller's OWN just-folded decision
    even though the row existed and was owned by them.

    Fix: for a non-admin caller, run an ADDITIONAL flat-equality scan scoped
    to ``where={"user_id": user.user_id}`` so every row the caller owns is a
    candidate for the cap, not just whichever ones happened to land in an
    unfiltered top-N slice; merge it (de-duplicated by id) with the original
    unfiltered scan so existing corpus/shared visibility within the cap is
    unchanged. Two separate flat-equality queries rather than one `$or`
    clause — mirrors ``ChromaSemanticService.search``'s established
    two-query pattern (`services/semantic.py`), because the in-repo
    ChromaDB test double only supports flat-equality `where`
    (`db/factory.py::MockCollection.get`). Admin keeps the single
    unfiltered scan, unchanged — an admin's discovery view must stay
    uncapped by ownership.
    """
    if user.is_admin:
        res = await col.get(
            limit=_SEMANTIC_DISCOVERY_LIMIT, include=["documents", "metadatas"]
        )
        return (
            list(res.get("ids") or []),
            list(res.get("documents") or []),
            list(res.get("metadatas") or []),
        )

    own_res, general_res = (
        await col.get(
            where={"user_id": user.user_id},
            limit=_SEMANTIC_DISCOVERY_LIMIT,
            include=["documents", "metadatas"],
        ),
        await col.get(
            limit=_SEMANTIC_DISCOVERY_LIMIT, include=["documents", "metadatas"]
        ),
    )
    ids: list[str] = []
    docs: list[str] = []
    metas: list[dict[str, Any]] = []
    seen: set[str] = set()
    for res in (own_res, general_res):
        res_ids = res.get("ids") or []
        res_docs = res.get("documents") or []
        res_metas = res.get("metadatas") or []
        for i, doc_id in enumerate(res_ids):
            if doc_id in seen:
                continue
            seen.add(doc_id)
            ids.append(doc_id)
            docs.append(res_docs[i] if i < len(res_docs) else "")
            metas.append(res_metas[i] if i < len(res_metas) and res_metas[i] else {})
    return ids, docs, metas


async def _merge_semantic_with_chroma(
    visible_entries: list[ManifestEntry],
    collection: str | None,
    user: UserContext,
) -> list[dict[str, Any]]:
    """Same shape as `_merge_layer_items_with_s3` for the semantic layer.

    ChromaDB has its own listing semantics — `collection.get()` with
    no `ids=` returns every doc. We cap the per-collection scan at
    ``_SEMANTIC_DISCOVERY_LIMIT`` rows to keep the list endpoint snappy
    on collections that grow large; operators who need to browse a
    large semantic store can paginate via the manifest's offset/limit
    once they've created tracked rows for the items they care about.
    ``_discover_rows_for_caller`` (above) additionally guarantees a
    non-admin caller's OWN rows are never dropped by that cap (ADR-059
    WU-1).

    ADR-062 Phase B (§3 hole 2): the raw `col.get()` discovery scan below
    used to enumerate EVERY row in every scanned collection with no
    `where` at all — any caller with `memory:semantic:read` could list
    another user's private vectors this way. A discovered row is now
    included only if the caller is admin, the row is CORPUS-tier
    (`metadata["tier"] == "corpus"`, deliberately unfiltered — D1), or
    the row's `metadata["user_id"]` matches the caller — the same
    predicate as `ChromaSemanticService._tier_authorized` (deliberately
    not imported here: this function reads raw ChromaDB responses, not
    through the service).

    ADR-062 Phase B (WU-B4): ``visible_entries`` is now ALSO scoped by
    the caller — ``list_semantic`` passes ``caller=user`` into
    ``list_for_layer``, closing the gap this docstring used to flag as
    deferred. ``all_known`` (dedup lookup only) is scoped the same way;
    see ``_merge_layer_items_with_s3``'s docstring for why that's safe
    (the discovery loop below has its own independent owner-or-corpus
    check, so a key hidden from ``known_keys`` by the caller filter
    still can't leak through as a "discovered" row for someone else's
    private vector).
    """
    items: list[dict[str, Any]] = [e.to_dict() for e in visible_entries]

    manifest = get_memory_manifest_service()
    all_known: list[ManifestEntry] = await manifest.list_for_layer(
        "semantic", include_deleted=True, caller=user
    )
    known_keys = {e.key for e in all_known}

    chroma = get_chromadb()
    # Discovery target collections: filter param if given, else all
    # collections the chroma client knows about (capped — see
    # _SEMANTIC_DISCOVERY_LIMIT). Reading every collection on every
    # list call would scale poorly past a few collections.
    target_cols: list[str]
    if collection is not None:
        # ADR-047: a named (logical) collection resolves to its physical
        # _v2 form; list_collections already yields physical names.
        target_cols = [_physical_collection(collection)]
    else:
        try:
            target_cols = [c.name for c in await chroma.list_collections()][:5]
        except Exception as exc:
            logger.warning("ChromaDB list_collections failed: %s", exc)
            return items

    for col_name in target_cols:
        try:
            # ADR-047: open with embedding_function=None (vectors are computed
            # on the nomic server); this path only reads (col.get).
            col = await chroma.get_or_create_collection(
                name=col_name,
                embedding_function=None,
            )
            ids, docs, metas = await _discover_rows_for_caller(col, user)
        except Exception as exc:
            logger.warning("ChromaDB get failed for collection %s: %s", col_name, exc)
            continue
        for i, doc_id in enumerate(ids):
            key = _semantic_key(col_name, doc_id)
            if key in known_keys:
                continue
            content = docs[i] if i < len(docs) else ""
            meta = metas[i] if i < len(metas) and metas[i] else {}
            # ADR-062 Phase B (§3 hole 2) — close the raw-Chroma enumeration
            # hole: skip a discovered row the caller does not own and that
            # isn't corpus-tier. See the function docstring.
            if not (
                user.is_admin
                or meta.get("tier") == "corpus"
                or meta.get("user_id") == user.user_id
            ):
                continue
            title = meta.get("title") or meta.get("source") or doc_id
            items.append(
                {
                    "id": None,
                    "layer": "semantic",
                    "key": key,
                    "title": title,
                    "size_bytes": len((content or "").encode("utf-8")),
                    "created_at_ms": None,
                    "modified_at_ms": None,
                    # ADR-059 WU-1b — propagate the row's owner (the SAME
                    # ``user_id`` key ``_index_md_objects`` stamps and this
                    # function's own owner-or-corpus predicate above already
                    # reads) instead of the previous hardcoded ``None``. This
                    # is what lets ``_filter_corpus_read_gate``'s ownership
                    # exemption recognise a discovered row as the caller's
                    # own even when its ``tier`` reads (or mis-defaults to)
                    # ``"corpus"``.
                    "created_by_user_id": meta.get("user_id"),
                    "modified_by_user_id": None,
                    "deleted_at_ms": None,
                    "deleted_by_user_id": None,
                    "discovered": True,
                    # ADR-062 Phase B (WU-B4) — surface tier; D3's corpus
                    # read-scope gate is applied by the caller
                    # (`list_semantic`) over the full merged item list.
                    "tier": meta.get("tier", "corpus"),
                }
            )
    return items


def _filter_corpus_read_gate(
    items: list[dict[str, Any]], user: UserContext
) -> list[dict[str, Any]]:
    """ADR-062 Phase B (WU-B4, D3) — drop corpus-tier semantic items the
    caller isn't cleared to see through the BACKOFFICE list surface.

    D3: "reading corpus content through /memory/{layer} requires the
    granular memory:corpus:<collection>:read". Applied here (not inside
    ``_merge_semantic_with_chroma``) so it uniformly covers BOTH
    manifest-tracked and discovered rows in one pass, keyed off each
    item's already-stamped ``tier``. Corpus recall via the LLM tool loop
    (``recall_semantic``) is NOT affected — that path stays
    deliberately unfiltered per D1; this gate is specific to the
    ``/memory/semantic`` REST surface (the operator/curator-tier
    backoffice), matching "Phase-A operator-tier corpus scoping".

    ADR-059 WU-1b (2026-08-07) — OWNERSHIP EXEMPTION. A caller reading
    their OWN content never needs a corpus scope: a row the caller wrote
    is exempted from this gate regardless of what its ``tier`` reads.
    This is the robust fix for the live fleet-recall gap — WU-1 (2026-08-07)
    fixed the tier STAMP written at index time, but a caller's own row
    must be visible independent of whether the tier stamp is honest,
    missing (pre-WU-1 legacy rows), or mis-defaulted to ``"corpus"`` by
    ``_merge_semantic_with_chroma``'s ``meta.get("tier", "corpus")``
    fallback. Checked BEFORE the scope lookup so an owner never needs
    ``memory:corpus:<collection>:read`` to see their own fold. Applies
    uniformly to manifest-tracked items (``created_by_user_id`` is the
    manifest's writer) and to discovered items (populated from the
    ChromaDB row's ``user_id`` metadata by ``_merge_semantic_with_chroma``,
    WU-1b) — the SAME ownership key ``ChromaSemanticService.search``
    filters on (see ``_index_md_objects``'s docstring)."""
    visible = []
    for item in items:
        if item.get("tier") != "corpus":
            visible.append(item)
            continue
        if item.get("created_by_user_id") == user.user_id:
            visible.append(item)
            continue
        key = item.get("key") or ""
        collection = key.split("/", 1)[0] if "/" in key else key
        if _has_corpus_scope(user, collection, "read"):
            visible.append(item)
    return visible


# Cap per-collection discovery scan so the list endpoint stays snappy
# even when a chroma collection grows large. Operators wanting full
# enumeration should paginate the manifest.
_SEMANTIC_DISCOVERY_LIMIT = 200


@router.get("/semantic")
async def list_semantic(
    collection: str | None = Query(
        None, description="Filter to a single collection if set."
    ),
    include_deleted: bool = Query(False),
    sort: ListSort = Query(
        "created_at",
        description="Sort field: created_at | modified_at | key | size.",
    ),
    order: ListOrder = Query(
        "desc", description="Sort direction. Default desc (most-recent-first)."
    ),
    limit: int = Query(
        _LIST_DEFAULT_LIMIT,
        ge=1,
        le=_LIST_MAX_LIMIT,
        description="Page size (max 500).",
    ),
    offset: int = Query(0, ge=0, description="Zero-based pagination offset."),
    _auth: dict[str, Any] = Security(validate_jwt, scopes=["memory:semantic:read"]),
    user: UserContext = Depends(require_user),
    *,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """List semantic-layer items. Merges the manifest with ChromaDB
    discovery so pre-PR-A vectors (seeded via index-chromadb.py) surface
    alongside operator-created ones. Sort/paginate mirror the other layer
    lists for consistency (backlog #15, R5).

    ADR-062 WU-A1: requires an authenticated user like every sibling
    list/read handler (closes the CS-4 anomaly of a memory-list endpoint
    with no identity at all, so a future audit event can attribute the
    read).

    ADR-062 Phase B (WU-B3): the ChromaDB-DISCOVERED rows (untracked in
    the manifest) are filtered — non-admin sees only its own
    private-tier rows plus corpus-tier rows (`_merge_semantic_with_chroma`
    §3 hole 2).

    ADR-062 Phase B (WU-B4): manifest-TRACKED rows (`entries` below) are
    now ALSO owner-or-corpus scoped (`list_for_layer(caller=user)`) —
    closes the gap the previous docstring flagged as deferred. On top
    of ownership scoping, D3's operator/curator-tier gate additionally
    drops any CORPUS-tier item (tracked or discovered) the caller
    lacks ``memory:corpus:<collection>:read`` for
    (`_filter_corpus_read_gate`)."""
    manifest = get_memory_manifest_service()
    entries: list[ManifestEntry] = await manifest.list_for_layer(
        "semantic", include_deleted=include_deleted, caller=user
    )
    if collection is not None:
        prefix = f"{collection}/"
        entries = [e for e in entries if e.key.startswith(prefix)]
    items = await _merge_semantic_with_chroma(entries, collection, user)
    items = _filter_corpus_read_gate(items, user)
    page, total = _sort_and_paginate(
        items, sort=sort, order=order, limit=limit, offset=offset
    )
    schedule_read_audit(
        background_tasks,
        user=user,
        op="list",
        layer="semantic",
        collection=collection,
    )
    return {"items": page, "total": total, "limit": limit, "offset": offset}


@router.get("/semantic/{collection}/{document_id}")
async def read_semantic(
    collection: str,
    document_id: str,
    _auth: dict[str, Any] = Security(validate_jwt, scopes=["memory:semantic:read"]),
    user: UserContext = Depends(require_user),
    *,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """ADR-062 Phase B: ``service.get_document`` already applies the
    owner-or-corpus predicate (WU-B3 §3 hole 1) — a cross-user private
    doc is a 404, same as a genuine miss (no existence disclosure). On
    top of that, D3 (WU-B4) requires ``memory:corpus:<collection>:read``
    to read a CORPUS-tier doc through this backoffice route — a 403 (not
    404: corpus existence isn't user-specific, so there's nothing to
    hide by returning a miss instead)."""
    service = get_semantic_service()
    manifest = get_memory_manifest_service()
    doc = await service.get_document(user, collection, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="semantic doc not found")
    if doc.metadata.get("tier") == "corpus":
        _require_corpus_scope(user, collection, "read")
    entry: ManifestEntry | None = await manifest.get(
        "semantic", _semantic_key(collection, document_id)
    )
    if entry is not None and not _manifest_visible(entry, user):
        entry = None
    schedule_read_audit(
        background_tasks,
        user=user,
        op="read",
        layer="semantic",
        collection=collection,
        key=document_id,
    )
    return {
        "content": doc.page_content,
        "metadata": doc.metadata,
        "manifest": entry.to_dict() if entry is not None else None,
    }


@router.put("/semantic/{collection}/{document_id}")
async def update_semantic(
    collection: str,
    document_id: str,
    payload: dict[str, Any] = Body(..., description="{text, metadata?, title?}"),
    _scope: dict[str, Any] = Security(validate_jwt, scopes=["memory:semantic:write"]),
    user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    """ADR-062 Phase B (WU-B5 review fix, 2026-08-04): ``metadata`` is
    sanitized the same way as ``create_semantic`` — a caller-supplied
    ``tier``/``user_id`` in the body's ``metadata`` field is stripped
    before it reaches the service, and ``ChromaSemanticService.upsert``
    additionally stamps both unconditionally regardless. This route
    does not accept a promote request at all (no ``tier``/``?promote=``
    parsing) — every update stays private-tier, matching the pre-review
    WU-B5 scope decision (updates were never in the promote-gate call
    site list); the fix here closes the metadata-forgery bypass, not a
    missing feature."""
    text = payload.get("text")
    if not isinstance(text, str):
        raise HTTPException(status_code=400, detail="text is a required string field")
    metadata = _sanitize_semantic_metadata(payload.get("metadata"))
    title = payload.get("title")
    service = get_semantic_service()
    manifest = get_memory_manifest_service()
    try:
        await service.upsert(user, collection, document_id, text, metadata)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    key = _semantic_key(collection, document_id)
    try:
        entry: ManifestEntry = await manifest.record_update(
            layer="semantic",
            key=key,
            size_bytes=len(text.encode("utf-8")),
            user_id=user.user_id,
            title=title,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"semantic doc has no manifest row: {exc}",
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _emit_write_audit(
        user=user,
        op="write",
        layer="semantic",
        collection=collection,
        key=document_id,
    )
    return entry.to_dict()


@router.delete("/semantic/{collection}/{document_id}")
async def delete_semantic(
    collection: str,
    document_id: str,
    hard: bool = Query(False),
    _scope: dict[str, Any] = Security(validate_jwt, scopes=["memory:semantic:write"]),
    user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    """Soft-delete by default; ``?hard=true`` also purges the Chroma
    vector (ADR-062 §6 — same discipline as ``delete_episodic``). The
    manifest tombstone alone is what ``search()``/``search_page()``
    consult to keep a soft-deleted item out of recall (WU-A5)."""
    if hard and "audittrace:admin" not in user.scopes and not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="?hard=true requires audittrace:admin scope",
        )
    service = get_semantic_service()
    manifest = get_memory_manifest_service()
    key = _semantic_key(collection, document_id)
    try:
        entry: ManifestEntry = await manifest.record_delete(
            "semantic", key, user.user_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if hard:
        try:
            await service.delete_document(user, collection, document_id)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    await _emit_write_audit(
        user=user,
        op="delete",
        layer="semantic",
        collection=collection,
        key=document_id,
        detail_extra={"hard": hard},
    )
    return entry.to_dict()


# ── /memory/conversational ──────────────────────────────────────────────────
#
# Layer 3 — chat sessions + interactions persisted in Postgres on the
# `/v1/chat/completions` hot path. Unlike the other three layers,
# conversational data is **per-user**: RLS on the ``sessions`` and
# ``interactions`` tables (migration 005) restricts every query to the
# caller's own ``user_id``. Read-only — sessions are produced by the
# chat path itself, not by operator writes.
#
# Why these endpoints exist on top of the older ``/sessions`` and
# ``/interactions`` audit routes: the audit routes gate on
# ``audittrace:audit`` scope (which carries auditor semantics —
# "see everything in your scope, including across projects"). For an
# end-user wanting to review their own chat history, ``memory:
# conversational:read-own`` is the right scope. The two surfaces serve
# different roles intentionally.


@router.get("/conversational", response_model=ConversationalListResponse)
async def list_conversational_sessions(
    project: str | None = Query(None, description="Filter by project tag (ADR-029)."),
    since: str | None = Query(
        None,
        description=(
            "ISO date string. Only sessions with ``date >= since`` are returned."
        ),
    ),
    summarised: bool | None = Query(
        None,
        description=(
            "true → only rows with ``summarized_at`` populated; "
            "false → only un-summarised rows; omit → both."
        ),
    ),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    _scope: dict[str, Any] = Security(
        validate_jwt, scopes=["memory:conversational:read-own"]
    ),
    _user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    """List the caller's own chat sessions, ordered by date DESC.

    RLS-scoped: the caller only sees rows whose ``user_id`` matches
    their JWT ``sub``. No cross-user reads are possible from this
    endpoint, even if the caller crafts a ``user_id`` filter — the
    ``after_begin`` listener sets ``app.current_user_id`` and the
    Postgres policy on ``sessions`` filters it.
    """
    try:
        pg = get_postgres_factory()
    except Exception as exc:
        logger.error(
            "Conversational endpoint unavailable — PostgresFactory not registered"
        )
        raise HTTPException(
            status_code=503, detail="Conversational store unavailable"
        ) from exc

    session_factory = pg.get_session_factory()
    async with session_factory() as db:
        # Phase 4 RLS: the sync after_begin listener no-ops on async
        # engines, so async routes set the per-user GUC explicitly here
        # (Postgres-only; no-op on SQLite). Reads the request-scoped
        # ContextVar populated by ``require_user``.
        await set_rls_user_id(db, current_user_id())
        stmt = select(SessionRow)
        if project is not None:
            stmt = stmt.where(SessionRow.project == project)
        if since is not None:
            stmt = stmt.where(SessionRow.date >= since)
        if summarised is True:
            stmt = stmt.where(SessionRow.summarized_at.is_not(None))
        elif summarised is False:
            stmt = stmt.where(SessionRow.summarized_at.is_(None))
        total = (
            await db.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        rows = (
            (
                await db.execute(
                    stmt.order_by(SessionRow.date.desc()).offset(offset).limit(limit)
                )
            )
            .scalars()
            .all()
        )

        # Serialise inside the session scope (#364) — ORM instances are
        # detached once the block exits, so building the payload here keeps
        # the handler independent of ``expire_on_commit``.
        items = [
            {
                "id": r.id,
                "project": r.project,
                "date": r.date,
                "model": r.model,
                "summary": r.summary,
                "key_points": r.key_points,
                "summarized_at": (
                    r.summarized_at.isoformat() if r.summarized_at is not None else None
                ),
                "user_id": r.user_id,
            }
            for r in rows
        ]

    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/conversational/{session_id}", response_model=ConversationalDetailResponse)
async def read_conversational_session(
    session_id: str,
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    _scope: dict[str, Any] = Security(
        validate_jwt, scopes=["memory:conversational:read-own"]
    ),
    _user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    """Fetch one session's metadata + its interactions, ordered by
    timestamp ASC (chronological). RLS gates visibility — calling on a
    session_id that exists but belongs to another user returns 404
    (not 403) so the caller can't probe for foreign session ids.
    """
    try:
        pg = get_postgres_factory()
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Conversational store unavailable"
        ) from exc

    session_factory = pg.get_session_factory()
    async with session_factory() as db:
        # Phase 4 RLS GUC for the async path (see list route above).
        await set_rls_user_id(db, current_user_id())
        session_row = (
            await db.execute(select(SessionRow).where(SessionRow.id == session_id))
        ).scalar_one_or_none()
        if session_row is None:
            raise HTTPException(status_code=404, detail="session not found")

        interactions = (
            (
                await db.execute(
                    select(InteractionRow)
                    .where(InteractionRow.session_id == session_id)
                    .order_by(InteractionRow.timestamp.asc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

        # Serialise inside the session scope (#364) — both `session_row`
        # and `interactions` are ORM instances that detach when the block
        # exits.
        session_payload = {
            "id": session_row.id,
            "project": session_row.project,
            "date": session_row.date,
            "model": session_row.model,
            "summary": session_row.summary,
            "key_points": session_row.key_points,
            "summarized_at": (
                session_row.summarized_at.isoformat()
                if session_row.summarized_at is not None
                else None
            ),
            "user_id": session_row.user_id,
        }
        interaction_payload = [
            {
                "id": r.id,
                # `timestamp` is stored as a String column in the
                # `interactions` table (migration 005), not a TIMESTAMP.
                # Pass it through as-is — schema-driven contract.
                "timestamp": r.timestamp,
                "session_id": r.session_id,
                "source": r.source,
                "project": r.project,
                # The ORM columns are `question` / `answer` (migration 005's
                # original names). Surface as `question` / `answer` here so
                # the response shape mirrors the row faithfully — the webui
                # will rename for display, not the API.
                "question": r.question,
                "answer": r.answer,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "model": r.model,
                "status": r.status,
                "failure_class": r.failure_class,
                "error_detail": r.error_detail,
                "duration_ms": r.duration_ms,
                "trace_id": r.trace_id,
            }
            for r in interactions
        ]

    return {
        "session": session_payload,
        "interactions": interaction_payload,
        "total": len(interaction_payload),
    }
