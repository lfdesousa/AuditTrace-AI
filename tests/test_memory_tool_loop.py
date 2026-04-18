"""Tests for the proxy-internal memory tool-call loop (ADR-025 §Decision.2).

The loop is the heart of ``memory_mode=tools``. It sits in front of
``llama-server`` and owns the "model decides → proxy executes memory tools
→ re-call model" round-trip. Every iteration is a non-streaming POST to
llama-server because the proxy must inspect ``tool_calls`` before deciding
what to do next.

Loop exit conditions:

  1. Response has NO ``tool_calls``           → done, final body returned
  2. Response has tool_calls that are ALL     → execute, append tool_result
     memory tools                               messages, loop again
  3. Response has ANY non-memory tool_call    → done, body returned so the
                                                 client handles externals
  4. Iteration cap reached                    → done, WARNING logged, body
                                                 returned so the caller can
                                                 decide what to render

Pending ``ToolCall`` audit records are accumulated during the loop and
returned to the caller so the chat handler can flush them to Postgres
after the parent ``InteractionRecord`` lands (FK constraint).

Cache hits skip the pending audit row entirely — ADR-025 §Decision.8.
"""

from __future__ import annotations

import fakeredis
import pytest

# Side-effect import — decorators populate MEMORY_TOOL_REGISTRY.
import audittrace.tools.memory_handlers  # noqa: F401
from audittrace import dependencies
from audittrace.dependencies import create_test_container
from audittrace.identity import sentinel_user_context
from audittrace.routes._memory_tool_loop import (
    PendingToolCall,
    run_memory_tool_loop,
)
from audittrace.tools import reset_registry_for_tests
from audittrace.tools.cache import (
    ToolResultCache,
    reset_tool_result_cache,
    set_tool_result_cache,
)

# Reuse the AsyncClient fake from the chat-proxy tests so this file does not
# duplicate the mock plumbing.
from tests.test_chat_proxy import _FakeAsyncClient, _patch_async_client


@pytest.fixture(autouse=True)
def _fresh_registry_with_handlers():
    """Reset and re-import the handlers so each test starts with a clean
    registry populated by the four real decorators."""
    reset_registry_for_tests()
    import importlib

    import audittrace.tools.memory_handlers as handlers_mod

    importlib.reload(handlers_mod)
    yield
    reset_registry_for_tests()


@pytest.fixture
def _populated_container():
    c = create_test_container()
    c._instances["episodic"].add_document(
        "KV cache compression reduces memory by 75%",
        title="ADR-009",
        file="ADR-009.md",
    )
    c._instances["procedural"].add_document(
        "OAuth2 OIDC JWT validation patterns",
        skill="IAM",
        file="SKILL-IAM.md",
    )
    c._instances["semantic"].add_document(
        "RAG body about cache compression",
        source="ADR-009",
        collection="decisions",
    )
    prior = dependencies.container
    dependencies.container = c
    yield c
    dependencies.container = prior


@pytest.fixture
def _fakeredis_cache():
    client = fakeredis.FakeRedis(decode_responses=True)
    cache = ToolResultCache(client, default_ttl_seconds=900)
    set_tool_result_cache(cache)
    yield cache
    reset_tool_result_cache()


# ───────────────────────── response-shape helpers ───────────────────────────
# The loop consumes llama.cpp non-streaming response bodies. The shape is
# OpenAI-compatible; we build it inline for each test rather than importing
# a fixture so each case stays self-contained.


def _text_response(text: str, model: str = "qwen3.5-35b") -> dict:
    return {
        "id": "cmpl-test",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _tool_call_response(
    tool_name: str, args_json: str, call_id: str = "call_abc"
) -> dict:
    return {
        "id": "cmpl-test",
        "object": "chat.completion",
        "model": "qwen3.5-35b",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": args_json,
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class _SequencedClient(_FakeAsyncClient):
    """``_FakeAsyncClient`` subclass that returns a different JSON body on
    each successive POST. The first call gets ``responses[0]``, the second
    gets ``responses[1]``, etc."""

    def __init__(self, responses: list[dict]):
        super().__init__()
        self._responses = responses
        self._i = 0

    async def post(self, url, json=None, **kwargs):
        self.last_post_url = url
        self.last_post_json = json
        self.post_calls.append({"url": url, "json": json, "kwargs": kwargs})
        body = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        from unittest.mock import MagicMock

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = body
        return resp


# ─────────────────────────── Loop behaviour ─────────────────────────────────


class TestLoopExitConditions:
    @pytest.mark.asyncio
    async def test_zero_tool_calls_exits_immediately(
        self, _populated_container, _fakeredis_cache
    ):
        """Trivial prompt: llama.cpp answers in one shot with no tool_calls.
        Exactly ONE POST to llama-server, zero audit rows."""
        user = sentinel_user_context()
        fake = _SequencedClient([_text_response("Hello!")])
        with _patch_async_client(fake):
            final_body, pending = await run_memory_tool_loop(
                llama_url="http://llama/chat/completions",
                payload={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "hi"}],
                    "tools": [],
                },
                user_context=user,
                session_id="sess-1",
                max_iterations=5,
            )
        assert len(fake.post_calls) == 1
        assert final_body["choices"][0]["message"]["content"] == "Hello!"
        assert pending == []

    @pytest.mark.asyncio
    async def test_one_memory_tool_then_text(
        self, _populated_container, _fakeredis_cache
    ):
        """Model calls recall_decisions, proxy dispatches, model answers
        on the second iteration. TWO POSTs total. ONE pending audit row."""
        user = sentinel_user_context()
        fake = _SequencedClient(
            [
                _tool_call_response(
                    "recall_decisions", '{"query": "cache compression"}'
                ),
                _text_response("Based on ADR-009, compression saves 75%."),
            ]
        )
        with _patch_async_client(fake):
            final_body, pending = await run_memory_tool_loop(
                llama_url="http://llama/chat/completions",
                payload={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "what about cache?"}],
                    "tools": [],
                },
                user_context=user,
                session_id="sess-1",
                max_iterations=5,
            )
        assert len(fake.post_calls) == 2
        # Final body is the second iteration's text answer.
        assert "ADR-009" in final_body["choices"][0]["message"]["content"]
        # Exactly one audit row — the recall_decisions execution.
        assert len(pending) == 1
        assert pending[0].tool_name == "recall_decisions"
        assert pending[0].error is None
        # The second POST must include the tool_result in its messages so
        # the model sees what recall_decisions returned.
        second_payload = fake.post_calls[1]["json"]
        tool_msgs = [m for m in second_payload["messages"] if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "call_abc"

    @pytest.mark.asyncio
    async def test_external_tool_call_breaks_loop(
        self, _populated_container, _fakeredis_cache
    ):
        """The model wants to call `bash` — the proxy doesn't know how to
        execute that and must stream the response back so the client
        handles it. Exactly ONE POST; the body passes through untouched."""
        user = sentinel_user_context()
        bash_response = {
            "id": "cmpl-test",
            "object": "chat.completion",
            "model": "qwen3.5-35b",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_bash_1",
                                "type": "function",
                                "function": {
                                    "name": "bash",
                                    "arguments": '{"cmd": "ls /tmp"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        fake = _SequencedClient([bash_response])
        with _patch_async_client(fake):
            final_body, pending = await run_memory_tool_loop(
                llama_url="http://llama/chat/completions",
                payload={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "ls /tmp"}],
                    "tools": [],
                },
                user_context=user,
                session_id="sess-1",
                max_iterations=5,
            )
        assert len(fake.post_calls) == 1
        assert final_body == bash_response
        assert pending == []  # no memory tools executed

    @pytest.mark.asyncio
    async def test_mixed_memory_and_external_breaks_loop(
        self, _populated_container, _fakeredis_cache
    ):
        """If llama.cpp returns both a memory tool call AND a bash call in
        the same message, the proxy must not execute half of them — it
        streams back untouched so the client gets the whole picture."""
        user = sentinel_user_context()
        mixed_response = {
            "id": "cmpl-test",
            "object": "chat.completion",
            "model": "qwen3.5-35b",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_mem",
                                "type": "function",
                                "function": {
                                    "name": "recall_decisions",
                                    "arguments": '{"query": "cache"}',
                                },
                            },
                            {
                                "id": "call_bash",
                                "type": "function",
                                "function": {
                                    "name": "bash",
                                    "arguments": '{"cmd": "ls"}',
                                },
                            },
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        fake = _SequencedClient([mixed_response])
        with _patch_async_client(fake):
            final_body, pending = await run_memory_tool_loop(
                llama_url="http://llama/chat/completions",
                payload={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "cache?"}],
                    "tools": [],
                },
                user_context=user,
                session_id="sess-1",
                max_iterations=5,
            )
        assert len(fake.post_calls) == 1
        assert final_body == mixed_response
        # Neither tool was executed — the client will re-emit next turn.
        assert pending == []

    @pytest.mark.asyncio
    async def test_iteration_cap_hit(
        self, _populated_container, _fakeredis_cache, monkeypatch
    ):
        """A misbehaving model that emits *distinct* memory tool calls
        every turn must still be stopped by the hard iteration cap. The
        ADR-030 early-exit only fires on repeated signatures; the cap is
        the safety net when each iteration asks something different."""
        user = sentinel_user_context()
        # Each iteration uses a different query so the repeat-detection
        # cannot fire — only the hard cap can stop this loop.
        responses = [
            _tool_call_response(
                "recall_decisions",
                f'{{"query": "cache-variant-{i}"}}',
                call_id=f"call_{i}",
            )
            for i in range(10)
        ]
        fake = _SequencedClient(responses)

        from audittrace.routes import _memory_tool_loop

        warnings: list[str] = []
        monkeypatch.setattr(
            _memory_tool_loop.logger,
            "warning",
            lambda msg, *a, **k: warnings.append(msg % a if a else msg),
        )

        with _patch_async_client(fake):
            final_body, pending = await run_memory_tool_loop(
                llama_url="http://llama/chat/completions",
                payload={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "cache?"}],
                    "tools": [],
                },
                user_context=user,
                session_id="sess-1",
                max_iterations=3,
            )
        # 3 iterations → 3 POSTs to llama-server.
        assert len(fake.post_calls) == 3
        # All 3 iterations executed distinct tool calls (cache misses) so
        # each produced a pending audit row.
        assert len(pending) == 3
        # Final body is the 3rd response (still tool_calls — we gave up)
        assert final_body["choices"][0]["finish_reason"] == "tool_calls"
        # And a cap-hit warning was logged
        assert any("max iterations" in w.lower() for w in warnings), warnings

    @pytest.mark.asyncio
    async def test_early_exit_on_repeated_signatures(
        self, _populated_container, _fakeredis_cache, monkeypatch
    ):
        """ADR-030: if two consecutive iterations emit identical memory
        tool calls, the loop exits early — executing again can only
        return the same cached result, so there is no new information
        to feed the model. We save the remaining llama round-trips."""
        user = sentinel_user_context()
        # Every iteration asks for the same thing — the ADR-030 exit
        # should fire after iteration 2 (first iteration sets the
        # baseline, second detects the repeat).
        responses = [
            _tool_call_response(
                "recall_decisions",
                '{"query": "cache"}',
                call_id=f"call_{i}",
            )
            for i in range(10)
        ]
        fake = _SequencedClient(responses)

        from audittrace.routes import _memory_tool_loop

        infos: list[str] = []
        monkeypatch.setattr(
            _memory_tool_loop.logger,
            "info",
            lambda msg, *a, **k: infos.append(msg % a if a else msg),
        )

        with _patch_async_client(fake):
            final_body, pending = await run_memory_tool_loop(
                llama_url="http://llama/chat/completions",
                payload={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "cache?"}],
                    "tools": [],
                },
                user_context=user,
                session_id="sess-1",
                max_iterations=5,
            )
        # Exactly 2 llama POSTs: the baseline + the detected repeat.
        assert len(fake.post_calls) == 2
        # One pending audit row from iteration 1 (cache miss); iteration 2
        # exited before executing, so no second row. ADR-030 §3.
        assert len(pending) == 1
        # Final body still carries the repeated tool_calls — the caller
        # will render whatever partial content is there.
        assert final_body["choices"][0]["finish_reason"] == "tool_calls"
        # And an info log recorded the early exit.
        assert any("repeated signatures" in msg.lower() for msg in infos), infos

    @pytest.mark.asyncio
    async def test_different_tools_do_not_trigger_early_exit(
        self, _populated_container, _fakeredis_cache
    ):
        """Two different tools across iterations are a legitimate
        multi-tool progression, not a repeat. The loop must keep going."""
        user = sentinel_user_context()
        responses = [
            _tool_call_response(
                "recall_decisions", '{"query": "cache"}', call_id="call_1"
            ),
            _tool_call_response("recall_skills", '{"query": "IAM"}', call_id="call_2"),
            _text_response("done"),
        ]
        fake = _SequencedClient(responses)

        with _patch_async_client(fake):
            final_body, pending = await run_memory_tool_loop(
                llama_url="http://llama/chat/completions",
                payload={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "both?"}],
                    "tools": [],
                },
                user_context=user,
                session_id="sess-multitool",
                max_iterations=5,
            )
        # All 3 responses consumed — the varied tool calls must not
        # trigger the early-exit heuristic.
        assert len(fake.post_calls) == 3
        assert final_body["choices"][0]["message"]["content"] == "done"
        assert len(pending) == 2


# ─────────────────────── Audit row bookkeeping ──────────────────────────────


class TestPendingAuditRows:
    @pytest.mark.asyncio
    async def test_successful_handler_produces_pending_record(
        self, _populated_container, _fakeredis_cache
    ):
        user = sentinel_user_context()
        fake = _SequencedClient(
            [
                _tool_call_response(
                    "recall_decisions", '{"query": "cache"}', call_id="call_1"
                ),
                _text_response("done"),
            ]
        )
        with _patch_async_client(fake):
            _, pending = await run_memory_tool_loop(
                llama_url="http://llama/chat/completions",
                payload={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "q"}],
                    "tools": [],
                },
                user_context=user,
                session_id="sess-1",
                max_iterations=5,
            )
        assert len(pending) == 1
        rec = pending[0]
        assert isinstance(rec, PendingToolCall)
        assert rec.tool_name == "recall_decisions"
        assert rec.user_id == user.user_id
        assert rec.agent_type == user.agent_type
        assert rec.granted_scope == "memory:episodic:read"
        assert rec.error is None
        assert rec.duration_ms is not None and rec.duration_ms >= 0
        assert "query" in rec.args

    @pytest.mark.asyncio
    async def test_cache_hit_skips_pending_record(
        self, _populated_container, _fakeredis_cache
    ):
        """ADR-025 §Decision.8: cache hits skip the audit row because they
        represent the same execution we already audited when the cache was
        populated."""
        user = sentinel_user_context()

        # First loop — cold cache, one pending row
        fake1 = _SequencedClient(
            [
                _tool_call_response(
                    "recall_decisions", '{"query": "cache"}', call_id="call_a"
                ),
                _text_response("first answer"),
            ]
        )
        with _patch_async_client(fake1):
            _, pending1 = await run_memory_tool_loop(
                llama_url="http://llama/chat/completions",
                payload={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "q"}],
                    "tools": [],
                },
                user_context=user,
                session_id="sess-1",
                max_iterations=5,
            )
        assert len(pending1) == 1

        # Second loop, same session + same args — cache hit, NO pending row
        fake2 = _SequencedClient(
            [
                _tool_call_response(
                    "recall_decisions", '{"query": "cache"}', call_id="call_b"
                ),
                _text_response("second answer"),
            ]
        )
        with _patch_async_client(fake2):
            _, pending2 = await run_memory_tool_loop(
                llama_url="http://llama/chat/completions",
                payload={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "q"}],
                    "tools": [],
                },
                user_context=user,
                session_id="sess-1",
                max_iterations=5,
            )
        assert pending2 == []  # cache hit → no audit row

    @pytest.mark.asyncio
    async def test_handler_error_produces_pending_record_with_error(
        self, _populated_container, _fakeredis_cache
    ):
        """An exploding handler still produces an audit row — the fact that
        a tool was invoked is the auditable event, success or failure."""
        from dataclasses import replace

        from audittrace.tools import MEMORY_TOOL_REGISTRY, get_tool_by_name

        tool = get_tool_by_name("recall_decisions")

        async def _exploding(user_context, args):
            raise RuntimeError("episodic on fire")

        MEMORY_TOOL_REGISTRY[tool.name] = replace(tool, handler=_exploding)

        user = sentinel_user_context()
        fake = _SequencedClient(
            [
                _tool_call_response(
                    "recall_decisions", '{"query": "cache"}', call_id="call_x"
                ),
                _text_response("fallback answer"),
            ]
        )
        with _patch_async_client(fake):
            _, pending = await run_memory_tool_loop(
                llama_url="http://llama/chat/completions",
                payload={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "q"}],
                    "tools": [],
                },
                user_context=user,
                session_id="sess-1",
                max_iterations=5,
            )
        assert len(pending) == 1
        assert pending[0].error is not None
        assert "episodic on fire" in pending[0].error


# ───────────────────────── Scope-gated dispatch ─────────────────────────────


class TestScopeGate:
    @pytest.mark.asyncio
    async def test_non_admin_denied_memory_tool_returns_error_to_model(
        self, _populated_container, _fakeredis_cache
    ):
        """If the model calls a memory tool the user is not scoped for
        (shouldn't happen since tools_visible_to filters at advertisement
        time, but defensive), the loop surfaces an error result so the
        model doesn't execute ghost data."""
        from dataclasses import replace

        alice = replace(
            sentinel_user_context(),
            user_id="user-alice",
            is_admin=False,
            scopes=("memory:procedural:read",),  # only procedural
        )
        fake = _SequencedClient(
            [
                _tool_call_response(
                    "recall_decisions",  # episodic, alice has no scope
                    '{"query": "cache"}',
                    call_id="call_1",
                ),
                _text_response("fallback"),
            ]
        )
        with _patch_async_client(fake):
            _, pending = await run_memory_tool_loop(
                llama_url="http://llama/chat/completions",
                payload={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "q"}],
                    "tools": [],
                },
                user_context=alice,
                session_id="sess-1",
                max_iterations=5,
            )
        # The scope-denied call still produces an audit row (it happened)
        # but the record's error field is populated.
        assert len(pending) == 1
        assert pending[0].error is not None
        assert "scope" in pending[0].error.lower()


class TestSpanEmission:
    """Phase-2 commit 2.3: each memory tool invocation emits a child span
    so Langfuse renders input/output per recall_* call and Tempo's
    service-graph traces Postgres/Chroma/MinIO traffic back to the tool
    that caused it."""

    @pytest.fixture
    def span_exporter(self, monkeypatch: pytest.MonkeyPatch):
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        from audittrace.routes import _memory_tool_loop

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        monkeypatch.setattr(_memory_tool_loop, "_tracer", provider.get_tracer("test"))
        return exporter

    @pytest.mark.asyncio
    async def test_span_emitted_per_tool_invocation(
        self, _populated_container, _fakeredis_cache, span_exporter
    ):
        """One memory tool call → exactly one memory_tool.<name> span with
        input/output/user.id/tool.name attributes."""
        user = sentinel_user_context()
        fake = _SequencedClient(
            [
                _tool_call_response(
                    "recall_decisions", '{"query": "cache"}', call_id="c1"
                ),
                _text_response("ok"),
            ]
        )
        with _patch_async_client(fake):
            await run_memory_tool_loop(
                llama_url="http://llama/chat/completions",
                payload={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "q"}],
                    "tools": [],
                },
                user_context=user,
                session_id="sess-span-1",
                max_iterations=5,
            )
        tool_spans = [
            s
            for s in span_exporter.get_finished_spans()
            if s.name.startswith("memory_tool.")
        ]
        assert len(tool_spans) == 1, f"expected 1, got {len(tool_spans)}"
        span = tool_spans[0]
        assert span.name == "memory_tool.recall_decisions"
        attrs = span.attributes or {}
        assert attrs.get("tool.name") == "recall_decisions"
        assert attrs.get("user.id") == user.user_id
        assert attrs.get("langfuse.user.id") == user.user_id
        # Input holds the serialised args (truncated but intact for small
        # payloads).
        input_val = attrs.get("input.value") or ""
        assert "cache" in input_val
        # Output populated with the tool result dict.
        assert attrs.get("output.value"), "output.value must be set on success"

    @pytest.mark.asyncio
    async def test_cache_hit_still_emits_span(
        self, _populated_container, _fakeredis_cache, span_exporter
    ):
        """ADR-025 §Decision.8: cache hits skip the audit row, but the
        span MUST still emit so Langfuse shows the latency of the cached
        recall (else the flame-graph hides fast paths)."""
        user = sentinel_user_context()

        # Cold — populates cache, emits 1 span
        with _patch_async_client(
            _SequencedClient(
                [
                    _tool_call_response(
                        "recall_decisions", '{"query": "cache"}', call_id="c1"
                    ),
                    _text_response("first"),
                ]
            )
        ):
            await run_memory_tool_loop(
                llama_url="http://llama/chat/completions",
                payload={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "q"}],
                    "tools": [],
                },
                user_context=user,
                session_id="sess-cache-span",
                max_iterations=5,
            )

        # Warm — cache hit, still emits 1 span with cache_hit=true
        with _patch_async_client(
            _SequencedClient(
                [
                    _tool_call_response(
                        "recall_decisions", '{"query": "cache"}', call_id="c2"
                    ),
                    _text_response("second"),
                ]
            )
        ):
            await run_memory_tool_loop(
                llama_url="http://llama/chat/completions",
                payload={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "q"}],
                    "tools": [],
                },
                user_context=user,
                session_id="sess-cache-span",
                max_iterations=5,
            )

        tool_spans = [
            s
            for s in span_exporter.get_finished_spans()
            if s.name.startswith("memory_tool.")
        ]
        # 2 spans total: one cold + one cache-hit
        assert len(tool_spans) == 2
        hit_spans = [
            s for s in tool_spans if (s.attributes or {}).get("tool.cache_hit")
        ]
        assert len(hit_spans) == 1, (
            "cache-hit span must carry tool.cache_hit=true for flame-graph"
        )

    @pytest.mark.asyncio
    async def test_scope_denied_span_marked_error(
        self, _populated_container, _fakeredis_cache, span_exporter
    ):
        """When the scope guard rejects a tool, the span should carry
        ERROR status so Langfuse flags the invocation visually."""
        from audittrace.identity import UserContext

        user_no_scope = UserContext(
            user_id="alice",
            username="alice",
            agent_type="opencode",
            scopes=(),  # empty — no memory scopes
            is_admin=False,
        )
        fake = _SequencedClient(
            [
                _tool_call_response("recall_decisions", '{"query": "x"}', call_id="c1"),
                _text_response("fallback"),
            ]
        )
        with _patch_async_client(fake):
            await run_memory_tool_loop(
                llama_url="http://llama/chat/completions",
                payload={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "q"}],
                    "tools": [],
                },
                user_context=user_no_scope,
                session_id="sess-scope",
                max_iterations=5,
            )
        tool_spans = [
            s
            for s in span_exporter.get_finished_spans()
            if s.name == "memory_tool.recall_decisions"
        ]
        assert tool_spans, "tool span must be emitted even when denied"
        assert tool_spans[0].status.status_code.name == "ERROR"
