"""The scope pair the BFF requests when exchanging a token for the
``/console/chat-projects/*`` proxy path (Chat-Projects domain,
MongoDB-elimination EPIC).

Deliberately a SEPARATE module from ``bff/memory_scopes.py``,
``bff/console_files_scopes.py``, ``bff/console_promote_scopes.py``,
``bff/console_conversations_scopes.py``, ``bff/console_presets_scopes.py``,
and ``bff/console_prompts_scopes.py`` — same rationale as those modules'
own docstrings: each distinct proxy concern carries its OWN
scope-request constant, so a future edit to one can never accidentally
widen another's ceiling. This exchange carries BOTH the read-own and
write scopes together (mirroring ``bff/console_presets_scopes.py``'s
rationale) because the downstream ``/console/chat-projects/*`` surface
is a full CRUD API with per-route scope enforcement already happening
at the orchestrator (``src/audittrace/routes/console_chat_projects.py``
— each route declares its own ``Security(validate_jwt, scopes=[...])``);
the BFF only needs to mint a token broad enough to reach whichever
specific route the caller's request targets.

Holds NO other scope — never ``audittrace:admin``, never any
``memory:corpus:*``, never any other layer's read/write scope. Held as a
plain, greppable tuple literal (not a runtime ``assert`` — asserts are
stripped under ``python -O``, so the falsifiable guard is the test
suite): ``tests/bff/test_console_chat_projects_scopes.py`` fails the
moment a forbidden scope is added to
:data:`CONSOLE_CHAT_PROJECTS_SCOPES`.
"""

from __future__ import annotations

CONSOLE_CHAT_PROJECTS_SCOPES: tuple[str, ...] = (
    "memory:chat_projects:read-own",
    "memory:chat_projects:write",
)

# Keycloak's token-exchange `scope` request parameter is a single
# space-separated string (RFC 8693 §2.1) — precomputed once at import
# time rather than re-joining ``CONSOLE_CHAT_PROJECTS_SCOPES`` per
# request.
CONSOLE_CHAT_PROJECTS_SCOPE_STRING: str = " ".join(CONSOLE_CHAT_PROJECTS_SCOPES)
