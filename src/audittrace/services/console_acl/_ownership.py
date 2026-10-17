"""Resource-ownership resolver — Sovereign Authorization Layer EPIC,
**ACL WU-2a** (``2026-09-18-SPEC-acl-wu2a-resource-ownership-
verification.md``).

**Why this module exists.** WU-1's read-path store (``__init__.py`` /
``_postgres.py`` in this package) documents, in ``db/models.py``
(``ConsoleAclEntry``), that ``resource_id``/``resource_type`` carry no
FK and that *"validating the reference is explicitly out of [WU-1's]
scope."* Combined with migration 031's write guard being exactly
``WITH CHECK (user_sub = current_setting('app.current_user_id',
true))``, the ACL write path's headline control — *derive the grantor
from the token, never the body* — is **correct and, on its own,
insufficient**: nothing asks whether the token-derived grantor
actually OWNS the resource named in the row. A caller can grant
themselves a permission on ANY other user's resource and
``has_permission`` will honour it. This module is the fix's
application-layer half.

**This is defence in depth, not the control.** The actual,
unbypassable barrier is the Postgres RLS ``WITH CHECK`` ownership
subquery added by migration 032 (``032_tighten_console_acl_entries_
ownership.py``) — a DB-level barrier an application bug cannot skip.
:func:`owns` exists so a future write path (WU-2b) can reject an
unowned grant EARLY, with a clear, named error, before ever reaching
the database — the same "fail closed and loudly" discipline as the
DB barrier, not a replacement for it.

**The choke point.** Every caller resolves ownership through
:func:`owns` — there is exactly one dispatch table
(``_OWNERSHIP_RESOLVERS``) and exactly one fail-closed path
(:class:`UnknownResourceTypeError`). A resource_type absent from the
table — because no sovereign store has been migrated for it yet
(``mcpServer``, ``remoteAgent``, ``skill``, ``sharedLink`` as of this
WU — see :data:`UNRESOLVED_RESOURCE_TYPES`), or because it is not a
real ``resource_type`` at all — is a **gap**, and a gap that silently
returns ``False`` is indistinguishable from a correct denial
(``feedback_guards_fail_closed_and_cannot_self_validate``). This
module never does that: an unresolved type raises, loudly, by name.

**Why the SAME authenticated ``UserContext`` is threaded through, never
a synthetic one built from a bare ``user_sub`` string.** Postgres RLS
enforcement for ``console_agents``/``console_prompt_groups`` (and every
other console-* domain) keys off the ``app.current_user_id`` GUC, which
``db/rls.py`` sets ONCE per request from a ContextVar bound by
``auth.require_user`` — NOT from any argument passed to a service
method. Constructing a throwaway ``UserContext(user_id=<some other
sub>, ...)`` and passing it to ``get_agent``/``get_group`` would only
change the SQLAlchemy-level ``WHERE user_sub = :x`` filter; the
ambient RLS GUC would still be the REAL caller's sub, and the two
would need to coincide for any row to survive both filters. Since a
grant's ``user_sub`` is always the token-derived caller (the very
thing WU-1's ``WITH CHECK`` already enforces), :func:`owns` is only
ever meaningful — and only ever called — for "does the CALLER (the
context this request already authenticated) own this resource", so it
takes the caller's own ``UserContext`` and reuses it verbatim against
the owning store's existing, RLS-isolated "get my own resource by id"
method. This is a deliberate, documented adaptation of the spec's
illustrative ``owns(user_sub, resource_type, resource_id)`` signature
— the difference is not cosmetic; a bare-``user_sub`` signature would
silently return ``False`` for a legitimate owner whenever the ambient
RLS GUC didn't happen to already match the queried sub, which is
exactly the kind of "gap that looks like a correct denial" this WU
exists to close.

**Enumeration (derived from code, not from spec prose).**
``RESOURCE_TYPES`` (``__init__.py``) lists six values mirrored from the
fork's ``accessPermissions`` surface. Of those, exactly two have a
migrated, single-owner sovereign store today: ``agent``
(``console_agents``, migration 028) and ``promptGroup``
(``console_prompt_groups``, migration 025). The other four have no
sovereign store yet — this module fails closed for them rather than
inventing a resolver ahead of the store that would back it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from audittrace.identity import UserContext
from audittrace.services.console_acl import RESOURCE_TYPES

logger = logging.getLogger(__name__)


class UnknownResourceTypeError(Exception):
    """Raised by :func:`owns` when ``resource_type`` has no ownership
    resolver wired — fail CLOSED, loudly, by name. Never returns
    ``False`` for this case: an unresolved type is a GAP, not a
    denial, and the two must never look identical
    (``feedback_guards_fail_closed_and_cannot_self_validate``)."""

    def __init__(self, resource_type: str) -> None:
        super().__init__(
            f"no ownership resolver registered for resource_type={resource_type!r}; "
            "refusing to authorize (fail-closed) rather than silently deny or allow"
        )
        self.resource_type = resource_type


async def _owns_agent(user_context: UserContext, resource_id: str) -> bool:
    """Ownership predicate for ``resource_type='agent'`` — delegates to
    the sovereign ``console_agents`` store's own RLS-isolated
    "get my own agent by id" method (own-agents-only v1: a non-``None``
    result IS an ownership proof, since no other user's row can ever
    be returned)."""
    from audittrace.dependencies import get_console_agents_service  # noqa: PLC0415

    service = get_console_agents_service()
    row = await service.get_agent(user_context, resource_id)
    return row is not None


async def _owns_prompt_group(user_context: UserContext, resource_id: str) -> bool:
    """Ownership predicate for ``resource_type='promptGroup'`` —
    delegates to the sovereign ``console_prompt_groups`` store's own
    RLS-isolated "get my own group by id" method, same reasoning as
    :func:`_owns_agent`."""
    from audittrace.dependencies import get_console_prompts_service  # noqa: PLC0415

    service = get_console_prompts_service()
    row = await service.get_group(user_context, resource_id)
    return row is not None


# The single dispatch table — the choke point. Every resource_type this
# WU can verify ownership for is registered here EXACTLY once; nothing
# outside :func:`owns` may branch on ``resource_type`` for an
# authorization decision.
_OWNERSHIP_RESOLVERS: dict[str, Callable[[UserContext, str], Awaitable[bool]]] = {
    "agent": _owns_agent,
    "promptGroup": _owns_prompt_group,
}

# Committed enumeration (WU-2a §3.1) — derived from the dispatch table
# above and cross-checked against RESOURCE_TYPES by
# ``tests/test_console_acl_ownership.py``, not hand-copied from spec
# prose.
RESOLVED_RESOURCE_TYPES: frozenset[str] = frozenset(_OWNERSHIP_RESOLVERS)
UNRESOLVED_RESOURCE_TYPES: frozenset[str] = (
    frozenset(RESOURCE_TYPES) - RESOLVED_RESOURCE_TYPES
)


async def owns(
    user_context: UserContext,
    resource_type: str,
    resource_id: str,
) -> bool:
    """Whether ``user_context`` (the CALLER — never an attacker-suppliable
    identity) owns ``resource_id`` of ``resource_type``.

    Raises :class:`UnknownResourceTypeError` — fail CLOSED, never
    ``False`` — for any ``resource_type`` without a registered
    resolver, whether that is because no sovereign store has been
    migrated for it yet (see :data:`UNRESOLVED_RESOURCE_TYPES`) or
    because the string is not a real ``resource_type`` at all. Both
    are the same kind of gap from this function's point of view: an
    unanswerable ownership question must never be silently treated as
    "not owner".
    """
    resolver = _OWNERSHIP_RESOLVERS.get(resource_type)
    if resolver is None:
        logger.warning(
            "acl.ownership.unknown_resource_type",
            extra={"resource_type": resource_type},
        )
        raise UnknownResourceTypeError(resource_type)
    return await resolver(user_context, resource_id)
