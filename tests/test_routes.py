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
    assert "Profile" in data["context_string"]  # profile always present


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


def test_list_interactions_empty(client):
    r = client.get("/interactions?project=p&limit=5&offset=0")
    assert r.status_code == 200
    body = r.json()
    assert body["interactions"] == []
    assert body["limit"] == 5
    assert body["offset"] == 0
    assert body["total"] == 0


def _seed_interaction(
    *,
    project: str,
    question: str = "q",
    answer: str = "a",
    session_id: str = "sess-1",
    source: str = "curl",
    user_id: str = "user-1",
    timestamp: str = "2026-04-14T12:00:00",
) -> int:
    """Insert one interaction row through the real DB factory."""
    from audittrace.db.models import InteractionRecord as Row
    from audittrace.dependencies import get_postgres_factory

    pg = get_postgres_factory()
    with pg.get_session_factory()() as db:
        r = Row(
            project=project,
            question=question,
            answer=answer,
            session_id=session_id,
            source=source,
            user_id=user_id,
            timestamp=timestamp,
        )
        db.add(r)
        db.commit()
        db.refresh(r)
        return int(r.id)


def test_list_interactions_returns_seeded_rows(client):
    """Seeded rows come back in DESC id order with correct shape."""
    _seed_interaction(project="alpha", question="q1")
    _seed_interaction(project="alpha", question="q2")
    _seed_interaction(project="beta", question="q3")

    r = client.get("/interactions?project=alpha&limit=10")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert len(body["interactions"]) == 2
    # DESC by id — newest first.
    assert body["interactions"][0]["question"] == "q2"
    assert body["interactions"][1]["question"] == "q1"
    # Shape check: every documented field round-trips.
    row = body["interactions"][0]
    for key in (
        "id",
        "project",
        "source",
        "question",
        "answer",
        "prompt_tokens",
        "completion_tokens",
        "timestamp",
        "session_id",
        "model",
        "user_id",
        # Migration 007 / ADR-033: failure-audit columns must also be
        # exposed or the audit API shows every row as implicitly
        # successful — masking real failures.
        "status",
        "failure_class",
        "error_detail",
        "duration_ms",
    ):
        assert key in row


def test_list_interactions_filters_by_session_source_user(client):
    """All four filter params narrow the result set independently."""
    _seed_interaction(
        project="multi", session_id="s-A", source="opencode", user_id="u-A"
    )
    _seed_interaction(
        project="multi", session_id="s-B", source="opencode", user_id="u-B"
    )
    _seed_interaction(project="multi", session_id="s-A", source="curl", user_id="u-A")

    r = client.get("/interactions?project=multi&session_id=s-A")
    assert r.json()["total"] == 2

    r = client.get("/interactions?project=multi&source=curl")
    assert r.json()["total"] == 1

    r = client.get("/interactions?project=multi&user_id=u-B")
    assert r.json()["total"] == 1


def test_list_interactions_exposes_failure_audit_columns(client):
    """ADR-033 migration 007 added status/failure_class/error_detail/
    duration_ms. The /interactions serialiser must surface them so the
    audit browser can enumerate real failures."""
    from audittrace.db.models import InteractionRecord as Row
    from audittrace.dependencies import get_postgres_factory

    pg = get_postgres_factory()
    with pg.get_session_factory()() as db:
        r = Row(
            project="fail-vis",
            question="q",
            answer="",
            session_id="s",
            source="curl",
            user_id="u",
            timestamp="2026-04-18T09:00:00",
            status="failed",
            failure_class="proxy_timeout",
            error_detail="upstream stalled for 300s",
            duration_ms=2018,
        )
        db.add(r)
        db.commit()

    resp = client.get("/interactions?project=fail-vis&status=failed")
    body = resp.json()
    assert body["total"] == 1
    row = body["interactions"][0]
    assert row["status"] == "failed"
    assert row["failure_class"] == "proxy_timeout"
    assert row["error_detail"] == "upstream stalled for 300s"
    assert row["duration_ms"] == 2018

    # status filter is strict — 'success' must not leak failures.
    resp_success = client.get("/interactions?project=fail-vis&status=success")
    assert resp_success.json()["total"] == 0


def test_list_interactions_filter_since(client):
    """since= is an inclusive lower bound on timestamp."""
    _seed_interaction(project="t", timestamp="2026-04-13T00:00:00")
    _seed_interaction(project="t", timestamp="2026-04-14T00:00:00")
    _seed_interaction(project="t", timestamp="2026-04-15T00:00:00")

    r = client.get("/interactions?project=t&since=2026-04-14T00:00:00")
    body = r.json()
    assert body["total"] == 2


def test_list_interactions_pagination(client):
    """limit + offset slice the ordered result set."""
    for i in range(5):
        _seed_interaction(project="page", question=f"q{i}")

    r = client.get("/interactions?project=page&limit=2&offset=0")
    body = r.json()
    assert body["total"] == 5
    assert len(body["interactions"]) == 2

    r = client.get("/interactions?project=page&limit=2&offset=4")
    body = r.json()
    assert body["total"] == 5
    assert len(body["interactions"]) == 1


def test_list_interactions_rejects_limit_over_max(client):
    """limit > 1000 is a 422 (FastAPI query-param validation)."""
    r = client.get("/interactions?limit=10000")
    assert r.status_code == 422


def test_list_interactions_rejects_negative_offset(client):
    r = client.get("/interactions?offset=-1")
    assert r.status_code == 422


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
    from audittrace.dependencies import get_conversational_service
    from audittrace.identity import sentinel_user_context

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
