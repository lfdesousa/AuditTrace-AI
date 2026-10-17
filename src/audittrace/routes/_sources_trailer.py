"""Deterministic markdown "sources consultées" trailer (M3 slice 1).

Spec: ``2026-08-24-SPEC-m3-librechat-console.md`` §3 (the sources
requirement) + Deliverable §4.4. Behind
``AUDITTRACE_RESPONSE_SOURCES=trailer|off`` (default ``off``) — the
orchestrator appends this trailer to a chat completion's answer so a
LibreChat-rendered pilot response shows *"chaque réponse cite les
documents qu'elle a consultés"*.

**This is a RENDERING task over already-audited data, never a data task.**
The trailer is built exclusively from ``PendingToolCall.source_refs`` --
the ``source_ref`` fields ADR-060 (``tools/memory_handlers.py::
_recall_identity_fields``) stamps into every recall match, extracted from
the FULL tool result BEFORE ``result_summary`` is truncated to 1000 chars
for the audit row (see ``_memory_tool_loop.py::_source_refs_from_result``
and the ``PendingToolCall.source_refs`` docstring). This module MUST NEVER
re-derive source_refs by re-parsing ``result_summary``: a multi-match
recall's serialized result routinely exceeds 1000 chars, which corrupts
the JSON at the cut and made the trailer inert for any realistic recall
in production (M3-SOURCES-TRAILER-TRUNCATION-FIX, 2026-08-24 finding).
``source_refs`` and ``result_summary`` are populated from the SAME
in-memory ``pending`` records the caller flushes to the audit table, so a
byte-compare between the trailer and the recorded recall identity is
still a compare against identical source data -- just via the structural
field rather than a second, lossy parse of the truncated column.

**Never LLM-generated.** No function in this module calls a model, an
HTTP client, or accepts one — a hallucinated citation is worse than none
(the spec's explicit rationale). This is enforced structurally: the only
input is ``list[PendingToolCall]``, an in-memory dataclass with no network
handle.

**Language discipline (spec §3.2 / Frank inspection).** The fixed French
label is "Sources consultées" — retrieved-into-context, not
"utilisées pour générer" (causation, out of the claim the system can
support). Do not change the label without re-checking that boundary.
"""

from __future__ import annotations

import logging

from audittrace.routes._memory_tool_loop import PendingToolCall

logger = logging.getLogger(__name__)

SOURCES_TRAILER_LABEL = "Sources consultées"
"""Fixed French label (spec §3.2) — retrieval, never causation wording."""


def _extract_source_refs(pending: list[PendingToolCall]) -> list[str]:
    """Pull the recorded ``source_ref`` values out of ``pending``, in
    first-seen order, deduplicated across rows.

    Reads ``PendingToolCall.source_refs`` -- a structural field captured
    from the FULL tool result before the 1000-char audit truncation
    (``_memory_tool_loop.py::_source_refs_from_result``) -- NEVER
    ``result_summary`` (that column is truncated for the audit row and is
    not a reliable source for rendering; see the module docstring).
    Errored tool calls (``error is not None``) contribute nothing -- there
    is no result to cite; in practice their ``source_refs`` is already
    empty (the loop never computes it on an error branch), but the
    explicit check keeps the exclusion intent visible here too.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for rec in pending:
        if rec.error is not None:
            continue
        for source_ref in rec.source_refs:
            if source_ref and source_ref not in seen:
                seen.add(source_ref)
                ordered.append(source_ref)
    return ordered


def build_sources_trailer(pending: list[PendingToolCall]) -> str | None:
    """Render the markdown trailer, or ``None`` when there is nothing to
    cite (no recall tool call recorded a ``source_ref``-bearing match).

    Returning ``None`` rather than an empty trailer is deliberate — an
    ungrounded answer should show no sources block at all, not an empty
    one that reads as "checked, found nothing" (the corpus_status "don't
    cry wolf" principle applied to citations).
    """
    source_refs = _extract_source_refs(pending)
    if not source_refs:
        return None
    lines = [f"**{SOURCES_TRAILER_LABEL}**"]
    lines.extend(f"- {ref}" for ref in source_refs)
    return "\n\n" + "\n".join(lines)
