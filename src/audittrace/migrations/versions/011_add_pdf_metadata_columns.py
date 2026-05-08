"""add PDF document-metadata columns to memory_items

Revision ID: a7c3f5e1b482
Revises: f6b9d3a2e8c1
Create Date: 2026-05-09 18:00:00.000000

Tier-C item #10 (ADR-056). Surfaces ``doc.metadata`` from pymupdf
into the manifest so auditors can answer "who wrote this, with what
tool, when" from a manifest row alone — no PDF re-fetch required.

Four nullable columns:

* ``pdf_title`` — ``doc.metadata["title"]``. Human-readable identifier;
  used by audit dashboards as the document label.
* ``pdf_author`` — ``doc.metadata["author"]``. Provenance — pairs with
  ``signature_status`` (who *says* they wrote it vs who actually
  signed it).
* ``pdf_creator`` — ``doc.metadata["creator"]``. The application that
  produced the PDF (e.g. ``"Microsoft Word"``, ``"SwissSign Web"``).
  Soft fraud signal — anomaly across a corpus, not single-document
  authenticity.
* ``pdf_creation_date`` — ``doc.metadata["creationDate"]`` parsed
  from PDF date string (PDF 1.7 §7.9.4 ``D:YYYYMMDDHHMMSS+HHMM``)
  to ``TIMESTAMPTZ`` on Postgres / ``DATETIME`` on SQLite.

Other pymupdf metadata keys (``subject``, ``keywords``, ``producer``,
``modDate``, ``format``) are intentionally NOT added — see ADR-056 §1
for the rationale. Future ADR can extend if a customer asks; the
forward-compatible nullable shape supports additive growth.

All columns nullable. Existing tier-A/tier-B rows read NULL for all
four — backwards-compatible with the existing ``ManifestEntry``
dataclass + serialiser.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c3f5e1b482"
down_revision: str | Sequence[str] | None = "f6b9d3a2e8c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the four metadata columns + PDF/A conformance + LTV.

    Tier-C item #10 — `pdf_title`, `pdf_author`, `pdf_creator`,
    `pdf_creation_date` from `doc.metadata`.

    Tier-C item #14 (ADR-056) — `pdfa_part` + `pdfa_conformance` parsed
    from the XMP `pdfaid:` namespace. Two short string columns rather
    than one combined value so audit queries can filter on either
    independently (e.g. ``WHERE pdfa_part = '3'`` for ZUGFeRD).

    Tier-C item #13 (ADR-056) — `ltv_data` is a JSONB summary of the
    DSS dictionary captured by pyhanko on signed documents. Flat object
    with counts (``ocsp_responses``, ``crls``, ``timestamps``, ``certs``)
    plus a ``has_dss`` boolean. Full ASN.1 stays in the source PDF; this
    is the audit-pivot index, not the certificate store.
    """
    op.add_column(
        "memory_items",
        sa.Column("pdf_title", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "memory_items",
        sa.Column("pdf_author", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "memory_items",
        sa.Column("pdf_creator", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "memory_items",
        sa.Column(
            "pdf_creation_date",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "memory_items",
        sa.Column("pdfa_part", sa.String(length=4), nullable=True),
    )
    op.add_column(
        "memory_items",
        sa.Column("pdfa_conformance", sa.String(length=4), nullable=True),
    )
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    from typing import Any as _Any

    from sqlalchemy.dialects import postgresql as _pg

    if is_postgres:
        ltv_type: _Any = _pg.JSONB(astext_type=sa.Text())
    else:
        ltv_type = sa.JSON()
    op.add_column(
        "memory_items",
        sa.Column("ltv_data", ltv_type, nullable=True),
    )


def downgrade() -> None:
    """Drop migration 011 columns."""
    op.drop_column("memory_items", "ltv_data")
    op.drop_column("memory_items", "pdfa_conformance")
    op.drop_column("memory_items", "pdfa_part")
    op.drop_column("memory_items", "pdf_creation_date")
    op.drop_column("memory_items", "pdf_creator")
    op.drop_column("memory_items", "pdf_author")
    op.drop_column("memory_items", "pdf_title")
