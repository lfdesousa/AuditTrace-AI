"""Shared recall-telemetry helper for EVERY memory READ surface.

Mirrors the module-level OTel-meter idiom in
``services/async_persist.py:55-72`` and the existing recall-telemetry
in ``tools/memory_handlers.py:78-118`` — ``metrics.get_meter(...)``
returns a no-op meter until a real ``MeterProvider`` is configured, so
this is safe/no-op when ``AUDITTRACE_OTLP_ENDPOINT`` is unset
(laptop-default off).

Two instruments, shared by both the in-process tool recalls AND the
REST read routes:

* ``audittrace_recall_total{source, collection, hit, cache}`` — counter
* ``audittrace_recall_results{source, collection, hit, cache}`` — histogram

One shared ``emit_recall_telemetry`` callable that both surfaces call,
so coverage cannot drift again (WU-1c, this PR).

**NO PII in metric labels.**  Labels are ``{source, collection, hit, cache}``
ONLY.  The ``source`` label distinguishes the calling surface: ``tool``
for in-process chat-tool recalls, ``fleet`` for REST-route recalls issued
by an OpenCode fleet agent (``X-Source: opencode-*`` or ``X-Agent-Role``
present — see :func:`classify_recall_source_from_request`), and
``backoffice`` for every other REST-route recall (the human front-door
default).  The ``cache`` label distinguishes Redis-backed cache hits/misses
at the
tool-recall surface (ADR-025 §Decision.8, M1): ``"hit"`` when the result
was served from the ADR-025 tool-result cache, ``"miss"`` when computed
fresh, ``"n/a"`` for REST/backoffice reads that do not consult that cache.
Never includes ``user_id``, ``query``, ``content``, or any other
identifying field.

Structured ``memory.read`` INFO log line (WU-B) emits the same fields
plus ``results_returned``, ``hit``, and ``cache``, and leverages the
already-propagated ``user_id``/``trace_id``/``session_id`` (logs already
carry these via the ``@log_call`` / context — this is the traceability
invariant, NOT new PII in metrics).
"""

from __future__ import annotations

import logging

from fastapi import Request
from opentelemetry import metrics

logger = logging.getLogger(__name__)

# ──────────────────────────── Telemetry ──────────────────────────────

# Mirrors the ``metrics.get_meter(...)`` no-op-safe idiom
# (services/async_persist.py:55-56, tools/memory_handlers.py:85-86):
# returns a no-op meter until ``telemetry.py`` configures a real
# ``MeterProvider``, safe/no-op when ``AUDITTRACE_OTLP_ENDPOINT`` is unset.
_recall_meter = metrics.get_meter("audittrace.recall")
_recall_counter = _recall_meter.create_counter(
    name="audittrace_recall_total",
    description=(
        "Recall-tool invocations by surface and collection, and whether "
        "the call hit (page.total > 0). Labels: "
        "source (tool|fleet|backoffice), collection, hit."
    ),
)
_recall_results_histogram = _recall_meter.create_histogram(
    name="audittrace_recall_results",
    description=(
        "True candidate count (page.total) returned per recall call, "
        "by surface and collection."
    ),
)


def emit_recall_telemetry(
    source: str,
    collection: str,
    results_count: int,
    *,
    cache: str = "n/a",
) -> None:
    """Emit the two recall-telemetry instruments and a structured log line
    for one memory read.

    Called by both the in-process tool recalls (via
    ``memory_handlers.py``) AND the REST read routes
    (via ``routes/memory.py``).  One shared helper so coverage cannot
    drift again.

    Args:
        source: Calling surface — ``"tool"`` for in-process tool recalls,
            ``"fleet"`` for REST-route recalls issued by an OpenCode fleet
            agent, ``"backoffice"`` for every other REST-route recall (see
            :func:`classify_recall_source_from_request`).
        collection: ChromaDB collection name (e.g. ``"decisions"``,
            ``"semantic"``, ``"sessions"``) or layer name for S3-backed
            reads (``"episodic"``, ``"procedural"``).
        results_count: The ``page.total`` / ``total`` field from the
            response — the true-candidate count.
        cache: Cache-hit signal at the tool-recall surface (M1).
            ``"hit"`` when the result was served from the ADR-025
            tool-result cache, ``"miss"`` when computed fresh,
            ``"n/a"`` for REST/backoffice reads that do not consult
            that cache.  Labels: ``{source, collection, hit, cache}``.
            **No PII** — ``cache`` is a fixed enum, not a user field.
    """
    hit = results_count > 0
    labels = {
        "source": source,
        "collection": collection,
        "hit": str(hit).lower(),
        "cache": cache,
    }

    # Metric side-effects (pure, no response-shape mutation).
    _recall_counter.add(1, labels)
    _recall_results_histogram.record(results_count, labels)

    # Structured log line (WU-B) — greppable in Loki, visible on laptop.
    # Fields: surface, collection, results_returned, hit, cache.
    # user_id/trace_id/session_id are already propagated via the
    # logging-context machinery (no new PII in labels).
    logger.info(
        "memory.read | surface=%s collection=%s results_returned=%d hit=%s cache=%s",
        source,
        collection,
        results_count,
        hit,
        cache,
    )


def classify_recall_source_from_request(
    # ``Request[Any]`` would satisfy pre-commit mypy 1.8 (which can't see
    # starlette's generic default), but FastAPI's Pydantic field
    # introspection rejects it at route registration for the routes that
    # call this helper. Bare ``Request`` matches every route in this
    # codebase (see routes/memory.py's ``upload_memory_file`` docstring);
    # we silence the pre-commit-only error class explicitly.
    request: Request,  # type: ignore[type-arg, unused-ignore]
) -> str:
    """Classify a REST recall request as ``"fleet"`` or ``"backoffice"``.

    RECALL-METRIC-COVERAGE (2026-08-12): the fleet's own recalls hit the
    same REST routes (``GET /memory/episodic|procedural|semantic``) as a
    human front-door caller — those routes hardcoded ``source="backoffice"``
    at every ``emit_recall_telemetry`` call site, so the fleet's recalls
    were counted but mislabeled, making the "agents learning" dashboard
    panels read ~0 even though the fleet path was fully instrumented.

    This reuses the SAME attribution the fleet already sends per
    SDLC-ADR-004 / #429-b — ``X-Source: opencode-<role>`` (+
    ``X-Agent-Role``) — and that ``routes/chat.py::_detect_source`` keys
    on for ``interactions.source``. It is NOT a second attribution path;
    it is the same signal read at a second call site (the REST recall
    routes, not just ``/v1/chat/completions``).

    Deliberately coarse: returns the single value ``"fleet"`` regardless
    of *which* role sent the request (builder/reviewer/deployer/...),
    never a per-role value. Per-role cardinality on a Prometheus counter
    is a cost without a consumer — the dashboard only needs "is the fleet
    recalling and hitting?" — and ``X-Agent-Role`` remains available on
    the request (and in ``interactions.source``) for anyone who needs the
    per-role split.

    Args:
        request: The incoming FastAPI request for a recall REST route.

    Returns:
        ``"fleet"`` when ``X-Source`` starts with ``"opencode-"``
        (case-insensitive) or ``X-Agent-Role`` is present with a non-empty
        value; ``"backoffice"`` otherwise (the human front-door default).
    """
    x_source = (request.headers.get("x-source") or "").strip().lower()
    if x_source.startswith("opencode-"):
        return "fleet"
    if (request.headers.get("x-agent-role") or "").strip():
        return "fleet"
    return "backoffice"
