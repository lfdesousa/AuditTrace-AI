"""Shared recall-telemetry helper for EVERY memory READ surface.

Mirrors the module-level OTel-meter idiom in
``services/async_persist.py:55-72`` and the existing recall-telemetry
in ``tools/memory_handlers.py:78-118`` — ``metrics.get_meter(...)``
returns a no-op meter until a real ``MeterProvider`` is configured, so
this is safe/no-op when ``AUDITTRACE_OTLP_ENDPOINT`` is unset
(laptop-default off).

Two instruments, shared by both the in-process tool recalls AND the
REST read routes:

* ``audittrace_recall_total{source, collection, hit}`` — counter
* ``audittrace_recall_results{source, collection, hit}`` — histogram

One shared ``emit_recall_telemetry`` callable that both surfaces call,
so coverage cannot drift again (WU-1c, this PR).

**NO PII in metric labels.**  Labels are ``{source, collection, hit}``
ONLY.  The ``source`` label distinguishes the calling surface
(``tool`` for in-process tool recalls, ``backoffice`` for REST routes).
Never includes ``user_id``, ``query``, ``content``, or any other
identifying field.

Structured ``memory.read`` INFO log line (WU-B) emits the same fields
plus ``results_returned`` and ``hit``, and leverages the already-
propagated ``user_id``/``trace_id``/``session_id`` (logs already carry
these via the ``@log_call`` / context — this is the traceability
invariant, NOT new PII in metrics).
"""

from __future__ import annotations

import logging

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
        "the call hit (page.total > 0). Labels: source (tool|backoffice), "
        "collection, hit."
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
) -> None:
    """Emit the two recall-telemetry instruments and a structured log line
    for one memory read.

    Called by both the in-process tool recalls (via
    ``memory_handlers.py``) AND the REST read routes
    (via ``routes/memory.py``).  One shared helper so coverage cannot
    drift again.

    Args:
        source: Calling surface — ``"tool"`` for in-process tool recalls,
            ``"backoffice"`` for REST routes.
        collection: ChromaDB collection name (e.g. ``"decisions"``,
            ``"semantic"``, ``"sessions"``) or layer name for S3-backed
            reads (``"episodic"``, ``"procedural"``).
        results_count: The ``page.total`` / ``total`` field from the
            response — the true-candidate count.
    """
    hit = results_count > 0
    labels = {"source": source, "collection": collection, "hit": str(hit).lower()}

    # Metric side-effects (pure, no response-shape mutation).
    _recall_counter.add(1, labels)
    _recall_results_histogram.record(results_count, labels)

    # Structured log line (WU-B) — greppable in Loki, visible on laptop.
    # Fields: surface, collection, results_returned, hit.
    # user_id/trace_id/session_id are already propagated via the
    # logging-context machinery (no new PII in labels).
    logger.info(
        "memory.read | surface=%s collection=%s results_returned=%d hit=%s",
        source,
        collection,
        results_count,
        hit,
    )
