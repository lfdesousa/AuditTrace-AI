"""MCP-only write/curation tool handlers (ADR-063 Phase 2 Track A).

Defines the actual ``write_decision``/``write_skill`` handlers and
decorates them into ``tools.mcp_write_registry.MCP_WRITE_TOOL_REGISTRY``
(a separate module — see that module's docstring for why the registry
and the registrant module must not be the same module). This module is
the one tests safely ``importlib.reload`` for per-test isolation.

**Why a SEPARATE registry, not ``audittrace.tools.MEMORY_TOOL_REGISTRY``.**
The parent spec's global out-of-scope line is explicit: "Any
``/v1/chat/completions`` change." ``audittrace.tools.tools_visible_to`` —
the function ``routes/chat.py``'s tool-call loop reads to build the
OpenAI ``tools`` array — iterates ``MEMORY_TOOL_REGISTRY`` unconditionally.
Registering write/curation tools into that SAME dict would silently hand
every scoped chat caller a new mutation capability with zero code touched
in ``chat.py`` — a real behavioural change to ``/v1/chat/completions``
riding in through a shared-registry side door, not the byte-stable
additive surface the spec requires. ``MCP_WRITE_TOOL_REGISTRY`` here is
structurally invisible to ``tools_visible_to`` (a different dict
entirely), so ``/v1/chat/completions`` is provably unchanged by this
module's existence — not merely unchanged by convention.

It is equally invisible to ``services.mcp_bridge`` — Phase 1's
``list_read_tools``/``call_read_tool`` keep iterating
``MEMORY_TOOL_REGISTRY`` only, so "Phase 1 read tools + mcp_bridge.py
behavior unchanged" (the spec's other additive-only invariant) holds by
construction, not by care. See
``tests/test_mcp_routes.py::TestToolsListManifest::test_manifest_excludes_injected_write_tool``
— the frozen Phase 1 guard this design keeps trivially true: a write
tool registered into ``MEMORY_TOOL_REGISTRY`` must never reach Phase 1's
manifest; a write tool registered HERE never reaches
``MEMORY_TOOL_REGISTRY`` in the first place.

**No ``is_admin`` bypass (deliberate deviation from ``list_read_tools`` /
``tools_visible_to``'s convention).** Every other memory-tool gate in
this codebase treats ``UserContext.is_admin`` as a universal bypass. The
sentinel identity ``require_user`` returns in the default
(``AUDITTRACE_AUTH_REQUIRED=false``, laptop-default) bypass mode is
``is_admin=True`` with NO ``memory:decisions:write``/``memory:skills:write``
in its scope tuple (``audittrace.identity.sentinel_user_context``). If
write-tool visibility bypassed on ``is_admin`` the same way read tools
do, the sentinel/dev-mode caller would silently gain two mutation tools
the moment this module is imported — the opposite of "operator-tier,
gated behind the stricter per-tool authz" the spec calls for ("Mutations
demand it"). ``services.mcp_write_bridge`` therefore checks the LITERAL
scope grant only, with no admin short-circuit — the fail-closed reading
on an otherwise-unspecified point (builder's note, not a deviation on
any falsifiable acceptance criterion).

**Handler contract** — same shape as ``tools/memory_handlers.py``:
``(user_context, args) -> dict``, success shape or ``{"error": "..."}"``
on known-bad input; unexpected exceptions propagate to the bridge layer
(``services.mcp_write_bridge.call_write_tool``), which wraps them exactly
like ``audittrace.tools.invoke_tool`` does for read tools.

**Write target.** Both tools upsert into the ``ChromaSemanticService``
(the SAME service ``POST /memory/semantic`` uses) at the caller's
PRIVATE tier only (``tier="private"``) — corpus promotion
(``memory:corpus:{decisions,skills}:write``) is deliberately out of
scope for this Track; an MCP write tool never accepts a caller-supplied
tier. Each write also records a manifest row
(``MemoryManifestService.record_create``, layer ``"semantic"``) and a
tamper-evident memory-audit event (``services.memory_audit.
emit_memory_audit_event``) — the SAME two side effects
``routes/memory.py::create_semantic`` produces, so a document written
via MCP is indistinguishable, downstream, from one written via the REST
API (visible to ``recall_decisions``/``recall_skills``/``recall_semantic``,
listed in ``GET /memory/semantic``, etc.).
"""

from __future__ import annotations

import logging
from typing import Any

from audittrace.dependencies import get_memory_manifest_service, get_semantic_service
from audittrace.identity import UserContext
from audittrace.services.memory_audit import emit_memory_audit_event
from audittrace.tools.mcp_write_registry import register_mcp_write_tool

logger = logging.getLogger(__name__)

# Caller-supplied argument keys stripped before dispatch — same guard,
# same rationale as ``mcp_bridge._strip_identity_args``
# (``feedback_never_trust_caller_metadata_for_security_fields``).
_IDENTITY_ARG_KEYS = frozenset({"user_id", "sub", "userId"})

# Metadata keys stripped from a caller-supplied ``metadata`` payload —
# mirrors ``routes/memory.py::_sanitize_semantic_metadata``: ``tier`` and
# ``user_id`` are token-derived/route-authorized values, never
# caller-supplied, even from an operator holding write scope.
_SECURITY_METADATA_KEYS = frozenset({"tier", "user_id"})


def _sanitize_metadata(metadata: Any) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    return {k: v for k, v in metadata.items() if k not in _SECURITY_METADATA_KEYS}


async def _write_semantic_document(
    user_context: UserContext, args: dict[str, Any], *, collection: str
) -> dict[str, Any]:
    """Shared implementation for ``write_decision``/``write_skill`` —
    upsert (private tier only) → manifest row → memory-audit event."""
    clean_args = {k: v for k, v in args.items() if k not in _IDENTITY_ARG_KEYS}

    document_id = clean_args.get("document_id")
    text = clean_args.get("text")
    if not isinstance(document_id, str) or not document_id:
        return {"error": "document_id is required and must be a non-empty string"}
    if not isinstance(text, str) or not text:
        return {"error": "text is required and must be a non-empty string"}
    title = clean_args.get("title")
    if title is not None and not isinstance(title, str):
        return {"error": "title must be a string if provided"}
    metadata = _sanitize_metadata(clean_args.get("metadata"))

    service = get_semantic_service()
    try:
        await service.upsert(
            user_context, collection, document_id, text, metadata, tier="private"
        )
    except Exception as exc:
        logger.exception(
            "mcp write tool upsert failed collection=%s document_id=%s",
            collection,
            document_id,
        )
        return {"error": f"{exc.__class__.__name__}: write failed"}

    manifest = get_memory_manifest_service()
    entry = await manifest.record_create(
        layer="semantic",
        key=f"{collection}/{document_id}",
        title=title,
        size_bytes=len(text.encode("utf-8")),
        user_id=user_context.user_id,
        tier="private",
    )

    try:
        await emit_memory_audit_event(
            user=user_context,
            op="write",
            layer="semantic",
            collection=collection,
            key=document_id,
        )
    except Exception as exc:
        # Fail-closed like routes/memory.py::_emit_write_audit — the
        # mutation above already landed (same known limitation that
        # wrapper accepts: the audit-write failure is reported, not the
        # mutation undone), but the caller is told plainly rather than
        # seeing a false "success".
        logger.exception(
            "mcp write tool memory-audit event failed collection=%s document_id=%s",
            collection,
            document_id,
        )
        return {
            "error": (
                f"memory audit-write failed — mutation not confirmed audited: {exc}"
            )
        }

    return {
        "document_id": document_id,
        "collection": collection,
        "tier": "private",
        "manifest_id": entry.id,
        "created_at_ms": entry.created_at_ms,
        "modified_at_ms": entry.modified_at_ms,
    }


_WRITE_DOCUMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "document_id": {
            "type": "string",
            "description": "Stable identifier for the document within the collection.",
        },
        "text": {
            "type": "string",
            "description": "Full document text to embed and store.",
        },
        "title": {"type": "string", "description": "Optional human-readable title."},
        "metadata": {
            "type": "object",
            "description": (
                "Optional extra metadata. 'tier' and 'user_id' are stripped "
                "if present — always token-derived, never caller-supplied."
            ),
        },
    },
    "required": ["document_id", "text"],
}


@register_mcp_write_tool(
    name="write_decision",
    description=(
        "Write or update a document in the curated 'decisions' semantic "
        "collection (vector-searchable via recall_decisions/recall_semantic "
        "once indexed). Operator/curator-tier tool (requires "
        "memory:decisions:write). Always writes to YOUR private tier — "
        "corpus promotion is not available through this tool."
    ),
    parameters_schema=_WRITE_DOCUMENT_SCHEMA,
    required_scope="memory:decisions:write",
)
async def write_decision(
    user_context: UserContext, args: dict[str, Any]
) -> dict[str, Any]:
    return await _write_semantic_document(user_context, args, collection="decisions")


@register_mcp_write_tool(
    name="write_skill",
    description=(
        "Write or update a document in the curated 'skills' semantic "
        "collection (vector-searchable via recall_skills/recall_semantic "
        "once indexed). Operator/curator-tier tool (requires "
        "memory:skills:write). Always writes to YOUR private tier — corpus "
        "promotion is not available through this tool."
    ),
    parameters_schema=_WRITE_DOCUMENT_SCHEMA,
    required_scope="memory:skills:write",
)
async def write_skill(
    user_context: UserContext, args: dict[str, Any]
) -> dict[str, Any]:
    return await _write_semantic_document(user_context, args, collection="skills")
