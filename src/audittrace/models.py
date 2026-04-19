from datetime import datetime
from typing import Any

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
    """Schema for /health endpoint."""

    status: str = "ok"
    version: str = "0.3.1"
    components: dict[str, str] = Field(default_factory=dict)


class MetricsResponse(BaseModel):
    """Schema for /metrics endpoint."""

    chroma_collections: int = 0
    total_chunks: int = 0
    active_sessions: int = 0
    uptime_seconds: int = 0
