"""Single source of truth for "what objects exist in a memory layer".

Backlog #15 — Defect A: three code paths held three *different* views of
the same episodic/procedural bucket:

* ``read()`` (by exact key)          → every ``.md`` object
* the ``/memory/index`` prefix walk  → every object
* the list / ``load()`` bulk loader  → only ``ADR-*.md`` / ``SKILL-*.md``

So a ``decision-2026-07-24-*.md`` uploaded via ``/memory/upload`` was
readable by key and indexable into ChromaDB, yet invisible in
``GET /memory/{layer}``. An audit product that cannot consistently
enumerate its own store across its own read paths is the real defect.

This module centralises the enumeration + the "is this a listable layer
object" rule so the three paths cannot diverge again. Every path that
answers "which objects are in this layer" flows through here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def _leaf_filename(key: str) -> str:
    """Return the leaf filename of an S3 key (the part after the last ``/``)."""
    return key.rsplit("/", 1)[-1] if "/" in key else key


def _last_modified_ms(obj: Any) -> int | None:
    """Epoch-milliseconds of the object's ``last_modified`` datetime, or None.

    ``ObjectMetadata.last_modified`` is a ``datetime`` on the real backends
    (MinIO + boto3). Test doubles may omit it (or leave it a non-datetime
    sentinel); we coalesce anything that is not a ``datetime`` to ``None`` so
    a missing timestamp can never crash enumeration.
    """
    last_modified = getattr(obj, "last_modified", None)
    if isinstance(last_modified, datetime):
        return int(last_modified.timestamp() * 1000)
    return None


def list_layer_objects(client: Any, bucket: str, prefix: str) -> list[dict[str, Any]]:
    """Enumerate every object under ``bucket/prefix`` — the shared listing.

    Returns ``[{key, filename, size_bytes, last_modified_ms}]``. This is the
    single enumeration shared by the list path, the ``/memory/index`` prefix
    walk, and the layer cache, so no path can hold a stricter view than
    another (backlog #15, Defect A).

    Deliberately applies *no* extension filter: the ``.md``-vs-``.pdf``
    decision belongs to the caller (via :func:`is_listable_md_object` for the
    markdown list/load path, or the PDF pipeline's own ``.pdf`` check), so
    the md-list and the pdf-index enumerate the *same* underlying object set.

    Directory-marker keys (a trailing ``/`` → empty leaf filename) and
    ``None`` object names are dropped: they are prefix markers, not
    documents, and would otherwise become phantom rows.
    """
    objects: list[dict[str, Any]] = []
    for obj in client.list_objects(bucket, prefix=prefix):
        key = getattr(obj, "object_name", None) or ""
        filename = _leaf_filename(key)
        if not filename:
            continue
        size = getattr(obj, "size", None)
        objects.append(
            {
                "key": key,
                "filename": filename,
                "size_bytes": int(size) if isinstance(size, int) else None,
                "last_modified_ms": _last_modified_ms(obj),
            }
        )
    return objects


def is_listable_md_object(filename: str) -> bool:
    """The unified rule for "is this a listable/loadable ``.md`` layer object".

    Replaces the old ``startswith("ADR-")`` / ``startswith("SKILL-")`` gates
    (backlog #15, Defect A): every ``.md`` object under the layer prefix is
    enumerable — decisions, session docs and papers, not only ADRs/SKILLs.
    This matches exactly what ``read()`` can fetch by key and what the
    ChromaDB ``/memory/index`` walk embeds.
    """
    return filename.endswith(".md")
