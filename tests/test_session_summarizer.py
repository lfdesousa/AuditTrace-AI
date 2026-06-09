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
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from audittrace.config import Settings
from audittrace.db.models import InteractionRecord, SessionRecord
from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.services.session_summarizer import (
    SessionSummarizer,
    _format_transcript,
    _parse_llm_response,
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
