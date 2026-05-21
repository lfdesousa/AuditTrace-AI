"""Deprecation shim — ADR-006 renamed this module.

The old ``QuarantineDenyingMinioClient`` (wrapped a bare ``Minio``
client) has been replaced by ``QuarantineDenyingObjectStorageClient``
(wraps an ``S3ObjectStorageProvider`` from the shared
``audittrace-object-storage`` package). The new class is the
backend-agnostic equivalent: it works the same way against MinIO,
AWS S3, or any future backend that implements the ABC.

This shim re-exports the renamed class under the old name so any
out-of-tree caller that imports
``audittrace.services.quarantine_guard:QuarantineDenyingMinioClient``
keeps working for one release. The shim WILL be removed in a follow-up
PR; new code MUST import from ``quarantine_denying_provider`` directly.
"""

from __future__ import annotations

from audittrace.services.quarantine_denying_provider import (
    _QUARANTINE_GUARD_ERROR_CODES,
    QuarantinedObjectAccessError,
)
from audittrace.services.quarantine_denying_provider import (
    QuarantineDenyingObjectStorageClient as QuarantineDenyingMinioClient,
)

__all__ = [
    "QuarantineDenyingMinioClient",
    "QuarantinedObjectAccessError",
    "_QUARANTINE_GUARD_ERROR_CODES",
]
