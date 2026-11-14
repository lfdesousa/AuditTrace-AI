"""Scopes of the chat-turn token exchange (LC10A-CHAT-EXCHANGE-SCOPE).

Constants only, like the ``bff/*_scopes.py`` siblings. ``CHAT_SCOPES`` is
exactly what ``/v1/chat/completions`` and the seven memory tools of the
tool loop read from the caller's token (measured sufficient and minimal:
dropping any one memory scope hides at least one tool): the route's
``audittrace:query`` plus one read scope per recall layer. No ``:write``,
no admin, no corpus. The tests pin this tuple by equality to the set
derived from the server's chat route and tool registry.

``OIDC_PROTOCOL_SCOPES`` are scopes Keycloak adds by its own rules and
that carry no privilege in the memory server (neither is in
``audittrace.auth.ALL_SCOPES``); the ceiling tolerates them.
``CHAT_SCOPE_CEILING`` is an allow-list: a minted token whose scope set
is not a subset of it is refused, whatever the extra scope is called.
"""

from __future__ import annotations

CHAT_SCOPES: tuple[str, ...] = (
    "audittrace:query",
    "memory:conversational:read-own",
    "memory:episodic:read",
    "memory:procedural:read",
    "memory:semantic:read",
    "memory:session:read-own",
)
CHAT_SCOPE_STRING = " ".join(CHAT_SCOPES)
OIDC_PROTOCOL_SCOPES = frozenset({"offline_access", "openid"})
CHAT_SCOPE_CEILING = frozenset(CHAT_SCOPES) | OIDC_PROTOCOL_SCOPES
