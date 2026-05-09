"""Application-layer denylist on MinIO ``quarantine/*`` GET (ADR-048 PR-B2).

Memory-server should NEVER read pre-scanned bytes (ADR-048 §Decision
rule #1). The genuine enforcement is at the bucket-policy / IAM
layer (PR-B7's MinIO IAM split: ``audittrace_app`` role gets
``Effect: Deny`` on ``quarantine/*`` GET, MinIO itself returns 403).

This module is the **defense-in-depth** layer that ships *before*
PR-B7 — wraps the ``Minio`` client and refuses any ``get_object``
call whose key starts with the quarantine prefix. When PR-B7 lands
the IAM split, this wrapper becomes the second of two enforcement
layers (audit-trail completeness: even if a future code change
accidentally bypasses the wrapper, MinIO itself refuses).

Closed-set error code per the AuditTrace closed-set pattern
(``_QUARANTINE_GUARD_ERROR_CODES``); pinned by
``tests/test_minio_quarantine_denylist.py`` so the contract can't
silently drift.
"""

from __future__ import annotations

from typing import Any

# ── Closed-set error codes ────────────────────────────────────────────
# Single value today; the closed-set scaffolding is here so future
# refusals (e.g., ``quarantine_age_exceeded`` for very old quarantine
# objects) can be added without breaking SOC parsers that filter on
# the code field.
_QUARANTINE_GUARD_ERROR_CODES: frozenset[str] = frozenset({"quarantine_read_denied"})


class QuarantinedObjectAccessError(PermissionError):
    """Raised when memory-server tries to read a ``quarantine/*`` key.

    Caller should treat this as a permanent denial (NOT a transient
    error). The right-shaped response is HTTP 409 (the object exists
    but is in a state that disallows the requested action) — see
    ADR-048 §"/memory/index returns 409 for any quarantine/* key"
    (PR-B3).

    Attributes:
        code: Closed-set value from ``_QUARANTINE_GUARD_ERROR_CODES``.
        key: The forbidden object key (for audit / log correlation;
            does NOT include bytes).
    """

    def __init__(self, key: str) -> None:
        self.code = "quarantine_read_denied"
        self.key = key
        super().__init__(
            f"refused MinIO get_object on quarantine/* (key={key!r}): "
            "ADR-048 application-layer denylist (PR-B2). Memory-server "
            "is not authorised to read pre-scanned bytes."
        )


class QuarantineDenyingMinioClient:
    """Proxy around a ``Minio`` client that refuses ``quarantine/*`` GET.

    Delegates every other method to the wrapped client. This is the
    only place in the source tree that knows about the quarantine
    prefix at the storage layer.

    Why a delegating proxy and not a subclass: ``minio.Minio`` carries
    a lot of internal state (HTTP pool, signing key derivation,
    region resolution) that subclassing would tangle with. Delegation
    is mechanical, safe, and trivially reversible if PR-B7 makes the
    wrapper redundant.
    """

    def __init__(self, inner: Any, quarantine_prefix: str = "quarantine/") -> None:
        """Construct.

        Args:
            inner: The wrapped ``minio.Minio`` instance. ``Any`` typed
                because importing minio at module load adds an
                unwanted dependency on the test path.
            quarantine_prefix: The forbidden key prefix. Defaults to
                ``"quarantine/"``; tests parameterise it.
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

    def get_object(
        self, bucket_name: str, object_name: str, *args: Any, **kwargs: Any
    ) -> Any:
        """Refuse ``quarantine/*`` GET; delegate everything else."""
        if object_name.startswith(self._quarantine_prefix):
            raise QuarantinedObjectAccessError(key=object_name)
        return self._inner.get_object(bucket_name, object_name, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        """Delegate everything not explicitly overridden to the inner client."""
        return getattr(self._inner, name)
