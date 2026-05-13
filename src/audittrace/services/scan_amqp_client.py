"""Thin aio_pika wrapper for the ADR-048 scan-request producer.

Discipline (Luis 2026-05-10 — systematic context-manager use):

- Lazy connect on first ``ensure_connected()``. Construction never
  reaches out to the broker, so the rest of the service starts
  cleanly when ``Settings.scan_pipeline_enabled=False`` or RabbitMQ
  is offline.
- ``__aenter__`` / ``__aexit__`` so the lifespan composes
  ``async with`` (or ``AsyncExitStack.enter_async_context``)
  rather than hand-rolling try/finally. Idempotent on aclose,
  so the manager nests safely.
- ``aio_pika.connect_robust`` handles auto-reconnect; we don't
  hand-roll retry. The publisher service treats publish failures as
  "leave ``published_at_ms`` NULL; janitor re-enqueues" — back-pressure
  is owned upstream.

Closed-set: only one entry-point — ``publish_scan_request``. The
caller owns serialization (the Pydantic model that snapshots
``contracts/v1.yaml::ScanRequest``).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from audittrace.config import Settings

logger = logging.getLogger(__name__)


class ScanAmqpClient:
    """Topic-exchange publisher for scan-request messages.

    Usage as an async context manager::

        async with ScanAmqpClient(settings) as client:
            await client.publish_scan_request(payload)
        # client is automatically aclosed on exit
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._connection: Any = None
        self._channel: Any = None
        self._exchange: Any = None

    # PR-B10 — retry budget for the initial AMQP connection. The
    # cumulative max wait is the sum of backoff sleeps:
    #   1 + 2 + 4 + 8 + 16 + 32 = 63 s of sleeping
    # plus up to 6 × CONNECT_TIMEOUT_SECONDS of in-flight connects.
    # That bracket comfortably covers fresh-cluster cases where
    # kube-proxy hasn't programmed the Service IP yet AND single-node
    # kind boots where RabbitMQ readiness lags the chart install.
    _CONNECT_MAX_ATTEMPTS: int = 6
    _CONNECT_TIMEOUT_SECONDS: float = 10.0

    async def ensure_connected(self) -> None:
        """Open the connection + channel + exchange handle on first
        call. Idempotent — repeat calls are no-ops.

        **Initial-connect resilience** (PR-B10): ``aio_pika.connect_robust``
        auto-reconnects AFTER a first successful connection, but raises
        on first-time ``ConnectionRefusedError`` /
        ``AMQPConnectionError``. On a fresh cluster install, kube-proxy
        may not have programmed the RabbitMQ Service IP yet by the time
        memory-server's lifespan runs — the kubelet-level readiness
        probe (and the chart's post-install hook) finish before kube-
        proxy's reconciliation loop, so the Service IP exists but isn't
        routable. We wrap the initial connect in an exponential-backoff
        retry so transient ``OSError`` / ``ConnectionError`` from that
        race window doesn't kill the lifespan.

        **Idempotent exchange declare** (PR-B10): switched from
        ``get_exchange(name, passive=True)`` to
        ``declare_exchange(name, type=topic, durable=True)``. Active
        declare is idempotent — RabbitMQ no-ops if the exchange
        already exists with matching args. This eliminates the
        chicken-and-egg between memory-server's lifespan and the
        chart's ``job-amqp-topology-bootstrap`` post-install hook:
        whichever side runs first declares the exchange; the other
        side adopts the existing handle. The bootstrap Job still owns
        queue + binding declaration.
        """
        if self._exchange is not None:
            return
        if not self._settings.scan_amqp_url:
            raise RuntimeError(
                "scan_amqp_url is required when scan_pipeline_enabled=true"
            )
        import asyncio  # noqa: PLC0415

        import aio_pika  # noqa: PLC0415 — avoid import on disabled paths

        last_exc: Exception | None = None
        for attempt in range(self._CONNECT_MAX_ATTEMPTS):
            try:
                self._connection = await aio_pika.connect_robust(
                    self._settings.scan_amqp_url,
                    timeout=self._CONNECT_TIMEOUT_SECONDS,
                )
                break
            except (TimeoutError, ConnectionError, OSError) as exc:
                last_exc = exc
                if attempt == self._CONNECT_MAX_ATTEMPTS - 1:
                    break
                delay = 2**attempt
                logger.warning(
                    "scan_amqp.connect_retry",
                    extra={
                        "attempt": attempt + 1,
                        "max_attempts": self._CONNECT_MAX_ATTEMPTS,
                        "delay_seconds": delay,
                        "reason": str(exc),
                    },
                )
                await asyncio.sleep(delay)
        else:  # pragma: no cover — loop only exits via break or final-attempt
            pass
        if self._connection is None:
            raise RuntimeError(
                f"scan_amqp.connect failed after {self._CONNECT_MAX_ATTEMPTS} "
                f"attempts (last error: {last_exc})"
            ) from last_exc

        self._channel = await self._connection.channel()
        # ``declare_exchange`` (active, not passive) so memory-server can
        # bootstrap the exchange itself on fresh clusters. RabbitMQ
        # idempotently accepts re-declares with matching arguments,
        # so this composes safely with the chart's bootstrap Job.
        self._exchange = await self._channel.declare_exchange(
            self._settings.scan_request_exchange,
            type=aio_pika.ExchangeType.TOPIC,
            durable=True,
        )
        logger.info(
            "scan_amqp.connected",
            extra={"exchange": self._settings.scan_request_exchange},
        )

    async def publish_scan_request(self, payload: dict[str, Any]) -> None:
        """Publish ``payload`` as a JSON message on the configured
        topic exchange. Persistent delivery so a broker restart does
        not lose in-flight requests.

        The caller (``ScanRequestPublisher``) translates exceptions
        into "leave the manifest row's published_at_ms NULL"; the
        janitor's grace-window query catches the orphan."""
        await self.ensure_connected()
        import aio_pika  # noqa: PLC0415

        body = json.dumps(payload).encode("utf-8")
        message = aio_pika.Message(
            body=body,
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            timestamp=datetime.now(UTC),
            message_id=str(payload.get("scan_id", "")),
            headers={
                # W3C trace context propagation. Content-control
                # extracts ``traceparent`` to stitch the cross-service
                # span tree.
                "traceparent": str(payload.get("traceparent", "")),
            },
        )
        assert self._exchange is not None
        await self._exchange.publish(
            message,
            routing_key=self._settings.scan_request_routing_key,
        )
        logger.info(
            "scan_amqp.published",
            extra={
                "scan_id": payload.get("scan_id"),
                "routing_key": self._settings.scan_request_routing_key,
            },
        )

    async def __aenter__(self) -> Self:
        await self.ensure_connected()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close channel + connection. Idempotent."""
        if self._channel is not None:
            try:
                await self._channel.close()
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "scan_amqp.channel_close_failed",
                    extra={"reason": str(exc)},
                )
            finally:
                self._channel = None
        if self._connection is not None:
            try:
                await self._connection.close()
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "scan_amqp.connection_close_failed",
                    extra={"reason": str(exc)},
                )
            finally:
                self._connection = None
        self._exchange = None
