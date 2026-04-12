"""Tests for API routes with mocked dependencies."""

# Reuse the AsyncClient fake from the chat-proxy tests so this file doesn't
# duplicate the mock plumbing (ADR-024).
from tests.test_chat_proxy import _FakeAsyncClient, _patch_async_client

_MOCK_LLM_RESPONSE = {
    "id": "cmpl-test",
    "object": "chat.completion",
    "model": "test",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
}


def test_chat_endpoint_with_mock_memory(client):
    """Test chat endpoint proxies to llama-server (ADR-018)."""
    fake = _FakeAsyncClient(post_json=_MOCK_LLM_RESPONSE)
    with _patch_async_client(fake):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "test",
                "messages": [{"role": "user", "content": "Hello"}],
                "temperature": 0.7,
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["model"] == "test"
    assert len(data["choices"]) == 1


def test_context_endpoint_with_mock_memory(client):
    """Test context endpoint returns 4-layer assembled context (ADR-018)."""
    response = client.post(
        "/context",
        json={
            "query": "test query",
            "project": "test-project",
            "limit": 10,
            "k": 5,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert "query" in data
    assert "context_string" in data
    assert "layer_stats" in data
    assert isinstance(data["context_string"], str)
    assert isinstance(data["layer_stats"], dict)


def test_context_endpoint_empty_results(client):
    """Test context endpoint with no matching results."""
    response = client.post(
        "/context",
        json={
            "query": "nonexistent query",
            "project": "nonexistent",
            "limit": 10,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert "Profil" in data["context_string"]  # profile always present


def test_health_endpoint(client):
    """Test health endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "components" in data


def test_metrics_endpoint(client):
    """Test metrics endpoint."""
    response = client.get("/metrics")
    assert response.status_code == 200
    data = response.json()
    assert "chroma_collections" in data
    assert "total_chunks" in data


def test_chat_endpoint_validation(client):
    """Test chat endpoint validates input."""
    # Missing messages
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test",
        },
    )
    assert response.status_code == 422  # Validation error


def test_context_endpoint_validation(client):
    """Test context endpoint validates input."""
    # Missing query
    response = client.post(
        "/context",
        json={
            "limit": 10,
        },
    )
    assert response.status_code == 422


def test_chat_endpoint_with_context_query(client):
    """Test chat endpoint with context query parameter."""
    fake = _FakeAsyncClient(post_json=_MOCK_LLM_RESPONSE)
    with _patch_async_client(fake):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "test",
                "messages": [{"role": "user", "content": "Hello"}],
                "context_query": "retrieval query",
            },
        )

    assert response.status_code == 200


def test_list_interactions(client):
    r = client.get("/interactions?project=p&limit=5&offset=0")
    assert r.status_code == 200
    body = r.json()
    assert body["interactions"] == []
    assert body["limit"] == 5


def test_create_interaction(client):
    r = client.post(
        "/interactions",
        json={
            "project": "p",
            "source": "test",
            "question": "q",
            "answer": "a",
        },
    )
    assert r.status_code == 200
    assert r.json()["project"] == "p"


def test_save_session(client):
    r = client.post(
        "/session/save",
        json={
            "project": "p",
            "interactions": [
                {"project": "p", "source": "s", "question": "q", "answer": "a"}
            ],
            "metadata": {"k": "v"},
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"


def test_save_session_summary(client):
    """Persists a one-sentence summary + key points (legacy memory.py session-save)."""
    r = client.post(
        "/session/summary",
        json={
            "project": "AuditTrace",
            "summary": "Hooked Langfuse SDK to fix trace graph view",
            "key_points": [
                "Langfuse OTLP nests attrs inside metadata['attributes']",
                "Native SDK writes top-level metadata Map keys",
                "telemetry.start_span() now routes through SDK when available",
            ],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["project"] == "AuditTrace"
    assert body["session_id"]  # generated YYYYMMDD_HHMMSS

    # Verify it landed in the test conversational service. Bypass mode
    # writes under the sentinel user_context — read back with the same
    # sentinel identity.
    from sovereign_memory.dependencies import get_conversational_service
    from sovereign_memory.identity import sentinel_user_context

    service = get_conversational_service()
    sessions = service.load_sessions(sentinel_user_context(), "AuditTrace")
    assert any("Langfuse" in s["summary"] for s in sessions)


def test_save_session_summary_minimum_fields(client):
    """key_points is optional."""
    r = client.post(
        "/session/summary",
        json={"project": "AuditTrace", "summary": "Quick note"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_streaming_parameter(client):
    """Test streaming parameter is accepted and returns SSE chunks."""
    fake = _FakeAsyncClient(stream_lines=["data: [DONE]", ""])
    with _patch_async_client(fake):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "test",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
        )

    assert response.status_code == 200
    assert "event-stream" in response.headers.get("content-type", "")
