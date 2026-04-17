"""Memory ingestion routes — single-gateway upload and indexing (ADR-027).

These endpoints make the memory server the sole gateway for writing to
MinIO and ChromaDB.  No external caller talks to those backends directly.

``POST /memory/upload`` stores a file in MinIO under the episodic or
procedural prefix.  ``POST /memory/index`` reads all documents from MinIO,
chunks them, and pushes the chunks into ChromaDB collections.
"""

import hashlib
import io
import logging
import time
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from minio import Minio

from audittrace.auth import require_scope
from audittrace.config import get_settings
from audittrace.dependencies import get_chromadb

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
