"""``audittrace.services.console_store`` — the foundational store abstraction
of the MongoDB-elimination EPIC (WU-A, spec 2026-09-13 + ADDENDUM A).

Shape::

    ConsoleStoreBase[T]  (ABC; sealed template helpers)
      ├── PostgresConsoleStore[T]   (shared; parameterized by a domain)
      └── MockConsoleStore[T]       (shared; parameterized by a domain)
    ConsoleDomain[T]     (what ONE domain declares: model, key, values,
                          order, hooks — never a session, never a row)

The base OWNS, once and concretely: the RLS ``user_sub`` predicate on every
query AND every aggregate; ``trace_id`` + ``session_id`` stamping from the
request context on every write (M5); soft-delete-then-re-create (D13);
serialize-inside-the-session (#364); caller-metadata rejection (a body can
never reach a server-stamped column). A domain declares only its specifics
and cannot re-implement those invariants wrong — the escape hatches are
closed structurally (see ``_sealing.py`` for what is closed and what is
disclosed as residual).

Package layout follows PYTHON-ENGINEERING §11: one concern per module,
each well under 500 LOC.
"""

from __future__ import annotations

from audittrace.services.console_store._base import ConsoleStoreBase
from audittrace.services.console_store._context import (
    RESERVED_COLUMNS,
    WriteStamp,
    bind_session_id,
    build_write_stamp,
    current_session_id,
    current_trace_id_hex,
    resolve_user_sub,
)
from audittrace.services.console_store._cursor import (
    Direction,
    decode_cursor,
    encode_cursor,
)
from audittrace.services.console_store._domain import ConsoleDomain, validate_domain
from audittrace.services.console_store._errors import (
    ConsoleStoreCapExceededError,
    ConsoleStoreDomainError,
    ConsoleStoreError,
    ConsoleStoreForbiddenFieldError,
    ConsoleStoreScopeError,
    ConsoleStoreSealedError,
)
from audittrace.services.console_store._mock import MockConsoleStore
from audittrace.services.console_store._postgres import PostgresConsoleStore

__all__ = [
    "RESERVED_COLUMNS",
    "ConsoleDomain",
    "ConsoleStoreBase",
    "ConsoleStoreCapExceededError",
    "ConsoleStoreDomainError",
    "ConsoleStoreError",
    "ConsoleStoreForbiddenFieldError",
    "ConsoleStoreScopeError",
    "ConsoleStoreSealedError",
    "Direction",
    "MockConsoleStore",
    "PostgresConsoleStore",
    "WriteStamp",
    "bind_session_id",
    "build_write_stamp",
    "current_session_id",
    "current_trace_id_hex",
    "decode_cursor",
    "encode_cursor",
    "resolve_user_sub",
    "validate_domain",
]
