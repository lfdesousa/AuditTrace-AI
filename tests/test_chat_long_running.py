"""Tests for ADR-034 long-running generation support.

Covers:
- Per-chunk idle timeout (_iter_with_idle_timeout)
- SSE keep-alive comment frames (Commit 3, extended later)
"""

import asyncio

import httpx
import pytest

from sovereign_memory.routes.chat import _iter_with_idle_timeout

# ──────────────── helpers: fake httpx.Response for aiter_lines ────────────────


class _TimedStreamResponse:
    """Yields SSE lines with configurable inter-line delays.

    *lines_with_delays* is a list of ``(line, delay_seconds)`` tuples.
    If *stall_after* is True, the iterator blocks indefinitely after
    yielding all lines — the idle timeout should kill it.
    """

    def __init__(
        self,
        lines_with_delays: list[tuple[str, float]],
        *,
        stall_after: bool = False,
    ) -> None:
        self._items = lines_with_delays
        self._stall_after = stall_after

    def raise_for_status(self) -> None:
        pass

    async def aiter_lines(self):  # type: ignore[override]
        for line, delay in self._items:
            if delay > 0:
                await asyncio.sleep(delay)
            yield line
        if self._stall_after:
            # Block forever — idle timeout will fire.
            await asyncio.sleep(9999)


# ──────────────────── per-chunk idle timeout tests ───────────────────────────


class TestPerChunkIdleTimeout:
    """ADR-034: per-chunk idle timeout replaces the flat total timeout."""

    @pytest.mark.asyncio
    async def test_stream_completes_when_chunks_arrive_within_timeout(self):
        """Lines arriving faster than the idle timeout → full stream consumed."""
        resp = _TimedStreamResponse(
            [
                ("data: chunk1", 0.0),
                ("data: chunk2", 0.05),
                ("data: chunk3", 0.05),
            ]
        )
        lines = [line async for line in _iter_with_idle_timeout(resp, 1.0)]
        assert lines == ["data: chunk1", "data: chunk2", "data: chunk3"]

    @pytest.mark.asyncio
    async def test_stream_raises_when_chunk_exceeds_idle_timeout(self):
        """One line then a stall longer than chunk_timeout → ReadTimeout."""
        resp = _TimedStreamResponse(
            [("data: first", 0.0)],
            stall_after=True,
        )
        with pytest.raises(httpx.ReadTimeout, match="per-chunk idle timeout"):
            lines = []
            async for line in _iter_with_idle_timeout(resp, 0.2):
                lines.append(line)
        # The first line was received before the stall.
        assert lines == ["data: first"]

    @pytest.mark.asyncio
    async def test_long_total_stream_succeeds_if_chunks_keep_flowing(self):
        """A stream whose TOTAL duration exceeds the old flat timeout
        (simulated here as 10 chunks × 0.05s = 0.5s total, well past a
        0.3s 'flat timeout') completes because no single inter-chunk gap
        exceeds the idle timeout."""
        lines_with_delays = [(f"data: chunk{i}", 0.05) for i in range(10)]
        resp = _TimedStreamResponse(lines_with_delays)
        lines = [line async for line in _iter_with_idle_timeout(resp, 0.3)]
        assert len(lines) == 10

    @pytest.mark.asyncio
    async def test_empty_stream_completes(self):
        """An empty stream (immediate StopAsyncIteration) should not raise."""
        resp = _TimedStreamResponse([])
        lines = [line async for line in _iter_with_idle_timeout(resp, 1.0)]
        assert lines == []
