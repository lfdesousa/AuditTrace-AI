"""Scopes of the evidence proxy's token exchange (CONSOLE-AUDIT-EVIDENCE WU-C).

Constants only, like the ``bff/*_scopes.py`` siblings (scope module 13).
``EVIDENCE_SCOPES`` is exactly what the memory server's ``/evidence/*`` routes
require of the caller's token: ``audittrace:audit``, and nothing else. No
``audittrace:query``, no memory scope, no admin. The tests pin this tuple by
equality, and pin the realm client that mints it (``audittrace-librechat-evidence``,
whose DEFAULT scopes are exactly this tuple).

``EVIDENCE_SCOPE_CEILING`` is the allow-list passed to ``exchange_token``: the
evidence scope plus the OIDC protocol scopes Keycloak adds by its own rules
(``bff.chat_scopes.OIDC_PROTOCOL_SCOPES``; they carry no privilege in the memory
server). A minted token whose scope set is not a subset of it is refused before
the ``granted=`` line is logged, whatever the extra scope is called.
"""

from __future__ import annotations

from bff.chat_scopes import OIDC_PROTOCOL_SCOPES

EVIDENCE_SCOPES: tuple[str, ...] = ("audittrace:audit",)
EVIDENCE_SCOPE_STRING = " ".join(EVIDENCE_SCOPES)
EVIDENCE_SCOPE_CEILING = frozenset(EVIDENCE_SCOPES) | OIDC_PROTOCOL_SCOPES
