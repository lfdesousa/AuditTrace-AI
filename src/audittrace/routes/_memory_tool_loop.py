"""Proxy-internal memory tool-call loop (ADR-025 §Decision.2).

This is the heart of ``memory_mode=tools``. It sits in front of
``llama-server`` and owns the "model decides → proxy executes memory
tools → re-call model" round-trip invisible to the agentic client.

Every iteration is a **non-streaming** POST to llama-server because the
proxy must inspect ``tool_calls`` before deciding what to do next:

1. POST the current messages + augmented tools array
2. Inspect the response's ``tool_calls``:
   - None → final answer, return the body
   - All memory tools → execute them, append tool_result messages, loop
   - Any external (non-memory) tool → return the body unchanged so the
     agentic client handles it (the proxy cannot execute ``bash``)
3. Bounded by a hard iteration cap from
   ``settings.memory_tool_loop_max_iterations`` — a misbehaving model
   that emits memory tool calls every turn is stopped at the cap and
   a WARNING is logged.

``PendingToolCall`` records are accumulated during the loop and
returned to the caller (the ``chat.py`` handler). The handler writes
them to the ``tool_calls`` table **after** the parent ``InteractionRecord``
lands so the FK constraint is satisfied.

Cache hits skip the pending audit row entirely (ADR-025 §Decision.8) —
a cache hit represents zero side effects on the memory layers and we
already audited the real execution when the cache was populated.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from opentelemetry import trace

from audittrace.identity import UserContext
from audittrace.tools import get_tool_by_name, invoke_tool

logger = logging.getLogger(__name__)

# Tracer used for per-tool child spans (ADR-025 Phase 5 / commit 2.3).
# One explicit span per memory_tool invocation so Langfuse renders
# input/output for every recall_* call, and service-graph shows
# Postgres/Chroma/MinIO traffic attributed to the tool that caused it.
_tracer = trace.get_tracer(__name__)

# Attribute payloads are capped to keep Tempo/Langfuse storage bounded
# without hiding the structure of either end of the call.
_SPAN_ATTR_CAP = 4000


# ──────────────────────────── PendingToolCall ───────────────────────────────


@dataclass
class PendingToolCall:
    """One in-flight tool-call audit record, awaiting an ``interaction_id``.

    The loop can't write a ``ToolCall`` row directly because the FK to
    ``interactions.id`` requires the parent ``InteractionRecord`` to exist
    first, and the parent row is persisted by the chat handler *after*
    the loop returns. The handler is responsible for flushing these
    records to Postgres once it has the interaction id in hand.
    """

    tool_name: str
    user_id: str
    agent_type: str
    args: str  # JSON-serialised
    result_summary: str | None
    error: str | None
    started_at: datetime
    duration_ms: int | None
    granted_scope: str
    metadata: dict[str, Any] = field(default_factory=dict)


# ─────────────────────── Tool-call extraction helpers ──────────────────────


def _extract_tool_calls(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the ``tool_calls`` list from an OpenAI chat.completion body.

    Returns ``[]`` when the response has no tool calls (final answer).
    Tolerates missing fields so malformed responses don't crash the loop.
    """
    choices = body.get("choices") or []
    if not choices:
        return []
    message = choices[0].get("message") or {}
    tool_calls = message.get("tool_calls") or []
    return [tc for tc in tool_calls if isinstance(tc, dict)]


def _extract_assistant_message(body: dict[str, Any]) -> dict[str, Any]:
    """Return the assistant message dict verbatim so it can be appended
    to the conversation history before we send the tool_result message.
    The second llama-server call must see the exact same tool_calls block
    the first response produced, otherwise the tool_call_id correlation
    breaks."""
    choices = body.get("choices") or []
    if not choices:
        return {"role": "assistant", "content": ""}
    message = choices[0].get("message") or {}
    return dict(message)


def _split_tool_calls_by_type(
    tool_calls: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition tool calls into (memory, external). A memory tool is one
    whose name resolves via the registry; everything else is external."""
    memory: list[dict[str, Any]] = []
    external: list[dict[str, Any]] = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        name = fn.get("name", "")
        if get_tool_by_name(name) is not None:
            memory.append(tc)
        else:
            external.append(tc)
    return memory, external


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """Coerce an OpenAI tool_call ``function.arguments`` field to a dict.

    OpenAI's spec says arguments is a JSON string, but some clients
    forward them as a dict already. Also tolerate empty strings and
    malformed JSON so a client quirk does not crash the loop.
    """
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _tool_call_signatures(
    tool_calls: list[dict[str, Any]],
) -> frozenset[tuple[str, str]]:
    """Stable ``{(tool_name, args_json)}`` set for repeat detection.

    Used to decide whether this iteration is asking for the same thing
    the previous iteration asked for. Args are serialised with
    ``sort_keys=True`` so semantically equal arg dicts compare equal
    regardless of key order. Returns a ``frozenset`` so set equality is
    order-insensitive across the iteration's calls.
    """
    sigs: set[tuple[str, str]] = set()
    for tc in tool_calls:
        fn = tc.get("function") or {}
        name = fn.get("name", "")
        args = _parse_arguments(fn.get("arguments"))
        sigs.add((name, json.dumps(args, sort_keys=True)))
    return frozenset(sigs)


# ───────────────────────────── The loop ─────────────────────────────────────


async def run_memory_tool_loop(
    *,
    llama_url: str,
    payload: dict[str, Any],
    user_context: UserContext,
    session_id: str,
    max_iterations: int,
    timeout_seconds: int = 120,
) -> tuple[dict[str, Any], list[PendingToolCall]]:
    """Run the proxy-internal tool-call loop and return the final body.

    ``payload`` must already carry the augmented ``tools`` array
    (memory tools + whatever the client sent) and the ambient-context
    system message. The loop replaces ``stream=True`` with ``False``
    for its own POSTs because it needs to inspect every tool_calls
    block; the caller is responsible for rendering the final body as
    SSE if the original request asked for streaming.

    Returns ``(final_body, pending_audit_rows)``. The caller flushes
    ``pending_audit_rows`` after ``_persist_interaction`` returns the
    parent interaction id so the FK constraint is satisfied.
    """
    # Shallow copy top-level so we don't mutate the caller's dict.
    proxy_payload = dict(payload)
    proxy_payload["stream"] = False  # the loop itself is always non-streaming
    # Messages: mutable working copy so we can append tool_result entries.
    messages = list(proxy_payload.get("messages") or [])

    pending: list[PendingToolCall] = []
    last_body: dict[str, Any] = {}
    # ADR-030: signatures of the PREVIOUS iteration's memory tool calls.
    # When the current iteration's signatures equal this set, the model
    # is asking for the same thing again — no new information possible.
    # Exit early to save the remaining llama round-trips.
    last_sigs: frozenset[tuple[str, str]] | None = None

    for iteration in range(max_iterations):
        proxy_payload["messages"] = messages
        # Wrap the outbound LLM round-trip in a Langfuse-recognised
        # generation span so the trace UI shows the call as an LLM
        # generation (model, prompt, completion) and not a bare HTTP
        # POST. Nested inside the HTTPXClientInstrumentor span so the
        # Tempo service-graph edge (peer.service=qwen-chat-llm) is
        # preserved.
        with _tracer.start_as_current_span("llm.chat.completions") as gen_span:
            gen_span.set_attribute("langfuse.observation.type", "generation")
            gen_span.set_attribute("openinference.span.kind", "LLM")
            gen_span.set_attribute(
                "gen_ai.request.model", proxy_payload.get("model", "")
            )
            gen_span.set_attribute("langfuse.user.id", user_context.user_id)
            gen_span.set_attribute("user.id", user_context.user_id)
            gen_span.set_attribute("gen_ai.iteration", iteration)
            try:
                gen_span.set_attribute(
                    "input.value",
                    json.dumps({"messages": messages[-10:]}, ensure_ascii=False)[
                        :_SPAN_ATTR_CAP
                    ],
                )
            except Exception:  # pragma: no cover - defensive serialisation
                pass
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=10.0, read=timeout_seconds, write=30.0, pool=10.0
                )
            ) as client:
                response = await client.post(llama_url, json=proxy_payload)
            last_body = response.json()
            try:
                usage = last_body.get("usage", {}) or {}
                gen_span.set_attribute(
                    "gen_ai.usage.prompt_tokens",
                    int(usage.get("prompt_tokens") or 0),
                )
                gen_span.set_attribute(
                    "gen_ai.usage.completion_tokens",
                    int(usage.get("completion_tokens") or 0),
                )
                gen_span.set_attribute(
                    "gen_ai.response.model",
                    str(last_body.get("model", "") or ""),
                )
                choice_msg = (last_body.get("choices") or [{}])[0].get("message") or {}
                completion = choice_msg.get("content") or ""
                if completion:
                    gen_span.set_attribute(
                        "gen_ai.response.completion",
                        str(completion)[:_SPAN_ATTR_CAP],
                    )
                    gen_span.set_attribute(
                        "output.value", str(completion)[:_SPAN_ATTR_CAP]
                    )
                else:
                    # Tool-calling turn — no content, but record that
                    # the response pivoted to tool_calls so the span
                    # isn't flagged "empty generation".
                    tc_list = choice_msg.get("tool_calls") or []
                    if tc_list:
                        gen_span.set_attribute(
                            "output.value",
                            f"<tool_calls x{len(tc_list)}>",
                        )
            except Exception:  # pragma: no cover - defensive serialisation
                pass

        tool_calls = _extract_tool_calls(last_body)
        if not tool_calls:
            # Final answer — no tool_calls at all.
            return last_body, pending

        memory_calls, external_calls = _split_tool_calls_by_type(tool_calls)
        if external_calls:
            # Model wants an external tool (bash, edit_file, …). Stream
            # the response back to the client so the client executes it;
            # any memory tool calls in the same message are NOT executed
            # (the model will re-emit them next turn if it still needs
            # them — keeping the semantics with the client clean).
            return last_body, pending

        # ADR-030 early exit: if this iteration's memory tool calls are
        # exactly the previous iteration's, the model is stuck repeating
        # itself. Executing again can only return the same tool_results
        # (cache-backed), so we short-circuit and let the caller render
        # whatever partial content has accumulated.
        this_sigs = _tool_call_signatures(memory_calls)
        if last_sigs is not None and this_sigs == last_sigs:
            logger.info(
                "memory tool-call loop detected repeated signatures at "
                "iteration %d — exiting early (saved %d iterations)",
                iteration + 1,
                max_iterations - iteration - 1,
            )
            return last_body, pending
        last_sigs = this_sigs

        # All memory tools — execute each, append tool_result messages,
        # and loop for the next iteration.
        assistant_msg = _extract_assistant_message(last_body)
        messages.append(assistant_msg)

        for tc in memory_calls:
            await _execute_memory_tool(
                tc=tc,
                user_context=user_context,
                session_id=session_id,
                messages=messages,
                pending=pending,
            )

    # Iteration cap reached — a misbehaving model, or a deeply-chained
    # memory query. Return whatever the last body was so the caller
    # can still render something; a cap-hit is not an error state.
    logger.warning(
        "memory tool-call loop reached max iterations (%d) — returning "
        "accumulated body with %d pending tool calls",
        max_iterations,
        len(pending),
    )
    return last_body, pending


async def _execute_memory_tool(
    *,
    tc: dict[str, Any],
    user_context: UserContext,
    session_id: str,
    messages: list[dict[str, Any]],
    pending: list[PendingToolCall],
) -> None:
    """Dispatch one memory tool_call, append the tool_result message, and
    record a pending audit row unless the call was a cache hit.

    This function owns the scope-defensive check: even though
    ``tools_visible_to`` already filtered at advertisement time, we
    re-check the scope here so a stale tool_calls message from a prior
    conversation (when the user had more scopes) cannot bypass the
    filter after a scope revocation.
    """
    started = datetime.now()
    perf_start = time.perf_counter()

    fn = tc.get("function") or {}
    tool_name = fn.get("name", "")
    raw_args = fn.get("arguments")
    args = _parse_arguments(raw_args)
    call_id = tc.get("id", "")

    span_name = f"memory_tool.{tool_name}" if tool_name else "memory_tool.unknown"
    with _tracer.start_as_current_span(span_name) as span:
        # Langfuse-native + OTel-semconv user tagging so every child span
        # is filterable by user.id in Tempo and appears under the user in
        # Langfuse — completes the Phase-2 reconstructibility chain.
        span.set_attribute("langfuse.user.id", user_context.user_id)
        span.set_attribute("user.id", user_context.user_id)
        span.set_attribute("tool.name", tool_name)
        span.set_attribute("tool.call_id", call_id)
        span.set_attribute(
            "input.value", json.dumps(args, ensure_ascii=False)[:_SPAN_ATTR_CAP]
        )

        tool = get_tool_by_name(tool_name)
        if tool is None:
            # Shouldn't happen — the caller partitioned by registry lookup
            # already — but treat it as an error-shaped tool_result anyway.
            err = f"unknown memory tool: {tool_name}"
            span.set_attribute("output.value", json.dumps({"error": err}))
            span.set_status(trace.StatusCode.ERROR, err)
            _append_tool_result(messages, call_id, tool_name, {"error": err})
            pending.append(
                PendingToolCall(
                    tool_name=tool_name,
                    user_id=user_context.user_id,
                    agent_type=user_context.agent_type,
                    args=json.dumps(args),
                    result_summary=None,
                    error=err,
                    started_at=started,
                    duration_ms=int((time.perf_counter() - perf_start) * 1000),
                    granted_scope="",
                )
            )
            return

        # Defensive scope re-check. Admins bypass; non-admins must carry
        # the tool's required_scope.
        if not user_context.is_admin and tool.required_scope not in user_context.scopes:
            err = (
                f"scope denied: {tool.required_scope} not in user scopes "
                f"for tool {tool_name}"
            )
            span.set_attribute("output.value", json.dumps({"error": err}))
            span.set_status(trace.StatusCode.ERROR, err)
            _append_tool_result(messages, call_id, tool_name, {"error": err})
            pending.append(
                PendingToolCall(
                    tool_name=tool_name,
                    user_id=user_context.user_id,
                    agent_type=user_context.agent_type,
                    args=json.dumps(args),
                    result_summary=None,
                    error=err,
                    started_at=started,
                    duration_ms=int((time.perf_counter() - perf_start) * 1000),
                    granted_scope=tool.required_scope,
                )
            )
            return

        # Dispatch through the cache-aware helper. was_cache_hit tells us
        # whether to record an audit row (hits skip — ADR-025 §Decision.8).
        result, was_cache_hit = await invoke_tool(user_context, tool, args, session_id)
        _append_tool_result(messages, call_id, tool_name, result)

        span.set_attribute("tool.cache_hit", was_cache_hit)
        span.set_attribute(
            "output.value", json.dumps(result, ensure_ascii=False)[:_SPAN_ATTR_CAP]
        )
        duration_ms = int((time.perf_counter() - perf_start) * 1000)
        span.set_attribute("sovereign.tool.duration_ms", duration_ms)

        if was_cache_hit:
            logger.debug(
                "memory tool cache HIT tool=%s session=%s — skipping audit row",
                tool_name,
                session_id,
            )
            return

        error_text: str | None = None
        summary: str | None = None
        if "error" in result:
            error_text = str(result.get("error"))
            span.set_status(trace.StatusCode.ERROR, error_text)
        else:
            # Truncated JSON summary for the audit row so the column stays
            # bounded even on huge result sets.
            summary = json.dumps(result)[:1000]

        pending.append(
            PendingToolCall(
                tool_name=tool_name,
                user_id=user_context.user_id,
                agent_type=user_context.agent_type,
                args=json.dumps(args),
                result_summary=summary,
                error=error_text,
                started_at=started,
                duration_ms=duration_ms,
                granted_scope=tool.required_scope,
            )
        )


def _append_tool_result(
    messages: list[dict[str, Any]],
    tool_call_id: str,
    tool_name: str,
    result: dict[str, Any],
) -> None:
    """Append an OpenAI-spec tool_result message to the conversation.

    Content is JSON-serialised so the LLM sees a structured string
    matching what it would see from any other tool in its ecosystem.
    """
    messages.append(
        {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": json.dumps(result),
        }
    )
