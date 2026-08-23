"""Tests for ``scripts/audittrace-scan-dlq`` — the ADR-057 scan-pipeline
RabbitMQ DLQ operator CLI (SPEC 2026-08-23-SPEC-scan-dlq-operator-cli).

Falsifiability anchors (spec's non-negotiables):

* ``inspect`` leaves queue depth UNCHANGED (requeue proven) — every test
  in ``TestInspect`` asserts ``queue.depth`` before/after, not just
  printed output.
* ``replay`` publishes to the configured exchange/routing key + ACKs on
  success, leaves the message in place (nack-requeue) on publish
  failure — proven via a controllable ``FakeExchange`` and depth deltas.
* ``drain`` ACKs WITHOUT publishing — proven by asserting the exchange is
  never even declared during a drain.
* **The neuter proof**: ``drain --all`` (and single-id ``drain``) WITHOUT
  ``--confirm`` must refuse: non-zero exit, ZERO acks, and — the
  strongest form — the AMQP connection is never even opened. Remove the
  ``if not args.confirm`` guard in ``cmd_drain`` and
  ``test_drain_all_without_confirm_never_connects`` /
  ``test_drain_single_without_confirm_never_connects`` go RED (the fake
  ``aio_pika.connect`` spy records a call, and messages get ACKed).
* No full ``object_uri`` is ever printed — only its scheme. Proven by
  asserting the raw key path is ABSENT from captured stdout while the
  scheme IS present.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def _load_module() -> Any:
    """Load ``scripts/audittrace-scan-dlq`` — no ``.py`` suffix, so
    ``spec_from_file_location`` needs an explicit ``SourceFileLoader``
    (mirrors ``tests/test_async_persist.py::TestDlqCli._load_dlq_module``
    for the sibling Redis DLQ CLI). Registered in ``sys.modules`` BEFORE
    ``exec_module`` — the module uses ``@dataclass`` under
    ``from __future__ import annotations``, and ``dataclasses`` resolves
    each field's annotation via ``sys.modules[cls.__module__]``, which
    only exists once the module is registered."""
    path = Path(__file__).parent.parent / "scripts" / "audittrace-scan-dlq"
    loader = SourceFileLoader("audittrace_scan_dlq", str(path))
    spec = importlib.util.spec_from_loader("audittrace_scan_dlq", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    loader.exec_module(mod)
    return mod


MOD = _load_module()


def _settings(
    *,
    amqp_url: str = "amqp://guest:guest@localhost:5672/",
    exchange: str = "audittrace.scan",
    routing_key: str = "scan.requested",
) -> SimpleNamespace:
    return SimpleNamespace(
        scan_amqp_url=amqp_url,
        scan_request_exchange=exchange,
        scan_request_routing_key=routing_key,
    )


# ─────────────────────────── fake AMQP seam ───────────────────────────
#
# Mirrors the queue/channel/connection boundary
# ``tests/test_health.py::test_get_scan_dlq_message_count_passive_declares_the_dlq``
# fakes for the SAME ``aio_pika.connect`` seam — only extended with
# ack/nack(requeue) + publish so the destructive/non-destructive
# behaviours are provable, not just the passive-declare round-trip.


class FakeMessage:
    def __init__(
        self,
        body: bytes,
        headers: dict[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        self.body = body
        self.headers: dict[str, Any] | None = headers
        self.timestamp = timestamp
        self.acked = False
        self.nacked_requeue: bool | None = None


def _make_message(
    scan_id: str,
    *,
    object_uri: str = "s3://audittrace-quarantine/user-1/scan-x/report.pdf",
    reason: str = "delivery_limit",
    count: int | None = 5,
    age_seconds: float = 120.0,
    headers: dict[str, Any] | None = None,
    timestamp: datetime | None = "USE_AGE",  # type: ignore[assignment]
) -> FakeMessage:
    body = json.dumps(
        {
            "scan_id": scan_id,
            "object_uri": object_uri,
            "traceparent": "00-trace-01",
        }
    ).encode("utf-8")
    if headers is None:
        death: dict[str, Any] = {"reason": reason}
        if count is not None:
            death["count"] = count
        headers = {"x-death": [death]}
    if timestamp == "USE_AGE":
        timestamp = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return FakeMessage(body=body, headers=headers, timestamp=timestamp)


class FakeQueue:
    """A queue that behaves like RabbitMQ for ``basic.get`` +
    ack/nack(requeue): each ``get()`` pops a DISTINCT message (never
    redelivers an outstanding unacked one); ``nack(requeue=True)`` puts
    the message back so ``depth`` reflects it again; ``ack()`` removes it
    permanently.

    **Reviewer finding (2026-08-23, vacuous-neuter reject).** ``depth``
    alone canNOT distinguish "ACKed" from "fetched-and-never-resolved" —
    both remove the message from ``_store`` at ``get()`` time, so a code
    path that forgets to call ``.ack()`` after a successful action looks
    IDENTICAL to a correct one under a depth-only assertion. ``acked``
    tracks the ACK explicitly (set only inside the ``_ack`` closure,
    never inferred from ``_store`` membership) so callers can assert the
    guarded side-effect itself, not just its depth-shaped shadow.
    ``acked_messages`` mirrors the same fact at the queue level, for
    bulk-path assertions that want a single collection to check against."""

    def __init__(self, messages: list[FakeMessage]) -> None:
        self._store: list[FakeMessage] = list(messages)
        self.get_calls = 0
        self.acked_messages: list[FakeMessage] = []

    async def get(self, *, fail: bool = True) -> FakeMessage | None:
        self.get_calls += 1
        if not self._store:
            return None
        message = self._store.pop(0)

        async def _ack() -> None:
            message.acked = True
            self.acked_messages.append(message)

        async def _nack(*, requeue: bool = False) -> None:
            message.nacked_requeue = requeue
            if requeue:
                self._store.append(message)

        message.ack = _ack  # type: ignore[method-assign]
        message.nack = _nack  # type: ignore[method-assign]
        return message

    @property
    def depth(self) -> int:
        return len(self._store)


class FakeExchange:
    def __init__(self, *, fail_scan_ids: frozenset[str] = frozenset()) -> None:
        self.published: list[tuple[Any, str]] = []
        self._fail_scan_ids = fail_scan_ids

    async def publish(self, message: Any, *, routing_key: str) -> None:
        if message.message_id in self._fail_scan_ids:
            raise RuntimeError(f"broker rejected publish for {message.message_id}")
        self.published.append((message, routing_key))


class FakeChannel:
    def __init__(self, queue: FakeQueue, exchange: FakeExchange) -> None:
        self._queue = queue
        self._exchange = exchange
        self.declared_queue: tuple[str, bool] | None = None
        self.declared_exchange: tuple[str, Any, bool] | None = None

    async def declare_queue(self, name: str, *, passive: bool) -> FakeQueue:
        self.declared_queue = (name, passive)
        return self._queue

    async def declare_exchange(
        self, name: str, *, type: Any, durable: bool
    ) -> FakeExchange:  # noqa: A002
        self.declared_exchange = (name, type, durable)
        return self._exchange


class FakeConnection:
    def __init__(self, channel: FakeChannel) -> None:
        self._channel = channel
        self.closed = False

    async def channel(self) -> FakeChannel:
        return self._channel

    async def close(self) -> None:
        self.closed = True


class ConnectSpy:
    """Records whether ``aio_pika.connect`` was ever called — the
    strongest proof that the ``--confirm`` guard refuses BEFORE opening
    any broker connection."""

    def __init__(self, connection: FakeConnection | None = None) -> None:
        self.connection = connection
        self.calls: list[tuple[str, float]] = []

    async def __call__(self, url: str, timeout: float) -> FakeConnection:
        self.calls.append((url, timeout))
        assert self.connection is not None, "connect() called with no fake configured"
        return self.connection


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    messages: list[FakeMessage],
    *,
    fail_scan_ids: frozenset[str] = frozenset(),
) -> tuple[FakeQueue, FakeExchange, FakeChannel, ConnectSpy]:
    import aio_pika

    queue = FakeQueue(messages)
    exchange = FakeExchange(fail_scan_ids=fail_scan_ids)
    channel = FakeChannel(queue, exchange)
    connection = FakeConnection(channel)
    spy = ConnectSpy(connection)
    monkeypatch.setattr(aio_pika, "connect", spy)
    return queue, exchange, channel, spy


def _ns(**kwargs: Any) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


# ──────────────────────────── inspect ────────────────────────────


class TestInspect:
    async def test_empty_queue(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        queue, *_ = _wire(monkeypatch, [])
        rc = await MOD.cmd_inspect(_ns(limit=100, reason=None), _settings())
        assert rc == 0
        assert "is empty" in capsys.readouterr().out
        assert queue.depth == 0

    async def test_lists_and_leaves_depth_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        messages = [
            _make_message("scan-1", object_uri="s3://bucket/secret-key-one.pdf"),
            _make_message("scan-2", object_uri="s3://bucket/secret-key-two.pdf"),
            _make_message("scan-3", object_uri="s3://bucket/secret-key-three.pdf"),
        ]
        queue, *_ = _wire(monkeypatch, messages)
        rc = await MOD.cmd_inspect(_ns(limit=100, reason=None), _settings())
        assert rc == 0
        out = capsys.readouterr().out
        # scan_id + scheme are printed...
        for sid in ("scan-1", "scan-2", "scan-3"):
            assert sid in out
        assert "s3" in out
        # ...but the full object_uri (bucket/key) is NEVER printed.
        assert "secret-key-one" not in out
        assert "bucket/secret-key" not in out
        # THE non-destructive proof: depth is exactly what it started as.
        assert queue.depth == 3

    async def test_reason_filter_still_leaves_depth_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        messages = [
            _make_message("scan-a", reason="delivery_limit"),
            _make_message("scan-b", reason="rejected"),
        ]
        queue, *_ = _wire(monkeypatch, messages)
        rc = await MOD.cmd_inspect(_ns(limit=100, reason="rejected"), _settings())
        assert rc == 0
        out = capsys.readouterr().out
        assert "scan-b" in out
        assert "scan-a" not in out
        assert queue.depth == 2  # both requeued, filter only affects display

    async def test_reason_filter_no_match(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        messages = [_make_message("scan-a", reason="delivery_limit")]
        queue, *_ = _wire(monkeypatch, messages)
        rc = await MOD.cmd_inspect(_ns(limit=100, reason="nope"), _settings())
        assert rc == 0
        assert "No entries match filter" in capsys.readouterr().out
        assert queue.depth == 1

    async def test_limit_bounds_the_peek(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        messages = [_make_message(f"scan-{i}") for i in range(5)]
        queue, *_ = _wire(monkeypatch, messages)
        rc = await MOD.cmd_inspect(_ns(limit=2, reason=None), _settings())
        assert rc == 0
        out = capsys.readouterr().out
        assert "Total: 2 entries (of 2 peeked" in out
        assert queue.depth == 5  # the 2 peeked are requeued; the other 3 untouched

    async def test_no_amqp_url_refuses_without_connecting(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        spy = ConnectSpy()
        import aio_pika

        monkeypatch.setattr(aio_pika, "connect", spy)
        rc = await MOD.cmd_inspect(_ns(limit=100, reason=None), _settings(amqp_url=""))
        assert rc == 2
        assert spy.calls == []


# ──────────────────────────── replay ─────────────────────────────


class TestReplay:
    async def test_all_success_publishes_and_acks(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        messages = [_make_message("scan-1"), _make_message("scan-2")]
        queue, exchange, channel, _spy = _wire(monkeypatch, messages)
        settings = _settings(exchange="audittrace.scan", routing_key="scan.requested")
        rc = await MOD.cmd_replay(_ns(all=True, max=100, dlq_id=None), settings)
        assert rc == 0
        out = capsys.readouterr().out
        assert "replayed=2 still_failing=0" in out
        assert queue.depth == 0  # both left the DLQ...
        # ...and THE non-vacuous proof: both were actually ACKed, not just
        # popped-and-abandoned (which would ALSO show depth == 0 — see
        # FakeQueue's docstring). Remove ``await message.ack()`` in
        # ``cmd_replay``'s ``--all`` branch and these two go RED while
        # ``queue.depth == 0`` stays green.
        assert all(m.acked for m in messages)
        assert queue.acked_messages == messages
        assert len(exchange.published) == 2
        assert channel.declared_exchange is not None
        assert channel.declared_exchange[0] == "audittrace.scan"
        for _msg, routing_key in exchange.published:
            assert routing_key == "scan.requested"

    async def test_all_partial_failure_leaves_failing_message(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        messages = [_make_message("scan-ok"), _make_message("scan-bad")]
        queue, exchange, *_ = _wire(
            monkeypatch, messages, fail_scan_ids=frozenset({"scan-bad"})
        )
        rc = await MOD.cmd_replay(_ns(all=True, max=100, dlq_id=None), _settings())
        assert rc == 1
        out = capsys.readouterr().out
        assert "replayed=1 still_failing=1" in out
        assert queue.depth == 1  # the failing one stayed
        assert len(exchange.published) == 1
        ok_msg, bad_msg = messages
        assert ok_msg.acked is True  # the succeeding publish WAS ACKed
        assert bad_msg.acked is False  # the failing publish must NOT be
        assert bad_msg.nacked_requeue is True  # left in place, not lost

    async def test_all_empty_queue(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        _wire(monkeypatch, [])
        rc = await MOD.cmd_replay(_ns(all=True, max=100, dlq_id=None), _settings())
        assert rc == 0
        assert "nothing to replay" in capsys.readouterr().out

    async def test_single_success_leaves_others_untouched(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        messages = [
            _make_message("scan-1"),
            _make_message("scan-target"),
            _make_message("scan-3"),
        ]
        queue, exchange, *_ = _wire(monkeypatch, messages)
        rc = await MOD.cmd_replay(
            _ns(all=False, max=100, dlq_id="scan-target"), _settings()
        )
        assert rc == 0
        assert queue.depth == 2  # only the target left the queue
        assert len(exchange.published) == 1
        assert exchange.published[0][0].message_id == "scan-target"
        # THE non-vacuous proof (single-replay success): the target was
        # actually ACKed; the requeued siblings were explicitly NOT.
        assert messages[1].acked is True  # scan-target
        assert messages[0].acked is False  # scan-1, requeued
        assert messages[2].acked is False  # scan-3, requeued
        assert messages[0].nacked_requeue is True
        assert messages[2].nacked_requeue is True
        assert queue.acked_messages == [messages[1]]

    async def test_single_failure_leaves_message_in_place(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        messages = [_make_message("scan-target")]
        queue, exchange, *_ = _wire(
            monkeypatch, messages, fail_scan_ids=frozenset({"scan-target"})
        )
        rc = await MOD.cmd_replay(
            _ns(all=False, max=100, dlq_id="scan-target"), _settings()
        )
        assert rc == 1
        assert queue.depth == 1  # NOT ACKed — publish failed
        assert messages[0].acked is False
        assert messages[0].nacked_requeue is True
        assert queue.acked_messages == []

    async def test_single_not_found_requeues_everything(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        messages = [_make_message("scan-1"), _make_message("scan-2")]
        queue, exchange, *_ = _wire(monkeypatch, messages)
        rc = await MOD.cmd_replay(
            _ns(all=False, max=100, dlq_id="scan-missing"), _settings()
        )
        assert rc == 2
        assert "No entry" in capsys.readouterr().err
        assert queue.depth == 2  # nothing lost
        assert exchange.published == []
        assert not any(m.acked for m in messages)
        assert all(m.nacked_requeue for m in messages)

    async def test_no_amqp_url_refuses_without_connecting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spy = ConnectSpy()
        import aio_pika

        monkeypatch.setattr(aio_pika, "connect", spy)
        rc = await MOD.cmd_replay(
            _ns(all=True, max=100, dlq_id=None), _settings(amqp_url="")
        )
        assert rc == 2
        assert spy.calls == []


# ──────────────────────────── drain ──────────────────────────────


class TestDrainConfirmGuard:
    """THE non-vacuous neuter proof (spec's non-negotiable): remove the
    ``if not args.confirm`` check in ``cmd_drain`` and every assertion
    below goes RED — the connect spy would record a call and messages
    would be ACKed."""

    async def test_all_without_confirm_never_connects_zero_acks(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        spy = ConnectSpy()
        import aio_pika

        monkeypatch.setattr(aio_pika, "connect", spy)
        rc = await MOD.cmd_drain(
            _ns(all=True, dlq_id=None, reason=None, older_than=None, confirm=False),
            _settings(),
        )
        assert rc != 0
        assert "Refusing to drain without --confirm" in capsys.readouterr().err
        assert spy.calls == []  # the broker was never even contacted

    async def test_single_without_confirm_never_connects_zero_acks(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        spy = ConnectSpy()
        import aio_pika

        monkeypatch.setattr(aio_pika, "connect", spy)
        rc = await MOD.cmd_drain(
            _ns(
                all=False,
                dlq_id="scan-1",
                reason=None,
                older_than=None,
                confirm=False,
            ),
            _settings(),
        )
        assert rc != 0
        assert spy.calls == []


class TestDrain:
    async def test_all_with_confirm_acks_without_publishing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        messages = [_make_message("scan-1"), _make_message("scan-2")]
        queue, exchange, channel, _spy = _wire(monkeypatch, messages)
        rc = await MOD.cmd_drain(
            _ns(all=True, dlq_id=None, reason=None, older_than=None, confirm=True),
            _settings(),
        )
        assert rc == 0
        assert "Drained 2 entries (kept 0)" in capsys.readouterr().out
        assert queue.depth == 0
        # THE non-vacuous proof (drain bulk-ack): both were actually
        # ACKed, not merely fetched-and-abandoned (which would ALSO leave
        # depth == 0 — see FakeQueue's docstring). Remove
        # ``await message.ack()`` in ``cmd_drain``'s ``--all`` branch and
        # this goes RED while ``queue.depth == 0`` stays green.
        assert all(m.acked for m in messages)
        assert queue.acked_messages == messages
        # drain never opens the exchange at all — no republish path.
        assert channel.declared_exchange is None
        assert exchange.published == []

    async def test_all_reason_filter_keeps_non_matching(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        messages = [
            _make_message("scan-drop", reason="delivery_limit"),
            _make_message("scan-keep", reason="rejected"),
        ]
        queue, *_ = _wire(monkeypatch, messages)
        rc = await MOD.cmd_drain(
            _ns(
                all=True,
                dlq_id=None,
                reason="delivery_limit",
                older_than=None,
                confirm=True,
            ),
            _settings(),
        )
        assert rc == 0
        assert "Drained 1 entries (kept 1)" in capsys.readouterr().out
        assert queue.depth == 1  # scan-keep survives
        drop_msg, keep_msg = messages
        assert drop_msg.acked is True
        assert keep_msg.acked is False
        assert keep_msg.nacked_requeue is True

    async def test_all_older_than_filter_keeps_recent(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        messages = [
            _make_message("scan-old", age_seconds=40 * 86400),  # 40 days
            _make_message("scan-recent", age_seconds=60),  # 1 minute
        ]
        queue, *_ = _wire(monkeypatch, messages)
        rc = await MOD.cmd_drain(
            _ns(all=True, dlq_id=None, reason=None, older_than=30, confirm=True),
            _settings(),
        )
        assert rc == 0
        assert "Drained 1 entries (kept 1)" in capsys.readouterr().out
        assert queue.depth == 1
        old_msg, recent_msg = messages
        assert old_msg.acked is True
        assert recent_msg.acked is False
        assert recent_msg.nacked_requeue is True

    async def test_all_older_than_fail_closed_on_missing_timestamp(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        """A message with an unparseable/missing age must NEVER be
        treated as old enough to drop — fail-closed."""
        messages = [_make_message("scan-unknown-age", timestamp=None)]
        queue, *_ = _wire(monkeypatch, messages)
        rc = await MOD.cmd_drain(
            _ns(all=True, dlq_id=None, reason=None, older_than=1, confirm=True),
            _settings(),
        )
        assert rc == 0
        assert "Drained 0 entries (kept 1)" in capsys.readouterr().out
        assert queue.depth == 1
        assert messages[0].acked is False
        assert messages[0].nacked_requeue is True

    async def test_single_success(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        messages = [
            _make_message("scan-1"),
            _make_message("scan-target"),
        ]
        queue, *_ = _wire(monkeypatch, messages)
        rc = await MOD.cmd_drain(
            _ns(
                all=False,
                dlq_id="scan-target",
                reason=None,
                older_than=None,
                confirm=True,
            ),
            _settings(),
        )
        assert rc == 0
        assert queue.depth == 1  # only scan-1 remains
        assert messages[1].acked is True  # scan-target
        assert messages[0].acked is False  # scan-1, requeued
        assert messages[0].nacked_requeue is True

    async def test_single_not_found(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        messages = [_make_message("scan-1")]
        queue, *_ = _wire(monkeypatch, messages)
        rc = await MOD.cmd_drain(
            _ns(
                all=False,
                dlq_id="scan-missing",
                reason=None,
                older_than=None,
                confirm=True,
            ),
            _settings(),
        )
        assert rc == 2
        assert "No entry" in capsys.readouterr().err
        assert queue.depth == 1  # nothing lost
        assert messages[0].acked is False
        assert messages[0].nacked_requeue is True

    async def test_no_amqp_url_refuses_without_connecting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spy = ConnectSpy()
        import aio_pika

        monkeypatch.setattr(aio_pika, "connect", spy)
        rc = await MOD.cmd_drain(
            _ns(all=True, dlq_id=None, reason=None, older_than=None, confirm=True),
            _settings(amqp_url=""),
        )
        assert rc == 2
        assert spy.calls == []


# ──────────────────────────── pure-function unit tests ─────────────


class TestDeathInfo:
    def test_no_headers(self) -> None:
        assert MOD._death_info(None) == ("unknown", "?")

    def test_no_x_death_key(self) -> None:
        assert MOD._death_info({}) == ("unknown", "?")

    def test_empty_deaths_list(self) -> None:
        assert MOD._death_info({"x-death": []}) == ("unknown", "?")

    def test_normal_shape(self) -> None:
        reason, attempt = MOD._death_info(
            {"x-death": [{"reason": "delivery_limit", "count": 5}]}
        )
        assert reason == "delivery_limit"
        assert attempt == "5"

    def test_missing_count(self) -> None:
        reason, attempt = MOD._death_info({"x-death": [{"reason": "rejected"}]})
        assert reason == "rejected"
        assert attempt == "?"

    def test_malformed_entry_degrades_gracefully(self) -> None:
        assert MOD._death_info({"x-death": ["not-a-dict"]}) == ("unknown", "?")


class TestUriScheme:
    def test_s3_scheme(self) -> None:
        assert MOD._uri_scheme("s3://bucket/key.pdf") == "s3"

    def test_no_scheme(self) -> None:
        assert MOD._uri_scheme("not-a-uri") == "?"

    def test_empty(self) -> None:
        assert MOD._uri_scheme("") == "?"


class TestFormatAge:
    def test_negative_is_unknown(self) -> None:
        assert MOD._format_age(-1.0) == "?"

    def test_seconds(self) -> None:
        assert MOD._format_age(30) == "30s"

    def test_minutes(self) -> None:
        assert MOD._format_age(120) == "2m"

    def test_hours(self) -> None:
        assert MOD._format_age(7200) == "2.0h"

    def test_days(self) -> None:
        assert MOD._format_age(2 * 86400) == "2.0d"


class TestOlderThan:
    def test_missing_timestamp_is_never_old(self) -> None:
        msg = _make_message("x", timestamp=None)
        assert MOD._older_than(msg, 1) is False

    def test_old_message_is_older(self) -> None:
        msg = _make_message("x", age_seconds=10 * 86400)
        assert MOD._older_than(msg, 5) is True

    def test_recent_message_is_not_older(self) -> None:
        msg = _make_message("x", age_seconds=10)
        assert MOD._older_than(msg, 5) is False

    def test_naive_timestamp(self) -> None:
        msg = _make_message("x", timestamp=datetime.now() - timedelta(days=10))
        assert MOD._older_than(msg, 5) is True


class TestParsePayload:
    def test_valid_json(self) -> None:
        body = json.dumps({"scan_id": "s1"}).encode("utf-8")
        assert MOD._parse_payload(body) == {"scan_id": "s1"}

    def test_invalid_json_degrades_to_empty_dict(self) -> None:
        assert MOD._parse_payload(b"not json") == {}

    def test_invalid_utf8_degrades_to_empty_dict(self) -> None:
        assert MOD._parse_payload(b"\xff\xfe") == {}


# ──────────────────────────── CLI parsing / main() ───────────────────


class TestMainArgParsing:
    def test_replay_requires_id_or_all(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            MOD.main(["replay"])
        assert exc_info.value.code == 2

    def test_drain_requires_id_or_all(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            MOD.main(["drain"])
        assert exc_info.value.code == 2

    def test_drain_all_without_confirm_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        spy = ConnectSpy()
        import aio_pika

        monkeypatch.setattr(aio_pika, "connect", spy)
        monkeypatch.setattr(MOD, "_load_settings", lambda: _settings())
        rc = MOD.main(["drain", "--all"])
        assert rc != 0
        assert spy.calls == []

    def test_inspect_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        messages = [_make_message("scan-e2e")]
        queue, *_ = _wire(monkeypatch, messages)
        monkeypatch.setattr(MOD, "_load_settings", lambda: _settings())
        rc = MOD.main(["inspect", "--limit", "5"])
        assert rc == 0
        assert "scan-e2e" in capsys.readouterr().out
        assert queue.depth == 1
