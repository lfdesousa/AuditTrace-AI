"""Row-Level Security (RLS) plumbing — DESIGN §16 Phase 4.

Phase 4 of the multi-user identity work adds infrastructure-level
isolation on top of the Phase 2 service-layer filter. The contract:

- Every resolved ``UserContext`` populates a module-level ``ContextVar``
  when ``require_user`` returns.
- A SQLAlchemy ``after_begin`` event listener reads the ContextVar and
  issues ``set_config('app.current_user_id', <sub>, true)`` on the
  underlying connection at the start of every transaction. The ``true``
  makes it session-LOCAL — scoped to the current transaction only, no
  leak via connection pooling.
- Postgres RLS policies (Alembic migration 005) then compare every
  row's ``user_id`` column against ``current_setting('app.current_user_id',
  true)``. Rows that don't match are filtered out at the database
  layer, regardless of any service-code bug that might forget the
  explicit ``WHERE``.
- The listener **skips silently** on non-Postgres dialects. SQLite has
  no RLS concept and the existing test suite relies on the service-
  layer filter (Phase 2). This keeps the fast SQLite path working
  unchanged.

This module exposes the primitives only. Wiring into ``auth.py`` and
the FastAPI dependency graph happens in the respective modules.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar

from sqlalchemy import event, text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ─────────────────────────── ContextVar contract ───────────────────────────
# The ContextVar is the request-scoped handoff between the auth layer
# (which resolves the user) and the DB session factory (which emits
# SET LOCAL). FastAPI runs each request handler in its own async task,
# and ContextVars propagate through ``asyncio.to_thread`` / ``run_in_executor``
# so the listener sees the correct user even when the chat handler does
# sync DB work on a thread pool.
#
# Default is ``None`` which means "no scope" — the listener does not
# emit any SQL, and RLS's empty-string default filters every row.
# Safe-by-default.


_current_user_id: ContextVar[str | None] = ContextVar(
    "_sovereign_current_user_id", default=None
)


def set_current_user_id(user_id: str | None) -> None:
    """Bind (or clear) the current user id for this request.

    Called from ``auth.require_user`` after resolving a ``UserContext``.
    Passing ``None`` clears the binding so any subsequent DB query is
    denied by the RLS policy — useful on logout paths or error cleanup.
    """
    _current_user_id.set(user_id)


def current_user_id() -> str | None:
    """Return the user id bound to the current request, or ``None``.

    Exposed for tests and defensive code that wants to check whether
    a DB query is about to run un-scoped.
    """
    return _current_user_id.get()


# ─────────────────────── SQLAlchemy event listener ─────────────────────────
# The listener runs inside the ``Session.after_begin`` hook, which fires
# at the start of every transaction. Since Postgres ``SET LOCAL`` /
# ``set_config(... , true)`` is transaction-scoped, re-emitting on
# every ``begin`` is exactly what we need.
#
# It's attached GLOBALLY to the SQLAlchemy ``Session`` class so every
# session created anywhere in the app inherits the behaviour without
# needing per-session wiring. The dialect gate inside the function
# body means the listener is a no-op on SQLite — the existing fast
# in-memory test path is unaffected.


def _apply_rls_guc(session, transaction, connection) -> None:  # noqa: ARG001
    """Push the request-scoped user id into Postgres as a GUC.

    Emitted via ``set_config('app.current_user_id', :uid, true)`` where
    the third argument ``true`` means LOCAL (transaction-scoped). This
    is equivalent to ``SET LOCAL app.current_user_id = :uid`` but
    parameterizable cleanly through SQLAlchemy's text() binding.

    No-op when:

    - the dialect is not PostgreSQL (tests against SQLite)
    - the ContextVar is unset (no user bound to the current request —
      the RLS policy's empty-string default denies every row)
    """
    if connection.dialect.name != "postgresql":
        return
    user_id = _current_user_id.get()
    if user_id is None:
        return
    connection.execute(
        text("SELECT set_config('app.current_user_id', :uid, true)"),
        {"uid": user_id},
    )


_listener_installed = False


def install_rls_listener() -> None:
    """Attach the after_begin listener to ``sqlalchemy.orm.Session``.

    Idempotent — repeated calls are no-ops. Production code calls this
    once at app startup (``server.py`` lifespan); tests can call it
    from conftest without worrying about double-registration.
    """
    global _listener_installed
    if _listener_installed:
        return
    event.listen(Session, "after_begin", _apply_rls_guc)
    _listener_installed = True
    logger.debug("RLS after_begin listener installed on Session")


def uninstall_rls_listener() -> None:
    """Detach the listener. Test-only escape hatch so fixtures can
    run a SQLAlchemy session without the RLS side effect."""
    global _listener_installed
    if not _listener_installed:
        return
    event.remove(Session, "after_begin", _apply_rls_guc)
    _listener_installed = False
