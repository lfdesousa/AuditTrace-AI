"""The closed ACL write-refusal error hierarchy — Sovereign Authorization
Layer EPIC, **ACL 2b-core-A1**
(``2026-09-26-SPEC-acl-2b-core-A-write-path.md`` §5.6).

Kept in its own module (rather than in ``__init__.py``) so the package's
abstract-contract file (``__init__.py``) stays an additions-only diff of
exactly the five new abstract write methods + the ``AclGrantOp`` op
dataclass their signatures need — see that file's own note on the
``git diff --numstat`` gate ADDENDUM V-4/W-4 add to spec §11.

Every error here corresponds to exactly one ``failure_class`` in
``services/console_acl/_audit.py``'s closed
:data:`~audittrace.services.console_acl._audit.ACL_DENIAL_FAILURE_CLASSES`
vocabulary (spec §5.6's table) — the mapping lives in
``_postgres_write.py``/``_mock_write.py``'s ``_write_denial`` helper, not
here; this module is pure error-shape, no classification logic, so it can
be imported by the abstract layer, the two write mixins, AND the test
suite without pulling in SQLAlchemy.
"""

from __future__ import annotations


class AclWriteRefused(Exception):  # noqa: N818 - spec-mandated name, ACL 2b-core-A §5.6
    """A sovereign ACL write was refused. Base of the closed hierarchy —
    ``acl_denied_policy`` (the generic DB-level refusal: RLS, an
    unnamed CHECK, 033's uniqueness index, 032's soft-deleted-resource
    ``WITH CHECK``) raises this class directly; the three more specific
    denials below subclass it so a caller that only cares "was my write
    refused" can catch this one type."""

    def __init__(
        self,
        message: str,
        *,
        failure_class: str,
        db_error_class: str | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.db_error_class = db_error_class


class AclPastExpiryError(AclWriteRefused):
    """``grant_permission`` refused a caller-supplied ``expired_at_ms``
    that is not strictly in the future of the server-stamped
    ``granted_at_ms`` (spec R-2/5.2) — refused BEFORE any I/O,
    ``failure_class = acl_denied_past_expiry``."""


class AclPrincipalTypeRefused(AclWriteRefused):
    """A ``principal_type`` outside the allowed set was refused at the
    database layer by ``ck_console_acl_entries_principal_type``
    (migration 031) — spec R-8/5.3, ``failure_class =
    acl_denied_principal_type``. Never raised from an application-level
    pre-check (5.3 forbids one — the DB CHECK is the control)."""


class AclBulkRolledBackError(AclWriteRefused):
    """``bulk_write_acl_entries`` refused one op and rolled back every
    op in the batch, including any already-staged success rows (spec
    O-4/5.4) — ``failure_class = acl_denied_bulk_rollback``. The
    refused op's index is carried in the denial row's
    ``error_detail.predicate_or_attempted_row.op_index``, never on this
    exception itself (the audit row is the durable record)."""
