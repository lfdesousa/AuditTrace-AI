"""INDEX-STATUS-PENDING-PREDICATE: one "pending index" predicate, three readers.

``/health`` (``unindexed_count``), ``IndexJanitor._scan_orphans`` and
``MemoryManifestService.index_status_summary`` must select the SAME rows as
"pending index": clean-scanned, not indexed, not dead-lettered, not deleted.
The summary feeds ``corpus_status`` in every recall tool result, so a wrong
count or a wrong match is what the model is told.

The fixture is the full product of the four columns the predicate reads, with
the scan-status vocabulary taken from the product's own closed set, so no row
state is chosen by hand. Expectations are equalities between the readers, plus
one independent statement of the rule (``_d1`` / ``_d2`` below) that the shared
module and one reader are pinned to, so a change inside the shared module
cannot move all readers together unseen.

Wording (ADDENDUM-1, B1): a row written by an ordinary create carries no index
state. The manifest does not know whether that note is in the vector store, so
it makes no claim about it, in either direction.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import itertools
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from audittrace import dependencies
from audittrace.db.models import MemoryItem
from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.identity import UserContext
from audittrace.routes import health as health_mod
from audittrace.routes.memory_scan import _SCAN_STATUS_CODES
from audittrace.services.index_janitor import IndexJanitor
from audittrace.services.memory_manifest import (
    MemoryManifestService,
    MockMemoryManifestService,
)
from audittrace.tools import memory_handlers

ROOT = Path(__file__).resolve().parents[1]
SHARED_MODULE = "audittrace.db.index_predicates"
SITES = (
    "src/audittrace/routes/health.py",
    "src/audittrace/services/index_janitor.py",
    "src/audittrace/services/memory_manifest.py",
    "scripts/replay_dead_lettered_index.py",
)
OUTBOX_COLUMNS = {"indexed_at_ms", "index_failed_at_ms"}

# None = a row that never entered the scan pipeline (every ordinary write).
SCAN_STATES: tuple[str | None, ...] = (None, *sorted(_SCAN_STATUS_CODES))
STATES = tuple(
    itertools.product(SCAN_STATES, (None, 1), (None, 1), (False, True))
)  # scan_status x indexed_at_ms x index_failed_at_ms x deleted
OLD_MS = 1  # far older than any grace window: the janitor's extra clause is inert


def _slug(i: int) -> str:
    return f"zq{i:03d}zq"  # letter-led, unique, never a substring of another


def _d1(
    scan: str | None, indexed: int | None, failed: int | None, deleted: bool
) -> bool:
    """SPEC D1 (pending index), stated once and independently of the product code."""
    return (
        scan == "scanned_clean" and indexed is None and failed is None and not deleted
    )


def _d2(
    scan: str | None, indexed: int | None, failed: int | None, deleted: bool
) -> bool:
    """SPEC D2 (dead-lettered), stated once and independently of the product code."""
    return failed is not None and not deleted


WANT_PENDING = {_slug(i) for i, state in enumerate(STATES) if _d1(*state)}
WANT_DEAD = {_slug(i) for i, state in enumerate(STATES) if _d2(*state)}


def _key(i: int) -> str:
    return f"s3://memory-shared/episodic/papers/{_slug(i)}/x.pdf"


def _admin() -> UserContext:
    return UserContext(
        user_id="admin", username="admin", agent_type="test", scopes=(), is_admin=True
    )


def _settings() -> MagicMock:
    s = MagicMock()
    s.index_janitor_grace_seconds = 60
    s.scan_janitor_grace_seconds = 60
    s.scan_enabled = False
    return s


@pytest_asyncio.fixture
async def session_factory():
    factory = InMemoryPostgresFactory()
    await factory.create_schema()
    dependencies.container._instances["postgres_factory"] = factory
    yield factory.get_session_factory()
    dependencies.container._instances.pop("postgres_factory", None)


@pytest_asyncio.fixture
async def product(session_factory):
    """One corpus-tier row per state of the product."""
    async with session_factory() as session:
        for i, (scan, indexed, failed, deleted) in enumerate(STATES):
            session.add(
                MemoryItem(
                    id=_slug(i),
                    layer="episodic",
                    key=_key(i),
                    title=None,
                    size_bytes=1,
                    created_at_ms=OLD_MS,
                    modified_at_ms=OLD_MS,
                    created_by_user_id="alice",
                    modified_by_user_id="alice",
                    deleted_at_ms=OLD_MS if deleted else None,
                    tier="corpus",
                    scan_status=scan,
                    indexed_at_ms=indexed,
                    index_failed_at_ms=failed,
                    index_failure_code="max_attempts_exceeded" if failed else None,
                )
            )
        await session.commit()
    return session_factory


async def _health(monkeypatch) -> dict[str, str]:
    monkeypatch.setattr(health_mod, "get_settings", _settings)
    return await health_mod._scan_index_health_fields()


async def _janitor_ids(session_factory) -> set[str]:
    janitor = IndexJanitor(
        settings=_settings(), session_factory=session_factory, queue=asyncio.Queue()
    )
    return {env.scan_id for env in await janitor._scan_orphans()}


async def _matched_states(svc) -> dict[str, str]:
    """slug -> state for every row the summary names, one token per row (the
    summary caps a single answer at five, so rows are asked for one by one)."""
    out: dict[str, str] = {}
    for i in range(len(STATES)):
        summary = await svc.index_status_summary(_admin(), [_slug(i)])
        for m in summary.matched:
            out[_slug(i)] = m.state
    return out


class TestOnePendingPredicate:
    async def test_summary_count_equals_health_and_janitor(
        self, product, monkeypatch
    ) -> None:
        health = await _health(monkeypatch)
        janitor = await _janitor_ids(product)
        summary = await MemoryManifestService(product).index_status_summary(
            _admin(), []
        )
        assert janitor, "positive control: the product holds a pending row"
        assert summary.pending == int(health["unindexed_count"]) == len(janitor)
        assert summary.dead_lettered == int(health["dead_lettered_index_count"])

    async def test_pending_set_is_exactly_d1(self, product, monkeypatch) -> None:
        """Anchors the readers to the rule itself, not only to each other:
        dropping any clause inside the shared module moves all three readers
        together and only this test and the two-forms test can see it."""
        health = await _health(monkeypatch)
        assert await _janitor_ids(product) == WANT_PENDING
        assert int(health["unindexed_count"]) == len(WANT_PENDING)
        assert int(health["dead_lettered_index_count"]) == len(WANT_DEAD)

    async def test_rows_named_pending_are_the_janitor_selection(
        self, product, monkeypatch
    ) -> None:
        health = await _health(monkeypatch)
        janitor = await _janitor_ids(product)
        states = await _matched_states(MemoryManifestService(product))
        pending = {slug for slug, state in states.items() if state == "pending"}
        dead = {slug for slug, state in states.items() if state == "dead_lettered"}
        assert pending == janitor
        assert len(dead) == int(health["dead_lettered_index_count"])
        assert set(states) == pending | dead

    async def test_ordinary_write_is_not_pending(
        self, session_factory, monkeypatch
    ) -> None:
        """What 23 474 was: rows written by the ordinary create path never
        enter the scan pipeline. The manifest holds no index state for them,
        so they are not an index backlog and are not counted as one."""
        svc = MemoryManifestService(session_factory)
        for n in range(3):
            await svc.record_create(
                "episodic", f"note-{n}.md", f"Note {n}", 1, "alice", tier="corpus"
            )
        health = await _health(monkeypatch)
        summary = await svc.index_status_summary(_admin(), ["note"])
        assert summary.pending == int(health["unindexed_count"]) == 0
        assert summary.matched == []

    @pytest.mark.parametrize(
        ("layer", "key"),
        [
            ("episodic", "note-0.md"),
            ("procedural", "note-0.md"),
            ("semantic", "decisions/note-0.md"),
        ],
    )
    async def test_manifest_makes_no_index_claim_about_an_ordinary_write(
        self, session_factory, monkeypatch, layer: str, key: str
    ) -> None:
        """Whether an ordinary note is in the vector store is UNKNOWN to the
        manifest: a semantic write is upserted before its row is written, an
        episodic or procedural write is not embedded until ``POST
        /memory/index``, and both leave the same row (no scan status, no index
        timestamp). So the model is told nothing about it: no ``corpus_status``
        is attached, which asserts neither "not yet indexed" nor "indexed"."""
        svc = MemoryManifestService(session_factory)
        await svc.record_create(layer, key, "Quarterly note", 1, "alice", tier="corpus")
        monkeypatch.setattr(memory_handlers, "get_memory_manifest_service", lambda: svc)
        status = await memory_handlers._maybe_corpus_status(_admin(), "quarterly note")
        assert status is None

    async def test_mock_service_mirrors_the_real_one(self, product) -> None:
        real = MemoryManifestService(product)
        mock = MockMemoryManifestService()
        for i, (scan, indexed, failed, deleted) in enumerate(STATES):
            await mock.record_create(
                "episodic", _key(i), None, 1, "alice", tier="corpus"
            )
            row = mock._rows[("episodic", _key(i))]
            row.update(
                scan_status=scan,
                indexed_at_ms=indexed,
                index_failed_at_ms=failed,
                deleted_at_ms=OLD_MS if deleted else None,
            )
        want = await real.index_status_summary(_admin(), [])
        got = await mock.index_status_summary(_admin(), [])
        assert (got.pending, got.dead_lettered) == (want.pending, want.dead_lettered)
        assert await _matched_states(mock) == await _matched_states(real)

    async def test_mock_seam_sets_and_resets_the_scan_status(self) -> None:
        """``set_index_state`` writes the row's whole index state: a keyword
        that is not passed is reset to ``None``, ``scan_status`` included, as
        the method already does for its other three columns."""
        mock = MockMemoryManifestService()
        await mock.record_create("episodic", "k.md", None, 1, "alice", tier="corpus")
        assert (await mock.index_status_summary(_admin(), [])).pending == 0
        mock.set_index_state("episodic", "k.md", scan_status="scanned_clean")
        assert (await mock.index_status_summary(_admin(), [])).pending == 1
        mock.set_index_state("episodic", "k.md")
        assert (await mock.index_status_summary(_admin(), [])).pending == 0


class TestPredicateHasOneHome:
    def test_sql_and_row_forms_agree_on_every_state(self) -> None:
        """The SQL clauses and the plain-row twin (used by the mock) are one
        rule: both forms select the same rows of the whole product."""
        import importlib

        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import Session

        from audittrace.db.models import Base

        shared = importlib.import_module(SHARED_MODULE)
        with contextlib.ExitStack() as stack:
            engine = create_engine("sqlite://")
            stack.callback(engine.dispose)
            Base.metadata.create_all(engine)
            session = stack.enter_context(Session(engine))
            for i, (scan, indexed, failed, deleted) in enumerate(STATES):
                session.add(
                    MemoryItem(
                        id=_slug(i), layer="episodic", key=_key(i), size_bytes=1,
                        created_at_ms=OLD_MS, modified_at_ms=OLD_MS,
                        created_by_user_id="a", modified_by_user_id="a",
                        deleted_at_ms=OLD_MS if deleted else None, tier="corpus",
                        scan_status=scan, indexed_at_ms=indexed,
                        index_failed_at_ms=failed,
                    )
                )  # fmt: skip
            session.commit()
            rows = session.execute(select(MemoryItem)).scalars().all()
            pending_sql = set(
                session.execute(
                    select(MemoryItem.id).where(*shared.pending_index_clauses())
                ).scalars()
            )
            pending_row = {
                r.id
                for r in rows
                if shared.is_pending_index(
                    scan_status=r.scan_status,
                    indexed_at_ms=r.indexed_at_ms,
                    index_failed_at_ms=r.index_failed_at_ms,
                    deleted_at_ms=r.deleted_at_ms,
                )
            }
            dead_sql = set(
                session.execute(
                    select(MemoryItem.id).where(*shared.dead_lettered_index_clauses())
                ).scalars()
            )
            dead_row = {
                r.id
                for r in rows
                if shared.is_dead_lettered_index(
                    index_failed_at_ms=r.index_failed_at_ms,
                    deleted_at_ms=r.deleted_at_ms,
                )
            }
        assert pending_sql == pending_row == WANT_PENDING and pending_sql
        assert dead_sql == dead_row == WANT_DEAD and dead_sql
        assert not pending_sql & dead_sql

    @pytest.mark.parametrize("site", SITES)
    def test_site_does_not_respell_the_predicate(self, site: str) -> None:
        """Each reader takes the predicate from the shared module: it tests
        neither index-outbox column of ``MemoryItem`` itself, under any name
        the module binds to that class (import alias, ``models.MemoryItem``,
        plain re-assignment). Not seen: ``getattr``, raw SQL text."""
        tree = ast.parse((ROOT / site).read_text())
        names = {"MemoryItem"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names |= {
                    a.asname or a.name for a in node.names if a.name == "MemoryItem"
                }
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and _is_model(node.value, names):
                names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        respelled = sorted(
            {
                (node.lineno, node.attr)
                for outer in ast.walk(tree)
                if isinstance(outer, ast.Compare)
                or (
                    isinstance(outer, ast.Call)
                    and isinstance(outer.func, ast.Attribute)
                    and outer.func.attr in {"is_", "is_not"}
                )
                for node in ast.walk(outer)
                if isinstance(node, ast.Attribute)
                and node.attr in OUTBOX_COLUMNS
                and _is_model(node.value, names)
            }
        )
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert respelled == []
        assert SHARED_MODULE in imports


def _is_model(node: ast.AST, names: set[str]) -> bool:
    return (isinstance(node, ast.Name) and node.id in names) or (
        isinstance(node, ast.Attribute) and node.attr == "MemoryItem"
    )


def test_fixture_covers_the_closed_vocabulary() -> None:
    assert len(STATES) == (len(_SCAN_STATUS_CODES) + 1) * 8
    assert (
        len(WANT_PENDING) == 1 and len(WANT_DEAD) == (len(_SCAN_STATUS_CODES) + 1) * 2
    )
    assert time.time() * 1000 > OLD_MS
