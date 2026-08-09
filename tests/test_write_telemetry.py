"""Tests for write-telemetry helpers (M2, Wave B).

Mirrors the spy-pattern in ``test_memory_tool_handlers.py::TestRecallTelemetry``:
swap the module-level counters for spy doubles, assert the exact ``add`` calls
with labels. No-op safe: when no MeterProvider is configured, the real
counters' ``add`` is a no-op, so the spies are the only way to verify.
"""

from __future__ import annotations

from typing import Any

import pytest


class _SpyCounter:
    """Minimal OTel counter double — records every ``add`` call."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, dict[str, Any]]] = []

    def add(self, amount: float, attributes: dict[str, Any] | None = None) -> None:
        self.calls.append((amount, dict(attributes or {})))


@pytest.fixture
def _write_telemetry_spy(monkeypatch):
    """Swap the module-level counters in write_telemetry for spies."""
    from audittrace.services import write_telemetry as wt_mod

    writes_counter = _SpyCounter()
    chunks_counter = _SpyCounter()
    monkeypatch.setattr(wt_mod, "_memory_writes_total", writes_counter)
    monkeypatch.setattr(wt_mod, "_memory_chunks_indexed_total", chunks_counter)
    return writes_counter, chunks_counter


class TestEmitMemoryWrite:
    """Non-vacuous proof: emit_memory_write increments the counter with
    the correct layer label and no PII."""

    def test_increments_with_layer_label(self, _write_telemetry_spy):
        from audittrace.services.write_telemetry import emit_memory_write

        writes_counter, _ = _write_telemetry_spy
        emit_memory_write(layer="episodic")
        assert writes_counter.calls == [(1, {"layer": "episodic"})]

    def test_procedural_layer_label(self, _write_telemetry_spy):
        from audittrace.services.write_telemetry import emit_memory_write

        writes_counter, _ = _write_telemetry_spy
        emit_memory_write(layer="procedural")
        assert writes_counter.calls == [(1, {"layer": "procedural"})]

    def test_no_pii_in_labels(self, _write_telemetry_spy):
        """Labels must contain ONLY the layer key — never user_id,
        filename, bucket, or any identifying field."""
        from audittrace.services.write_telemetry import emit_memory_write

        writes_counter, _ = _write_telemetry_spy
        emit_memory_write(layer="episodic")
        labels = writes_counter.calls[0][1]
        assert set(labels.keys()) == {"layer"}, (
            f"write_telemetry labels contain unexpected keys: {set(labels.keys())}"
        )


class TestEmitChunksIndexed:
    """Non-vacuous proof: emit_chunks_indexed increments the counter with
    the correct collection label and chunk count, no PII."""

    def test_increments_by_chunk_count(self, _write_telemetry_spy):
        from audittrace.services.write_telemetry import emit_chunks_indexed

        _, chunks_counter = _write_telemetry_spy
        emit_chunks_indexed(collection="decisions", chunk_count=42)
        assert chunks_counter.calls == [(42, {"collection": "decisions"})]

    def test_different_collection(self, _write_telemetry_spy):
        from audittrace.services.write_telemetry import emit_chunks_indexed

        _, chunks_counter = _write_telemetry_spy
        emit_chunks_indexed(collection="semantic", chunk_count=7)
        assert chunks_counter.calls == [(7, {"collection": "semantic"})]

    def test_no_pii_in_labels(self, _write_telemetry_spy):
        """Labels must contain ONLY the collection key — never user_id,
        filename, or any identifying field."""
        from audittrace.services.write_telemetry import emit_chunks_indexed

        _, chunks_counter = _write_telemetry_spy
        emit_chunks_indexed(collection="decisions", chunk_count=5)
        labels = chunks_counter.calls[0][1]
        assert set(labels.keys()) == {"collection"}, (
            f"write_telemetry chunk labels contain unexpected keys: {set(labels.keys())}"
        )

    def test_zero_chunks_skipped_no_emit(self, _write_telemetry_spy):
        """Zero-chunk writes should NOT create a counter entry."""
        from audittrace.services.write_telemetry import emit_chunks_indexed

        _, chunks_counter = _write_telemetry_spy
        emit_chunks_indexed(collection="decisions", chunk_count=0)
        assert chunks_counter.calls == []
