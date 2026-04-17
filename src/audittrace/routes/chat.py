"""Chat completions route — memory augmentation + llama-server proxy (ADR-024).

Receives OpenAI-compatible chat requests, augments the system message with
4-layer memory context, and forwards the *raw* request dict to llama-server.
Pass-through is intentional: every field on the inbound payload (``tools``,
``tool_choice``, ``tool_calls``, ``tool_call_id``, ``name``, ``function_call``,
``response_format``, …) is preserved so the OpenAI tool-calling protocol works
end-to-end. A strict Pydantic schema would silently strip unknown fields and
break the tool workflow — see ADR-024 for the regression history.

Spans are produced by an explicit ``@observe``-decorated memory build helper
that returns a ``trace_id`` value. The streaming generator runs *after* the
request handler returns, so we cannot rely on a context-manager span being
open during stream consumption — instead we update the trace via Langfuse's
ingestion API with the captured ``trace_id`` once the stream ends.
"""

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

# Side-effect import so the four @register_memory_tool decorators run at
# module import time — memory tools have to be in the registry before any
# request reaches ``chat_completions``.
import audittrace.tools.memory_handlers  # noqa: F401
from audittrace import telemetry
from audittrace.auth import require_scope, require_user
from audittrace.config import Settings, get_settings
from audittrace.db.models import InteractionRecord, ToolCall
from audittrace.dependencies import get_context_builder, get_postgres_factory
from audittrace.identity import UserContext
from audittrace.logging_config import log_call, reset_langgraph_step
from audittrace.routes._memory_tool_loop import (
    PendingToolCall,
    run_memory_tool_loop,
)
from audittrace.services.context_builder import (
    ContextBuilderService,
    build_ambient_context,
)
from audittrace.tools import tools_visible_to

# Failure taxonomy (ADR-033 seed). Stored verbatim in
# ``interactions.failure_class``. Kept as module-level constants rather than
# an Enum so call sites read as plain strings and the value written to
# Postgres is copy-pasteable in SQL audit queries.
FAILURE_PROXY_TIMEOUT = "proxy_timeout"
FAILURE_UPSTREAM_ERROR = "upstream_error"
FAILURE_UPSTREAM_UNREACHABLE = "upstream_unreachable"
FAILURE_INTERNAL_ERROR = "internal_error"


def _openai_error_body(
    message: str,
    code: str,
    *,
    type_: str = "api_error",
    param: str | None = None,
    **extensions: Any,
) -> dict[str, Any]:
    """Build an error body that is a strict superset of OpenAI's shape.

    OpenAI's canonical error body is
    ``{"error": {"message", "type", "param", "code"}}``. We keep those
    four keys with OpenAI-compatible semantics so any OpenAI SDK parses
    the error unchanged. AuditTrace-specific extensions
    (``status``, ``operator_hint``, ``trace_id``,
    ``user_facing_message``) are added as NET-NEW keys — a client that
    only reads the four OpenAI keys keeps working.

    See ``feedback_openai_schema_inviolate`` memory for the principle:
    OpenAI compatibility is the project's integration fan-in and the
    thing that keeps us in the race; every error-shape change must
    preserve it.
    """
    body: dict[str, Any] = {
        "message": message,
        "type": type_,
        "param": param,
        "code": code,
    }
    body.update(extensions)
    return {"error": body}


# ──────────────── Stream helpers (backlog #01) ─────────────────────────


@dataclass
class _StreamState:
    """Mutable accumulator for SSE stream parsing."""

    chunks: list[str] = field(default_factory=list)
    tool_calls_acc: dict[int, dict[str, Any]] = field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str | None = None
    id: str | None = None
    created: int | None = None
    finish_reason: str | None = None
    done_seen: bool = False


def _accumulate_chunk(state: _StreamState, chunk: dict[str, Any]) -> None:
    """Extract metadata, content, tool_calls, and usage from a parsed SSE chunk."""
    if not state.model and chunk.get("model"):
        state.model = chunk["model"]
    if not state.id and chunk.get("id"):
        state.id = chunk["id"]
    if not state.created and chunk.get("created"):
        state.created = chunk["created"]
    choices = chunk.get("choices") or []
    if choices:
        choice0 = choices[0]
        if choice0.get("finish_reason"):
            state.finish_reason = choice0["finish_reason"]
        delta = choice0.get("delta") or {}
        content = delta.get("content")
        if content:
            state.chunks.append(content)
        # Accumulate streamed tool_calls by index — fragments
        # of function.arguments arrive across many chunks.
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            if idx not in state.tool_calls_acc:
                state.tool_calls_acc[idx] = {
                    "id": tc.get("id", ""),
                    "type": tc.get("type", "function"),
                    "function": {
                        "name": "",
                        "arguments": "",
                    },
                }
            entry = state.tool_calls_acc[idx]
            if tc.get("id"):
                entry["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                entry["function"]["name"] = fn["name"]
            if fn.get("arguments"):
                entry["function"]["arguments"] += fn["arguments"]
    usage = chunk.get("usage") or {}
    if usage:
        state.prompt_tokens = usage.get("prompt_tokens", state.prompt_tokens)
        state.completion_tokens = usage.get(
            "completion_tokens", state.completion_tokens
        )
    timings = chunk.get("timings") or {}
    if timings:
        cache_n = int(timings.get("cache_n", 0) or 0)
        prompt_n = int(timings.get("prompt_n", 0) or 0)
        predicted_n = int(timings.get("predicted_n", 0) or 0)
        if state.prompt_tokens == 0:
            state.prompt_tokens = cache_n + prompt_n
        if state.completion_tokens == 0:
            state.completion_tokens = predicted_n


def _build_synthetic_usage_chunk(state: _StreamState, requested_model: str) -> bytes:
    """Build the synthetic usage SSE chunk emitted after the stream ends."""
    usage_chunk: dict[str, Any] = {
        "id": state.id or "chatcmpl-sovereign",
        "object": "chat.completion.chunk",
        "created": state.created or 0,
        "model": state.model or requested_model,
        "choices": [],
        "usage": {
            "prompt_tokens": state.prompt_tokens,
            "completion_tokens": state.completion_tokens,
            "total_tokens": state.prompt_tokens + state.completion_tokens,
        },
    }
    return ("data: " + json.dumps(usage_chunk) + "\n\n").encode()


def _emit_sse_error_frame(failure_class: str, message: str, status: int) -> bytes:
    """Build an SSE error frame + [DONE] terminator for streaming errors."""
    err_body = _openai_error_body(message=message, code=failure_class, status=status)
    return ("data: " + json.dumps(err_body) + "\n\n" + "data: [DONE]\n\n").encode()


# Optional Langfuse decorator + client lookup. The package is in requirements
# but the @observe path is a soft dependency — when Langfuse is disabled or
# the import fails for any reason, we fall back to a no-op decorator and a
# null trace_id so the proxy keeps working unchanged.
try:
    from langfuse import get_client as _lf_get_client
    from langfuse import observe as _lf_observe

    _LANGFUSE_AVAILABLE = True
except Exception:  # pragma: no cover - optional dep path
    _LANGFUSE_AVAILABLE = False

    def _lf_observe(*args: Any, **kwargs: Any) -> Any:
        def deco(fn: Any) -> Any:
            return fn

        if args and callable(args[0]):
            return args[0]
        return deco

    def _lf_get_client() -> None:
        return None


logger = logging.getLogger(__name__)

router = APIRouter()


def _compute_session_id(source: str, first_user_content: str, user_id: str) -> str:
    """Deterministic session id grouping all turns of the same conversation.

    Format: ``{source}-{YYYY-MM-DD}-{sha256(source|date|user_id|first)[:16]}``
    Stable across requests so Langfuse can cluster traces by session and
    PostgreSQL ``interactions.session_id`` correlates rows.

    DESIGN §15 Phase 2: ``user_id`` (Keycloak ``sub``) is mixed into the
    hash so two users with identical ``(source, date, first_message)``
    can never produce the same session id — a correctness invariant
    once multi-user traffic lands.
    """
    today = date.today().isoformat()
    raw = f"{source}|{today}|{user_id}|{first_user_content}"
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"{source}-{today}-{h}"


def _resolve_project(request: Request, payload: dict[str, Any]) -> str:
    """Resolve the project tag for this request (ADR-029).

    Precedence:
      1. ``X-Project`` HTTP header (case-insensitive — preferred).
      2. ``body.metadata.project`` — OpenAI-compatible metadata dict, if the
         client uses the standard field.
      3. ``body.project`` — legacy / direct JSON body field.
      4. ``"default"`` — unknown caller, still queryable.

    The header is trusted at face value (same honesty model as source
    detection). Per-project ACLs would add a JWT-claim cross-check; for
    now this is metadata for audit + recall_recent_sessions filtering.
    """
    header = request.headers.get("x-project")
    if isinstance(header, str) and header.strip():
        return header.strip()
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        meta_project = metadata.get("project")
        if isinstance(meta_project, str) and meta_project.strip():
            return meta_project.strip()
    body_project = payload.get("project")
    if isinstance(body_project, str) and body_project.strip():
        return body_project.strip()
    return "default"


def _detect_source(request: Request) -> str:
    """Best-effort agent identification from the User-Agent header."""
    ua = (request.headers.get("user-agent") or "").lower()
    for marker in ("opencode", "continue", "roocode", "openai", "curl", "httpx"):
        if marker in ua:
            return marker
    return "unknown"


def _resolve_thinking(request: Request) -> str | None:
    """Parse ``X-Thinking`` header (ADR-034).

    Returns ``"deep"`` or ``"fast"`` when explicitly set, or ``None``
    for ``auto`` (the default — don't touch ``chat_template_kwargs``).
    """
    raw = (request.headers.get("x-thinking") or "").strip().lower()
    if raw in ("deep", "fast"):
        return raw
    return None


def _apply_thinking_mode(payload: dict[str, Any], thinking: str | None) -> None:
    """Inject ``chat_template_kwargs.enable_thinking`` into *payload* in-place.

    ``deep``  → ``enable_thinking = True``
    ``fast``  → ``enable_thinking = False``
    ``None``  → leave payload untouched (auto / model default)
    """
    if thinking is None:
        return
    kwargs = payload.setdefault("chat_template_kwargs", {})
    kwargs["enable_thinking"] = thinking == "deep"


async def _iter_with_idle_timeout(
    resp: httpx.Response,
    chunk_timeout: float,
    keepalive_interval: float = 0,
) -> AsyncIterator[str | None]:
    """Wrap ``resp.aiter_lines()`` with per-chunk idle timeout + keep-alive (ADR-034).

    As long as SSE lines keep arriving within *chunk_timeout* seconds of
    each other, the stream stays alive indefinitely — even if the total
    duration far exceeds any flat timeout.

    When *keepalive_interval* > 0, yields ``None`` every
    *keepalive_interval* seconds during quiet periods (e.g. Qwen
    ``<think>``). The caller converts ``None`` to ``b": keep-alive\\n\\n"``
    (SSE comment frame). After *chunk_timeout* total silence, raises
    ``httpx.ReadTimeout``.

    When *keepalive_interval* is 0 (default), raises ``httpx.ReadTimeout``
    after *chunk_timeout* of silence — pure idle-timeout mode.
    """
    aiter = resp.aiter_lines().__aiter__()
    idle_elapsed = 0.0
    wait = keepalive_interval if keepalive_interval > 0 else chunk_timeout
    # We keep a single pending __anext__ task alive across keep-alive cycles
    # so cancellation doesn't corrupt the async generator's internal state.
    pending_next: asyncio.Task[str] | None = None
    try:
        while True:
            if pending_next is None:
                pending_next = asyncio.ensure_future(aiter.__anext__())
            done, _ = await asyncio.wait({pending_next}, timeout=wait)
            if done:
                try:
                    line = pending_next.result()
                except StopAsyncIteration:
                    return
                pending_next = None
                idle_elapsed = 0.0
                yield line
            else:
                # Timeout — no data arrived in this window.
                idle_elapsed += wait
                if keepalive_interval <= 0 or idle_elapsed >= chunk_timeout:
                    pending_next.cancel()
                    raise httpx.ReadTimeout(
                        f"No data received for {chunk_timeout}s"
                        " (per-chunk idle timeout)"
                    )
                yield None  # keep-alive signal
    finally:
        if pending_next is not None and not pending_next.done():
            pending_next.cancel()


def _persist_interaction(
    project: str,
    source: str,
    question: str,
    answer: str,
    prompt_tokens: int,
    completion_tokens: int,
    session_id: str | None,
    model: str | None,
    user_id: str,
    *,
    status: str = "success",
    failure_class: str | None = None,
    error_detail: str | None = None,
    duration_ms: int | None = None,
) -> int | None:
    """Persist a question/answer pair to PostgreSQL ``interactions``.

    Best-effort — failures are logged but never raised so a DB hiccup
    cannot break a chat response. Returns the autoincrement primary key
    of the new row (or ``None`` on failure) so the caller can use it as
    the FK for any downstream ``ToolCall`` rows linked to this interaction.

    DESIGN §15 Phase 2: ``user_id`` (Keycloak ``sub``) is persisted on
    every row so Phase 4 RLS policies and Phase 5 cross-user isolation
    tests can enforce per-user boundaries.

    ADR-025 Phase 4: return value is used by ``_flush_pending_tool_calls``
    to populate the ``tool_calls.interaction_id`` FK after the parent
    row lands.

    Migration 007 / ADR-033 seed: ``status`` defaults to ``"success"``;
    pass ``status="failed"`` with a ``failure_class`` when the upstream
    call timed out or errored so the failure is auditable alongside the
    successes.
    """
    try:
        pg_factory = get_postgres_factory()
        session_factory = pg_factory.get_session_factory()
        db = session_factory()
        try:
            record = InteractionRecord(
                project=project,
                source=source,
                question=question,
                answer=answer,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                timestamp=datetime.now().isoformat(),
                session_id=session_id,
                model=model,
                user_id=user_id,
                status=status,
                failure_class=failure_class,
                error_detail=error_detail,
                duration_ms=duration_ms,
            )
            db.add(record)
            db.commit()
            db.refresh(record)
            return record.id
        finally:
            db.close()
    except Exception as exc:  # pragma: no cover - defensive logging only
        logger.warning("Failed to persist interaction: %s", exc)
        return None


def _flush_pending_tool_calls(
    pending: list[PendingToolCall],
    interaction_id: int | None,
) -> None:
    """Write the accumulated ``ToolCall`` audit rows to Postgres.

    ADR-025 §Decision.5: every memory-tool invocation produces one row
    in the ``tool_calls`` table. The loop in ``_memory_tool_loop``
    accumulates ``PendingToolCall`` records during execution; this
    helper fills in the ``interaction_id`` FK and persists them after
    the parent ``InteractionRecord`` has landed.

    Best-effort semantics mirror ``_persist_interaction`` — a DB hiccup
    logs a warning instead of breaking the chat response. Partial
    success is acceptable: some rows land, some don't, rather than
    all-or-nothing rollback.
    """
    if not pending or interaction_id is None:
        return
    try:
        pg_factory = get_postgres_factory()
        session_factory = pg_factory.get_session_factory()
        db = session_factory()
        try:
            for rec in pending:
                db.add(
                    ToolCall(
                        interaction_id=interaction_id,
                        user_id=rec.user_id,
                        agent_type=rec.agent_type,
                        tool_name=rec.tool_name,
                        args=rec.args,
                        result_summary=rec.result_summary,
                        error=rec.error,
                        started_at=rec.started_at,
                        duration_ms=rec.duration_ms,
                        granted_scope=rec.granted_scope,
                    )
                )
            db.commit()
        finally:
            db.close()
    except Exception as exc:  # pragma: no cover - defensive logging only
        logger.warning(
            "Failed to persist %d tool_calls for interaction %s: %s",
            len(pending),
            interaction_id,
            exc,
        )


def _set_genai_request_attributes(
    payload: dict[str, Any], query: str, session_id: str, source: str, user_id: str
) -> None:
    """Tag the current span with gen_ai.* request attributes for Langfuse.

    DESIGN §15 Phase 2: ``langfuse.user.id`` now carries the Keycloak
    ``sub`` claim (the real identity) and ``sovereign.source`` keeps the
    agent string for observability.
    """
    messages = payload.get("messages") or []
    telemetry.set_current_span_attributes(
        {
            "gen_ai.system": "llama.cpp",
            "gen_ai.request.model": payload.get("model", ""),
            "gen_ai.request.temperature": payload.get("temperature"),
            "gen_ai.request.top_p": payload.get("top_p"),
            "gen_ai.request.max_tokens": payload.get("max_tokens") or 0,
            "gen_ai.request.streaming": bool(payload.get("stream")),
            # Langfuse-native attributes
            "langfuse.session.id": session_id,
            "langfuse.user.id": user_id,
            "input.value": json.dumps(messages, ensure_ascii=False)[:8000],
            "sovereign.memory.query": query,
            "sovereign.memory.project": payload.get("project") or "",
            "sovereign.source": source,
            "sovereign.user.id": user_id,
        }
    )


def _set_genai_response_attributes(response_json: dict[str, Any]) -> None:
    """Tag the current span with gen_ai.* response attributes from llama-server."""
    try:
        choices = response_json.get("choices") or []
        first = choices[0] if choices else {}
        message = first.get("message") or {}
        content = message.get("content") or ""
        finish_reason = first.get("finish_reason") or ""
        usage = response_json.get("usage") or {}
        telemetry.set_current_span_attributes(
            {
                "gen_ai.response.model": response_json.get("model", ""),
                "gen_ai.response.finish_reasons": finish_reason,
                "gen_ai.usage.input_tokens": usage.get("prompt_tokens", 0),
                "gen_ai.usage.output_tokens": usage.get("completion_tokens", 0),
                "output.value": content[:8000],
            }
        )
    except Exception:  # pragma: no cover
        pass


@log_call(logger=logger)
def _extract_query(payload: dict[str, Any]) -> str:
    """Extract the retrieval query from a raw chat request dict.

    Honours an explicit ``context_query`` field, otherwise falls back to the
    last user message. Tolerates OpenAI multi-part content arrays by joining
    their text parts.
    """
    cq = payload.get("context_query")
    if isinstance(cq, str) and cq:
        return cq
    for msg in reversed(payload.get("messages") or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
    return ""


@log_call(logger=logger)
def _merge_system_message(
    messages: list[dict[str, Any]], memory_context: str
) -> list[dict[str, Any]]:
    """Merge memory context into the system message. Preserves all other fields.

    Crucially, every message is shallow-copied with ``dict(m)`` so fields like
    ``tool_call_id``, ``name``, ``tool_calls``, and ``function_call`` survive
    the augmentation. Pre-ADR-024 code stripped these.
    """
    result = [dict(m) for m in messages]
    for m in result:
        if m.get("role") == "system":
            original = m.get("content", "") or ""
            m["content"] = (
                memory_context
                + "\n\n---\n\n"
                + "**IMPORTANT: The memory context above contains ADRs, skills, "
                + "and semantic search results retrieved from the memory-server. "
                + "Answer questions from this context FIRST before using external "
                + "tools like bash, curl, or file reads. Only use tools when the "
                + "answer is not in the context above.**"
                + "\n\n---\n\n## Agent Instructions\n"
                + original
            )
            return result
    # No system message — insert one at index 0
    result.insert(0, {"role": "system", "content": memory_context})
    return result


@_lf_observe(name="sovereign-chat-request")
def _build_memory_context_with_trace(
    context_builder: ContextBuilderService,
    payload: dict[str, Any],
    query: str,
    session_id: str,
    source: str,
    user_context: UserContext,
) -> tuple[str, str | None]:
    """Build memory context inside a Langfuse span; return (context, trace_id).

    Setting span attributes here works because the span IS active during this
    function's lifetime — unlike inside the streaming generator, which is
    consumed *after* the request handler has already returned its
    StreamingResponse object (at which point any context-manager span is
    closed). Capturing ``trace_id`` as a return value lets the post-stream
    code update the trace explicitly via the Langfuse ingestion API.

    DESIGN §15 Phase 2: ``user_context`` is threaded into the context
    builder so every layer can apply per-user scoping.
    """
    _set_genai_request_attributes(
        payload, query, session_id, source, user_context.user_id
    )
    memory_context = context_builder.build_system_context(
        user_context,
        project=payload.get("project"),
        query=query,
    )
    trace_id: str | None = None
    if _LANGFUSE_AVAILABLE:
        try:
            client = _lf_get_client()
            if client is not None:
                trace_id = client.get_current_trace_id()
        except Exception:  # pragma: no cover
            trace_id = None
    return memory_context, trace_id


async def _record_langfuse_output(
    trace_id: str | None,
    answer: str,
    prompt_tokens: int,
    completion_tokens: int,
    finish_reason: str | None,
    tool_calls: list[dict[str, Any]] | None,
    session_id: str | None,
    model: str | None,
) -> None:
    """Update a Langfuse trace with the LLM output via the ingestion API.

    Defensive — uses an explicit ``trace_id`` rather than relying on a span
    context manager being open. The streaming generator runs *after* the
    ``@observe`` span has already exited, so context-based updates would land
    on a closed observation and never appear in the UI.

    Async on purpose: a sync ``httpx.post`` here would block the event loop
    inside the streaming generator's tail and stall stream completion if
    Langfuse is slow. Both call sites are async so awaiting is free.
    """
    if not trace_id:
        return
    settings = get_settings()
    if not (
        settings.langfuse_host
        and settings.langfuse_public_key
        and settings.langfuse_secret_key
    ):
        return
    metadata: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "finish_reason": finish_reason or "unknown",
        "has_tool_calls": bool(tool_calls),
        "model_backend": model or "unknown",
    }
    if tool_calls:
        metadata["tool_calls"] = [
            {
                "name": (tc.get("function") or {}).get("name", ""),
                "id": tc.get("id", ""),
            }
            for tc in tool_calls
        ]
    body: dict[str, Any] = {
        "id": trace_id,
        "output": answer,
        "metadata": metadata,
    }
    if session_id:
        body["sessionId"] = session_id
    try:
        ingestion_url = settings.langfuse_host.rstrip("/") + "/api/public/ingestion"
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                ingestion_url,
                auth=(settings.langfuse_public_key, settings.langfuse_secret_key),
                json={
                    "batch": [
                        {
                            "id": f"output-{trace_id}",
                            "type": "trace-create",
                            "timestamp": datetime.now().isoformat() + "Z",
                            "body": body,
                        }
                    ]
                },
            )
    except Exception:  # pragma: no cover
        logger.debug("Langfuse ingestion update failed (non-fatal)", exc_info=True)


def _synthesize_sse_from_body(
    body: dict[str, Any], requested_model: str
) -> AsyncIterator[bytes]:
    """Produce an OpenAI-spec SSE stream from a non-streamed chat body.

    ADR-025 Phase 4: the tool-call loop is always non-streaming so the
    proxy can inspect ``tool_calls`` between iterations. When the
    caller asked for ``stream=true`` we synthesise the SSE bytes from
    the final body the loop returned — the content lands in one chunk
    rather than being word-by-word streamed, but the wire format is
    correct and OpenAI-compatible clients handle it uniformly.

    The emitted frames are:

      1. One data chunk with the full content (and tool_calls if any)
      2. One data chunk with the finish_reason
      3. A synthetic usage chunk (so clients that track token counts
         see the cost the loop incurred in aggregate)
      4. ``data: [DONE]``

    Returns an async iterator suitable for ``StreamingResponse``.
    """
    choices = body.get("choices") or []
    first = choices[0] if choices else {}
    message = first.get("message") or {}
    content = message.get("content") or ""
    tool_calls = message.get("tool_calls")
    finish_reason = first.get("finish_reason") or "stop"
    usage = body.get("usage") or {}
    model = body.get("model") or requested_model
    resp_id = body.get("id") or "chatcmpl-sovereign"
    created = body.get("created") or 0

    async def _iter() -> AsyncIterator[bytes]:
        # Content chunk
        delta: dict[str, Any] = {"content": content}
        if tool_calls:
            delta["tool_calls"] = tool_calls
        content_chunk = {
            "id": resp_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta}],
        }
        yield ("data: " + json.dumps(content_chunk) + "\n\n").encode()

        # Finish chunk
        finish_chunk: dict[str, Any] = {
            "id": resp_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        }
        yield ("data: " + json.dumps(finish_chunk) + "\n\n").encode()

        # Usage chunk (optional but helps clients that track cost)
        usage_chunk: dict[str, Any] = {
            "id": resp_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [],
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }
        yield ("data: " + json.dumps(usage_chunk) + "\n\n").encode()
        yield b"data: [DONE]\n\n"

    return _iter()


def _render_tool_calls_text(tool_calls_acc: dict[int, dict[str, Any]]) -> str:
    """Render accumulated tool_calls as ``[tool_call] name(args)`` lines."""
    lines: list[str] = []
    for idx in sorted(tool_calls_acc):
        tc = tool_calls_acc[idx]
        fn = tc.get("function") or {}
        args = fn.get("arguments") or ""
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        lines.append(f"[tool_call] {fn.get('name', '')}({args[:500]})")
    return "\n".join(lines)


@router.get("/models")
@log_call(logger=logger)
async def list_models(
    _auth: dict[str, Any] = Depends(require_scope("audittrace:query")),
) -> Any:
    """Proxy GET /v1/models to llama-server.

    OpenAI-compatible clients (OpenCode, Continue) call this to discover
    available models before sending chat completions.
    """
    settings = get_settings()
    models_url = settings.llama_url.rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(models_url)
            return response.json()
    except httpx.ConnectError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"llama-server unreachable at {models_url}",
        ) from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=504,
            detail=f"llama-server timeout at {models_url}",
        ) from exc


@_lf_observe(name="sovereign-chat-request", capture_output=False)
def _prepare_tools_mode_trace(
    payload: dict[str, Any],
    query: str,
    session_id: str,
    source: str,
    user_context: UserContext,
) -> tuple[list[dict[str, Any]], str | None]:
    """Set gen_ai.* request attributes on the active Langfuse span and
    return ``(tools_for_user, trace_id)``.

    Mirrors the inject-mode helper ``_build_memory_context_with_trace`` so
    tools mode gets a real parent trace visible in the Langfuse UI. We
    must set the request attributes *inside* the decorated function so
    they land on the span the decorator just opened — writing them from
    the caller would land on whichever span is active there, or on none.

    ``tools_for_user`` is returned because computing it here is cheap and
    the caller needs it to build the ambient context and augment the
    outbound tools array. Keeping the computation on the same call avoids
    asking the registry twice.

    ``capture_output=False`` on the decorator stops Langfuse from
    auto-capturing this helper's return value (the tools list + trace_id
    tuple) as the span's ``output``. The authoritative output for the
    span is the LLM reply, which ``_record_langfuse_output`` pushes via
    the ingestion API *after* the tool-call loop completes. Without this
    flag the tools list clobbers the real answer in the Langfuse UI.
    """
    _set_genai_request_attributes(
        payload, query, session_id, source, user_context.user_id
    )
    tools_for_user = tools_visible_to(user_context)
    trace_id: str | None = None
    if _LANGFUSE_AVAILABLE:
        try:
            client = _lf_get_client()
            if client is not None:
                trace_id = client.get_current_trace_id()
        except Exception:  # pragma: no cover
            trace_id = None
    return tools_for_user, trace_id


async def _handle_tools_mode(
    *,
    payload: dict[str, Any],
    user: UserContext,
    source: str,
    session_id: str,
    query: str,
    settings: Settings,
) -> Any:
    """ADR-025 ``memory_mode=tools`` path.

    1. Build the minimal ambient context (identity + project + date +
       tool hints) and merge it into the request's system message.
    2. Augment the ``tools`` array with the memory tools the caller is
       scoped for, alongside whatever tools the client already sent.
    3. Run the proxy-internal tool-call loop (non-streaming round-trips
       to llama-server until the model answers, a mixed/external tool
       call appears, or the iteration cap is hit).
    4. Persist the ``InteractionRecord`` and flush accumulated
       ``PendingToolCall`` audit rows linked to its id.
    5. Push the LLM output to Langfuse via the ingestion API using the
       ``trace_id`` captured from the ``@_lf_observe`` helper span.
    6. Return the final body as JSON, or synthesise an SSE stream from
       it when the caller asked for ``stream=true``.

    Per-tool-call child spans are intentionally not emitted here — that
    richer instrumentation is a Phase 5 follow-up for ADR-025. The parent
    chat observation with input/output + usage is enough to restore
    parity with inject mode in the Langfuse UI.
    """
    project = payload["project"]  # ADR-029: set by _resolve_project on entry
    requested_model = payload.get("model", "")
    is_stream = bool(payload.get("stream"))

    # 1 — Open the parent Langfuse span, capture trace_id, and compute
    # which memory tools the caller is scoped for. Runs in a worker
    # thread so the synchronous @_lf_observe decorator doesn't block the
    # event loop (same pattern as inject mode's context build).
    tools_for_user, trace_id = await asyncio.to_thread(
        _prepare_tools_mode_trace,
        payload,
        query,
        session_id,
        source,
        user,
    )

    ambient = build_ambient_context(user, project, tools_for_user)

    existing_tools = payload.get("tools") or []
    augmented_tools = list(existing_tools) + tools_for_user

    augmented_messages = _merge_system_message(payload["messages"], ambient)
    loop_payload = dict(payload)
    loop_payload["messages"] = augmented_messages
    loop_payload["tools"] = augmented_tools
    loop_payload["stream"] = False  # loop is always non-streaming internally

    llama_url = settings.llama_url.rstrip("/") + "/chat/completions"

    # 2 — Run the loop. On TimeoutException or unexpected Exception we
    # persist a failed-interaction audit row BEFORE propagating, so a
    # request that times out mid-loop still has a forensic DB trail.
    # Accumulated ``pending`` tool_calls from pre-failure iterations
    # are intentionally dropped — the tool_calls FK needs an
    # interaction_id and linking them to a failed parent is out of
    # scope for this fix (see migration 007 plan, "pending tool_calls
    # … deliberately NOT flushed").
    perf_start = time.perf_counter()
    try:
        final_body, pending = await run_memory_tool_loop(
            llama_url=llama_url,
            payload=loop_payload,
            user_context=user,
            session_id=session_id,
            max_iterations=settings.memory_tool_loop_max_iterations,
            timeout_seconds=settings.llama_chunk_timeout,
        )
    except httpx.TimeoutException as exc:
        _persist_interaction(
            project=project,
            source=source,
            question=query,
            answer="",
            prompt_tokens=0,
            completion_tokens=0,
            session_id=session_id,
            model=requested_model,
            user_id=user.user_id,
            status="failed",
            failure_class=FAILURE_PROXY_TIMEOUT,
            error_detail=str(exc)[:500],
            duration_ms=int((time.perf_counter() - perf_start) * 1000),
        )
        raise HTTPException(
            status_code=504,
            detail=(f"llama-server timeout after {settings.llama_chunk_timeout}s"),
        ) from exc
    except httpx.HTTPStatusError as exc:
        _persist_interaction(
            project=project,
            source=source,
            question=query,
            answer="",
            prompt_tokens=0,
            completion_tokens=0,
            session_id=session_id,
            model=requested_model,
            user_id=user.user_id,
            status="failed",
            failure_class=FAILURE_UPSTREAM_ERROR,
            error_detail=f"status={exc.response.status_code}: {str(exc)[:400]}",
            duration_ms=int((time.perf_counter() - perf_start) * 1000),
        )
        raise HTTPException(
            status_code=502,
            detail=f"llama-server returned {exc.response.status_code}",
        ) from exc
    except httpx.ConnectError as exc:
        _persist_interaction(
            project=project,
            source=source,
            question=query,
            answer="",
            prompt_tokens=0,
            completion_tokens=0,
            session_id=session_id,
            model=requested_model,
            user_id=user.user_id,
            status="failed",
            failure_class=FAILURE_UPSTREAM_UNREACHABLE,
            error_detail=str(exc)[:500],
            duration_ms=int((time.perf_counter() - perf_start) * 1000),
        )
        raise HTTPException(
            status_code=502,
            detail=f"llama-server unreachable at {llama_url}",
        ) from exc
    except Exception as exc:
        _persist_interaction(
            project=project,
            source=source,
            question=query,
            answer="",
            prompt_tokens=0,
            completion_tokens=0,
            session_id=session_id,
            model=requested_model,
            user_id=user.user_id,
            status="failed",
            failure_class=FAILURE_INTERNAL_ERROR,
            error_detail=str(exc)[:500],
            duration_ms=int((time.perf_counter() - perf_start) * 1000),
        )
        raise  # let the FastAPI global handler shape the 500

    # 3 — Extract answer text + usage for the interaction row
    choices = final_body.get("choices") or []
    first = choices[0] if choices else {}
    message = first.get("message") or {}
    answer_text = message.get("content") or ""
    final_tool_calls = message.get("tool_calls") or []
    if final_tool_calls:
        tc_acc = {i: tc for i, tc in enumerate(final_tool_calls)}
        answer_text = (
            answer_text + "\n" + _render_tool_calls_text(tc_acc)
            if answer_text
            else _render_tool_calls_text(tc_acc)
        )
    usage = final_body.get("usage") or {}
    finish_reason = first.get("finish_reason") or "stop"
    response_model = final_body.get("model") or requested_model

    # 4 — Persist interaction + flush audit rows
    interaction_id = _persist_interaction(
        project=project,
        source=source,
        question=query,
        answer=answer_text,
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        session_id=session_id,
        model=response_model,
        user_id=user.user_id,
        duration_ms=int((time.perf_counter() - perf_start) * 1000),
    )
    _flush_pending_tool_calls(pending, interaction_id)

    # 5 — Push the output to Langfuse. Best-effort: the helper swallows
    # its own exceptions so a Langfuse outage cannot break the chat
    # response. No-op when ``trace_id`` is None (Langfuse disabled).
    await _record_langfuse_output(
        trace_id=trace_id,
        answer=answer_text,
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        finish_reason=finish_reason,
        tool_calls=final_tool_calls or None,
        session_id=session_id,
        model=response_model,
    )

    # 6 — Return JSON or synthesised SSE
    if is_stream:
        return StreamingResponse(
            _synthesize_sse_from_body(final_body, requested_model),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return final_body


@router.post("/chat/completions")
async def chat_completions(
    http_request: Request,
    context_builder: ContextBuilderService = Depends(get_context_builder),
    _auth: dict[str, Any] = Depends(require_scope("audittrace:query")),
    user: UserContext = Depends(require_user),
) -> Any:
    """OpenAI-compatible chat completions with memory augmentation.

    Raw dict pass-through proxy: every field on the inbound request is
    forwarded to llama-server unchanged except ``messages``, which has the
    memory context merged into its system entry. This preserves
    ``tools``, ``tool_choice``, ``tool_calls``, ``tool_call_id``, ``name``,
    ``response_format``, ``parallel_tool_calls``, and any future OpenAI-spec
    field without code changes (ADR-024).

    No ``@log_call`` decorator on purpose — the streaming generator is
    consumed *after* this function returns, so a context-manager span here
    would close before the generator ran. The explicit ``@observe`` on
    ``_build_memory_context_with_trace`` owns the request span instead.
    """
    settings = get_settings()
    reset_langgraph_step()  # backlog #02: per-trace step counter
    try:
        payload: dict[str, Any] = await http_request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid JSON body: {exc}"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        raise HTTPException(status_code=422, detail="messages: list required")

    # ADR-029: tag every interaction with the caller's project. Precedence:
    # X-Project header → body.metadata.project → body.project → "default".
    # Header wins so an agent can carry one provider config per project and
    # drop the tag in cleanly via a single HTTP header.
    payload["project"] = _resolve_project(http_request, payload)

    # ADR-034: depth is a user-expressed knob. Parse X-Thinking header
    # and inject chat_template_kwargs.enable_thinking into the payload
    # BEFORE branching into inject/tools so both paths honour it.
    thinking = _resolve_thinking(http_request)
    _apply_thinking_mode(payload, thinking)

    query = _extract_query(payload)
    source = _detect_source(http_request)
    first_user = next(
        (
            m.get("content", "")
            for m in payload["messages"]
            if isinstance(m, dict)
            and m.get("role") == "user"
            and isinstance(m.get("content"), str)
        ),
        query or "",
    )
    session_id = _compute_session_id(source, first_user, user.user_id)

    # ADR-025 Phase 4: branch on memory_mode. The inject path (default)
    # runs the pre-Phase-4 4-layer context build; the tools path runs
    # the proxy-internal tool-call loop.
    if settings.memory_mode == "tools":
        return await _handle_tools_mode(
            payload=payload,
            user=user,
            source=source,
            session_id=session_id,
            query=query,
            settings=settings,
        )

    # Build memory context inside an @observe span (off-loop because the
    # context builder hits ChromaDB and disk synchronously) and capture an
    # explicit trace_id for the post-stream Langfuse update.
    memory_context, trace_id = await asyncio.to_thread(
        _build_memory_context_with_trace,
        context_builder,
        payload,
        query,
        session_id,
        source,
        user,
    )

    # Augment messages list — every other message field is preserved verbatim.
    augmented_messages = _merge_system_message(payload["messages"], memory_context)
    proxy_payload = dict(payload)  # shallow copy of top-level keys
    proxy_payload["messages"] = augmented_messages

    llama_url = settings.llama_url.rstrip("/") + "/chat/completions"
    project = payload["project"]  # ADR-029: set by _resolve_project on entry
    requested_model = payload.get("model", "")
    is_stream = bool(payload.get("stream"))

    # ─────────────────────────── Streaming branch ───────────────────────────
    if is_stream:

        async def _iter_and_capture() -> AsyncIterator[bytes]:
            state = _StreamState()
            perf_start = time.perf_counter()
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(
                        connect=10.0, read=None, write=30.0, pool=10.0
                    )
                ) as client:
                    async with client.stream(
                        "POST", llama_url, json=proxy_payload
                    ) as resp:
                        resp.raise_for_status()
                        async for line in _iter_with_idle_timeout(
                            resp,
                            settings.llama_chunk_timeout,
                            settings.sse_keepalive_interval,
                        ):
                            if line is None:
                                # ADR-034: SSE comment frame — invisible to
                                # JSON parsers, holds connection through proxies.
                                yield b": keep-alive\n\n"
                                continue
                            stripped = line.strip()
                            if not stripped.startswith("data: "):
                                # Forward blank/non-data lines verbatim (SSE separators)
                                yield (line + "\n").encode()
                                continue
                            payload_str = stripped[6:]
                            if payload_str == "[DONE]":
                                # Hold the marker — synthetic usage chunk goes first.
                                state.done_seen = True
                                continue
                            yield (line + "\n").encode()
                            try:
                                chunk = json.loads(payload_str)
                            except json.JSONDecodeError:
                                continue
                            _accumulate_chunk(state, chunk)
                # Inject synthetic usage chunk for OpenAI clients that need it.
                yield _build_synthetic_usage_chunk(state, requested_model)
                if state.done_seen:
                    yield b"data: [DONE]\n\n"
            except httpx.TimeoutException as exc:
                # Proxy timeout — the Qwen <think> loop took longer than
                # AUDITTRACE_LLAMA_PROXY_TIMEOUT. Emit a structured SSE
                # error frame so clients (OpenCode, curl) see the shape
                # of the failure, persist a failed-interaction audit
                # row, and let the stream close cleanly. See migration
                # 007 and the 2026-04-16 plan in
                # ~/.claude/plans/reflective-discovering-platypus.md.
                yield _emit_sse_error_frame(
                    FAILURE_PROXY_TIMEOUT,
                    f"llama-server idle timeout after {settings.llama_chunk_timeout}s",
                    504,
                )
                _persist_interaction(
                    project=project,
                    source=source,
                    question=query,
                    answer="",
                    prompt_tokens=0,
                    completion_tokens=0,
                    session_id=session_id,
                    model=requested_model,
                    user_id=user.user_id,
                    status="failed",
                    failure_class=FAILURE_PROXY_TIMEOUT,
                    error_detail=str(exc)[:500],
                    duration_ms=int((time.perf_counter() - perf_start) * 1000),
                )
                return
            except httpx.ConnectError as exc:
                yield _emit_sse_error_frame(
                    FAILURE_UPSTREAM_UNREACHABLE,
                    f"llama-server unreachable at {llama_url}",
                    502,
                )
                _persist_interaction(
                    project=project,
                    source=source,
                    question=query,
                    answer="",
                    prompt_tokens=0,
                    completion_tokens=0,
                    session_id=session_id,
                    model=requested_model,
                    user_id=user.user_id,
                    status="failed",
                    failure_class=FAILURE_UPSTREAM_UNREACHABLE,
                    error_detail=str(exc)[:500],
                    duration_ms=int((time.perf_counter() - perf_start) * 1000),
                )
                return
            except httpx.HTTPStatusError as exc:
                yield _emit_sse_error_frame(
                    FAILURE_UPSTREAM_ERROR,
                    f"llama-server returned status {exc.response.status_code}",
                    502,
                )
                _persist_interaction(
                    project=project,
                    source=source,
                    question=query,
                    answer="",
                    prompt_tokens=0,
                    completion_tokens=0,
                    session_id=session_id,
                    model=requested_model,
                    user_id=user.user_id,
                    status="failed",
                    failure_class=FAILURE_UPSTREAM_ERROR,
                    error_detail=f"status={exc.response.status_code}: {str(exc)[:400]}",
                    duration_ms=int((time.perf_counter() - perf_start) * 1000),
                )
                return
            except Exception as exc:
                logger.exception("streaming chat generator failed unexpectedly")
                yield _emit_sse_error_frame(
                    FAILURE_INTERNAL_ERROR,
                    "Internal error during streaming.",
                    500,
                )
                _persist_interaction(
                    project=project,
                    source=source,
                    question=query,
                    answer="",
                    prompt_tokens=0,
                    completion_tokens=0,
                    session_id=session_id,
                    model=requested_model,
                    user_id=user.user_id,
                    status="failed",
                    failure_class=FAILURE_INTERNAL_ERROR,
                    error_detail=str(exc)[:500],
                    duration_ms=int((time.perf_counter() - perf_start) * 1000),
                )
                return

            # Post-stream: build answer including tool_calls if present
            text_answer = "".join(state.chunks)
            if state.tool_calls_acc:
                tool_calls_text = _render_tool_calls_text(state.tool_calls_acc)
                answer = (
                    text_answer + "\n" + tool_calls_text
                    if text_answer
                    else tool_calls_text
                )
            else:
                answer = text_answer

            _persist_interaction(
                project=project,
                source=source,
                question=query,
                answer=answer,
                prompt_tokens=state.prompt_tokens,
                completion_tokens=state.completion_tokens,
                session_id=session_id,
                model=state.model or requested_model,
                user_id=user.user_id,
                duration_ms=int((time.perf_counter() - perf_start) * 1000),
            )
            await _record_langfuse_output(
                trace_id=trace_id,
                answer=answer,
                prompt_tokens=state.prompt_tokens,
                completion_tokens=state.completion_tokens,
                finish_reason=state.finish_reason,
                tool_calls=(
                    [state.tool_calls_acc[i] for i in sorted(state.tool_calls_acc)]
                    if state.tool_calls_acc
                    else None
                ),
                session_id=session_id,
                model=state.model or requested_model,
            )

        return StreamingResponse(
            _iter_and_capture(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ──────────────────────── Non-streaming branch ──────────────────────────
    ns_perf_start = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0, read=settings.llama_chunk_timeout, write=30.0, pool=10.0
            )
        ) as client:
            response = await client.post(llama_url, json=proxy_payload)
        body = response.json()
    except httpx.ConnectError as exc:
        _persist_interaction(
            project=project,
            source=source,
            question=query,
            answer="",
            prompt_tokens=0,
            completion_tokens=0,
            session_id=session_id,
            model=requested_model,
            user_id=user.user_id,
            status="failed",
            failure_class=FAILURE_UPSTREAM_UNREACHABLE,
            error_detail=str(exc)[:500],
            duration_ms=int((time.perf_counter() - ns_perf_start) * 1000),
        )
        raise HTTPException(
            status_code=502,
            detail=f"llama-server unreachable at {llama_url}",
        ) from exc
    except httpx.TimeoutException as exc:
        _persist_interaction(
            project=project,
            source=source,
            question=query,
            answer="",
            prompt_tokens=0,
            completion_tokens=0,
            session_id=session_id,
            model=requested_model,
            user_id=user.user_id,
            status="failed",
            failure_class=FAILURE_PROXY_TIMEOUT,
            error_detail=str(exc)[:500],
            duration_ms=int((time.perf_counter() - ns_perf_start) * 1000),
        )
        raise HTTPException(
            status_code=504,
            detail=f"llama-server timeout after {settings.llama_chunk_timeout}s",
        ) from exc

    _set_genai_response_attributes(body)
    try:
        choices = body.get("choices") or []
        message_obj = (choices[0].get("message") or {}) if choices else {}
        answer = message_obj.get("content") or ""
        ns_tool_calls = message_obj.get("tool_calls") or []
        if ns_tool_calls:
            # Reuse the same renderer as the streaming branch by indexing into
            # the same shape (function/{name,arguments}).
            tc_acc = {i: tc for i, tc in enumerate(ns_tool_calls)}
            tool_text = _render_tool_calls_text(tc_acc)
            answer = (answer + "\n" + tool_text) if answer else tool_text
        usage = body.get("usage") or {}
        finish_reason = (choices[0].get("finish_reason") if choices else None) or "stop"
        _persist_interaction(
            project=project,
            source=source,
            question=query,
            answer=answer,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            session_id=session_id,
            model=body.get("model") or requested_model,
            user_id=user.user_id,
            duration_ms=int((time.perf_counter() - ns_perf_start) * 1000),
        )
        await _record_langfuse_output(
            trace_id=trace_id,
            answer=answer,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            finish_reason=finish_reason,
            tool_calls=ns_tool_calls or None,
            session_id=session_id,
            model=body.get("model") or requested_model,
        )
    except Exception:  # pragma: no cover - capture is best-effort
        logger.exception("post-response capture failed")
    return body
