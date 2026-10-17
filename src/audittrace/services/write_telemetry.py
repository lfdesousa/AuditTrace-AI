"""Shared write-telemetry helpers for memory CORPUS writes (M2, ADR-062).

Mirrors the module-level OTel-meter idiom in
``services/recall_telemetry.py:46-61`` and ``services/async_persist.py:55-72``
— ``metrics.get_meter(...)`` returns a no-op meter until a real
``MeterProvider`` is configured, so this is safe/no-op when
``AUDITTRACE_OTLP_ENDPOINT`` is unset (laptop-default off).

Two counters, created from scratch (they did NOT exist before Wave B):

* ``audittrace_memory_writes_total{layer}`` — counter, +1 per successful
  memory-write (upload to S3/MinIO, episodic or procedural).
* ``audittrace_memory_chunks_indexed_total{collection}`` — counter,
  increments by ``len(chunks)`` per index operation, per collection
  (decisions, skills, semantic, etc.). This is the real corpus-growth
  signal; the old flat-line-of-1s in v1 plotted the HTTP-call counter,
  not chunk counts (D2 root cause).

One structured ``memory.write`` INFO log line (greppable in Loki).

**NO PII in metric labels.** Labels are ``{layer}`` / ``{collection}``
ONLY — never ``user_id``, ``filename``, or any identifying field.
"""

from __future__ import annotations

import logging

from opentelemetry import metrics

logger = logging.getLogger(__name__)

# ──────────────────────────── Telemetry ──────────────────────────────

_meter = metrics.get_meter("audittrace.write")

_memory_writes_total = _meter.create_counter(
    name="audittrace_memory_writes_total",
    description=(
        "Successful memory-corpus writes by layer. Labels: layer "
        "(episodic | procedural). No PII."
    ),
)

_memory_chunks_indexed_total = _meter.create_counter(
    name="audittrace_memory_chunks_indexed_total",
    description=(
        "Chunks indexed per collection into ChromaDB. Labels: collection "
        "(decisions | skills | semantic | ai_research_papers). No PII."
    ),
)


def emit_memory_write(*, layer: str) -> None:
    """Emit a +1 increment to ``audittrace_memory_writes_total{layer}``
    and a structured ``memory.write`` INFO log line.

    Call from the memory-upload surface (one per successful upload).

    Args:
        layer: Memory layer being written to — ``"episodic"`` or
            ``"procedural"``.  No PII.
    """
    _memory_writes_total.add(1, {"layer": layer})

    logger.info(
        "memory.write | op=upload layer=%s",
        layer,
    )


def emit_chunks_indexed(*, collection: str, chunk_count: int) -> None:
    """Emit ``+= chunk_count`` to ``audittrace_memory_chunks_indexed_total{collection}``
    and a structured ``memory.write`` INFO log line.

    Call from the index surface once per collection after upsert (one per
    successful index operation).

    Zero-chunk operations are skipped entirely (no counter increment, no log
    line) — a no-op index pass is not a corpus-growth event, and a spurious
    ``+= 0`` entry would create a phantom series/label combination in
    Prometheus for a collection that never actually grew.

    Args:
        collection: ChromaDB collection name (e.g. ``"decisions"``).
            No PII.
        chunk_count: Number of chunks written for this collection
            (the real corpus-growth signal).
    """
    if chunk_count == 0:
        return

    _memory_chunks_indexed_total.add(chunk_count, {"collection": collection})

    logger.info(
        "memory.write | op=index collection=%s chunks_indexed=%d",
        collection,
        chunk_count,
    )
