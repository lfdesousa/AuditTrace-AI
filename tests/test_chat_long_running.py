"""Tests for ADR-034 long-running generation support.

Covers:
- Per-chunk idle timeout (_iter_with_idle_timeout)
- SSE keep-alive comment frames (Commit 3, extended later)
"""

import asyncio

import httpx
import pytest

from audittrace.routes.chat import _iter_with_idle_timeout

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


# ───────────────────── SSE keep-alive comment frames ─────────────────────────


class TestSSEKeepAlive:
    """ADR-034: keep-alive comment frames emitted during quiet periods."""

    @pytest.mark.asyncio
    async def test_keepalive_emitted_during_quiet_period(self):
        """A gap longer than keepalive_interval yields None (keep-alive signal)."""
        # One line, then a 0.3s stall (> 0.1s keepalive), then another line.
        resp = _TimedStreamResponse(
            [
                ("data: first", 0.0),
                ("data: second", 0.3),
            ]
        )
        items = [
            item
            async for item in _iter_with_idle_timeout(
                resp, chunk_timeout=2.0, keepalive_interval=0.1
            )
        ]
        # Expect: "data: first", then 2-3 Nones (keep-alives), then "data: second"
        assert items[0] == "data: first"
        assert items[-1] == "data: second"
        none_count = sum(1 for x in items if x is None)
        assert none_count >= 1, f"Expected at least 1 keep-alive, got {none_count}"

    @pytest.mark.asyncio
    async def test_no_keepalive_when_chunks_arrive_fast(self):
        """When chunks arrive faster than keepalive_interval, no Nones are yielded."""
        resp = _TimedStreamResponse(
            [
                ("data: a", 0.0),
                ("data: b", 0.01),
                ("data: c", 0.01),
            ]
        )
        items = [
            item
            async for item in _iter_with_idle_timeout(
                resp, chunk_timeout=2.0, keepalive_interval=0.5
            )
        ]
        assert items == ["data: a", "data: b", "data: c"]

    @pytest.mark.asyncio
    async def test_stall_after_keepalives_raises_timeout(self):
        """After enough keep-alive cycles without real data, raise ReadTimeout."""
        resp = _TimedStreamResponse([], stall_after=True)
        with pytest.raises(httpx.ReadTimeout, match="per-chunk idle timeout"):
            async for _ in _iter_with_idle_timeout(
                resp, chunk_timeout=0.3, keepalive_interval=0.1
            ):
                pass

    @pytest.mark.asyncio
    async def test_idle_elapsed_resets_on_real_data(self):
        """A real line arriving after some keep-alives resets the idle clock."""
        # Stall 0.25s (2-3 keep-alives at 0.1s), then a real line, then stall
        # another 0.25s (2-3 keep-alives). chunk_timeout=0.3s means neither
        # stall alone triggers timeout — only total silence would.
        resp = _TimedStreamResponse(
            [
                ("data: first", 0.25),
                ("data: second", 0.25),
            ]
        )
        items = [
            item
            async for item in _iter_with_idle_timeout(
                resp, chunk_timeout=0.3, keepalive_interval=0.1
            )
        ]
        data_items = [x for x in items if x is not None]
        assert data_items == ["data: first", "data: second"]

    @pytest.mark.asyncio
    async def test_keepalive_disabled_when_interval_zero(self):
        """keepalive_interval=0 means pure idle-timeout mode — no None yields."""
        resp = _TimedStreamResponse(
            [
                ("data: a", 0.0),
                ("data: b", 0.05),
            ]
        )
        items = [
            item
            async for item in _iter_with_idle_timeout(
                resp, chunk_timeout=1.0, keepalive_interval=0
            )
        ]
        assert items == ["data: a", "data: b"]
