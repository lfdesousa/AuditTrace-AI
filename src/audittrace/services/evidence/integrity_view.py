"""The ``integrity`` block of the evidence document.

The route recomputes the hash of the STORED row with :func:`audittrace.integrity.content_hash`
and compares it with the stored ``content_hash``; it never trusts a stored verdict and never
calls :func:`~audittrace.integrity.verify_content_hash` directly (that returns ``False`` for a
NULL hash, which would read as tamper).

Falsifiable: return the stored hash as the recomputation, or call ``verify_content_hash``
for the NULL row, and ``tests/test_integrity_evidence.py`` goes RED.
"""

from __future__ import annotations

from typing import Any

from audittrace.integrity import _CONTENT_FIELDS, _OPTIONAL_CONTENT_FIELDS, content_hash

#: Served verbatim as ``integrity.hash_inputs`` and frozen in fixtures v3 / v3-image
#: (ADDENDUM-A6 section 1): the recipe a third party holding the served object follows.
HASH_INPUTS = (
    "interaction[hashed_fields] — every hashed field is served; input_media is "
    "served as an object and is hashed as its STORED TEXT: payload = {k: "
    "interaction[k] for k in hashed_fields}; if interaction['input_media'] is not "
    "None: payload['input_media'] = json.dumps(interaction['input_media'], "
    "sort_keys=True, separators=(',', ':')); recompute = sha256(json.dumps(payload, "
    "sort_keys=True, separators=(',', ':'), default=str)). hashed_fields lists "
    "'input_media' only when it is not null "
    "(docs/reference/audittrace/audit-record-integrity.md)."
)


def hashed_field_names(row: Any) -> list[str]:
    """The names the row's hash covers, emitted from the integrity tuples (never typed).

    An optional field is listed only when the row has it set, so a row without an image
    lists exactly the 16 names it always hashed.
    """
    optional = [
        name
        for name in _OPTIONAL_CONTENT_FIELDS
        if getattr(row, name, None) is not None
    ]
    return [*_CONTENT_FIELDS, *optional]


def recompute(row: Any) -> str:
    """The hash of the row as stored (``input_media`` as its stored text)."""
    fields = {
        name: getattr(row, name, None)
        for name in (*_CONTENT_FIELDS, *_OPTIONAL_CONTENT_FIELDS)
    }
    return content_hash(fields)


def integrity_block(row: Any) -> dict[str, Any]:
    """``match`` / ``mismatch`` / ``unavailable`` (stored hash NULL: rows older than
    migration 017 and the writers that never hash, e.g. ``memory_access`` and scan
    ``security`` rows)."""
    stored = getattr(row, "content_hash", None)
    if not stored:
        verdict = "unavailable"
        stored = None
    else:
        verdict = "match" if recompute(row) == stored else "mismatch"
    return {
        "content_hash": stored,
        "recomputed": verdict,
        "hashed_fields": hashed_field_names(row),
        "hash_inputs": HASH_INPUTS,
    }
