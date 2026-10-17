"""One-time corpus backfill migration — ADR-062 Phase B, WU-B6 (D2).

**The problem this closes (spec §0).** WU-B3/B4/B5 (already shipped) gave the
system a real per-user PRIVATE tier and a real, separately-recalled CORPUS
tier (``ChromaSemanticService._physical`` / ``._physical_corpus`` — the
single choke-point this module reuses rather than re-deriving the
``_corpus_v2`` suffix). But every vector indexed BEFORE that work landed
still physically sits in the PRIVATE ChromaDB collection: the bulk indexer
stamped the *operator's own* ``user_id`` on every corpus chunk, and some rows
predate even that (no ``user_id`` at all — the "untagged legacy row"
fixture in ``tests/test_semantic_service.py``). Until those rows are moved,
a non-admin caller's ``recall_*``/``search`` — which applies the #383
``where={"user_id": caller}`` filter against the PRIVATE physical collection
— never sees them; the shared knowledge base is visible ONLY through the
admin bypass (``identity.py``). D2 (ratified 2026-08-04): "all existing
memory-shared content = CORPUS; new writes = PRIVATE-default." This module
is the one-time backfill that makes that true for ChromaDB. The S3 side
needs NO migration — ``EpisodicService``/``ProceduralService`` (WU-B1/B2)
already tag every row read from the shared bucket ``tier="corpus"``
unconditionally, by construction, regardless of when it was written.

**The move, precisely.** For each ratified D1 corpus-scoped logical
collection (``decisions``/``skills``/``semantic`` — the same frozenset
``routes/memory.py::_DEFAULT_COLLECTIONS`` declares a
``memory:corpus:<collection>:{read,write}`` scope for; see
``tests/test_chart_drift_guards.py``), this runner enumerates the PRIVATE
physical collection (``<name>_v2``) and, for every document whose metadata
was never stamped ``tier="private"`` by the WU-B5-hardened
``ChromaSemanticService.upsert()`` write path — i.e. every pre-Phase-B row,
operator-stamped OR fully untagged — moves it into the CORPUS physical
collection (``<name>_corpus_v2``), stamping ``tier="corpus"`` **from this
runner's own authority, never from the source document's metadata**
(LESSON-never-trust-caller-metadata-for-security-fields-20260804: ``tier``
is a security-relevant field exactly like the WU-B5 promote-gate forgery
this lesson names — the migration must not trust a possibly-forged or
merely-absent ``tier`` key on the row it is reading, it must ASSIGN the
value). A genuine post-Phase-B private write (``tier="private"``,
unconditionally stamped by ``upsert()``) is never a candidate and is never
touched.

**Determinism / idempotency / dry-run — mirrors** :mod:`scripts.release.runner`
**and** :mod:`scripts.deploy.runner`: **the same ordered phases run every
time, dry-run's read-only phases execute for real, its mutating phases are
recorded as "planned" and NOTHING is written to ChromaDB or disk**:

* **M0 Preflight** — resolve the private + corpus physical collection for
  every requested logical collection (read-only: ``get_or_create_collection``
  auto-vivifying an EMPTY collection is the same no-op-if-empty side effect
  every production read already performs via ``ChromaSemanticService.search``
  — it never touches vector data). ABORTS the run before M1 on any
  connectivity failure.
* **M1 Plan** — enumerate every PRIVATE-physical candidate per collection and
  check, by id, whether it is already present in the CORPUS-physical
  collection. Always runs for real (never mutates: ``.get()`` calls only) —
  this IS the dry-run's plan.
* **M2 Migrate** — SKIPPED (recorded ``"planned"``) under ``--dry-run``.
  Otherwise: for each M1 candidate not already present in the corpus
  collection, embed it (fresh — this is a low-volume one-time script, not a
  hot path, so re-embedding is the simplest way to get a real, current
  vector without adding embeddings-passthrough plumbing to the mock) and
  upsert into the corpus collection with ``tier="corpus"`` forced; a
  candidate ALREADY present in the corpus collection by id is never
  re-embedded/re-upserted (the idempotency guarantee — proven by a test that
  seeds a stale private-tier duplicate of an already-migrated id and asserts
  the corpus copy is untouched). Either way, the id is then removed from the
  private collection — completing the move. A second full run therefore
  finds ZERO remaining candidates (every DoD-required "second run moves 0"
  proof follows from this, not from a separate cache).
* **M3 Report** — SKIPPED (recorded ``"planned"``) under ``--dry-run`` (no
  file is ever written for a plan that was never executed — mirrors the
  release runner's "stops without mutating" posture exactly). Otherwise
  emits ``scripts/migrate/runs/corpus-backfill-<stamp>.{json,txt}``.

There is NO wall-clock branching in the control flow — timestamps are
stamped into evidence only (``_now_iso``), never read back to decide
anything.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from audittrace.config import get_settings
from audittrace.db.factory import ChromaDBClient, ChromaDBFactory, HTTPChromaDBFactory
from audittrace.services.embedder import embed_via_nomic
from audittrace.services.semantic import ChromaSemanticService

logger = logging.getLogger("audittrace.migrate.corpus_backfill")

# Ordered phase identifiers — the determinism contract fixes this sequence.
PHASES = (
    "M0-preflight",
    "M1-plan",
    "M2-migrate",
    "M3-report",
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# D1 ratified corpus-scoped collections (ADR-062 §4's frozenset;
# ``routes/memory.py::_DEFAULT_COLLECTIONS`` and the ``memory:corpus:*``
# scope catalogue in ``auth.py`` / ``charts/audittrace/files/realm-audittrace.json``
# both declare exactly this set). Duplicated here as a plain tuple — NOT
# imported from ``audittrace.routes.memory`` — because that module is the
# HTTP route layer (FastAPI router construction, auth dependencies) and this
# is a standalone offline script; importing it would pull in the whole route
# graph for three strings. If the ratified set ever changes, both places
# must move together (``tests/test_chart_drift_guards.py`` catches drift on
# the scope side; ``tests/test_corpus_backfill.py`` pins this tuple).
DEFAULT_CORPUS_COLLECTIONS: tuple[str, ...] = ("decisions", "skills", "semantic")

# Pagination window for enumerating a private physical collection via
# ChromaDB's non-ranked ``.get(limit=..., offset=...)`` — bounded so a
# collection with tens of thousands of legacy rows doesn't require one
# unbounded fetch (mirrors the ``MAX_RECALL_WINDOW`` posture in
# ``services/semantic.py``, just paged rather than capped).
DEFAULT_PAGE_SIZE = 200


class ChromaConnectError(RuntimeError):
    """M0 preflight failure — raised before any M1/M2 phase runs."""


@dataclass(frozen=True)
class BackfillConfig:
    collections: tuple[str, ...] = DEFAULT_CORPUS_COLLECTIONS
    dry_run: bool = False
    page_size: int = DEFAULT_PAGE_SIZE
    out_dir: Path = REPO_ROOT / "scripts" / "migrate" / "runs"


@dataclass
class CollectionResult:
    """Per-logical-collection outcome, both for the M1 plan and the M2
    execution (the same object is populated by M1 first, then M2 adds to
    it — never recomputed, so the two phases can never disagree)."""

    collection: str
    candidates: int = 0
    """Private-physical rows whose metadata was never stamped
    ``tier="private"`` — the M1 plan count, unaffected by dry-run."""
    already_corpus: int = 0
    """Of ``candidates``, how many already exist in the corpus collection
    by id (computed in M1; each is skip-upserted, not re-embedded, in M2)."""
    migrated: int = 0
    """Candidates newly embedded + upserted into the corpus collection this
    run (0 for every id that was ``already_corpus``)."""
    moved: int = 0
    """Total ids removed from the private collection this run — always
    ``<= candidates``; equals ``candidates`` on a clean, error-free run.
    The DoD idempotency metric: a second run's ``moved`` is 0 for every
    collection once a first run completed cleanly."""
    errors: list[str] = field(default_factory=list)


@dataclass
class PhaseRecord:
    name: str
    status: str  # ok | planned | aborted
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    ended_at: str = ""


def _now_iso() -> str:
    """UTC ISO-8601 timestamp — EVIDENCE ONLY, never read back into logic."""
    return datetime.now(UTC).isoformat()


def _is_migration_candidate(meta: dict[str, Any]) -> bool:
    """``True`` iff a row sitting in the PRIVATE physical collection is
    actually pre-Phase-B shared content that belongs in the CORPUS
    collection (D2).

    Covers BOTH failure modes named in spec §0: the bulk indexer's
    operator-``user_id``-stamped rows (``user_id`` present, ``tier``
    absent) AND the genuinely untagged legacy rows (neither key present —
    ``tests/test_semantic_service.py``'s ``"untagged legacy row"``
    fixture). The single safe discriminator is the ABSENCE of an explicit
    ``tier="private"`` stamp: every real WU-B5+ private write sets that
    key unconditionally (``ChromaSemanticService.upsert()``), so this
    predicate can never misclassify a genuine private write as corpus,
    and never needs to special-case ``user_id`` at all.
    """
    return meta.get("tier") != "private"


class CorpusBackfillRunner:
    """Executes the ordered M0-M3 phases described in the module docstring.

    ``embed_fn`` is injected (never imported+called directly) so unit tests
    run fully offline against :class:`~audittrace.db.factory.MockChromaDBFactory`
    — mirrors the ``_mock_nomic_embed`` fixture pattern in
    ``tests/test_semantic_service.py``, just wired at the constructor
    boundary instead of a module-level monkeypatch (this class has no
    module-level nomic import to patch).
    """

    def __init__(
        self,
        cfg: BackfillConfig,
        client: ChromaDBClient,
        embed_fn: Callable[[str], Awaitable[list[float]]],
    ) -> None:
        self.cfg = cfg
        self._client = client
        self._embed = embed_fn
        self.records: list[PhaseRecord] = []
        self.results: dict[str, CollectionResult] = {}
        self._planned_candidates: dict[str, list[dict[str, Any]]] = {}

    # -- phase plumbing --

    def _record(self, name: str, status: str, **kw: Any) -> PhaseRecord:
        rec = PhaseRecord(name=name, status=status, **kw)
        self.records.append(rec)
        level = logging.ERROR if status == "aborted" else logging.INFO
        logger.log(level, "[%s] %s %s", name, status, rec.detail)
        return rec

    async def _collection(self, physical_name: str) -> Any:
        return await self._client.get_or_create_collection(
            name=physical_name, embedding_function=None
        )

    # -- M0 --

    async def phase_preflight(self) -> None:
        """Resolve every requested collection's private + corpus physical
        name. Read-only w.r.t. vector DATA (see module docstring on why
        collection auto-vivification is not treated as a data mutation).
        ABORTS before M1 on any connectivity failure."""
        started = _now_iso()
        try:
            for name in self.cfg.collections:
                await self._collection(ChromaSemanticService._physical(name))
                await self._collection(ChromaSemanticService._physical_corpus(name))
        except Exception as exc:
            self._record(
                PHASES[0],
                "aborted",
                detail=f"ChromaDB unreachable: {exc}",
                started_at=started,
                ended_at=_now_iso(),
            )
            raise ChromaConnectError(str(exc)) from exc
        self._record(
            PHASES[0],
            "ok",
            detail=f"{len(self.cfg.collections)} collection(s) reachable: "
            f"{', '.join(self.cfg.collections)}",
            started_at=started,
            ended_at=_now_iso(),
        )

    # -- M1 --

    async def _enumerate_candidates(self, private_col: Any) -> list[dict[str, Any]]:
        """Page through ``private_col`` and return every row that fails
        :func:`_is_migration_candidate`. READ-ONLY — only ``.get()`` calls."""
        candidates: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await private_col.get(
                limit=self.cfg.page_size,
                offset=offset,
                include=["documents", "metadatas"],
            )
            ids = page.get("ids") or []
            if not ids:
                break
            documents = page.get("documents") or []
            metadatas = page.get("metadatas") or []
            for i, doc_id in enumerate(ids):
                meta = metadatas[i] if i < len(metadatas) else {}
                if _is_migration_candidate(meta):
                    candidates.append(
                        {
                            "id": doc_id,
                            "document": documents[i] if i < len(documents) else "",
                            "metadata": dict(meta),
                        }
                    )
            if len(ids) < self.cfg.page_size:
                break
            offset += self.cfg.page_size
        return candidates

    async def phase_plan(self) -> None:
        """Always runs for real (dry-run and not) — this IS the dry-run
        plan. Mutates nothing: every call below is a ``.get()``."""
        started = _now_iso()
        for name in self.cfg.collections:
            private_col = await self._collection(ChromaSemanticService._physical(name))
            corpus_col = await self._collection(
                ChromaSemanticService._physical_corpus(name)
            )
            candidates = await self._enumerate_candidates(private_col)
            already = 0
            for candidate in candidates:
                existing = await corpus_col.get(ids=[candidate["id"]])
                if existing.get("ids"):
                    already += 1
            self.results[name] = CollectionResult(
                collection=name, candidates=len(candidates), already_corpus=already
            )
            self._planned_candidates[name] = candidates
        detail = (
            "; ".join(
                f"{name}: {r.candidates} candidate(s) "
                f"({r.already_corpus} already in corpus)"
                for name, r in self.results.items()
            )
            or "no candidates"
        )
        self._record(
            PHASES[1],
            "ok",
            detail=detail,
            evidence={name: asdict(r) for name, r in self.results.items()},
            started_at=started,
            ended_at=_now_iso(),
        )

    # -- M2 --

    async def phase_migrate(self) -> None:
        """Executes the M1 plan. Never re-derives the candidate list — uses
        the SAME rows M1 already found, so the plan and the execution can
        never disagree."""
        started = _now_iso()
        for name in self.cfg.collections:
            private_col = await self._collection(ChromaSemanticService._physical(name))
            corpus_col = await self._collection(
                ChromaSemanticService._physical_corpus(name)
            )
            result = self.results.setdefault(name, CollectionResult(collection=name))
            for candidate in self._planned_candidates.get(name, []):
                doc_id = candidate["id"]
                try:
                    existing = await corpus_col.get(ids=[doc_id])
                    if not existing.get("ids"):
                        # Not already migrated — the idempotency-critical
                        # branch: only reachable for a genuinely new
                        # candidate, never re-executed for one already
                        # present in the corpus collection (proven by
                        # ``TestIdempotency::test_already_in_corpus_is_never_reembedded``).
                        vector = await self._embed(candidate["document"])
                        meta = dict(candidate["metadata"])
                        # ``tier`` is ALWAYS assigned by this runner, never
                        # read from the candidate's own (possibly absent or
                        # forged) metadata — LESSON-never-trust-caller-
                        # metadata-for-security-fields-20260804.
                        meta["tier"] = "corpus"
                        await corpus_col.upsert(
                            ids=[doc_id],
                            documents=[candidate["document"]],
                            embeddings=[vector],
                            metadatas=[meta],
                        )
                        result.migrated += 1
                    await private_col.delete(ids=[doc_id])
                    result.moved += 1
                except Exception as exc:  # isolate per-id, keep migrating the rest
                    logger.warning(
                        "corpus backfill: %s/%s failed: %s", name, doc_id, exc
                    )
                    result.errors.append(f"{doc_id}: {exc}")
        detail = "; ".join(
            f"{name}: migrated={r.migrated} moved={r.moved} errors={len(r.errors)}"
            for name, r in self.results.items()
        )
        self._record(
            PHASES[2],
            "ok",
            detail=detail or "nothing to migrate",
            evidence={name: asdict(r) for name, r in self.results.items()},
            started_at=started,
            ended_at=_now_iso(),
        )

    # -- M3 --

    def _emit_record_path(self) -> Path:
        out_dir = Path(self.cfg.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = _now_iso().replace(":", "").replace("-", "")
        return out_dir / f"corpus-backfill-{stamp}.json"

    def phase_report(self) -> dict[str, Any]:
        started = _now_iso()
        record_path = self._emit_record_path()
        self._record(
            PHASES[3],
            "ok",
            detail=f"backfill report written to {record_path}",
            evidence={"record_path": str(record_path)},
            started_at=started,
            ended_at=_now_iso(),
        )
        report = self.build_report()
        record_path.write_text(json.dumps(report, indent=2, default=str))
        record_path.with_suffix(".txt").write_text(self.human_summary(report))
        return report

    # -- report --

    def build_report(self, *, aborted: bool = False) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "runner": "audittrace-corpus-backfill",
            "dry_run": self.cfg.dry_run,
            "aborted": aborted,
            "collections": {name: asdict(r) for name, r in self.results.items()},
            "phases": [asdict(r) for r in self.records],
            "generated_at": _now_iso(),
        }

    def human_summary(self, report: dict[str, Any]) -> str:
        lines = [
            "AuditTrace corpus backfill — report (ADR-062 Phase B, WU-B6)",
            "=" * 62,
            f"dry run        : {report['dry_run']}",
            f"aborted        : {report['aborted']}",
            "",
            "collections:",
        ]
        for name, r in report["collections"].items():
            lines.append(
                f"  {name:<12} candidates={r['candidates']:<4} "
                f"migrated={r['migrated']:<4} already_corpus={r['already_corpus']:<4} "
                f"moved={r['moved']:<4} errors={len(r['errors'])}"
            )
        lines += ["", "phases:"]
        for rec in report["phases"]:
            lines.append(f"  [{rec['name']}] {rec['status']} — {rec['detail']}")
        return "\n".join(lines) + "\n"

    # -- orchestration --

    async def run(self) -> dict[str, Any]:
        try:
            await self.phase_preflight()
        except ChromaConnectError:
            return self.build_report(aborted=True)
        await self.phase_plan()
        if self.cfg.dry_run:
            self._record(
                PHASES[2],
                "planned",
                detail="would migrate the M1-planned candidates; dry-run mutates nothing",
            )
            self._record(
                PHASES[3],
                "planned",
                detail="would write the backfill report artefact; dry-run writes nothing",
            )
            return self.build_report(aborted=False)
        await self.phase_migrate()
        return self.phase_report()


# ── CLI ───────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.migrate.corpus_backfill",
        description=(
            "ADR-062 Phase B WU-B6 — one-time corpus backfill: moves "
            "pre-existing shared ChromaDB vectors from each collection's "
            "PRIVATE physical collection into its CORPUS physical "
            "collection. Idempotent — safe to re-run."
        ),
    )
    parser.add_argument(
        "--collections",
        nargs="+",
        default=list(DEFAULT_CORPUS_COLLECTIONS),
        help=f"logical collections to backfill (default: "
        f"{', '.join(DEFAULT_CORPUS_COLLECTIONS)})",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the plan; mutate nothing"
    )
    parser.add_argument("--out-dir", type=Path, default=BackfillConfig.out_dir)
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    return parser


def config_from_args(args: argparse.Namespace) -> BackfillConfig:
    return BackfillConfig(
        collections=tuple(args.collections),
        dry_run=args.dry_run,
        out_dir=args.out_dir,
        page_size=args.page_size,
    )


def print_plan(report: dict[str, Any]) -> None:
    print(f"corpus backfill plan (dry-run={report['dry_run']})\n")
    for name, r in report["collections"].items():
        print(
            f"  {name}: {r['candidates']} candidate(s), "
            f"{r['already_corpus']} already in corpus"
        )
    print()
    for rec in report["phases"]:
        print(f"  [{rec['name']}] {rec['status']} — {rec['detail']}")


async def _amain(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    args = build_parser().parse_args(argv)
    cfg = config_from_args(args)

    # Portability invariant: the ChromaDB + nomic endpoints come from
    # Settings (AUDITTRACE_* env, laptop defaults) — never hardcoded here.
    settings = get_settings()
    factory: ChromaDBFactory = HTTPChromaDBFactory(
        settings.chroma_url, token=settings.chroma_token
    )
    client = await factory.get_client()

    async def _embed(text: str) -> list[float]:
        vectors = await embed_via_nomic(
            [text], embed_url=settings.embed_url, model=settings.embed_model
        )
        return vectors[0]

    runner = CorpusBackfillRunner(cfg, client, _embed)
    report = await runner.run()
    print_plan(report)
    return 2 if report["aborted"] else 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
