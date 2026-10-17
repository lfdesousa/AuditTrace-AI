"""The scope pair the BFF requests when exchanging a token for the
``/console/tool-favorites/*`` proxy path (Tool-Favorites domain,
MongoDB-elimination EPIC).

Deliberately a SEPARATE module from ``bff/memory_scopes.py``,
``bff/console_files_scopes.py``, ``bff/console_promote_scopes.py``,
``bff/console_conversations_scopes.py``, ``bff/console_presets_scopes.py``,
``bff/console_prompts_scopes.py``, ``bff/console_chat_projects_scopes.py``,
``bff/console_agents_scopes.py``, and
``bff/console_conversation_tags_scopes.py`` — same rationale as those
modules' own docstrings: each distinct proxy concern carries its OWN
scope-request constant, so a future edit to one can never accidentally
widen another's ceiling. This exchange carries BOTH the read-own and
write scopes together (mirroring
``bff/console_conversation_tags_scopes.py``'s rationale) because the
downstream ``/console/tool-favorites/*`` surface is a full list/add/
remove API with per-route scope enforcement already happening at the
orchestrator (``src/audittrace/routes/console_tool_favorites.py`` —
each route declares its own ``Security(validate_jwt, scopes=[...])``);
the BFF only needs to mint a token broad enough to reach whichever
specific route the caller's request targets.

Holds NO other scope — never ``audittrace:admin``, never any
``memory:corpus:*``, never any other layer's read/write scope. Held as a
plain, greppable tuple literal (not a runtime ``assert`` — asserts are
stripped under ``python -O``, so the falsifiable guard is the test
suite): ``tests/bff/test_console_tool_favorites_scopes.py`` fails the
moment a forbidden scope is added to
:data:`CONSOLE_TOOL_FAVORITES_SCOPES`.
"""

from __future__ import annotations

CONSOLE_TOOL_FAVORITES_SCOPES: tuple[str, ...] = (
    "memory:tool_favorites:read-own",
    "memory:tool_favorites:write",
)

# Keycloak's token-exchange `scope` request parameter is a single
# space-separated string (RFC 8693 §2.1) — precomputed once at import
# time rather than re-joining ``CONSOLE_TOOL_FAVORITES_SCOPES`` per
# request.
CONSOLE_TOOL_FAVORITES_SCOPE_STRING: str = " ".join(CONSOLE_TOOL_FAVORITES_SCOPES)
