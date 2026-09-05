"""The narrow durable-promote scope the BFF requests when exchanging a
token for the ``POST /console/files/{filename}/promote`` route (M3
Sovereign-Attach WU-4, "keep this").

Deliberately a SEPARATE module from both ``bff/console_files_scopes.py``
(WU-2's narrow ``memory:session:write`` INGEST scope) and
``bff/memory_scopes.py`` (the broad seven-scope Souvenirs-panel set) — a
promote exchange must NEVER accidentally carry the session scope (the
whole point of promote is to require a scope the session-only WU-1/2/3
grant does NOT carry) or the broad set. See
``2026-09-05-SPEC-wu4-promote-session-to-durable.md`` §"the end-to-end
posture" — this is the SECOND, DISTINCT RFC 8693 exchange the spec
requires, never a re-use of the session-scoped token.

``target_layer`` is config-driven (``bff/config.py::Settings.
console_promote_default_layer``, default ``"episodic"``), so the scope
requested here MUST be a FUNCTION of that value, not a bare constant
tuple — an operator who repoints the config to ``"semantic"`` must get
``memory:semantic:write`` requested, never a stale ``memory:episodic:
write`` left over from a hardcoded default (that mismatch would make
every promote 403 at the orchestrator's own scope gate).
"""

from __future__ import annotations

# The durable set this BFF seam may EVER promote into — mirrors the
# orchestrator's own ``routes.memory_promote._DURABLE_PROMOTE_LAYERS``
# frozenset exactly (episodic/semantic only — NOT procedural, NOT
# session, NOT conversational). Held as a plain, greppable frozenset
# (not a runtime ``assert`` — asserts are stripped under ``python -O``,
# so the falsifiable guard is the test suite:
# ``tests/bff/test_console_promote_scopes.py::TestNeverSessionOrAdminScope``,
# which fails the moment this set is widened to include a non-durable
# layer).
DURABLE_PROMOTE_LAYERS: frozenset[str] = frozenset({"episodic", "semantic"})


def promote_scope_string_for_layer(target_layer: str) -> str:
    """Return the SINGLE durable write scope for *target_layer* —
    ``memory:<target_layer>:write``, and NOTHING else: never
    ``memory:session:write``, never ``audittrace:admin``, never the
    FULL ``bff.memory_scopes.MEMORY_SCOPE_STRING`` set (the individual
    scope NAME legitimately overlaps one of ``MEMORY_SCOPES`` — the
    Souvenirs panel already has broad durable write access — but the
    promote exchange only ever requests this ONE scope, never the
    seven-scope union).

    Raises ``ValueError`` for any *target_layer* outside
    :data:`DURABLE_PROMOTE_LAYERS` — fail-closed: an operator
    misconfiguring ``console_promote_default_layer`` to a non-durable
    value must never silently produce a request for the wrong (or a
    session/admin) scope.
    """
    if target_layer not in DURABLE_PROMOTE_LAYERS:
        raise ValueError(
            f"target_layer must be one of {sorted(DURABLE_PROMOTE_LAYERS)} "
            f"(got {target_layer!r})"
        )
    return f"memory:{target_layer}:write"
