"""Session-memory GC janitor — WU-6 Part A of the Sovereign-Attach EPIC.

Periodic bounded hard-DELETE sweep of ``session_memory_items`` rows past
the retention window. The ``session`` layer (WU-1) is ephemeral by
ratified design (A-b decision, spec §2.2): there is no corpus tier and no
soft-delete/tombstone concept for it, so GC here is a genuine hard delete,
never a flag flip — a promoted (WU-4) durable copy lives in a completely
different table and is structurally untouched by this sweep
(:meth:`~audittrace.services.session_memory.SessionMemoryService.gc_expired`'s
docstring spells out why).

Modeled on ``services/index_janitor.py``'s shape (an ``asyncio.create_task``
loop started in ``server.py``'s lifespan, own interval knob, a bounded
per-call batch, cancelled cleanly on shutdown) with one deliberate
difference: ``IndexJanitor``/``ScanRequestJanitor`` push ONE bounded batch
per tick onto a queue for a DOWNSTREAM worker to drain the rest on its own
schedule. There is no downstream worker here — the DELETE is the terminal
action — so a tick that finds a full batch still eligible loops
immediately (:meth:`SessionGCJanitor._sweep_once`) rather than waiting a
whole ``session_gc_interval_seconds`` to make further progress on a
backlog.

Flag-gated by ``AUDITTRACE_SESSION_GC_ENABLED`` (default on) at the
``server.py`` lifespan call site — this module itself has no opinion on
the flag; neutering the gate at the call site (running the task
unconditionally) is the non-vacuity guard spec §2.5 guard 4 names.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from audittrace.config import Settings
    from audittrace.services.session_memory import SessionMemoryService

logger = logging.getLogger(__name__)

# Bounded batch keeps the janitor's per-call delete cost predictable —
# same rationale as IndexJanitor/ScanRequestJanitor's _JANITOR_BATCH_SIZE.
# This is the guard spec §2.5 guard 2 names: neuter it (pass an unbounded
# limit) and the batch-cap test goes RED.
_SESSION_GC_BATCH_SIZE = 100


def _now_ms() -> int:
    return int(time.time() * 1000)


class SessionGCJanitor:
    """Periodic bounded hard-DELETE of expired ``session_memory_items``."""

    def __init__(
        self,
        *,
        settings: Settings,
        service: SessionMemoryService,
    ) -> None:
        self._settings = settings
        self._service = service

    async def _sweep_once(self) -> int:
        """One tick: delete expired rows in bounded batches until fewer
        than a full batch remain eligible. Returns the TOTAL count
        deleted across every batch this tick (may be zero).

        Looping here (rather than a single bounded call per tick) is what
        lets the janitor drain a backlog faster than one batch per
        ``session_gc_interval_seconds`` — stopping the instant a call
        returns fewer than ``_SESSION_GC_BATCH_SIZE`` is what bounds the
        loop itself (a call returning exactly a full batch means more
        rows may still be eligible; anything less means the sweep for
        this cutoff is exhausted)."""
        cutoff = _now_ms() - (self._settings.session_retention_hours * 3600 * 1000)
        total = 0
        while True:
            deleted = await self._service.gc_expired(
                older_than_ms=cutoff, limit=_SESSION_GC_BATCH_SIZE
            )
            total += deleted
            if deleted < _SESSION_GC_BATCH_SIZE:
                break
        return total

    async def run(self) -> None:
        logger.info(
            "session_gc_janitor.run.start",
            extra={
                "interval_s": self._settings.session_gc_interval_seconds,
                "retention_h": self._settings.session_retention_hours,
            },
        )
        try:
            while True:
                try:
                    await self._sweep_once()
                except Exception as exc:
                    logger.error(
                        "session_gc_janitor.tick_failed",
                        extra={"reason": str(exc)},
                    )
                await asyncio.sleep(self._settings.session_gc_interval_seconds)
        except asyncio.CancelledError:
            logger.info("session_gc_janitor.run.cancelled")
            raise
