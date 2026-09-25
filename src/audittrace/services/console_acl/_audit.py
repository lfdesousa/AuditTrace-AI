"""The sovereign ACL audit writer — Sovereign Authorization Layer EPIC,
**ACL 2b-core-B** (``2026-09-25-SPEC-acl-2b-core-B-audit-writer-
CONSOLIDATED-v2.md``).

**Why this module exists.** Every future ACL write (WU-2b-core-A's
``grantPermission``/``revokePermission``/``modifyPermissionBits``/
``bulkWriteAclEntries``/``deleteAclEntries``) needs a regulator-facing
audit trail — *who tried to grant/revoke what, and did it happen* — but
this module builds **no write method and no route**. It builds the
closed writer interface those future methods will call: :func:`record`
for a write that succeeded, :func:`record_denial` for one that was
refused. Rows are **``InteractionRecord``** (``db/models.py``, table
``interactions``) — not a new table; every ACL fact fits the existing
audit-schema freeze (additive, no migration needed, see ``event_class``
below).

**§3.2 — the derivation rule is the control, and it is why this module
takes NO ``trace_id``, NO ``session_id``, NO ``user_id``, NO
``granted_by``, NO ``user_sub`` parameter.** The precedent this module
deliberately does NOT copy, ``services/memory_audit.py:130``, accepts
``trace_id`` as an *optional* parameter
(``trace_id if trace_id is not None else _current_trace_id_hex()``) — a
writer built that way is satisfied by a **test-supplied** value without
ever exercising choke-stamping, and a caller could later pass a
caller-controlled trace. Here, every security-relevant / traceability
column is derived from the ACTIVE REQUEST CONTEXT, never from an
argument a caller could set to an arbitrary value
(``feedback_never_trust_caller_metadata_for_security_fields``):

* ``user_id`` / ``granted_by`` — both ``user_context.user_id``, the
  SAME authenticated :class:`~audittrace.identity.UserContext` the
  caller already resolved via ``require_user`` (never a bare string
  parameter reconstructed from who-knows-where — the same reasoning
  ``services/console_acl/_ownership.py`` gives for threading the real
  ``UserContext`` through rather than a synthetic one). 2b-core-B never
  models "grant on another user's behalf" — that is always the acting
  caller.
* ``session_id`` — ``console_store._context.current_session_id()``, the
  M5 request-scoped ContextVar (``NULL`` today; invariant 8 / decision
  D-R keeps it mandatory-NULL until the M5 retrofit wires
  ``X-Session-Id``).
* ``trace_id`` — ``console_store._context.current_trace_id_hex()``, the
  SAME OpenTelemetry-span derivation ``routes/chat.py::
  _current_trace_id_hex`` uses for ``interactions.trace_id`` — ``None``
  when no span is active (the laptop telemetry no-op default). Proven
  by **N7**: replace this call with a hard-coded ``None`` and the §4.1
  reconstruction match MUST go RED.

Proven by **N7/N8** (this module's own test suite), never by code
inspection alone (``feedback_unpinnable_claim_check_your_own_
techniques``).

**§5 — denial rows commit in an INDEPENDENT transaction, never a
parameter-supplied identity.** :func:`record_denial` opens its OWN
session via ``get_postgres_factory().get_session_factory()`` and
commits on it directly. This is deliberate: a DB-level refusal rolls
back the transaction it happened in, and a denial row written INSIDE
that same transaction would roll back with it — the one artefact that
must survive the very failure it documents. The new session's RLS GUC
is stamped by the ALREADY-INSTALLED global ``after_begin`` listener
(``db/rls.py::install_rls_listener``) reading the ALREADY-BOUND
request-scoped ContextVar (``db/rls.py::set_current_user_id``, set once
per request by ``auth.require_user``) — this module never calls
``set_config`` or ``set_current_user_id`` itself. A writer that took an
identity parameter and pushed it into the GUC directly would be
caller-chosen RLS identity, which is exactly what §5.4 forbids: if the
ambient ContextVar is unbound, the new session's ``WITH CHECK`` refuses
the write and this module lets that raise (fail-closed, never a silent
downgrade).

**§1 — the closed interface.** Only :func:`record` and
:func:`record_denial` are public; both raise rather than swallow a
failed audit write (**N4** — the writer's half; the CALLER's half, i.e.
that 2b-core-A must never catch and discard that exception either, is
logged as a forward obligation since no caller exists yet in this WU).
:func:`record` takes the CALLER's own already-open ``AsyncSession`` and
does **not** commit it — the audit INSERT rides in the SAME transaction
as 2b-core-A's future domain write, so a later rollback of that
transaction takes the "successful" audit row down with it too (2b-
core-A's rule, the mirror image of §5.2's denial-row rule: "a rolled-
back write with a surviving audit row is a false record").

**§3.1 — the payload, every field hash-covered.** ``content_hash =
integrity.content_hash(fields)`` computed exactly as ``routes/chat.py:
594`` and ``routes/audit.py:388`` do — over the SAME
``integrity._CONTENT_FIELDS`` tuple, which includes ``event_class`` and
``trace_id``. The precedent ``services/memory_audit.py:123-134`` sets
NO ``content_hash`` at all (``verify_content_hash`` returns ``False``
on a ``NULL`` stored hash) — that is a documented gap in that module,
not a pattern to copy here.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from audittrace.db.models import InteractionRecord
from audittrace.dependencies import get_postgres_factory
from audittrace.identity import UserContext
from audittrace.integrity import content_hash as _content_hash
from audittrace.services.console_acl import (
    PERMISSION_BIT_DELETE,
    PERMISSION_BIT_EDIT,
    PERMISSION_BIT_SHARE,
    PERMISSION_BIT_VIEW,
)
from audittrace.services.console_store._context import (
    current_session_id,
    current_trace_id_hex,
)

logger = logging.getLogger(__name__)

# ── §7 — the event_class literal, ONE constant, imported everywhere it is
# registered (memory_scan.py's closed set, memory.py's re-export, audit.py's
# and models.py's documentation) so the four sites can never independently
# drift on the string itself. 9 chars — `acl_authorization` (17) would
# overflow `interactions.event_class` (String(16)).
EVENT_CLASS_ACL_AUTHZ = "acl_authz"

# §3.1 — project/source constants. `source` gives a filter that isolates
# this class independently of `event_class` itself (defence in depth if the
# event_class column is ever queried loosely).
_PROJECT = "console-acl"
_SOURCE = "console-acl"

# §3.1 — the closed, pinned failure_class vocabulary for denial rows.
# Extending this set is a SOC-tooling-shape change (same discipline as
# routes/memory_scan.py's _EVENT_CLASS_VALUES) — never done silently.
FAILURE_CLASS_ACL_DENIED_POLICY = "acl_denied_policy"
FAILURE_CLASS_ACL_DENIED_PRINCIPAL_TYPE = "acl_denied_principal_type"
FAILURE_CLASS_ACL_DENIED_PAST_EXPIRY = "acl_denied_past_expiry"
FAILURE_CLASS_ACL_DENIED_BULK_ROLLBACK = "acl_denied_bulk_rollback"
FAILURE_CLASS_ACL_AUDIT_WRITE_FAILED = "acl_audit_write_failed"

ACL_DENIAL_FAILURE_CLASSES: frozenset[str] = frozenset(
    {
        FAILURE_CLASS_ACL_DENIED_POLICY,
        FAILURE_CLASS_ACL_DENIED_PRINCIPAL_TYPE,
        FAILURE_CLASS_ACL_DENIED_PAST_EXPIRY,
        FAILURE_CLASS_ACL_DENIED_BULK_ROLLBACK,
        FAILURE_CLASS_ACL_AUDIT_WRITE_FAILED,
    }
)

# The four PermissionBits, named, for the answer payload's `bits` breakdown
# (§3.1's `answer` column). Uses the SAME mirrored constant names the rest
# of the console_acl package uses (VIEW/EDIT/DELETE/SHARE — `__init__.py`),
# not an invented vocabulary.
_NAMED_BITS: tuple[tuple[str, int], ...] = (
    ("VIEW", PERMISSION_BIT_VIEW),
    ("EDIT", PERMISSION_BIT_EDIT),
    ("DELETE", PERMISSION_BIT_DELETE),
    ("SHARE", PERMISSION_BIT_SHARE),
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _bit_breakdown(perm_bits: int) -> dict[str, bool]:
    """Decompose ``perm_bits`` into its named flags — containment
    (``&``), never equality, matching the package-wide rule
    (``__init__.py``'s module docstring, epic invariant 2)."""
    return {name: bool(perm_bits & bit) for name, bit in _NAMED_BITS}


def _principal_repr(principal_type: str, principal_id: str | None) -> str:
    return f"{principal_type}:{principal_id if principal_id is not None else '-'}"


def _question(
    *,
    op: str,
    principal_type: str,
    principal_id: str | None,
    resource_type: str,
    resource_id: str,
    perm_bits: int,
    tenant_id: str | None,
) -> str:
    """§3.1's exact ``question`` shape: ``op=<method> principal=<type>:<id>
    resource=<type>:<id> bits=<int> tenant=<id or ->``."""
    principal = _principal_repr(principal_type, principal_id)
    resource = f"{resource_type}:{resource_id}"
    tenant = tenant_id if tenant_id else "-"
    return f"op={op} principal={principal} resource={resource} bits={perm_bits} tenant={tenant}"


def _require_user_id(user_context: UserContext) -> str:
    """Fail-closed: never persist an ACL audit row with no owner — it
    would be permanently unreadable under RLS (mirrors
    ``services/memory_audit.py::_build_row``'s identical guard)."""
    if not user_context.user_id:
        raise ValueError(
            "ACL audit event has no user_id on the resolved UserContext; "
            "refusing to persist an unattributable audit row"
        )
    return user_context.user_id


async def record(
    db: AsyncSession,
    *,
    user_context: UserContext,
    op: str,
    principal_type: str,
    principal_id: str | None,
    resource_type: str,
    resource_id: str,
    perm_bits: int,
    acl_entry_ids: list[str],
    expired_ids: list[str] | None = None,
    visible_matched_count: int | None = None,
    expired_at_ms: int | None = None,
    tenant_id: str | None = None,
) -> InteractionRecord:
    """Stage a SUCCESS audit row on ``db`` — the CALLER's own already-open
    session — for a write the caller has determined succeeded.

    Does **not** commit. The row rides in the SAME transaction as the
    caller's domain write (2b-core-A) so that a subsequent rollback of
    that transaction takes this "success" row down with it too — the
    mirror image of :func:`record_denial`'s independent-transaction
    rule (§5.2): a rolled-back write with a SURVIVING audit row would be
    a false record.

    Every identity/traceability column is derived here, at the choke —
    see the module docstring's §3.2 section. No parameter on this
    signature can supply ``user_id``, ``granted_by``, ``session_id`` or
    ``trace_id``.
    """
    user_id = _require_user_id(user_context)
    trace_id = current_trace_id_hex()
    session_id = current_session_id()
    granted_by = user_id  # §3.2 — same source as user_id, never a parameter.

    answer_payload: dict[str, Any] = {
        "acl_entry_ids": list(acl_entry_ids),
        "expired_ids": list(expired_ids or []),
        "visible_matched_count": visible_matched_count,
        "perm_bits": perm_bits,
        "bits": _bit_breakdown(perm_bits),
        "granted_by": granted_by,
        "expired_at_ms": expired_at_ms,
    }

    fields: dict[str, Any] = {
        "project": _PROJECT,
        "source": _SOURCE,
        "question": _question(
            op=op,
            principal_type=principal_type,
            principal_id=principal_id,
            resource_type=resource_type,
            resource_id=resource_id,
            perm_bits=perm_bits,
            tenant_id=tenant_id,
        ),
        "answer": json.dumps(answer_payload, sort_keys=True, default=str),
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "timestamp": _now_iso(),
        "session_id": session_id,
        "model": None,
        "user_id": user_id,
        "status": "success",
        "failure_class": None,
        "error_detail": None,
        "duration_ms": None,
        "trace_id": trace_id,
        "event_class": EVENT_CLASS_ACL_AUTHZ,
    }
    row = InteractionRecord(**fields, content_hash=_content_hash(fields))
    db.add(row)
    return row


async def record_denial(
    *,
    user_context: UserContext,
    op: str,
    principal_type: str,
    principal_id: str | None,
    resource_type: str,
    resource_id: str,
    perm_bits: int,
    failure_class: str,
    predicate_or_attempted_row: Any,
    db_error_class: str | None = None,
    tenant_id: str | None = None,
) -> InteractionRecord:
    """Persist a DENIAL audit row in its OWN, independent transaction —
    *who tried to grant/revoke what, and was stopped* (§5.1).

    ``failure_class`` MUST be one of :data:`ACL_DENIAL_FAILURE_CLASSES`
    — refused loudly (``ValueError``), never silently coerced.

    Opens a fresh session via ``get_postgres_factory().get_session_
    factory()`` and commits it directly (§5 — see the module docstring's
    §5 section for why this must NOT share the caller's transaction, and
    why this function never touches the RLS GUC itself). Raises on any
    failure — including a ``WITH CHECK`` refusal from Postgres RLS when
    the ambient identity ContextVar is unbound (fail-closed, §5.3).
    """
    if failure_class not in ACL_DENIAL_FAILURE_CLASSES:
        raise ValueError(
            f"failure_class={failure_class!r} is not in the closed ACL "
            f"denial set {sorted(ACL_DENIAL_FAILURE_CLASSES)!r}"
        )
    user_id = _require_user_id(user_context)
    trace_id = current_trace_id_hex()
    session_id = current_session_id()

    error_detail: dict[str, Any] = {
        "predicate_or_attempted_row": predicate_or_attempted_row,
        "db_error_class": db_error_class,
    }

    fields: dict[str, Any] = {
        "project": _PROJECT,
        "source": _SOURCE,
        "question": _question(
            op=op,
            principal_type=principal_type,
            principal_id=principal_id,
            resource_type=resource_type,
            resource_id=resource_id,
            perm_bits=perm_bits,
            tenant_id=tenant_id,
        ),
        "answer": "{}",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "timestamp": _now_iso(),
        "session_id": session_id,
        "model": None,
        "user_id": user_id,
        "status": "failed",
        "failure_class": failure_class,
        "error_detail": json.dumps(error_detail, sort_keys=True, default=str),
        "duration_ms": None,
        "trace_id": trace_id,
        "event_class": EVENT_CLASS_ACL_AUTHZ,
    }
    row = InteractionRecord(**fields, content_hash=_content_hash(fields))

    pg = get_postgres_factory()
    session_factory = pg.get_session_factory()
    async with session_factory() as db:
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row
