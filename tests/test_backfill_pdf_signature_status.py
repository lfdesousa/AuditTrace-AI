"""Tests for scripts/backfill_pdf_signature_status.py (#460 / GH #366).

The instrument is the fixture ``tests/fixtures/backfill_live_shapes_2026-10-06.json``:
the REAL shapes measured on the live front door (owner subs redacted to
``caller-sub``). The fake front door serves it and RAISES on any
``GET /memory/semantic`` listing call, so a reader that enumerates instead of
reading by identity cannot pass.

Individual one-edit neuters (each reddens ONLY the named tests):

* dry run reaches the write path             -> test_dry_run_never_issues_a_real_index
* other-owner rows are processed             -> test_other_owner_rows_are_listed_and_never_touched
* post-write equality assertion removed      -> TestPostWrite (one test per field)
* selection narrowed by ``created_at_ms``    -> test_selection_covers_every_date_and_skips_deleted_and_healthy
* reader re-introduces a listing call        -> TestLiveShapes (the fake raises)
* AC2 drop ``user_id == caller``             -> test_ac2_*
* AC3 drop ``source_key`` comparison          -> test_ac3_*
* AC4 treat 404 as readable                  -> test_ac4_*
* AC5 probe page 1 only                      -> test_ac5_*
* AC6 remove ``status_unchanged``            -> test_ac6_*
* AC8 re-join ``layer/key`` / drop the body  -> test_ac8_*
* S2 drop a manifest pin                     -> test_s2_*
"""

from __future__ import annotations

import ast
import base64
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from scripts import backfill_pdf_signature_status as bf

FIXTURE = json.loads(
    (
        Path(__file__).parent / "fixtures" / "backfill_live_shapes_2026-10-06.json"
    ).read_text()
)
ME = FIXTURE["caller_sub"]
OTHER = "other-sub"
TOKEN = (
    "aaa."
    + base64.urlsafe_b64encode(json.dumps({"sub": ME}).encode()).decode()
    + ".sig"
)
FRONT = "https://front.test"
# Server detail for a non-admin corpus re-index (measured 2026-10-06, token_probe).
DETAIL_403 = (
    "Required scope: memory:corpus:<collection>:write (or audittrace:admin) to "
    "index into an existing corpus-tier target — missing for: "
    "['ai_research_papers']"
)
KEY_CHANGE = "episodic/papers/00000000-0000-4000-8000-000000000001/zz-probe-swiss-tsl-20261002.pdf"
KEY_STAY = "episodic/papers/00000000-0000-4000-8000-000000000002/doc-02.pdf"
KEY_NOCHUNK = "episodic/corrupt-phase2.pdf"


def _sha_id(source_key: str, page: int = 1) -> str:
    raw = f"ai_research_papers:{source_key}:p{page}:0"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _doc_id(key: str, page: int = 1) -> str:
    return _sha_id(key[len("episodic/") :], page)


class ListingCallError(AssertionError):
    """The reader enumerated the chunk listing (forbidden: capped + title-keyed)."""


class FakeFront:
    """The live front door as measured, behind ``_http_request``."""

    def __init__(self) -> None:
        self.layers: dict[str, list[dict[str, Any]]] = {
            "episodic": copy.deepcopy(FIXTURE["manifest_rows"]),
            "procedural": [],
        }
        self.per_id: dict[str, dict[str, Any]] = copy.deepcopy(FIXTURE["per_id"])
        self.dry: dict[str, str | None] = dict(FIXTURE["dry_run_new_status"])
        self.posts: list[dict[str, str]] = []
        self.gets: list[str] = []
        self.index_status = 200
        self.dry_status = 200
        self.error_body: Any = {"detail": DETAIL_403}
        self.list_status = 200
        self.page = 500
        self.restamp: dict[str, Any] = {}  # "<chunk|manifest>.<field>" -> value
        self.chunk_status_override: int | None = None
        self.drop_row_after_write = False
        self.real: dict[str, str | None] = {}  # key -> status the WRITE produces

    @property
    def writes(self) -> list[dict[str, str]]:
        return [p for p in self.posts if "dry_run" not in p]

    def _row(self, key: str) -> dict[str, Any]:
        return next(r for r in self.layers["episodic"] if r["key"] == key)

    def _chunk_meta(self, key: str) -> dict[str, Any]:
        return self.per_id[_doc_id(key)]["body"]["metadata"]

    def __call__(
        self,
        method: str,
        url: str,
        headers: Any = None,
        body: Any = None,
        timeout: int = 30,
        context: Any = None,
    ) -> tuple[int, dict[str, str], bytes]:
        assert headers and headers["Authorization"] == f"Bearer {TOKEN}"
        parsed = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        path = parsed.path
        if method == "GET" and path in ("/memory/episodic", "/memory/procedural"):
            if self.list_status != 200:
                return self.list_status, {}, b"{}"
            rows = self.layers[path.rsplit("/", 1)[1]]
            off, lim = int(q["offset"]), min(int(q["limit"]), self.page)
            return (
                200,
                {},
                json.dumps(
                    {"items": rows[off : off + lim], "total": len(rows)}
                ).encode(),
            )
        if method == "GET" and path == "/memory/semantic":
            raise ListingCallError(f"forbidden chunk-listing call: {url}")
        if method == "GET" and path.startswith("/memory/semantic/ai_research_papers/"):
            doc_id = path.rsplit("/", 1)[1]
            self.gets.append(doc_id)
            if self.chunk_status_override is not None:
                return self.chunk_status_override, {}, b"{}"
            hit = self.per_id.get(doc_id, {"status": 404, "body": {"detail": "nf"}})
            return hit["status"], {}, json.dumps(hit["body"]).encode()
        if method == "POST" and path == "/memory/index":
            self.posts.append(q)
            dry = q.get("dry_run") == "true"
            status = self.dry_status if dry else self.index_status
            if status != 200:
                return status, {}, json.dumps(self.error_body).encode()
            new = self.dry[q["file"]]
            if not dry:
                new = self.real.get(q["file"], new)
                self._write(q["file"], new)
            doc = {"documents": [{"signature_status": new}]}
            return 200, {"x-trace-id": "trace-1"}, json.dumps(doc).encode()
        raise AssertionError(f"unexpected {method} {url}")

    def _write(self, key: str, new: str | None) -> None:
        self._chunk_meta(key)["signature_status"] = new
        self._row(key)["signature_status"] = new
        for dotted, value in self.restamp.items():
            scope, field = dotted.split(".")
            target = self._chunk_meta(key) if scope == "chunk" else self._row(key)
            target[field] = value
        if self.drop_row_after_write:
            self.layers["episodic"].remove(self._row(key))


@pytest.fixture
def front(monkeypatch: pytest.MonkeyPatch) -> FakeFront:
    fake = FakeFront()
    monkeypatch.setattr(bf._mem, "_http_request", fake)
    return fake


@pytest.fixture
def client() -> bf.Client:
    return bf.Client(FRONT, TOKEN, insecure=False, timeout=5)


def _run(client: bf.Client, **kw: Any) -> bf.BackfillReport:
    return bf.run_backfill(client, ME, sleep=lambda s: None, **kw)


def _by_outcome(report: bf.BackfillReport) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in report.rows:
        out.setdefault(r.outcome, []).append(r.key)
    return out


def _single(front: FakeFront, key: str) -> None:
    """Keep only *key* among the manifest rows (isolates one guard)."""
    front.layers["episodic"] = [r for r in front.layers["episodic"] if r["key"] == key]


class TestLiveShapes:
    """AC1: the full run against the recorded live shapes."""

    def test_fixture_is_the_recorded_shape(self) -> None:
        rows = FIXTURE["manifest_rows"]
        assert len(rows) == 19
        listing = FIXTURE["semantic_listing"]
        assert (listing["total"], listing["total_documents"]) == (200, 2)
        assert len(listing["items"]) == 200
        assert sorted(listing["items"][0]) == [
            "created_at_ms",
            "created_by_user_id",
            "deleted_at_ms",
            "deleted_by_user_id",
            "discovered",
            "id",
            "key",
            "layer",
            "modified_at_ms",
            "modified_by_user_id",
            "size_bytes",
            "tier",
            "title",
        ]
        # The defect: none of the 19 file names is visible in the capped listing.
        titles = {i["title"] for i in listing["items"]}
        assert len(titles) == 2
        assert not titles & {r["key"].rsplit("/", 1)[-1] for r in rows}
        statuses = [FIXTURE["per_id"][_doc_id(r["key"])]["status"] for r in rows]
        assert (statuses.count(200), statuses.count(404)) == (12, 7)
        assert "0b0cdd4d" not in json.dumps(FIXTURE)  # owner subs stay redacted

    def test_derivation_matches_the_recorded_ids(self) -> None:
        for row in FIXTURE["manifest_rows"]:
            sk = bf.derive_source_key("episodic", row["key"])
            assert bf.chunk_doc_id(sk, 1) == _doc_id(row["key"])
            assert _doc_id(row["key"]) in FIXTURE["per_id"]
        anchors = FIXTURE["_provenance"]["live_anchor_ids"]
        assert anchors["episodic/main_signed.pdf"] == "31857246cea26a31"
        for key, recorded in anchors.items():
            assert _doc_id(key) == recorded  # recorded live ids, public-safe rows

    def test_the_fake_raises_on_a_listing_call(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        with pytest.raises(ListingCallError):
            client.request(
                "GET", "/memory/semantic", {"collection": "ai_research_papers"}
            )

    def test_ac1_full_dry_run(self, front: FakeFront, client: bf.Client) -> None:
        report = _run(client)
        got = _by_outcome(report)
        assert {k: len(v) for k, v in got.items()} == {
            "skipped:chunk_not_found": 7,
            "dry_run_ok": 6,
            "skipped:status_unchanged": 6,
        }
        assert front.writes == []
        assert all(p["dry_run"] == "true" for p in front.posts)
        # Owners resolved for the 12 rows that have chunks; ids are the measured ones.
        resolved = [r for r in report.rows if r.chunk_owner_before == ME]
        assert len(resolved) == 12
        assert all(r.doc_id == _doc_id(r.key) for r in resolved)
        assert KEY_CHANGE in got["dry_run_ok"]
        assert KEY_STAY in got["skipped:status_unchanged"]
        assert KEY_NOCHUNK in got["skipped:chunk_not_found"]
        assert report.other_owner_rows == []

    def test_the_seven_not_found_rows_are_never_dry_run(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        _run(client)
        no_chunk = {
            r["key"]
            for r in FIXTURE["manifest_rows"]
            if FIXTURE["per_id"][_doc_id(r["key"])]["status"] == 404
        }
        assert len(no_chunk) == 7
        assert not {p["file"] for p in front.posts} & no_chunk

    def test_ac1_apply_writes_exactly_the_six(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        report = _run(client, apply=True)
        got = _by_outcome(report)
        assert len(got["applied"]) == 6
        assert len(front.writes) == 6
        assert {p["file"] for p in front.writes} == set(got["applied"])
        for r in report.rows:
            if r.outcome == "applied":
                assert r.new_status == "signed_expired"
                assert r.manifest_status_after == "signed_expired"
                assert r.chunk_owner_before == r.chunk_owner_after == ME
        flags = ["dry_run" in p for p in front.posts]
        assert flags == [True] * 12 + [False] * 6  # every dry run precedes any write


class TestFixtureRedaction:
    """The repo is public: the fixture carries no counterparty names or scan ids."""

    # Every ``.pdf`` name that may appear anywhere in the fixture or this module.
    # Public paper titles, public-safe probe/sample names, and neutral synthetic
    # names used by the tests. Anything else fails closed.
    SAFE_NAMES = frozenset(
        {
            "main_signed.pdf",
            "clean.pdf",
            "eicar.pdf",
            "357-rls-evidence.pdf",
            "zz-probe-swiss-tsl-20261002.pdf",
            "corrupt-phase2.pdf",
            "corrupt-refactor.pdf",
            "corrupt-tier-c.pdf",
            "2402.01613-nomic-embed.pdf",
            "ISO_IEC_42001_2023(en).pdf",
            "theirs.pdf",
            "anon.pdf",
            "fine.pdf",
            "gone.pdf",
            "late.pdf",
            "runbook.pdf",
            "other.pdf",
            "zz.pdf",
            "x.pdf",
            "a.pdf",
            "a b.pdf",
            "a%20b.pdf",
        }
    )
    EXT = "." + "pdf"  # built, so this module carries no bare extension literal
    NAME_TOKEN = re.compile(r"[^\s|/]+\." + "pdf", re.IGNORECASE)

    @classmethod
    def is_safe(cls, name: str) -> bool:
        low = name.lower()  # the server matches the extension case-insensitively
        stem = low[: -len(cls.EXT)]
        placeholder = stem.startswith("doc-") and stem[4:].isdigit() and len(stem) == 6
        return low in {n.lower() for n in cls.SAFE_NAMES} or placeholder

    @classmethod
    def pdf_names(cls, text: str) -> set[str]:
        """Every pdf file name a string carries (a whole-string name, or tokens)."""
        low = text.lower()
        if cls.EXT not in low:
            return set()
        if low.endswith(cls.EXT):
            return {text.rsplit("/", 1)[-1]}  # a whole-string name
        return set(cls.NAME_TOKEN.findall(text))

    @staticmethod
    def json_strings(node: Any) -> Any:
        """Every string anywhere in a JSON value: keys and values."""
        if isinstance(node, str):
            yield node
        elif isinstance(node, dict):
            for k, v in node.items():
                yield from TestFixtureRedaction.json_strings(k)
                yield from TestFixtureRedaction.json_strings(v)
        elif isinstance(node, list):
            for v in node:
                yield from TestFixtureRedaction.json_strings(v)

    UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

    def test_every_fixture_pdf_name_is_on_the_allowlist(self) -> None:
        names: set[str] = set()
        for text in self.json_strings(FIXTURE):
            names |= self.pdf_names(text)
        assert {"main_signed.pdf", "2402.01613-nomic-embed.pdf"} <= names
        assert not {n for n in names if not self.is_safe(n)}

    def test_every_test_module_pdf_literal_is_on_the_allowlist(self) -> None:
        tree = ast.parse(Path(__file__).read_text())
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                names |= self.pdf_names(node.value)
        assert "late.pdf" in names
        assert not {n for n in names if not self.is_safe(n)}

    def test_every_scan_id_is_the_neutral_placeholder(self) -> None:
        text = json.dumps(FIXTURE)
        found = set(self.UUID.findall(text))
        assert found
        assert all(u.startswith("00000000-0000-4000-8000-") for u in found), found

    def test_no_counterparty_marker_appears_in_fixture_or_tests(self) -> None:
        markers = ("fr" + "ank", "under" + "ground")
        here = Path(__file__)
        for text in (json.dumps(FIXTURE), here.read_text()):
            assert not any(m in text.lower() for m in markers)


class TestChunkGuards:
    def test_ac2_chunk_owned_by_someone_else_is_skipped(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        _single(front, KEY_CHANGE)
        front._chunk_meta(KEY_CHANGE)["user_id"] = OTHER
        report = _run(client, apply=True)
        assert report.rows[0].outcome == "skipped:chunk_owner_not_caller"
        assert report.rows[0].chunk_owner_before == OTHER
        assert front.posts == []

    @pytest.mark.parametrize("owner", [None, ""])
    def test_ac2_missing_owner_is_skipped(
        self, front: FakeFront, client: bf.Client, owner: Any
    ) -> None:
        _single(front, KEY_CHANGE)
        front._chunk_meta(KEY_CHANGE)["user_id"] = owner
        report = _run(client, apply=True)
        assert report.rows[0].outcome == "skipped:chunk_owner_missing"
        assert front.posts == []

    def test_ac2_absent_owner_key_is_skipped(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        _single(front, KEY_CHANGE)
        del front._chunk_meta(KEY_CHANGE)["user_id"]
        report = _run(client, apply=True)
        assert report.rows[0].outcome == "skipped:chunk_owner_missing"

    def test_ac3_source_key_mismatch_is_skipped(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        _single(front, KEY_CHANGE)
        front._chunk_meta(KEY_CHANGE)["source_key"] = "papers/other/zz.pdf"
        report = _run(client, apply=True)
        assert report.rows[0].outcome == "skipped:chunk_source_key_mismatch"
        assert front.posts == []

    def test_ac4_a_404_is_a_skip_never_a_run(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        _single(front, KEY_CHANGE)
        del front.per_id[_doc_id(KEY_CHANGE)]  # every probed page answers 404
        report = _run(client, apply=True)
        assert report.rows[0].outcome == "skipped:chunk_not_found"
        assert report.rows[0].doc_id is None
        assert front.posts == []
        assert len(front.gets) == 23  # probed pages 1..page_count, then gave up

    @pytest.mark.parametrize("page_count", [None, 0, True, "3"])
    def test_ac4_no_usable_page_count_means_no_probe(
        self, front: FakeFront, client: bf.Client, page_count: Any
    ) -> None:
        _single(front, KEY_CHANGE)
        front._row(KEY_CHANGE)["page_count"] = page_count
        report = _run(client)
        assert report.rows[0].outcome == "skipped:chunk_not_found"
        assert front.gets == []

    def test_ac5_the_page_probe_walks_past_a_404(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        _single(front, KEY_CHANGE)
        entry = front.per_id.pop(_doc_id(KEY_CHANGE))
        entry["body"]["metadata"]["page"] = 2
        front.per_id[_doc_id(KEY_CHANGE, 2)] = entry
        report = _run(client)
        assert report.rows[0].outcome == "dry_run_ok"
        assert report.rows[0].doc_id == _doc_id(KEY_CHANGE, 2)
        assert front.gets == [_doc_id(KEY_CHANGE, 1), _doc_id(KEY_CHANGE, 2)]

    @pytest.mark.parametrize("status", [403, 500])
    def test_a_non_404_failure_is_a_distinct_fail_closed_skip(
        self, front: FakeFront, client: bf.Client, status: int
    ) -> None:
        _single(front, KEY_CHANGE)
        front.chunk_status_override = status
        report = _run(client, apply=True)
        assert report.rows[0].outcome == "skipped:chunk_read_failed"
        assert front.posts == []
        assert len(front.gets) == 1  # no page walk after a hard failure

    def test_a_200_without_metadata_is_an_owner_miss(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        _single(front, KEY_CHANGE)
        front.per_id[_doc_id(KEY_CHANGE)]["body"] = {"content": "x"}
        report = _run(client, apply=True)
        assert report.rows[0].outcome == "skipped:chunk_owner_missing"

    def test_ac6_status_unchanged_is_never_written(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        report = _run(client, apply=True)
        stayed = {p["file"] for p in front.writes} & {
            k for k, v in front.dry.items() if v == "check_failed"
        }
        assert stayed == set()
        assert len(_by_outcome(report)["skipped:status_unchanged"]) == 6

    def test_a_dry_run_without_a_status_is_skipped(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        _single(front, KEY_CHANGE)
        front.dry[KEY_CHANGE] = None
        report = _run(client, apply=True)
        assert report.rows[0].outcome == "skipped:dry_run_status_missing"
        assert front.writes == []


class TestManifestPins:
    """S2: pins read from the manifest row itself."""

    def test_s2_modifier_not_caller_is_skipped_before_any_read(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        _single(front, KEY_CHANGE)
        front._row(KEY_CHANGE)["modified_by_user_id"] = OTHER
        report = _run(client, apply=True)
        assert report.rows[0].outcome == "skipped:manifest_modifier_not_caller"
        assert front.gets == [] and front.posts == []

    def test_s2_parse_timeout_row_is_skipped(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        _single(front, KEY_CHANGE)
        front._row(KEY_CHANGE)["extraction_warnings"] = [
            {"code": "attachment"},
            {"code": "parse_timeout"},
        ]
        report = _run(client, apply=True)
        assert report.rows[0].outcome == "skipped:parse_timeout_partial_index"
        assert front.gets == [] and front.posts == []

    def test_other_warnings_do_not_block(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        _single(front, KEY_STAY)  # carries an ``attachment`` warning
        front._row(KEY_STAY)["extraction_warnings"].append("not-a-dict")
        assert _run(client).rows[0].outcome == "skipped:status_unchanged"
        front._row(KEY_STAY)["extraction_warnings"] = None
        assert _run(client).rows[0].outcome == "skipped:status_unchanged"


class TestPostWrite:
    """AC7: the second line, one field per test."""

    @pytest.mark.parametrize(
        ("dotted", "value", "needle"),
        [
            ("chunk.user_id", OTHER, "chunk user_id"),
            ("chunk.source_key", "papers/other.pdf", "chunk source_key"),
            ("chunk.document_hash", "deadbeef", "chunk document_hash"),
            ("chunk.signature_status", "check_failed", "chunk signature_status"),
            ("manifest.signature_status", "check_failed", "manifest signature_status"),
            ("manifest.created_by_user_id", OTHER, "manifest created_by_user_id"),
        ],
    )
    def test_ac7_each_field_stops_the_run(
        self,
        front: FakeFront,
        client: bf.Client,
        dotted: str,
        value: str,
        needle: str,
    ) -> None:
        front.restamp = {dotted: value}
        with pytest.raises(
            bf.OwnerMismatchError, match=f"post-write mismatch.*{needle}"
        ):
            _run(client, apply=True)
        assert len(front.writes) == 1  # stopped at the first mismatching row

    def test_r2_a_write_that_does_not_make_the_predicted_change_stops_the_run(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        # Dry run predicts signed_expired; the write itself re-classifies to
        # check_failed (and the chunk + manifest agree with the WRITE, so only a
        # comparison against the DRY-RUN prediction can see it).
        front.real = {KEY_CHANGE: "check_failed"}
        with pytest.raises(
            bf.OwnerMismatchError, match="index response signature_status"
        ):
            _run(client, apply=True)
        assert len(front.writes) == 1  # no further writes

    def test_r2_state_must_equal_the_prediction_not_the_response(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        # Response matches the prediction but the stores did not take the change.
        front.restamp = {"manifest.signature_status": "check_failed"}
        with pytest.raises(bf.OwnerMismatchError, match="manifest signature_status"):
            _run(client, apply=True)

    def test_r2_a_write_answering_without_a_status_stops_the_run(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        front.real = {KEY_CHANGE: None}
        with pytest.raises(
            bf.OwnerMismatchError, match="index response signature_status"
        ):
            _run(client, apply=True)

    def test_a1_the_apu_cap_sleep_runs_before_every_real_write(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        naps: list[float] = []
        bf.run_backfill(client, ME, apply=True, sleep_seconds=7.5, sleep=naps.append)
        assert naps == [7.5] * 6 == [7.5] * len(front.writes)

    def test_a_clean_write_passes_every_check(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        assert len(_by_outcome(_run(client, apply=True))["applied"]) == 6

    def test_a_row_missing_after_the_write_stops_the_run(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        front.drop_row_after_write = True
        with pytest.raises(bf.OwnerMismatchError, match="manifest row"):
            _run(client, apply=True)

    def test_an_unreadable_chunk_after_the_write_stops_the_run(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        row = bf.TableRow("episodic", KEY_CHANGE, "check_failed")
        chunk = bf.ChunkOwner("nope", 1, ME, "k", "h", "s")
        with pytest.raises(bf.OwnerMismatchError, match="chunk"):
            bf.verify_post_write(client, "episodic", row, chunk, ME, "signed_expired")
        assert row.outcome == "failed:post_write_mismatch"

    def test_real_index_http_failure_is_recorded_and_the_run_continues(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        front.index_status = 500
        front.error_body = "boom"  # a non-object body carries no detail
        report = _run(client, apply=True)
        assert len(_by_outcome(report)["failed:index_http_500"]) == 6
        assert len(front.writes) == 6
        assert {r.error for r in report.rows if r.outcome.startswith("failed")} == {
            None
        }


class TestSelection:
    def test_dry_run_never_issues_a_real_index(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        report = _run(client)
        assert report.applied is False
        assert front.posts and all(p["dry_run"] == "true" for p in front.posts)
        assert all(r.chunk_owner_after is None for r in report.rows)

    def test_other_owner_rows_are_listed_and_never_touched(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        theirs = copy.deepcopy(front._row(KEY_CHANGE))
        theirs["key"] = "episodic/papers/theirs.pdf"
        theirs["created_by_user_id"] = OTHER
        front.layers["episodic"].append(theirs)
        theirs2 = dict(theirs, key="episodic/papers/anon.pdf", created_by_user_id=None)
        front.layers["episodic"].append(theirs2)
        report = _run(client, apply=True)
        assert [o.key for o in report.other_owner_rows] == [
            "episodic/papers/theirs.pdf",
            "episodic/papers/anon.pdf",
        ]
        assert report.other_owner_rows[0].owner == OTHER
        assert report.other_owner_rows[1].owner is None
        assert not any("theirs" in p["file"] for p in front.posts)
        assert not any("anon" in p["file"] for p in front.posts)
        assert len(report.rows) == 19

    def test_selection_covers_every_date_and_skips_deleted_and_healthy(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        base = front._row(KEY_CHANGE)
        front.layers["episodic"] += [
            dict(base, key="episodic/fine.pdf", signature_status="signed_valid"),
            dict(base, key="episodic/gone.pdf", deleted_at_ms=5),
            dict(base, key="episodic/late.pdf", created_at_ms=10**13),
        ]
        keys = {r.key for r in _run(client).rows}
        assert "episodic/late.pdf" in keys and KEY_CHANGE in keys
        assert "episodic/fine.pdf" not in keys
        assert "episodic/gone.pdf" not in keys

    def test_procedural_rows_use_the_procedural_prefix(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        row = dict(front._row(KEY_CHANGE), key="procedural/runbook.pdf")
        front.layers["procedural"] = [row]
        sk = "runbook.pdf"
        entry = copy.deepcopy(front.per_id[_doc_id(KEY_CHANGE)])
        entry["body"]["metadata"]["source_key"] = sk
        front.per_id[_sha_id(sk)] = entry
        front.dry["procedural/runbook.pdf"] = "signed_valid"
        report = _run(client, layers=("procedural",))
        assert report.rows[0].outcome == "dry_run_ok"
        assert front.posts[0]["file"] == "procedural/runbook.pdf"

    def test_pagination_walks_every_page(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        front.page = 2
        assert len(_run(client).rows) == 19

    def test_list_failure_stops_the_run(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        front.list_status = 403
        with pytest.raises(bf.BackfillError, match="HTTP 403"):
            _run(client)

    def test_the_scanner_seam_is_honoured(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        seen: list[str] = []

        def scanner(c: bf.Client, layer: str, key: str, pages: Any) -> bf.ChunkRead:
            seen.append(key)
            return bf.ChunkRead(None, None)  # no reason given -> chunk_not_found

        report = _run(client, scanner=scanner)
        assert len(seen) == 19
        assert {r.outcome for r in report.rows} == {"skipped:chunk_not_found"}


class TestReportAndErrors:
    def test_ac8_report_key_column_is_the_fixture_key_byte_for_byte(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        text = bf.format_report(_run(client))
        data_lines = [ln for ln in text.splitlines() if " | " in ln][1:20]
        cols = [ln.split(" | ") for ln in data_lines]
        assert {c[1] for c in cols} == {r["key"] for r in FIXTURE["manifest_rows"]}
        assert {c[0] for c in cols} == {"episodic"}
        assert "episodic/episodic/" not in text

    def test_ac8_the_403_detail_is_carried_in_the_error_column(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        front.dry_status = 403
        report = _run(client, apply=True)
        failed = [r for r in report.rows if r.outcome == "failed:dry_run_http_403"]
        assert len(failed) == 12  # the rows with a readable chunk reach the dry run
        assert {r.error for r in failed} == {DETAIL_403}
        assert front.writes == []
        assert DETAIL_403 in bf.format_report(report)

    def test_a_structured_detail_is_serialised_and_bounded(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        front.dry_status = 422
        front.error_body = {"detail": [{"loc": ["query"], "msg": "x" * 2000}]}
        report = _run(client)
        err = next(r.error for r in report.rows if r.outcome.endswith("422"))
        assert err is not None and err.startswith('[{"loc"')
        assert len(err) == bf.DETAIL_MAX_CHARS

    def test_report_json_round_trips_the_new_columns(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        row = json.loads(_run(client).to_json())["rows"][0]
        assert {"doc_id", "manifest_status_after", "error"} <= set(row)

    def test_report_formats_an_empty_run(self) -> None:
        text = bf.format_report(bf.BackfillReport(caller_sub=ME, applied=False))
        assert "caller-owned check_failed rows: 0" in text
        assert "other-owner rows:" not in text

    def test_report_lists_other_owner_rows(self) -> None:
        rep = bf.BackfillReport(caller_sub=ME, applied=False)
        rep.other_owner_rows.append(
            bf.OtherOwnerRow("episodic", "a b.pdf", OTHER, 1, 2)
        )
        assert "episodic/a%20b.pdf | other-sub | 1 | 2" in bf.format_report(rep)


class TestFileParamAndIdentity:
    @pytest.mark.parametrize(
        ("row", "layer", "expected"),
        [
            ({"key": "episodic/x.pdf", "tier": "corpus"}, "episodic", "episodic/x.pdf"),
            (
                {"key": f"{ME}/episodic/x.pdf", "tier": "private"},
                "episodic",
                f"{ME}/episodic/x.pdf",
            ),
            ({"key": "x.pdf", "tier": "private"}, "episodic", f"{ME}/episodic/x.pdf"),
            ({"key": "x.pdf", "tier": "corpus"}, "procedural", "procedural/x.pdf"),
            ({"key": "x.pdf"}, "procedural", "procedural/x.pdf"),
        ],
    )
    def test_index_file_param(
        self, row: dict[str, Any], layer: str, expected: str
    ) -> None:
        assert bf.index_file_param(row, ME, layer) == expected

    def test_derive_source_key(self) -> None:
        assert bf.derive_source_key("episodic", "episodic/a.pdf") == "a.pdf"
        assert bf.derive_source_key("episodic", f"{ME}/episodic/a.pdf") == (
            f"{ME}/episodic/a.pdf"
        )

    def test_chunk_doc_id_is_the_pipeline_formula(self) -> None:
        # A recorded LIVE id (200, owner = caller): p1, chunk 0 of the public-safe
        # anchor row ``episodic/main_signed.pdf`` (source_key ``main_signed.pdf``).
        assert bf.chunk_doc_id("main_signed.pdf", 1) == "31857246cea26a31"
        assert bf.chunk_doc_id("a.pdf", 1) != bf.chunk_doc_id("a.pdf", 2)

    def test_caller_sub_from_token(self) -> None:
        assert bf.caller_sub_from_token(TOKEN) == ME

    @pytest.mark.parametrize(
        "token",
        [
            "nodots",
            "a.%%%.c",
            "a." + base64.urlsafe_b64encode(b"[]").decode() + ".c",
            "a." + base64.urlsafe_b64encode(b'{"x":1}').decode() + ".c",
        ],
    )
    def test_caller_sub_rejects_a_bad_token(self, token: str) -> None:
        with pytest.raises(bf.BackfillError):
            bf.caller_sub_from_token(token)


class TestMain:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUDITTRACE_FRONT_DOOR", FRONT)
        monkeypatch.setattr(bf._mem, "_resolve_token", lambda t, f=None: TOKEN)

    def test_dry_run_prints_the_table_and_never_the_token(
        self, front: FakeFront, capsys: pytest.CaptureFixture[str], tmp_path: Any
    ) -> None:
        out = tmp_path / "report.json"
        code = bf.main(["--json-out", str(out)])
        captured = capsys.readouterr()
        assert code == bf.EXIT_OK
        assert "DRY RUN" in captured.out
        assert (
            "other-owner check_failed rows (listed, NEVER touched): 0" in captured.out
        )
        assert KEY_CHANGE in captured.out
        assert TOKEN not in captured.out + captured.err
        report = json.loads(out.read_text())
        assert report["applied"] is False
        assert TOKEN not in out.read_text()

    def test_apply_exits_ok(
        self, front: FakeFront, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert bf.main(["--apply", "--sleep", "0"]) == bf.EXIT_OK
        assert "APPLY" in capsys.readouterr().out
        assert len(front.writes) == 6

    def test_a_post_write_mismatch_exits_3(
        self, front: FakeFront, capsys: pytest.CaptureFixture[str]
    ) -> None:
        front.restamp = {"chunk.user_id": OTHER}
        assert bf.main(["--apply", "--sleep", "0"]) == bf.EXIT_OWNER_MISMATCH
        assert "STOPPED" in capsys.readouterr().err

    def test_a_failed_row_exits_nonzero(self, front: FakeFront) -> None:
        front.dry_status = 500
        assert bf.main([]) == bf.EXIT_FAILURE

    def test_a_listing_failure_exits_failure(
        self, front: FakeFront, capsys: pytest.CaptureFixture[str]
    ) -> None:
        front.list_status = 401
        assert bf.main([]) == bf.EXIT_FAILURE
        assert "FAILED" in capsys.readouterr().err

    @pytest.mark.parametrize("layers", ["", "semantic", "episodic,bogus"])
    def test_bad_layers_is_a_usage_error(self, layers: str) -> None:
        assert bf.main(["--layers", layers]) == bf.EXIT_USAGE

    def test_bad_front_door_is_a_usage_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AUDITTRACE_FRONT_DOOR", "not-a-url")
        assert bf.main([]) == bf.EXIT_USAGE

    def test_no_token_is_a_usage_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bf._mem, "_resolve_token", lambda t, f=None: None)
        assert bf.main([]) == bf.EXIT_USAGE

    def test_undecodable_token_fails_cleanly(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(bf._mem, "_resolve_token", lambda t, f=None: "garbage")
        assert bf.main([]) == bf.EXIT_FAILURE
        assert "garbage" not in capsys.readouterr().err
