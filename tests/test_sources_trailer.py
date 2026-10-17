"""Tests for the M3 slice 1 deterministic sources trailer (spec §3 / §4.4).

Two layers:

- ``TestBuildSourcesTrailerUnit`` / ``TestNotLlmGenerated`` — pure
  ``build_sources_trailer`` behaviour against hand-built ``PendingToolCall``
  fixtures. No HTTP, no DB, no model.
- ``TestSourcesTrailerFlagOffByteIdentical`` /
  ``TestSourcesTrailerFlagOnGate4`` — full ``client.post("/v1/chat/
  completions")`` integration through ``memory_mode=tools``, proving the
  two ADR-049 HEAVY-GATE claims: (1) the flag defaults off and the
  response is byte-identical to the pre-feature shape; (2) with the flag
  on, the trailer names are EXACTLY the ``source_ref`` values recorded in
  the ``tool_calls`` audit row — a real byte-compare against the
  persisted Postgres row, not just against the fixture.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime

import pytest
from langchain_core.documents import Document
from sqlalchemy import select

from audittrace.db.models import InteractionRecord, ToolCall
from audittrace.dependencies import get_postgres_factory
from audittrace.identity import SENTINEL_SUBJECT
from audittrace.routes import _sources_trailer as trailer_mod
from audittrace.routes._memory_tool_loop import (
    PendingToolCall,
    _source_refs_from_result,
)
from audittrace.routes._sources_trailer import (
    SOURCES_TRAILER_LABEL,
    build_sources_trailer,
)
from audittrace.tools import reset_registry_for_tests
from tests.test_chat_proxy import (
    _parse_sse_deltas,
    _patch_async_client,
    _patch_tool_loop_client,
    _SequencedClient,
    _SequencedStreamClient,
    _sse_content_lines,
    _sse_line,
    _sse_tool_call_lines,
    _tools_mode_response_text,
    _tools_mode_tool_call_response,
)


@pytest.fixture(autouse=True)
def _fresh_registry_with_handlers():
    """Reset and re-import the handlers so each test starts with a clean
    registry populated by the real decorators — mirrors
    ``test_memory_tool_loop.py``'s fixture of the same name. Without this,
    a suite-order neighbour that deliberately leaves the global
    ``MEMORY_TOOL_REGISTRY`` empty (``test_memory_tools_registry.py``,
    by design) would starve every test here of ``recall_decisions`` et al.
    — the module-level ``@register_memory_tool`` decorators only fire once,
    at first import, so a later plain ``import ...memory_handlers`` does
    NOT re-populate a registry another file cleared."""
    reset_registry_for_tests()
    import importlib

    import audittrace.tools.memory_handlers as handlers_mod

    importlib.reload(handlers_mod)
    yield
    reset_registry_for_tests()


# ─────────────────────────── fixture helpers ────────────────────────────────


def _pending(
    tool_name: str,
    *,
    result_summary: str | None = None,
    source_refs: list[str] | None = None,
    error: str | None = None,
    granted_scope: str = "memory:episodic:read",
) -> PendingToolCall:
    """Build a ``PendingToolCall`` fixture. ``source_refs`` is the
    structural field ``build_sources_trailer`` actually reads (M3-SOURCES-
    TRAILER-TRUNCATION-FIX); ``result_summary`` is accepted only so a
    fixture can also carry a realistic (possibly truncated / garbled)
    audit-column value to prove the trailer never re-parses it. Defaults
    to ``[]`` — a tool call that never populated the structural field
    (e.g. an unknown-tool or scope-denied row) contributes nothing."""
    return PendingToolCall(
        tool_name=tool_name,
        user_id=SENTINEL_SUBJECT,
        agent_type="test",
        args="{}",
        result_summary=result_summary,
        error=error,
        started_at=datetime.now(),
        duration_ms=1,
        granted_scope=granted_scope,
        source_refs=source_refs if source_refs is not None else [],
    )


def _matches_summary(*source_refs: str) -> str:
    """Shape a ``result_summary`` string the way ``_execute_memory_tool``
    records it for a real recall_* result (a JSON-dumped tool result)."""
    return json.dumps(
        {
            "matches": [
                {"id": ref, "source_ref": ref, "sha256": None, "distance": 0.1}
                for ref in source_refs
            ],
            "total": len(source_refs),
        }
    )


def _seed_decisions(test_container, docs: list[tuple[str, str]]) -> None:
    """Seed the mock semantic ``decisions`` collection so ``recall_decisions``
    returns real, source_ref-bearing matches end-to-end — mirrors
    ``_populated_container`` in test_memory_tool_loop.py."""
    semantic = test_container._instances["semantic"]
    for source, body in docs:
        semantic._docs.setdefault("decisions", []).append(
            Document(
                page_content=body,
                metadata={"source": source, "collection": "decisions"},
            )
        )


# ───────────────────── unit tests — build_sources_trailer ───────────────────


class TestBuildSourcesTrailerUnit:
    def test_empty_pending_returns_none(self):
        assert build_sources_trailer([]) is None

    def test_no_source_refs_returns_none(self):
        """``read_decision``/``read_skill``-shaped results (single doc, no
        ``matches`` list) never populate ``source_refs`` — no per-tool
        special-casing needed here, the empty structural field alone
        excludes them."""
        pending = [
            _pending(
                "read_decision",
                result_summary=json.dumps({"file": "x", "content": "..."}),
                source_refs=[],
            )
        ]
        assert build_sources_trailer(pending) is None

    def test_matches_without_source_ref_returns_none(self):
        """``recall_recent_sessions``-shaped matches carry no ``source_ref``
        — ``_source_refs_from_result`` never populates the structural
        field for them, so they are excluded without special-casing the
        tool name."""
        summary = json.dumps(
            {"matches": [{"title": "session", "snippet": "x", "source": "2026-08-01"}]}
        )
        pending = [
            _pending(
                "recall_recent_sessions",
                result_summary=summary,
                source_refs=[],
                granted_scope="memory:conversational:read-own",
            )
        ]
        assert build_sources_trailer(pending) is None

    def test_errored_call_contributes_nothing(self):
        """An errored tool call has no result to cite even if a stray
        ``source_refs`` were present (defence-in-depth — in practice the
        loop never populates ``source_refs`` on an error branch)."""
        pending = [
            _pending(
                "recall_decisions",
                error="scope denied",
                source_refs=["ADR-009.md"],
            )
        ]
        assert build_sources_trailer(pending) is None

    def test_single_match_renders_label_and_bullet(self):
        pending = [
            _pending(
                "recall_decisions",
                result_summary=_matches_summary("ADR-009.md"),
                source_refs=["ADR-009.md"],
            )
        ]
        trailer = build_sources_trailer(pending)
        assert trailer == f"\n\n**{SOURCES_TRAILER_LABEL}**\n- ADR-009.md"

    def test_multiple_matches_across_rows_dedup_first_seen_order(self):
        """Two tool calls citing an overlapping source — the trailer lists
        each source ONCE, in first-seen order across rows."""
        pending = [
            _pending(
                "recall_decisions",
                result_summary=_matches_summary("ADR-009.md", "ADR-010.md"),
                source_refs=["ADR-009.md", "ADR-010.md"],
            ),
            _pending(
                "recall_semantic",
                result_summary=_matches_summary("ADR-010.md", "campagne_2024.md"),
                source_refs=["ADR-010.md", "campagne_2024.md"],
            ),
        ]
        trailer = build_sources_trailer(pending)
        assert trailer == (
            f"\n\n**{SOURCES_TRAILER_LABEL}**\n"
            "- ADR-009.md\n"
            "- ADR-010.md\n"
            "- campagne_2024.md"
        )

    def test_cache_hit_shaped_result_still_contributes(self):
        """A cache-served recall (ADR-060 / #372 D3) still shaped the
        answer — its ``result_summary`` carries ``cache_hit: true`` plus
        the same ``matches`` shape, and the loop populates ``source_refs``
        for it the same way as a fresh call; it must still cite."""
        summary = json.dumps(
            {
                "cache_hit": True,
                "matches": [{"id": "ADR-009.md", "source_ref": "ADR-009.md"}],
            }
        )
        pending = [
            _pending(
                "recall_decisions",
                result_summary=summary,
                source_refs=["ADR-009.md"],
            )
        ]
        trailer = build_sources_trailer(pending)
        assert trailer == f"\n\n**{SOURCES_TRAILER_LABEL}**\n- ADR-009.md"

    def test_garbled_result_summary_is_never_reparsed(self, caplog):
        """THE structural proof at unit scale (M3-SOURCES-TRAILER-
        TRUNCATION-FIX): ``result_summary`` truncated mid-JSON at the
        1000-char audit cap — exactly what a realistic multi-match recall
        produces in production — must have ZERO effect on the trailer.
        ``build_sources_trailer`` reads ``source_refs`` only; it must
        never even attempt to parse ``result_summary``, so a row whose
        column is invalid JSON still renders correctly and logs nothing.
        Neutering the fix (making ``_extract_source_refs`` re-parse
        ``result_summary`` again) turns this fixture's ``result_summary``
        into an unterminated-string JSONDecodeError and the trailer would
        drop this row's citation — this test would go RED."""
        pending = [
            _pending(
                "recall_decisions",
                result_summary='{"matches": [{"source_ref": "ADR-009',
                source_refs=["ADR-009.md"],
            ),
            _pending(
                "recall_semantic",
                result_summary=_matches_summary("ADR-010.md"),
                source_refs=["ADR-010.md"],
            ),
        ]
        with caplog.at_level("WARNING"):
            trailer = build_sources_trailer(pending)
        assert trailer == (
            f"\n\n**{SOURCES_TRAILER_LABEL}**\n- ADR-009.md\n- ADR-010.md"
        )
        assert caplog.text == ""

    def test_empty_result_summary_contributes_nothing(self):
        pending = [_pending("recall_decisions", result_summary=None, source_refs=[])]
        assert build_sources_trailer(pending) is None

    def test_label_is_exact_french_wording_no_causation(self):
        """Spec §3.2 — the fixed retrieval-not-causation label. Assert the
        RENDERED trailer carries the exact wording and none of the
        causation phrasing the spec explicitly rules out."""
        assert SOURCES_TRAILER_LABEL == "Sources consultées"
        trailer = build_sources_trailer(
            [
                _pending(
                    "recall_decisions",
                    result_summary=_matches_summary("ADR-009.md"),
                    source_refs=["ADR-009.md"],
                )
            ]
        )
        assert trailer is not None
        assert "Sources consultées" in trailer
        assert "utilisées pour générer" not in trailer
        assert "générer" not in trailer


class TestSourceRefsFromResult:
    """Unit tests for ``_memory_tool_loop._source_refs_from_result`` — the
    pure function that extracts ``source_ref`` values from a FULL,
    pre-truncation tool result. This is where the JSON-shape tolerance
    that used to live in ``_sources_trailer._extract_source_refs`` (before
    the truncation fix moved extraction upstream of the 1000-char cut)
    now lives."""

    def test_no_matches_key_returns_empty(self):
        assert _source_refs_from_result({"file": "x", "content": "..."}) == []

    def test_matches_without_source_ref_returns_empty(self):
        result = {
            "matches": [{"title": "session", "snippet": "x", "source": "2026-08-01"}]
        }
        assert _source_refs_from_result(result) == []

    def test_dedup_first_seen_order(self):
        result = {
            "matches": [
                {"source_ref": "ADR-009.md"},
                {"source_ref": "ADR-010.md"},
                {"source_ref": "ADR-009.md"},
            ]
        }
        assert _source_refs_from_result(result) == ["ADR-009.md", "ADR-010.md"]

    def test_non_list_matches_tolerated(self):
        assert _source_refs_from_result({"matches": "not-a-list"}) == []

    def test_non_dict_match_entries_tolerated(self):
        result = {"matches": ["not-a-dict", {"source_ref": "ADR-010.md"}]}
        assert _source_refs_from_result(result) == ["ADR-010.md"]

    def test_blank_source_ref_excluded(self):
        result = {"matches": [{"source_ref": ""}, {"source_ref": "ADR-010.md"}]}
        assert _source_refs_from_result(result) == ["ADR-010.md"]


class TestNotLlmGenerated:
    """Structural proof that no model / network call feeds the trailer —
    the spec's explicit "never LLM-generated" requirement."""

    def test_signature_accepts_only_pending(self):
        sig = inspect.signature(build_sources_trailer)
        assert list(sig.parameters) == ["pending"]

    def test_module_has_no_network_or_llm_client(self):
        source = inspect.getsource(trailer_mod)
        for forbidden in (
            "httpx",
            "AsyncClient",
            "requests.",
            "llama_url",
            "chat/completions",
        ):
            assert forbidden not in source, (
                f"unexpected network reference: {forbidden!r}"
            )


# ───────────────── integration — flag OFF byte-identical (Gate 1) ───────────


class TestSourcesTrailerFlagOffByteIdentical:
    """AUDITTRACE_RESPONSE_SOURCES defaults to 'off' — /v1/chat/completions
    stays byte-identical to the pre-feature response for every existing
    consumer, streaming and non-streaming, even when a recall tool call
    produced real, citable matches."""

    @pytest.fixture
    def _tools_mode_only(self, monkeypatch):
        from audittrace import config as config_mod

        config_mod.get_settings.cache_clear()
        monkeypatch.setenv("AUDITTRACE_MEMORY_MODE", "tools")
        config_mod.get_settings.cache_clear()
        yield
        config_mod.get_settings.cache_clear()

    async def test_non_streaming_content_unchanged(
        self, client, test_container, _tools_mode_only
    ):
        _seed_decisions(test_container, [("ADR-009.md", "cache compression body")])
        fake = _SequencedClient(
            [
                _tools_mode_tool_call_response(
                    "recall_decisions", '{"query": "cache compression"}'
                ),
                _tools_mode_response_text("Based on ADR-009: 75% reduction."),
            ]
        )
        with _patch_tool_loop_client(fake), _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "what about KV cache?"}],
                    "project": "AuditTrace",
                },
            )
        assert response.status_code == 200
        content = response.json()["choices"][0]["message"]["content"]
        assert content == "Based on ADR-009: 75% reduction."
        assert SOURCES_TRAILER_LABEL not in content

    def test_streaming_content_unchanged(
        self, client, test_container, _tools_mode_only
    ):
        _seed_decisions(test_container, [("ADR-009.md", "cache compression body")])
        fake = _SequencedStreamClient(
            [
                _sse_tool_call_lines(
                    "recall_decisions", '{"query": "cache compression"}'
                ),
                _sse_content_lines(["Based on ", "ADR-009."]),
            ]
        )
        with _patch_tool_loop_client(fake), _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "what about KV cache?"}],
                    "stream": True,
                },
            )
        assert response.status_code == 200
        chunks = _parse_sse_deltas(response.content.decode())
        # Exactly the two loop turns' frames — no third "trailer" chunk.
        joined = "".join(
            c["choices"][0]["delta"].get("content", "")
            for c in chunks
            if c.get("choices") and c["choices"][0].get("delta")
        )
        # The tool-call turn's <think> text is forwarded live (per #299 —
        # content deltas stream regardless of whether the turn ends in a
        # tool_call); only the trailer's absence is under test here.
        assert joined == "<think>recall</think>Based on ADR-009."
        assert SOURCES_TRAILER_LABEL not in joined
        assert len(fake.stream_calls) == 2


# ───────────────── integration — flag ON, gate 4 byte-compare ───────────────


class TestSourcesTrailerFlagOnGate4:
    """AUDITTRACE_RESPONSE_SOURCES=trailer — the trailer names are EXACTLY
    the ``source_ref`` values persisted in the ``tool_calls`` audit row.
    Also proves "not LLM-generated" at the integration level: no EXTRA
    upstream POST/stream call is made to produce the trailer."""

    @pytest.fixture
    def _tools_mode_with_trailer(self, monkeypatch):
        from audittrace import config as config_mod

        config_mod.get_settings.cache_clear()
        monkeypatch.setenv("AUDITTRACE_MEMORY_MODE", "tools")
        monkeypatch.setenv("AUDITTRACE_RESPONSE_SOURCES", "trailer")
        config_mod.get_settings.cache_clear()
        yield
        config_mod.get_settings.cache_clear()

    async def test_non_streaming_trailer_matches_recorded_audit_rows(
        self, client, test_container, _tools_mode_with_trailer
    ):
        _seed_decisions(
            test_container,
            [
                ("ADR-009.md", "cache compression body about caching"),
                ("ADR-010.md", "another cache compression note"),
            ],
        )
        fake = _SequencedClient(
            [
                _tools_mode_tool_call_response(
                    "recall_decisions",
                    '{"query": "cache compression"}',
                    call_id="call_1",
                ),
                _tools_mode_tool_call_response(
                    "recall_decisions", '{"query": "cache"}', call_id="call_2"
                ),
                _tools_mode_response_text("Final grounded answer."),
            ]
        )
        with _patch_tool_loop_client(fake), _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "what about KV cache?"}],
                    "project": "AuditTrace",
                },
            )
        assert response.status_code == 200
        content = response.json()["choices"][0]["message"]["content"]
        assert content.startswith("Final grounded answer.")
        assert SOURCES_TRAILER_LABEL in content

        # Exactly THREE upstream POSTs (two tool turns + final answer) —
        # no extra call was made to produce the trailer.
        assert len(fake.post_calls) == 3

        pg = get_postgres_factory()
        async with pg.get_session_factory()() as db:
            rows = (await db.execute(select(ToolCall))).scalars().all()
            interactions = (await db.execute(select(InteractionRecord))).scalars().all()
        assert len(rows) == 2  # one row per recall_decisions call

        # B5 (LibreChat state is auxiliary, the audit plane is the system
        # of record) — the trailer is DISPLAY-ONLY. It must NEVER enter the
        # persisted audit record: the interactions.answer column stays
        # exactly what the model actually answered, byte-for-byte, with no
        # trailer suffix. A regression here (e.g. persisting
        # ``answer_text + trailer``) would silently corrupt the audit trail
        # with rendering-layer text no model ever produced.
        latest_interaction = interactions[-1]
        assert SOURCES_TRAILER_LABEL not in latest_interaction.answer
        assert latest_interaction.answer == "Final grounded answer."

        recorded_refs: list[str] = []
        for row in rows:
            parsed = json.loads(row.result_summary)
            for m in parsed["matches"]:
                ref = m["source_ref"]
                if ref not in recorded_refs:
                    recorded_refs.append(ref)
        assert recorded_refs == ["ADR-009.md", "ADR-010.md"]

        expected_trailer = f"\n\n**{SOURCES_TRAILER_LABEL}**\n" + "\n".join(
            f"- {ref}" for ref in recorded_refs
        )
        # Byte-compare: the trailer appended to the answer is EXACTLY the
        # rendering of the recorded audit-row source_refs.
        assert content == "Final grounded answer." + expected_trailer

    async def test_non_streaming_trailer_survives_over_1000_char_result(
        self, client, test_container, _tools_mode_with_trailer
    ):
        """THE regression test (M3-SOURCES-TRAILER-TRUNCATION-FIX,
        2026-08-24 finding) — the fixture that would have caught the bug
        before it shipped live on v1.24.9. A realistic multi-match
        (3+ matches) recall whose FULL serialized ``result`` exceeds the
        1000-char audit-row truncation cap (``result_summary =
        json.dumps(result)[:1000]``, ``_memory_tool_loop.py``). Before the
        fix, the trailer re-derived ``source_refs`` from that truncated
        column with strict ``json.loads`` — invalid JSON at the cut,
        every match skipped, ``source_refs=[]``, trailer ``None``. The
        trailer must STILL render exactly the recorded ``source_ref``s.

        Ground truth first: assert the recorded audit row genuinely IS
        truncated to invalid JSON — proving this fixture reflects the
        real production data shape, not another short fixture that
        happens to pass either way (the vacuous-neuter-test antipattern
        this bug's own postmortem calls out).

        Neuter the fix — revert ``_sources_trailer._extract_source_refs``
        to re-derive from ``rec.result_summary`` via ``json.loads`` (the
        pre-fix behaviour) — and this test goes RED (trailer disappears
        from ``content``).
        """
        long_body = "cache compression trade-off analysis paragraph. " * 20
        _seed_decisions(
            test_container,
            [
                ("ADR-101.md", long_body + "variant one."),
                ("ADR-102.md", long_body + "variant two."),
                ("ADR-103.md", long_body + "variant three."),
            ],
        )
        fake = _SequencedClient(
            [
                _tools_mode_tool_call_response(
                    "recall_decisions",
                    '{"query": "cache compression"}',
                    call_id="call_1",
                ),
                _tools_mode_response_text("Final grounded answer."),
            ]
        )
        with _patch_tool_loop_client(fake), _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "what about KV cache?"}],
                    "project": "AuditTrace",
                },
            )
        assert response.status_code == 200
        content = response.json()["choices"][0]["message"]["content"]

        pg = get_postgres_factory()
        async with pg.get_session_factory()() as db:
            rows = (await db.execute(select(ToolCall))).scalars().all()
            interactions = (await db.execute(select(InteractionRecord))).scalars().all()
        assert len(rows) == 1
        row = rows[0]

        # Ground truth: the audit column really is truncated at the
        # 1000-char cap AND is genuinely invalid JSON at the cut — this is
        # the exact production data shape the bug needed, not a fixture
        # that happens to stay under the cap.
        assert row.result_summary is not None
        assert len(row.result_summary) == 1000
        with pytest.raises(json.JSONDecodeError):
            json.loads(row.result_summary)

        expected_refs = ["ADR-101.md", "ADR-102.md", "ADR-103.md"]
        expected_trailer = f"\n\n**{SOURCES_TRAILER_LABEL}**\n" + "\n".join(
            f"- {ref}" for ref in expected_refs
        )
        assert content == "Final grounded answer." + expected_trailer

        # B5, now under the REAL >1000-char rendering trailer (not the
        # trivial no-render case) — the persisted answer stays clean even
        # though the trailer is the whole point of this test.
        latest_interaction = interactions[-1]
        assert SOURCES_TRAILER_LABEL not in latest_interaction.answer
        assert latest_interaction.answer == "Final grounded answer."

    async def test_streaming_trailer_matches_recorded_audit_row(
        self, client, test_container, _tools_mode_with_trailer
    ):
        _seed_decisions(test_container, [("ADR-009.md", "cache compression body")])
        fake = _SequencedStreamClient(
            [
                _sse_tool_call_lines(
                    "recall_decisions", '{"query": "cache compression"}'
                ),
                _sse_content_lines(["Based on ", "ADR-009."]),
            ]
        )
        with _patch_tool_loop_client(fake), _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "what about KV cache?"}],
                    "stream": True,
                },
            )
        assert response.status_code == 200
        chunks = _parse_sse_deltas(response.content.decode())
        joined = "".join(
            c["choices"][0]["delta"].get("content", "")
            for c in chunks
            if c.get("choices") and c["choices"][0].get("delta")
        )
        assert SOURCES_TRAILER_LABEL in joined
        # Exactly the two loop turns' stream calls — no extra call for
        # the trailer, which is rendered in-process from ``result.pending``.
        assert len(fake.stream_calls) == 2

        expected_trailer = f"\n\n**{SOURCES_TRAILER_LABEL}**\n- ADR-009.md"
        # The tool-call turn's <think> text is forwarded live (#299); the
        # byte-compare under test here is the trailer itself, appended
        # exactly once, exactly after the real answer text.
        assert joined == "<think>recall</think>Based on ADR-009." + expected_trailer

        # B5 — the trailer is DISPLAY-ONLY on the STREAMING path too. The
        # persisted interactions.answer (built from result.answer_text,
        # which accumulates only the LOOP'S OWN streamed chunks) must
        # never carry the trailer, even though the trailer chunk was
        # yielded on the wire after those chunks.
        pg = get_postgres_factory()
        async with pg.get_session_factory()() as db:
            interactions = (await db.execute(select(InteractionRecord))).scalars().all()
        latest_interaction = interactions[-1]
        assert SOURCES_TRAILER_LABEL not in latest_interaction.answer
        assert latest_interaction.answer == "<think>recall</think>Based on ADR-009."

    async def test_trivial_prompt_no_recall_no_trailer(
        self, client, _tools_mode_with_trailer
    ):
        """Flag on but nothing was recalled — no trailer, not an empty one
        (the corpus_status "don't cry wolf" principle applied here too)."""
        fake = _SequencedClient([_tools_mode_response_text("Just text, no recall.")])
        with _patch_tool_loop_client(fake), _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "hello"}],
                    "project": "AuditTrace",
                },
            )
        assert response.status_code == 200
        content = response.json()["choices"][0]["message"]["content"]
        assert content == "Just text, no recall."
        assert SOURCES_TRAILER_LABEL not in content

    def test_external_tool_call_forwarded_gets_no_trailer(
        self, client, test_container, _tools_mode_with_trailer
    ):
        """finish_reason == 'tool_calls' (external tool forwarded to the
        client) means there is no final answer yet to cite sources for —
        the trailer must not be appended to a tool-call turn."""
        _seed_decisions(test_container, [("ADR-009.md", "cache compression body")])
        external_tool_call_turn = [
            _sse_line(
                {
                    "id": "cmpl-s",
                    "created": 1,
                    "model": "qwen3.5-35b",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_ext",
                                        "type": "function",
                                        "function": {"name": "bash", "arguments": "{}"},
                                    }
                                ]
                            },
                        }
                    ],
                }
            ),
            _sse_line(
                {
                    "id": "cmpl-s",
                    "created": 1,
                    "model": "qwen3.5-35b",
                    "choices": [
                        {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                }
            ),
            "data: [DONE]",
        ]
        fake = _SequencedStreamClient([external_tool_call_turn])
        with _patch_tool_loop_client(fake), _patch_async_client(fake):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.5-35b",
                    "messages": [{"role": "user", "content": "run a command"}],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "bash",
                                "description": "x",
                                "parameters": {},
                            },
                        }
                    ],
                    "stream": True,
                },
            )
        assert response.status_code == 200
        raw = response.content.decode()
        assert SOURCES_TRAILER_LABEL not in raw
