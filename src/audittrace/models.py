from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def _reject_project_pii(value: str | None) -> str | None:
    """Reject obviously-PII project names before they reach logs + audit rows.

    The ``project`` field flows into INFO logs, audit rows, Langfuse trace
    attributes, and the context string sent to the LLM. Customer-facing
    deployments sometimes let operators invent project names freely; this
    guardrail catches the shapes that would turn a project name into
    personal data under GDPR (emails most commonly). It is intentionally
    narrow — slug conventions belong in the operator runbook, not here —
    so it does not break legitimate mixed-case or short identifiers.
    """
    if value is None:
        return None
    if "@" in value:
        raise ValueError("project must not contain '@' (looks like an email address)")
    if any(ord(c) < 32 for c in value):
        raise ValueError("project must not contain control characters")
    if len(value) > 256:
        raise ValueError("project is too long (max 256 characters)")
    return value


class ChatMessage(BaseModel):
    """Message schema for chat completions."""

    role: str = Field(..., description="Message role (user, assistant, system)")
    content: str = Field(..., description="Message content")


class ChatRequest(BaseModel):
    """Request schema for /v1/chat/completions (OpenAI-compatible)."""

    model: str = Field(default="sovereign-memory", description="Model identifier")
    messages: list[ChatMessage] = Field(..., description="Conversation history")
    temperature: float = Field(
        default=0.7, ge=0.0, le=2.0, description="Sampling temperature"
    )
    top_p: float = Field(default=1.0, ge=0.0, le=1.0, description="Nucleus sampling")
    max_tokens: int | None = Field(
        default=None, ge=1, description="Max tokens to generate"
    )
    stream: bool = Field(default=False, description="Enable SSE streaming")
    context_query: str | None = Field(
        default=None, description="Query for memory retrieval"
    )
    project: str | None = Field(default=None, description="Project for memory context")

    _validate_project = field_validator("project")(_reject_project_pii)


class ChatChoice(BaseModel):
    """Response choice schema."""

    index: int = Field(default=0)
    message: ChatMessage = Field(
        default_factory=lambda: ChatMessage(role="assistant", content="")
    )
    finish_reason: str | None = Field(default="stop")


class ChatCompletionResponse(BaseModel):
    """Response schema for /v1/chat/completions."""

    id: str = Field(default="cmpl-sovereign-001")
    object: str = Field(default="chat.completion")
    created: int = Field(default_factory=lambda: int(datetime.now().timestamp()))
    model: str = Field(default="sovereign-memory")
    choices: list[ChatChoice] = Field(default_factory=list)
    usage: dict[str, int] | None = Field(default=None)


class ContextRequest(BaseModel):
    """Request schema for /context endpoint."""

    query: str = Field(..., description="Query to search memory")
    project: str | None = Field(default=None, description="Project filter")
    limit: int = Field(default=10, ge=1, le=100, description="Max results")
    k: int = Field(default=10, ge=1, le=100, description="NN search k-nearest")

    _validate_project = field_validator("project")(_reject_project_pii)


class ContextResponse(BaseModel):
    """Response schema for /context endpoint (raw ChromaDB results)."""

    context: list[dict[str, Any]] = Field(default_factory=list)
    query: str
    retrieved_at: datetime = Field(default_factory=datetime.now)


class ContextBuildResponse(BaseModel):
    """Response schema for /context endpoint (4-layer assembled context)."""

    context_string: str = Field(
        ..., description="Assembled memory context for system prompt"
    )
    layer_stats: dict[str, int] = Field(
        default_factory=dict, description="Per-layer retrieval counts"
    )
    query: str
    project: str | None = None
    retrieved_at: datetime = Field(default_factory=datetime.now)

    _validate_project = field_validator("project")(_reject_project_pii)


class InteractionRecord(BaseModel):
    """Schema for interaction audit records."""

    id: int | None = None
    project: str
    _validate_project = field_validator("project")(_reject_project_pii)
    source: str = "unknown"
    question: str
    answer: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    timestamp: datetime = Field(default_factory=datetime.now)
    trace_id: str | None = None
    forwarded_turns: int = 0
    has_agent_system: bool = False


class AssessmentQuestion(BaseModel):
    """One adversary question in a recorded self-assessment (ADR-058).

    Maps to an ``assessment_question`` child row when the assessment is
    recorded through ``POST /audit/assessments``.
    """

    question: str = Field(..., description="The adversary question that was asked")
    verdict: str = Field(..., description="The result, e.g. 'pass' or 'low'")
    method: str | None = Field(
        default=None, description="How the question was tested (method / RoE)"
    )


class AssessmentFinding(BaseModel):
    """One finding in a recorded self-assessment (ADR-058)."""

    finding_id: str = Field(..., description="Stable id for this finding")
    severity: str = Field(..., description="critical | high | medium | low | info")
    title: str = Field(..., description="One-line finding title")
    detail: str | None = Field(default=None, description="Fuller description")


class AssessmentDeferral(BaseModel):
    """One deferred / out-of-scope item in a recorded self-assessment (ADR-058)."""

    item: str = Field(..., description="What was deferred or left out of scope")
    reason: str | None = Field(default=None, description="Why it was deferred")


class AssessmentIngestRequest(BaseModel):
    """Request body for ``POST /audit/assessments`` (ADR-058 recursive self-audit).

    One assessment fans out to a single ``assessment_header`` row plus N
    child rows (question / finding / deferral), all correlated by
    ``assessment_id`` and owner-scoped to the authenticated assessor.
    """

    project: str = Field(default="self-audit", description="Audit project namespace")
    source: str = Field(default="security-assessment", description="Audit source tag")
    assessment_id: str = Field(
        ..., description="Stable id correlating the header and its child rows"
    )
    frameworks: list[str] = Field(
        default_factory=list,
        description="Frameworks the assessment ran against (OWASP, ASVS, ...)",
    )
    rules_of_engagement: str | None = Field(
        default=None, description="The fixed RoE the assessment ran under"
    )
    teardown: str | None = Field(
        default=None, description="Teardown / environment-destroyed evidence"
    )
    questions: list[AssessmentQuestion] = Field(default_factory=list)
    findings: list[AssessmentFinding] = Field(default_factory=list)
    deferrals: list[AssessmentDeferral] = Field(default_factory=list)

    _validate_project = field_validator("project")(_reject_project_pii)


class SessionSaveRequest(BaseModel):
    """Request schema for /session/save endpoint."""

    project: str = Field(..., description="Project identifier")
    interactions: list[InteractionRecord] = Field(
        ..., description="List of interactions to persist"
    )
    metadata: dict[str, Any] | None = Field(
        default=None, description="Session metadata"
    )

    _validate_project = field_validator("project")(_reject_project_pii)


class SessionSummaryRequest(BaseModel):
    """Request schema for /session/summary endpoint.

    Equivalent of the legacy ``python3 memory.py session-save --project P
    --summary S --key-points P1 P2 ...`` workflow — saves a session summary
    row directly to the conversational memory layer.
    """

    project: str = Field(..., description="Project identifier")
    summary: str = Field(..., description="One-sentence summary of what was done")
    key_points: list[str] = Field(
        default_factory=list, description="Discrete decisions, facts, or milestones"
    )
    # ADR-030 contract: callers summarising a real chat session should
    # pass the chat session_id here so hybrid recall can merge this row
    # with the matching interactions. Standalone summaries (admin,
    # historical import) can omit — the route generates a UUID.
    session_id: str | None = Field(
        default=None,
        description=(
            "Session identifier. Omit to have the server generate a UUID "
            "for standalone summaries; pass the chat session_id when "
            "summarising a chat the LLM participated in."
        ),
    )

    _validate_project = field_validator("project")(_reject_project_pii)


class SessionSummaryResponse(BaseModel):
    """Response from /session/summary endpoint."""

    status: str = "ok"
    session_id: str
    project: str


class HealthResponse(BaseModel):
    """Schema for /health endpoint.

    ``version`` defaults to a constant for backwards compatibility with
    older clients; in production handlers should override with the
    package version (see audittrace.routes.health.health_check).
    """

    status: str = "ok"
    # Default ``"unknown"`` signals an uninstalled source tree (running
    # outside of ``pip install``). The route handler always overrides
    # via ``server._resolve_version()``, so the default only fires
    # when callers construct the model directly without a value
    # (tests + ad-hoc scripts). ADR-055 §1 — no hardcoded version
    # literals; importlib.metadata is the single source.
    version: str = "unknown"
    components: dict[str, str] = Field(default_factory=dict)


class MetricsResponse(BaseModel):
    """Schema for /metrics endpoint."""

    chroma_collections: int = 0
    total_chunks: int = 0
    active_sessions: int = 0
    uptime_seconds: int = 0


# ─────────────────── ADR-046 Bucket-2 (v1.0.10) — list responses ─────────
# These types narrow what was previously ``dict[str, Any]`` on five
# routes so /openapi.json can render exact field shapes. No runtime
# behaviour change — same fields are returned in the same order.


class SessionSaveResponse(BaseModel):
    """Response from POST /session/save."""

    status: str = "ok"
    project: str
    interactions_saved: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class InteractionListItem(BaseModel):
    """One row in the interactions list. Mirror of InteractionRecord
    fields the GET /interactions handler returns today."""

    id: int
    project: str
    source: str
    question: str
    answer: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    timestamp: str
    session_id: str | None = None
    model: str | None = None
    user_id: str | None = None
    status: str = "success"
    failure_class: str | None = None
    error_detail: str | None = None
    duration_ms: int | None = None
    trace_id: str | None = None
    # Migration 012 (ADR-048): interaction | security | assessment.
    event_class: str | None = None
    # Migration 015 (ADR-058 WS-A1): DB-server-assigned insert clock,
    # serialised to ISO-8601. Surfaced here so the audit API actually
    # returns the writer-independent timestamp (response_model filters
    # any field not declared).
    created_at: str | None = None
    # Migration 017 (ADR-058 WS-A3): content-integrity hash.
    content_hash: str | None = None


class InteractionListResponse(BaseModel):
    """Response from GET /interactions."""

    interactions: list[InteractionListItem] = Field(default_factory=list)
    total: int = 0
    limit: int = 100
    offset: int = 0


class ToolCallListItem(BaseModel):
    """One row in ``GET /interactions/{id}/tool-calls`` (#363 / RF-10).

    Mirrors the ``tool_calls`` columns. ``granted_scope`` is the field
    that answers "who was allowed to call what" under audit: a REFUSED
    call is recorded with ``granted_scope=""`` and an ``error``, which is
    what makes a genuine zero-tool interaction distinguishable from one
    where the calls went missing.
    """

    id: str
    interaction_id: int
    user_id: str
    agent_type: str
    tool_name: str
    args: str
    result_summary: str | None = None
    error: str | None = None
    started_at: str | None = None
    duration_ms: int | None = None
    granted_scope: str


class ToolCallListResponse(BaseModel):
    """Response from ``GET /interactions/{id}/tool-calls``.

    ``total`` is deliberately the count of rows returned for THIS
    interaction rather than a paginated grand total: the per-interaction
    fan-out is bounded (one row per tool the model invoked in a single
    completion), so a caller counting tool calls for cross-store
    corroboration gets the whole set in one response and never has to
    reason about whether a zero means "none" or "next page".
    """

    interaction_id: int
    tool_calls: list[ToolCallListItem] = Field(default_factory=list)
    total: int = 0


class SessionListItem(BaseModel):
    """One row in GET /sessions. Mirrors the SessionRecord columns
    serialised by ``audit._session_row_to_dict``."""

    id: str
    project: str
    date: str | None = None
    summary: str | None = None
    key_points: str | None = None  # JSON-encoded list of strings
    model: str | None = None
    user_id: str | None = None
    summarized_at: str | None = None
    # #344 — trace_id of the background summariser run that produced the row,
    # so an audit-API reader can pivot to the Tempo/Langfuse trace.
    trace_id: str | None = None


class SessionListResponse(BaseModel):
    """Response from GET /sessions."""

    sessions: list[SessionListItem] = Field(default_factory=list)
    total: int = 0
    limit: int = 100
    offset: int = 0


class ConversationalSessionItem(BaseModel):
    """One row in GET /memory/conversational. Mirrors what the handler
    returns from SessionRow today (note: ``key_points`` is the raw
    JSON-encoded string straight off the column, not parsed)."""

    id: str
    project: str
    date: str | None = None
    model: str | None = None
    summary: str | None = None
    key_points: str | None = None
    summarized_at: str | None = None
    user_id: str | None = None


class ConversationalListResponse(BaseModel):
    """Response from GET /memory/conversational."""

    items: list[ConversationalSessionItem] = Field(default_factory=list)
    total: int = 0
    limit: int = 100
    offset: int = 0


class ConversationalDetailInteraction(BaseModel):
    """One interaction row in the per-session detail response. Distinct
    from InteractionListItem because the order/keys come from a
    different SELECT and must match for response_model validation."""

    id: int
    timestamp: str
    session_id: str | None = None
    source: str
    project: str
    question: str
    answer: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str | None = None
    status: str = "success"
    failure_class: str | None = None
    error_detail: str | None = None
    duration_ms: int | None = None
    trace_id: str | None = None


class ConversationalDetailResponse(BaseModel):
    """Response from GET /memory/conversational/{session_id}."""

    session: ConversationalSessionItem
    interactions: list[ConversationalDetailInteraction] = Field(default_factory=list)
    total: int = 0


# ── Console-conversations (WU-1, MongoDB-elimination EPIC) ──────────────
#
# Deliberately carry NO ``user_sub``/``user_id`` field on any request
# model below — the route layer stamps ``user_sub`` from the resolved
# ``UserContext`` (token-derived), never from the request body
# (feedback_never_trust_caller_metadata_for_security_fields). Pydantic's
# default ``extra="ignore"`` means a hostile caller sending a
# ``user_sub``/``user_id`` JSON key is silently dropped during parsing —
# there is no field on these models it could bind to.


class ConsoleConversationUpsertRequest(BaseModel):
    """Request body for ``POST /console/conversations`` — create/update
    the caller's own conversation (upsert by ``conversation_id``)."""

    conversation_id: str = Field(..., min_length=1, max_length=255)
    title: str | None = Field(default=None, max_length=512)
    endpoint: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=128)
    is_temporary: bool = False
    agent_id: str | None = Field(default=None, max_length=255)
    chat_project_id: str | None = Field(default=None, max_length=255)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConsoleConversationTitleUpdateRequest(BaseModel):
    """Request body for ``PATCH /console/conversations/{conversation_id}``."""

    title: str = Field(..., min_length=1, max_length=512)


class ConsoleConversationItem(BaseModel):
    """One conversation row, as returned by the console-conversations API."""

    conversation_id: str
    title: str
    endpoint: str | None = None
    model: str | None = None
    is_temporary: bool = False
    agent_id: str | None = None
    chat_project_id: str | None = None
    created_at_ms: int
    updated_at_ms: int
    deleted_at_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConsoleConversationListResponse(BaseModel):
    """Response from ``GET /console/conversations`` — cursor-paginated,
    newest-first."""

    items: list[ConsoleConversationItem] = Field(default_factory=list)
    next_cursor: str | None = None


class ConsoleMessageUpsertRequest(BaseModel):
    """Request body for
    ``POST /console/conversations/{conversation_id}/messages`` —
    create/update the caller's own message (upsert by ``message_id``)."""

    message_id: str = Field(..., min_length=1, max_length=255)
    parent_message_id: str | None = Field(default=None, max_length=255)
    sender: str = Field(..., min_length=1, max_length=64)
    text: str
    is_created_by_user: bool
    model: str | None = Field(default=None, max_length=128)
    endpoint: str | None = Field(default=None, max_length=64)
    token_count: int | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConsoleMessageEditRequest(BaseModel):
    """Request body for
    ``PATCH /console/conversations/{conversation_id}/messages/{message_id}``.
    Both fields optional — only the ones supplied are updated."""

    text: str | None = None
    metadata: dict[str, Any] | None = None


class ConsoleMessageItem(BaseModel):
    """One message row, as returned by the console-conversations API."""

    message_id: str
    conversation_id: str
    parent_message_id: str | None = None
    sender: str
    text: str
    is_created_by_user: bool
    model: str | None = None
    endpoint: str | None = None
    token_count: int | None = None
    error: str | None = None
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConsoleMessageListResponse(BaseModel):
    """Response from
    ``GET /console/conversations/{conversation_id}/messages`` — the full
    message tree, chronological (``created_at_ms`` ASC)."""

    items: list[ConsoleMessageItem] = Field(default_factory=list)


# ── Console-presets (WU-presets, MongoDB-elimination EPIC) ───────────────
#
# Deliberately carry NO ``user_sub``/``user_id`` field on the request
# model below — same rationale as the console-conversations models
# above (feedback_never_trust_caller_metadata_for_security_fields).


class ConsolePresetUpsertRequest(BaseModel):
    """Request body for ``POST /console/presets`` — create/update the
    caller's own preset (upsert by ``preset_id``)."""

    preset_id: str = Field(..., min_length=1, max_length=255)
    title: str | None = Field(default=None, max_length=512)
    data: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConsolePresetItem(BaseModel):
    """One preset row, as returned by the console-presets API."""

    preset_id: str
    title: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at_ms: int
    updated_at_ms: int
    deleted_at_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConsolePresetListResponse(BaseModel):
    """Response from ``GET /console/presets`` — cursor-paginated,
    newest-first."""

    items: list[ConsolePresetItem] = Field(default_factory=list)
    next_cursor: str | None = None


# ── Console-prompts (Mongo-repl WU-prompts, MongoDB-elimination EPIC) ────
#
# Deliberately carry NO ``user_sub``/``user_id`` field on any request
# model below — same rationale as the console-conversations/console-
# presets models above (feedback_never_trust_caller_metadata_for_security_fields).
# Model shape mirrors console-conversations' group+child-rows split
# (group ~ ConsoleConversation, version ~ ConsoleMessage) — see
# services/console_prompts.py's module docstring for the full
# LibreChat-PromptGroup/Prompt design-note rationale.


class ConsolePromptGroupUpsertRequest(BaseModel):
    """Request body for ``POST /console/prompts`` — create/update the
    caller's own prompt group (upsert by ``group_id``)."""

    group_id: str = Field(..., min_length=1, max_length=255)
    name: str = Field(..., min_length=1, max_length=512)
    category: str | None = Field(default=None, max_length=128)
    oneliner: str | None = Field(default=None, max_length=4096)
    command: str | None = Field(default=None, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConsolePromptVersionUpsertRequest(BaseModel):
    """Request body for ``POST /console/prompts/{group_id}/versions`` —
    create/update the caller's own prompt version (upsert by
    ``prompt_id``) attached to ``group_id``."""

    prompt_id: str = Field(..., min_length=1, max_length=255)
    text: str = Field(..., min_length=1)
    type: Literal["text", "chat"] = "text"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConsolePromptSetProductionRequest(BaseModel):
    """Request body for ``PATCH /console/prompts/{group_id}/production``
    — mark ``prompt_id`` (an existing version of the group) as the
    group's production version."""

    prompt_id: str = Field(..., min_length=1, max_length=255)


class ConsolePromptVersionItem(BaseModel):
    """One prompt-version row, as returned by the console-prompts API."""

    prompt_id: str
    group_id: str
    text: str
    type: str
    version: int
    created_at_ms: int
    updated_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConsolePromptGroupItem(BaseModel):
    """One prompt-group row (summary shape, no versions), as returned
    by the list-groups API."""

    group_id: str
    name: str
    category: str
    oneliner: str
    command: str | None = None
    production_prompt_id: str | None = None
    created_at_ms: int
    updated_at_ms: int
    deleted_at_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConsolePromptGroupWithVersionsItem(ConsolePromptGroupItem):
    """The full group shape, INCLUDING its version history — returned
    by ``GET /console/prompts/{group_id}`` and
    ``PATCH /console/prompts/{group_id}/production``."""

    versions: list[ConsolePromptVersionItem] = Field(default_factory=list)


class ConsolePromptGroupListResponse(BaseModel):
    """Response from ``GET /console/prompts`` — cursor-paginated,
    newest-first, summary shape (no versions per row)."""

    items: list[ConsolePromptGroupItem] = Field(default_factory=list)
    next_cursor: str | None = None


# ── Console-chat-projects (Chat-Projects domain, MongoDB-elimination EPIC) ─
#
# Deliberately carry NO ``user_sub``/``user_id`` field on the request
# model below — same rationale as the console-conversations/console-
# presets/console-prompts models above
# (feedback_never_trust_caller_metadata_for_security_fields).


class ConsoleChatProjectUpsertRequest(BaseModel):
    """Request body for ``POST /console/chat-projects`` — create/update
    the caller's own chat-project (upsert by ``chat_project_id``)."""

    chat_project_id: str = Field(..., min_length=1, max_length=255)
    name: str = Field(..., min_length=1, max_length=512)
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConsoleChatProjectItem(BaseModel):
    """One chat-project row, as returned by the console-chat-projects
    API."""

    chat_project_id: str
    name: str
    description: str
    created_at_ms: int
    updated_at_ms: int
    deleted_at_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConsoleChatProjectListResponse(BaseModel):
    """Response from ``GET /console/chat-projects`` — cursor-paginated,
    newest-first."""

    items: list[ConsoleChatProjectItem] = Field(default_factory=list)
    next_cursor: str | None = None


# ── Console-files (Files-metadata domain, MongoDB-elimination EPIC) ───────
#
# METADATA ONLY — the file bytes stay in object storage
# (feedback_storage_always_s3); this request model carries no byte
# payload, only the record that references it.
#
# Deliberately carries NO ``user_sub``/``user_id`` field — same
# rationale as every other console-* upsert request model above
# (feedback_never_trust_caller_metadata_for_security_fields).


class ConsoleFileUpsertRequest(BaseModel):
    """Request body for ``POST /console/files`` — create/update the
    caller's own file-metadata record (upsert by ``file_id``)."""

    file_id: str = Field(..., min_length=1, max_length=255)
    filename: str = Field(..., min_length=1, max_length=512)
    type: str = Field(..., min_length=1, max_length=255)
    bytes: int = Field(default=0, ge=0)
    object_key: str | None = None
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    context: str | None = Field(default=None, max_length=128)
    usage: dict[str, Any] = Field(default_factory=dict)
    embedded: bool = False
    temp_file_id: str | None = Field(default=None, max_length=255)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConsoleFileItem(BaseModel):
    """One file-metadata row, as returned by the console-files API."""

    file_id: str
    filename: str
    type: str
    bytes: int
    object_key: str | None = None
    width: int | None = None
    height: int | None = None
    context: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    embedded: bool = False
    temp_file_id: str | None = None
    created_at_ms: int
    updated_at_ms: int
    deleted_at_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConsoleFileListResponse(BaseModel):
    """Response from ``GET /console/files`` — cursor-paginated,
    newest-first."""

    items: list[ConsoleFileItem] = Field(default_factory=list)
    next_cursor: str | None = None


class ConsoleFileBatchGetRequest(BaseModel):
    """Request body for ``POST /console/files/batch-get`` — fetch the
    caller's OWN file-metadata records for a batch of ``file_id``s in
    one round trip (the fork resolves a conversation's attachments this
    way). Capped at ``MAX_LIST_LIMIT`` (200) — same bound as the
    cursor-paginated list route, so a hostile caller cannot force an
    unbounded ``IN (...)`` query."""

    file_ids: list[str] = Field(..., min_length=1, max_length=200)


class ConsoleFileBatchGetResponse(BaseModel):
    """Response from ``POST /console/files/batch-get``. ``items`` omits
    any requested ``file_id`` that doesn't exist, is soft-deleted, or
    belongs to another user — same not-found-vs-403 discipline as
    ``GET /console/files/{file_id}`` (never leaks existence), just
    batched: the caller cannot distinguish "not mine" from "never
    existed" from the response shape alone."""

    items: list[ConsoleFileItem] = Field(default_factory=list)


# ── Console-agents (Agents domain, MongoDB-elimination EPIC) ──────────────
#
# Own-agents-only v1 — sharing/marketplace (a fork ``author``/global-agent
# concept) is explicitly OUT OF SCOPE (disclosed in the ratified spec).
# ``tools``/``model_parameters``/``artifacts`` are opaque jsonb blobs; this
# store persists the agent record, it never executes or validates tools.
#
# Deliberately carries NO ``user_sub``/``user_id`` field on the request
# model below — same rationale as every other console-* upsert request
# model above (feedback_never_trust_caller_metadata_for_security_fields).


class ConsoleAgentUpsertRequest(BaseModel):
    """Request body for ``POST /console/agents`` — create/update the
    caller's own agent (upsert by ``agent_id``)."""

    agent_id: str = Field(..., min_length=1, max_length=255)
    name: str = Field(..., min_length=1, max_length=512)
    description: str | None = None
    instructions: str | None = None
    provider: str | None = Field(default=None, max_length=255)
    model: str | None = Field(default=None, max_length=255)
    model_parameters: dict[str, Any] = Field(default_factory=dict)
    tools: list[Any] = Field(default_factory=list)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    end_after_tools: bool = False
    project_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConsoleAgentItem(BaseModel):
    """One agent row, as returned by the console-agents API."""

    agent_id: str
    name: str
    description: str
    instructions: str | None = None
    provider: str | None = None
    model: str | None = None
    model_parameters: dict[str, Any] = Field(default_factory=dict)
    tools: list[Any] = Field(default_factory=list)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    end_after_tools: bool = False
    project_ids: list[str] = Field(default_factory=list)
    created_at_ms: int
    updated_at_ms: int
    deleted_at_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConsoleAgentListResponse(BaseModel):
    """Response from ``GET /console/agents`` — cursor-paginated,
    newest-first."""

    items: list[ConsoleAgentItem] = Field(default_factory=list)
    next_cursor: str | None = None


class ConsoleAgentBatchGetRequest(BaseModel):
    """Request body for ``POST /console/agents/batch-get`` — fetch the
    caller's OWN agents for a batch of ``agent_id``s in one round trip.
    Capped at ``MAX_LIST_LIMIT`` (200) — same bound as the
    cursor-paginated list route, so a hostile caller cannot force an
    unbounded ``IN (...)`` query."""

    agent_ids: list[str] = Field(..., min_length=1, max_length=200)


class ConsoleAgentBatchGetResponse(BaseModel):
    """Response from ``POST /console/agents/batch-get``. ``items`` omits
    any requested ``agent_id`` that doesn't exist, is soft-deleted, or
    belongs to another user — same not-found-vs-403 discipline as
    ``GET /console/agents/{agent_id}`` (never leaks existence), just
    batched: the caller cannot distinguish "not mine" from "never
    existed" from the response shape alone."""

    items: list[ConsoleAgentItem] = Field(default_factory=list)


# ── Console-conversation-tags (Conversation-Tags domain, MongoDB- ─────────
# elimination EPIC) ────────────────────────────────────────────────────────
#
# Own-tags-only v1 — every row is owned by exactly one user_sub, same
# discipline as every other console-* domain above. ``count``/``position``
# are plain caller-maintained integers this store persists as-is.
#
# Deliberately carries NO ``user_sub``/``user_id`` field on the request
# model below — same rationale as every other console-* upsert request
# model above (feedback_never_trust_caller_metadata_for_security_fields).


class ConsoleConversationTagUpsertRequest(BaseModel):
    """Request body for ``POST /console/conversation-tags`` — create/
    update the caller's own conversation-tag (upsert by ``tag``)."""

    tag: str = Field(..., min_length=1, max_length=512)
    description: str | None = None
    count: int = Field(default=0, ge=0)
    position: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConsoleConversationTagItem(BaseModel):
    """One conversation-tag row, as returned by the console-
    conversation-tags API."""

    tag: str
    description: str
    count: int
    position: int
    created_at_ms: int
    updated_at_ms: int
    deleted_at_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConsoleConversationTagListResponse(BaseModel):
    """Response from ``GET /console/conversation-tags`` —
    cursor-paginated, newest-first."""

    items: list[ConsoleConversationTagItem] = Field(default_factory=list)
    next_cursor: str | None = None


# ── Console-tool-favorites (Tool-Favorites domain, MongoDB- ───────────────
# elimination EPIC) ────────────────────────────────────────────────────────
#
# Own-favorites-only v1 — every row is owned by exactly one user_sub, same
# discipline as every other console-* domain above.
#
# Deliberately carries NO ``user_sub``/``user_id`` field on the request
# model below — same rationale as every other console-* upsert request
# model above (feedback_never_trust_caller_metadata_for_security_fields).

# The SINGLE source of truth for the closed item_type vocabulary (mirrors
# the fork's types/favorite.ts::FAVORITE_ITEM_TYPES Mongoose enum). The
# service module deliberately carries no duplicate — a typo'd item_type
# is rejected here with 422 before any service method runs.
_TOOL_FAVORITE_ITEM_TYPE = Literal["builtin", "tool", "mcp", "skill"]


class ConsoleToolFavoriteAddRequest(BaseModel):
    """Request body for ``POST /console/tool-favorites`` — add (or
    idempotently re-affirm) the caller's own tool-favorite."""

    item_type: _TOOL_FAVORITE_ITEM_TYPE
    item_id: str = Field(..., min_length=1, max_length=256)
    tenant_id: str | None = Field(default=None, max_length=256)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConsoleToolFavoriteItem(BaseModel):
    """One tool-favorite row, as returned by the console-tool-favorites
    API."""

    item_type: str
    item_id: str
    tenant_id: str | None = None
    created_at_ms: int
    updated_at_ms: int
    deleted_at_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConsoleToolFavoriteListResponse(BaseModel):
    """Response from ``GET /console/tool-favorites`` — the caller's
    ENTIRE favorites list (no pagination; bounded by
    ``MAX_TOOL_FAVORITES``), oldest-first."""

    items: list[ConsoleToolFavoriteItem] = Field(default_factory=list)
