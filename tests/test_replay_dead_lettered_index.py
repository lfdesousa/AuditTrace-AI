"""Tests for scripts/replay_dead_lettered_index.py — SPEC #387 Phase 2a.

The reviewer's mandate is to prove each guard can FAIL, so this file proves,
positively, the falsifiable gates from the spec's Acceptance section:

1. ``TestApply`` — a dead-lettered row, replayed via ``--apply`` (both at the
   ``DeadLetterReplayer`` level and through the CLI's ``main()``), lands with
   ``index_failed_at_ms IS NULL``, ``index_attempts == 0``,
   ``index_failure_code IS NULL``.
2. ``TestDryRunNonMutating`` — the direct falsifiability proof named in the
   spec: running WITHOUT ``--apply`` (the CLI default) leaves the row's
   terminal columns byte-for-byte unchanged while still listing it in the
   printed output. Neutering ``plan()`` to reach the ``UPDATE`` path — e.g.
   deleting the ``dry_run`` branch in ``run()`` — makes this test RED.
3. ``TestScopeSafety`` — a non-dead-lettered row is never touched by any
   scope (including ``--all``); a soft-deleted dead-lettered row is never
   touched; ``--all`` without ``--apply`` mutates nothing at all.
4. ``TestCollectionScope`` — ``--collection`` only matches rows that route
   to that collection via the SAME ``collection_for_key``/``bare_key_from_uri``
   pair ``IndexJanitor`` uses, proven with one ``.pdf`` row (routes to
   ``ai_research_papers``) and one non-PDF row (routes to ``semantic``) in
   the same seed.
"""

from __future__ import annotations

import time

import pytest
import pytest_asyncio

from audittrace.db.postgres import InMemoryPostgresFactory
from audittrace.routes.memory_upload import manifest as manifest_mod
from scripts import replay_dead_lettered_index as cli
from scripts.replay_dead_lettered_index import (
    DeadLetterReplayer,
    ReplayScope,
    ScopeError,
)

# ``asyncio_mode = "auto"`` (pyproject.toml) already runs every ``async def
# test_*`` in the loop — no decorator needed; the plain ``def test_*`` CLI
# argparse tests below need no marker either.


async def _seed(
    factory,
    *,
    scan_id: str,
    key: str | None = None,
    index_attempts: int = 3,
    index_failed_at_ms: int | None = 1_700_000_000_000,
    index_failure_code: str | None = "max_attempts_exceeded",
    deleted: bool = False,
) -> None:
    """Insert a manifest row simulating a scan-pipeline promotion, with the
    dead-letter/soft-delete columns set to the caller's chosen state."""
    async with factory() as session:
        row = await manifest_mod.insert_pending_scan(
            session,
            scan_id=scan_id,
            user_id="alice",
            object_uri=f"s3://memory-shared/quarantine/alice/{scan_id}/x.pdf",
            object_sha256="0" * 64,
            size_bytes=1,
            title="x.pdf",
            trace_id="trace-1",
        )
        row.key = key or f"episodic/papers/{scan_id}/x.pdf"
        row.index_attempts = index_attempts
        row.index_failed_at_ms = index_failed_at_ms
        row.index_failure_code = index_failure_code
        if deleted:
            row.deleted_at_ms = int(time.time() * 1000)
        await session.commit()


@pytest_asyncio.fixture
async def factory() -> object:
    f = InMemoryPostgresFactory()
    await f.create_schema()
    return f.get_session_factory()


async def _row_state(factory, scan_id: str) -> tuple[int | None, int, str | None]:
    """Read back a row's three dead-letter columns by id (== scan_id here)."""
    from sqlalchemy import select

    from audittrace.db.models import MemoryItem

    async with factory() as session:
        row = (
            await session.execute(select(MemoryItem).where(MemoryItem.id == scan_id))
        ).scalar_one()
        return row.index_failed_at_ms, row.index_attempts, row.index_failure_code


# ── ReplayScope — fail-closed selector validation ──────────────────────────


class TestReplayScope:
    def test_key_only_is_valid(self):
        scope = ReplayScope(key="episodic/papers/x/y.pdf")
        assert scope.describe() == "--key episodic/papers/x/y.pdf"

    def test_collection_only_is_valid(self):
        scope = ReplayScope(collection="semantic")
        assert scope.describe() == "--collection semantic"

    def test_all_only_is_valid(self):
        scope = ReplayScope(all_=True)
        assert scope.describe() == "--all"

    def test_zero_selectors_raises(self):
        with pytest.raises(ScopeError):
            ReplayScope()

    def test_two_selectors_raises(self):
        with pytest.raises(ScopeError):
            ReplayScope(key="a", collection="semantic")

    def test_all_three_selectors_raises(self):
        with pytest.raises(ScopeError):
            ReplayScope(key="a", collection="semantic", all_=True)


# ── DeadLetterReplayer.apply — falsifiable acceptance guard #1 ────────────


class TestApply:
    async def test_apply_resets_terminal_columns_scoped_by_key(self, factory) -> None:
        await _seed(
            factory,
            scan_id="dead-1",
            index_attempts=4,
            index_failure_code="pdf_corrupted_xref",
        )

        replayer = DeadLetterReplayer(factory)
        result = await replayer.apply(ReplayScope(key="episodic/papers/dead-1/x.pdf"))

        assert result.dry_run is False
        assert result.matched_count == 1
        assert result.reset_count == 1
        failed_at, attempts, code = await _row_state(factory, "dead-1")
        assert failed_at is None
        assert attempts == 0
        assert code is None

    async def test_apply_via_cli_main_resets_row(
        self, monkeypatch, factory, capsys
    ) -> None:
        await _seed(
            factory, scan_id="dead-cli", index_failure_code="max_attempts_exceeded"
        )
        monkeypatch.setattr(cli, "_build_session_factory", lambda _settings: factory)

        rc = await cli._amain(["--key", "episodic/papers/dead-cli/x.pdf", "--apply"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "APPLY" in out
        assert "rows reset   : 1" in out
        failed_at, attempts, code = await _row_state(factory, "dead-cli")
        assert failed_at is None
        assert attempts == 0
        assert code is None

    async def test_apply_with_no_matches_resets_nothing(self, factory) -> None:
        replayer = DeadLetterReplayer(factory)
        result = await replayer.apply(ReplayScope(key="does/not/exist.pdf"))
        assert result.matched_count == 0
        assert result.reset_count == 0
        assert result.dry_run is False


# ── Dry-run non-mutating — the spec's named neuter proof ───────────────────


class TestDryRunNonMutating:
    async def test_plan_does_not_mutate(self, factory) -> None:
        await _seed(
            factory,
            scan_id="dead-dry",
            index_attempts=2,
            index_failure_code="pdf_corrupted_structure",
        )

        replayer = DeadLetterReplayer(factory)
        result = await replayer.plan(ReplayScope(key="episodic/papers/dead-dry/x.pdf"))

        assert result.dry_run is True
        assert result.reset_count == 0
        assert result.matched_count == 1
        # UNCHANGED — this is the RED-if-neutered assertion: if `plan()` (or
        # `run(..., apply=False)`) were wired to the `UPDATE` path, these
        # three would read (None, 0, None) instead.
        failed_at, attempts, code = await _row_state(factory, "dead-dry")
        assert failed_at == 1_700_000_000_000
        assert attempts == 2
        assert code == "pdf_corrupted_structure"

    async def test_cli_default_invocation_is_dry_run_and_lists_the_row(
        self, monkeypatch, factory, capsys
    ) -> None:
        await _seed(factory, scan_id="dead-cli-dry")
        monkeypatch.setattr(cli, "_build_session_factory", lambda _settings: factory)

        rc = await cli._amain(
            ["--key", "episodic/papers/dead-cli-dry/x.pdf"]
        )  # no --apply

        assert rc == 0
        out = capsys.readouterr().out
        assert "DRY-RUN" in out
        assert "episodic/papers/dead-cli-dry/x.pdf" in out
        assert "rows matched : 1" in out
        assert "rows reset   : 0" in out
        failed_at, attempts, code = await _row_state(factory, "dead-cli-dry")
        assert failed_at is not None
        assert attempts == 3
        assert code == "max_attempts_exceeded"

    async def test_all_without_apply_mutates_nothing(
        self, monkeypatch, factory, capsys
    ) -> None:
        await _seed(factory, scan_id="dead-all-1")
        await _seed(
            factory, scan_id="dead-all-2", key="episodic/papers/dead-all-2/y.pdf"
        )
        monkeypatch.setattr(cli, "_build_session_factory", lambda _settings: factory)

        rc = await cli._amain(["--all"])  # no --apply — fail-closed default

        assert rc == 0
        out = capsys.readouterr().out
        assert "rows matched : 2" in out
        assert "rows reset   : 0" in out
        for scan_id in ("dead-all-1", "dead-all-2"):
            failed_at, attempts, _code = await _row_state(factory, scan_id)
            assert failed_at is not None
            assert attempts == 3


# ── Scope safety ────────────────────────────────────────────────────────────


class TestScopeSafety:
    async def test_non_dead_lettered_row_never_touched_under_all_apply(
        self, factory
    ) -> None:
        await _seed(
            factory,
            scan_id="dead-x",
            index_attempts=5,
            index_failure_code="max_attempts_exceeded",
        )
        # A row mid-retry — attempts already incremented, but NOT yet
        # dead-lettered (index_failed_at_ms still NULL). Must never match.
        await _seed(
            factory,
            scan_id="pending-y",
            index_attempts=2,
            index_failed_at_ms=None,
            index_failure_code=None,
        )

        replayer = DeadLetterReplayer(factory)
        result = await replayer.apply(ReplayScope(all_=True))

        assert result.matched_count == 1
        assert result.reset_count == 1
        failed_at, attempts, code = await _row_state(factory, "dead-x")
        assert (failed_at, attempts, code) == (None, 0, None)
        # untouched — the pending row's own attempts counter survives intact
        pending_failed_at, pending_attempts, pending_code = await _row_state(
            factory, "pending-y"
        )
        assert pending_failed_at is None
        assert pending_attempts == 2
        assert pending_code is None

    async def test_soft_deleted_dead_lettered_row_never_touched(self, factory) -> None:
        await _seed(factory, scan_id="dead-deleted", deleted=True)

        replayer = DeadLetterReplayer(factory)
        result = await replayer.apply(ReplayScope(all_=True))

        assert result.matched_count == 0
        assert result.reset_count == 0
        failed_at, attempts, code = await _row_state(factory, "dead-deleted")
        assert failed_at is not None
        assert attempts == 3
        assert code == "max_attempts_exceeded"

    async def test_key_scope_never_touches_a_different_row(self, factory) -> None:
        await _seed(factory, scan_id="dead-a")
        await _seed(factory, scan_id="dead-b", key="episodic/papers/dead-b/z.pdf")

        replayer = DeadLetterReplayer(factory)
        result = await replayer.apply(ReplayScope(key="episodic/papers/dead-a/x.pdf"))

        assert result.matched_count == 1
        failed_at_a, attempts_a, _ = await _row_state(factory, "dead-a")
        assert (failed_at_a, attempts_a) == (None, 0)
        failed_at_b, attempts_b, code_b = await _row_state(factory, "dead-b")
        assert failed_at_b is not None
        assert attempts_b == 3
        assert code_b == "max_attempts_exceeded"


# ── --collection scope — same routing IndexJanitor uses ────────────────────


class TestCollectionScope:
    async def test_collection_scope_matches_only_routed_rows(self, factory) -> None:
        # PDF -> ai_research_papers (per collection_for_key); non-PDF -> semantic.
        await _seed(
            factory, scan_id="dead-pdf", key="episodic/papers/dead-pdf/report.pdf"
        )
        await _seed(factory, scan_id="dead-md", key="episodic/notes/dead-md/summary.md")

        replayer = DeadLetterReplayer(factory)
        result = await replayer.apply(ReplayScope(collection="ai_research_papers"))

        assert result.matched_count == 1
        assert result.matched[0].key == "episodic/papers/dead-pdf/report.pdf"
        failed_at_pdf, attempts_pdf, _ = await _row_state(factory, "dead-pdf")
        assert (failed_at_pdf, attempts_pdf) == (None, 0)
        # the .md row was never a candidate for the ai_research_papers scope
        failed_at_md, attempts_md, code_md = await _row_state(factory, "dead-md")
        assert failed_at_md is not None
        assert attempts_md == 3
        assert code_md == "max_attempts_exceeded"

    async def test_collection_scope_semantic_matches_non_pdf_rows(
        self, factory
    ) -> None:
        await _seed(
            factory, scan_id="dead-pdf2", key="episodic/papers/dead-pdf2/report.pdf"
        )
        await _seed(
            factory, scan_id="dead-md2", key="episodic/notes/dead-md2/summary.md"
        )

        replayer = DeadLetterReplayer(factory)
        result = await replayer.plan(ReplayScope(collection="semantic"))

        assert result.matched_count == 1
        assert result.matched[0].key == "episodic/notes/dead-md2/summary.md"

    async def test_unknown_collection_matches_nothing(self, factory) -> None:
        await _seed(factory, scan_id="dead-unknown")
        replayer = DeadLetterReplayer(factory)
        result = await replayer.plan(ReplayScope(collection="not-a-real-collection"))
        assert result.matched_count == 0


# ── CLI — parser, formatting, main() wiring ─────────────────────────────────


class TestParser:
    def test_requires_exactly_one_scope_flag(self):
        parser = cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_rejects_two_scope_flags(self):
        parser = cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--key", "a", "--collection", "b"])

    def test_key_scope_defaults_to_no_apply(self):
        parser = cli.build_parser()
        args = parser.parse_args(["--key", "episodic/papers/x/y.pdf"])
        assert args.key == "episodic/papers/x/y.pdf"
        assert args.collection is None
        assert args.all is False
        assert args.apply is False

    def test_all_with_apply(self):
        parser = cli.build_parser()
        args = parser.parse_args(["--all", "--apply"])
        assert args.all is True
        assert args.apply is True

    def test_scope_from_args_builds_replay_scope(self):
        parser = cli.build_parser()
        args = parser.parse_args(["--collection", "semantic"])
        scope = cli.scope_from_args(args)
        assert scope == ReplayScope(collection="semantic")


class TestPrintResult:
    def test_dry_run_empty_match(self, capsys):
        cli.print_result(
            cli.ReplayResult(matched=[], reset_count=0, dry_run=True),
            ReplayScope(all_=True),
        )
        out = capsys.readouterr().out
        assert "DRY-RUN" in out
        assert "no dead-lettered rows matched this scope." in out

    def test_apply_lists_rows_and_note(self, capsys):
        matched = [
            cli.MatchedRow(
                id="r1",
                key="episodic/papers/r1/x.pdf",
                index_attempts=5,
                index_failed_at_ms=1_700_000_000_000,
                index_failure_code="max_attempts_exceeded",
            )
        ]
        cli.print_result(
            cli.ReplayResult(matched=matched, reset_count=1, dry_run=False),
            ReplayScope(key="episodic/papers/r1/x.pdf"),
        )
        out = capsys.readouterr().out
        assert "APPLY" in out
        assert "episodic/papers/r1/x.pdf" in out
        assert "rows reset   : 1" in out
        assert "IndexJanitor will re-enqueue" in out


class TestAmainSettingsWiring:
    def test_no_database_url_aborts(self, monkeypatch, capsys):
        class _FakeSettings:
            database_url = None

        monkeypatch.setattr(cli, "get_settings", lambda: _FakeSettings())
        rc = cli.main(["--all"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "no database_url configured" in err

    def test_build_session_factory_returns_none_without_url(self):
        class _FakeSettings:
            database_url = None

        assert cli._build_session_factory(_FakeSettings()) is None

    def test_build_session_factory_builds_url_factory(self):
        class _FakeSettings:
            database_url = "sqlite+aiosqlite:///:memory:"

        session_factory = cli._build_session_factory(_FakeSettings())
        assert session_factory is not None
