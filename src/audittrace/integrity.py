"""Content-integrity hashing for audit rows (ADR-058 WS-A3).

Each audit row carries a SHA-256 over its immutable content. Paired with
the append-only trigger (WS-A2, which blocks UPDATE/DELETE at the database),
the hash makes any post-hoc mutation *detectable*: recompute and compare. A
higher-privileged actor who disables the trigger and edits a row still can't
make the stored digest match the tampered content.

Computed app-side over a canonical field set, so it is deterministic across
processes and Python builds, and adds NO predecessor read on the hot path —
no lock, no serialisation of the 500-user write path (a strict
predecessor-linked chain would have; that trade-off is deliberate).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# The immutable content fields that define an audit row. ``id`` and
# ``created_at`` are DB-assigned; ``content_hash`` excludes itself.
_CONTENT_FIELDS: tuple[str, ...] = (
    "project",
    "source",
    "question",
    "answer",
    "prompt_tokens",
    "completion_tokens",
    "timestamp",
    "session_id",
    "model",
    "user_id",
    "status",
    "failure_class",
    "error_detail",
    "duration_ms",
    "trace_id",
    "event_class",
)


def content_hash(fields: dict[str, Any]) -> str:
    """Return the SHA-256 hex digest of an audit row's immutable content.

    Canonicalises the content fields (sorted keys, compact separators) so
    the digest is stable across processes and Python builds.
    """
    payload = {k: fields.get(k) for k in _CONTENT_FIELDS}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_content_hash(row: Any) -> bool:
    """True iff a row's stored ``content_hash`` matches a recomputation.

    A ``False`` means the content changed after the row was written
    (tamper), or the row predates the column (``content_hash`` is None).
    """
    stored = getattr(row, "content_hash", None)
    if not stored:
        return False
    fields = {k: getattr(row, k, None) for k in _CONTENT_FIELDS}
    return bool(content_hash(fields) == stored)
