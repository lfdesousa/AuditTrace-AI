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

import httpx
import pytest

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


@pytest.fixture
def pg_factory() -> InMemoryPostgresFactory:
    return InMemoryPostgresFactory()


@pytest.fixture
def seed_session_turns(pg_factory):
    """Seed a session idle for 30 minutes (comfortably > 15m threshold)."""
    session = pg_factory.get_session_factory()()
    try:
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
        session.commit()
    finally:
        session.close()
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

        db = seed_session_turns.get_session_factory()()
        try:
            row = db.query(SessionRecord).filter_by(id="sess-idle").one()
            assert row.summary == "Capitals of France and Germany"
            assert json.loads(row.key_points) == ["Paris", "Berlin"]
            assert row.model == "mistral-7b-summarizer"
            assert row.user_id == "user-1"
            assert row.summarized_at is not None
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_recent_session_not_eligible(self, pg_factory):
        """A session idle only 5 minutes must NOT be summarised — still
        inside the 15-minute idle window."""
        db = pg_factory.get_session_factory()()
        try:
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
            db.commit()
        finally:
            db.close()

        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=pg_factory.get_session_factory(),
            http_client=_mock_summariser_client(),
        )
        count = await summariser.run_once()
        assert count == 0

        db = pg_factory.get_session_factory()()
        try:
            assert (
                db.query(SessionRecord).filter_by(id="sess-recent").one_or_none()
                is None
            )
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_up_to_date_session_skipped(self, pg_factory):
        """summarized_at >= last_ts → no new work."""
        db = pg_factory.get_session_factory()()
        try:
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
            db.commit()
        finally:
            db.close()

        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=pg_factory.get_session_factory(),
            http_client=_mock_summariser_client(summary="NEW"),
        )
        count = await summariser.run_once()
        assert count == 0

        db = pg_factory.get_session_factory()()
        try:
            row = db.query(SessionRecord).filter_by(id="sess-fresh").one()
            assert row.summary == "Prior summary"  # untouched
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_stale_session_re_summarised(self, pg_factory):
        """summarized_at < last_ts → re-summarise and UPDATE in place."""
        last_ts = _iso_minutes_ago(40)
        db = pg_factory.get_session_factory()()
        try:
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
            db.commit()
        finally:
            db.close()

        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=pg_factory.get_session_factory(),
            http_client=_mock_summariser_client(summary="Refreshed summary"),
        )
        count = await summariser.run_once()
        assert count == 1

        db = pg_factory.get_session_factory()()
        try:
            # Still exactly one row — updated, not duplicated.
            rows = db.query(SessionRecord).filter_by(id="sess-stale").all()
            assert len(rows) == 1
            assert rows[0].summary == "Refreshed summary"
            assert rows[0].summarized_at is not None
            assert rows[0].summarized_at > datetime.now() - timedelta(minutes=1)
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_null_session_id_ignored(self, pg_factory):
        """Interactions with NULL session_id must not reach the LLM."""
        db = pg_factory.get_session_factory()()
        try:
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
            db.commit()
        finally:
            db.close()

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
        db = pg_factory.get_session_factory()()
        try:
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
            db.commit()
        finally:
            db.close()

        summariser = SessionSummarizer(
            settings=_settings(summarizer_max_per_cycle=2),
            session_factory=pg_factory.get_session_factory(),
            http_client=_mock_summariser_client(),
        )
        count = await summariser.run_once()
        assert count == 2

        db = pg_factory.get_session_factory()()
        try:
            assert db.query(SessionRecord).count() == 2
        finally:
            db.close()

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

        db = seed_session_turns.get_session_factory()()
        try:
            row = db.query(SessionRecord).filter_by(id="sess-idle").one_or_none()
            assert row is None
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_never_summarised_beats_backlog_of_up_to_date(self, pg_factory):
        """Regression guard (2026-04-15): with max_per_cycle=10 and a
        backlog of 20 already-up-to-date sessions older than an
        unsummarised session, the old ordering (``last_ts ASC`` only)
        starved the unsummarised row because the oldest 30 fetched
        rows were all fresh-enough-already. Fixed by
        ``ORDER BY (summarized_at IS NULL) DESC`` — unsummarised rows
        are prioritised regardless of their relative age."""
        db = pg_factory.get_session_factory()()
        try:
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
            db.commit()
        finally:
            db.close()

        summariser = SessionSummarizer(
            settings=_settings(summarizer_max_per_cycle=10),
            session_factory=pg_factory.get_session_factory(),
            http_client=_mock_summariser_client(),
        )
        await summariser.run_once()

        db = pg_factory.get_session_factory()()
        try:
            # The unsummarised session must have been picked despite
            # being newer than the 20-session backlog.
            assert (
                db.query(SessionRecord)
                .filter_by(id="sess-fresh-but-unsummarised")
                .one_or_none()
                is not None
            )
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_per_user_attribution(self, pg_factory):
        """Two idle sessions for different users → each SessionRecord
        carries its own user_id (no cross-user leakage)."""
        db = pg_factory.get_session_factory()()
        try:
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
            db.commit()
        finally:
            db.close()

        summariser = SessionSummarizer(
            settings=_settings(),
            session_factory=pg_factory.get_session_factory(),
            http_client=_mock_summariser_client(),
        )
        assert await summariser.run_once() == 2

        db = pg_factory.get_session_factory()()
        try:
            alice = db.query(SessionRecord).filter_by(id="sess-alice").one()
            bob = db.query(SessionRecord).filter_by(id="sess-bob").one()
            assert alice.user_id == "alice"
            assert bob.user_id == "bob"
        finally:
            db.close()


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

        db = seed_session_turns.get_session_factory()()
        try:
            row = db.query(SessionRecord).filter_by(id="sess-idle").one()
            assert json.loads(row.key_points) == []
        finally:
            db.close()

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

        db = seed_session_turns.get_session_factory()()
        try:
            assert (
                db.query(SessionRecord).filter_by(id="sess-idle").one_or_none() is None
            )
        finally:
            db.close()


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
