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
from datetime import datetime
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

# ─── PDF helpers re-exported from memory_pdf/ sub-package (2026-05-09) ─
# Pre-refactor memory.py was 3263 LOC; the §11 (PYTHON-ENGINEERING SKILL)
# discipline says modules > 2000 LOC stop accepting new code. The PDF
# concerns moved to ``audittrace.routes.memory_pdf``; this file re-exports
# the public seam so existing imports
# (``from audittrace.routes.memory import _PDF_WARNING_CODES``) keep
# working unchanged.
from audittrace.routes import memory_pdf as _pdf  # noqa: E402
from audittrace.services.embedder import SINGLETON_EMBEDDER
from audittrace.services.memory_manifest import ManifestEntry

_PDF_WARNING_CODES = _pdf._PDF_WARNING_CODES
_SIGNATURE_STATUS_CODES = _pdf._SIGNATURE_STATUS_CODES
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
    file: UploadFile = File(...),
    layer: MemoryLayer = Query(...),
    filename: str | None = Query(None),
    _auth: dict[str, Any] = Security(validate_jwt, scopes=[]),
    user: UserContext = Depends(require_user),
) -> dict[str, Any]:
    """Upload a file to MinIO under the specified memory layer.

    The file lands in the ``memory-shared`` bucket at
    ``{layer}/{filename}``.  If *filename* is omitted the upload's
    original filename is used.

    Authorization (per-layer): the caller's JWT must carry
    ``memory:<layer>:write`` matching the ``layer`` query parameter
    (or ``audittrace:admin``). A token with ``memory:procedural:write``
    cannot upload to ``layer=episodic`` and vice-versa. The empty
    static ``scopes=[]`` keeps OAuth2 declared in the OpenAPI spec
    without baking the dynamic per-layer scope into the schema —
    the prose contract lives here.
    """
    _require_layer_write(user, layer)
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
_VC_LOCK = __import__("threading").Lock()
_VC_TRUST_STORE_PATH: str = ""  # tracks which trust store the cached VC was built with
# Cached parsed cert list — re-used by the as-of-signing-time retry
# path in ``_pdf_signature_status`` (ADR-054 §3) so we don't re-parse
# the PEM bundle for the second ValidationContext.
_VC_TRUST_ROOTS: list[Any] = []


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


# Closed-set of ``code`` values allowed inside ``extraction_warnings``
# JSONB entries (per ADR-050). New codes need an ADR amendment so the
# set stays auditable. Tested by tests/test_memory_routes.py
# (TestExtractionWarningCodes). Adding a code here without updating
# ADR-050 is a documentation drift; CI doesn't catch it but reviewers
# will.
def _pdf_is_encrypted(doc: Any) -> bool:
    """Return True if the PDF is encrypted and not yet authenticated.

    Tier-B item #15. ``pymupdf`` exposes both ``is_encrypted`` (any
    encryption present) and ``needs_pass`` (encryption requires a
    password to read content). We treat *any* encryption requiring
    authentication as a refuse — see ADR-050 §#15 for the rationale
    (no password-bearing endpoint).

    Strict ``is True`` comparison (not truthy-check) means MagicMock
    attributes — which would otherwise be truthy by default — read
    as False. Test fixtures don't need to opt into the tier-B
    encryption contract; real pymupdf documents return real ``bool``
    values from these attrs and the comparison works correctly.
    """
    is_enc = getattr(doc, "is_encrypted", False)
    needs_pw = getattr(doc, "needs_pass", False)
    return is_enc is True and needs_pw is True


def _quarantine_pdf_attachments(
    doc: Any,
    *,
    parent_filename: str,
    layer_prefix: str,
    minio_client: Any,
    bucket: str,
) -> tuple[int, list[dict[str, Any]]]:
    """Extract embedded attachments and write them to MinIO under
    ``{layer_prefix}{parent_filename}/attachments/{name}``.

    Tier-B item #6. Returns ``(count, warnings)``. Each successful
    quarantine appends one ``{"code": "attachment", ...}`` entry to
    *warnings*; failures append ``"attachment_quarantine_failed"`` so
    auditors can distinguish "no attachments" from "attachments
    existed but we couldn't store them." Recursion bound = 1 level
    (per ADR-050 §#6) — embedded PDFs get an attachment row but are
    not themselves parsed.
    """
    warnings: list[dict[str, Any]] = []
    try:
        # int() coercion: real pymupdf returns int; tests with
        # MagicMock would otherwise return a MagicMock, breaking the
        # range() iteration below.
        count = int(doc.embfile_count() or 0)
    except (TypeError, ValueError, Exception):
        # Older pymupdf or malformed catalog. Treat as zero
        # attachments rather than failing the whole document.
        return 0, []
    if count <= 0:
        return 0, []
    successes = 0
    # Sanity cap on attachment count — a real PDF rarely carries
    # more than a few. MagicMock-driven tests sometimes return
    # ``__int__`` defaults of 1, which is fine; if a real PDF
    # somehow declared 10 000 attachments we'd refuse to walk all
    # of them.
    if count > 256:
        return 0, [
            {
                "code": "attachment_quarantine_failed",
                "error": "too_many_attachments",
                "count": count,
            }
        ]
    for i in range(count):
        # One try/except per iteration covering the full extract +
        # write cycle so any failure (malformed embfile entry, MinIO
        # error, MagicMock duck-typing edge case in tests) records a
        # structured warning and moves on rather than aborting the
        # whole document.
        try:
            info = doc.embfile_info(i)
            name = info.get("filename") or f"attachment-{i}"
            mime = info.get("mime") or "application/octet-stream"
            data = doc.embfile_get(i)
            if not isinstance(data, (bytes, bytearray)):
                raise TypeError(
                    f"embfile_get({i}) returned {type(data).__name__}, "
                    f"expected bytes-like"
                )
            sha256 = hashlib.sha256(data).hexdigest()
            # parent_filename is the human key (e.g. "main.pdf");
            # the layer prefix is "episodic/" or "procedural/".
            # MinIO key shape: "episodic/main.pdf/attachments/<name>".
            attachment_key = f"{layer_prefix}{parent_filename}/attachments/{name}"
            minio_client.put_object(
                bucket,
                attachment_key,
                io.BytesIO(data),
                length=len(data),
            )
        except Exception as exc:
            warnings.append(
                {
                    "code": "attachment_quarantine_failed",
                    "index": i,
                    "error": type(exc).__name__,
                }
            )
            continue
        successes += 1
        warnings.append(
            {
                "code": "attachment",
                "name": name,
                "mime": mime,
                "size": len(data),
                "sha256": sha256,
                "minio_key": attachment_key,
            }
        )
    return successes, warnings


def _acroform_text_for_page(page: Any) -> tuple[str | None, int]:
    """Return ``(text, count)`` rendered from AcroForm widgets on *page*.

    Tier-B item #7. Concatenates ``Label: Value`` lines in widget
    order. Returns ``(None, 0)`` when the page has no widgets — caller
    can fall back to plain text-layer extraction. The page's normal
    text is chunked separately; form-field text is its own chunk so
    embeddings carry the label-value semantic anchor (per ADR-050 §#7).
    """
    try:
        widgets = list(page.widgets() or [])
    except Exception:
        return None, 0
    if not widgets:
        return None, 0
    lines: list[str] = []
    count = 0
    for w in widgets:
        # pymupdf Widget exposes field_name + field_value + field_label
        # (the latter is the human-readable display string when the
        # PDF carries one, else None).
        name = getattr(w, "field_name", None) or ""
        value = getattr(w, "field_value", None)
        label = getattr(w, "field_label", None) or name
        if value is None or value == "":
            # Empty fields contribute no semantic signal — skip rather
            # than emit "Label: " noise that pollutes the embedding.
            continue
        lines.append(f"{label}: {value}".strip())
        count += 1
    if not lines:
        return None, 0
    return "\n".join(lines), count


def _ocr_render_page(
    page: Any,
    *,
    enabled: bool,
    languages: str,
    dpi: int,
) -> tuple[str, str, float]:
    """Run Tesseract OCR on *page* if it has raster content but no text.

    Tier-B item #1. Returns ``(text, text_source, confidence)`` where:

    * ``text_source`` ∈ {``"native"``, ``"ocr"``, ``"no_text_layer"``}.
      ``"native"`` is the no-op return for callers that pre-checked
      and decided OCR isn't needed. ``"no_text_layer"`` signals a
      raster page where OCR was unavailable (binary missing) or
      returned empty; caller should emit an extraction warning.
    * ``confidence`` is Tesseract's mean per-word confidence in
      [0.0, 1.0]; 1.0 for the native short-circuit; 0.0 for
      no_text_layer.

    Graceful degradation: if pytesseract is not importable OR the
    tesseract binary is missing OR rendering fails, return
    ``("", "no_text_layer", 0.0)`` — caller logs the warning, the
    page produces zero chunks, processing continues. Per ADR-050 §#1.
    """
    if not enabled:
        return ("", "no_text_layer", 0.0)
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return ("", "no_text_layer", 0.0)
    try:
        # Render the page to a 300-DPI raster. pymupdf's get_pixmap
        # returns a Pixmap; we convert to PNG bytes then to PIL.Image
        # for pytesseract's image_to_data API.
        pix = page.get_pixmap(dpi=dpi)
        png_bytes = pix.tobytes("png")
        img = Image.open(io.BytesIO(png_bytes))
        # image_to_data returns word-level results with confidences.
        # The mean of the (non -1) confidences is the cleanest signal.
        data = pytesseract.image_to_data(
            img, lang=languages, output_type=pytesseract.Output.DICT
        )
    except Exception as exc:
        logger.debug("OCR unavailable for page: %s", exc)
        return ("", "no_text_layer", 0.0)
    words: list[str] = []
    confidences: list[float] = []
    for word, conf in zip(data.get("text", []), data.get("conf", []), strict=False):
        if not word or not word.strip():
            continue
        try:
            conf_value = float(conf)
        except (TypeError, ValueError):
            continue
        if conf_value < 0:
            # Tesseract emits -1 for "no recognition"; skip.
            continue
        words.append(word)
        confidences.append(conf_value)
    if not words:
        return ("", "no_text_layer", 0.0)
    text = " ".join(words)
    mean_conf = (sum(confidences) / len(confidences)) / 100.0
    return (text, "ocr", round(mean_conf, 3))


# ── Tier-C PDF metadata + corrupted-file classification (ADR-056) ────────


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
    manifest_service: Any | None = None,
    details_log: list[dict[str, Any]] | None = None,
    dry_run: bool = False,
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

    *manifest_service* (tier-B item #22) — when supplied, every PDF
    processed yields one ``upsert_pdf_metadata`` call carrying
    page_count, signature_status, ocr_coverage_pct, attachment_count,
    form_field_count, the structured ``extraction_warnings`` list,
    and document_sha256. Pre-tier-B callers passing ``None`` skip
    the manifest write — backward-compatible during the rollout
    window. Production wiring always supplies the service.
    """
    import pymupdf  # heavy import; only load when ai_research_papers is requested

    settings = get_settings()
    max_size_bytes = settings.pdf_max_size_mb * 1024 * 1024

    # Layer for manifest writes. The function takes ``layer_prefix``
    # as ``"episodic/"`` / ``"procedural/"``; strip the trailing slash
    # for the manifest's ``layer`` column.
    manifest_layer = layer_prefix.rstrip("/")

    total = 0
    for obj in objects:
        if not obj["filename"].lower().endswith(".pdf"):
            continue
        raw = _read_minio_object(minio_client, bucket, obj["key"])
        if raw is None:
            continue

        # Per-document state — accumulated through the per-page loop
        # and flushed to the manifest at end-of-file. Reset per file
        # so a previous bomb-rejected document never bleeds into the
        # next one.
        warnings: list[dict[str, Any]] = []
        attachment_count_doc = 0
        form_field_count_doc = 0
        ocr_pages_doc = 0
        page_count_doc: int | None = None
        # Tier-C metadata (ADR-056 #10) — populated after pymupdf.open
        # succeeds. Bomb-rejected docs leave them None.
        pdf_title_doc: str | None = None
        pdf_author_doc: str | None = None
        pdf_creator_doc: str | None = None
        pdf_creation_date_doc: datetime | None = None
        # Tier-C PDF/A (ADR-056 #14) — extracted from XMP if present.
        pdfa_part_doc: str | None = None
        pdfa_conformance_doc: str | None = None
        # Tier-C LTV (ADR-056 #13) — DSS dictionary summary; only
        # meaningful on signed PDFs.
        ltv_data_doc: dict[str, Any] | None = None
        # Per-document chunk count for the ?details=true response.
        chunks_written_doc = 0

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
            warnings.append(
                {
                    "code": "max_size",
                    "size_bytes": len(raw),
                    "cap_bytes": max_size_bytes,
                }
            )
            _flush_pdf_manifest(
                manifest_service=manifest_service,
                layer=manifest_layer,
                key=obj["key"],
                user_id=user_id,
                size_bytes=len(raw),
                page_count=None,
                signature_status=None,
                ocr_coverage_pct=None,
                attachment_count=0,
                form_field_count=0,
                extraction_warnings=warnings,
                document_sha256=None,
                ok=False,
                error="max_size",
                details_log=details_log,
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
        # Tier-C #13 (ADR-056) — LTV summary lives on the document
        # itself, not on a single signature, so we compute it once per
        # file alongside signature_status. Returns ``None`` for
        # unsigned / no-DSS PDFs; never raises.
        ltv_data_doc = _summarize_ltv(raw)
        try:
            # Assign through Any so mypy doesn't flag pymupdf.open's
            # untyped Document return; the project's `disallow_untyped_calls`
            # would otherwise reject the with-context here.
            doc_factory: Any = pymupdf.open
            with doc_factory(stream=raw, filetype="pdf") as doc:
                # Tier-B item #15 — encrypted PDFs refused before any
                # text extraction. ADR-050 §#15: no password-bearing
                # endpoint; operator must decrypt out-of-band first.
                if _pdf_is_encrypted(doc):
                    logger.warning(
                        "PDF %s rejected: encrypted (refusing — no "
                        "password endpoint per ADR-050)",
                        obj["key"],
                        extra={"file": obj["key"], "reason": "encrypted"},
                    )
                    warnings.append({"code": "encrypted", "page": None})
                    _flush_pdf_manifest(
                        manifest_service=manifest_service,
                        layer=manifest_layer,
                        key=obj["key"],
                        user_id=user_id,
                        size_bytes=len(raw),
                        page_count=None,
                        signature_status=signature_status,
                        ocr_coverage_pct=None,
                        attachment_count=0,
                        form_field_count=0,
                        extraction_warnings=warnings,
                        document_sha256=document_hash,
                        ok=False,
                        error="encrypted",
                        details_log=details_log,
                    )
                    continue
                # Tier-C item #10 (ADR-056) — extract document metadata
                # once the encryption gate is past but before the page
                # loop. Failures append a single ``pdf_metadata_parse_error``
                # warning and leave the fields None; never raise.
                (
                    pdf_title_doc,
                    pdf_author_doc,
                    pdf_creator_doc,
                    pdf_creation_date_doc,
                    metadata_warning_codes,
                ) = _extract_pdf_metadata(doc)
                for code in metadata_warning_codes:
                    warnings.append({"code": code, "page": None})
                # Tier-C item #14 (ADR-056) — PDF/A conformance from XMP.
                # Both fields stay NULL when the doc isn't a PDF/A or
                # its XMP packet doesn't expose ``pdfaid:*``.
                pdfa_part_doc, pdfa_conformance_doc = _extract_pdfa_conformance(doc)
                # Tier-C item #9 (ADR-056) — page → TOC-section map.
                # Empty dict when the doc has no TOC; per-chunk metadata
                # then carries no ``toc_section`` field (additive only).
                toc_index = _build_toc_index(doc)
                # Item #18 — bomb defense layer 2: declared-shape caps.
                # Read both page_count and xref_length from the document
                # catalogue WITHOUT decompressing any stream — these are
                # cheap dictionary lookups. A bomb declaring billions of
                # pages or millions of xrefs is rejected here before any
                # page is ever rendered.
                page_count_doc = doc.page_count
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
                    warnings.append(
                        {
                            "code": "max_pages",
                            "page_count": doc.page_count,
                            "cap": settings.pdf_max_pages,
                        }
                    )
                    _flush_pdf_manifest(
                        manifest_service=manifest_service,
                        layer=manifest_layer,
                        key=obj["key"],
                        user_id=user_id,
                        size_bytes=len(raw),
                        page_count=page_count_doc,
                        signature_status=signature_status,
                        ocr_coverage_pct=None,
                        attachment_count=0,
                        form_field_count=0,
                        extraction_warnings=warnings,
                        document_sha256=document_hash,
                        pdf_title=pdf_title_doc,
                        pdf_author=pdf_author_doc,
                        pdf_creator=pdf_creator_doc,
                        pdf_creation_date=pdf_creation_date_doc,
                        ok=False,
                        error="max_pages",
                        details_log=details_log,
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
                    warnings.append(
                        {
                            "code": "max_xref",
                            "xref_count": xref_count,
                            "cap": settings.pdf_max_xref_count,
                        }
                    )
                    _flush_pdf_manifest(
                        manifest_service=manifest_service,
                        layer=manifest_layer,
                        key=obj["key"],
                        user_id=user_id,
                        size_bytes=len(raw),
                        page_count=page_count_doc,
                        signature_status=signature_status,
                        ocr_coverage_pct=None,
                        attachment_count=0,
                        form_field_count=0,
                        extraction_warnings=warnings,
                        document_sha256=document_hash,
                        pdf_title=pdf_title_doc,
                        pdf_author=pdf_author_doc,
                        pdf_creator=pdf_creator_doc,
                        pdf_creation_date=pdf_creation_date_doc,
                        ok=False,
                        error="max_xref",
                        details_log=details_log,
                    )
                    continue
                # Tier-B item #6 — quarantine embedded attachments
                # (PDF/A-3, ZUGFeRD, evidence bundles). Done before
                # the page loop because attachments are document-level.
                # Per ADR-050 §#6: write to MinIO, emit
                # ``{"code":"attachment", ...}`` warnings, do not
                # recurse into PDF-typed attachments.
                attachment_count_doc, attachment_warnings = _quarantine_pdf_attachments(
                    doc,
                    parent_filename=source_key,
                    layer_prefix=layer_prefix,
                    minio_client=minio_client,
                    bucket=bucket,
                )
                warnings.extend(attachment_warnings)
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
                        warnings.append(
                            {
                                "code": "parse_timeout",
                                "pages_processed": page_num - 1,
                            }
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
                            warnings.append(
                                {
                                    "code": "redaction_rejected",
                                    "page": page_num,
                                    "redaction_count": len(redaction_rects),
                                }
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
                            warnings.append(
                                {
                                    "code": "redaction_clipped",
                                    "page": page_num,
                                    "redaction_count": len(redaction_rects),
                                }
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
                        warnings.append(
                            {
                                "code": "max_page_text",
                                "page": page_num,
                                "extracted_bytes": len(text),
                                "cap": settings.pdf_max_page_text_bytes,
                            }
                        )
                        continue
                    text = text.strip()
                    # Tier-B item #1 — OCR fallback for raster-only
                    # pages. Triggered only when the native text-layer
                    # extraction is empty AND the page carries images.
                    # Per ADR-050 §#1: graceful degradation if Tesseract
                    # binary missing — page produces zero chunks but
                    # the warning is recorded.
                    text_source = "native"
                    extraction_confidence = 1.0
                    if not text:
                        try:
                            has_images = bool(page.get_images(full=False))
                        except Exception:
                            has_images = False
                        if has_images:
                            ocr_text, ocr_source, ocr_conf = _ocr_render_page(
                                page,
                                enabled=settings.pdf_ocr_enabled,
                                languages=settings.pdf_ocr_languages,
                                dpi=settings.pdf_ocr_dpi,
                            )
                            if ocr_source == "ocr" and ocr_text:
                                text = ocr_text
                                text_source = "ocr"
                                extraction_confidence = ocr_conf
                                ocr_pages_doc += 1
                                if ocr_conf < 0.6:
                                    warnings.append(
                                        {
                                            "code": "ocr_low_confidence",
                                            "page": page_num,
                                            "confidence": ocr_conf,
                                        }
                                    )
                            else:
                                # Raster-only page, OCR unavailable or
                                # produced nothing — the silent-data-
                                # loss case the gap inventory cites.
                                # Record + skip; do not pretend the
                                # page is empty.
                                warnings.append(
                                    {
                                        "code": "no_text_layer",
                                        "page": page_num,
                                    }
                                )
                                continue
                        else:
                            # Truly empty page (no text layer, no
                            # images) — benign, no warning needed.
                            continue
                    # Tier-B item #7 — AcroForm widget extraction.
                    # Form-field text emits its own chunk so the
                    # embedding carries the label-value semantic
                    # anchor (per ADR-050 §#7). The page's natural
                    # text is chunked separately below.
                    form_text, form_count = _acroform_text_for_page(page)
                    if form_text and form_count > 0:
                        form_field_count_doc += form_count
                        warnings.append(
                            {
                                "code": "form_fields",
                                "page": page_num,
                                "field_count": form_count,
                            }
                        )
                    if not text and not form_text:
                        continue
                    chunks = _chunk_text(text) if text else []
                    if form_text:
                        # Append form-field text as a dedicated chunk
                        # at the end of the page's chunk list so
                        # downstream callers can identify it via the
                        # ``chunk_type`` metadata field.
                        chunks.append(form_text)
                    if not chunks:
                        continue
                    bbox_x0, bbox_y0, bbox_x1, bbox_y1 = _page_bbox(page)
                    ids = [
                        _doc_id(col_name, f"{source_key}:p{page_num}", i)
                        for i in range(len(chunks))
                    ]
                    # Form-field chunk (always last when present)
                    # gets ``chunk_type=form_field``; preceding chunks
                    # are normal text or OCR — already disambiguated
                    # by ``text_source``.
                    form_idx = len(chunks) - 1 if form_text else -1
                    toc_section = toc_index.get(page_num)
                    metadatas: list[dict[str, Any]] = [
                        {
                            "source": obj["filename"],
                            "source_key": source_key,
                            "category": category,
                            "file_type": "pdf",
                            "page": page_num,
                            "chunk": i,
                            # Tier-C #9 (ADR-056) — TOC section title that
                            # contains this page (forward-filled). Absent
                            # when the doc has no TOC; ``None`` when the
                            # page sits before the first TOC entry.
                            "toc_section": toc_section,
                            # Item #21 — per-chunk provenance fields.
                            # Tier-B completes this set: ``text_source``
                            # now flips to ``"ocr"`` for OCR'd pages
                            # (was always ``"native"`` in tier-A); the
                            # per-chunk ``extraction_confidence``
                            # carries Tesseract's mean-per-word
                            # confidence on OCR pages.
                            "bbox_x0": bbox_x0,
                            "bbox_y0": bbox_y0,
                            "bbox_x1": bbox_x1,
                            "bbox_y1": bbox_y1,
                            "text_source": (
                                "form_field" if i == form_idx else text_source
                            ),
                            "extraction_confidence": extraction_confidence,
                            "document_hash": document_hash,
                            "signature_status": signature_status,
                            "redaction_status": redaction_status,
                            "ingested_by_user_id": user_id,
                            "ingestion_ts_ms": ingestion_ts_ms,
                            "chunk_type": ("form_field" if i == form_idx else "text"),
                        }
                        for i in range(len(chunks))
                    ]
                    # ChromaDB metadata can't carry None values — drop
                    # ``toc_section`` when this page sits before any
                    # TOC entry or the document has no TOC at all.
                    for md in metadatas:
                        if md.get("toc_section") is None:
                            md.pop("toc_section", None)
                    if not dry_run:
                        _upsert_in_batches(collection, ids, chunks, metadatas)
                        total += len(chunks)
                    # Tier-C #23 (ADR-056) — chunks_written_doc reflects
                    # what *would* have been written, so the dry-run
                    # response shape matches the real-run shape exactly.
                    chunks_written_doc += len(chunks)
            # Successful (or partial) processing — flush manifest. In
            # dry-run we still surface the per-doc outcome via
            # ``details_log`` (so callers can preview), but pass
            # ``manifest_service=None`` so no Postgres row is written.
            ocr_coverage_pct: float | None = None
            if page_count_doc and page_count_doc > 0:
                ocr_coverage_pct = round((ocr_pages_doc / page_count_doc) * 100.0, 2)
            _flush_pdf_manifest(
                manifest_service=None if dry_run else manifest_service,
                layer=manifest_layer,
                key=obj["key"],
                user_id=user_id,
                size_bytes=len(raw),
                page_count=page_count_doc,
                signature_status=signature_status,
                ocr_coverage_pct=ocr_coverage_pct,
                attachment_count=attachment_count_doc,
                form_field_count=form_field_count_doc,
                extraction_warnings=warnings,
                document_sha256=document_hash,
                pdf_title=pdf_title_doc,
                pdf_author=pdf_author_doc,
                pdf_creator=pdf_creator_doc,
                pdf_creation_date=pdf_creation_date_doc,
                pdfa_part=pdfa_part_doc,
                pdfa_conformance=pdfa_conformance_doc,
                ltv_data=ltv_data_doc,
                chunks_written=chunks_written_doc,
                ok=True,
                error=None,
                details_log=details_log,
            )
        except Exception as exc:
            # Tier-C item #16 (ADR-056) — classify pymupdf raises into
            # closed-set codes so auditors can query "show me all docs
            # that failed for THIS class of reason" rather than reading
            # exception strings out of logs. Falls through to
            # ``pdf_corrupted_structure`` for unmatched raises.
            code = _classify_pdf_extraction_error(exc)
            logger.warning(
                "Failed to process PDF %s: %s (classified=%s)",
                obj["key"],
                exc,
                code,
                extra={"file": obj["key"], "reason": code},
            )
            warnings.append({"code": code, "page": None})
            _flush_pdf_manifest(
                manifest_service=manifest_service,
                layer=manifest_layer,
                key=obj["key"],
                user_id=user_id,
                size_bytes=len(raw),
                page_count=page_count_doc,
                signature_status=signature_status,
                ocr_coverage_pct=None,
                attachment_count=attachment_count_doc,
                form_field_count=form_field_count_doc,
                extraction_warnings=warnings,
                document_sha256=document_hash,
                pdf_title=pdf_title_doc,
                pdf_author=pdf_author_doc,
                pdf_creator=pdf_creator_doc,
                pdf_creation_date=pdf_creation_date_doc,
                pdfa_part=pdfa_part_doc,
                pdfa_conformance=pdfa_conformance_doc,
                ltv_data=ltv_data_doc,
                chunks_written=chunks_written_doc,
                ok=False,
                error=str(exc) or code,
                details_log=details_log,
            )
            continue
    return total


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
