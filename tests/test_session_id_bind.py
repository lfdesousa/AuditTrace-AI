"""SESSION-ID-BIND (v1.31.0 WP-6), UNIT layer: the token-returning binder, the
``bound_session_id`` context manager, the stream re-bind wrapper, the
``TraceContextFilter`` precedence for ``session_id`` (WP-3 NB-1), and the
source-level pins. The INTEGRATION layer (the real app through ``create_app()``
+ ``TestClient``: chat inject/tools x stream/non-stream, MCP ``tools/call``) is
``test_session_id_bind_route.py``; the two layers are not mixed.

Spec ``2026-10-25-SPEC-v1310-telemetry-closeout.md`` WP-6, ADDENDUM-1 C14
(token-returning binder, unit-level AC-6.3) and ADDENDUM-2 S8 (reset after the
stream). Cell names (AC-6.1 ... AC-6.3) are the spec's.

Mocks declared: none. The Filter is exercised through the PRODUCTION bootstrap
(``tests/_log_trace_id_support.bootstrap``), the ContextVar through the real
console-store module, the stream wrapper with hand-written async generators.
No clock, no network, no wall-clock assertion.

Neuters (each run ALONE, RED recorded in the build record):
  N-6.3  drop ``reset_session_id`` from ``bound_session_id``
  N-6.3s drop the ``with bound_session_id`` from ``stream_with_bound_session``
  N-6.3c drop the ``aclosing`` from ``stream_with_bound_session``
  N-F1   the Filter overwrites an explicit ``session_id`` (drop "only when absent")
  N-F2   the Filter stamps ``session_id=None`` when nothing is bound
"""

from __future__ import annotations

import io
from collections.abc import AsyncGenerator, Iterator
from contextvars import Token

import pytest

from audittrace.routes._session_bind import stream_with_bound_session
from audittrace.services.console_store._context import (
    bind_session_id,
    bound_session_id,
    current_session_id,
    reset_session_id,
)
from tests._log_trace_id_support import (
    PROBE_LOG,
    SESSION,
    bootstrap,
    isolated_logging_state,
    log_line,
)


@pytest.fixture(autouse=True)
def _clean_binding() -> Iterator[None]:
    """Nothing may be bound before or after a test in this file (the binder is
    process-wide state; a failing test must not poison the next one)."""
    bind_session_id(None)
    yield
    bind_session_id(None)


@pytest.fixture
def product_logging() -> Iterator[io.StringIO]:
    with isolated_logging_state():
        yield bootstrap()


# ---------------------------------------------------------------------------
# AC-6.3 (unit, ADDENDUM-1 C14): bind -> assert -> reset -> unset
# ---------------------------------------------------------------------------
def test_ac_6_3_bind_returns_a_token_and_reset_restores_the_previous_value() -> None:
    """The binder returns the ContextVar token; resetting it restores exactly the
    value it replaced (not just ``None``).

    Neuter: ``bind_session_id`` returning ``None`` -> RED (type pin).
    """
    first = bind_session_id("outer")
    assert isinstance(first, Token)
    second = bind_session_id("inner")
    assert current_session_id() == "inner"
    reset_session_id(second)
    assert current_session_id() == "outer"
    reset_session_id(first)
    assert current_session_id() is None


def test_ac_6_3_the_context_manager_unsets_the_id_after_the_block() -> None:
    """AC-6.3. After the block the ContextVar is unset.

    Neuter N-6.3 (drop the reset in the context manager) -> RED.
    """
    assert current_session_id() is None
    with bound_session_id(SESSION):
        assert current_session_id() == SESSION
    assert current_session_id() is None


def test_ac_6_3_nested_blocks_restore_the_enclosing_value() -> None:
    """The restore is the PREVIOUS value, so a nested bind never clears the
    enclosing request's id. Neuter N-6.3 -> RED."""
    with bound_session_id("outer"):
        with bound_session_id("inner"):
            assert current_session_id() == "inner"
        assert current_session_id() == "outer"
    assert current_session_id() is None


def test_ac_6_3_the_id_is_restored_when_the_block_raises() -> None:
    """Every exit path restores (the ``finally`` of the context manager).

    Neuter N-6.3 -> RED."""
    with pytest.raises(RuntimeError, match="boom"):
        with bound_session_id(SESSION):
            raise RuntimeError("boom")
    assert current_session_id() is None


async def test_ac_6_3_no_bleed_into_the_next_request_on_the_same_task() -> None:
    """Two requests served one after the other on ONE task (what a reused worker
    task does): the second never sees the first one's id.

    Neuter N-6.3 -> RED.
    """
    seen: list[str | None] = []

    async def request(session: str | None) -> None:
        with bound_session_id(session):
            seen.append(current_session_id())

    await request("request-one")
    assert current_session_id() is None
    await request(None)
    assert seen == ["request-one", None]


# ---------------------------------------------------------------------------
# S8: the stream re-bind wrapper
# ---------------------------------------------------------------------------
async def _frames(
    seen: list[str | None], *, raise_after: int | None = None
) -> AsyncGenerator[bytes, None]:
    try:
        for i in range(3):
            seen.append(current_session_id())
            yield f"data: {i}\n\n".encode()
            if raise_after is not None and i == raise_after:
                raise RuntimeError("upstream broke")
    finally:
        seen.append(current_session_id())  # the inner finally runs INSIDE the bind


async def test_s8_the_stream_body_runs_with_the_id_bound_and_unbinds_after() -> None:
    """Every frame is produced with ``session_id`` bound, the bytes are
    unchanged, and the id is unset after exhaustion.

    Neuter N-6.3s (drop the ``with`` in the wrapper) -> RED.
    """
    seen: list[str | None] = []
    body = [c async for c in stream_with_bound_session(SESSION, _frames(seen))]
    assert body == [b"data: 0\n\n", b"data: 1\n\n", b"data: 2\n\n"]
    assert seen == [SESSION] * 4  # 3 frames + the inner finally
    assert current_session_id() is None


async def test_s8_an_early_close_closes_the_inner_body_inside_the_bind() -> None:
    """A client disconnect (``aclose`` after the first frame): the inner
    generator's ``finally`` runs while the id is still bound, then it is unset.

    Neuters: N-6.3c (drop ``aclosing``: the inner finalises later, outside the
    bind) and N-6.3s -> RED.
    """
    seen: list[str | None] = []
    stream = stream_with_bound_session(SESSION, _frames(seen))
    assert await anext(stream) == b"data: 0\n\n"
    await stream.aclose()
    assert seen == [SESSION, SESSION]  # frame 0, then the inner finally
    assert current_session_id() is None


async def test_s8_an_error_inside_the_stream_still_unbinds() -> None:
    """The inner generator raising mid-stream propagates AND the id is unset;
    the first frame was produced with the id bound.

    Neuters: N-6.3r (drop the reset: the id stays bound after the error) and
    N-6.3s (drop the bind: the first frame saw nothing) -> RED."""
    seen: list[str | None] = []
    stream = stream_with_bound_session(SESSION, _frames(seen, raise_after=0))
    assert await anext(stream) == b"data: 0\n\n"
    assert seen == [SESSION]
    with pytest.raises(RuntimeError, match="upstream broke"):
        await anext(stream)
    assert current_session_id() is None


# ---------------------------------------------------------------------------
# WP-3 NB-1: the Filter stamps ``session_id`` only when the record has none
# ---------------------------------------------------------------------------
def test_nb1_an_explicit_session_id_on_the_record_is_kept(
    product_logging: io.StringIO,
) -> None:
    """A record that already carries a non-empty ``session_id`` (explicit
    ``extra=``) keeps it even while a DIFFERENT id is bound: the Filter stamps
    only when absent.

    Neuter N-F1 (``if bound_session:`` without the "not already set" clause) -> RED.
    """
    with bound_session_id(SESSION):
        PROBE_LOG.info("explicit.line", extra={"session_id": "explicit-wins"})
    assert log_line(product_logging, "explicit.line")["session_id"] == "explicit-wins"


def test_nb1_an_empty_explicit_value_is_replaced_by_the_bound_id(
    product_logging: io.StringIO,
) -> None:
    """The precedence is on a NON-EMPTY value: an empty ``extra`` value does not
    block the bound id. Together with the test above this pins both halves of
    ``if bound_session and not getattr(record, "session_id", None)``.

    Neuter: replace the ``getattr`` clause with ``hasattr`` -> RED.
    """
    with bound_session_id(SESSION):
        PROBE_LOG.info("empty.line", extra={"session_id": ""})
    assert log_line(product_logging, "empty.line")["session_id"] == SESSION


def test_nb1_nothing_bound_and_nothing_explicit_means_no_key(
    product_logging: io.StringIO,
) -> None:
    """Unbound and no explicit value: the key is ABSENT (no ``null``, no
    sentinel).

    Neuter N-F2 (drop the ``bound_session`` truth test, stamping ``None``) -> RED.
    """
    PROBE_LOG.info("unbound.line")
    assert "session_id" not in log_line(product_logging, "unbound.line")


def test_nb1_the_explicit_value_still_wins_with_nothing_bound(
    product_logging: io.StringIO,
) -> None:
    """With nothing bound an explicit ``extra`` value reaches the line as is."""
    PROBE_LOG.info("explicit.only", extra={"session_id": "explicit-only"})
    assert log_line(product_logging, "explicit.only")["session_id"] == "explicit-only"
