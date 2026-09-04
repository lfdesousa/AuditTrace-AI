"""The narrow ephemeral-ingest scope the BFF requests when exchanging a
token for the ``POST /console/files`` route (M3 Sovereign-Attach WU-2).

This is the narrow half of the WU-1 least-privilege wall: WU-1 (merged
``main @ 6d24735``) built the enforced ``session`` MemoryLayer + the
``memory:session:write`` scope on the orchestrator side, and granted that
scope OPTIONAL on ``audittrace-librechat``. WU-2 is the first BFF path
that actually requests it.

Deliberately a SEPARATE module from ``bff/memory_scopes.py`` — the broad
Souvenirs-panel scope set (seven durable read/write scopes) and this
narrow one-scope ingest set must never accidentally share a scope
request, for the same reason the chat path and the memory-proxy path
each carry their own scope constant. A console file-upload's exchange
must NEVER send ``bff.memory_scopes.MEMORY_SCOPE_STRING``, any
``memory:{episodic,procedural,semantic}:write``, any ``memory:corpus:*``
scope, or ``audittrace:admin`` — it may EVER only carry
``memory:session:write``, which the orchestrator's ``session`` layer
enforces (WU-1) refuses to place on any durable layer and refuses PDF
content into (400).

Held as a plain, greppable tuple literal (not a runtime ``assert`` —
asserts are stripped under ``python -O``, so the falsifiable guard is
the test suite, not a runtime check: see
``tests/bff/test_console_files_scopes.py::TestNeverDurableOrAdminScope``,
which fails the moment any durable/corpus/admin scope is added to
:data:`INGEST_SCOPES`). Mirrors ``bff/memory_scopes.py``'s own
discipline for the same reason.
"""

from __future__ import annotations

INGEST_SCOPES: tuple[str, ...] = ("memory:session:write",)

# Keycloak's token-exchange `scope` request parameter is a single
# space-separated string (RFC 8693 §2.1) — precomputed once at import
# time rather than re-joining ``INGEST_SCOPES`` on every request.
INGEST_SCOPE_STRING: str = " ".join(INGEST_SCOPES)
