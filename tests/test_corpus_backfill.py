"""Unit tests for the ADR-062 Phase B WU-B6 corpus backfill runner.

Every external effect is either the in-repo :class:`MockChromaDBFactory`
(no real ChromaDB server) or an injected/mocked ``embed_fn`` (no real nomic
server) — this file runs fully offline.

The reviewer's mandate is to prove each guard can FAIL, so this file proves,
positively, the three hard falsifiable gates from the build brief:

1. ``TestFalsifiableRecallGate`` — BEFORE migration a non-admin caller
   cannot recall a pre-Phase-B ("legacy"/operator-stamped) document except
   via the admin bypass; AFTER migration the SAME non-admin caller recalls
   it from the corpus tier, unfiltered, with no admin scope.
2. ``TestIdempotency`` — a second full run moves 0 documents, and a
   document already present in the corpus collection by id is never
   re-embedded/re-upserted (its content is provably untouched).
3. ``TestDryRun`` — ``--dry-run`` mutates neither ChromaDB collection and
   writes no report file, while still reporting accurate per-collection
   candidate counts.

``TestMigrate::test_migrate_forces_tier_from_authority_never_trusts_source_metadata``
is the direct falsifiability proof for
``LESSON-never-trust-caller-metadata-for-security-fields-20260804``: a
forged/bogus ``tier`` value sitting on the SOURCE (private-collection) row
must never survive into the corpus collection — only the runner's own
``"corpus"`` literal may land there.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from audittrace.db.factory import MockChromaDBFactory
from audittrace.identity import sentinel_user_context
from audittrace.services.semantic import ChromaSemanticService
from scripts.migrate import corpus_backfill
from scripts.migrate.corpus_backfill import (
    DEFAULT_CORPUS_COLLECTIONS,
    BackfillConfig,
    ChromaConnectError,
    CorpusBackfillRunner,
    _is_migration_candidate,
)

# ``asyncio_mode = "auto"`` (pyproject.toml) already runs every ``async def
# test_*`` in the loop — no ``pytestmark``/decorator needed, and adding one
# would warn on this file's plain ``def test_*`` CLI tests (see TestCLI).


def _stub_embed_fn():
    """Deterministic, offline, call-counting embed stub."""
    calls: list[str] = []

    async def _embed(text: str) -> list[float]:
        calls.append(text)
        return [0.1, 0.2, 0.3]

    _embed.calls = calls  # type: ignore[attr-defined]
    return _embed


async def _seeded_client() -> tuple[MockChromaDBFactory, object]:
    factory = MockChromaDBFactory()
    client = await factory.get_client()
    return factory, client


# ── pure predicate ───────────────────────────────────────────────────────────


class TestIsMigrationCandidate:
    def test_private_tier_is_never_a_candidate(self):
        assert _is_migration_candidate({"tier": "private", "user_id": "alice"}) is False

    def test_missing_tier_operator_stamped_is_a_candidate(self):
        assert _is_migration_candidate({"user_id": "operator-luis"}) is True

    def test_fully_untagged_legacy_row_is_a_candidate(self):
        assert _is_migration_candidate({}) is True

    def test_any_non_private_tier_value_is_a_candidate(self):
        # Defensive: a private-physical row should never carry tier="corpus"
        # under normal operation, but if one does (data anomaly / forged
        # value), it is still correctly treated as "belongs in corpus".
        assert _is_migration_candidate({"tier": "corpus"}) is True
        assert _is_migration_candidate({"tier": "bogus-value"}) is True


# ── M1 plan (read-only) ──────────────────────────────────────────────────────


class TestPlanPhase:
    async def test_plan_counts_candidates_and_already_corpus(self, tmp_path):
        _, client = await _seeded_client()
        private_col = await client.get_or_create_collection(name="decisions_v2")
        await private_col.add(
            ids=["mine", "operator-stamped", "already-corpus-dup"],
            documents=["mine text", "operator text", "dup text"],
            metadatas=[
                {"tier": "private", "user_id": "alice"},
                {"user_id": "operator-luis"},
                {},
            ],
        )
        corpus_col = await client.get_or_create_collection(name="decisions_corpus_v2")
        await corpus_col.add(
            ids=["already-corpus-dup"],
            documents=["already migrated"],
            metadatas=[{"tier": "corpus"}],
        )

        cfg = BackfillConfig(collections=("decisions",), out_dir=tmp_path)
        runner = CorpusBackfillRunner(cfg, client, _stub_embed_fn())
        await runner.phase_preflight()
        await runner.phase_plan()

        result = runner.results["decisions"]
        assert result.candidates == 2  # "mine" is private, excluded
        assert result.already_corpus == 1  # "already-corpus-dup"

    async def test_plan_is_read_only(self, tmp_path):
        _, client = await _seeded_client()
        private_col = await client.get_or_create_collection(name="skills_v2")
        await private_col.add(ids=["legacy1"], documents=["legacy"], metadatas=[{}])
        before_private = list(private_col.data)
        corpus_col = await client.get_or_create_collection(name="skills_corpus_v2")
        before_corpus = list(corpus_col.data)

        cfg = BackfillConfig(collections=("skills",), out_dir=tmp_path)
        runner = CorpusBackfillRunner(cfg, client, _stub_embed_fn())
        await runner.phase_preflight()
        await runner.phase_plan()

        assert private_col.data == before_private
        assert corpus_col.data == before_corpus


# ── dry-run ───────────────────────────────────────────────────────────────────


class TestDryRun:
    async def test_dry_run_mutates_nothing_and_writes_no_file(self, tmp_path):
        _, client = await _seeded_client()
        private_col = await client.get_or_create_collection(name="decisions_v2")
        await private_col.add(
            ids=["legacy1", "legacy2"],
            documents=["one", "two"],
            metadatas=[{}, {"user_id": "operator"}],
        )
        before_private = list(private_col.data)
        corpus_col = await client.get_or_create_collection(name="decisions_corpus_v2")
        before_corpus = list(corpus_col.data)

        out_dir = tmp_path / "runs"
        cfg = BackfillConfig(collections=("decisions",), dry_run=True, out_dir=out_dir)
        embed_fn = _stub_embed_fn()
        runner = CorpusBackfillRunner(cfg, client, embed_fn)
        report = await runner.run()

        assert report["aborted"] is False
        assert report["dry_run"] is True
        assert report["collections"]["decisions"]["candidates"] == 2
        phase_status = {p["name"]: p["status"] for p in report["phases"]}
        assert phase_status["M0-preflight"] == "ok"
        assert phase_status["M1-plan"] == "ok"
        assert phase_status["M2-migrate"] == "planned"
        assert phase_status["M3-report"] == "planned"

        # No ChromaDB mutation.
        assert private_col.data == before_private
        assert corpus_col.data == before_corpus
        # No embed call — dry-run never reaches the migrate phase.
        assert embed_fn.calls == []
        # No filesystem mutation either.
        assert not out_dir.exists()


# ── M2 migrate (real mutation) ───────────────────────────────────────────────


class TestMigrate:
    async def test_migrate_moves_legacy_and_operator_stamped_rows_only(self, tmp_path):
        _, client = await _seeded_client()
        private_col = await client.get_or_create_collection(name="decisions_v2")
        await private_col.add(
            ids=["mine", "operator-stamped", "untagged-legacy"],
            documents=[
                "mine: genuine post-Phase-B private write",
                "operator: pre-Phase-B bulk-indexed content",
                "untagged: pre-Phase-2 legacy row",
            ],
            metadatas=[
                {"tier": "private", "user_id": "alice"},
                {"user_id": "operator-luis", "source": "bulk-index"},
                {},
            ],
        )

        cfg = BackfillConfig(collections=("decisions",), out_dir=tmp_path)
        runner = CorpusBackfillRunner(cfg, client, _stub_embed_fn())
        report = await runner.run()

        assert report["aborted"] is False
        result = report["collections"]["decisions"]
        assert result["candidates"] == 2
        assert result["migrated"] == 2
        assert result["moved"] == 2
        assert result["already_corpus"] == 0
        assert result["errors"] == []

        # Private collection now holds ONLY the genuine private write.
        private_ids = {row["id"] for row in private_col.data}
        assert private_ids == {"mine"}

        # Corpus collection now holds the other two, tagged tier=corpus.
        corpus_col = await client.get_or_create_collection(name="decisions_corpus_v2")
        corpus_by_id = {row["id"]: row for row in corpus_col.data}
        assert set(corpus_by_id) == {"operator-stamped", "untagged-legacy"}
        for row in corpus_by_id.values():
            assert row["metadata"]["tier"] == "corpus"

        # Report + human-summary artefacts were written.
        json_files = list(tmp_path.glob("corpus-backfill-*.json"))
        txt_files = list(tmp_path.glob("corpus-backfill-*.txt"))
        assert len(json_files) == 1
        assert len(txt_files) == 1
        on_disk = json.loads(json_files[0].read_text())
        assert on_disk["collections"]["decisions"]["migrated"] == 2

    async def test_migrate_preserves_other_metadata_fields(self, tmp_path):
        _, client = await _seeded_client()
        private_col = await client.get_or_create_collection(name="skills_v2")
        await private_col.add(
            ids=["skill1"],
            documents=["skill content"],
            metadatas=[{"user_id": "operator", "source": "SKILL-python.md"}],
        )
        cfg = BackfillConfig(collections=("skills",), out_dir=tmp_path)
        runner = CorpusBackfillRunner(cfg, client, _stub_embed_fn())
        await runner.run()

        corpus_col = await client.get_or_create_collection(name="skills_corpus_v2")
        row = corpus_col.data[0]
        assert row["metadata"]["source"] == "SKILL-python.md"
        assert row["metadata"]["user_id"] == "operator"
        assert row["metadata"]["tier"] == "corpus"

    async def test_migrate_paginates_across_multiple_pages(self, tmp_path):
        """Forces ``_enumerate_candidates`` past a single page (page_size=1)
        so the "fetch the next page" branch is exercised, not just the
        "collection fits in one page" happy path."""
        _, client = await _seeded_client()
        private_col = await client.get_or_create_collection(name="decisions_v2")
        await private_col.add(
            ids=["legacy1", "legacy2", "legacy3"],
            documents=["one", "two", "three"],
            metadatas=[{}, {}, {}],
        )
        cfg = BackfillConfig(collections=("decisions",), out_dir=tmp_path, page_size=1)
        report = await CorpusBackfillRunner(cfg, client, _stub_embed_fn()).run()
        result = report["collections"]["decisions"]
        assert result["candidates"] == 3
        assert result["migrated"] == 3
        assert result["moved"] == 3

    async def test_migrate_isolates_per_id_failures(self, tmp_path):
        """One candidate's embed call failing must not abort the rest of
        the run, and must leave THAT id in place (not deleted) rather than
        losing it — a partial-failure recovery matches the M2 docstring's
        "isolate per-id, keep migrating the rest" contract."""
        _, client = await _seeded_client()
        private_col = await client.get_or_create_collection(name="decisions_v2")
        await private_col.add(
            ids=["ok", "boom"],
            documents=["fine content", "content that blows up embedding"],
            metadatas=[{}, {}],
        )

        async def _flaky_embed(text: str) -> list[float]:
            if "blows up" in text:
                raise RuntimeError("nomic unreachable")
            return [0.1, 0.2, 0.3]

        cfg = BackfillConfig(collections=("decisions",), out_dir=tmp_path)
        report = await CorpusBackfillRunner(cfg, client, _flaky_embed).run()

        result = report["collections"]["decisions"]
        assert result["migrated"] == 1
        assert result["moved"] == 1
        assert len(result["errors"]) == 1
        assert "boom" in result["errors"][0]

        # The failed id was never deleted from private — it stays a
        # candidate for the next run rather than being silently lost.
        assert any(r["id"] == "boom" for r in private_col.data)
        corpus_col = await client.get_or_create_collection(name="decisions_corpus_v2")
        assert not any(r["id"] == "boom" for r in corpus_col.data)
        assert any(r["id"] == "ok" for r in corpus_col.data)

    async def test_migrate_forces_tier_from_authority_never_trusts_source_metadata(
        self, tmp_path
    ):
        """LESSON-never-trust-caller-metadata-for-security-fields-20260804:
        a forged/bogus ``tier`` value already sitting on the source
        (private-collection) row must NEVER survive into the corpus
        collection verbatim — only the runner's own ``"corpus"`` literal
        may land there. If the implementation regressed to
        ``meta.setdefault("tier", "corpus")`` (honouring a pre-existing key
        instead of assigning it), this assertion goes RED because the
        forged value would ride straight through."""
        _, client = await _seeded_client()
        private_col = await client.get_or_create_collection(name="decisions_v2")
        await private_col.add(
            ids=["forged"],
            documents=["a row trying to smuggle a tier value"],
            metadatas=[{"tier": "corpus-but-actually-forged", "user_id": "attacker"}],
        )
        cfg = BackfillConfig(collections=("decisions",), out_dir=tmp_path)
        runner = CorpusBackfillRunner(cfg, client, _stub_embed_fn())
        await runner.run()

        corpus_col = await client.get_or_create_collection(name="decisions_corpus_v2")
        row = corpus_col.data[0]
        assert row["metadata"]["tier"] == "corpus"


# ── idempotency ──────────────────────────────────────────────────────────────


class TestIdempotency:
    async def test_second_run_moves_zero(self, tmp_path):
        _, client = await _seeded_client()
        private_col = await client.get_or_create_collection(name="decisions_v2")
        await private_col.add(
            ids=["legacy1", "legacy2"],
            documents=["one", "two"],
            metadatas=[{}, {"user_id": "operator"}],
        )

        cfg = BackfillConfig(collections=("decisions",), out_dir=tmp_path)
        first = await CorpusBackfillRunner(cfg, client, _stub_embed_fn()).run()
        assert first["collections"]["decisions"]["moved"] == 2

        second = await CorpusBackfillRunner(cfg, client, _stub_embed_fn()).run()
        result = second["collections"]["decisions"]
        assert result["candidates"] == 0
        assert result["migrated"] == 0
        assert result["moved"] == 0

    async def test_already_in_corpus_is_never_reembedded(self, tmp_path):
        _, client = await _seeded_client()
        corpus_col = await client.get_or_create_collection(name="decisions_corpus_v2")
        await corpus_col.add(
            ids=["x"],
            documents=["original corpus content"],
            metadatas=[{"tier": "corpus"}],
        )
        private_col = await client.get_or_create_collection(name="decisions_v2")
        await private_col.add(
            ids=["x"],
            documents=["stale private-collection duplicate"],
            metadatas=[{}],
        )

        embed_fn = _stub_embed_fn()
        cfg = BackfillConfig(collections=("decisions",), out_dir=tmp_path)
        report = await CorpusBackfillRunner(cfg, client, embed_fn).run()

        result = report["collections"]["decisions"]
        assert result["already_corpus"] == 1
        assert result["migrated"] == 0
        assert result["moved"] == 1
        # Never re-embedded — the whole point of the idempotency guarantee.
        assert embed_fn.calls == []
        # The corpus copy was NOT clobbered by the stale private duplicate.
        row = next(r for r in corpus_col.data if r["id"] == "x")
        assert row["document"] == "original corpus content"
        # The stale private duplicate was still removed (completes the move).
        assert not any(r["id"] == "x" for r in private_col.data)


# ── M0 preflight abort ───────────────────────────────────────────────────────


class TestPreflightAbort:
    async def test_unreachable_chromadb_aborts_before_any_mutation(self, tmp_path):
        class _BoomClient:
            async def get_or_create_collection(self, name, **kw):
                raise ConnectionError("no healthy upstream")

        cfg = BackfillConfig(collections=("decisions",), out_dir=tmp_path)
        runner = CorpusBackfillRunner(cfg, _BoomClient(), _stub_embed_fn())

        with pytest.raises(ChromaConnectError):
            await runner.phase_preflight()

        report = await CorpusBackfillRunner(cfg, _BoomClient(), _stub_embed_fn()).run()
        assert report["aborted"] is True
        assert report["collections"] == {}
        phase_status = {p["name"]: p["status"] for p in report["phases"]}
        assert phase_status["M0-preflight"] == "aborted"
        assert "M1-plan" not in phase_status


# ── the falsifiable end-to-end recall gate ──────────────────────────────────


@pytest.fixture(autouse=True)
def _mock_semantic_service_nomic(monkeypatch):
    """Offline stub for ChromaSemanticService's OWN embed call (used by
    ``search()`` in the falsifiable-gate test below) — separate from the
    migration runner's injected ``embed_fn``."""
    monkeypatch.setattr(
        "audittrace.services.semantic.embed_via_nomic",
        AsyncMock(side_effect=lambda texts, **_: [[0.1, 0.2, 0.3] for _ in texts]),
    )


class TestFalsifiableRecallGate:
    """The build brief's central falsifiable proof (spec §4 WU-B6 /
    §6 DoD item 2): BEFORE the backfill a non-admin caller cannot recall a
    pre-Phase-B document except via the admin bypass; AFTER the backfill the
    SAME non-admin caller recalls it from the corpus tier, unfiltered, no
    admin scope required."""

    async def test_before_admin_only_after_non_admin_recalls_corpus(self, tmp_path):
        _, client = await _seeded_client()
        private_col = await client.get_or_create_collection(name="decisions_v2")
        await private_col.add(
            ids=["adr-062"],
            documents=["ADR-062 five-layer memory model corpustoken content"],
            metadatas=[{"user_id": "operator-luis"}],  # pre-Phase-B, no tier
        )

        service = ChromaSemanticService(
            client=client, default_collections=["decisions"]
        )
        admin = sentinel_user_context()
        non_admin = replace(admin, user_id="alice", is_admin=False, scopes=())

        # BEFORE: non-admin sees nothing; admin (bypass) sees it.
        before_non_admin = await service.search(non_admin, "corpustoken", k=10)
        assert not any("corpustoken" in d.page_content for d in before_non_admin)
        before_admin = await service.search(admin, "corpustoken", k=10)
        assert any("corpustoken" in d.page_content for d in before_admin)

        # Run the backfill.
        cfg = BackfillConfig(collections=("decisions",), out_dir=tmp_path)
        report = await CorpusBackfillRunner(cfg, client, _stub_embed_fn()).run()
        assert report["collections"]["decisions"]["migrated"] == 1

        # AFTER: the SAME non-admin recalls it — via corpus, unfiltered.
        after_non_admin = await service.search(non_admin, "corpustoken", k=10)
        matches = [d for d in after_non_admin if "corpustoken" in d.page_content]
        assert len(matches) == 1
        assert matches[0].metadata["tier"] == "corpus"


# ── CLI ───────────────────────────────────────────────────────────────────────


class TestCLI:
    def test_build_parser_defaults(self):
        parser = corpus_backfill.build_parser()
        args = parser.parse_args([])
        assert tuple(args.collections) == DEFAULT_CORPUS_COLLECTIONS
        assert args.dry_run is False

    def test_config_from_args(self, tmp_path):
        parser = corpus_backfill.build_parser()
        args = parser.parse_args(
            [
                "--collections",
                "decisions",
                "skills",
                "--dry-run",
                "--out-dir",
                str(tmp_path),
                "--page-size",
                "50",
            ]
        )
        cfg = corpus_backfill.config_from_args(args)
        assert cfg.collections == ("decisions", "skills")
        assert cfg.dry_run is True
        assert cfg.out_dir == tmp_path
        assert cfg.page_size == 50

    def test_print_plan_prints_phases(self, capsys):
        report = {
            "dry_run": True,
            "collections": {
                "decisions": {
                    "candidates": 2,
                    "already_corpus": 0,
                    "migrated": 0,
                    "moved": 0,
                    "errors": [],
                }
            },
            "phases": [
                {"name": "M0-preflight", "status": "ok", "detail": "2 reachable"},
                {
                    "name": "M1-plan",
                    "status": "ok",
                    "detail": "decisions: 2 candidate(s)",
                },
            ],
        }
        corpus_backfill.print_plan(report)
        out = capsys.readouterr().out
        assert "corpus backfill plan (dry-run=True)" in out
        assert "decisions: 2 candidate(s)" in out
        assert "M1-plan" in out

    def test_main_dry_run_happy_path(self, monkeypatch, capsys):
        async def _seed():
            factory = MockChromaDBFactory()
            client = await factory.get_client()
            col = await client.get_or_create_collection(name="decisions_v2")
            await col.add(
                ids=["d1"], documents=["legacy"], metadatas=[{"user_id": "operator"}]
            )
            return client

        client = asyncio.run(_seed())

        class _FakeHTTPFactory:
            def __init__(self, *a, **kw):
                pass

            async def get_client(self):
                return client

        monkeypatch.setattr(corpus_backfill, "HTTPChromaDBFactory", _FakeHTTPFactory)
        monkeypatch.setattr(
            corpus_backfill,
            "embed_via_nomic",
            AsyncMock(side_effect=lambda texts, **_: [[0.1, 0.2, 0.3] for _ in texts]),
        )

        rc = corpus_backfill.main(["--collections", "decisions", "--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "corpus backfill plan (dry-run=True)" in out
        assert "decisions: 1 candidate(s)" in out

    def test_main_real_run_writes_report(self, monkeypatch, tmp_path, capsys):
        async def _seed():
            factory = MockChromaDBFactory()
            client = await factory.get_client()
            col = await client.get_or_create_collection(name="skills_v2")
            await col.add(ids=["s1"], documents=["legacy skill"], metadatas=[{}])
            return client

        client = asyncio.run(_seed())

        class _FakeHTTPFactory:
            def __init__(self, *a, **kw):
                pass

            async def get_client(self):
                return client

        monkeypatch.setattr(corpus_backfill, "HTTPChromaDBFactory", _FakeHTTPFactory)
        monkeypatch.setattr(
            corpus_backfill,
            "embed_via_nomic",
            AsyncMock(side_effect=lambda texts, **_: [[0.1, 0.2, 0.3] for _ in texts]),
        )

        rc = corpus_backfill.main(
            ["--collections", "skills", "--out-dir", str(tmp_path)]
        )
        assert rc == 0
        assert len(list(tmp_path.glob("corpus-backfill-*.json"))) == 1

    def test_main_aborted_returns_2(self, monkeypatch, capsys):
        class _BoomClient:
            async def get_or_create_collection(self, name, **kw):
                raise ConnectionError("no healthy upstream")

        class _FakeHTTPFactory:
            def __init__(self, *a, **kw):
                pass

            async def get_client(self):
                return _BoomClient()

        monkeypatch.setattr(corpus_backfill, "HTTPChromaDBFactory", _FakeHTTPFactory)

        rc = corpus_backfill.main(["--collections", "decisions"])
        assert rc == 2
        out = capsys.readouterr().out
        assert "corpus backfill plan (dry-run=False)" in out
