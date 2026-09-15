"""Exception taxonomy for the console-store base (WU-A).

Every error the base raises derives from :class:`ConsoleStoreError` so a
route can map the family in one ``except`` clause; the leaves exist so a
DOMAIN'S OWN tests can tell a refused caller-metadata field from a refused
descriptor from a cap hit. :class:`ConsoleStoreSealedError` additionally
derives from ``TypeError`` because it is raised at CLASS-CREATION time (from
``__init_subclass__``), the same moment and the same family Python itself
uses for a refused class statement.
"""

from __future__ import annotations


class ConsoleStoreError(Exception):
    """Base class for every error the console-store base raises."""


class ConsoleStoreSealedError(TypeError, ConsoleStoreError):
    """Raised at class-creation time when code OUTSIDE the
    ``audittrace.services.console_store`` package tries to subclass a sealed
    store class, or when a domain overrides a sealed template member.

    This is the runtime backing for PEP 591's ``@final``, which is advisory
    (type-checker only). Falsifiable: neuter ``seal_subclass`` and
    ``tests/test_console_store_hostile.py`` shows a hostile subclass reading
    another user's row.
    """


class ConsoleStoreDomainError(ConsoleStoreError):
    """A :class:`ConsoleDomain` descriptor is invalid — e.g. it declares a
    RESERVED (server-stamped) column as writable, names a column the ORM
    model does not have, or orders by a column that cannot page
    deterministically. Raised by the store constructor, BEFORE any I/O.
    """


class ConsoleStoreScopeError(ConsoleStoreError):
    """The caller's ``UserContext`` cannot be trusted for this request: its
    ``user_id`` is empty, or it disagrees with the ``user_sub`` bound in the
    Postgres-RLS request ContextVar (``audittrace.db.rls``). Fail-closed:
    raised BEFORE any session is opened, so nothing is read or written.
    """


class ConsoleStoreForbiddenFieldError(ConsoleStoreError):
    """A caller or a domain hook tried to supply a server-stamped column
    (``user_sub``, ``trace_id``, ``session_id``, ``deleted_at_ms``, …).
    Those are stamped by the base from the request context and are NEVER
    taken from a body or a hook (``feedback_never_trust_caller_metadata_for_
    security_fields``). Raised BEFORE any session is opened.
    """


class ConsoleStoreCapExceededError(ConsoleStoreError):
    """The domain's :meth:`ConsoleDomain.cap` would be exceeded by a genuine
    transition into "active" (a brand-new row or an un-tombstoned one). The
    COUNT behind this decision is user-scoped by the base
    (``lesson-aggregate-queries-must-be-user-scoped-20260913``)."""

    def __init__(self, domain: str, cap: int, message: str | None = None) -> None:
        super().__init__(message or f"{domain}: maximum of {cap} active rows reached")
        self.domain = domain
        self.cap = cap
