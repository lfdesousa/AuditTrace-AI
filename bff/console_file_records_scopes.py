"""The scope pair the BFF requests when exchanging a token for the
``/console/file-records/*`` proxy path (Files-metadata domain,
MongoDB-elimination EPIC).

NAMED ``console_file_records`` rather than ``console_files`` (unlike
every sibling domain module, which names itself after the orchestrator
mount 1:1) because ``bff/console_files_scopes.py`` ALREADY EXISTS — it
holds the narrow ``memory:session:write`` ingest scope for the
pre-existing M3 Sovereign-Attach WU-2 ephemeral file-upload route
(``POST /console/files``, a completely different concern: multipart
bytes forwarded to ``/memory/upload``). This module's own BFF-facing
route path is ``/console/file-records`` for the same collision-avoidance
reason — see ``bff/config.py``'s
``orchestrator_console_files_path_prefix`` docstring for the full
rationale, and ``bff/app.py`` module docstring for the route wiring.

Deliberately a SEPARATE module from ``bff/memory_scopes.py``,
``bff/console_files_scopes.py`` (the ingest one), ``bff/
console_promote_scopes.py``, ``bff/console_conversations_scopes.py``,
``bff/console_presets_scopes.py``, and ``bff/console_prompts_scopes.py``,
and ``bff/console_chat_projects_scopes.py`` — same rationale as those
modules' own docstrings: each distinct proxy concern carries its OWN
scope-request constant, so a future edit to one can never accidentally
widen another's ceiling. This exchange carries BOTH the read-own and
write scopes together (mirroring ``bff/console_chat_projects_scopes.py``'s
rationale) because the downstream ``/console/files/*`` surface is a
full CRUD+batch-get API with per-route scope enforcement already
happening at the orchestrator (``src/audittrace/routes/
console_files.py`` — each route declares its own ``Security(validate_jwt,
scopes=[...])``); the BFF only needs to mint a token broad enough to
reach whichever specific route the caller's request targets.

Holds NO other scope — never ``audittrace:admin``, never any
``memory:corpus:*``, never any other layer's read/write scope, and
never ``memory:session:write`` (the ingest route's own scope — see
above). Held as a plain, greppable tuple literal (not a runtime
``assert`` — asserts are stripped under ``python -O``, so the
falsifiable guard is the test suite):
``tests/bff/test_console_file_records_scopes.py`` fails the moment a
forbidden scope is added to :data:`CONSOLE_FILE_RECORDS_SCOPES`.
"""

from __future__ import annotations

CONSOLE_FILE_RECORDS_SCOPES: tuple[str, ...] = (
    "memory:files:read-own",
    "memory:files:write",
)

# Keycloak's token-exchange `scope` request parameter is a single
# space-separated string (RFC 8693 §2.1) — precomputed once at import
# time rather than re-joining ``CONSOLE_FILE_RECORDS_SCOPES`` per
# request.
CONSOLE_FILE_RECORDS_SCOPE_STRING: str = " ".join(CONSOLE_FILE_RECORDS_SCOPES)
