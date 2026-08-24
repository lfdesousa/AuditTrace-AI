"""Deterministic markdown "sources consultées" trailer (M3 slice 1).

Spec: ``2026-08-24-SPEC-m3-librechat-console.md`` §3 (the sources
requirement) + Deliverable §4.4. Behind
``AUDITTRACE_RESPONSE_SOURCES=trailer|off`` (default ``off``) — the
orchestrator appends this trailer to a chat completion's answer so a
LibreChat-rendered pilot response shows *"chaque réponse cite les
documents qu'elle a consultés"*.

**This is a RENDERING task over already-audited data, never a data task.**
The trailer is built exclusively from the ``source_ref`` fields ADR-060
(``tools/memory_handlers.py::_recall_identity_fields``) already stamps
into every recall match, which ride into the ``tool_calls.result_summary``
column verbatim via ``PendingToolCall.result_summary`` (see
``_memory_tool_loop.py::_execute_memory_tool``). Building the trailer from
``pending`` — the SAME in-memory records the caller flushes to that column
— means a byte-compare between the trailer and the persisted audit row is
a compare against identical source data, not a second, potentially
divergent, read path.

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

import json
import logging

from audittrace.routes._memory_tool_loop import PendingToolCall

logger = logging.getLogger(__name__)

SOURCES_TRAILER_LABEL = "Sources consultées"
"""Fixed French label (spec §3.2) — retrieval, never causation wording."""


def _extract_source_refs(pending: list[PendingToolCall]) -> list[str]:
    """Pull the recorded ``source_ref`` values out of ``pending``, in
    first-seen order, deduplicated.

    Only rows that actually carry a ``matches`` list contribute — this is
    what naturally excludes ``recall_recent_sessions`` (no ``source_ref``
    on its match shape) and ``read_decision``/``read_skill`` (single-doc
    shape, no ``matches`` key at all) without any per-tool special-casing.
    Errored tool calls (``error is not None``) contribute nothing — there
    is no result to cite.

    ``result_summary`` is truncated to 1000 chars at the point it is
    recorded (``_execute_memory_tool``); a very large match set can in
    principle produce invalid JSON at the cut. This is a rendering
    concern, never a reason to break the chat response — malformed rows
    are logged and skipped, not raised.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for rec in pending:
        if rec.error is not None or not rec.result_summary:
            continue
        try:
            parsed = json.loads(rec.result_summary)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "sources trailer: result_summary for tool=%s is not valid "
                "JSON (likely truncated at the 1000-char audit cap) — "
                "omitting its matches from the trailer",
                rec.tool_name,
            )
            continue
        if not isinstance(parsed, dict):
            continue
        matches = parsed.get("matches")
        if not isinstance(matches, list):
            continue
        for match in matches:
            if not isinstance(match, dict):
                continue
            source_ref = match.get("source_ref")
            if isinstance(source_ref, str) and source_ref and source_ref not in seen:
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
