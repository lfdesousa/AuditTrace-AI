"""Application-layer denylist on ``quarantine/*`` GET (ADR-048 PR-B2).

Memory-server should NEVER read pre-scanned bytes (ADR-048 §Decision
rule #1). The genuine enforcement is at the bucket-policy / IAM
layer (PR-B7 in MinIO mode; cross-IRSA-role split on AWS S3).

This module is the **defense-in-depth** layer that ships at the
application level — wraps an ``S3ObjectStorageProvider`` and refuses
any ``get_object`` call whose key starts with the quarantine prefix.

ADR-006 migration note: the old ``QuarantineDenyingMinioClient`` used
``__getattr__`` to delegate to a bare ``Minio`` client. The new
``QuarantineDenyingObjectStorageClient`` IS an
``S3ObjectStorageProvider`` itself — every ABC method is explicit, no
``__getattr__`` escape hatch. The ABC's surface is fixed and small, so
the cost of explicit delegation is one short method per ABC method.

Closed-set error code per the AuditTrace closed-set pattern
(``_QUARANTINE_GUARD_ERROR_CODES``); pinned by
``tests/test_minio_quarantine_denylist.py`` so the contract cannot
silently drift.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import BinaryIO

from audittrace_object_storage import (
    ObjectMetadata,
    ObjectReader,
    ObjectStorageError,
    S3ObjectStorageProvider,
)

logger = logging.getLogger(__name__)

# ── Closed-set error codes ────────────────────────────────────────────
# Single value today; the closed-set scaffolding is here so future
# refusals (e.g., ``quarantine_age_exceeded`` for very old quarantine
# objects) can be added without breaking SOC parsers that filter on
# the code field.
_QUARANTINE_GUARD_ERROR_CODES: frozenset[str] = frozenset({"quarantine_read_denied"})


class QuarantinedObjectAccessError(ObjectStorageError, PermissionError):
    """Raised when memory-server tries to read a ``quarantine/*`` key.

    Caller should treat this as a permanent denial (NOT a transient
    error). The right-shaped HTTP response is 409 (the object exists
    but is in a state that disallows the requested action) — see
    ADR-048 §"/memory/index returns 409 for any quarantine/* key"
    (PR-B3).

    Multiple inheritance bridges two consumer expectations:

    - ``except ObjectStorageError`` catches it via the shared-package
      hierarchy (post-ADR-006).
    - ``except PermissionError`` catches it via the original
      stdlib-style hierarchy (pre-ADR-006 backwards compat).

    Attributes:
        code: Closed-set value from ``_QUARANTINE_GUARD_ERROR_CODES``.
        key: The forbidden object key (for audit / log correlation;
            does NOT include bytes).
    """

    def __init__(self, key: str) -> None:
        self.code = "quarantine_read_denied"
        self.key = key
        super().__init__(
            f"refused get_object on quarantine/* (key={key!r}): "
            "ADR-048 application-layer denylist (PR-B2). Memory-server "
            "is not authorised to read pre-scanned bytes."
        )


class QuarantineDenyingObjectStorageClient(S3ObjectStorageProvider):
    """Wrap an ``S3ObjectStorageProvider`` and refuse ``quarantine/*`` GET.

    Implements the same ABC interface as the wrapped provider, so it
    can be injected anywhere an ``S3ObjectStorageProvider`` is expected.
    Every method delegates to the inner provider EXCEPT ``get_object``,
    which checks the key prefix before forwarding.
    """

    def __init__(
        self,
        inner: S3ObjectStorageProvider,
        quarantine_prefix: str = "quarantine/",
    ) -> None:
        """Construct.

        Args:
            inner: The wrapped provider. Must implement
                :class:`S3ObjectStorageProvider`.
            quarantine_prefix: The forbidden key prefix. Defaults to
                ``"quarantine/"``; tests parameterise it.

        Raises:
            ValueError: If ``quarantine_prefix`` does not end with ``/``.
        """
        if not quarantine_prefix.endswith("/"):
            raise ValueError(
                f"quarantine_prefix must end with '/' (got {quarantine_prefix!r}); "
                "S3 prefix discipline."
            )
        self._inner = inner
        self._quarantine_prefix = quarantine_prefix

    @property
    def quarantine_prefix(self) -> str:
        """The configured forbidden prefix (read-only)."""
        return self._quarantine_prefix

    # ---- The guarded method ----

    def get_object(self, bucket: str, key: str) -> ObjectReader:
        """Refuse ``quarantine/*`` GET; delegate everything else."""
        if key.startswith(self._quarantine_prefix):
            logger.warning(
                "refused get_object on quarantine/* key",
                extra={"bucket": bucket, "key": key, "code": "quarantine_read_denied"},
            )
            raise QuarantinedObjectAccessError(key=key)
        return self._inner.get_object(bucket, key)

    # ---- Pass-through delegators ----

    def list_objects(self, bucket: str, prefix: str = "") -> Iterator[ObjectMetadata]:
        return self._inner.list_objects(bucket, prefix=prefix)

    def put_object(
        self,
        bucket: str,
        key: str,
        data: BinaryIO,
        length: int,
        content_type: str | None = None,
    ) -> None:
        self._inner.put_object(bucket, key, data, length, content_type=content_type)

    def stat_object(self, bucket: str, key: str) -> ObjectMetadata:
        return self._inner.stat_object(bucket, key)

    def remove_object(self, bucket: str, key: str) -> None:
        self._inner.remove_object(bucket, key)

    def copy_object(
        self,
        src_bucket: str,
        src_key: str,
        dst_bucket: str,
        dst_key: str,
    ) -> None:
        self._inner.copy_object(src_bucket, src_key, dst_bucket, dst_key)

    def health_check(self) -> bool:
        return self._inner.health_check()
