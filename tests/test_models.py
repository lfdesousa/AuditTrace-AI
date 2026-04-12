"""Tests for Pydantic models."""

from datetime import datetime

import pytest

from sovereign_memory.models import (
    ChatChoice,
    ChatCompletionResponse,
    ChatMessage,
    ChatRequest,
    ContextBuildResponse,
    ContextRequest,
    ContextResponse,
    HealthResponse,
    InteractionRecord,
    MetricsResponse,
    SessionSaveRequest,
)


def test_chat_message_model():
    """Test ChatMessage model."""
    msg = ChatMessage(role="user", content="Hello")
    assert msg.role == "user"
    assert msg.content == "Hello"

    msg = ChatMessage(role="assistant", content="Hi there")
    assert msg.role == "assistant"


def test_chat_request_model():
    """Test ChatRequest model."""
    request = ChatRequest(
        model="sovereign-memory",
        messages=[ChatMessage(role="user", content="Test")],
        temperature=0.7,
    )
    assert request.model == "sovereign-memory"
    assert len(request.messages) == 1
    assert request.temperature == 0.7
    assert request.top_p == 1.0


def test_chat_request_with_all_params():
    """Test ChatRequest with all parameters."""
    request = ChatRequest(
        model="custom-model",
        messages=[
            ChatMessage(role="system", content="You are helpful"),
            ChatMessage(role="user", content="Hello"),
        ],
        temperature=0.8,
        top_p=0.9,
        max_tokens=500,
        stream=False,
        context_query="retrieval context",
    )
    assert request.model == "custom-model"
    assert len(request.messages) == 2
    assert request.temperature == 0.8
    assert request.top_p == 0.9
    assert request.max_tokens == 500
    assert request.context_query == "retrieval context"


def test_chat_request_validation_temperature():
    """Test ChatRequest temperature validation."""
    # Temperature too low
    with pytest.raises(ValueError):
        ChatRequest(
            model="test",
            messages=[],
            temperature=-0.1,
        )

    # Temperature too high
    with pytest.raises(ValueError):
        ChatRequest(
            model="test",
            messages=[],
            temperature=2.1,
        )


def test_chat_request_validation_top_p():
    """Test ChatRequest top_p validation."""
    with pytest.raises(ValueError):
        ChatRequest(
            model="test",
            messages=[],
            top_p=1.1,
        )


def test_chat_request_validation_max_tokens():
    """Test ChatRequest max_tokens validation."""
    with pytest.raises(ValueError):
        ChatRequest(
            model="test",
            messages=[],
            max_tokens=0,
        )


def test_chat_choice_model():
    """Test ChatChoice model."""
    choice = ChatChoice(
        index=0,
        message=ChatMessage(role="assistant", content="Answer"),
        finish_reason="stop",
    )
    assert choice.index == 0
    assert choice.message.role == "assistant"
    assert choice.finish_reason == "stop"


def test_chat_completion_response_model():
    """Test ChatCompletionResponse model."""
    response = ChatCompletionResponse(
        id="test-id",
        model="sovereign-memory",
        choices=[
            ChatChoice(
                index=0,
                message=ChatMessage(role="assistant", content="Hello"),
            )
        ],
    )
    assert response.id == "test-id"
    assert response.object == "chat.completion"
    assert len(response.choices) == 1


def test_context_request_model():
    """Test ContextRequest model."""
    request = ContextRequest(
        query="test query",
        project="test-project",
        limit=10,
        k=5,
    )
    assert request.query == "test query"
    assert request.project == "test-project"
    assert request.limit == 10
    assert request.k == 5


def test_context_request_validation():
    """Test ContextRequest validation."""
    # Invalid limit
    with pytest.raises(ValueError):
        ContextRequest(
            query="test",
            limit=0,
        )

    # Invalid k
    with pytest.raises(ValueError):
        ContextRequest(
            query="test",
            k=101,
        )


def test_context_response_model():
    """Test ContextResponse model."""
    response = ContextResponse(
        query="test query",
        context=[
            {"id": "1", "content": "doc1", "metadata": {"project": "test"}},
        ],
    )
    assert response.query == "test query"
    assert len(response.context) == 1
    assert isinstance(response.retrieved_at, datetime)


def test_interaction_record_model():
    """Test InteractionRecord model."""
    record = InteractionRecord(
        project="test",
        source="opencode",
        question="What is AI?",
        answer="AI is artificial intelligence",
        prompt_tokens=10,
        completion_tokens=20,
    )
    assert record.project == "test"
    assert record.source == "opencode"
    assert record.has_agent_system is False
    assert isinstance(record.timestamp, datetime)
    assert record.trace_id is None
    assert record.forwarded_turns == 0


def test_session_save_request_model():
    """Test SessionSaveRequest model."""
    request = SessionSaveRequest(
        project="test-project",
        interactions=[
            InteractionRecord(
                project="test",
                question="Q1",
                answer="A1",
            )
        ],
        metadata={"version": "1.0"},
    )
    assert request.project == "test-project"
    assert len(request.interactions) == 1
    assert request.metadata["version"] == "1.0"


def test_chat_request_with_project():
    """Test ChatRequest with project field (ADR-018)."""
    request = ChatRequest(
        messages=[ChatMessage(role="user", content="Hello")],
        project="AuditTrace",
    )
    assert request.project == "AuditTrace"


def test_chat_request_project_defaults_none():
    """Test ChatRequest project defaults to None."""
    request = ChatRequest(
        messages=[ChatMessage(role="user", content="Hello")],
    )
    assert request.project is None


def test_context_build_response_model():
    """Test ContextBuildResponse model (ADR-018)."""
    response = ContextBuildResponse(
        context_string="## Profil\nLuis Filipe...",
        layer_stats={
            "episodic": 2,
            "procedural": 1,
            "conversational": 3,
            "semantic": 4,
        },
        query="KV cache compression",
        project="AuditTrace",
    )
    assert response.context_string.startswith("## Profil")
    assert response.layer_stats["episodic"] == 2
    assert response.layer_stats["semantic"] == 4
    assert response.query == "KV cache compression"
    assert response.project == "AuditTrace"
    assert response.retrieved_at is not None


def test_context_build_response_minimal():
    """Test ContextBuildResponse with minimal fields."""
    response = ContextBuildResponse(
        context_string="",
        query="hello",
    )
    assert response.context_string == ""
    assert response.layer_stats == {}
    assert response.project is None


def test_health_response_model():
    """Test HealthResponse model."""
    response = HealthResponse(
        status="ok",
        version="0.2.0",
        components={"server": "running"},
    )
    assert response.status == "ok"
    assert response.version == "0.2.0"


def test_metrics_response_model():
    """Test MetricsResponse model."""
    response = MetricsResponse(
        chroma_collections=5,
        total_chunks=1654,
        active_sessions=0,
        uptime_seconds=3600,
    )
    assert response.chroma_collections == 5
    assert response.total_chunks == 1654
