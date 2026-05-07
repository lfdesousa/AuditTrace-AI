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
from urllib.parse import urlparse

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    HTTPException,
    Query,
    Security,
    UploadFile,
)
from minio import Minio

from audittrace.auth import require_user, validate_jwt
from audittrace.config import get_settings
from audittrace.db.models import InteractionRecord as InteractionRow
from audittrace.db.models import SessionRecord as SessionRow
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
from audittrace.services.embedder import SINGLETON_EMBEDDER
from audittrace.services.memory_manifest import ManifestEntry

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
    """Build a MinIO client from the configured settings.

    Uses the same pattern as ``dependencies._create_minio_client`` but
    always returns a client (raises on failure rather than returning None).
    """
    settings = get_settings()
    parsed = urlparse(settings.minio_url)
    endpoint = parsed.netloc or parsed.path
    secure = parsed.scheme == "https"
    return Minio(
        endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=secure,
    )


def _list_objects_from_minio(
    client: Any, bucket: str, prefix: str
) -> list[dict[str, str]]:
    """List all objects under *prefix* in *bucket*.

    Returns a list of ``{"key": ..., "filename": ...}`` dicts.

    ``recursive=True`` walks the full subtree. Without it, MinIO's
    default returns only direct children of *prefix*, hiding files
    in subdirectories. The pre-existing .md corpus was flat
    (``episodic/ADR-NNN.md``) and got away with non-recursive
    listing; the ai_research_papers corpus uses nested paths
    (``episodic/papers/books/foo.pdf``) and would otherwise return
    zero objects (caught live, 2026-05-06).
    """
    objects: list[dict[str, str]] = []
    for obj in client.list_objects(bucket, prefix=prefix, recursive=True):
        key = obj.object_name or ""
        filename = key.rsplit("/", 1)[-1] if "/" in key else key
        if filename:
            objects.append({"key": key, "filename": filename})
    return objects


# ── POST /memory/upload ─────────────────────────────────────────────────────


@router.post("/upload")
async def upload_memory_file(
    file: UploadFile = File(...),
    layer: MemoryLayer = Query(...),
    filename: str | None = Query(None),
    _auth: dict[str, Any] = Security(validate_jwt, scopes=["audittrace:admin"]),
) -> dict[str, Any]:
    """Upload a file to MinIO under the specified memory layer.

    The file lands in the ``memory-shared`` bucket at
    ``{layer}/{filename}``.  If *filename* is omitted the upload's
    original filename is used.
    """
    settings = get_settings()
    bucket = settings.minio_shared_bucket
    target_filename = filename or file.filename or "unnamed"
    key = f"{layer.value}/{target_filename}"

    content = await file.read()
    minio_client = _get_minio_client()

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


def _page_bbox(page: Any) -> tuple[float, float, float, float]:
    """Return (x0, y0, x1, y1) in PDF user-space units for *page*.

    Page-level bbox in v1 — every chunk on a page shares the page's
    rectangle. Per-block bbox (one rect per text block) is a future
    item (#4 in docs/architecture/pdf-ingestion-gaps.md, multi-column
    reading order). Falls back to zeros if rect access raises — keeps
    the metadata schema stable on malformed PDFs.
    """
    try:
        rect = page.rect
        return (float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1))
    except (AttributeError, TypeError, ValueError):
        return (0.0, 0.0, 0.0, 0.0)


# pymupdf.PDF_ANNOT_REDACT — the integer code for redaction-type
# annotations in the PDF spec. Inlined to avoid importing pymupdf at
# module-load time (the heavy import is gated to inside
# _index_pdf_objects). Matches pymupdf.PDF_ANNOT_REDACT == 12 across
# 1.24+ versions; the type tuple's [1] string ("Redact") is also
# checked as a belt-and-braces fallback.
_PDF_ANNOT_REDACT = 12


def _redaction_rects(page: Any) -> list[Any]:
    """Return list of redaction-annotation rects on *page*.

    Empty list if the page has no annotations or no redactions.
    Robust to ``page.annots()`` returning ``None`` or raising — both
    seen in the wild on malformed PDFs.
    """
    try:
        annots = page.annots() or []
    except Exception:
        return []
    rects: list[Any] = []
    for annot in annots:
        annot_type = getattr(annot, "type", None)
        if not annot_type:
            continue
        # type is a (int_code, name_str) tuple in pymupdf.
        type_code = annot_type[0] if len(annot_type) > 0 else None
        type_name = annot_type[1] if len(annot_type) > 1 else None
        if type_code == _PDF_ANNOT_REDACT or type_name == "Redact":
            rects.append(annot.rect)
    return rects


def _rects_intersect(a: tuple[float, float, float, float], b: Any) -> bool:
    """Standard rect-overlap test on (x0, y0, x1, y1) pairs.

    Inclusive on edges (two rects touching at a single line still
    count as intersecting — conservative for redaction safety: when
    in doubt, drop the block).
    """
    bx0, by0, bx1, by1 = float(b.x0), float(b.y0), float(b.x1), float(b.y1)
    return not (a[2] < bx0 or a[0] > bx1 or a[3] < by0 or a[1] > by1)


# Module-level lazy singleton for pyhanko's ValidationContext.
# Building a ValidationContext is cheap, but the OCSP/CRL session
# state inside it is process-resident and amortises across many
# files. Per PYTHON-ENGINEERING §2 — "ML models, ONNX sessions,
# HTTPX/Boto3 clients, connection pools — these are process-resident
# infrastructure. They load once, live forever." Same shape as
# `_SingletonOnnxEmbedder` in services/embedder.py: double-checked
# locking, fast read path stays lock-free, init runs exactly once.
_VALIDATION_CONTEXT: Any = None
_VC_LOCK = __import__("threading").Lock()
_VC_TRUST_STORE_PATH: str = ""  # tracks which trust store the cached VC was built with


def _get_validation_context(trust_store_path: str) -> Any:
    """Return a process-cached pyhanko ValidationContext.

    First call builds it from the configured trust store; subsequent
    calls return the cached instance. If *trust_store_path* changes
    between calls (operator updated Settings without restart), the
    context is rebuilt — a deliberate cache invalidation point.
    """
    global _VALIDATION_CONTEXT, _VC_TRUST_STORE_PATH
    # Fast path — lock-free read.
    if _VALIDATION_CONTEXT is not None and _VC_TRUST_STORE_PATH == trust_store_path:
        return _VALIDATION_CONTEXT
    # Slow path — race-safe init.
    with _VC_LOCK:
        if _VALIDATION_CONTEXT is not None and _VC_TRUST_STORE_PATH == trust_store_path:
            return _VALIDATION_CONTEXT
        from pyhanko_certvalidator import ValidationContext

        kwargs: dict[str, Any] = {}
        if trust_store_path:
            # Operator-provided extra trust roots (PEM bundle).
            try:
                with open(trust_store_path, "rb") as fh:
                    pem_bytes = fh.read()
                kwargs["trust_roots"] = [pem_bytes]
            except OSError as exc:
                logger.warning(
                    "Could not read pdf_signature_trust_store=%r: %s; "
                    "falling back to system trust roots",
                    trust_store_path,
                    exc,
                )
        # Defaults: certifi + system trust + pyhanko's built-in
        # algorithm_usage_policy (rejects MD5, SHA-1, RSA<2048 with
        # warnings or hard-fail per pyhanko's current defaults).
        # IAM §"Algorithm Security Rules" — never accept weak algs.
        _VALIDATION_CONTEXT = ValidationContext(**kwargs)
        _VC_TRUST_STORE_PATH = trust_store_path
        return _VALIDATION_CONTEXT


def _pdf_signature_status(
    raw: bytes,
    *,
    enabled: bool,
    trust_store_path: str,
) -> tuple[str, int]:
    """Return ``(status, signers_count)`` for *raw* PDF bytes.

    Status taxonomy (every chunk metadata carries one of these via
    ``signature_status``):

    * ``"check_skipped"`` — operator disabled the check via
      ``AUDITTRACE_PDF_SIGNATURE_CHECK_ENABLED=false``.
    * ``"check_unavailable"`` — pyhanko not importable (graceful
      degradation per PYTHON-ENGINEERING §4).
    * ``"check_failed"`` — pyhanko raised on this file (malformed
      PDF, network failure during OCSP, unexpected exception).
      Distinct from ``"signed_invalid"`` so auditors can separate
      "we tried and the document broke" from "the document said it
      was signed and the signature was bad."
    * ``"none"`` — file parsed successfully and contains zero
      embedded signatures.
    * ``"signed_valid"`` — every signature is intact, valid, AND
      trusted by the configured trust store.
    * ``"signed_invalid"`` — at least one signature exists but
      fails validation (untrusted chain, expired cert, etc.) —
      content itself is intact.
    * ``"signed_tampered"`` — at least one signature shows the
      content was modified after signing (``intact=False``). The
      strongest negative signal: the file is provably altered
      from what was signed.

    Detect-and-record only in v1 — never reject. The chunk metadata
    field lets auditors query for any non-clean state without changing
    the ingestion contract.
    """
    if not enabled:
        return ("check_skipped", 0)
    try:
        from pyhanko.pdf_utils.reader import PdfFileReader
        from pyhanko.sign.validation import validate_pdf_signature
    except ImportError:
        return ("check_unavailable", 0)
    try:
        reader = PdfFileReader(io.BytesIO(raw))
        signatures = list(reader.embedded_signatures)
        if not signatures:
            return ("none", 0)
        vc = _get_validation_context(trust_store_path)
        any_tampered = False
        any_invalid = False
        for emb in signatures:
            status = validate_pdf_signature(emb, vc)
            if not getattr(status, "intact", True):
                any_tampered = True
                continue
            if not getattr(status, "valid", True):
                any_invalid = True
                continue
            if not getattr(status, "trusted", True):
                any_invalid = True
        if any_tampered:
            return ("signed_tampered", len(signatures))
        if any_invalid:
            return ("signed_invalid", len(signatures))
        return ("signed_valid", len(signatures))
    except Exception as exc:
        logger.warning(
            "Signature validation raised on document: %s",
            exc,
            extra={"reason": "signature_check_exception", "error": repr(exc)},
        )
        return ("check_failed", 0)


def _text_clipped_around_redactions(page: Any, redaction_rects: list[Any]) -> str:
    """Join block-level text from blocks NOT intersecting any redaction.

    Uses ``page.get_text("blocks")`` which returns
    ``(x0, y0, x1, y1, text, block_no, block_type)`` tuples. Blocks
    whose bbox intersects any redaction rect are dropped — the rest
    are joined in their existing reading order. This is the
    clip-extract path for the AUDITTRACE_PDF_REDACTION_POLICY setting.
    """
    blocks = page.get_text("blocks")
    safe_text: list[str] = []
    for block in blocks:
        # Each block tuple: (x0, y0, x1, y1, text, ...). Defensive
        # indexing — pymupdf has stayed stable on this shape, but
        # downstream changes shouldn't crash extraction.
        if len(block) < 5:
            continue
        bbox = (
            float(block[0]),
            float(block[1]),
            float(block[2]),
            float(block[3]),
        )
        text = block[4]
        if not isinstance(text, str) or not text.strip():
            continue
        if any(_rects_intersect(bbox, r) for r in redaction_rects):
            continue
        safe_text.append(text)
    return "\n".join(safe_text)


def _index_pdf_objects(
    collection: Any,
    minio_client: Any,
    bucket: str,
    objects: list[dict[str, str]],
    col_name: str,
    category: str,
    layer_prefix: str,
    user_id: str,
    ingestion_ts_ms: int,
) -> int:
    """Stream-index ``.pdf`` files into *collection*, per-page.

    Each page yields one or more text chunks; embedding happens per
    page in _INDEX_BATCH_SIZE slices. The pymupdf ``Document`` is
    opened in a ``with`` block so its internal C-level page cache
    is released deterministically when the block exits — important
    for the per-file client loop where each request must come down
    cleanly before the next one starts.

    *layer_prefix* (``"episodic/"`` / ``"procedural/"``) is stripped
    from the MinIO key to produce a stable ``source_key`` metadata
    field — useful for disambiguating same-name files across folders
    (e.g. two ``main.pdf`` in different paper subdirs).

    *user_id* + *ingestion_ts_ms* propagate into every chunk's
    metadata as ``ingested_by_user_id`` and ``ingestion_ts_ms`` —
    per-chunk reconstructibility per gap-inventory item #21
    (docs/architecture/pdf-ingestion-gaps.md §2.6). Both ChromaDB
    and Postgres can answer "who ingested this chunk and when" from
    the chunk row alone.
    """
    import pymupdf  # heavy import; only load when ai_research_papers is requested

    settings = get_settings()
    max_size_bytes = settings.pdf_max_size_mb * 1024 * 1024
    total = 0
    for obj in objects:
        if not obj["filename"].lower().endswith(".pdf"):
            continue
        raw = _read_minio_object(minio_client, bucket, obj["key"])
        if raw is None:
            continue
        # Item #18 — bomb defense layer 1: raw byte-size cap. Catches
        # the simplest case (operator drag-drops a 2 GiB file). The
        # check fires before pymupdf.open so a small file claiming
        # to span a giant document never instantiates the parser.
        if len(raw) > max_size_bytes:
            logger.warning(
                "PDF %s rejected: %d bytes exceeds pdf_max_size_mb=%d",
                obj["key"],
                len(raw),
                settings.pdf_max_size_mb,
                extra={
                    "file": obj["key"],
                    "reason": "max_size",
                    "size_bytes": len(raw),
                    "cap_bytes": max_size_bytes,
                },
            )
            continue
        source_key = (
            obj["key"][len(layer_prefix) :]
            if obj["key"].startswith(layer_prefix)
            else obj["key"]
        )
        # SHA-256 of the raw bytes is the canonical document identity
        # for the entire downstream lifecycle — same file produces the
        # same hash regardless of MinIO key, letting auditors prove the
        # indexed content matches a specific version of the file. Item
        # #21: document_hash field on every chunk.
        document_hash = hashlib.sha256(raw).hexdigest()
        # Item #12 — signature validation. Computed once per file
        # (cert-chain checks are network-bound; doing them per-chunk
        # would multiply OCSP/CRL load by chunks-per-doc). Every chunk
        # of this file carries the same ``signature_status`` value.
        signature_status, _signers_count = _pdf_signature_status(
            raw,
            enabled=settings.pdf_signature_check_enabled,
            trust_store_path=settings.pdf_signature_trust_store,
        )
        try:
            # Assign through Any so mypy doesn't flag pymupdf.open's
            # untyped Document return; the project's `disallow_untyped_calls`
            # would otherwise reject the with-context here.
            doc_factory: Any = pymupdf.open
            with doc_factory(stream=raw, filetype="pdf") as doc:
                # Item #18 — bomb defense layer 2: declared-shape caps.
                # Read both page_count and xref_length from the document
                # catalogue WITHOUT decompressing any stream — these are
                # cheap dictionary lookups. A bomb declaring billions of
                # pages or millions of xrefs is rejected here before any
                # page is ever rendered.
                if doc.page_count > settings.pdf_max_pages:
                    logger.warning(
                        "PDF %s rejected: page_count=%d exceeds pdf_max_pages=%d",
                        obj["key"],
                        doc.page_count,
                        settings.pdf_max_pages,
                        extra={
                            "file": obj["key"],
                            "reason": "max_pages",
                            "page_count": doc.page_count,
                            "cap": settings.pdf_max_pages,
                        },
                    )
                    continue
                xref_count = doc.xref_length()
                if xref_count > settings.pdf_max_xref_count:
                    logger.warning(
                        "PDF %s rejected: xref_count=%d exceeds pdf_max_xref_count=%d",
                        obj["key"],
                        xref_count,
                        settings.pdf_max_xref_count,
                        extra={
                            "file": obj["key"],
                            "reason": "max_xref",
                            "xref_count": xref_count,
                            "cap": settings.pdf_max_xref_count,
                        },
                    )
                    continue
                # Item #18 — bomb defense layer 3: wall-clock budget.
                # Page-boundary granularity (signal.alarm doesn't work
                # in FastAPI's worker-thread pool). A single pathological
                # page can still spike past the cap mid-call, but the
                # total stays bounded to (timeout + one-page latency).
                parse_start = time.monotonic()
                for page_num, page in enumerate(doc, start=1):
                    if (
                        time.monotonic() - parse_start
                        > settings.pdf_parse_timeout_seconds
                    ):
                        logger.warning(
                            "PDF %s parse aborted: exceeded pdf_parse_timeout_seconds=%d",
                            obj["key"],
                            settings.pdf_parse_timeout_seconds,
                            extra={
                                "file": obj["key"],
                                "reason": "parse_timeout",
                                "pages_processed": page_num - 1,
                            },
                        )
                        break
                    # Item #8 — unflattened redaction handling. Detect
                    # redaction annotations BEFORE pulling text so the
                    # reject path never instantiates the redacted
                    # content stream, and the clip-extract path can
                    # filter blocks against the redaction rects.
                    redaction_rects = _redaction_rects(page)
                    redaction_status = "none"
                    if redaction_rects:
                        if settings.pdf_redaction_policy == "reject":
                            logger.warning(
                                "PDF %s rejected at page %d: %d unflattened "
                                "redaction(s); pdf_redaction_policy=reject",
                                obj["key"],
                                page_num,
                                len(redaction_rects),
                                extra={
                                    "file": obj["key"],
                                    "page": page_num,
                                    "reason": "unflattened_redactions",
                                    "redaction_count": len(redaction_rects),
                                    "policy": "reject",
                                },
                            )
                            # Whole-file abort, not per-page skip — the
                            # gap-doc directive is "reject the document"
                            # (item #8). One leaky page = whole doc.
                            break
                        if settings.pdf_redaction_policy == "clip-extract":
                            text = _text_clipped_around_redactions(
                                page, redaction_rects
                            )
                            redaction_status = "clipped"
                            logger.info(
                                "PDF %s page %d: %d redaction(s) clipped",
                                obj["key"],
                                page_num,
                                len(redaction_rects),
                                extra={
                                    "file": obj["key"],
                                    "page": page_num,
                                    "redaction_count": len(redaction_rects),
                                    "policy": "clip-extract",
                                },
                            )
                        else:
                            # Unknown policy value: log + reject for
                            # safety. Misconfiguration shouldn't silently
                            # leak redacted content.
                            logger.warning(
                                "PDF %s rejected at page %d: unknown "
                                "pdf_redaction_policy=%r (expected "
                                "'reject' | 'clip-extract')",
                                obj["key"],
                                page_num,
                                settings.pdf_redaction_policy,
                            )
                            break
                    else:
                        text = page.get_text()
                    # Item #18 — bomb defense layer 4: per-page text
                    # decompression cap. If a page yields gigabytes of
                    # text from a small source, that's the
                    # decompression-ratio bomb shape. Skip the page, log,
                    # but keep processing the file (one bad page in an
                    # otherwise legit doc is rare but plausible).
                    if len(text) > settings.pdf_max_page_text_bytes:
                        logger.warning(
                            "PDF %s page %d skipped: extracted_bytes=%d exceeds pdf_max_page_text_bytes=%d",
                            obj["key"],
                            page_num,
                            len(text),
                            settings.pdf_max_page_text_bytes,
                            extra={
                                "file": obj["key"],
                                "page": page_num,
                                "reason": "max_page_text",
                                "extracted_bytes": len(text),
                                "cap": settings.pdf_max_page_text_bytes,
                            },
                        )
                        continue
                    text = text.strip()
                    if not text:
                        continue
                    chunks = _chunk_text(text)
                    if not chunks:
                        continue
                    bbox_x0, bbox_y0, bbox_x1, bbox_y1 = _page_bbox(page)
                    ids = [
                        _doc_id(col_name, f"{source_key}:p{page_num}", i)
                        for i in range(len(chunks))
                    ]
                    metadatas: list[dict[str, Any]] = [
                        {
                            "source": obj["filename"],
                            "source_key": source_key,
                            "category": category,
                            "file_type": "pdf",
                            "page": page_num,
                            "chunk": i,
                            # Item #21 — per-chunk provenance fields.
                            # Flattened (bbox_x0..y1) for ChromaDB
                            # queryability since metadata only accepts
                            # str|int|float|bool. Static defaults below
                            # (text_source="native", confidence=1.0,
                            # signature_status="unknown") will be
                            # overridden by future commits in this
                            # tier-A series — #1 (OCR fallback) flips
                            # text_source; #12 (signature validity)
                            # flips signature_status.
                            "bbox_x0": bbox_x0,
                            "bbox_y0": bbox_y0,
                            "bbox_x1": bbox_x1,
                            "bbox_y1": bbox_y1,
                            "text_source": "native",
                            "extraction_confidence": 1.0,
                            "document_hash": document_hash,
                            "signature_status": signature_status,
                            "redaction_status": redaction_status,
                            "ingested_by_user_id": user_id,
                            "ingestion_ts_ms": ingestion_ts_ms,
                        }
                        for i in range(len(chunks))
                    ]
                    _upsert_in_batches(collection, ids, chunks, metadatas)
                    total += len(chunks)
        except Exception as exc:
            logger.warning("Failed to process PDF %s: %s", obj["key"], exc)
            continue
    return total


# `def` (NOT `async def`) is deliberate. The body holds CPU-bound
# blocking work — ChromaDB upserts that compute embeddings via the
# in-process ONNX model, plus pymupdf page extraction. FastAPI runs
# sync `def` handlers in its threadpool, so the event loop stays
# free to answer /health probes. Originally written as `async def`,
# which blocked the loop and caused k8s livez/readyz to time out
# mid-index → Istio marked the pod NotReady → next request 503'd
# with "no healthy upstream" (caught live 2026-05-06 on the
# per-file PDF loop). The threadpool also lets concurrent
# /memory/index calls run side-by-side without serialising on the
# loop. See feedback_use_context_managers for the related leak fix
# that landed in the same PR.
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
    _auth: dict[str, Any] = Security(validate_jwt, scopes=["audittrace:admin"]),
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
    """
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
    bucket = settings.minio_shared_bucket
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
        if not single_file_mode:
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
            )

        results[col_name] = chunk_count
        total_chunks += chunk_count
        logger.info("Indexed %s: %d chunks", col_name, chunk_count)

    duration = time.time() - start
    return {
        "status": "indexed",
        "collections": results,
        "total_chunks": total_chunks,
        "duration_s": round(duration, 2),
    }


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
        service.write(user, filename, content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    entry: ManifestEntry = manifest.record_create(
        layer="episodic",
        key=filename,
        title=title,
        size_bytes=len(content.encode("utf-8")),
        user_id=user.user_id,
    )
    return entry.to_dict()


def _merge_layer_items_with_s3(
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
    all_known: list[ManifestEntry] = manifest.list_for_layer(
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
        docs = service.load(user)
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
    entries: list[ManifestEntry] = manifest.list_for_layer(
        "episodic", include_deleted=include_deleted
    )
    items = _merge_layer_items_with_s3("episodic", entries, user)
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
    doc = service.read(user, filename)
    if doc is None:
        raise HTTPException(status_code=404, detail="ADR not found")
    entry: ManifestEntry | None = manifest.get("episodic", filename)
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
        service.write(user, filename, content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    try:
        entry: ManifestEntry = manifest.record_update(
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
        entry: ManifestEntry = manifest.record_delete(
            "episodic", filename, user.user_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if hard:
        try:
            service.delete(user, filename)
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
        service.write(user, filename, content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    entry: ManifestEntry = manifest.record_create(
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
    entries: list[ManifestEntry] = manifest.list_for_layer(
        "procedural", include_deleted=include_deleted
    )
    items = _merge_layer_items_with_s3("procedural", entries, user)
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
    doc = service.read(user, filename)
    if doc is None:
        raise HTTPException(status_code=404, detail="SKILL not found")
    entry: ManifestEntry | None = manifest.get("procedural", filename)
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
        service.write(user, filename, content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    try:
        entry: ManifestEntry = manifest.record_update(
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
        entry: ManifestEntry = manifest.record_delete(
            "procedural", filename, user.user_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if hard:
        try:
            service.delete(user, filename)
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
        service.upsert(user, collection, document_id, text, metadata)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    entry: ManifestEntry = manifest.record_create(
        layer="semantic",
        key=_semantic_key(collection, document_id),
        title=title,
        size_bytes=len(text.encode("utf-8")),
        user_id=user.user_id,
    )
    return entry.to_dict()


def _merge_semantic_with_chroma(
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
    all_known: list[ManifestEntry] = manifest.list_for_layer(
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
    entries: list[ManifestEntry] = manifest.list_for_layer(
        "semantic", include_deleted=include_deleted
    )
    if collection is not None:
        prefix = f"{collection}/"
        entries = [e for e in entries if e.key.startswith(prefix)]
    items = _merge_semantic_with_chroma(entries, collection)
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
    doc = service.get_document(user, collection, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="semantic doc not found")
    entry: ManifestEntry | None = manifest.get(
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
        service.upsert(user, collection, document_id, text, metadata)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    key = _semantic_key(collection, document_id)
    try:
        entry: ManifestEntry = manifest.record_update(
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
        entry: ManifestEntry = manifest.record_delete("semantic", key, user.user_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if hard:
        try:
            service.delete_document(user, collection, document_id)
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
    with session_factory() as db:
        q = db.query(SessionRow)
        if project is not None:
            q = q.filter(SessionRow.project == project)
        if since is not None:
            q = q.filter(SessionRow.date >= since)
        if summarised is True:
            q = q.filter(SessionRow.summarized_at.is_not(None))
        elif summarised is False:
            q = q.filter(SessionRow.summarized_at.is_(None))
        total = q.count()
        rows = q.order_by(SessionRow.date.desc()).offset(offset).limit(limit).all()

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
    with session_factory() as db:
        session_row = (
            db.query(SessionRow).filter(SessionRow.id == session_id).one_or_none()
        )
        if session_row is None:
            raise HTTPException(status_code=404, detail="session not found")

        q = (
            db.query(InteractionRow)
            .filter(InteractionRow.session_id == session_id)
            .order_by(InteractionRow.timestamp.asc())
            .offset(offset)
            .limit(limit)
        )
        interactions = q.all()

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
