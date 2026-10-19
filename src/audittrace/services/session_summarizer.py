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
   that session's ``user_id`` via ``set_config('app.current_user_id',
   ..., true)`` so the RLS ``WITH CHECK`` on ``sessions`` accepts the
   INSERT / UPDATE. No cross-user leakage is possible: each row is
   written under its own user's GUC.

On SQLite (tests), the ``row_security`` / ``set_config`` calls are
no-ops because SQLite has no GUCs and no RLS.

**Ownership scoping (security).** Step 1 above bypasses RLS by design
(it must see every user's idle sessions), which means it must never
hand a ``sessions`` row to a group that does not own it. Every lookup
and write in this module is scoped by ``(session_id, user_id)``, not
``id`` alone: ``_is_foreign_owner`` is the single predicate for "this
row is not this group's own", the eligibility query excludes any
group whose id collides with a foreign-owned row *before* any slot or
LLM call is spent on it, and a pre-LLM re-check closes the window
between eligibility and the model call. A primary-key race from a
different writer is only ever swallowed when it is the ``sessions``
PK itself; every other integrity error propagates. See the build
record for v1.29.5 for the guard-by-guard proof.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from opentelemetry import metrics, trace
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audittrace.config import Settings
from audittrace.db.models import InteractionRecord, SessionRecord

logger = logging.getLogger(__name__)

# #344 — the background sweep runs with no inbound request, so without an
# explicit span its model call surfaces as an unattributed orphan root
# trace. Open one span per session, attributed to the user/session it
# condenses (mirrors the request-path LLM spans). ``get_tracer`` returns a
# proxy that resolves the global TracerProvider lazily, so this works
# whether or not tracing is configured (no-op span when it is not).
_tracer = trace.get_tracer(__name__)

# Backlog #10 — counter for ctx-overflow handling.
# Label `branch` ∈ {"truncate", "sentinel"} so the operator can tell ordinary
# clipping (most long sessions, recoverable) from pathological skips
# (single-turn bigger than ctx; data-loss class).
_meter = metrics.get_meter("audittrace.summariser")
_ctx_overflow_counter = _meter.create_counter(
    name="audittrace.summariser.skipped_ctx_overflow",
    description=(
        "Sessions where the pre-flight token count exceeded ctx_size and "
        "the summariser had to drop turns ('truncate') or insert a sentinel "
        "row ('sentinel') instead of sending the full transcript."
    ),
)

# Ownership-scoping security fix (v1.29.5). ``stage`` is one of
# eligibility | pre_llm | persist_pk — see ``_record_collisions`` /
# ``_foreign_owner_blocks`` / ``_handle_persist_pk_conflict``.
# ``stage=eligibility`` counts a colliding (session_id, user_id) group
# only on its FIRST sighting in this process: the groups are built from
# append-only ``interactions`` rows, so they never clear on their own,
# and counting every cycle would make the 15-minute-increase alert fire
# forever once a single collision exists. A restart's fresh seen-set
# re-counts every still-standing collision once — an expected single
# blip, not a new incident. Multi-replica dashboards must aggregate the
# gauge with max(), never sum() (each replica sets its own point).
_session_collisions_counter = _meter.create_counter(
    name="audittrace.summariser.session_collisions",
    description=(
        "Session/user ownership collisions the summariser refused to "
        "write across. stage=eligibility counts first sightings per "
        "process; stage=pre_llm / stage=persist_pk count per blocked "
        "event. No summary text in the label set."
    ),
)
_session_collisions_gauge = _meter.create_gauge(
    name="audittrace.summariser.session_collisions_current",
    description=(
        "Distinct (session_id, user_id) groups excluded this cycle "
        "because the existing sessions row belongs to a different user "
        "(or none). Set every cycle. Aggregate across replicas with "
        "max(), never sum()."
    ),
)


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
#
# Ownership scoping (v1.29.5, security): the join on ``sessions`` is
# scoped by ``user_id`` too, not ``id`` alone, and a group whose id
# already carries a FOREIGN owner (someone else's row, or an
# explicitly ownerless one) is excluded entirely by the trailing
# ``NOT EXISTS`` — before any slot or LLM call is spent on it. This is
# the SQL transcription of ``_is_foreign_owner`` (the one place the
# ownership rule is written); portable across both dialects, no
# ``IS DISTINCT FROM``.
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
          AND i.user_id IS NOT NULL
        GROUP BY i.session_id, i.user_id, i.project
    ) sub
    LEFT JOIN sessions s ON s.id = sub.session_id AND s.user_id = sub.user_id
    WHERE sub.last_ts < :threshold
      AND NOT EXISTS (
          SELECT 1 FROM sessions x
          WHERE x.id = sub.session_id
            AND (x.user_id IS NULL OR x.user_id <> sub.user_id)
      )
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

# Lists every idle (session_id, user_id) group that has an EXISTING
# sessions row of any ownership, including its owner, so the caller can
# apply ``_is_foreign_owner`` in Python to find the groups the query
# above structurally excludes. A group with NO sessions row at all is
# not a collision (it is simply never-summarised) and is deliberately
# left out by the INNER JOIN.
_COLLISION_CANDIDATES_SQL = text(
    """
    SELECT sub.session_id    AS session_id,
           sub.user_id       AS user_id,
           x.user_id         AS owner_user_id
    FROM (
        SELECT i.session_id,
               i.user_id,
               MAX(i.timestamp) AS last_ts
        FROM interactions i
        WHERE i.session_id IS NOT NULL
          AND i.user_id IS NOT NULL
        GROUP BY i.session_id, i.user_id
    ) sub
    JOIN sessions x ON x.id = sub.session_id
    WHERE sub.last_ts < :threshold
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
        session_factory: async_sessionmaker[AsyncSession],
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        # Injected client lets tests swap in a MockTransport. In
        # production we own the client so we can close it cleanly on
        # shutdown.
        self._http_client = http_client
        self._owns_http_client = http_client is None
        # Effective summariser context window, resolved once from the
        # server's /props (single source of truth) and cached. None until
        # the first resolution. See `_resolve_ctx_tokens`.
        self._ctx_tokens: int | None = None
        # First-sighting memory for the collision counter (R5′/G7e) —
        # per-INSTANCE, not module-global, so a fresh process (restart)
        # re-counts every still-standing collision exactly once rather
        # than staying silent forever. Never persisted: a durable mark
        # would need a migration on a frozen table (rejected, see R2).
        self._seen_collisions: set[tuple[str, str]] = set()

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
        # AsyncExitStack over a hand-rolled try/finally (PEP 343 / PYTHON-
        # ENGINEERING §1): register the owned-client teardown declaratively
        # so it runs on every exit path — normal, exception, or cancellation.
        # The client is created lazily (`_ensure_client`) and may be injected
        # by a test (which we must NOT close), so the callback re-checks
        # ownership at teardown time rather than entering a client we may not
        # yet have.
        async with contextlib.AsyncExitStack() as stack:
            stack.push_async_callback(self._aclose_owned_client)
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

    async def _aclose_owned_client(self) -> None:
        """Close the HTTP client iff we own it (not test-injected)."""
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
        eligible = await self._find_eligible()
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

    async def _find_eligible(self) -> list[EligibleSession]:
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
        async with self._session_factory() as db:
            await self._disable_rls_if_postgres(db)
            rows = (
                await db.execute(
                    _ELIGIBILITY_SQL,
                    {
                        "threshold": threshold,
                        # Over-fetch so fresh sessions filtered out in Python
                        # do not silently cap the cycle's useful work.
                        "fetch_limit": max_per_cycle * 3,
                    },
                )
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

            candidates = (
                await db.execute(_COLLISION_CANDIDATES_SQL, {"threshold": threshold})
            ).all()
            self._record_collisions(
                [
                    (row.session_id, row.user_id, row.owner_user_id)
                    for row in candidates
                    if _is_foreign_owner(row.owner_user_id, row.user_id)
                ]
            )
            return eligible

    def _record_collisions(
        self, collisions: Sequence[tuple[str, str, str | None]]
    ) -> None:
        """R5′ — gauge = groups excluded THIS cycle; counter + one
        WARNING only on a group's FIRST sighting in this process (see
        the module-level counter's docstring for why)."""
        _session_collisions_gauge.set(len(collisions))
        for session_id, user_id, owner_user_id in collisions:
            key = (session_id, user_id)
            if key in self._seen_collisions:
                continue
            self._seen_collisions.add(key)
            _session_collisions_counter.add(1, {"stage": "eligibility"})
            logger.warning(
                "session summariser: session/user ownership collision "
                "session=%r user=%r owner=%r",
                session_id,
                user_id,
                owner_user_id,
            )

    async def _foreign_owner_blocks(self, es: EligibleSession) -> bool:
        """R4 — re-check ownership immediately before the LLM call,
        closing the eligibility→LLM-call race window. Returns True (and
        counts/logs) when ``es`` must NOT be summarised.

        #364 session-scope discipline: ``owner`` is consumed entirely
        inside the ``async with`` block, never read after the session
        closes — even though it is already a plain ``str | None`` (not
        an ORM instance) by the time ``_owner_of`` returns it. The
        repo's structural guard conservatively treats any value a
        session-handed helper returns as session-bound, specifically so
        a FUTURE change to that helper cannot silently reintroduce #364
        without this function's shape also having to change; following
        that discipline here costs nothing and keeps the guard
        meaningful.
        """
        async with self._session_factory() as db:
            await self._disable_rls_if_postgres(db)
            exists, owner = await _owner_of(db, es.session_id)
            if not (exists and _is_foreign_owner(owner, es.user_id)):
                return False
            logger.warning(
                "session summariser: pre-LLM ownership mismatch "
                "session=%r user=%r owner=%r",
                es.session_id,
                es.user_id,
                owner,
            )
        _session_collisions_counter.add(1, {"stage": "pre_llm"})
        return True

    async def _handle_persist_pk_conflict(self, es: EligibleSession) -> None:
        """After a ``sessions``-PK race, classify it (A1-F4): a
        same-owner replica racing this one is the expected multi-pod
        shape and gets an INFO log with no count; a foreign owner
        winning the race is the security-relevant case and gets a
        counted, logged skip.

        #364 session-scope discipline: see ``_foreign_owner_blocks``'s
        docstring — ``owner`` stays inside the ``async with`` block.
        """
        async with self._session_factory() as db:
            await self._disable_rls_if_postgres(db)
            # A PK violation means a row for this id now exists (the
            # writer we raced); ``exists`` is discarded because a
            # missing row would make ``owner`` None too, which
            # ``_is_foreign_owner`` already treats as foreign (the safe
            # default for an otherwise-impossible state).
            _, owner = await _owner_of(db, es.session_id)
            if _is_foreign_owner(owner, es.user_id):
                _session_collisions_counter.add(1, {"stage": "persist_pk"})
                logger.warning(
                    "session summariser: persist race, foreign owner "
                    "session=%r user=%r owner=%r",
                    es.session_id,
                    es.user_id,
                    owner,
                )
            else:
                logger.info(
                    "session summariser: persist race, same-owner replica "
                    "session=%r user=%r",
                    es.session_id,
                    es.user_id,
                )

    # ── per-session work ───────────────────────────────────────────────

    async def _summarise_one(self, es: EligibleSession) -> None:
        # #344 — open an attributed span for the whole per-session unit so
        # the LLM call (and its HTTPX client span) nests under a trace that
        # carries the user/session it condenses, instead of an anonymous
        # orphan root. The captured trace_id is persisted onto the
        # SessionRecord so the row links back to its trace.
        with _tracer.start_as_current_span("session.summarise") as span:
            span.set_attribute("langfuse.user.id", es.user_id)
            span.set_attribute("user.id", es.user_id)
            span.set_attribute("session.id", es.session_id)
            span.set_attribute("audittrace.project", es.project)
            span.set_attribute("langfuse.observation.type", "generation")
            trace_id = _span_trace_id_hex(span)

            if await self._foreign_owner_blocks(es):
                return

            turns = await self._fetch_turns(es)
            if not turns:
                logger.warning(
                    "session summariser: 0 turns for session=%s — skipping",
                    es.session_id,
                )
                return

            # Backlog #10 — pre-flight ctx guard. Before sending the prompt,
            # check it fits in the summariser's ctx window. If not, drop oldest
            # turns until it does ("truncate" branch). If even one turn is
            # bigger than ctx by itself, write a sentinel SessionRecord so the
            # row leaves the eligibility set ("sentinel" branch) rather than
            # looping every cycle on an HTTP 400.
            prompt, dropped = await self._fit_prompt_to_ctx(turns)
            if prompt is None:
                await self._persist_sentinel_overflow(es, len(turns), trace_id)
                _ctx_overflow_counter.add(1, {"branch": "sentinel"})
                return

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

            if dropped > 0:
                # Surface the truncation to the operator via the persisted
                # summary so audit consumers see what was elided.
                note = (
                    f"[truncated: oldest {dropped} of {len(turns)} turns omitted, "
                    f"summariser ctx={self._settings.summarizer_ctx_tokens}]"
                )
                existing = parsed.get("summary") or ""
                parsed = {
                    **parsed,
                    "summary": f"{note} {existing}".strip(),
                }
                _ctx_overflow_counter.add(1, {"branch": "truncate"})

            await self._persist(es, parsed, trace_id)

    async def _fit_prompt_to_ctx(
        self, turns: Sequence[InteractionRecord]
    ) -> tuple[str | None, int]:
        """Render ``turns`` as a transcript, dropping oldest turns until the
        token count fits below ``ctx_size - reserve``.

        Returns ``(prompt, dropped_count)`` on success; ``(None, len(turns))``
        if even the most recent single turn is over ctx (pathological — the
        sentinel branch handles this).

        Counts tokens via llama-server's native ``/tokenize`` endpoint so
        we use the same tokeniser as the model. Falls back to sending the
        prompt as-is if ``/tokenize`` is unreachable — the existing
        post-error path is the safety net of last resort.
        """
        ctx_tokens = await self._resolve_ctx_tokens()
        budget = ctx_tokens - self._settings.summarizer_ctx_reserve_tokens

        # System prompt counts against the budget too. ``_safe_count_tokens``
        # never raises — a /tokenize failure falls back to a conservative
        # char estimate so a poison-pill session can never wedge the worker
        # into an infinite-retry loop (the 2026-06-09 incident).
        sys_tokens = await self._safe_count_tokens(_SYSTEM_PROMPT)

        # Try the full transcript first; if it fits, no truncation needed.
        full = _format_transcript(turns)
        full_tokens = await self._safe_count_tokens(full)
        if sys_tokens + full_tokens <= budget:
            return full, 0

        # Drop oldest turns one at a time until it fits. We keep the most
        # recent context because (a) it's likeliest to be on-topic and
        # (b) it includes whatever final user query the summary should
        # capture.
        kept = list(turns)
        dropped = 0
        while kept:
            kept = kept[1:]  # drop oldest
            dropped += 1
            if not kept:
                break
            candidate = _format_transcript(kept)
            cand_tokens = await self._safe_count_tokens(candidate)
            if sys_tokens + cand_tokens <= budget:
                return candidate, dropped

        # Even the most recent single turn is over ctx. Fall to sentinel.
        return None, len(turns)

    async def _resolve_ctx_tokens(self) -> int:
        """Return the summariser context window, preferring the server's own
        ``/props`` over the configured default. Cached after first call.

        ``summarizer_ctx_tokens`` drifts from the deployed llama-server's
        actual ``--ctx-size`` (the 2026-06-09 incident: config 32768, server
        8192 → the guard waved oversized prompts through → HTTP 4xx/5xx every
        cycle). Reading ``n_ctx`` from ``/props`` makes the server the single
        source of truth. Falls back to the configured value if ``/props`` is
        unreachable or malformed, so the feature degrades rather than crashes.
        """
        if self._ctx_tokens is not None:
            return self._ctx_tokens
        configured = self._settings.summarizer_ctx_tokens
        n_ctx: int | None = None
        try:
            n_ctx = await self._fetch_server_n_ctx()
        except Exception as exc:
            logger.debug(
                "session summariser: /props unreachable (%s) — using configured ctx=%d",
                exc,
                configured,
            )
        resolved = n_ctx if n_ctx and n_ctx > 0 else configured
        if n_ctx and n_ctx != configured:
            logger.info(
                "session summariser: ctx window from /props=%d overrides configured=%d",
                n_ctx,
                configured,
            )
        self._ctx_tokens = resolved
        return resolved

    async def _fetch_server_n_ctx(self) -> int | None:
        """GET the summariser server's ``/props`` and return ``n_ctx``.

        ``summarizer_url`` ends in ``/v1``; ``/props`` lives at the server
        root, so we strip the suffix first (same idiom as ``_count_tokens``).
        Returns ``None`` if the field is absent or non-positive.
        """
        client = self._ensure_client()
        base = self._settings.summarizer_url.rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        url = f"{base}/props"
        response = await client.get(url, timeout=10)
        response.raise_for_status()
        body = response.json()
        gen = body.get("default_generation_settings") or {}
        n_ctx = gen.get("n_ctx")
        if isinstance(n_ctx, int) and n_ctx > 0:
            return n_ctx
        # Some llama.cpp builds expose it at the top level instead.
        top = body.get("n_ctx")
        return top if isinstance(top, int) and top > 0 else None

    async def _safe_count_tokens(self, content: str) -> int:
        """Token count via ``/tokenize`` with a conservative char fallback.

        A poison-pill session must never wedge the worker. If ``/tokenize``
        is slow or unreachable we estimate high (1 token per 3 chars) so the
        truncation loop still converges and we err on the side of dropping
        more rather than overflowing the server's context window.
        """
        try:
            return await self._count_tokens(content)
        except Exception as exc:
            est = len(content) // 3 + 1
            logger.debug(
                "session summariser: /tokenize failed (%s) — char estimate %d",
                exc,
                est,
            )
            return est

    async def _count_tokens(self, content: str) -> int:
        """POST to llama-server's ``/tokenize`` and return the token count.

        ``summarizer_url`` ends in ``/v1`` (the OpenAI-compat shim). The
        ``/tokenize`` endpoint lives at the server root, so we strip the
        ``/v1`` suffix before constructing the URL.
        """
        client = self._ensure_client()
        base = self._settings.summarizer_url.rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        url = f"{base}/tokenize"
        # 30s (was 10s): a very large transcript can take longer than 10s to
        # tokenise; a timeout here used to propagate and re-queue the session
        # forever. ``_safe_count_tokens`` is the backstop if this still fails.
        response = await client.post(url, json={"content": content}, timeout=30)
        response.raise_for_status()
        body = response.json()
        tokens = body.get("tokens") or []
        return len(tokens)

    async def _persist_sentinel_overflow(
        self, es: EligibleSession, turn_count: int, trace_id: str | None = None
    ) -> None:
        """Write a sentinel SessionRecord for a session whose latest single
        turn exceeds ctx_size. Marks ``summarized_at`` so the row leaves
        the eligibility set; manual operator follow-up resolves the data
        archive question.
        """
        async with self._session_factory() as db:
            try:
                await self._set_user_id_if_postgres(db, es.user_id)
                now = datetime.now()
                sentinel_summary = (
                    f"[sentinel-skip-ctx-overflow-auto] session has "
                    f"{turn_count} turns whose most recent single turn alone "
                    f"exceeds the summariser ctx window "
                    f"({self._settings.summarizer_ctx_tokens} tokens). "
                    "No automated summary produced. Manual review recommended."
                )
                existing = await _load_own_row(db, es)
                if existing is None:
                    db.add(
                        SessionRecord(
                            id=es.session_id,
                            project=es.project,
                            date=now.isoformat(),
                            summary=sentinel_summary,
                            key_points=json.dumps([]),
                            model="sentinel-skip-ctx-overflow-auto",
                            user_id=es.user_id,
                            summarized_at=now,
                            trace_id=trace_id,
                        )
                    )
                else:
                    existing.summary = sentinel_summary
                    existing.key_points = json.dumps([])
                    existing.date = now.isoformat()
                    existing.model = "sentinel-skip-ctx-overflow-auto"
                    existing.summarized_at = now
                    existing.trace_id = trace_id
                await db.commit()
                logger.warning(
                    "session summariser: sentinel-skip session=%s user=%s "
                    "(latest turn alone exceeds ctx=%d)",
                    es.session_id,
                    es.user_id,
                    self._settings.summarizer_ctx_tokens,
                )
            except Exception:
                await db.rollback()
                raise

    async def _fetch_turns(self, es: EligibleSession) -> list[InteractionRecord]:
        async with self._session_factory() as db:
            await self._disable_rls_if_postgres(db)
            return list(
                (
                    await db.execute(
                        select(InteractionRecord)
                        .filter(InteractionRecord.session_id == es.session_id)
                        .filter(InteractionRecord.user_id == es.user_id)
                        .filter(InteractionRecord.project == es.project)
                        .order_by(InteractionRecord.timestamp)
                    )
                )
                .scalars()
                .all()
            )

    async def _persist(
        self,
        es: EligibleSession,
        parsed: dict[str, Any],
        trace_id: str | None = None,
    ) -> None:
        """Upsert SessionRecord under the session's user's GUC.

        R3 — the lookup is owner-scoped (``_load_own_row``), so this
        write can only ever touch a row ``es.user_id`` already owns, or
        create a brand-new one under that user. A PK race with a
        DIFFERENT writer for the same ``id`` surfaces as the
        ``sessions`` primary-key violation — the only exception this
        method swallows (see ``_handle_persist_pk_conflict``); every
        other integrity error propagates (A1-F3).
        """
        async with self._session_factory() as db:
            try:
                # set_config('app.current_user_id', ...) so the RLS WITH
                # CHECK on sessions accepts the write.
                await self._set_user_id_if_postgres(db, es.user_id)
                now = datetime.now()
                summary = str(parsed.get("summary") or "")[:400]
                key_points = parsed.get("key_points") or []
                if not isinstance(key_points, list):
                    key_points = []
                # Only keep string entries, bounded length.
                kp_clean: list[str] = [
                    str(k)[:120] for k in key_points[:8] if isinstance(k, str)
                ]

                existing = await _load_own_row(db, es)
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
                            trace_id=trace_id,
                        )
                    )
                else:
                    existing.summary = summary
                    existing.key_points = json.dumps(kp_clean)
                    existing.date = now.isoformat()
                    existing.model = self._settings.summarizer_model
                    existing.summarized_at = now
                    existing.trace_id = trace_id
                await db.commit()
                logger.info(
                    "session summariser: wrote summary session=%s user=%s kp=%d",
                    es.session_id,
                    es.user_id,
                    len(kp_clean),
                )
            except Exception as exc:
                await db.rollback()
                if not _is_sessions_pk_violation(exc):
                    raise
                await self._handle_persist_pk_conflict(es)

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
    async def _disable_rls_if_postgres(db: AsyncSession) -> None:
        """Table-owner bypass of RLS for the current txn on Postgres.

        See `_find_eligible` / `_fetch_turns` — we need to read rows
        across every user. SQLite has no RLS so this is a no-op.
        """
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            await db.execute(text("SET LOCAL row_security = off"))

    @staticmethod
    async def _set_user_id_if_postgres(db: AsyncSession, user_id: str) -> None:
        """Scope the write txn to the session's user for RLS WITH CHECK.

        Emits ``set_config('app.current_user_id', :uid, true)`` rather than
        ``SET LOCAL app.current_user_id = :uid``: Postgres ``SET`` is a
        utility command that cannot take a bind parameter, so the latter
        compiles to ``SET LOCAL app.current_user_id = $1`` and raises
        ``syntax error at or near "$1"``. ``set_config(..., true)`` is the
        parameterizable, transaction-LOCAL equivalent — same idiom as
        ``audittrace.db.rls.set_rls_user_id`` / ``_apply_rls_guc``.
        """
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            await db.execute(
                text("SELECT set_config('app.current_user_id', :uid, true)"),
                {"uid": user_id},
            )


# ──────────────── Ownership-scoping helpers (v1.29.5, security) ──────────


def _is_foreign_owner(owner_user_id: str | None, user_id: str) -> bool:
    """R1 — the ONE place the ownership rule is written. A ``sessions``
    row this group cannot prove is its own — no row at all, or a row
    whose owner is explicitly NULL or someone else — is foreign. The
    SQL in ``_ELIGIBILITY_SQL`` is this predicate's transcription; every
    other call site in this module calls this function directly."""
    return owner_user_id is None or owner_user_id != user_id


async def _owner_of(db: AsyncSession, session_id: str) -> tuple[bool, str | None]:
    """Read the current owner of ``sessions.id == session_id`` without
    filtering by any particular user. The caller must already have
    disabled RLS on this transaction (``_disable_rls_if_postgres``) or
    hold a role that bypasses it, so the read sees the row regardless of
    who owns it.

    Returns ``(row_exists, owner_user_id)``. The pair is deliberate: a
    bare ``owner_user_id`` alone cannot distinguish "no sessions row at
    all" (the common, benign case — never summarised yet) from "a row
    exists whose ``user_id`` column is NULL" (F12's legacy-ownerless
    row — genuinely foreign). Both would read back as Python ``None``
    from a single-column select, which is exactly the ambiguity that
    made an early draft of this check block every never-summarised
    session. Callers must test ``row_exists`` before applying the
    ownership predicate (R4: "if a row exists AND the group cannot
    prove it owns it").
    """
    row = (
        await db.execute(
            select(SessionRecord.id, SessionRecord.user_id).filter(
                SessionRecord.id == session_id
            )
        )
    ).first()
    if row is None:
        return False, None
    return True, row.user_id


async def _load_own_row(db: AsyncSession, es: EligibleSession) -> SessionRecord | None:
    """R3 — the ONE owner-scoped loader both writers (``_persist`` and
    ``_persist_sentinel_overflow``) use. Filtering by BOTH ``id`` and
    ``user_id`` means a write can only ever touch a row ``es.user_id``
    already owns, or create a brand-new one under that user — never a
    row belonging to someone else."""
    return (
        await db.execute(
            select(SessionRecord).filter(
                SessionRecord.id == es.session_id,
                SessionRecord.user_id == es.user_id,
            )
        )
    ).scalar_one_or_none()


def _driver_exception(exc: IntegrityError) -> BaseException | None:
    """The real driver exception behind a SQLAlchemy ``DBAPIError``.

    ``DBAPIError.driver_exception`` exists only from SQLAlchemy 2.1; the
    product runs the 2.0 line (SQLALCHEMY-SPANS), so read it defensively:
    ``driver_exception`` when present, else ``exc.orig``, and for the asyncpg
    dialect's adapted exception (2.0: ``orig`` raised ``from`` the real
    asyncpg error) the real error at ``orig.__cause__``. Degrades to
    ``exc.orig`` itself for non-emulated DBAPIs (sqlite3). No
    ``type: ignore``: ``getattr`` keeps mypy clean on both lines.
    """
    driver_exc: BaseException | None = getattr(exc, "driver_exception", None)
    if driver_exc is not None:
        return driver_exc
    orig = exc.orig
    if orig is not None and orig.__cause__ is not None:
        return orig.__cause__
    return orig


def _is_sessions_pk_violation(exc: BaseException) -> bool:
    """S2 / A1-F3 — narrowly recognise the ``sessions`` table's own
    primary-key violation so every OTHER integrity error (a NOT NULL
    violation, a foreign-key violation, …) escapes ``_persist`` /
    ``_persist_sentinel_overflow`` instead of being silently swallowed
    and miscounted as a benign ownership race.

    Postgres (asyncpg, the resolved production driver): SQLSTATE
    ``23505`` *and* the constraint named ``sessions_pkey``. SQLite
    (aiosqlite/sqlite3, the test driver): sqlite3 exposes no structured
    constraint name, only the message text, which names the table and
    column verbatim.

    Unwraps the real driver exception through :func:`_driver_exception`
    (SQLAlchemy 2.0 and 2.1 alike - the product is pinned to the 2.0 line,
    ``sqlalchemy[asyncio]>=2.0.25,<2.1``, because the OTel SQLAlchemy
    instrumentor does not declare 2.1 support), not ``exc.orig`` directly:
    SQLAlchemy's asyncpg dialect wraps the real
    ``asyncpg.exceptions.UniqueViolationError`` in an adapted exception
    whose own ``sqlstate`` is present but whose ``constraint_name`` is NOT
    - that attribute lives only on the nested real driver exception.
    Measured against a real Postgres PK violation during the AC4 build: the
    first draft of this function, reading ``exc.orig`` directly, let the
    violation escape instead of classifying it.
    """
    if not isinstance(exc, IntegrityError):
        return False
    driver_exc = _driver_exception(exc)
    sqlstate = getattr(driver_exc, "sqlstate", None)
    if sqlstate is not None:
        return sqlstate == "23505" and getattr(driver_exc, "constraint_name", None) == (
            "sessions_pkey"
        )
    return "UNIQUE constraint failed: sessions.id" in str(driver_exc)


# ──────────────────────────── Pure helpers ───────────────────────────────


def _span_trace_id_hex(span: Any) -> str | None:
    """Return ``span``'s trace_id as a 32-char hex string, or ``None``.

    #344 — reads the trace_id straight off the span object (not the ambient
    context) so the caller can persist it onto the SessionRecord. Returns
    ``None`` when the span context is invalid — i.e. tracing is disabled and
    the tracer handed back a non-recording span — so we never fabricate an
    id. Mirrors ``chat.py::_current_trace_id_hex`` but span-scoped.
    """
    try:
        ctx = span.get_span_context()
        if ctx is not None and getattr(ctx, "is_valid", False):
            return format(ctx.trace_id, "032x")
    except Exception:  # pragma: no cover - defensive instrumentation read
        return None
    return None


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
