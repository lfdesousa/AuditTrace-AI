"""The memory-scope set the BFF requests when exchanging a token for the
Souvenirs panel's ``/memory/*`` proxy path (M3-WU-D2-1).

The chat path (``bff/app.py::chat_completions``) exchanges for
``audittrace:query`` unchanged — this module exists ONLY for the memory
proxy, so the two paths can never accidentally share a scope request.

Spec (``2026-08-30-SPEC-m3-souvenirs-sovereign-memory.md``, WU-D2-1) names
seven scopes, one per per-user isolated layer (ADR-062) read/write pair
plus the conversational read. The spec text writes the conversational
scope as ``memory:conversational:read`` — the scope that actually exists in
``src/audittrace/auth.py::ALL_SCOPES`` (and in both realm files) is
``memory:conversational:read-own``; this module uses the REAL name so the
exchange request actually matches a grantable scope rather than silently
asking for one that does not exist.

**NEVER ``audittrace:admin``.** The spec is explicit: the console must
never obtain hard-delete of the audit trail. This module holds that
invariant as a plain, greppable tuple literal (not a runtime ``assert`` —
asserts are stripped under ``python -O``, so the falsifiable guard is the
test suite, not a runtime check: see
``tests/bff/test_memory_scopes.py::TestNeverAdminScope``, which fails the
moment ``audittrace:admin`` is added to :data:`MEMORY_SCOPES`).
"""

from __future__ import annotations

MEMORY_SCOPES: tuple[str, ...] = (
    "memory:episodic:read",
    "memory:procedural:read",
    "memory:semantic:read",
    "memory:conversational:read-own",
    "memory:episodic:write",
    "memory:procedural:write",
    "memory:semantic:write",
)

# Keycloak's token-exchange `scope` request parameter is a single
# space-separated string (RFC 8693 §2.1) — precomputed once at import time
# rather than re-joining ``MEMORY_SCOPES`` on every request.
MEMORY_SCOPE_STRING: str = " ".join(MEMORY_SCOPES)
