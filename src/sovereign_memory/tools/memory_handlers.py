"""Memory tool handlers (ADR-025 §Decision.1).

The four tools exposed to the LLM by the proxy-internal tool-call loop.
Each handler is a thin adapter from the canonical
``{matches, total, truncated}`` tool result schema to the underlying
memory service. All four handlers share the same shape so the LLM sees
a stable result schema regardless of which layer answered.

Handler contract (observed by every handler in this file):

  - First positional argument is ``user_context: UserContext`` —
    always threaded into the underlying service's per-user filter.
  - Second positional argument is ``args: dict`` — the JSON payload
    the LLM produced for this tool call.
  - Returns a dict. On success the dict is
    ``{"matches": [...], "total": N, "truncated": bool}``. On known
    bad input (e.g. missing required argument) the dict is
    ``{"error": "..."}``. Unexpected exceptions propagate to
    ``tools.invoke_tool`` which wraps them as ``{"error": ...}``.

The handlers do NOT check the scope gate — that is enforced at tool
advertisement time by ``tools_visible_to``. By the time the tool-call
loop dispatches here, the user is already authorised.

Importing this module has side effects: the four ``@register_memory_tool``
decorators run and populate ``MEMORY_TOOL_REGISTRY``. Production startup
imports this module from ``server.py``; tests that need the handlers
registered import it explicitly.
"""

from __future__ import annotations

import logging
from typing import Any

from sovereign_memory.dependencies import (
    get_conversational_service,
    get_episodic_service,
    get_procedural_service,
    get_semantic_service,
)
from sovereign_memory.identity import UserContext
from sovereign_memory.tools import register_memory_tool

logger = logging.getLogger(__name__)


# Snippet length for every document-style match result. The LLM only needs
# enough context to decide whether the match is relevant; the full content
# can be retrieved on a follow-up call.
_SNIPPET_LIMIT = 400


# ───────────────────────────── recall_decisions ─────────────────────────────


@register_memory_tool(
    name="recall_decisions",
    description=(
        "Recall past architectural decisions (ADRs) relevant to a topic. "
        "Use when the user asks about architectural history, design trade-offs, "
        "or wants to know what was decided and why."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "The topic or keywords to search for, e.g. "
                    "'KV cache compression' or 'OAuth2 token validation'."
                ),
            },
        },
        "required": ["query"],
    },
    required_scope="memory:episodic:read",
)
async def recall_decisions(
    user_context: UserContext, args: dict[str, Any]
) -> dict[str, Any]:
    """Wrap ``EpisodicService.search`` in the canonical tool-result shape."""
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"error": "recall_decisions: 'query' is required and must be a string"}

    episodic = get_episodic_service()
    matches = episodic.search(user_context, query)
    return {
        "matches": [
            {
                "title": d.metadata.get("title", d.metadata.get("file", "ADR")),
                "snippet": d.page_content[:_SNIPPET_LIMIT],
                "source": d.metadata.get("file", ""),
            }
            for d in matches
        ],
        "total": len(matches),
        "truncated": False,
    }


# ─────────────────────────────── recall_skills ──────────────────────────────


@register_memory_tool(
    name="recall_skills",
    description=(
        "Recall relevant skill documents for a topic or technique. "
        "Use when the user asks how to do something, or when the answer "
        "depends on a specific methodology, framework, or practice captured "
        "in the skill library (e.g. IAM, C4 architecture, writing style)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "The skill area or technique to search for, e.g. "
                    "'OAuth2 BFF pattern' or 'C4 model Structurizr DSL'."
                ),
            },
        },
        "required": ["query"],
    },
    required_scope="memory:procedural:read",
)
async def recall_skills(
    user_context: UserContext, args: dict[str, Any]
) -> dict[str, Any]:
    """Wrap ``ProceduralService.search`` in the canonical tool-result shape."""
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"error": "recall_skills: 'query' is required and must be a string"}

    procedural = get_procedural_service()
    matches = procedural.search(user_context, query)
    return {
        "matches": [
            {
                "title": d.metadata.get("skill", d.metadata.get("file", "Skill")),
                "snippet": d.page_content[:_SNIPPET_LIMIT],
                "source": d.metadata.get("file", ""),
            }
            for d in matches
        ],
        "total": len(matches),
        "truncated": False,
    }


# ───────────────────────── recall_recent_sessions ──────────────────────────


@register_memory_tool(
    name="recall_recent_sessions",
    description=(
        "Recall the most recent conversation sessions for a project. "
        "Use when the user asks about continuity — 'what did we work on', "
        "'remind me what we decided last time', 'where did we leave off'. "
        "Scoped to the caller: only returns sessions owned by this user."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "project": {
                "type": "string",
                "description": (
                    "The project identifier to scope the session lookup. "
                    "Pass the current request's project name."
                ),
            },
            "n": {
                "type": "integer",
                "description": "Max number of sessions to return. Default 5.",
                "default": 5,
                "minimum": 1,
                "maximum": 50,
            },
        },
        "required": ["project"],
    },
    required_scope="memory:conversational:read-own",
)
async def recall_recent_sessions(
    user_context: UserContext, args: dict[str, Any]
) -> dict[str, Any]:
    """Wrap ``ConversationalService.load_sessions`` in the canonical shape.

    Session key points are appended to the ``snippet`` field so the LLM
    sees them inline without the schema needing a bespoke ``key_points``
    column.
    """
    project = args.get("project")
    if not isinstance(project, str) or not project.strip():
        return {
            "error": "recall_recent_sessions: 'project' is required and must be a string"
        }

    n_raw = args.get("n", 5)
    try:
        n = max(1, min(int(n_raw), 50))
    except (TypeError, ValueError):
        return {"error": "recall_recent_sessions: 'n' must be an integer"}

    conversational = get_conversational_service()
    sessions = conversational.load_sessions(user_context, project, n)

    matches: list[dict[str, Any]] = []
    for s in sessions:
        snippet = s.get("summary", "")[:_SNIPPET_LIMIT]
        key_points = s.get("key_points") or []
        if key_points:
            kp_joined = "; ".join(str(kp) for kp in key_points)
            snippet = f"{snippet}\nKey points: {kp_joined}"
        matches.append(
            {
                "title": s.get("id", "session"),
                "snippet": snippet,
                "source": s.get("date", ""),
            }
        )
    return {
        "matches": matches,
        "total": len(matches),
        "truncated": False,
    }


# ───────────────────────────── recall_semantic ──────────────────────────────


@register_memory_tool(
    name="recall_semantic",
    description=(
        "Semantic similarity search across the RAG knowledge base. "
        "Use when the user asks a conceptual question where keyword "
        "matching may miss the right document — this tool finds passages "
        "that are semantically related even without literal keyword overlap."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language query to embed and match.",
            },
            "k": {
                "type": "integer",
                "description": "Top-k results to return. Default 4.",
                "default": 4,
                "minimum": 1,
                "maximum": 20,
            },
        },
        "required": ["query"],
    },
    required_scope="memory:semantic:read",
)
async def recall_semantic(
    user_context: UserContext, args: dict[str, Any]
) -> dict[str, Any]:
    """Wrap ``ChromaSemanticService.search`` in the canonical shape."""
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"error": "recall_semantic: 'query' is required and must be a string"}

    k_raw = args.get("k", 4)
    try:
        k = max(1, min(int(k_raw), 20))
    except (TypeError, ValueError):
        return {"error": "recall_semantic: 'k' must be an integer"}

    semantic = get_semantic_service()
    matches = semantic.search(user_context, query, k=k)
    return {
        "matches": [
            {
                "title": d.metadata.get("source", d.metadata.get("file", "?")),
                "snippet": d.page_content[:_SNIPPET_LIMIT],
                "source": d.metadata.get("collection", ""),
            }
            for d in matches
        ],
        "total": len(matches),
        "truncated": False,
    }


__all__ = [
    "recall_decisions",
    "recall_skills",
    "recall_recent_sessions",
    "recall_semantic",
]
