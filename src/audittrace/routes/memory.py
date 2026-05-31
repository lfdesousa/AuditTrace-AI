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

import contextlib
import hashlib
import io
import logging
import time
from enum import StrEnum
from typing import Any

from fastapi import (
    APIRouter,
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
from audittrace.services.embedder import SINGLETON_EMBEDDER
from audittrace.services.memory_manifest import ManifestEntry

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

    The ABC's ``list_objects`` paginates AND recurses transparently
    (MinIO backend uses ``recursive=True``; boto3 paginator walks every
    page). Before ADR-006 this helper also passed ``recursive=True``
    explicitly to the minio-py client; that kwarg no longer exists on
    the ABC because the contract guarantees full-subtree walking.
    """
    objects: list[dict[str, str]] = []
    for obj in client.list_objects(bucket, prefix=prefix):
        key = obj.object_name or ""
        filename = key.rsplit("/", 1)[-1] if "/" in key else key
        if filename:
            objects.append({"key": key, "filename": filename})
    return objects


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
      asynchronously in content-control once the verdict is clean.

    * **Non-PDF uploads** (markdown skills, ADRs, plain text)
      keep the existing synchronous path: PUT to
      ``{layer}/{filename}`` and return HTTP 200 with the
      direct-write summary.

    Authorization (per-layer): the caller's JWT must carry
    ``memory:<layer>:write`` matching the ``layer`` query parameter
    (or ``audittrace:admin``). A token with ``memory:procedural:write``
    cannot upload to ``layer=episodic`` and vice-versa.
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
    # ADR-006 — effective bucket switches between MinIO (default) and
    # AWS S3 based on AUDITTRACE_OBJECT_STORAGE_BACKEND. The minio
    # variable name in settings is kept for backwards compatibility.
    bucket = (
        settings.aws_bucket
        if settings.object_storage_backend == "aws"
        else settings.minio_shared_bucket
    )
    key = f"{layer.value}/{target_filename}"

    try:
        minio_client.put_object(
            bucket,
            key,
            io.BytesIO(content),
            length=len(content),
        )
    except Exception as exc:
        logger.error("MinIO upload failed: %s", exc)
        raise HTTPException(
            status_code=502, detail="Object storage write failed"
        ) from exc

    logger.info(
        "Uploaded %s (%d bytes) to s3://%s/%s",
        target_filename,
        len(content),
        bucket,
        key,
    )

    return {
        "status": "uploaded",
        "bucket": bucket,
        "key": key,
        "size_bytes": len(content),
    }


# ── POST /memory/index ──────────────────────────────────────────────────────


def _upsert_in_batches(
    collection: Any,
    ids: list[str],
    documents: list[str],
    metadatas: list[dict[str, Any]],
) -> None:
    """``collection.upsert`` in fixed-size slices.

    Upsert (vs. add) so the single-file ?file= mode is idempotent —
    a client looping per file can re-run the same call without
    duplicate-id errors. Splitting per file into _INDEX_BATCH_SIZE
    chunks bounds the in-flight payload size to ChromaDB.
    """
    for start in range(0, len(ids), _INDEX_BATCH_SIZE):
        end = min(start + _INDEX_BATCH_SIZE, len(ids))
        collection.upsert(
            ids=ids[start:end],
            documents=documents[start:end],
            metadatas=metadatas[start:end],
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


def _index_md_objects(
    collection: Any,
    minio_client: Any,
    bucket: str,
    objects: list[dict[str, str]],
    col_name: str,
    category: str,
) -> int:
    """Stream-index ``.md`` files into *collection*.

    Per-file: read → chunk → add → drop. Avoids holding the cross-corpus
    document list in memory.
    """
    total = 0
    for obj in objects:
        if not obj["filename"].endswith(".md"):
            continue
        raw = _read_minio_object(minio_client, bucket, obj["key"])
        if raw is None:
            continue
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
            }
            for i in range(len(chunks))
        ]
        if category == "procedural" and obj["filename"].startswith("SKILL-"):
            skill_name = obj["filename"].replace("SKILL-", "").replace(".md", "")
            for m in metadatas:
                m["skill"] = skill_name
        _upsert_in_batches(collection, ids, chunks, metadatas)
        total += len(chunks)
    return total


# ── Tier-C PDF metadata + corrupted-file classification (ADR-056) ────────


@router.post("/index")
def index_memory(
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
    if file is None:
        _require_admin(user, "bulk /memory/index rebuild")
    else:
        try:
            layer_str, _ = file.split("/", 1)
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
    bucket = (
        settings.aws_bucket
        if settings.object_storage_backend == "aws"
        else settings.minio_shared_bucket
    )
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
        single_obj: dict[str, str] = {
            "key": file,
            "filename": file.rsplit("/", 1)[-1],
        }
        if file.startswith("episodic/"):
            episodic_objects = [single_obj]
        elif file.startswith("procedural/"):
            procedural_objects = [single_obj]
        else:
            raise HTTPException(
                status_code=400,
                detail="file= must start with 'episodic/' or 'procedural/'",
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
        if not single_file_mode and not dry_run:
            with contextlib.suppress(Exception):
                chroma_client.delete_collection(col_name)
        collection = chroma_client.get_or_create_collection(
            name=col_name,
            embedding_function=SINGLETON_EMBEDDER,
        )

        chunk_count = 0
        if col_name in ("decisions", "semantic"):
            chunk_count += _index_md_objects(
                collection,
                minio_client,
                bucket,
                episodic_objects,
                col_name,
                category="episodic",
            )
        if col_name in ("skills", "semantic"):
            chunk_count += _index_md_objects(
                collection,
                minio_client,
                bucket,
                procedural_objects,
                col_name,
                category="procedural",
            )
        if col_name == "ai_research_papers":
            # Tier-B item #22 + tier-C items #23/#24 (ADR-056): thread
            # the manifest service AND the per-document details
            # accumulator AND the dry-run flag. The manifest service
            # writes Postgres rows (skipped under dry_run); the details
            # accumulator collects per-doc outcomes for the
            # ?details=true response shape.
            manifest_service = get_memory_manifest_service()
            chunk_count += _index_pdf_objects(
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
            )
            chunk_count += _index_pdf_objects(
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
            )

        results[col_name] = chunk_count
        total_chunks += chunk_count
        logger.info("Indexed %s: %d chunks", col_name, chunk_count)

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
    _scope: dict[str, Any] = Security(validate_jwt, scopes=["memory:episodic:write"]),
    user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    """Create an ADR. Body: ``{filename, content, title?}``.

    Idempotent on the manifest: re-creating a soft-deleted item revives
    it (clearing ``deleted_at_ms``) and bumps ``modified_at_ms``.
    """
    filename = payload.get("filename")
    content = payload.get("content")
    if not isinstance(filename, str) or not isinstance(content, str):
        raise HTTPException(
            status_code=400,
            detail="filename and content are required string fields",
        )
    _validate_filename_or_400(filename, "episodic")
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
    )
    return entry.to_dict()


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
    """
    items: list[dict[str, Any]] = [e.to_dict() for e in visible_entries]

    manifest = get_memory_manifest_service()
    all_known: list[ManifestEntry] = await manifest.list_for_layer(
        layer, include_deleted=True
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
        items.append(
            {
                "id": None,
                "layer": layer,
                "key": filename,
                "title": doc.metadata.get("title")
                or doc.metadata.get("skill")
                or filename,
                "size_bytes": len(doc.page_content.encode("utf-8")),
                "created_at_ms": None,
                "modified_at_ms": None,
                "created_by_user_id": None,
                "modified_by_user_id": None,
                "deleted_at_ms": None,
                "deleted_by_user_id": None,
                "discovered": True,
            }
        )
    return items


@router.get("/episodic")
async def list_episodic(
    include_deleted: bool = Query(False),
    _auth: dict[str, Any] = Security(validate_jwt, scopes=["memory:episodic:read"]),
    user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    """List ADRs. Merges manifest rows with S3 objects so pre-PR-A
    content (uploaded via /memory/upload or seeded via index-chromadb)
    surfaces alongside operator-created items.

    ``include_deleted=true`` returns soft-deleted manifest rows; it
    has no effect on discovered entries (they have no soft-delete
    state)."""
    manifest = get_memory_manifest_service()
    entries: list[ManifestEntry] = await manifest.list_for_layer(
        "episodic", include_deleted=include_deleted
    )
    items = await _merge_layer_items_with_s3("episodic", entries, user)
    return {"items": items, "total": len(items)}


@router.get("/episodic/{filename}")
async def read_episodic(
    filename: str,
    _auth: dict[str, Any] = Security(validate_jwt, scopes=["memory:episodic:read"]),
    user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    """Read ADR content + manifest metadata."""
    _validate_filename_or_400(filename, "episodic")
    service = get_episodic_service()
    manifest = get_memory_manifest_service()
    doc = await service.read(user, filename)
    if doc is None:
        raise HTTPException(status_code=404, detail="ADR not found")
    entry: ManifestEntry | None = await manifest.get("episodic", filename)
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
    """Soft-delete by default; ``?hard=true`` also purges the S3 object."""
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
    return entry.to_dict()


# ── /memory/procedural ──────────────────────────────────────────────────────


@router.post("/procedural")
async def create_procedural(
    payload: dict[str, Any] = Body(..., description="{filename, content, title?}"),
    _scope: dict[str, Any] = Security(validate_jwt, scopes=["memory:procedural:write"]),
    user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    """Create a SKILL document. Body shape mirrors POST /memory/episodic."""
    filename = payload.get("filename")
    content = payload.get("content")
    if not isinstance(filename, str) or not isinstance(content, str):
        raise HTTPException(
            status_code=400,
            detail="filename and content are required string fields",
        )
    _validate_filename_or_400(filename, "procedural")
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
    )
    return entry.to_dict()


@router.get("/procedural")
async def list_procedural(
    include_deleted: bool = Query(False),
    _auth: dict[str, Any] = Security(validate_jwt, scopes=["memory:procedural:read"]),
    user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    """List SKILLs. Same merge-with-S3 semantics as `/memory/episodic`
    so pre-PR-A items appear alongside operator-created ones."""
    manifest = get_memory_manifest_service()
    entries: list[ManifestEntry] = await manifest.list_for_layer(
        "procedural", include_deleted=include_deleted
    )
    items = await _merge_layer_items_with_s3("procedural", entries, user)
    return {"items": items, "total": len(items)}


@router.get("/procedural/{filename}")
async def read_procedural(
    filename: str,
    _auth: dict[str, Any] = Security(validate_jwt, scopes=["memory:procedural:read"]),
    user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    _validate_filename_or_400(filename, "procedural")
    service = get_procedural_service()
    manifest = get_memory_manifest_service()
    doc = await service.read(user, filename)
    if doc is None:
        raise HTTPException(status_code=404, detail="SKILL not found")
    entry: ManifestEntry | None = await manifest.get("procedural", filename)
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
    return entry.to_dict()


@router.delete("/procedural/{filename}")
async def delete_procedural(
    filename: str,
    hard: bool = Query(False),
    _scope: dict[str, Any] = Security(validate_jwt, scopes=["memory:procedural:write"]),
    user: UserContext = Depends(require_user),
) -> dict[str, Any]:
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
        ..., description="{collection, document_id, text, metadata?, title?}"
    ),
    _scope: dict[str, Any] = Security(validate_jwt, scopes=["memory:semantic:write"]),
    user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    """Upsert a semantic-layer document. The collection's embedding
    function (configured at chart install time) handles vectorisation."""
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
    metadata = payload.get("metadata")
    title = payload.get("title")
    service = get_semantic_service()
    manifest = get_memory_manifest_service()
    try:
        await service.upsert(user, collection, document_id, text, metadata)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    entry: ManifestEntry = await manifest.record_create(
        layer="semantic",
        key=_semantic_key(collection, document_id),
        title=title,
        size_bytes=len(text.encode("utf-8")),
        user_id=user.user_id,
    )
    return entry.to_dict()


async def _merge_semantic_with_chroma(
    visible_entries: list[ManifestEntry],
    collection: str | None,
) -> list[dict[str, Any]]:
    """Same shape as `_merge_layer_items_with_s3` for the semantic layer.

    ChromaDB has its own listing semantics — `collection.get()` with
    no `ids=` returns every doc. We cap the per-collection scan at
    ``_SEMANTIC_DISCOVERY_LIMIT`` rows to keep the list endpoint snappy
    on collections that grow large; operators who need to browse a
    large semantic store can paginate via the manifest's offset/limit
    once they've created tracked rows for the items they care about.
    """
    items: list[dict[str, Any]] = [e.to_dict() for e in visible_entries]

    manifest = get_memory_manifest_service()
    all_known: list[ManifestEntry] = await manifest.list_for_layer(
        "semantic", include_deleted=True
    )
    known_keys = {e.key for e in all_known}

    chroma = get_chromadb()
    # Discovery target collections: filter param if given, else all
    # collections the chroma client knows about (capped — see
    # _SEMANTIC_DISCOVERY_LIMIT). Reading every collection on every
    # list call would scale poorly past a few collections.
    target_cols: list[str]
    if collection is not None:
        target_cols = [collection]
    else:
        try:
            target_cols = [c.name for c in chroma.list_collections()][:5]
        except Exception as exc:
            logger.warning("ChromaDB list_collections failed: %s", exc)
            return items

    for col_name in target_cols:
        try:
            col = chroma.get_or_create_collection(
                name=col_name,
                embedding_function=SINGLETON_EMBEDDER,
            )
            res = col.get(
                limit=_SEMANTIC_DISCOVERY_LIMIT,
                include=["documents", "metadatas"],
            )
        except Exception as exc:
            logger.warning("ChromaDB get failed for collection %s: %s", col_name, exc)
            continue
        ids = res.get("ids") or []
        docs = res.get("documents") or []
        metas = res.get("metadatas") or []
        for i, doc_id in enumerate(ids):
            key = _semantic_key(col_name, doc_id)
            if key in known_keys:
                continue
            content = docs[i] if i < len(docs) else ""
            meta = metas[i] if i < len(metas) and metas[i] else {}
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
                    "created_by_user_id": None,
                    "modified_by_user_id": None,
                    "deleted_at_ms": None,
                    "deleted_by_user_id": None,
                    "discovered": True,
                }
            )
    return items


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
    _auth: dict[str, Any] = Security(validate_jwt, scopes=["memory:semantic:read"]),
) -> dict[str, Any]:
    """List semantic-layer items. Merges the manifest with ChromaDB
    discovery so pre-PR-A vectors (seeded via index-chromadb.py)
    surface alongside operator-created ones."""
    manifest = get_memory_manifest_service()
    entries: list[ManifestEntry] = await manifest.list_for_layer(
        "semantic", include_deleted=include_deleted
    )
    if collection is not None:
        prefix = f"{collection}/"
        entries = [e for e in entries if e.key.startswith(prefix)]
    items = await _merge_semantic_with_chroma(entries, collection)
    return {"items": items, "total": len(items)}


@router.get("/semantic/{collection}/{document_id}")
async def read_semantic(
    collection: str,
    document_id: str,
    _auth: dict[str, Any] = Security(validate_jwt, scopes=["memory:semantic:read"]),
    user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    service = get_semantic_service()
    manifest = get_memory_manifest_service()
    doc = await service.get_document(user, collection, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="semantic doc not found")
    entry: ManifestEntry | None = await manifest.get(
        "semantic", _semantic_key(collection, document_id)
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
    text = payload.get("text")
    if not isinstance(text, str):
        raise HTTPException(status_code=400, detail="text is a required string field")
    metadata = payload.get("metadata")
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
    return entry.to_dict()


@router.delete("/semantic/{collection}/{document_id}")
async def delete_semantic(
    collection: str,
    document_id: str,
    hard: bool = Query(False),
    _scope: dict[str, Any] = Security(validate_jwt, scopes=["memory:semantic:write"]),
    user: UserContext = Depends(require_user),
) -> dict[str, Any]:
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

    return {
        "items": [
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
        ],
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

    return {
        "session": {
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
        },
        "interactions": [
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
        ],
        "total": len(interactions),
    }
