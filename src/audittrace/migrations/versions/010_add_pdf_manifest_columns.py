"""add PDF-specific manifest columns to memory_items

Revision ID: f6b9d3a2e8c1
Revises: e4a8c2d6f1e9
Create Date: 2026-05-08 21:00:00.000000

Tier-B (gap-inventory item #22 — document-level manifest). Adds
nullable columns to ``memory_items`` so a PDF's audit-relevant
properties can be answered from the manifest row alone, without
scanning every chunk in ChromaDB:

* ``page_count`` — total pages in the document.
* ``signature_status`` — same 7-class taxonomy that lives on each
  chunk in ChromaDB (tier-A item #12). Doc-level mirror of the
  per-chunk field; saves a ChromaDB query for the common audit
  question "is this signed?".
* ``ocr_coverage_pct`` — percentage of pages that needed OCR
  (tier-B item #1). 0.0 = fully native text; 100.0 = fully scanned.
* ``attachment_count`` — number of embedded attachments quarantined
  for this document (tier-B item #6). Default 0 so existing rows
  read as "no attachments seen" without backfill.
* ``form_field_count`` — number of AcroForm fields whose values
  were extracted (tier-B item #7). Default 0 same reasoning.
* ``extraction_warnings`` — JSONB array of structured warnings
  (closed-set ``code`` enum per ADR-050). The single audit pivot
  for "what happened to this document during ingestion."
* ``document_sha256`` — SHA-256 of the raw bytes. Already computed
  per-chunk in tier-A; mirroring at doc-level lets reviewers prove
  "the manifest row matches a specific bytes version" without a
  chunk fetch.

All columns are nullable. Existing rows pre-dating this migration
read the new fields as NULL / 0 / [] — backward-compatible with
the existing ``ManifestEntry`` consumers; the dataclass picks up
the new fields as Optional and old code paths are unchanged.

Per ADR-050, the deferred fields (``pdfa_conformance``,
``scan_verdict``) are intentionally NOT added — adding columns
without a populating code path is dead weight.

The ``extraction_warnings`` GIN index is added so the audit
queries documented in the gap inventory (e.g.
``WHERE extraction_warnings @> '[{"code": "ocr_low_confidence"}]'``)
are O(log N) rather than full-table-scan.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f6b9d3a2e8c1"
down_revision: str | Sequence[str] | None = "e4a8c2d6f1e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add tier-B PDF manifest columns + JSONB GIN index.

    The ``extraction_warnings`` column uses JSONB on Postgres (queryable
    via the GIN-backed ``@>`` containment operator that the audit pivot
    relies on) and plain JSON on other dialects (SQLite-in-memory for
    the unit-test alembic-replay path). Same shape as the ORM model in
    ``audittrace.db.models`` — keeps the test runtime in sync with the
    live runtime without duplicating the type definition.
    """
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    if is_postgres:
        warnings_type: Any = postgresql.JSONB(astext_type=sa.Text())
        warnings_default = sa.text("'[]'::jsonb")
    else:
        warnings_type = sa.JSON()
        warnings_default = None  # SQLite has no native JSON default literal.

    op.add_column(
        "memory_items",
        sa.Column("page_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "memory_items",
        sa.Column("signature_status", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "memory_items",
        sa.Column("ocr_coverage_pct", sa.Float(), nullable=True),
    )
    op.add_column(
        "memory_items",
        sa.Column(
            "attachment_count",
            sa.Integer(),
            nullable=True,
            server_default="0",
        ),
    )
    op.add_column(
        "memory_items",
        sa.Column(
            "form_field_count",
            sa.Integer(),
            nullable=True,
            server_default="0",
        ),
    )
    op.add_column(
        "memory_items",
        sa.Column(
            "extraction_warnings",
            warnings_type,
            nullable=True,
            server_default=warnings_default,
        ),
    )
    op.add_column(
        "memory_items",
        sa.Column("document_sha256", sa.CHAR(length=64), nullable=True),
    )
    # GIN index is Postgres-only — SQLite alembic-replay path skips it.
    if is_postgres:
        op.create_index(
            "ix_memory_items_extraction_warnings",
            "memory_items",
            ["extraction_warnings"],
            unique=False,
            postgresql_using="gin",
        )


def downgrade() -> None:
    """Drop tier-B columns + GIN index."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_index(
            "ix_memory_items_extraction_warnings",
            table_name="memory_items",
        )
    op.drop_column("memory_items", "document_sha256")
    op.drop_column("memory_items", "extraction_warnings")
    op.drop_column("memory_items", "form_field_count")
    op.drop_column("memory_items", "attachment_count")
    op.drop_column("memory_items", "ocr_coverage_pct")
    op.drop_column("memory_items", "signature_status")
    op.drop_column("memory_items", "page_count")
