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


# ────────── keep-alive from t=0 across the upstream header-await (#458) ──────
#
# ``_iter_with_idle_timeout`` (above) only runs AFTER the upstream response
# object exists. llama.cpp withholds HTTP headers until the first token — the
# window BEFORE that point was, before this fix, silent: zero bytes reached
# the client from request-accept to first-token, which let a slow-first-token
# model's prompt-eval exceed the CLIENT's own body-read timeout. These tests
# cover ``_stream_upstream_with_keepalive_from_t0``, the helper that closes
# that window, independent of the full route (see test_chat_proxy.py for the
# through-the-route regression coverage on both streaming call sites).


class _FakeHttpxRequest:
    """Minimal stand-in for ``httpx.Request`` — only the attributes
    ``_DelayedHeaderClient``/``_ImmediateHeaderClient``/``_RaisingHeaderClient``
    read back out in ``.send()``."""

    def __init__(self, method: str, url: str, json: dict | None) -> None:
        self.method = method
        self.url = url
        self.json = json


class _DelayedHeaderClient:
    """Fake ``httpx.AsyncClient`` slice (``build_request`` + ``send``) whose
    ``send()`` — i.e. header arrival — stalls *delay* seconds before
    resolving. Models llama.cpp withholding HTTP headers until the first
    token (the prompt-eval window, #458)."""

    def __init__(self, response: object, delay: float) -> None:
        self._response = response
        self._delay = delay
        self.sent_payloads: list[dict | None] = []

    def build_request(self, method, url, json=None, **kwargs):
        return _FakeHttpxRequest(method, url, json)

    async def send(self, request, *, stream=False, **kwargs):
        self.sent_payloads.append(request.json)
        await asyncio.sleep(self._delay)
        return self._response


class _ImmediateHeaderClient:
    """Fake client whose ``send()`` resolves immediately — the fast path
    (spec sub-decision #4): headers arrive before the first keep-alive
    tick."""

    def __init__(self, response: object) -> None:
        self._response = response

    def build_request(self, method, url, json=None, **kwargs):
        return _FakeHttpxRequest(method, url, json)

    async def send(self, request, *, stream=False, **kwargs):
        return self._response


class _RaisingHeaderClient:
    """Fake client whose ``send()`` raises immediately — models llama-server
    being unreachable while opening the stream."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def build_request(self, method, url, json=None, **kwargs):
        return _FakeHttpxRequest(method, url, json)

    async def send(self, request, *, stream=False, **kwargs):
        raise self._exc


class _ControllableHeaderClient:
    """Fake client whose ``send()`` blocks indefinitely until cancelled —
    lets a test deterministically tear down the race generator while the
    background send task is still pending, exercising the cancellation
    cleanup path (a client disconnecting mid prompt-eval)."""

    def __init__(self) -> None:
        self.send_task_cancelled = False

    def build_request(self, method, url, json=None, **kwargs):
        return _FakeHttpxRequest(method, url, json)

    async def send(self, request, *, stream=False, **kwargs):
        try:
            await asyncio.sleep(9999)
        except asyncio.CancelledError:
            self.send_task_cancelled = True
            raise
        return None  # pragma: no cover - unreachable, sleep is cancelled first


class TestStreamUpstreamWithKeepaliveFromT0:
    """WU-1 (#458): the keep-alive-from-t=0 race
    (``_stream_upstream_with_keepalive_from_t0``). Proves both halves of the
    spec's falsifiable acceptance: (1) a slow-first-token upstream gets
    keep-alive frames DURING the header-await window, and (2) the fast path
    (headers arrive before the first tick — the MoE, the common case) stays
    byte-identical to a bare ``client.send`` — no spurious early frame."""

    @pytest.mark.asyncio
    async def test_keepalive_frames_precede_the_response_when_headers_are_slow(
        self,
    ):
        """Headers delayed past the keepalive interval → ≥1 keep-alive frame
        is yielded BEFORE the resolved Response.

        Neuter the ticker (drop the ``yield b": keep-alive\\n\\n"`` on
        timeout in ``_stream_upstream_with_keepalive_from_t0``) and this
        goes RED: the helper would simply await the send task in silence for
        the full delay with zero intermediate frames — exactly the
        client-starvation regression #458 exists to prevent.
        """
        from audittrace.routes.chat import _stream_upstream_with_keepalive_from_t0

        sentinel_response = object()
        client = _DelayedHeaderClient(sentinel_response, delay=0.3)

        items = [
            item
            async for item in _stream_upstream_with_keepalive_from_t0(
                client, "http://llama/chat/completions", {"stream": True}, 0.1
            )
        ]

        assert items[-1] is sentinel_response
        keepalive_frames = items[:-1]
        assert keepalive_frames, (
            "no keep-alive frame was emitted before headers arrived"
        )
        assert all(f == b": keep-alive\n\n" for f in keepalive_frames)

    @pytest.mark.asyncio
    async def test_fast_path_yields_only_the_response_no_spurious_frame(self):
        """Sub-decision #4: headers arrive before the first tick → NO
        keep-alive frame, output is byte-identical to a bare
        ``client.send``.

        Neuter the fast path (e.g. always emit one frame up front
        regardless of timing) and this goes RED.
        """
        from audittrace.routes.chat import _stream_upstream_with_keepalive_from_t0

        sentinel_response = object()
        client = _ImmediateHeaderClient(sentinel_response)

        items = [
            item
            async for item in _stream_upstream_with_keepalive_from_t0(
                client, "http://llama/chat/completions", {"stream": True}, 5.0
            )
        ]

        assert items == [sentinel_response]

    @pytest.mark.asyncio
    async def test_upstream_send_failure_propagates_unchanged(self):
        """A connect failure while opening the stream must propagate
        exactly as it would from a bare ``client.send`` — the existing
        ConnectError → SSE error frame + failed audit row handling at both
        call sites is unchanged by the race."""
        from audittrace.routes.chat import _stream_upstream_with_keepalive_from_t0

        client = _RaisingHeaderClient(httpx.ConnectError("refused"))

        with pytest.raises(httpx.ConnectError):
            async for _ in _stream_upstream_with_keepalive_from_t0(
                client, "http://llama/chat/completions", {}, 5.0
            ):
                pass

    @pytest.mark.asyncio
    async def test_forwards_the_exact_json_payload(self):
        """Pass-through (ADR-024) must hold through the race too — the
        upstream POST body must be byte-identical to what the caller
        passed in, not a re-serialised copy."""
        from audittrace.routes.chat import _stream_upstream_with_keepalive_from_t0

        sentinel_response = object()
        client = _DelayedHeaderClient(sentinel_response, delay=0.0)
        payload = {"model": "qwen3.5-35b", "messages": [], "stream": True}

        async for _ in _stream_upstream_with_keepalive_from_t0(
            client, "http://llama/chat/completions", payload, 5.0
        ):
            pass

        assert client.sent_payloads == [payload]

    @pytest.mark.asyncio
    async def test_closing_the_generator_mid_wait_cancels_the_send_task(self):
        """If the caller tears down the SSE generator while still awaiting
        upstream headers (e.g. the client disconnected during prompt-eval),
        the background send task must be cancelled — not leaked running
        forever un-awaited.

        Neuter the cleanup (drop the ``send_task.cancel()`` / await in the
        ``except BaseException`` branch) and this goes RED: the background
        task is still running, un-cancelled, after the generator closes.
        """
        from audittrace.routes.chat import _stream_upstream_with_keepalive_from_t0

        client = _ControllableHeaderClient()
        gen = _stream_upstream_with_keepalive_from_t0(
            client, "http://llama/chat/completions", {}, keepalive_interval=0.05
        )
        # Pull one keep-alive frame so we know the background send is still
        # pending, then tear the generator down mid-wait.
        first = await gen.__anext__()
        assert first == b": keep-alive\n\n"

        await gen.aclose()

        assert client.send_task_cancelled is True
