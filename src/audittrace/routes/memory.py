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

import hashlib
import io
import logging
import time
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from minio import Minio

from audittrace.auth import require_scope, require_user
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
from audittrace.services.memory_manifest import ManifestEntry

logger = logging.getLogger(__name__)

router = APIRouter()

# Chunking parameters — must match scripts/index-chromadb.py
CHUNK_SIZE = 1500
CHUNK_OVERLAP = 200


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
    """
    objects: list[dict[str, str]] = []
    for obj in client.list_objects(bucket, prefix=prefix):
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
    _auth: dict[str, Any] = Depends(require_scope("audittrace:admin")),
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


@router.post("/index")
async def index_memory(
    collections: str | None = Query(None),
    _auth: dict[str, Any] = Depends(require_scope("audittrace:admin")),
) -> dict[str, Any]:
    """Read documents from MinIO and push chunked embeddings to ChromaDB.

    If *collections* is provided (comma-separated), only those collections
    are rebuilt.  Otherwise all default collections are rebuilt.
    """
    target_collections = (
        [c.strip() for c in collections.split(",") if c.strip()]
        if collections
        else ["decisions", "skills", "semantic"]
    )

    settings = get_settings()
    bucket = settings.minio_shared_bucket
    minio_client = _get_minio_client()
    chroma_client = get_chromadb()

    start = time.time()
    results: dict[str, int] = {}
    total_chunks = 0

    # Read all documents from MinIO across both layers
    episodic_objects = _list_objects_from_minio(minio_client, bucket, "episodic/")
    procedural_objects = _list_objects_from_minio(minio_client, bucket, "procedural/")

    for col_name in target_collections:
        docs: list[dict[str, Any]] = []

        if col_name in ("decisions", "semantic"):
            # Episodic layer — ADR-*.md and any other .md files
            for obj in episodic_objects:
                if not obj["filename"].endswith(".md"):
                    continue
                try:
                    response = minio_client.get_object(bucket, obj["key"])
                    try:
                        content = response.read().decode("utf-8", errors="replace")
                    finally:
                        response.close()
                        response.release_conn()
                except Exception as exc:
                    logger.warning("Failed to read %s: %s", obj["key"], exc)
                    continue
                chunks = _chunk_text(content)
                for i, chunk in enumerate(chunks):
                    docs.append(
                        {
                            "id": _doc_id(col_name, obj["filename"], i),
                            "document": chunk,
                            "metadata": {
                                "source": obj["filename"],
                                "category": "episodic",
                                "file_type": "md",
                                "chunk": i,
                            },
                        }
                    )

        if col_name in ("skills", "semantic"):
            # Procedural layer — SKILL-*.md and any other .md files
            for obj in procedural_objects:
                if not obj["filename"].endswith(".md"):
                    continue
                try:
                    response = minio_client.get_object(bucket, obj["key"])
                    try:
                        content = response.read().decode("utf-8", errors="replace")
                    finally:
                        response.close()
                        response.release_conn()
                except Exception as exc:
                    logger.warning("Failed to read %s: %s", obj["key"], exc)
                    continue
                chunks = _chunk_text(content)
                for i, chunk in enumerate(chunks):
                    skill_name = (
                        obj["filename"].replace("SKILL-", "").replace(".md", "")
                    )
                    docs.append(
                        {
                            "id": _doc_id(col_name, obj["filename"], i),
                            "document": chunk,
                            "metadata": {
                                "source": obj["filename"],
                                "category": "procedural",
                                "file_type": "md",
                                "skill": skill_name,
                                "chunk": i,
                            },
                        }
                    )

        # Push to ChromaDB — delete-and-recreate for idempotency
        if docs:
            try:
                chroma_client.delete_collection(col_name)
            except Exception:
                pass
            collection = chroma_client.get_or_create_collection(name=col_name)
            batch_size = 100
            for i in range(0, len(docs), batch_size):
                batch = docs[i : i + batch_size]
                collection.add(
                    ids=[d["id"] for d in batch],
                    documents=[d["document"] for d in batch],
                    metadatas=[d["metadata"] for d in batch],
                )

        results[col_name] = len(docs)
        total_chunks += len(docs)
        logger.info("Indexed %s: %d chunks", col_name, len(docs))

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
    _scope: dict[str, Any] = Depends(require_scope("memory:episodic:write")),
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
    _auth: dict[str, Any] = Depends(require_scope("memory:episodic:read")),
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
    _auth: dict[str, Any] = Depends(require_scope("memory:episodic:read")),
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
    _scope: dict[str, Any] = Depends(require_scope("memory:episodic:write")),
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
    _scope: dict[str, Any] = Depends(require_scope("memory:episodic:write")),
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
    _scope: dict[str, Any] = Depends(require_scope("memory:procedural:write")),
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
    _auth: dict[str, Any] = Depends(require_scope("memory:procedural:read")),
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
    _auth: dict[str, Any] = Depends(require_scope("memory:procedural:read")),
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
    _scope: dict[str, Any] = Depends(require_scope("memory:procedural:write")),
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
    _scope: dict[str, Any] = Depends(require_scope("memory:procedural:write")),
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
    _scope: dict[str, Any] = Depends(require_scope("memory:semantic:write")),
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
            col = chroma.get_or_create_collection(name=col_name)
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
    _auth: dict[str, Any] = Depends(require_scope("memory:semantic:read")),
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
    _auth: dict[str, Any] = Depends(require_scope("memory:semantic:read")),
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
    _scope: dict[str, Any] = Depends(require_scope("memory:semantic:write")),
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
    _scope: dict[str, Any] = Depends(require_scope("memory:semantic:write")),
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


@router.get("/conversational")
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
    _scope: dict[str, Any] = Depends(require_scope("memory:conversational:read-own")),
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


@router.get("/conversational/{session_id}")
async def read_conversational_session(
    session_id: str,
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    _scope: dict[str, Any] = Depends(require_scope("memory:conversational:read-own")),
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
