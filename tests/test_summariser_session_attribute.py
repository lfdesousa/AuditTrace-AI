"""LANGFUSE-WU-3 (v1.31.0 WP-6, A16): the summariser's per-session root span
carries the session it condenses.

Characterisation-plus-guard. At the base commit ``_summarise_one`` already sets
``session.id`` (and ``langfuse.user.id`` / ``user.id`` / ``audittrace.project`` /
``langfuse.observation.type``) on its ``session.summarise`` span, AFTER the span
starts (``session_summarizer.py`` ~L489-494). WU-2 G5 (WP-9, Phase 2) OWNS moving
those same keys into ``start_as_current_span(attributes=...)``; WP-6 therefore does
NOT touch the summariser code, it pins the attribute so the G5 move cannot drop it.

Mocks declared: the summariser LLM is an ``httpx.MockTransport``; the DB is the
in-memory SQLAlchemy factory (dialect-agnostic logic, as ``test_session_summarizer``);
the tracer is a local SDK provider with an ``InMemorySpanExporter`` swapped in for
the module-level ``_tracer`` (the global provider is set-once). No clock assertion.

Neuter (alone): delete the ``span.set_attribute("session.id", ...)`` line -> RED.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from audittrace.db.models import InteractionRecord
from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.services import session_summarizer as ss_mod
from audittrace.services.session_summarizer import SessionSummarizer
from tests.test_session_summarizer import (
    _iso_minutes_ago,
    _mock_summariser_client,
    _settings,
)

SESSION = "sess-wu3-attribute"
USER = "user-wu3"


async def test_the_summarise_root_span_carries_the_session_it_condenses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(ss_mod, "_tracer", provider.get_tracer("wu3-test"))

    factory = InMemoryPostgresFactory()
    await factory.create_schema()
    async with factory.get_session_factory()() as db:
        db.add_all(
            InteractionRecord(
                project="P",
                source="chat",
                question=f"q{i}",
                answer=f"a{i}",
                timestamp=_iso_minutes_ago(40 - i),
                session_id=SESSION,
                user_id=USER,
            )
            for i in range(2)
        )
        await db.commit()

    summariser = SessionSummarizer(
        settings=_settings(),
        session_factory=factory.get_session_factory(),
        http_client=_mock_summariser_client(),
    )
    assert await summariser.run_once() == 1  # positive control: it ran

    roots = [s for s in exporter.get_finished_spans() if s.name == "session.summarise"]
    assert len(roots) == 1
    attrs = dict(roots[0].attributes or {})
    assert attrs["session.id"] == SESSION
    assert attrs["langfuse.user.id"] == USER
    assert attrs["langfuse.observation.type"] == "generation"
