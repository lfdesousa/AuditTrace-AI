#!/usr/bin/env python3
"""Operator recovery lever — SPEC #387 Phase 2a: replay dead-lettered index rows.

**The gap this closes.** #450 gave unindexable docs a terminal dead-letter
state on ``memory_items``: ``index_failed_at_ms`` (set = terminal),
``index_attempts`` (capped at ``Settings.index_max_attempts``),
``index_failure_code``. ``IndexJanitor._scan_orphans`` deliberately excludes
dead-lettered rows (``index_failed_at_ms IS NOT NULL``) so a permanently
unindexable doc stops being re-enqueued forever — correct, but it also means
there was no operator way to REPLAY a row after the root cause is fixed
(e.g. a corrupt PDF re-uploaded correctly, or an OCR/tooling bug shipped a
fix). This CLI is that lever: it clears the three terminal columns on
targeted rows so ``IndexJanitor`` picks them back up on its next tick — it
never touches the janitor's exclusion logic itself (that stays exactly as
#450 shipped it), it only un-dead-letters the row.

**Off-gate by design.** This is operator DB tooling, not a served API —
deliberately kept OUT of ``src/audittrace/routes/**`` and
``src/audittrace/services/**`` so it never trips the ADR-049 heavy gate /
``check-commit-evidence.sh``. The reset logic is entirely self-contained in
this module; it imports the ``MemoryItem`` ORM model and the async Postgres
session factory from ``src/audittrace`` for reads/writes only.

**Fail-closed scope + dry-run (spec's non-negotiables).**

* Exactly one scope flag is required: ``--key <exact memory_items.key>``,
  ``--collection <chromadb collection name>`` (derived per row via the SAME
  ``collection_for_key``/``bare_key_from_uri`` helpers ``IndexJanitor`` uses,
  so this CLI can never disagree with the janitor about what collection a
  row belongs to), or ``--all`` (every dead-lettered row). argparse's
  mutually-exclusive **required** group enforces "exactly one" — a bare
  invocation with zero or two-plus scope flags is a parse error, not a
  silent no-op or a silent blanket match.
* **Dry-run is the default.** ``plan()`` only ever executes ``SELECT``
  statements — there is no code path from a dry-run invocation to an
  ``UPDATE``. ``--apply`` is required to reach :meth:`DeadLetterReplayer.apply`,
  which re-derives the plan and re-guards the ``UPDATE`` with the SAME
  ``index_failed_at_ms IS NOT NULL AND deleted_at_ms IS NULL`` predicate used
  to build the plan — so a row that changed state between the plan read and
  the apply write (e.g. two operators racing) can never be reset twice or
  reset after having been legitimately re-dead-lettered in between.
* Only rows that are **currently dead-lettered** (``index_failed_at_ms IS
  NOT NULL``) and **not soft-deleted** (``deleted_at_ms IS NULL``) ever
  match any scope — a pending/never-failed row or a soft-deleted row is
  never a candidate under any of the three scope flags, including ``--all``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update

from audittrace.config import get_settings
from audittrace.db.models import MemoryItem
from audittrace.db.postgres import PostgresFactory, URLPostgresFactory
from audittrace.services.index_routing import bare_key_from_uri, collection_for_key

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger("audittrace.scripts.replay_dead_lettered_index")

# Bounded read — an operator recovery tool, not a hot loop, but an unbounded
# ``SELECT`` on a pathological dead-letter backlog is still worth capping.
# Mirrors the "bounded batch" posture the rest of this pipeline already uses
# (``IndexJanitor``'s ``_JANITOR_BATCH_SIZE``, ``corpus_backfill``'s paging).
DEFAULT_QUERY_LIMIT = 10_000


class ScopeError(ValueError):
    """Raised when a :class:`ReplayScope` is constructed with != 1 selector.

    argparse's mutually-exclusive **required** group already prevents this
    at the CLI boundary; this is the defense-in-depth check for any caller
    that builds a :class:`ReplayScope` directly (tests, future callers) —
    fail-closed rather than silently defaulting to the widest scope.
    """


@dataclass(frozen=True)
class ReplayScope:
    """Exactly one of ``key`` / ``collection`` / ``all_`` must be set."""

    key: str | None = None
    collection: str | None = None
    all_: bool = False

    def __post_init__(self) -> None:
        selected = sum(
            1
            for v in (self.key is not None, self.collection is not None, self.all_)
            if v
        )
        if selected != 1:
            raise ScopeError(
                "exactly one of key/collection/all_ must be set "
                f"(got key={self.key!r} collection={self.collection!r} all_={self.all_!r})"
            )

    def describe(self) -> str:
        if self.key is not None:
            return f"--key {self.key}"
        if self.collection is not None:
            return f"--collection {self.collection}"
        return "--all"


@dataclass(frozen=True)
class MatchedRow:
    """One dead-lettered row's pre-reset state — the dry-run listing shape."""

    id: str
    key: str
    index_attempts: int
    index_failed_at_ms: int | None
    index_failure_code: str | None


@dataclass
class ReplayResult:
    matched: list[MatchedRow] = field(default_factory=list)
    reset_count: int = 0
    dry_run: bool = True

    @property
    def matched_count(self) -> int:
        return len(self.matched)


def _row_matches_collection(row: MemoryItem, collection: str) -> bool:
    """True iff *row* routes to *collection* — the SAME derivation
    ``IndexJanitor._scan_orphans`` uses (``bare_key_from_uri`` then
    ``collection_for_key``), so ``--collection`` can never disagree with
    what the janitor itself will route the row to once replayed."""
    return collection_for_key(bare_key_from_uri(row.key)) == collection


class DeadLetterReplayer:
    """Self-contained reset logic — no ``src/audittrace/services`` import of
    its own code, only the ORM model + session factory (per spec: keep the
    reset logic in ``scripts/``, never add it to ``services/**``)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def _matching_rows(
        self, scope: ReplayScope, *, limit: int = DEFAULT_QUERY_LIMIT
    ) -> list[MemoryItem]:
        """Read-only. Only rows that are dead-lettered AND not soft-deleted
        are ever candidates, under every scope including ``--all``."""
        async with self._session_factory() as session:
            stmt = (
                select(MemoryItem)
                .where(MemoryItem.index_failed_at_ms.is_not(None))
                .where(MemoryItem.deleted_at_ms.is_(None))
            )
            if scope.key is not None:
                stmt = stmt.where(MemoryItem.key == scope.key)
            stmt = stmt.order_by(MemoryItem.key).limit(limit)
            rows = list((await session.execute(stmt)).scalars())
        if scope.collection is not None:
            rows = [
                row for row in rows if _row_matches_collection(row, scope.collection)
            ]
        return rows

    async def plan(self, scope: ReplayScope) -> ReplayResult:
        """Dry-run — executes ``SELECT`` only, never mutates."""
        rows = await self._matching_rows(scope)
        matched = [
            MatchedRow(
                id=row.id,
                key=row.key,
                index_attempts=row.index_attempts,
                index_failed_at_ms=row.index_failed_at_ms,
                index_failure_code=row.index_failure_code,
            )
            for row in rows
        ]
        return ReplayResult(matched=matched, reset_count=0, dry_run=True)

    async def apply(self, scope: ReplayScope) -> ReplayResult:
        """Re-derives the plan, then resets the terminal dead-letter columns
        for exactly those rows — re-guarded at ``UPDATE`` time with the same
        dead-lettered/not-soft-deleted predicate the plan used, so a row's
        state is never trusted stale across the read->write boundary."""
        planned = await self.plan(scope)
        if not planned.matched:
            return ReplayResult(matched=[], reset_count=0, dry_run=False)
        ids = [row.id for row in planned.matched]
        async with self._session_factory() as session:
            stmt = (
                update(MemoryItem)
                .where(MemoryItem.id.in_(ids))
                .where(MemoryItem.index_failed_at_ms.is_not(None))
                .where(MemoryItem.deleted_at_ms.is_(None))
                .values(
                    index_failed_at_ms=None, index_attempts=0, index_failure_code=None
                )
            )
            result = await session.execute(stmt)
            await session.commit()
        return ReplayResult(
            matched=planned.matched, reset_count=result.rowcount or 0, dry_run=False
        )

    async def run(self, scope: ReplayScope, *, apply: bool) -> ReplayResult:
        return await (self.apply(scope) if apply else self.plan(scope))


# ── CLI ───────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.replay_dead_lettered_index",
        description=(
            "SPEC #387 Phase 2a — operator recovery lever: clears the terminal "
            "dead-letter state (index_failed_at_ms/index_attempts/"
            "index_failure_code) on targeted memory_items rows so IndexJanitor "
            "re-enqueues them on its next tick. Dry-run by default; --apply "
            "performs the update."
        ),
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--key", help="exact memory_items.key of a single row to replay")
    scope.add_argument(
        "--collection",
        help="replay every dead-lettered row routed to this ChromaDB collection "
        "(e.g. semantic, ai_research_papers)",
    )
    scope.add_argument(
        "--all",
        action="store_true",
        help="replay every dead-lettered row (dry-run by default)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the reset; default is dry-run (list matching rows, mutate nothing)",
    )
    return parser


def scope_from_args(args: argparse.Namespace) -> ReplayScope:
    return ReplayScope(key=args.key, collection=args.collection, all_=args.all)


def print_result(result: ReplayResult, scope: ReplayScope) -> None:
    mode = "DRY-RUN" if result.dry_run else "APPLY"
    print(f"replay dead-lettered index — {mode} — scope={scope.describe()}")
    print()
    if not result.matched:
        print("no dead-lettered rows matched this scope.")
        return
    for row in result.matched:
        print(
            f"  {row.key}  attempts={row.index_attempts}  "
            f"failure_code={row.index_failure_code}  failed_at_ms={row.index_failed_at_ms}"
        )
    print()
    print(f"rows matched : {len(result.matched)}")
    print(f"rows reset   : {result.reset_count}")
    if result.dry_run:
        print("dry-run — nothing mutated. Re-run with --apply to reset these rows.")
    else:
        print(
            "IndexJanitor will re-enqueue the reset rows on its next tick, once "
            "past its grace window (Settings.index_janitor_grace_seconds)."
        )


def _build_session_factory(settings: Any) -> async_sessionmaker[AsyncSession] | None:
    if not settings.database_url:
        return None
    factory: PostgresFactory = URLPostgresFactory(settings.database_url)
    return factory.get_session_factory()


async def _amain(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    args = build_parser().parse_args(argv)
    scope = scope_from_args(args)

    # Portability invariant: the Postgres URL comes from Settings
    # (AUDITTRACE_* env, laptop defaults) — never hardcoded here.
    settings = get_settings()
    session_factory = _build_session_factory(settings)
    if session_factory is None:
        print("no database_url configured — aborting", file=sys.stderr)
        return 2

    replayer = DeadLetterReplayer(session_factory)
    result = await replayer.run(scope, apply=args.apply)
    print_result(result, scope)
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
