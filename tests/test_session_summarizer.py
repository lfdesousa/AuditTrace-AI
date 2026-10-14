"""Tests for the ADR-030 background session summariser.

Covers the whole per-cycle path using the in-memory SQLAlchemy
factory and an httpx ``MockTransport`` for the summariser LLM. No
real Postgres, no real llama-server — the logic that matters (what
counts as eligible, what gets written, how stale summaries are
upserted) is dialect-agnostic.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from audittrace.config import Settings
from audittrace.db.models import InteractionRecord, SessionRecord
from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.services import session_summarizer as ss_mod
from audittrace.services.session_summarizer import (
    EligibleSession,
    SessionSummarizer,
    _format_transcript,
    _is_stale,
    _parse_llm_response,
    _span_trace_id_hex,
)

# ──────────────────────────── Helpers ────────────────────────────────


def _settings(**overrides) -> Settings:
    """Build a Settings with summariser defaults overridable."""
    base = {
        "summarizer_enabled": True,
        "summarizer_url": "http://fake-summarizer/v1",
        "summarizer_model": "mistral-7b-summarizer",
        "summarizer_idle_minutes": 15,
        "summarizer_interval_minutes": 5,
        "summarizer_max_per_cycle": 10,
    }
    base.update(overrides)
    return Settings(**base)


def _iso_minutes_ago(minutes: int) -> str:
    return (datetime.now() - timedelta(minutes=minutes)).isoformat()


def _mock_summariser_client(
    *,
    summary: str = "A brief summary of the session.",
    key_points: list[str] | None = None,
    raw_override: str | None = None,
) -> httpx.AsyncClient:
    """Build an httpx.AsyncClient with a MockTransport returning a
    minimal OpenAI-shaped response carrying the given strict-JSON body."""
    if key_points is None:
        key_points = ["point-a", "point-b"]
    content = (
        raw_override
        if raw_override is not None
        else json.dumps({"summary": summary, "key_points": key_points})
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(_handler))


# v1.29.5 — ownership-scoping security fix. Deliberately its own,
# independently-sorted import group, placed AFTER the helper above (same
# convention ``test_acl_ownership_rls.py`` uses, ADDENDUM-A1 §8): merging
# these into the top-of-file block would make ruff's import-sort check
# want to reorder/re-wrap the EXISTING lines there, which would show as
# removed lines in a diff whose production-side intent is additions-only.
import inspect  # noqa: E402
import logging  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402

from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from audittrace.services.session_summarizer import (  # noqa: E402
    _is_foreign_owner,
    _is_sessions_pk_violation,
)

# ──────────────────────────── Fixtures ───────────────────────────────


@pytest_asyncio.fixture
async def pg_factory() -> InMemoryPostgresFactory:
    f = InMemoryPostgresFactory()
    await f.create_schema()
    return f


@pytest_asyncio.fixture
async def seed_session_turns(pg_factory):
    """Seed a session idle for 30 minutes (comfortably > 15m threshold)."""
    async with pg_factory.get_session_factory()() as session:
        session.add_all(
            [
                InteractionRecord(
                    project="P",
                    source="chat",
                    question="What is the capital of France?",
                    answer="Paris.",
                    timestamp=_iso_minutes_ago(40),
                    session_id="sess-idle",
                    user_id="user-1",
                ),
                InteractionRecord(
                    project="P",
                    source="chat",
                    question="What about Germany?",
                    answer="Berlin.",
                    timestamp=_iso_minutes_ago(30),
                    session_id="sess-idle",
                    user_id="user-1",
                ),
            ]
        )
        await session.commit()
    return pg_factory


# ──────────────────────────── Pure-helper tests ──────────────────────


class TestParseLLMResponse:
    def test_valid_json(self):
        parsed = _parse_llm_response('{"summary": "ok", "key_points": ["a"]}')
        assert parsed == {"summary": "ok", "key_points": ["a"]}

    def test_empty_string_returns_none(self):
        assert _parse_llm_response("") is None

    def test_malformed_json_returns_none(self):
        assert _parse_llm_response("{not json") is None

    def test_non_dict_returns_none(self):
        assert _parse_llm_response('["a", "b"]') is None

    def test_markdown_fenced_json_tolerated(self):
        raw = '```json\n{"summary": "x", "key_points": []}\n```'
        parsed = _parse_llm_response(raw)
        assert parsed == {"summary": "x", "key_points": []}


class TestCoerceDatetime:
    """ADR-030: driver-native values differ by dialect; the summariser
    must parse both datetime objects (Postgres) and ISO / SQLite strings."""

    def test_datetime_passthrough(self):
        from audittrace.services.session_summarizer import _coerce_datetime

        dt = datetime.now()
        assert _coerce_datetime(dt) is dt

    def test_none_returns_none(self):
        from audittrace.services.session_summarizer import _coerce_datetime

        assert _coerce_datetime(None) is None

    def test_iso_string(self):
        from audittrace.services.session_summarizer import _coerce_datetime

        got = _coerce_datetime("2026-04-15T10:30:00")
        assert isinstance(got, datetime)
        assert got.year == 2026 and got.month == 4 and got.day == 15

    def test_sqlite_style_space_separator(self):
        from audittrace.services.session_summarizer import _coerce_datetime

        got = _coerce_datetime("2026-04-15 10:30:00.123456")
        assert isinstance(got, datetime)
        assert got.hour == 10 and got.minute == 30

    def test_malformed_string_returns_none(self):
        from audittrace.services.session_summarizer import _coerce_datetime

        assert _coerce_datetime("not a date") is None

    def test_other_type_returns_none(self):
        from audittrace.services.session_summarizer import _coerce_datetime

        assert _coerce_datetime(12345) is None


class TestFormatTranscript:
    def test_numbered_q_and_a(self):
        turns = [
            InteractionRecord(
                project="P",
                source="chat",
                question="Q1",
                answer="A1",
                timestamp="t1",
                session_id="s",
                user_id="u",
            ),
            InteractionRecord(
                project="P",
                source="chat",
                question="Q2",
                answer="A2",
                timestamp="t2",
                session_id="s",
                user_id="u",
            ),
        ]
        out = _format_transcript(turns)
        assert "[1] Q: Q1" in out
        assert "[2] Q: Q2" in out
        assert "A: A1" in out and "A: A2" in out


# ──────────────────────────── Integration tests ──────────────────────


class TestRunOnce:
    @pytest.mark.asyncio
    async def test_zero_eligible_returns_zero(self, pg_factory):
        """No interactions at all → no work."""
        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=pg_factory.get_session_factory(),
            http_client=_mock_summariser_client(),
        )
        count = await summariser.run_once()
        assert count == 0

    @pytest.mark.asyncio
    async def test_summarises_idle_session(self, seed_session_turns):
        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=seed_session_turns.get_session_factory(),
            http_client=_mock_summariser_client(
                summary="Capitals of France and Germany",
                key_points=["Paris", "Berlin"],
            ),
        )
        count = await summariser.run_once()
        assert count == 1

        async with seed_session_turns.get_session_factory()() as db:
            row = (
                await db.execute(select(SessionRecord).filter_by(id="sess-idle"))
            ).scalar_one()
            assert row.summary == "Capitals of France and Germany"
            assert json.loads(row.key_points) == ["Paris", "Berlin"]
            assert row.model == "mistral-7b-summarizer"
            assert row.user_id == "user-1"
            assert row.summarized_at is not None

    @pytest.mark.asyncio
    async def test_recent_session_not_eligible(self, pg_factory):
        """A session idle only 5 minutes must NOT be summarised — still
        inside the 15-minute idle window."""
        async with pg_factory.get_session_factory()() as db:
            db.add(
                InteractionRecord(
                    project="P",
                    source="chat",
                    question="Q",
                    answer="A",
                    timestamp=_iso_minutes_ago(5),
                    session_id="sess-recent",
                    user_id="user-1",
                )
            )
            await db.commit()

        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=pg_factory.get_session_factory(),
            http_client=_mock_summariser_client(),
        )
        count = await summariser.run_once()
        assert count == 0

        async with pg_factory.get_session_factory()() as db:
            assert (
                await db.execute(select(SessionRecord).filter_by(id="sess-recent"))
            ).scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_up_to_date_session_skipped(self, pg_factory):
        """summarized_at >= last_ts → no new work."""
        async with pg_factory.get_session_factory()() as db:
            db.add_all(
                [
                    InteractionRecord(
                        project="P",
                        source="chat",
                        question="Q",
                        answer="A",
                        timestamp=_iso_minutes_ago(40),
                        session_id="sess-fresh",
                        user_id="user-1",
                    ),
                    SessionRecord(
                        id="sess-fresh",
                        project="P",
                        date=_iso_minutes_ago(20),
                        summary="Prior summary",
                        key_points="[]",
                        model="mistral-7b-summarizer",
                        user_id="user-1",
                        # summarized_at NEWER than the last interaction ts
                        summarized_at=datetime.now() - timedelta(minutes=10),
                    ),
                ]
            )
            await db.commit()

        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=pg_factory.get_session_factory(),
            http_client=_mock_summariser_client(summary="NEW"),
        )
        count = await summariser.run_once()
        assert count == 0

        async with pg_factory.get_session_factory()() as db:
            row = (
                await db.execute(select(SessionRecord).filter_by(id="sess-fresh"))
            ).scalar_one()
            assert row.summary == "Prior summary"  # untouched

    @pytest.mark.asyncio
    async def test_stale_session_re_summarised(self, pg_factory):
        """summarized_at < last_ts → re-summarise and UPDATE in place."""
        last_ts = _iso_minutes_ago(40)
        async with pg_factory.get_session_factory()() as db:
            db.add_all(
                [
                    InteractionRecord(
                        project="P",
                        source="chat",
                        question="Old Q",
                        answer="Old A",
                        timestamp=_iso_minutes_ago(90),
                        session_id="sess-stale",
                        user_id="user-1",
                    ),
                    InteractionRecord(
                        project="P",
                        source="chat",
                        question="New Q",
                        answer="New A",
                        timestamp=last_ts,
                        session_id="sess-stale",
                        user_id="user-1",
                    ),
                    SessionRecord(
                        id="sess-stale",
                        project="P",
                        date=_iso_minutes_ago(80),
                        summary="Stale summary",
                        key_points="[]",
                        model="mistral-7b-summarizer",
                        user_id="user-1",
                        # summarized_at OLDER than last_ts
                        summarized_at=datetime.now() - timedelta(minutes=80),
                    ),
                ]
            )
            await db.commit()

        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=pg_factory.get_session_factory(),
            http_client=_mock_summariser_client(summary="Refreshed summary"),
        )
        count = await summariser.run_once()
        assert count == 1

        async with pg_factory.get_session_factory()() as db:
            # Still exactly one row — updated, not duplicated.
            rows = (
                (await db.execute(select(SessionRecord).filter_by(id="sess-stale")))
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert rows[0].summary == "Refreshed summary"
            assert rows[0].summarized_at is not None
            assert rows[0].summarized_at > datetime.now() - timedelta(minutes=1)

    @pytest.mark.asyncio
    async def test_null_session_id_ignored(self, pg_factory):
        """Interactions with NULL session_id must not reach the LLM."""
        async with pg_factory.get_session_factory()() as db:
            db.add(
                InteractionRecord(
                    project="P",
                    source="chat",
                    question="orphan",
                    answer="orphan",
                    timestamp=_iso_minutes_ago(40),
                    session_id=None,
                    user_id="user-1",
                )
            )
            await db.commit()

        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=pg_factory.get_session_factory(),
            http_client=_mock_summariser_client(),
        )
        count = await summariser.run_once()
        assert count == 0

    @pytest.mark.asyncio
    async def test_max_per_cycle_respected(self, pg_factory):
        """max_per_cycle=2 over 5 eligible sessions → process 2 only."""
        async with pg_factory.get_session_factory()() as db:
            for idx in range(5):
                db.add(
                    InteractionRecord(
                        project="P",
                        source="chat",
                        question=f"Q{idx}",
                        answer=f"A{idx}",
                        timestamp=_iso_minutes_ago(40 + idx),
                        session_id=f"sess-{idx}",
                        user_id="user-1",
                    )
                )
            await db.commit()

        summariser = SessionSummarizer(
            settings=_settings(summarizer_max_per_cycle=2),
            session_factory=pg_factory.get_session_factory(),
            http_client=_mock_summariser_client(),
        )
        count = await summariser.run_once()
        assert count == 2

        async with pg_factory.get_session_factory()() as db:
            assert (
                await db.execute(select(func.count()).select_from(SessionRecord))
            ).scalar_one() == 2

    @pytest.mark.asyncio
    async def test_malformed_json_leaves_summarized_at_null(self, seed_session_turns):
        """LLM returns garbage → no SessionRecord is written. The row
        stays eligible for the next cycle."""
        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=seed_session_turns.get_session_factory(),
            http_client=_mock_summariser_client(raw_override="not json at all"),
        )
        count = await summariser.run_once()
        assert count == 1  # the cycle attempted — the LLM just failed

        async with seed_session_turns.get_session_factory()() as db:
            row = (
                await db.execute(select(SessionRecord).filter_by(id="sess-idle"))
            ).scalar_one_or_none()
            assert row is None

    @pytest.mark.asyncio
    async def test_never_summarised_beats_backlog_of_up_to_date(self, pg_factory):
        """Regression guard (2026-04-15): with max_per_cycle=10 and a
        backlog of 20 already-up-to-date sessions older than an
        unsummarised session, the old ordering (``last_ts ASC`` only)
        starved the unsummarised row because the oldest 30 fetched
        rows were all fresh-enough-already. Fixed by
        ``ORDER BY (summarized_at IS NULL) DESC`` — unsummarised rows
        are prioritised regardless of their relative age."""
        async with pg_factory.get_session_factory()() as db:
            # 20 old sessions, all already summarised AFTER their last
            # interaction — up-to-date, should never be picked.
            for idx in range(20):
                sid = f"sess-old-{idx:02d}"
                db.add_all(
                    [
                        InteractionRecord(
                            project="P",
                            source="chat",
                            question="q",
                            answer="a",
                            timestamp=_iso_minutes_ago(120 + idx),
                            session_id=sid,
                            user_id="user-1",
                        ),
                        SessionRecord(
                            id=sid,
                            project="P",
                            date=_iso_minutes_ago(100),
                            summary=f"up-to-date {idx}",
                            key_points="[]",
                            model="mistral-7b-summarizer",
                            user_id="user-1",
                            summarized_at=datetime.now() - timedelta(minutes=90),
                        ),
                    ]
                )
            # One NEVER-summarised session, NEWER than the backlog
            # head but still past the idle threshold.
            db.add(
                InteractionRecord(
                    project="P",
                    source="chat",
                    question="new q",
                    answer="new a",
                    timestamp=_iso_minutes_ago(30),
                    session_id="sess-fresh-but-unsummarised",
                    user_id="user-1",
                )
            )
            await db.commit()

        summariser = SessionSummarizer(
            settings=_settings(summarizer_max_per_cycle=10),
            session_factory=pg_factory.get_session_factory(),
            http_client=_mock_summariser_client(),
        )
        await summariser.run_once()

        async with pg_factory.get_session_factory()() as db:
            # The unsummarised session must have been picked despite
            # being newer than the 20-session backlog.
            assert (
                await db.execute(
                    select(SessionRecord).filter_by(id="sess-fresh-but-unsummarised")
                )
            ).scalar_one_or_none() is not None

    @pytest.mark.asyncio
    async def test_per_user_attribution(self, pg_factory):
        """Two idle sessions for different users → each SessionRecord
        carries its own user_id (no cross-user leakage)."""
        async with pg_factory.get_session_factory()() as db:
            db.add_all(
                [
                    InteractionRecord(
                        project="P",
                        source="chat",
                        question="alice q",
                        answer="alice a",
                        timestamp=_iso_minutes_ago(40),
                        session_id="sess-alice",
                        user_id="alice",
                    ),
                    InteractionRecord(
                        project="P",
                        source="chat",
                        question="bob q",
                        answer="bob a",
                        timestamp=_iso_minutes_ago(30),
                        session_id="sess-bob",
                        user_id="bob",
                    ),
                ]
            )
            await db.commit()

        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=pg_factory.get_session_factory(),
            http_client=_mock_summariser_client(),
        )
        assert await summariser.run_once() == 2

        async with pg_factory.get_session_factory()() as db:
            alice = (
                await db.execute(select(SessionRecord).filter_by(id="sess-alice"))
            ).scalar_one()
            bob = (
                await db.execute(select(SessionRecord).filter_by(id="sess-bob"))
            ).scalar_one()
            assert alice.user_id == "alice"
            assert bob.user_id == "bob"


class TestMalformedLLMOutputs:
    """The grammar-constrained decoding path is llama.cpp-specific; the
    summariser must tolerate other backends that treat response_format
    as advisory. These cases cover the defensive branches in ``_persist``
    and ``_call_llm`` that would otherwise be easy to regress on."""

    @pytest.mark.asyncio
    async def test_non_list_key_points_coerced_to_empty(self, seed_session_turns):
        """LLM returns ``{"key_points": "oops"}`` instead of a list — the
        write still succeeds with an empty list rather than crashing."""
        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=seed_session_turns.get_session_factory(),
            http_client=_mock_summariser_client(
                raw_override='{"summary": "ok", "key_points": "oops"}'
            ),
        )
        await summariser.run_once()

        async with seed_session_turns.get_session_factory()() as db:
            row = (
                await db.execute(select(SessionRecord).filter_by(id="sess-idle"))
            ).scalar_one()
            assert json.loads(row.key_points) == []

    @pytest.mark.asyncio
    async def test_empty_choices_in_llm_response_yields_no_summary(
        self, seed_session_turns
    ):
        """Degenerate backend: ``choices: []`` — no content to parse.
        Nothing written, row stays eligible next cycle."""

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": []})

        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=seed_session_turns.get_session_factory(),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
        )
        await summariser.run_once()

        async with seed_session_turns.get_session_factory()() as db:
            assert (
                await db.execute(select(SessionRecord).filter_by(id="sess-idle"))
            ).scalar_one_or_none() is None


class TestRunLifecycle:
    @pytest.mark.asyncio
    async def test_run_cancellation_exits_cleanly(self, pg_factory):
        """Cancelling the ``run()`` task must surface as
        CancelledError — lifespan relies on this for clean shutdown."""
        summariser = SessionSummarizer(
            settings=_settings(summarizer_interval_minutes=60),
            session_factory=pg_factory.get_session_factory(),
            http_client=_mock_summariser_client(),
        )
        task = asyncio.create_task(summariser.run())
        # Let the first run_once finish.
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ─────────────── Backlog #10 — pre-flight ctx-overflow guard ────────────


def _mock_summariser_client_with_tokenize(
    *,
    tokens_per_call: int,
    summary: str = "ok",
    key_points: list[str] | None = None,
    tokenize_status: int = 200,
) -> httpx.AsyncClient:
    """Mock transport that routes ``/tokenize`` and ``/v1/chat/completions``
    to different responses, matching llama-server's URL layout.

    ``tokens_per_call`` lets a test pin the count returned for ANY
    tokenize call. The granularity is intentionally coarse — we are
    testing the budget-comparison branch, not the truncation algorithm
    pinned to a particular tokeniser.

    ``tokenize_status`` lets a test simulate ``/tokenize`` being
    unreachable (5xx / non-200) so the fall-through-to-raw-send branch
    is exercised.
    """
    if key_points is None:
        key_points = ["a"]
    chat_content = json.dumps({"summary": summary, "key_points": key_points})

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tokenize"):
            if tokenize_status != 200:
                return httpx.Response(tokenize_status, json={})
            # llama.cpp tokenize returns {"tokens": [int, int, ...]}.
            return httpx.Response(200, json={"tokens": list(range(tokens_per_call))})
        # /v1/chat/completions
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": chat_content,
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(_handler))


@pytest.fixture
async def seed_five_turn_session(pg_factory):
    """Seed a session with 5 idle turns so the truncate branch has
    something to drop.

    Timestamps are spaced 1 minute apart inside the idle window so the
    eligibility query treats this as one session, idle for >15 min.
    """
    async with pg_factory.get_session_factory()() as session:
        for i in range(5):
            session.add(
                InteractionRecord(
                    project="P",
                    source="chat",
                    question=f"Q{i}",
                    answer=f"A{i}",
                    timestamp=_iso_minutes_ago(60 - i),  # 60, 59, 58, 57, 56
                    session_id="sess-long",
                    user_id="user-1",
                )
            )
        await session.commit()
    return pg_factory


class TestCtxOverflowGuard:
    """Backlog #10 — primary fix from project_summarizer_400.md."""

    @pytest.mark.asyncio
    async def test_truncate_branch_drops_oldest_turns(self, seed_five_turn_session):
        """Prompt over ctx → drop oldest turns until it fits, write a
        normal summary annotated with the truncation note.

        Mock returns token counts in a fixed sequence so we can pin which
        truncation step succeeds. With budget = ctx(100)-reserve(20) = 80
        and sys_tokens = 10, the per-step transcript count must drop from
        > 70 to ≤ 70 between calls. Sequence: full=100, drop1→90, drop2→80,
        drop3→70 (fits, 10+70=80 ≤ 80). Expect dropped=3, 2 turns kept.
        """
        token_counts = iter(
            [
                10,  # _SYSTEM_PROMPT
                100,  # full 5-turn transcript — over budget
                90,  # 4 turns — still over
                80,  # 3 turns — still over (10+80=90 > 80)
                70,  # 2 turns — fits (10+70=80 ≤ 80) ← returns here
            ]
        )

        def _handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/tokenize"):
                count = next(token_counts, 0)
                return httpx.Response(200, json={"tokens": list(range(count))})
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {"summary": "trunc-ok", "key_points": ["k"]}
                                ),
                            },
                            "finish_reason": "stop",
                        }
                    ]
                },
            )

        summariser = SessionSummarizer(
            settings=_settings(
                summarizer_ctx_tokens=100,
                summarizer_ctx_reserve_tokens=20,
            ),
            session_factory=seed_five_turn_session.get_session_factory(),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
        )
        count = await summariser.run_once()
        assert count == 1

        async with seed_five_turn_session.get_session_factory()() as db:
            row = (
                await db.execute(select(SessionRecord).filter_by(id="sess-long"))
            ).scalar_one()
            assert "[truncated:" in row.summary, (
                f"expected truncation note in summary, got: {row.summary}"
            )
            assert "trunc-ok" in row.summary
            assert row.model == "mistral-7b-summarizer"  # NOT the sentinel model
            assert row.summarized_at is not None

    @pytest.mark.asyncio
    async def test_sentinel_branch_for_pathological_single_turn(
        self, seed_session_turns
    ):
        """Even the most recent single turn exceeds ctx → write a sentinel
        SessionRecord so the row leaves the eligibility set; no infinite
        retry. The seeded fixture has 2 turns; we report tokens_per_call
        large enough that even one turn alone busts the budget."""
        summariser = SessionSummarizer(
            settings=_settings(
                summarizer_ctx_tokens=10,
                summarizer_ctx_reserve_tokens=2,
            ),
            session_factory=seed_session_turns.get_session_factory(),
            http_client=_mock_summariser_client_with_tokenize(
                tokens_per_call=999,  # everything is way over a budget of 8
            ),
        )
        count = await summariser.run_once()
        assert count == 1  # sentinel write counts as a successful skip

        async with seed_session_turns.get_session_factory()() as db:
            row = (
                await db.execute(select(SessionRecord).filter_by(id="sess-idle"))
            ).scalar_one()
            assert row.model == "sentinel-skip-ctx-overflow-auto"
            assert "sentinel-skip-ctx-overflow-auto" in row.summary
            assert row.summarized_at is not None  # critical: leaves eligibility set

    @pytest.mark.asyncio
    async def test_tokenize_unreachable_falls_back_to_raw_send(
        self, seed_session_turns
    ):
        """If ``/tokenize`` is unavailable (e.g. older llama-server build,
        partial outage), the summariser must NOT block on the pre-flight
        check — it falls through and sends the prompt as-is. The existing
        post-error path is the safety net of last resort.

        Regression guard: the happy path produced a normal summary BEFORE
        backlog #10 landed; introducing the pre-flight must not break it."""
        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=seed_session_turns.get_session_factory(),
            http_client=_mock_summariser_client_with_tokenize(
                tokens_per_call=10,
                summary="happy-path-summary",
                key_points=["p"],
                tokenize_status=503,  # /tokenize unreachable
            ),
        )
        count = await summariser.run_once()
        assert count == 1

        async with seed_session_turns.get_session_factory()() as db:
            row = (
                await db.execute(select(SessionRecord).filter_by(id="sess-idle"))
            ).scalar_one()
            assert row.summary == "happy-path-summary"
            assert "[truncated:" not in row.summary  # no spurious annotation
            assert row.model == "mistral-7b-summarizer"


class TestPostgresGUCStatements:
    """Postgres-only SQL the SQLite test factory never exercises.

    The write-path GUC was once emitted as ``SET LOCAL
    app.current_user_id = :uid``. Postgres ``SET`` is a utility command
    that cannot bind a parameter, so it compiled to ``SET LOCAL
    app.current_user_id = $1`` and raised ``syntax error at or near
    "$1"`` on every cycle — invisible to the SQLite suite (the branch is
    ``# pragma: no cover``). These tests fake a ``postgresql`` dialect and
    assert the *parameterizable* ``set_config(...)`` form is emitted, so
    the regression cannot recur silently.
    """

    @staticmethod
    def _fake_pg_db() -> MagicMock:
        db = MagicMock()
        db.bind.dialect.name = "postgresql"
        db.execute = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_set_user_id_uses_set_config_not_set_local(self):
        db = self._fake_pg_db()
        await SessionSummarizer._set_user_id_if_postgres(db, "user-x")

        db.execute.assert_awaited_once()
        stmt, params = db.execute.await_args.args
        sql = str(stmt)
        # The parameterizable, transaction-LOCAL form.
        assert "set_config('app.current_user_id'" in sql
        assert ", true)" in sql
        # The broken un-parameterizable form must NOT come back.
        assert "SET LOCAL app.current_user_id =" not in sql
        # The user id flows through a real bind parameter.
        assert params == {"uid": "user-x"}

    @pytest.mark.asyncio
    async def test_set_user_id_noop_on_sqlite(self):
        db = MagicMock()
        db.bind.dialect.name = "sqlite"
        db.execute = AsyncMock()
        await SessionSummarizer._set_user_id_if_postgres(db, "user-x")
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disable_rls_emits_row_security_off_on_postgres(self):
        db = self._fake_pg_db()
        await SessionSummarizer._disable_rls_if_postgres(db)
        db.execute.assert_awaited_once()
        (stmt,) = db.execute.await_args.args
        assert "row_security = off" in str(stmt)


# ──────────────── ctx auto-detect + poison-pill hardening (2026-06-09) ────────


def _mock_client_props_tokenize(
    *,
    n_ctx: int | None = None,
    props_status: int = 200,
    tokenize_status: int = 200,
    token_counts=None,
    tokens_per_call: int = 10,
    summary: str = "ok",
    props_calls: list[int] | None = None,
) -> httpx.AsyncClient:
    """Mock transport routing ``/props``, ``/tokenize`` and ``/chat/completions``.

    ``n_ctx`` (when set) is returned under ``default_generation_settings`` on
    ``/props``. ``props_status``/``tokenize_status`` simulate those endpoints
    being unreachable. ``token_counts`` is an iterator pinning successive
    ``/tokenize`` counts; otherwise every call returns ``tokens_per_call``.
    ``props_calls`` (a list) records one entry per ``/props`` hit so a test can
    assert the result is cached.
    """
    chat_content = json.dumps({"summary": summary, "key_points": ["k"]})

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/props"):
            if props_calls is not None:
                props_calls.append(1)
            if props_status != 200:
                return httpx.Response(props_status, json={})
            body: dict = {}
            if n_ctx is not None:
                body["default_generation_settings"] = {"n_ctx": n_ctx}
            return httpx.Response(200, json=body)
        if path.endswith("/tokenize"):
            if tokenize_status != 200:
                return httpx.Response(tokenize_status, json={})
            count = next(token_counts) if token_counts is not None else tokens_per_call
            return httpx.Response(200, json={"tokens": list(range(count))})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": chat_content},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(_handler))


class TestCtxAutoDetect:
    """ctx window resolved from the server's /props, not the configured default.

    Regression for the 2026-06-09 incident: config said 32768, the live
    summariser ran n_ctx=8192, the guard waved oversized prompts through and
    every cycle 4xx/5xx'd into an infinite retry.
    """

    @pytest.mark.asyncio
    async def test_props_n_ctx_overrides_large_configured_default(
        self, seed_five_turn_session
    ):
        """A small server n_ctx must win over a huge configured value, forcing
        truncation that the configured value alone would never trigger."""
        # sys=10, full=100, then 90, 80, 50 as oldest turns drop.
        counts = iter([10, 100, 90, 80, 50])
        summariser = SessionSummarizer(
            settings=_settings(
                summarizer_ctx_tokens=100_000,  # would fit everything
                summarizer_ctx_reserve_tokens=20,
            ),
            session_factory=seed_five_turn_session.get_session_factory(),
            http_client=_mock_client_props_tokenize(n_ctx=80, token_counts=counts),
        )
        assert await summariser.run_once() == 1
        async with seed_five_turn_session.get_session_factory()() as db:
            row = (
                await db.execute(select(SessionRecord).filter_by(id="sess-long"))
            ).scalar_one()
            # Truncation only happens if budget came from n_ctx(80), not 100000.
            assert "[truncated:" in row.summary
            assert row.summarized_at is not None

    @pytest.mark.asyncio
    async def test_props_unreachable_falls_back_to_configured(
        self, seed_five_turn_session
    ):
        """/props down → use the configured ctx; a transcript that fits it is
        summarised without truncation."""
        counts = iter([10, 100, 90, 80, 50])
        summariser = SessionSummarizer(
            settings=_settings(
                summarizer_ctx_tokens=100_000,
                summarizer_ctx_reserve_tokens=20,
            ),
            session_factory=seed_five_turn_session.get_session_factory(),
            http_client=_mock_client_props_tokenize(
                props_status=503, token_counts=counts
            ),
        )
        assert await summariser.run_once() == 1
        async with seed_five_turn_session.get_session_factory()() as db:
            row = (
                await db.execute(select(SessionRecord).filter_by(id="sess-long"))
            ).scalar_one()
            assert "[truncated:" not in row.summary  # configured budget fit it

    @pytest.mark.asyncio
    async def test_ctx_resolution_is_cached(self):
        """/props is queried at most once; the result is memoised."""
        props_calls: list[int] = []
        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=MagicMock(),
            http_client=_mock_client_props_tokenize(
                n_ctx=8192, props_calls=props_calls
            ),
        )
        first = await summariser._resolve_ctx_tokens()
        second = await summariser._resolve_ctx_tokens()
        assert first == second == 8192
        assert len(props_calls) == 1
        await summariser._aclose_owned_client()

    @pytest.mark.asyncio
    async def test_fetch_server_n_ctx_top_level_and_missing(self):
        """n_ctx may live at the top level; a malformed body yields None."""
        top = SessionSummarizer(
            settings=_settings(),
            session_factory=MagicMock(),
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(200, json={"n_ctx": 4096})
                )
            ),
        )
        assert await top._fetch_server_n_ctx() == 4096
        await top._aclose_owned_client()

        bad = SessionSummarizer(
            settings=_settings(),
            session_factory=MagicMock(),
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(200, json={"unrelated": 1})
                )
            ),
        )
        assert await bad._fetch_server_n_ctx() is None
        await bad._aclose_owned_client()


class TestPoisonPillHardening:
    """A session must never wedge the worker into an infinite-retry loop."""

    @pytest.mark.asyncio
    async def test_char_fallback_converges_when_tokenize_down(
        self, seed_five_turn_session
    ):
        """/tokenize AND /props down → char-estimate keeps the fit loop
        converging; the session is still summarised, never re-queued forever."""
        summariser = SessionSummarizer(
            settings=_settings(
                summarizer_ctx_tokens=400,  # generous vs the tiny transcript
                summarizer_ctx_reserve_tokens=10,
            ),
            session_factory=seed_five_turn_session.get_session_factory(),
            http_client=_mock_client_props_tokenize(
                props_status=503,
                tokenize_status=503,
                summary="converged",
            ),
        )
        assert await summariser.run_once() == 1
        async with seed_five_turn_session.get_session_factory()() as db:
            row = (
                await db.execute(select(SessionRecord).filter_by(id="sess-long"))
            ).scalar_one()
            assert row.summary == "converged"
            assert row.summarized_at is not None  # left the eligibility set

    @pytest.mark.asyncio
    async def test_aclose_only_closes_owned_client(self):
        """The teardown callback closes a worker-owned client but never a
        test/DI-injected one."""
        injected = AsyncMock()
        injected.aclose = AsyncMock()
        s_injected = SessionSummarizer(
            settings=_settings(),
            session_factory=MagicMock(),
            http_client=injected,  # injected → not owned
        )
        await s_injected._aclose_owned_client()
        injected.aclose.assert_not_awaited()

        s_owned = SessionSummarizer(
            settings=_settings(),
            session_factory=MagicMock(),
        )
        owned = AsyncMock()
        owned.aclose = AsyncMock()
        s_owned._http_client = owned  # simulate lazy-created, owned client
        await s_owned._aclose_owned_client()
        owned.aclose.assert_awaited_once()


# ──────────────────────── Trace attribution (#344) ───────────────────────


class _RecordingSpan:
    """Minimal context-manager span that records set_attribute calls and
    returns a fixed trace_id, so we can assert attribution without standing
    up a real OTel SDK TracerProvider (which is process-global and fragile
    across the suite)."""

    def __init__(self, trace_id_int: int, *, valid: bool = True) -> None:
        self._trace_id_int = trace_id_int
        self._valid = valid
        self.attributes: dict[str, object] = {}

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def get_span_context(self):
        return SimpleNamespace(trace_id=self._trace_id_int, is_valid=self._valid)

    def __enter__(self) -> _RecordingSpan:
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _RecordingTracer:
    def __init__(self, trace_id_int: int, *, valid: bool = True) -> None:
        self.span = _RecordingSpan(trace_id_int, valid=valid)
        self.span_names: list[str] = []

    def start_as_current_span(self, name: str) -> _RecordingSpan:
        self.span_names.append(name)
        return self.span


class TestSpanTraceIdHex:
    """Unit coverage for the span→trace_id helper."""

    def test_valid_context_returns_32_hex(self):
        span = _RecordingSpan(0x0123456789ABCDEF0123456789ABCDEF, valid=True)
        assert _span_trace_id_hex(span) == "0123456789abcdef0123456789abcdef"

    def test_invalid_context_returns_none(self):
        # Non-recording span (tracing disabled) → invalid ctx → never fabricate.
        span = _RecordingSpan(0, valid=False)
        assert _span_trace_id_hex(span) is None

    def test_exception_returns_none(self):
        class _Boom:
            def get_span_context(self):
                raise RuntimeError("no span context")

        assert _span_trace_id_hex(_Boom()) is None


class TestTraceAttribution:
    """#344 — the background summariser must attribute its model call to the
    user/session it condenses and persist the trace_id onto the SessionRecord,
    so the row links back to its Tempo/Langfuse trace instead of an
    unattributed orphan root span."""

    _TID_INT = 0xABCDEF0123456789ABCDEF0123456789
    _TID_HEX = "abcdef0123456789abcdef0123456789"

    @pytest.mark.asyncio
    async def test_span_attributed_and_trace_id_persisted(
        self, seed_session_turns, monkeypatch
    ):
        tracer = _RecordingTracer(self._TID_INT)
        monkeypatch.setattr(ss_mod, "_tracer", tracer)

        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=seed_session_turns.get_session_factory(),
            http_client=_mock_summariser_client(
                summary="Capitals", key_points=["Paris", "Berlin"]
            ),
        )
        assert await summariser.run_once() == 1

        # The span was opened and attributed to the user/session/project.
        assert "session.summarise" in tracer.span_names
        assert tracer.span.attributes["langfuse.user.id"] == "user-1"
        assert tracer.span.attributes["user.id"] == "user-1"
        assert tracer.span.attributes["session.id"] == "sess-idle"
        assert tracer.span.attributes["audittrace.project"] == "P"
        assert tracer.span.attributes["langfuse.observation.type"] == "generation"

        # The persisted row links back to that exact trace.
        async with seed_session_turns.get_session_factory()() as db:
            row = (
                await db.execute(select(SessionRecord).filter_by(id="sess-idle"))
            ).scalar_one()
            assert row.trace_id == self._TID_HEX
            assert row.user_id == "user-1"

    @pytest.mark.asyncio
    async def test_trace_id_null_when_no_recording_span(
        self, seed_session_turns, monkeypatch
    ):
        """Tracing disabled (non-recording span) → trace_id left NULL rather
        than fabricated; the summary is still written."""
        tracer = _RecordingTracer(0, valid=False)
        monkeypatch.setattr(ss_mod, "_tracer", tracer)

        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=seed_session_turns.get_session_factory(),
            http_client=_mock_summariser_client(),
        )
        assert await summariser.run_once() == 1

        async with seed_session_turns.get_session_factory()() as db:
            row = (
                await db.execute(select(SessionRecord).filter_by(id="sess-idle"))
            ).scalar_one()
            assert row.trace_id is None
            assert row.summarized_at is not None

    @pytest.mark.asyncio
    async def test_sentinel_overflow_also_persists_trace_id(
        self, seed_session_turns, monkeypatch
    ):
        """The sentinel-overflow path is still part of the attributed span, so
        the sentinel row must carry the trace_id too."""
        tracer = _RecordingTracer(self._TID_INT)
        monkeypatch.setattr(ss_mod, "_tracer", tracer)

        summariser = SessionSummarizer(
            settings=_settings(
                summarizer_ctx_tokens=10,
                summarizer_ctx_reserve_tokens=2,
            ),
            session_factory=seed_session_turns.get_session_factory(),
            http_client=_mock_summariser_client_with_tokenize(tokens_per_call=999),
        )
        assert await summariser.run_once() == 1

        async with seed_session_turns.get_session_factory()() as db:
            row = (
                await db.execute(select(SessionRecord).filter_by(id="sess-idle"))
            ).scalar_one()
            assert row.model == "sentinel-skip-ctx-overflow-auto"
            assert row.trace_id == self._TID_HEX


# ── Branch-level hardening (#364) ────────────────────────────────────
#
# Each path below is the less-travelled half of a decision the summariser
# makes every cycle. Getting one wrong either fabricates a SessionRecord
# with no evidence behind it, drops a session out of the eligibility set
# without a summary, or points the LLM calls at a URL that does not exist.


class TestEmptyTurnSet:
    """A session that vanishes between eligibility and fetch must be skipped."""

    @pytest.mark.asyncio
    async def test_zero_turns_writes_no_session_record_and_calls_no_llm(
        self, pg_factory
    ):
        """Eligibility and turn-fetch are two separate transactions.

        A retention purge or a GDPR erasure can delete the interactions in
        between, leaving an ``EligibleSession`` whose turns are gone. Without
        the guard the summariser would send an empty transcript to the LLM
        and persist whatever came back — a SessionRecord with no interactions
        behind it, which is exactly the kind of unsupported row the audit
        trail must never contain (ADR-033). The correct behaviour is to write
        nothing and leave the session out of the sessions table entirely.
        """
        llm_calls: list[str] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            llm_calls.append(str(request.url))
            return httpx.Response(500, json={})

        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=pg_factory.get_session_factory(),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
        )

        await summariser._summarise_one(
            EligibleSession(
                session_id="sess-purged",
                user_id="user-1",
                project="P",
                last_ts=_iso_minutes_ago(30),
            )
        )

        # No LLM round trip was even attempted.
        assert llm_calls == []
        async with pg_factory.get_session_factory()() as db:
            count = (
                await db.execute(select(func.count()).select_from(SessionRecord))
            ).scalar_one()
        assert count == 0


class TestSummariserUrlWithoutV1Suffix:
    """``/props`` and ``/tokenize`` live at the server root, /v1 or not."""

    @pytest.mark.asyncio
    async def test_props_url_built_from_root_when_url_has_no_v1(self):
        """``summarizer_url`` is operator-configured and need not end in /v1.

        A llama-server pointed at ``http://llama:8080`` is a legitimate
        configuration. The suffix-strip is conditional, so the code must not
        mangle a URL that never had ``/v1``. Getting this wrong turns ctx
        auto-detection into a permanent 404, silently reverting to the
        configured ctx value — the exact condition behind the 2026-06-09
        infinite-retry incident.
        """
        seen: list[str] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={"n_ctx": 4096})

        summariser = SessionSummarizer(
            settings=_settings(summarizer_url="http://llama:8080"),
            session_factory=MagicMock(),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
        )

        assert await summariser._fetch_server_n_ctx() == 4096
        # No spurious "/v1" and no doubled slash.
        assert seen == ["http://llama:8080/props"]
        await summariser._aclose_owned_client()

    @pytest.mark.asyncio
    async def test_tokenize_url_built_from_root_when_url_has_no_v1(self):
        """Same suffix-strip, on the path the ctx guard depends on.

        If ``/tokenize`` 404s, ``_safe_count_tokens`` swallows it and falls
        back to the coarse char estimate — the guard keeps working but every
        prompt is sized by guesswork. Pinning the URL keeps the real
        tokeniser in the loop for non-/v1 deployments.
        """
        seen: list[str] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={"tokens": [1, 2, 3, 4, 5]})

        summariser = SessionSummarizer(
            settings=_settings(summarizer_url="http://llama:8080/"),
            session_factory=MagicMock(),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
        )

        assert await summariser._count_tokens("some transcript") == 5
        assert seen == ["http://llama:8080/tokenize"]
        await summariser._aclose_owned_client()


class TestSentinelOverflowUpsert:
    """A re-summarised overflow session must update its row, not duplicate it."""

    @pytest.mark.asyncio
    async def test_existing_session_record_is_updated_in_place(self, pg_factory):
        """``sessions.id`` is the primary key — a blind INSERT would raise.

        A session already carrying a summary can grow one oversized turn and
        become an overflow case on a later cycle. The sentinel write then has
        to UPDATE. If it inserted instead, the IntegrityError would propagate,
        the transaction would roll back, ``summarized_at`` would stay at its
        old value, and the session would be re-picked every cycle forever
        while never getting a correct row. This pins the update branch,
        including the trace_id refresh that links the row to the cycle that
        produced it.
        """
        async with pg_factory.get_session_factory()() as db:
            db.add(
                SessionRecord(
                    id="sess-grew",
                    project="P",
                    date="2026-07-01T00:00:00",
                    summary="an earlier, normal summary",
                    key_points=json.dumps(["old-point"]),
                    model="mistral-7b-summarizer",
                    user_id="user-1",
                    summarized_at=datetime(2026, 7, 1),
                    trace_id="0000000000000000000000000000aaaa",
                )
            )
            await db.commit()

        summariser = SessionSummarizer(
            settings=_settings(summarizer_ctx_tokens=8192),
            session_factory=pg_factory.get_session_factory(),
            http_client=_mock_summariser_client(),
        )
        es = EligibleSession(
            session_id="sess-grew",
            user_id="user-1",
            project="P",
            last_ts=_iso_minutes_ago(30),
        )

        await summariser._persist_sentinel_overflow(
            es, turn_count=7, trace_id="ffffffffffffffffffffffffffffbbbb"
        )

        async with pg_factory.get_session_factory()() as db:
            count = (
                await db.execute(select(func.count()).select_from(SessionRecord))
            ).scalar_one()
            row = (
                await db.execute(select(SessionRecord).filter_by(id="sess-grew"))
            ).scalar_one()

        # Still exactly one row — updated, not duplicated.
        assert count == 1
        assert row.model == "sentinel-skip-ctx-overflow-auto"
        assert "sentinel-skip-ctx-overflow-auto" in row.summary
        assert "7 turns" in row.summary
        # The stale summary and its key points were replaced, not appended to.
        assert "an earlier, normal summary" not in row.summary
        assert json.loads(row.key_points) == []
        # Traceability: the row now points at the cycle that wrote it.
        assert row.trace_id == "ffffffffffffffffffffffffffffbbbb"
        # summarized_at moved forward, so the row leaves the eligibility set.
        assert row.summarized_at > datetime(2026, 7, 1)


class TestStalenessWithUnparseableTimestamp:
    """A malformed ``last_ts`` must surface the session, not hide it."""

    def test_unparseable_last_ts_is_treated_as_stale(self):
        """``last_ts`` comes back as a raw string from the eligibility SQL.

        Dialect differences and hand-edited rows can produce something
        ``_coerce_datetime`` cannot parse. Defaulting to "not stale" would
        drop the session out of every future cycle — it would never be
        summarised and nobody would be told. Defaulting to stale means the
        row is picked up and any real problem surfaces loudly.
        """
        already_summarised = datetime(2026, 7, 1, 12, 0, 0)

        assert _is_stale("not-a-timestamp", already_summarised) is True
        assert _is_stale(None, already_summarised) is True

        # Contrast: a parseable last_ts older than summarized_at is NOT stale,
        # which is what makes the malformed case a deliberate default rather
        # than an accident of the same code path.
        assert _is_stale("2026-06-30T12:00:00", already_summarised) is False


class TestUnlabelledCodeFence:
    """A ``` fence without a language tag must still parse."""

    def test_bare_fenced_json_is_parsed(self):
        """Small instruct models fence their output with or without "json".

        The prompt asks for bare JSON; both fenced forms are observed
        violations. If only the ```json form were stripped, a bare-fenced
        response would fail ``json.loads``, ``_parse_llm_response`` would
        return None, ``summarized_at`` would stay NULL, and the session would
        be retried every cycle — burning a full LLM call each time for a
        response that was actually correct.
        """
        raw = '```\n{"summary": "bare fence", "key_points": ["a", "b"]}\n```'

        parsed = _parse_llm_response(raw)

        assert parsed == {"summary": "bare fence", "key_points": ["a", "b"]}
        # The literal "json" language tag is not treated as part of the body
        # in the labelled form either.
        assert _parse_llm_response(
            '```JSON\n{"summary": "upper", "key_points": []}\n```'
        ) == {"summary": "upper", "key_points": []}


# ──────────── v1.29.5 — ownership-scoping security fix (G1–G9, G7a–e) ────────
#
# Guard numbering follows the ratified build digest (A1 > spec). Each test
# below is the PERMANENT regression guard; the per-guard RED/GREEN neuter
# table (production code reverted one mechanism at a time, test run,
# restored) lives in the build record, not in this file.


class _SpyCounter:
    """Records every ``add(amount, attributes)`` call verbatim, so a test
    can assert exactly what the production code emitted without standing
    up a real OTel MeterProvider (which is a process-global, set-once
    singleton — see the build record for why that rules out
    InMemoryMetricReader for a shared test process). Same idiom as
    ``test_memory_tool_handlers.py``'s ``_SpyInstrument``."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, dict]] = []

    def add(self, amount: float, attributes: dict | None = None) -> None:
        self.calls.append((amount, dict(attributes or {})))


class _SpyGauge:
    """Records every ``set(amount, attributes)`` call verbatim."""

    def __init__(self) -> None:
        self.values: list[tuple[float, dict]] = []

    def set(self, amount: float, attributes: dict | None = None) -> None:
        self.values.append((amount, dict(attributes or {})))


@pytest.fixture
def collision_spies(monkeypatch):
    """Swap the module-level collision counter/gauge for spies so tests
    assert on exactly what the production code calls, not a duplicated
    instrument."""
    counter = _SpyCounter()
    gauge = _SpyGauge()
    monkeypatch.setattr(ss_mod, "_session_collisions_counter", counter)
    monkeypatch.setattr(ss_mod, "_session_collisions_gauge", gauge)
    return counter, gauge


@pytest_asyncio.fixture
async def colliding_group(pg_factory):
    """A owns 'shared-sid' (already summarised, 89m ago); B's turns land
    under the SAME session_id 40 minutes ago — both idle. The classic
    cross-user collision shape (spec §2 P1)."""
    async with pg_factory.get_session_factory()() as db:
        db.add_all(
            [
                InteractionRecord(
                    project="P",
                    source="chat",
                    question="a-q",
                    answer="a-a",
                    timestamp=_iso_minutes_ago(90),
                    session_id="shared-sid",
                    user_id="user-A",
                ),
                InteractionRecord(
                    project="P",
                    source="chat",
                    question="b-q",
                    answer="b-a",
                    timestamp=_iso_minutes_ago(40),
                    session_id="shared-sid",
                    user_id="user-B",
                ),
                SessionRecord(
                    id="shared-sid",
                    project="P",
                    date=_iso_minutes_ago(85),
                    summary="A-ORIGINAL summary",
                    key_points=json.dumps(["a-point"]),
                    model="mistral-7b-summarizer",
                    user_id="user-A",
                    summarized_at=datetime.now() - timedelta(minutes=89),
                ),
            ]
        )
        await db.commit()
    return pg_factory


class TestIsForeignOwner:
    """R1 — the one place the ownership rule is written."""

    def test_none_owner_is_foreign(self):
        assert _is_foreign_owner(None, "user-1") is True

    def test_different_owner_is_foreign(self):
        assert _is_foreign_owner("user-2", "user-1") is True

    def test_same_owner_is_not_foreign(self):
        assert _is_foreign_owner("user-1", "user-1") is False


class TestIsSessionsPkViolation:
    """S2 / A1-F3 — the narrowed catch, as a pure-function unit."""

    def test_sqlite_sessions_id_message_is_pk_violation(self):
        exc = IntegrityError(
            "x", params=None, orig=Exception("UNIQUE constraint failed: sessions.id")
        )
        assert _is_sessions_pk_violation(exc) is True

    def test_sqlite_other_unique_message_is_not_pk_violation(self):
        exc = IntegrityError(
            "x",
            params=None,
            orig=Exception("UNIQUE constraint failed: sessions.trace_id"),
        )
        assert _is_sessions_pk_violation(exc) is False

    def test_non_integrity_error_is_not_pk_violation(self):
        assert _is_sessions_pk_violation(ValueError("nope")) is False

    def test_postgres_shaped_exception_is_pk_violation(self):
        class _FakeAsyncpgOrigError(Exception):
            sqlstate = "23505"
            constraint_name = "sessions_pkey"

        exc = IntegrityError("x", params=None, orig=_FakeAsyncpgOrigError("dup"))
        assert _is_sessions_pk_violation(exc) is True

    def test_postgres_shaped_exception_wrong_constraint_is_not_pk_violation(self):
        class _FakeAsyncpgOrigError(Exception):
            sqlstate = "23505"
            constraint_name = "some_other_pkey"

        exc = IntegrityError("x", params=None, orig=_FakeAsyncpgOrigError("dup"))
        assert _is_sessions_pk_violation(exc) is False


class TestOwnershipScopingG1toG3:
    """G1 (owner row intact), G2 (zero LLM calls), G3 (no slot
    displacement)."""

    @pytest.mark.asyncio
    async def test_g1_owner_row_intact_structurally(self, colliding_group):
        """The whole cycle, end to end: B's group never becomes a
        candidate at all, so A's row cannot be touched and no LLM call
        is spent on it."""
        llm_calls: list[str] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            llm_calls.append(str(request.url))
            return httpx.Response(200, json={"choices": []})

        async with colliding_group.get_session_factory()() as db:
            before = (
                await db.execute(select(SessionRecord).filter_by(id="shared-sid"))
            ).scalar_one()
            before_snapshot = (
                before.summary,
                before.key_points,
                before.date,
                before.model,
                before.user_id,
                before.summarized_at,
                before.trace_id,
            )

        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=colliding_group.get_session_factory(),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
        )
        count = await summariser.run_once()

        assert count == 0
        assert llm_calls == []
        async with colliding_group.get_session_factory()() as db:
            after = (
                await db.execute(select(SessionRecord).filter_by(id="shared-sid"))
            ).scalar_one()
            after_snapshot = (
                after.summary,
                after.key_points,
                after.date,
                after.model,
                after.user_id,
                after.summarized_at,
                after.trace_id,
            )
        assert after_snapshot == before_snapshot

    @pytest.mark.asyncio
    async def test_g1_persist_direct_call_cannot_touch_foreign_row(
        self, colliding_group
    ):
        """R3 in isolation — even if ``_persist`` is reached directly for
        B's ``EligibleSession`` (as if R2's SQL exclusion had not fired),
        the owner-scoped loader means it still cannot UPDATE A's row: the
        write attempts to INSERT its own row under the same id, which
        collides with A's PK and is swallowed as a foreign-owner race
        (G5), leaving A's row untouched."""
        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=colliding_group.get_session_factory(),
            http_client=_mock_summariser_client(),
        )
        es = EligibleSession(
            session_id="shared-sid",
            user_id="user-B",
            project="P",
            last_ts=_iso_minutes_ago(40),
        )

        await summariser._persist(es, {"summary": "B-DIRECT", "key_points": []})

        async with colliding_group.get_session_factory()() as db:
            rows = (
                (await db.execute(select(SessionRecord).filter_by(id="shared-sid")))
                .scalars()
                .all()
            )
        assert len(rows) == 1
        assert rows[0].summary == "A-ORIGINAL summary"
        assert rows[0].user_id == "user-A"

    @pytest.mark.asyncio
    async def test_g2_zero_llm_calls_for_colliding_group_two_cycles(
        self, colliding_group
    ):
        llm_calls: list[str] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            llm_calls.append(str(request.url))
            return httpx.Response(200, json={"choices": []})

        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=colliding_group.get_session_factory(),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
        )
        count1 = await summariser.run_once()
        count2 = await summariser.run_once()

        assert count1 == 0
        assert count2 == 0
        assert llm_calls == []

    @pytest.mark.asyncio
    async def test_g3_clean_group_not_displaced_by_colliding_group(self, pg_factory):
        """max_per_cycle=1: the colliding group must never consume the
        one slot — it is excluded before slot assignment, so the clean
        session gets summarised instead of being starved.

        B's turn is seeded OLDER than D's (80m vs 50m ago) deliberately:
        the eligibility ordering is ``(summarized_at IS NULL) DESC,
        last_ts ASC`` (oldest-idle-first), so if R2(iii)'s exclusion were
        removed, B's (older, never-summarised) group would win the ONE
        slot ahead of D's — making the neuter observable. With B and D's
        ages reversed (B newer than D) the neuter would be invisible here
        because D would win the slot on age alone regardless of whether B
        is excluded.
        """
        async with pg_factory.get_session_factory()() as db:
            db.add_all(
                [
                    InteractionRecord(
                        project="P",
                        source="chat",
                        question="a-q",
                        answer="a-a",
                        timestamp=_iso_minutes_ago(90),
                        session_id="shared-sid3",
                        user_id="user-A",
                    ),
                    InteractionRecord(
                        project="P",
                        source="chat",
                        question="b-q",
                        answer="b-a",
                        timestamp=_iso_minutes_ago(80),
                        session_id="shared-sid3",
                        user_id="user-B",
                    ),
                    SessionRecord(
                        id="shared-sid3",
                        project="P",
                        date=_iso_minutes_ago(85),
                        summary="A-ORIGINAL",
                        key_points="[]",
                        model="m",
                        user_id="user-A",
                        summarized_at=datetime.now() - timedelta(minutes=89),
                    ),
                    InteractionRecord(
                        project="P",
                        source="chat",
                        question="d-q",
                        answer="d-a",
                        timestamp=_iso_minutes_ago(50),
                        session_id="clean-sid",
                        user_id="user-D",
                    ),
                ]
            )
            await db.commit()

        summariser = SessionSummarizer(
            settings=_settings(summarizer_max_per_cycle=1),
            session_factory=pg_factory.get_session_factory(),
            http_client=_mock_summariser_client(summary="D-NEW"),
        )
        count = await summariser.run_once()

        assert count == 1
        async with pg_factory.get_session_factory()() as db:
            d_row = (
                await db.execute(select(SessionRecord).filter_by(id="clean-sid"))
            ).scalar_one()
            assert d_row.summary == "D-NEW"


class TestOwnershipScopingG4:
    """G4 — the eligibility→LLM-call race window (R4)."""

    @pytest.mark.asyncio
    async def test_g4_pre_llm_race_blocks_before_any_llm_call(
        self, pg_factory, collision_spies
    ):
        """A foreign row appears for this id AFTER eligibility (a separate
        read) but BEFORE the LLM call — ``_summarise_one``'s pre-flight
        check must catch it without spending an LLM call."""
        counter, _ = collision_spies
        async with pg_factory.get_session_factory()() as db:
            db.add(
                InteractionRecord(
                    project="P",
                    source="chat",
                    question="c-q",
                    answer="c-a",
                    timestamp=_iso_minutes_ago(40),
                    session_id="race-sid",
                    user_id="user-C",
                )
            )
            db.add(
                SessionRecord(
                    id="race-sid",
                    project="P",
                    date=_iso_minutes_ago(1),
                    summary="LATE-FOREIGN",
                    key_points="[]",
                    model="m",
                    user_id="user-LATE",
                    summarized_at=datetime.now() - timedelta(minutes=1),
                )
            )
            await db.commit()

        llm_calls: list[str] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            llm_calls.append(str(request.url))
            return httpx.Response(200, json={"choices": []})

        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=pg_factory.get_session_factory(),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
        )
        es = EligibleSession(
            session_id="race-sid",
            user_id="user-C",
            project="P",
            last_ts=_iso_minutes_ago(40),
        )
        await summariser._summarise_one(es)

        assert llm_calls == []
        assert counter.calls == [(1, {"stage": "pre_llm"})]
        async with pg_factory.get_session_factory()() as db:
            row = (
                await db.execute(select(SessionRecord).filter_by(id="race-sid"))
            ).scalar_one()
            assert row.summary == "LATE-FOREIGN"

    @pytest.mark.asyncio
    async def test_g4_never_summarised_session_is_not_blocked(
        self, pg_factory, collision_spies
    ):
        """Regression guard for the lookup's own ambiguity (R4/F4 fix):
        a session with NO sessions row at all (the ordinary
        never-summarised case) must NOT be treated as foreign — only a
        row that actually EXISTS under a different owner counts."""
        counter, _ = collision_spies
        async with pg_factory.get_session_factory()() as db:
            db.add(
                InteractionRecord(
                    project="P",
                    source="chat",
                    question="f-q",
                    answer="f-a",
                    timestamp=_iso_minutes_ago(40),
                    session_id="fresh-sid",
                    user_id="user-F",
                )
            )
            await db.commit()

        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=pg_factory.get_session_factory(),
            http_client=_mock_summariser_client(summary="F-OK"),
        )
        count = await summariser.run_once()

        assert count == 1
        assert counter.calls == []
        async with pg_factory.get_session_factory()() as db:
            row = (
                await db.execute(select(SessionRecord).filter_by(id="fresh-sid"))
            ).scalar_one()
            assert row.summary == "F-OK"
            assert row.user_id == "user-F"


class TestOwnershipScopingG5WriteConflicts:
    """G5 — the sessions-PK race, classified by current owner (R3/S2/S3)."""

    @pytest.mark.asyncio
    async def test_g5a_foreign_pk_race_counted_and_warned(
        self, pg_factory, collision_spies, caplog
    ):
        counter, _ = collision_spies
        async with pg_factory.get_session_factory()() as db:
            db.add(
                SessionRecord(
                    id="race2-sid",
                    project="P",
                    date="d",
                    summary="OTHER-OWNED",
                    key_points="[]",
                    model="m",
                    user_id="user-OTHER",
                    summarized_at=datetime.now(),
                )
            )
            await db.commit()

        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=pg_factory.get_session_factory(),
            http_client=_mock_summariser_client(),
        )
        es = EligibleSession(
            session_id="race2-sid",
            user_id="user-C",
            project="P",
            last_ts=_iso_minutes_ago(40),
        )

        with caplog.at_level(logging.WARNING):
            await summariser._persist(es, {"summary": "C-ATTEMPT", "key_points": []})

        assert counter.calls == [(1, {"stage": "persist_pk"})]
        assert "persist race, foreign owner" in caplog.text
        async with pg_factory.get_session_factory()() as db:
            row = (
                await db.execute(select(SessionRecord).filter_by(id="race2-sid"))
            ).scalar_one()
            assert row.summary == "OTHER-OWNED"

    @pytest.mark.asyncio
    async def test_g5b_non_pk_integrity_error_escapes(
        self, seed_session_turns, monkeypatch, collision_spies
    ):
        """Only the ``sessions`` PK violation is swallowed. A NOT NULL
        violation (or any other integrity error) must propagate so it
        surfaces via ``run_once``'s own per-session ``logger.exception``
        instead of being silently miscounted as an ownership race."""
        counter, _ = collision_spies

        class _FakeNotNullOrigError(Exception):
            pass

        async def _raise_not_null(self, *a, **kw):
            raise IntegrityError(
                "NOT NULL constraint failed: sessions.summary",
                params=None,
                orig=_FakeNotNullOrigError(
                    "NOT NULL constraint failed: sessions.summary"
                ),
            )

        monkeypatch.setattr(AsyncSession, "commit", _raise_not_null)

        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=seed_session_turns.get_session_factory(),
            http_client=_mock_summariser_client(),
        )
        count = await summariser.run_once()

        # The attempt failed and was logged by run_once's own per-session
        # try/except — it must NOT be swallowed/counted as a race.
        assert count == 0
        assert counter.calls == []
        async with seed_session_turns.get_session_factory()() as db:
            row = (
                await db.execute(select(SessionRecord).filter_by(id="sess-idle"))
            ).scalar_one_or_none()
            assert row is None

    @pytest.mark.asyncio
    async def test_g5c_same_owner_pk_race_logs_info_no_count(
        self, pg_factory, collision_spies, caplog, monkeypatch
    ):
        """A replica that committed the SAME owner's row between this
        call's (simulated-stale) lookup and its INSERT must be an INFO,
        uncounted no-op — not a security event."""
        counter, _ = collision_spies
        async with pg_factory.get_session_factory()() as db:
            db.add(
                SessionRecord(
                    id="replica-sid",
                    project="P",
                    date="d",
                    summary="REPLICA-WON",
                    key_points="[]",
                    model="m",
                    user_id="user-D",
                    summarized_at=datetime.now(),
                )
            )
            await db.commit()

        # Simulate the race window: the loader's read happened BEFORE the
        # replica's row landed, so it (falsely, for this one call) saw no
        # existing row.
        monkeypatch.setattr(ss_mod, "_load_own_row", AsyncMock(return_value=None))

        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=pg_factory.get_session_factory(),
            http_client=_mock_summariser_client(),
        )
        es = EligibleSession(
            session_id="replica-sid",
            user_id="user-D",
            project="P",
            last_ts=_iso_minutes_ago(40),
        )

        with caplog.at_level(logging.INFO):
            await summariser._persist(es, {"summary": "D-LATE", "key_points": []})

        assert counter.calls == []
        assert "persist race, same-owner replica" in caplog.text
        async with pg_factory.get_session_factory()() as db:
            row = (
                await db.execute(select(SessionRecord).filter_by(id="replica-sid"))
            ).scalar_one()
            assert row.summary == "REPLICA-WON"


class TestOwnershipScopingG6:
    """G6 — a legacy NULL-owner sessions row is never adopted (R1/R2(iii))."""

    @pytest.mark.asyncio
    async def test_g6_null_owner_row_never_adopted(self, pg_factory, collision_spies):
        counter, gauge = collision_spies
        async with pg_factory.get_session_factory()() as db:
            db.add_all(
                [
                    InteractionRecord(
                        project="P",
                        source="chat",
                        question="e-q",
                        answer="e-a",
                        timestamp=_iso_minutes_ago(40),
                        session_id="legacy-sid",
                        user_id="user-E",
                    ),
                    SessionRecord(
                        id="legacy-sid",
                        project="P",
                        date=_iso_minutes_ago(100),
                        summary="LEGACY-NULL-OWNER",
                        key_points="[]",
                        model="m",
                        user_id=None,
                        summarized_at=datetime.now() - timedelta(minutes=200),
                    ),
                ]
            )
            await db.commit()

        llm_calls: list[str] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            llm_calls.append(str(request.url))
            return httpx.Response(200, json={"choices": []})

        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=pg_factory.get_session_factory(),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
        )
        count = await summariser.run_once()

        assert count == 0
        assert llm_calls == []
        assert counter.calls == [(1, {"stage": "eligibility"})]
        assert gauge.values[-1] == (1, {})
        async with pg_factory.get_session_factory()() as db:
            row = (
                await db.execute(select(SessionRecord).filter_by(id="legacy-sid"))
            ).scalar_one()
            assert row.summary == "LEGACY-NULL-OWNER"
            assert row.user_id is None


class TestOwnershipScopingG7Metrics:
    """G7a–e — the first-sighting counter, the per-cycle gauge, the
    once-only WARNING, the per-stage event points, and the restart
    re-count (R5′, ADDENDUM A1 §3)."""

    @pytest.mark.asyncio
    async def test_g7a_first_sighting_counter_counts_once_across_cycles(
        self, colliding_group, collision_spies
    ):
        counter, _ = collision_spies
        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=colliding_group.get_session_factory(),
            http_client=_mock_summariser_client(),
        )
        for _ in range(3):
            await summariser.run_once()

        eligibility_adds = [
            c for c in counter.calls if c[1].get("stage") == "eligibility"
        ]
        assert eligibility_adds == [(1, {"stage": "eligibility"})]

    @pytest.mark.asyncio
    async def test_g7b_gauge_tracks_excluded_groups_per_cycle(
        self, colliding_group, collision_spies
    ):
        _, gauge = collision_spies
        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=colliding_group.get_session_factory(),
            http_client=_mock_summariser_client(),
        )
        for _ in range(3):
            await summariser.run_once()
        assert [v for v, _ in gauge.values] == [1, 1, 1]

        async with colliding_group.get_session_factory()() as db:
            row = (
                await db.execute(select(SessionRecord).filter_by(id="shared-sid"))
            ).scalar_one()
            await db.delete(row)
            await db.commit()
        await summariser.run_once()
        assert gauge.values[-1] == (0, {})

    @pytest.mark.asyncio
    async def test_g7c_warning_logged_once_not_per_cycle(self, colliding_group, caplog):
        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=colliding_group.get_session_factory(),
            http_client=_mock_summariser_client(),
        )
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                await summariser.run_once()

        collision_warnings = [
            r
            for r in caplog.records
            if "ownership collision" in r.getMessage()
            and "shared-sid" in r.getMessage()
        ]
        assert len(collision_warnings) == 1

    @pytest.mark.asyncio
    async def test_g7e_restart_re_counts_once_then_silent(
        self, colliding_group, collision_spies
    ):
        """A fresh ``SessionSummarizer`` instance (the restart shape) has
        its OWN seen-set, so it re-counts a still-standing collision
        exactly once, then goes silent again — the seen-set must be
        per-instance, never module-global."""
        counter, _ = collision_spies

        def _eligibility_count() -> int:
            return len([c for c in counter.calls if c[1].get("stage") == "eligibility"])

        s1 = SessionSummarizer(
            settings=_settings(),
            session_factory=colliding_group.get_session_factory(),
            http_client=_mock_summariser_client(),
        )
        await s1.run_once()
        assert _eligibility_count() == 1

        s2 = SessionSummarizer(
            settings=_settings(),
            session_factory=colliding_group.get_session_factory(),
            http_client=_mock_summariser_client(),
        )
        await s2.run_once()
        assert _eligibility_count() == 2  # the restart's one re-count

        await s2.run_once()
        assert _eligibility_count() == 2  # silent again


class TestOwnershipScopingG8StructuralInvariants:
    """G8 — one predicate, three readers; the loader consolidation."""

    @staticmethod
    def _source() -> str:
        return inspect.getsource(ss_mod)

    def test_g8_is_foreign_owner_has_exactly_three_call_sites(self):
        """``_is_foreign_owner`` is the ONE place the ownership rule is
        written in Python; it must be called from exactly the three
        sites R1/G8 name: the eligibility collision pass, the pre-LLM
        check, and the persist-PK-conflict handler. A fourth call site
        (or a comparison inlined elsewhere instead of calling it) is the
        exact drift this guard exists to catch."""
        src = self._source()
        calls = re.findall(r"(?<!def )_is_foreign_owner\(", src)
        assert len(calls) == 3

    def test_g8_select_sessionrecord_confined_to_the_two_loaders(self):
        """Every raw ``select(SessionRecord`` in the module must live
        inside ``_load_own_row`` or ``_owner_of`` — the two sanctioned,
        owner-aware readers. A third site would bypass R3's loader
        consolidation."""
        src = self._source()
        sites = [m.start() for m in re.finditer(r"select\(SessionRecord", src)]
        assert len(sites) == 2


class TestOwnershipScopingG9:
    """G9 — a user's own group is still re-summarised after a new turn,
    even when a FOREIGN collision for the same id also exists (R2(ii)'s
    scoped join).

    **Build-record finding, not a deviation from the fix:** R2(ii)'s join
    condition (``AND s.user_id = sub.user_id``) is NOT independently
    single-kill-provable once R2(iii)'s ``NOT EXISTS`` exclusion is in
    place — verified empirically. ``sessions.id`` is the table's sole
    PRIMARY KEY, so at most one row can exist per id; whenever R2(iii)
    has not already excluded a group, that group's owner is, by
    construction, either absent or the one existing row's owner — so an
    id-only join and an id+owner-scoped join resolve to the identical
    row either way. Reverting R2(ii) ALONE left this test GREEN (the
    neuter was silently absorbed by R2(iii)). Reverting R2(ii) AND
    R2(iii) TOGETHER (the literal pre-fix query shape) turns it RED via
    ``count`` going from 1 to 2 — B's structurally-foreign group becomes
    a candidate again, is attempted (and correctly blocked by R4 at the
    pre-LLM check), but still counts as an attempted session in
    ``run_once``'s return (F10's established semantics). R2(ii) is kept
    in the code as defence-in-depth / self-documentation (it states
    explicitly, at the SQL level, "this row must belong to this group"),
    not because it is load-bearing on its own.
    """

    @pytest.mark.asyncio
    async def test_g9_owners_own_group_still_summarised_after_new_turn(
        self, colliding_group
    ):
        async with colliding_group.get_session_factory()() as db:
            db.add(
                InteractionRecord(
                    project="P",
                    source="chat",
                    question="a-q2",
                    answer="a-a2",
                    timestamp=_iso_minutes_ago(20),
                    session_id="shared-sid",
                    user_id="user-A",
                )
            )
            await db.commit()

        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=colliding_group.get_session_factory(),
            http_client=_mock_summariser_client(summary="A-REFRESHED"),
        )
        count = await summariser.run_once()

        assert count == 1
        async with colliding_group.get_session_factory()() as db:
            row = (
                await db.execute(select(SessionRecord).filter_by(id="shared-sid"))
            ).scalar_one()
            assert row.summary == "A-REFRESHED"
            assert row.user_id == "user-A"


class TestOwnershipScopingG10DiffPin:
    """G10 — the fleet contract. Not a behavioural test: a build-record
    field, pinned here so CI itself also fails if the invariant breaks."""

    def test_g10_chat_py_untouched_since_v1_29_3(self):
        import subprocess

        result = subprocess.run(
            [
                "git",
                "diff",
                "--quiet",
                "v1.29.3",
                "HEAD",
                "--",
                "src/audittrace/routes/chat.py",
            ],
            cwd=Path(__file__).resolve().parent.parent,
            check=False,
        )
        assert result.returncode == 0, (
            "routes/chat.py changed since v1.29.3 — out of scope for the "
            "v1.29.5 summariser ownership-scoping fix"
        )
