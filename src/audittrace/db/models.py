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
    Boolean,
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
    # Migration 017 (2026-07-14, ADR-058 WS-A3): SHA-256 over the row's
    # immutable content (``integrity.content_hash``). Paired with the
    # append-only trigger (WS-A2), a post-hoc mutation is detectable by
    # recomputation. Nullable for rows predating the column.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


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
    # Migration 021 (ADR-063 Phase 2 Track B): broker provenance. NULL on
    # every Phase 1 own-tool row (unchanged, untouched by this migration);
    # ``"brokered"`` is the ONLY value the broker path ever writes — the
    # spec's "provenance clearly distinguishable from Phase-1 own-tool
    # rows" requirement, enforced as a real column rather than a naming
    # convention on ``tool_name`` (which is namespaced ``broker:<server>:
    # <tool>`` for readability, not as the provenance signal).
    provenance: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Which half of the broker's "record(request) → forward → record
    # (result)" sequence this row is. ``"request"`` rows are written
    # before the outbound call to the downstream server; ``"result"``
    # rows (success OR failure/timeout) are written after. Exactly two
    # rows per brokered call, sharing one ``interaction_id`` (one trace).
    # NULL on Phase 1 rows.
    phase: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # The operator-configured downstream server name (the registry key in
    # ``Settings.mcp_broker_servers``) — the "downstream identity" the
    # spec requires on every brokered audit row.
    downstream_server: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The tool name AS KNOWN TO the downstream server (before this
    # gateway's ``broker:<server>:`` namespacing).
    downstream_tool: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # SHA-256 hex digest of the canonicalised outbound arguments — set on
    # BOTH the request and result row (the result row still names what was
    # asked for), so a reviewer can confirm the two rows describe the same
    # call without re-parsing ``args``.
    args_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # SHA-256 hex digest of the canonicalised downstream response (or, on
    # failure, of the error payload) — set on the result row only; NULL on
    # the request row (the result is not known yet when it is written).
    result_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)


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

    # ── ADR-062 Phase B (WU-B4, migration 018) — per-user tiering ──────
    # Closed-set: ``"corpus"`` (Layer 5, shared-read) | ``"private"``
    # (per-user, owner-only). ``server_default="corpus"`` means every row
    # written BEFORE migration 018 reads ``tier="corpus"`` without a
    # backfill script — D2 (2026-08-04): "existing content = corpus, new
    # writes = private-default". The per-layer CRUD routes
    # (create_episodic/create_procedural/create_semantic) pass an
    # explicit ``tier`` on every new manifest row (WU-B5); this column's
    # server-side default is a safety net for rows inserted through a
    # path this PR does not touch (e.g. the PDF-indexing manifest writer
    # in ``memory_pdf``, which sources bulk-reindexed shared content and
    # is therefore correctly corpus-tier by default too).
    tier: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="corpus"
    )

    # ── SPEC #387 Phase 1 (WU-1, migration 019) — auto-index outbox ────
    # ``indexed_at_ms`` NULL = the manifest row's promoted, scanned-clean
    # bytes have NOT yet been embedded + upserted into ChromaDB — the
    # place→index leg of the pipeline is still pending. Mirrors
    # ``published_at_ms`` 1:1 (same Hohpe Transactional Outbox shape, one
    # hop later): ``ScanVerdictConsumer`` stamps it NULL and enqueues an
    # ``IndexRequestEnvelope`` on every ``scanned_clean`` verdict;
    # ``IndexWorker`` sets it on success (idempotent ``WHERE
    # indexed_at_ms IS NULL``); ``IndexJanitor`` re-drives any row still
    # NULL past the grace window — closes GAP-1 (no auto-index trigger)
    # with the same durability guarantee already proven for
    # place→publish. Nullable so every pre-migration-019 row (and every
    # non-PDF / non-scanned row, which never populates ``scan_status``
    # either) reads NULL forever, which is correct — nothing outside the
    # scan pipeline claims to have been auto-indexed.
    indexed_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # ── SPEC #450 (WU-1, migration 020) — index dead-letter state ──────
    # Adds the THIRD state the ``indexed_at_ms`` outbox above never had:
    # "will never succeed" — a corrupt/unindexable PDF must stop being
    # re-enqueued by ``IndexJanitor`` forever. ``index_attempts`` counts
    # every failed ``IndexWorker`` pass (0 = never attempted or never
    # failed); ``index_failed_at_ms`` NULL = still retryable, a
    # timestamp = terminally dead-lettered; ``index_failure_code`` names
    # WHY — either a permanent structural code from
    # ``PERMANENT_INDEX_FAILURE_CODES`` (routes/memory_pdf/classification.py)
    # or ``"max_attempts_exceeded"`` once ``index_attempts`` reaches
    # ``Settings.index_max_attempts``. All three nullable/defaulted so
    # every pre-migration-020 row reads NULL/0 — additive, backward
    # compatible, no backfill. Mirrors the two terminal patterns already
    # proven elsewhere in this codebase: ``async_persist.py``
    # max-deliveries→DLQ and ``scan_verdict_consumer.py`` DLX +
    # ``row_missing`` no-requeue.
    index_attempts: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    index_failed_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    index_failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint("layer", "key", name="uq_memory_items_layer_key"),
    )


class SessionMemoryItem(Base):
    """The ``session`` memory layer — WU-1 of the Sovereign-Attach EPIC
    (migration 022): a per-user, EPHEMERAL ingest tier, distinct from the
    S3-backed ``episodic``/``procedural`` layers above.

    Unlike ``MemoryItem`` (an operator-global manifest describing bytes
    that live in S3/ChromaDB), this table IS the content — there is no
    corpus/shared tier for this layer (WU-1's ephemeral-default decision
    deliberately excludes promotion; that is WU-4's job) and no listing/
    recall surface yet (WU-5), so a lightweight, GC-friendly (WU-6)
    Postgres row is the natural fit rather than an S3 object plus a
    manifest row.

    RLS (migration 022, mirrors migration 005's shape exactly): a
    Postgres policy compares ``user_id`` against
    ``current_setting('app.current_user_id', true)``. On SQLite (the
    unit-test suite) RLS is a no-op — ``PostgresSessionMemoryService``
    additionally filters every query by ``user_id`` explicitly at the
    SERVICE layer so a dropped filter is still caught by SQLite unit
    tests instead of only by a live-Postgres integration run
    (feedback_unit_tests_miss_rls).
    """

    __tablename__ = "session_memory_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    # Keycloak ``sub`` claim — no FK, same rationale as every other
    # user_id column in this module (§15 — identity is Keycloak-owned).
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    # Unix epoch milliseconds UTC — same convention as MemoryItem's
    # created_at_ms (sub-second ordering, matches Date.now()/time.time()*1000).
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # W3C-traceparent-derived trace_id from the originating request, when
    # a span is active — same convention as MemoryItem.trace_id.
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


# JSONB on Postgres, plain JSON (TEXT under the hood) on SQLite — same
# portability rationale as ``_PdfWarningsType`` above, reused generically
# for the console-conversations ``metadata`` columns below (WU-1,
# MongoDB-elimination EPIC).
_ConsoleMetadataType = JSON().with_variant(JSONB(), "postgresql")


class ConsoleConversation(Base):
    """The ``console_conversations`` store — WU-1 of the MongoDB-
    elimination EPIC (migration 023): AuditTrace's first-party,
    RLS-isolated replacement for LibreChat's Mongo ``Conversation``
    collection (``packages/data-schemas/src/schema/convo.ts``).

    ``conversation_id`` is a CLIENT-SUPPLIED STRING (LibreChat mints its
    own, not an ObjectId) — the internal PK ``id`` is a separate
    server-generated UUID so ``conversation_id`` collisions across users
    (two different subs both minting the same client id) can coexist,
    disambiguated only by ``(user_sub, conversation_id)`` — see the
    unique constraint below.

    ``user_sub`` is the Keycloak ``sub`` claim, stamped from the TOKEN at
    the route layer — NEVER from the request body
    (``feedback_never_trust_caller_metadata_for_security_fields``). RLS
    (migration 023, mirrors migration 022's shape exactly) compares
    ``user_sub`` against ``current_setting('app.current_user_id', true)``.
    On SQLite (unit tests) RLS is a no-op —
    ``PostgresConsoleConversationsService`` additionally filters every
    query by ``user_sub`` explicitly at the SERVICE layer, so a dropped
    filter is caught by the SQLite unit suite too
    (feedback_unit_tests_miss_rls).

    Distinct from the existing ``conversational`` layer
    (``PostgresConversationalService`` / ``SessionRecord``): that table
    stores ONE per-session SUMMARY, not a message tree — see the WU-1
    spec's "EPIC CORRECTION" for why a new store was needed rather than
    reusing it.
    """

    __tablename__ = "console_conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    conversation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # Keycloak `sub` claim — no FK, same rationale as every other user_id/
    # user_sub column in this module (§15 — identity is Keycloak-owned).
    user_sub: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False, default="New Chat")
    endpoint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_temporary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Reserved for the later chat-projects domain (DESIGN §WU breakdown,
    # not built here) — carried now so a future migration doesn't need to
    # widen this table's shape again.
    chat_project_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Soft delete — same convention as MemoryItem.deleted_at_ms.
    deleted_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", _ConsoleMetadataType, nullable=False, default=dict
    )
    # W3C-traceparent-derived trace_id from the originating request
    # (EU AI Act Art 12 traceability) — same convention as
    # SessionMemoryItem.trace_id.
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "user_sub", "conversation_id", name="uq_console_conversations_user_convo"
        ),
    )


class ConsoleMessage(Base):
    """The ``console_messages`` store — WU-1 of the MongoDB-elimination
    EPIC (migration 023): the message-tree rows a conversation's
    ``parent_message_id`` chain reconstructs (LibreChat's Mongo
    ``Message`` collection, ``message.ts``).

    ``conversation_id`` is a plain string column (the same client-
    supplied key as ``ConsoleConversation.conversation_id``), not a hard
    SQLAlchemy ``ForeignKey`` — the practical uniqueness constraint that
    matters for RLS isolation is ``(user_sub, message_id)`` below, and a
    composite FK against ``(user_sub, conversation_id)`` would add
    migration complexity for no isolation benefit (the service layer
    always filters by ``user_sub`` AND ``conversation_id`` together, so
    an orphaned ``conversation_id`` is a not-found, not a leak).

    Same RLS + explicit-filter discipline as :class:`ConsoleConversation`
    above.
    """

    __tablename__ = "console_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    conversation_id: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True
    )
    user_sub: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # The tree link — NULL for the first message in a conversation.
    parent_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sender: Mapped[str] = mapped_column(String(64), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    is_created_by_user: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    endpoint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", _ConsoleMetadataType, nullable=False, default=dict
    )
    # W3C-traceparent-derived trace_id (EU AI Act Art 12 traceability).
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "user_sub", "message_id", name="uq_console_messages_user_message"
        ),
    )


class ConsolePreset(Base):
    """The ``console_presets`` store — WU-presets of the MongoDB-
    elimination EPIC (migration 024): AuditTrace's first-party,
    RLS-isolated replacement for LibreChat's Mongo ``Preset`` collection
    (``packages/data-schemas/src/schema/preset.ts``) — the user's saved
    model/endpoint presets.

    ``preset_id`` is a CLIENT-SUPPLIED STRING (LibreChat mints its own),
    same pattern as :attr:`ConsoleConversation.conversation_id` — the
    internal PK ``id`` is a separate server-generated UUID so
    ``preset_id`` collisions across users can coexist, disambiguated
    only by ``(user_sub, preset_id)`` (the unique constraint below).

    The preset's config (``endpoint``/``model``/``temperature``/... —
    the large, loosely-typed field set on the fork's ``IPreset``
    interface) is carried as a single ``data`` jsonb blob rather than
    one column per field: the fork's own schema treats these as an open
    index-signature bag (``...conversationPreset`` spread), so promoting
    each field to a typed column here would need re-widening on every
    upstream fork change for no isolation or query benefit — only
    ``title``/``preset_id`` are promoted to first-class columns because
    the console's list view sorts/displays by them directly.

    ``user_sub`` is the Keycloak ``sub`` claim, stamped from the TOKEN at
    the route layer — NEVER from the request body
    (``feedback_never_trust_caller_metadata_for_security_fields``). RLS
    (migration 024, mirrors migrations 022/023's shape exactly) compares
    ``user_sub`` against ``current_setting('app.current_user_id', true)``.
    On SQLite (unit tests) RLS is a no-op —
    ``PostgresConsolePresetsService`` additionally filters every query by
    ``user_sub`` explicitly at the SERVICE layer, so a dropped filter is
    caught by the SQLite unit suite too (feedback_unit_tests_miss_rls).
    """

    __tablename__ = "console_presets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    preset_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # Keycloak `sub` claim — no FK, same rationale as every other user_id/
    # user_sub column in this module (§15 — identity is Keycloak-owned).
    user_sub: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False, default="New Chat")
    # The preset config blob (endpoint/model/temperature/... — see the
    # class docstring for why this is one jsonb column, not many).
    data: Mapped[dict[str, Any]] = mapped_column(
        _ConsoleMetadataType, nullable=False, default=dict
    )
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Soft delete — same convention as MemoryItem.deleted_at_ms /
    # ConsoleConversation.deleted_at_ms.
    deleted_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", _ConsoleMetadataType, nullable=False, default=dict
    )
    # W3C-traceparent-derived trace_id from the originating request
    # (EU AI Act Art 12 traceability) — same convention as
    # ConsoleConversation.trace_id.
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "user_sub", "preset_id", name="uq_console_presets_user_preset"
        ),
    )


class ConsolePromptGroup(Base):
    """The ``console_prompt_groups`` store — WU-prompts of the MongoDB-
    elimination EPIC (migration 025): AuditTrace's first-party,
    RLS-isolated replacement for LibreChat's Mongo ``PromptGroup``
    collection (``packages/data-schemas/src/schema/promptGroup.ts``).

    Model shape RESOLVED per the ratified spec's design note: LibreChat
    ships TWO Mongo collections (``PromptGroup`` + ``Prompt``, one row
    per version); this is modelled here the same way console-
    conversations models convo/message — a group row
    (:class:`ConsolePromptGroup`) plus one row per prompt VERSION
    (:class:`ConsolePromptVersion`), linked by the client-supplied
    string ``group_id`` (mirrors :attr:`ConsoleMessage.conversation_id`
    — no hard SQLAlchemy ``ForeignKey``, since the isolation invariant
    that matters is ``(user_sub, group_id)``/``(user_sub, prompt_id)``,
    not a database-level cascade). This preserves LibreChat's
    versioning + ``productionId`` semantics: each edit is a NEW,
    immutable version row, and ``production_prompt_id`` below points at
    whichever version is "live" — exactly like ``PromptGroup
    .productionId`` referencing a specific ``Prompt`` document.

    ``group_id`` is CLIENT-SUPPLIED (LibreChat mints its own, not an
    ObjectId) — the internal PK ``id`` is a separate server-generated
    UUID so ``group_id`` collisions across users can coexist,
    disambiguated only by ``(user_sub, group_id)`` (the unique
    constraint below) — same pattern as
    :attr:`ConsoleConversation.conversation_id`.

    ``user_sub`` is the Keycloak ``sub`` claim, stamped from the TOKEN
    at the route layer — NEVER from the request body
    (``feedback_never_trust_caller_metadata_for_security_fields``). RLS
    (migration 025, mirrors migration 023's shape exactly) compares
    ``user_sub`` against ``current_setting('app.current_user_id',
    true)``. On SQLite (unit tests) RLS is a no-op —
    ``PostgresConsolePromptsService`` additionally filters every query
    by ``user_sub`` explicitly at the SERVICE layer, so a dropped
    filter is caught by the SQLite unit suite too
    (feedback_unit_tests_miss_rls).

    ``author``/``authorName`` on the fork's schema are not modelled as
    separate columns here — in this single-tenant-per-user store the
    owning ``user_sub`` already IS the sole author (no cross-user
    sharing of a prompt group exists), so promoting a redundant author
    column would carry no isolation or query benefit (same rationale
    as :class:`ConsolePreset` declining to widen its ``data`` blob into
    typed columns).
    """

    __tablename__ = "console_prompt_groups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    group_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # Keycloak `sub` claim — no FK, same rationale as every other user_id/
    # user_sub column in this module (§15 — identity is Keycloak-owned).
    user_sub: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    oneliner: Mapped[str] = mapped_column(Text, nullable=False, default="")
    command: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Client-supplied `prompt_id` of the version currently marked
    # "production" for this group (LibreChat's `PromptGroup.productionId`).
    # Plain string, not a FK — same rationale as `group_id` above; the
    # service layer verifies the target version exists AND belongs to
    # this (user_sub, group_id) before ever writing this column.
    production_prompt_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Soft delete — same convention as ConsoleConversation.deleted_at_ms.
    deleted_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", _ConsoleMetadataType, nullable=False, default=dict
    )
    # W3C-traceparent-derived trace_id from the originating request
    # (EU AI Act Art 12 traceability) — same convention as
    # ConsoleConversation.trace_id.
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "user_sub", "group_id", name="uq_console_prompt_groups_user_group"
        ),
    )


class ConsolePromptVersion(Base):
    """The ``console_prompt_versions`` store — WU-prompts of the
    MongoDB-elimination EPIC (migration 025): the per-version rows a
    prompt group's history is built from (LibreChat's Mongo ``Prompt``
    collection, ``prompt.ts`` — one document per version, ``groupId``
    FK + ``type`` ``text``/``chat``).

    ``group_id`` is a plain string column (the same client-supplied key
    as :attr:`ConsolePromptGroup.group_id`), not a hard SQLAlchemy
    ``ForeignKey`` — same rationale as :attr:`ConsoleMessage
    .conversation_id`: the practical uniqueness constraint that matters
    for RLS isolation is ``(user_sub, prompt_id)`` below, and the
    service layer always filters by ``user_sub`` AND ``group_id``
    together when listing a group's versions, so an orphaned
    ``group_id`` is a not-found, not a leak.

    Immutable-by-convention: each edit the caller makes is a NEW
    version row (the ``upsert_version`` service method updates an
    EXISTING row only when the caller re-submits the SAME ``prompt_id``
    — same idempotent-upsert-by-client-id shape as every other Mongo-
    repl WU, not a "create a new version on every PATCH" free-for-all).

    Same RLS + explicit-filter discipline as :class:`ConsolePromptGroup`
    above.
    """

    __tablename__ = "console_prompt_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    prompt_id: Mapped[str] = mapped_column(String(255), nullable=False)
    group_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    user_sub: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # LibreChat's Prompt.type enum — enforced at the Pydantic request-
    # model layer (Literal["text", "chat"]), stored here as a plain
    # string column (same portability rationale as every enum-shaped
    # column in this module — SQLite has no native CHECK-enum type
    # worth fighting).
    type: Mapped[str] = mapped_column(String(16), nullable=False, default="text")
    # 1-based, monotonically increasing per (user_sub, group_id) —
    # assigned by the service at creation (max existing + 1), never
    # caller-supplied (a hostile caller cannot renumber another
    # version by racing this field in the request body — no such field
    # exists on the upsert-version request model).
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", _ConsoleMetadataType, nullable=False, default=dict
    )
    # W3C-traceparent-derived trace_id from the originating request
    # (EU AI Act Art 12 traceability) — same convention as
    # ConsolePromptGroup.trace_id.
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "user_sub", "prompt_id", name="uq_console_prompt_versions_user_prompt"
        ),
    )


class ConsoleChatProject(Base):
    """The ``console_chat_projects`` store — the Chat-Projects domain of
    the MongoDB-elimination EPIC (migration 026): AuditTrace's
    first-party, RLS-isolated replacement for LibreChat's Mongo
    ``ChatProject`` collection
    (``packages/data-schemas/src/schema/chatProject.ts``) — a
    first-class, user-created grouping of conversations.

    ``chat_project_id`` is a CLIENT-SUPPLIED STRING (LibreChat mints its
    own), same pattern as :attr:`ConsolePreset.preset_id` — the internal
    PK ``id`` is a separate server-generated UUID so ``chat_project_id``
    collisions across users can coexist, disambiguated only by
    ``(user_sub, chat_project_id)`` (the unique constraint below).

    **Design note — reconciling the two pre-existing "project" seams**
    (per the ratified spec's design note): ``src/audittrace/models.py``
    already carries an unrelated ``project`` field (a memory-context/
    audit-namespace string, e.g. default ``"self-audit"``) — that field
    is NOT touched by this WU and remains exactly what it was; it is a
    namespacing label, not a first-class store. Separately,
    :class:`ConsoleConversation` (migration 023, WU-1) already reserved
    a NULLABLE ``chat_project_id`` column on the conversations table in
    anticipation of this store landing later — THIS table is what that
    reserved column will eventually reference (by client-supplied string
    key, same non-FK convention as every other cross-table reference in
    this module — e.g. :attr:`ConsolePromptVersion.group_id` — since the
    isolation invariant that matters is ``(user_sub, chat_project_id)``,
    not a database-level cascade). Wiring
    ``ConsoleConversation.chat_project_id`` to validate against this
    table is explicitly OUT OF SCOPE for this WU (a later WU, per the
    spec's "Out of scope" section).

    ``user_sub`` is the Keycloak ``sub`` claim, stamped from the TOKEN at
    the route layer — NEVER from the request body
    (``feedback_never_trust_caller_metadata_for_security_fields``). RLS
    (migration 026, mirrors migrations 022/023/024/025's shape exactly)
    compares ``user_sub`` against ``current_setting('app.current_user_id',
    true)``. On SQLite (unit tests) RLS is a no-op —
    ``PostgresConsoleChatProjectsService`` additionally filters every
    query by ``user_sub`` explicitly at the SERVICE layer, so a dropped
    filter is caught by the SQLite unit suite too
    (feedback_unit_tests_miss_rls).
    """

    __tablename__ = "console_chat_projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    chat_project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # Keycloak `sub` claim — no FK, same rationale as every other user_id/
    # user_sub column in this module (§15 — identity is Keycloak-owned).
    user_sub: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Soft delete — same convention as ConsolePreset.deleted_at_ms.
    deleted_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", _ConsoleMetadataType, nullable=False, default=dict
    )
    # W3C-traceparent-derived trace_id from the originating request
    # (EU AI Act Art 12 traceability) — same convention as
    # ConsolePreset.trace_id.
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "user_sub",
            "chat_project_id",
            name="uq_console_chat_projects_user_project",
        ),
    )


class ConsoleFile(Base):
    """The ``console_files`` store — the Files-metadata domain of the
    MongoDB-elimination EPIC (migration 027): AuditTrace's first-party,
    RLS-isolated replacement for LibreChat's Mongo ``File`` collection
    (``packages/data-schemas/src/schema/file.ts``) — the file METADATA
    record only.

    **Scope boundary (the ratified spec's design note).** This table
    owns the metadata record; the file BYTES stay in object storage
    (S3/MinIO, ``feedback_storage_always_s3``) — no code path in this
    module reads or writes bytes. :attr:`object_key` is the pointer
    into that store, not the payload.

    **Naming resolution — ``object_key`` vs the fork's ``source``/
    ``filepath`` pair.** LibreChat's Mongo schema splits the storage
    reference across ``source`` (backend enum, e.g. ``"local"``/
    ``"s3"``) and ``filepath``/``storageKey`` (the path within that
    backend). The ratified spec names this column ambiguously
    (``object_key``/``source``) and leaves the exact shape to the
    builder. RESOLVED here as a single ``object_key`` column — the one
    fact this store needs to reference the bytes-at-rest location
    (which object-storage backend it lives in is an operator-wide
    config concern, not a per-file fact, per the portability
    invariant) — nullable, since a file record can exist
    (upload-in-flight, or promoted-but-not-yet-relocated) before its
    final object key is known.

    ``file_id`` is a CLIENT-SUPPLIED STRING (LibreChat mints its own),
    same pattern as :attr:`ConsoleChatProject.chat_project_id` — the
    internal PK ``id`` is a separate server-generated UUID so
    ``file_id`` collisions across users can coexist, disambiguated only
    by ``(user_sub, file_id)`` (the unique constraint below).

    ``usage`` is modelled as a jsonb column (not the fork's numeric
    counter) per the ratified spec's explicit text ("`usage` jsonb") —
    a structured bag for forward compatibility (e.g. per-purpose usage
    counts), distinct from :attr:`metadata` (the generic free-form
    bag every console-* table carries).

    ``user_sub`` is the Keycloak ``sub`` claim, stamped from the TOKEN
    at the route layer — NEVER from the request body
    (``feedback_never_trust_caller_metadata_for_security_fields``). RLS
    (migration 027, mirrors migrations 022-026's shape exactly)
    compares ``user_sub`` against ``current_setting('app.current_user_id',
    true)``. On SQLite (unit tests) RLS is a no-op —
    ``PostgresConsoleFilesService`` additionally filters every query by
    ``user_sub`` explicitly at the SERVICE layer, so a dropped filter is
    caught by the SQLite unit suite too (feedback_unit_tests_miss_rls).
    """

    __tablename__ = "console_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    file_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # Keycloak `sub` claim — no FK, same rationale as every other user_id/
    # user_sub column in this module (§15 — identity is Keycloak-owned).
    user_sub: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    type: Mapped[str] = mapped_column(String(255), nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # The object-storage pointer — see the class docstring's naming
    # resolution note. Nullable: a record can exist before its final
    # key is known.
    object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context: Mapped[str | None] = mapped_column(String(128), nullable=True)
    usage_json: Mapped[dict[str, Any]] = mapped_column(
        "usage", _ConsoleMetadataType, nullable=False, default=dict
    )
    embedded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    temp_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Soft delete — same convention as ConsoleChatProject.deleted_at_ms.
    deleted_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", _ConsoleMetadataType, nullable=False, default=dict
    )
    # W3C-traceparent-derived trace_id from the originating request
    # (EU AI Act Art 12 traceability) — same convention as
    # ConsoleChatProject.trace_id.
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "user_sub",
            "file_id",
            name="uq_console_files_user_file",
        ),
    )


class ConsoleAgent(Base):
    """The ``console_agents`` store — the Agents domain of the
    MongoDB-elimination EPIC (migration 028): AuditTrace's first-party,
    RLS-isolated replacement for LibreChat's Mongo ``Agent`` collection
    (``packages/data-schemas/src/schema/agent.ts``).

    **Own-agents-only v1 (the ratified spec's scope boundary).** LibreChat
    supports agent SHARING/marketplace (an ``author`` owner field plus
    global/shared agents visible to other users). That is explicitly OUT
    OF SCOPE here — every row is owned by exactly one ``user_sub`` (the
    unique constraint below), and there is no "shared"/"global" row
    concept in this table. Cross-user sharing is a later WU, per the
    spec's "Out of scope" section.

    ``agent_id`` is a CLIENT-SUPPLIED STRING (LibreChat mints its own),
    same pattern as :attr:`ConsoleFile.file_id` — the internal PK ``id``
    is a separate server-generated UUID so ``agent_id`` collisions across
    users can coexist, disambiguated only by ``(user_sub, agent_id)``
    (the unique constraint below).

    ``tools``/``model_parameters``/``artifacts`` are stored as opaque
    jsonb blobs per the ratified spec's explicit text — this store
    persists the agent record; it never executes or validates the tools
    a row references (that stays entirely client-side / a later WU).

    ``project_ids`` is a jsonb array of :attr:`ConsoleChatProject.
    chat_project_id` STRING keys — no FK, same non-FK cross-table-
    reference convention as every other console-* domain in this module
    (e.g. :attr:`ConsoleConversation.chat_project_id`), since the
    isolation invariant that matters is ``(user_sub, agent_id)``, not a
    database-level cascade. Validating that a referenced chat-project
    exists/is owned by the same user is explicitly OUT OF SCOPE for this
    WU.

    ``user_sub`` is the Keycloak ``sub`` claim, stamped from the TOKEN
    at the route layer — NEVER from the request body
    (``feedback_never_trust_caller_metadata_for_security_fields``). RLS
    (migration 028, mirrors migrations 022-027's shape exactly) compares
    ``user_sub`` against ``current_setting('app.current_user_id',
    true)``. On SQLite (unit tests) RLS is a no-op —
    ``PostgresConsoleAgentsService`` additionally filters every query by
    ``user_sub`` explicitly at the SERVICE layer, so a dropped filter is
    caught by the SQLite unit suite too (feedback_unit_tests_miss_rls).
    """

    __tablename__ = "console_agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # Keycloak `sub` claim — no FK, same rationale as every other user_id/
    # user_sub column in this module (§15 — identity is Keycloak-owned).
    user_sub: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model_parameters_json: Mapped[dict[str, Any]] = mapped_column(
        "model_parameters", _ConsoleMetadataType, nullable=False, default=dict
    )
    tools_json: Mapped[list[Any]] = mapped_column(
        "tools", _ConsoleMetadataType, nullable=False, default=list
    )
    artifacts_json: Mapped[dict[str, Any]] = mapped_column(
        "artifacts", _ConsoleMetadataType, nullable=False, default=dict
    )
    end_after_tools: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    project_ids_json: Mapped[list[str]] = mapped_column(
        "project_ids", _ConsoleMetadataType, nullable=False, default=list
    )
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Soft delete — same convention as ConsoleFile.deleted_at_ms.
    deleted_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", _ConsoleMetadataType, nullable=False, default=dict
    )
    # W3C-traceparent-derived trace_id from the originating request
    # (EU AI Act Art 12 traceability) — same convention as
    # ConsoleFile.trace_id.
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "user_sub",
            "agent_id",
            name="uq_console_agents_user_agent",
        ),
    )
