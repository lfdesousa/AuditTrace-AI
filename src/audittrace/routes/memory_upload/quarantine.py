"""MinIO PUT into the quarantine prefix + content sniffing helpers.

The POST /memory/upload PDF branch calls these helpers to:

1. Compute SHA-256 of the bytes (stable identifier carried into
   the audit trail + the AMQP message).
2. Detect content-type — both the client-claimed value (from the
   multipart header) and a server-side sniff of the magic bytes.
   If they disagree, we fail the upload with 400 — a PDF must be
   declared as ``application/pdf`` AND start with ``%PDF-`` to
   land in the scan flow. Spoofed content-type slipping through
   is precisely the attack surface ADR-048 closes.
3. PUT the bytes to ``s3://shared/quarantine/<user>/<scan_id>/<filename>``.

The MinIO client used here is a brand-new instance scoped to the
caller's scan_id, NOT the existing memory-server MinIO client —
the existing one carries the
``QuarantineDenyingMinioClient`` wrapper from PR-B2 that explicitly
refuses ``quarantine/`` GETs. PR-B7 will harden this further with
bucket-policy enforcement; for now the app-layer denylist + this
intentionally separate write client is the seam.
"""

from __future__ import annotations

import hashlib
import io
import logging
from typing import TYPE_CHECKING

from fastapi import HTTPException

if TYPE_CHECKING:
    from audittrace_object_storage import S3ObjectStorageProvider

    from audittrace.config import Settings

logger = logging.getLogger(__name__)

# Magic bytes for a PDF — first four bytes are always `%PDF`.
# The fifth byte is the spec version (PDF 1.x: `-`, PDF 2.0:
# also `-`). We only check the prefix.
_PDF_MAGIC = b"%PDF-"


def is_pdf_upload(*, claimed_content_type: str | None, content: bytes) -> bool:
    """Return True iff the upload is a PDF.

    Both checks must pass:
    - claimed content-type starts with ``application/pdf``
    - first 5 bytes are the PDF magic ``%PDF-``

    This is the dispatcher for the PDF branch in
    /memory/upload — markdown / non-PDF uploads bypass scanning."""
    if not claimed_content_type:
        return False
    if not claimed_content_type.lower().startswith("application/pdf"):
        return False
    return content.startswith(_PDF_MAGIC)


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def quarantine_key(*, user_id: str, scan_id: str, filename: str) -> str:
    """Compute the quarantine MinIO key.

    Format: ``quarantine/<user_id>/<scan_id>/<filename>``. The
    user_id segment lets a future operator scoped query pull all
    pending uploads for one user; the scan_id segment guarantees
    uniqueness even on duplicate uploads of the same file."""
    return f"quarantine/{user_id}/{scan_id}/{filename}"


def quarantine_uri(*, bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key}"


def put_quarantine(
    *,
    settings: Settings,
    minio_client: S3ObjectStorageProvider,
    user_id: str,
    scan_id: str,
    filename: str,
    content: bytes,
    content_type: str,
) -> tuple[str, str]:
    """PUT bytes into the quarantine prefix and return (key, uri).

    Errors return 502 — the upload route translates them. Logs
    include scan_id but NOT bytes so the audit trail stays clean.

    ``minio_client`` keeps its parameter name for backwards compatibility
    with the pre-ADR-006 call sites; the runtime type is now the
    ABC-shaped :class:`S3ObjectStorageProvider`.
    """
    bucket = (
        settings.aws_bucket
        if settings.object_storage_backend == "aws"
        else settings.minio_shared_bucket
    )
    key = quarantine_key(user_id=user_id, scan_id=scan_id, filename=filename)

    # contextlib.closing on BytesIO so the buffer is released
    # deterministically (PEP 343 + feedback_use_context_managers
    # — every resource cleanup goes through `with`, never relies on
    # GC). The provider reads the stream eagerly so the close after
    # the call is safe.
    try:
        with io.BytesIO(content) as buf:
            minio_client.put_object(
                bucket,
                key,
                buf,
                length=len(content),
                content_type=content_type,
            )
    except HTTPException:
        # If put_object itself raised an HTTPException (testing
        # fakes, quarantine_guard, etc.) propagate as-is.
        raise
    except Exception as exc:
        logger.error(
            "memory_upload.quarantine_put_failed",
            extra={"scan_id": scan_id, "reason": str(exc)},
        )
        raise HTTPException(
            status_code=502, detail="Object storage write failed"
        ) from exc

    uri = quarantine_uri(bucket=bucket, key=key)
    logger.info(
        "memory_upload.quarantine_put",
        extra={
            "scan_id": scan_id,
            "user_id": user_id,
            "uri": uri,
            "size_bytes": len(content),
        },
    )
    return key, uri
