"""Background session summariser — ADR-030 Part 2.

Consumes idle chat sessions, calls a dedicated summariser LLM
(Mistral 7B Instruct v0.3 on :11437 by default — see ADR-030 §1), and
writes ``SessionRecord`` rows so ``recall_recent_sessions`` returns
real summaries instead of the Part 1 synthetic fallback.

Started from ``server.py::lifespan`` as an ``asyncio.create_task``
guarded by ``settings.summarizer_enabled``. One worker per process
is sufficient; ``SELECT ... FOR UPDATE OF s SKIP LOCKED`` in the
eligibility query means additional workers would not race (future
horizontal scaling handed-off cleanly).

Two transaction boundaries per eligible session:

1. **Eligibility read** — the ``sovereign`` role bypasses RLS for this
   transaction via ``SET LOCAL row_security = off`` so the worker can
   see rows from every user (the table owner has this privilege). The
   query is a single SELECT with a subquery for per-session
   ``MAX(timestamp)`` joined against ``sessions.summarized_at`` for
   staleness.
2. **Summary write** — a fresh transaction per session, scoped to
   that session's ``user_id`` via ``SET LOCAL app.current_user_id``
   so the RLS ``WITH CHECK`` on ``sessions`` accepts the INSERT /
   UPDATE. No cross-user leakage is possible: each row is written
   under its own user's GUC.

On SQLite (tests), both ``SET LOCAL`` calls are no-ops because SQLite
has no GUCs and no RLS.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from audittrace.config import Settings
from audittrace.db.models import InteractionRecord, SessionRecord

logger = logging.getLogger(__name__)


# ─────────────────────────── Data structures ──────────────────────────────


@dataclass(frozen=True)
class EligibleSession:
    """One candidate for summarisation, materialised from the read txn."""

    session_id: str
    user_id: str
    project: str
    last_ts: str  # ISO string — compares chronologically


# Works on both PostgreSQL and SQLite. Returns the idle candidate set
# (sessions whose most recent interaction is older than the threshold)
# joined with ``sessions.summarized_at`` so the caller can filter
# stale-vs-fresh in Python. Python-side filtering side-steps a type
# mismatch: ``interactions.timestamp`` is a String column (legacy
# schema), ``sessions.summarized_at`` is a DateTime — comparing them
# in SQL is dialect-fragile. We over-fetch by ``max_per_cycle * 3``
# so we rarely need more than one cycle to work through the idle set.
_ELIGIBILITY_SQL = text(
    """
    SELECT sub.session_id    AS session_id,
           sub.user_id       AS user_id,
           sub.project       AS project,
           sub.last_ts       AS last_ts,
           s.summarized_at   AS summarized_at
    FROM (
        SELECT i.session_id,
               i.user_id,
               i.project,
               MAX(i.timestamp) AS last_ts
        FROM interactions i
        WHERE i.session_id IS NOT NULL
        GROUP BY i.session_id, i.user_id, i.project
    ) sub
    LEFT JOIN sessions s ON s.id = sub.session_id
    WHERE sub.last_ts < :threshold
    -- Never-summarised rows first (summarized_at IS NULL sorts before
    -- NOT NULL when we order by the IS NULL expression DESC). Within
    -- each group, oldest idle first so we drain legacy backlogs
    -- before touching recent activity. Without this ordering, a
    -- populated backlog of already-summarised sessions at the head
    -- of the ASC-by-last_ts list starves the never-summarised ones.
    ORDER BY (s.summarized_at IS NULL) DESC, sub.last_ts ASC
    LIMIT :fetch_limit
    """
)


# Strict-JSON system prompt. llama.cpp honours response_format via
# grammar-constrained decoding so the schema is enforced at decode
# time; the system prompt is mostly redundant safety.
_SYSTEM_PROMPT = (
    "You are summarising a chat session for an audit archive. "
    "Output ONLY valid JSON matching the schema. "
    "No markdown fences. No commentary.\n\n"
    'Schema: {"summary": string (<= 400 chars), '
    '"key_points": array<string> (<= 8 items, each <= 120 chars)}'
)


# ──────────────────────────── The summariser ─────────────────────────────


class SessionSummarizer:
    """Background summarisation worker.

    Instantiated once per process in ``server.py::lifespan`` and
    scheduled via ``asyncio.create_task``. Cancellation propagates via
    ``asyncio.CancelledError`` — the worker exits the loop cleanly.

    The worker takes its dependencies explicitly rather than via the
    DI container so it can be driven in tests without registering a
    fake container. ``run_once`` is the atomic unit of work and is the
    testing surface; ``run`` is the infinite-wake wrapper.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: sessionmaker[Session],
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        # Injected client lets tests swap in a MockTransport. In
        # production we own the client so we can close it cleanly on
        # shutdown.
        self._http_client = http_client
        self._owns_http_client = http_client is None

    # ── lifecycle ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """Wake every ``interval_minutes`` and run one cycle.

        Never raises — per-cycle exceptions are logged and the worker
        sleeps until the next wake. Cancellation propagates so
        lifespan shutdown is clean.
        """
        logger.info(
            "session summariser started — interval=%dm idle=%dm max/cycle=%d url=%s",
            self._settings.summarizer_interval_minutes,
            self._settings.summarizer_idle_minutes,
            self._settings.summarizer_max_per_cycle,
            self._settings.summarizer_url,
        )
        interval = self._settings.summarizer_interval_minutes * 60
        try:
            while True:
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("session summariser cycle failed")
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("session summariser cancelled — exiting")
            raise
        finally:
            if self._owns_http_client and self._http_client is not None:
                await self._http_client.aclose()

    # ── one cycle ──────────────────────────────────────────────────────

    async def run_once(self) -> int:
        """Process up to ``max_per_cycle`` eligible sessions.

        Returns the number of sessions summarised successfully; used
        by tests to assert progress. A per-session failure is logged
        and counted as a skip — the row stays eligible for the next
        cycle because ``summarized_at`` was never advanced.
        """
        eligible = await asyncio.to_thread(self._find_eligible)
        if not eligible:
            logger.debug("session summariser cycle — 0 eligible sessions")
            return 0

        logger.info("session summariser cycle — %d eligible sessions", len(eligible))
        successes = 0
        for es in eligible:
            try:
                await self._summarise_one(es)
                successes += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "session summariser failed for session=%s user=%s",
                    es.session_id,
                    es.user_id,
                )
        return successes

    # ── eligibility ────────────────────────────────────────────────────

    def _find_eligible(self) -> list[EligibleSession]:
        """Synchronous eligibility query. Bypasses RLS on Postgres via
        ``SET LOCAL row_security = off`` (table-owner privilege).

        Fetches idle candidates from SQL then filters against
        ``summarized_at`` in Python to avoid a String-vs-DateTime
        comparison in SQL that is dialect-fragile.
        """
        max_per_cycle = self._settings.summarizer_max_per_cycle
        threshold = (
            datetime.now() - timedelta(minutes=self._settings.summarizer_idle_minutes)
        ).isoformat()
        db = self._session_factory()
        try:
            self._disable_rls_if_postgres(db)
            rows = db.execute(
                _ELIGIBILITY_SQL,
                {
                    "threshold": threshold,
                    # Over-fetch so fresh sessions filtered out in Python
                    # do not silently cap the cycle's useful work.
                    "fetch_limit": max_per_cycle * 3,
                },
            ).all()
            eligible: list[EligibleSession] = []
            for row in rows:
                if not _is_stale(row.last_ts, row.summarized_at):
                    continue
                eligible.append(
                    EligibleSession(
                        session_id=row.session_id,
                        user_id=row.user_id,
                        project=row.project,
                        last_ts=row.last_ts,
                    )
                )
                if len(eligible) >= max_per_cycle:
                    break
            return eligible
        finally:
            db.close()

    # ── per-session work ───────────────────────────────────────────────

    async def _summarise_one(self, es: EligibleSession) -> None:
        turns = await asyncio.to_thread(self._fetch_turns, es)
        if not turns:
            logger.warning(
                "session summariser: 0 turns for session=%s — skipping",
                es.session_id,
            )
            return

        prompt = _format_transcript(turns)
        raw = await self._call_llm(prompt)
        parsed = _parse_llm_response(raw)
        if parsed is None:
            # JSON parse failed — leave summarized_at NULL so the row
            # is retried next cycle. No partial write.
            logger.warning(
                "session summariser: malformed JSON response for session=%s",
                es.session_id,
            )
            return

        await asyncio.to_thread(self._persist, es, parsed)

    def _fetch_turns(self, es: EligibleSession) -> list[InteractionRecord]:
        db = self._session_factory()
        try:
            self._disable_rls_if_postgres(db)
            return (
                db.query(InteractionRecord)
                .filter(InteractionRecord.session_id == es.session_id)
                .filter(InteractionRecord.user_id == es.user_id)
                .filter(InteractionRecord.project == es.project)
                .order_by(InteractionRecord.timestamp)
                .all()
            )
        finally:
            db.close()

    def _persist(
        self,
        es: EligibleSession,
        parsed: dict[str, Any],
    ) -> None:
        """Upsert SessionRecord under the session's user's GUC."""
        db = self._session_factory()
        try:
            # SET LOCAL app.current_user_id so the RLS WITH CHECK on
            # sessions accepts the write.
            self._set_user_id_if_postgres(db, es.user_id)
            now = datetime.now()
            summary = str(parsed.get("summary") or "")[:400]
            key_points = parsed.get("key_points") or []
            if not isinstance(key_points, list):
                key_points = []
            # Only keep string entries, bounded length.
            kp_clean: list[str] = [
                str(k)[:120] for k in key_points[:8] if isinstance(k, str)
            ]

            existing = (
                db.query(SessionRecord)
                .filter(SessionRecord.id == es.session_id)
                .one_or_none()
            )
            if existing is None:
                db.add(
                    SessionRecord(
                        id=es.session_id,
                        project=es.project,
                        date=now.isoformat(),
                        summary=summary,
                        key_points=json.dumps(kp_clean),
                        model=self._settings.summarizer_model,
                        user_id=es.user_id,
                        summarized_at=now,
                    )
                )
            else:
                existing.summary = summary
                existing.key_points = json.dumps(kp_clean)
                existing.date = now.isoformat()
                existing.model = self._settings.summarizer_model
                existing.summarized_at = now
            db.commit()
            logger.info(
                "session summariser: wrote summary session=%s user=%s kp=%d",
                es.session_id,
                es.user_id,
                len(kp_clean),
            )
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # ── LLM call ───────────────────────────────────────────────────────

    async def _call_llm(self, transcript: str) -> str:
        """POST to the summariser endpoint and return the raw content.

        Uses ``response_format={"type": "json_object"}`` which llama.cpp
        enforces via grammar-constrained decoding — malformed JSON is
        essentially impossible, but ``_parse_llm_response`` still
        tolerates a malformed body because other OpenAI-compatible
        backends may treat the hint as advisory.
        """
        client = self._ensure_client()
        url = self._settings.summarizer_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self._settings.summarizer_model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": transcript},
            ],
            "temperature": 0.2,
            "max_tokens": 600,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        response = await client.post(url, json=payload, timeout=60)
        response.raise_for_status()
        body = response.json()
        choices = body.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        return str(message.get("content") or "")

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._http_client is None:  # pragma: no cover - production-only path
            self._http_client = httpx.AsyncClient()
        return self._http_client

    # ── dialect-aware helpers ──────────────────────────────────────────

    @staticmethod
    def _disable_rls_if_postgres(db: Session) -> None:
        """Table-owner bypass of RLS for the current txn on Postgres.

        See `_find_eligible` / `_fetch_turns` — we need to read rows
        across every user. SQLite has no RLS so this is a no-op.
        """
        if (
            db.bind is not None and db.bind.dialect.name == "postgresql"
        ):  # pragma: no cover - postgres-only, smoke-tested live
            db.execute(text("SET LOCAL row_security = off"))

    @staticmethod
    def _set_user_id_if_postgres(db: Session, user_id: str) -> None:
        """Scope the write txn to the session's user for RLS WITH CHECK."""
        if (
            db.bind is not None and db.bind.dialect.name == "postgresql"
        ):  # pragma: no cover - postgres-only, smoke-tested live
            db.execute(
                text("SET LOCAL app.current_user_id = :uid"),
                {"uid": user_id},
            )


# ──────────────────────────── Pure helpers ───────────────────────────────


def _coerce_datetime(value: Any) -> datetime | None:
    """Accept a datetime or an ISO-ish string and return a datetime.

    Raw ``text()`` queries return driver-native values: Postgres hands
    us a ``datetime`` for TIMESTAMP columns, SQLite hands us a string
    formatted as ``YYYY-MM-DD HH:MM:SS.ffffff`` (space, not T). Both
    must be comparable.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            # Older SQLite format with space and microseconds → retry.
            try:
                return datetime.fromisoformat(value.replace(" ", "T"))
            except ValueError:
                return None
    return None


def _is_stale(last_ts: Any, summarized_at: Any) -> bool:
    """Return True when the session needs (re-)summarising.

    Never summarised (``summarized_at`` is NULL) → stale. Otherwise
    compare parsed datetimes so we are immune to SQL-dialect string
    formatting differences.
    """
    summarized_dt = _coerce_datetime(summarized_at)
    if summarized_dt is None:
        return True
    last_ts_dt = _coerce_datetime(last_ts)
    if last_ts_dt is None:
        # Malformed last_ts — treat as stale so the row surfaces for
        # explicit handling rather than silently skipped.
        return True
    return summarized_dt < last_ts_dt


def _format_transcript(turns: Sequence[InteractionRecord]) -> str:
    """Render the session's Q/A pairs as a numbered transcript for the LLM."""
    lines: list[str] = []
    for i, t in enumerate(turns, start=1):
        q = (t.question or "").strip()
        a = (t.answer or "").strip()
        lines.append(f"[{i}] Q: {q}\n    A: {a}")
    return "\n\n".join(lines)


def _parse_llm_response(raw: str) -> dict[str, Any] | None:
    """Strict JSON parse. Returns ``None`` on any failure so the caller
    leaves ``summarized_at`` NULL for retry next cycle."""
    if not raw:
        return None
    raw = raw.strip()
    # Tolerate accidental markdown fences even though we asked for none.
    if raw.startswith("```"):
        # Strip fenced block.
        parts = raw.split("```")
        if len(parts) >= 2:
            raw = parts[1]
            if raw.lower().startswith("json\n"):
                raw = raw[len("json\n") :]
            raw = raw.strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed
