"""SQLAlchemy ORM models for audittrace-server.

ADR-020: PostgreSQL (production) and SQLite-in-memory (tests) via the
same declarative models.

ADR-026 §15 (2026-04-11): identity is delegated
to Keycloak. There is NO local users table — see §15.1 for the
mental model. ``user_id`` columns on ``interactions``, ``sessions``,
and ``tool_calls`` are plain VARCHAR(36) Keycloak ``sub`` claims with
no foreign-key constraint to a local users table (because no such
table exists).

The Phase 0 ``users`` / ``user_roles`` / ``pat_tokens`` tables were
dropped via Alembic migration 004 in the same refactor.

All schema changes are managed via Alembic migrations.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# JSONB on Postgres (queryable via GIN + the `@>` containment operator),
# plain JSON (TEXT under the hood) on SQLite for the test suite. The
# audit-pivot query
# ``WHERE extraction_warnings @> '[{"code": "ocr_low_confidence"}]'``
# only needs JSONB at production runtime; SQLite-in-memory tests use
# pure-Python list comprehensions on the loaded value.
_PdfWarningsType = JSON().with_variant(JSONB(), "postgresql")


def _uuid_str() -> str:
    """UUID4 as a 36-character string. Cross-database default."""
    import uuid

    return str(uuid.uuid4())


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class SessionRecord(Base):
    """Conversational memory session — Layer 3 of the 4-layer architecture."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    project: Mapped[str] = mapped_column(String, index=True)
    date: Mapped[str] = mapped_column(String)
    summary: Mapped[str] = mapped_column(Text)
    key_points: Mapped[str] = mapped_column(Text)  # JSON-encoded list
    model: Mapped[str] = mapped_column(String)
    # Keycloak ``sub`` claim — no FK because Keycloak owns the identity
    # store. Nullable until Phase 5 flips it after backfill + isolation
    # tests.
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # ADR-030 Part 2. NULL = never summarised. A value older than the
    # session's max interaction timestamp means "stale — re-summarise".
    summarized_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )
    # #344 — OpenTelemetry trace_id (32-char hex) of the background
    # summariser run that produced this row, so the persisted summary
    # links back to its Tempo/Langfuse trace. The summariser sweep is a
    # background task whose model call would otherwise surface as an
    # unattributed orphan root span; capturing the trace_id closes the
    # DB→trace correlation. NULL for rows written before this migration
    # or when no span was active. Mirrors ``InteractionRecord.trace_id``.
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)


class InteractionRecord(Base):
    """Audit trail — every question/answer pair with token counts.

    ``status`` == 'failed' rows carry ``failure_class`` +
    ``error_detail`` and typically have ``answer=''`` and
    ``*_tokens=0``. See migration 007 for the motivation.
    """

    __tablename__ = "interactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project: Mapped[str] = mapped_column(String, index=True)
    source: Mapped[str] = mapped_column(String, default="unknown")
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    timestamp: Mapped[str] = mapped_column(String, index=True)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    # Keycloak ``sub`` claim — see SessionRecord.user_id docstring.
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    status: Mapped[str] = mapped_column(
        String(16), default="success", nullable=False, index=True
    )
    failure_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Migration 008 (2026-05-03): OpenTelemetry trace_id for single-query
    # Postgres↔Tempo correlation. Captured once per request from the active
    # span context; indexed because the lookup pattern is "find rows by
    # trace_id". 32-char lowercase hex string per OTel format.
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # Migration 012 (2026-05-10, ADR-048 PR-B1): closed-set
    # ``{"interaction", "security"}``. ``interaction`` (legacy implicit)
    # is the chat-completion / tool-call default; ``security`` is added
    # by PR-B4's verdict consumer to distinguish content-control verdict
    # rows from interaction rows so SOC tooling can alert on
    # ``rejected_malware`` outcomes without scanning every row. Pinned
    # by ``tests/test_memory_routes.py::TestEventClassValues``.
    event_class: Mapped[str | None] = mapped_column(
        String(16), nullable=True, index=True
    )
    # Migration 015 (2026-07-14, ADR-058 WS-A1): server-set
    # contemporaneity anchor. Unlike ``timestamp`` (a String the
    # application sets via ``datetime.now()``), ``created_at`` is
    # assigned by Postgres at INSERT via ``server_default=now()`` — a
    # writer-independent clock, so the record's time cannot be backdated
    # by the caller. Indexed for "rows created since T" audit queries.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )


class ToolCall(Base):
    """Audit row for a single memory tool invocation by an LLM.

    One row per tool call. Multiple rows per ``interactions`` row when
    the LLM calls more than one tool in a single chat completion. The
    combination of ``user_id`` + ``granted_scope`` answers "who was
    allowed to call what" under audit.

    ``user_id`` is a Keycloak ``sub`` claim (no FK to a local users
    table — see DESIGN §15). ``interaction_id`` keeps its FK to
    ``interactions`` because that table is owned by sovereign-memory-
    server.
    """

    __tablename__ = "tool_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    interaction_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("interactions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    agent_type: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    args: Mapped[str] = mapped_column(Text, nullable=False)  # JSON
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    granted_scope: Mapped[str] = mapped_column(String(255), nullable=False)


class MemoryItem(Base):
    """Manifest row for a memory-layer item managed via the operator
    backoffice (migration 009 — 2026-05-03).

    Stores authorship + sub-second-precision timestamps + soft-delete
    state for items whose actual content lives in S3 (episodic /
    procedural) or ChromaDB (semantic). This table is the source of
    truth for "what items exist + who put them there"; the storage
    backends hold the bytes.

    Timestamps are **Unix epoch milliseconds UTC** (per user
    directive — evening 2026-05-03). API surface returns BIGINT
    integers; clients render them as needed.

    No RLS on this table — the manifest is operator-global, not
    per-user content. Access is gated by the per-layer write scope
    (``memory:<layer>:write``) at the route layer.
    """

    __tablename__ = "memory_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    layer: Mapped[str] = mapped_column(String(16), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    modified_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    modified_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    deleted_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    deleted_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # ── Tier-B PDF manifest columns (migration 010, ADR-050 #22) ────────
    # Nullable so existing rows pre-dating migration 010 keep reading
    # cleanly. PDF-specific fields populated by /memory/index; non-PDF
    # rows (Markdown, plain text, etc.) leave them NULL.
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    signature_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ocr_coverage_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    attachment_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=0
    )
    form_field_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=0
    )
    # JSONB array of {"code": "<closed-set>", ...} entries. Closed-set
    # codes per ADR-050 §extraction_warnings; new codes need an ADR
    # amendment. Default `[]` so callers can iterate unconditionally.
    extraction_warnings: Mapped[list[dict[str, Any]] | None] = mapped_column(
        _PdfWarningsType, nullable=True, default=list
    )
    # SHA-256 of the raw bytes — same value tier-A propagated to every
    # chunk's metadata. Doc-level mirror saves a ChromaDB query for the
    # "manifest row ↔ specific bytes version" audit question.
    document_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)

    # ── Tier-C PDF document-metadata columns (migration 011, ADR-056 #10) ─
    # Populated from pymupdf's ``doc.metadata`` during /memory/index.
    # All nullable — non-PDF rows + PDFs that pre-date migration 011
    # leave them NULL.
    pdf_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pdf_author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pdf_creator: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pdf_creation_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # ── ADR-056 #14 (PDF/A) + #13 (LTV) ────────────────────────────────
    # pdfa_part is "1" / "2" / "3" / "4" and pdfa_conformance is
    # "A" / "B" / "U" (per ISO 19005-1..-4); both NULL means "not a
    # PDF/A document" or "XMP missing".
    pdfa_part: Mapped[str | None] = mapped_column(String(4), nullable=True)
    pdfa_conformance: Mapped[str | None] = mapped_column(String(4), nullable=True)
    # JSONB summary of the DSS dictionary on signed PDFs:
    # ``{"has_dss": bool, "ocsp_responses": int, "crls": int,
    #   "timestamps": int, "certs": int, "vri_keys": int}``. NULL on
    # unsigned / non-LTV-enabled documents.
    ltv_data: Mapped[dict[str, Any] | None] = mapped_column(
        _PdfWarningsType, nullable=True
    )

    # ── ADR-048 ingestion content-control (migration 012, PR-B1) ──────
    # Closed-set per ADR-048 §Failure modes:
    # ``{"pending_scan", "scanning", "scanned_clean", "rejected_malware",
    #    "scan_failed", "scan_unrecoverable"}``. Existing rows
    # pre-dating migration 012 read NULL (non-uploads, pre-ADR-048
    # uploads). PR-B3's rewrite of /memory/upload writes
    # ``pending_scan`` on insert; PR-B4's verdict consumer transitions
    # it to one of the terminal states. Pinned by
    # ``tests/test_memory_routes.py::TestScanStatusCodes``.
    scan_status: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )

    # ── ADR-048 PR-B3 outbox columns (migration 013) ──────────────────
    # ``published_at_ms`` NULL = the manifest row is the only record of
    # this scan-request — the AMQP basic_publish hasn't completed yet.
    # The publisher sets it on success; the janitor (60s grace) finds
    # NULL rows that crashed mid-flight and re-enqueues them.
    published_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # W3C-traceparent-derived trace_id from the originating
    # /memory/upload request. Carried into the AMQP message header so
    # content-control's worker (PR-A3) stitches the same trace across
    # the async boundary.
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint("layer", "key", name="uq_memory_items_layer_key"),
    )
