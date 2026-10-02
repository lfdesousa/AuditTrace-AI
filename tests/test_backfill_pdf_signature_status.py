"""Tests for scripts/backfill_pdf_signature_status.py (#460, caller-owned).

Individual neuters (each makes the named test RED):

* dry run reaches the write path            -> test_dry_run_never_issues_a_real_index
* other-owner rows are processed            -> test_other_owner_rows_are_listed_and_never_touched
* chunk-owner equality assertion removed    -> test_chunk_owner_change_stops_the_run
* unreadable chunk owner treated as equal   -> test_unreadable_chunk_owner_is_skipped
* selection narrowed by ``created_at_ms``   -> test_selection_covers_every_date
"""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from scripts import backfill_pdf_signature_status as bf

ME = "caller-sub"
OTHER = "other-sub"
TOKEN = (
    "aaa."
    + base64.urlsafe_b64encode(json.dumps({"sub": ME}).encode()).decode()
    + ".sig"
)
FRONT = "https://front.test"


def _row(
    key: str, owner: str, *, status: str = "check_failed", **kw: Any
) -> dict[str, Any]:
    return {
        "key": key,
        "created_by_user_id": owner,
        "signature_status": status,
        "deleted_at_ms": None,
        "created_at_ms": 1,
        "modified_at_ms": 2,
        "tier": "corpus",
        **kw,
    }


class FakeFront:
    """In-memory stand-in for the front door behind ``_http_request``."""

    def __init__(self) -> None:
        self.layers: dict[str, list[dict[str, Any]]] = {
            "episodic": [],
            "procedural": [],
        }
        self.chunks: list[dict[str, Any]] = []
        self.posts: list[dict[str, str]] = []
        self.index_status = 200
        self.dry_status = 200
        self.new_status = "signed_valid"
        self.owner_after: str | None = None  # set to simulate a changed owner
        self.list_status = 200
        self.page = 500

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
            off, lim = int(q["offset"]), int(q["limit"])
            lim = min(lim, self.page)
            return (
                200,
                {},
                json.dumps(
                    {"items": rows[off : off + lim], "total": len(rows)}
                ).encode(),
            )
        if method == "GET" and path == "/memory/semantic":
            off = int(q["offset"])
            return (
                200,
                {},
                json.dumps(
                    {
                        "items": self.chunks[off : off + self.page],
                        "total": len(self.chunks),
                    }
                ).encode(),
            )
        if method == "POST" and path == "/memory/index":
            self.posts.append(q)
            dry = q.get("dry_run") == "true"
            status = self.dry_status if dry else self.index_status
            if not dry and self.owner_after is not None:
                for c in self.chunks:
                    c["created_by_user_id"] = self.owner_after
            doc = {"documents": [{"signature_status": self.new_status}]}
            return status, {"x-trace-id": "trace-1"}, json.dumps(doc).encode()
        raise AssertionError(f"unexpected {method} {url}")


@pytest.fixture
def front(monkeypatch: pytest.MonkeyPatch) -> FakeFront:
    fake = FakeFront()
    monkeypatch.setattr(bf._mem, "_http_request", fake)
    return fake


@pytest.fixture
def client() -> bf.Client:
    return bf.Client(FRONT, TOKEN, insecure=False, timeout=5)


def _seed(front: FakeFront) -> None:
    front.layers["episodic"] = [
        _row("episodic/papers/mine.pdf", ME, created_at_ms=1),
        _row("episodic/papers/theirs.pdf", OTHER),
        _row("episodic/papers/fine.pdf", ME, status="signed_valid"),
        _row("episodic/papers/gone.pdf", ME, deleted_at_ms=5),
    ]
    front.layers["procedural"] = [_row("procedural/old.pdf", ME, created_at_ms=10)]
    front.chunks = [
        {"title": "mine.pdf", "created_by_user_id": ME},
        {"title": "old.pdf", "created_by_user_id": ME},
        {"title": "theirs.pdf", "created_by_user_id": OTHER},
    ]


def _run(client: bf.Client, **kw: Any) -> bf.BackfillReport:
    return bf.run_backfill(client, ME, sleep=lambda s: None, **kw)


class TestSelection:
    def test_dry_run_never_issues_a_real_index(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        _seed(front)
        report = _run(client)
        assert report.applied is False
        assert front.posts and all(p["dry_run"] == "true" for p in front.posts)
        assert {r.outcome for r in report.rows} == {"dry_run_ok"}
        assert all(r.new_status == "signed_valid" for r in report.rows)
        assert all(r.chunk_owner_after is None for r in report.rows)

    def test_other_owner_rows_are_listed_and_never_touched(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        _seed(front)
        report = _run(client, apply=True)
        assert [o.key for o in report.other_owner_rows] == [
            "episodic/papers/theirs.pdf"
        ]
        assert report.other_owner_rows[0].owner == OTHER
        assert not any("theirs" in p["file"] for p in front.posts)
        assert {r.key for r in report.rows} == {
            "episodic/papers/mine.pdf",
            "procedural/old.pdf",
        }

    def test_selection_covers_every_date_and_skips_deleted_and_healthy(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        _seed(front)
        report = _run(client)
        keys = {r.key for r in report.rows}
        # Rows from every creation date are selected (never by created_at alone).
        assert {"episodic/papers/mine.pdf", "procedural/old.pdf"} <= keys
        assert "episodic/papers/fine.pdf" not in keys
        assert "episodic/papers/gone.pdf" not in keys

    def test_pagination_walks_every_page(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        front.page = 2
        front.layers["episodic"] = [_row(f"episodic/p{i}.pdf", ME) for i in range(5)]
        front.chunks = [
            {"title": f"p{i}.pdf", "created_by_user_id": ME} for i in range(5)
        ]
        # PAGE_SIZE is 500 but the fake caps pages at 2 to force multiple reads.
        report = _run(client, layers=("episodic",))
        assert len(report.rows) == 5

    def test_list_failure_stops_the_run(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        front.list_status = 403
        with pytest.raises(bf.BackfillError, match="HTTP 403"):
            _run(client)


class TestApply:
    def test_apply_reclassifies_only_own_rows_and_records_the_table(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        _seed(front)
        report = _run(client, apply=True)
        real = [p for p in front.posts if "dry_run" not in p]
        assert sorted(p["file"] for p in real) == [
            "episodic/papers/mine.pdf",
            "procedural/old.pdf",
        ]
        for r in report.rows:
            assert r.outcome == "applied"
            assert r.chunk_owner_before == r.chunk_owner_after == ME
            assert r.trace_id == "trace-1"
            assert r.old_status == "check_failed"
            assert r.new_status == "signed_valid"

    def test_dry_run_runs_for_every_row_before_any_real_write(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        _seed(front)
        _run(client, apply=True)
        flags = ["dry_run" in p for p in front.posts]
        assert flags == [True, True, False, False]

    def test_chunk_owner_change_stops_the_run(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        _seed(front)
        front.owner_after = OTHER
        with pytest.raises(bf.OwnerMismatchError, match="chunk owner changed"):
            _run(client, apply=True)
        # Stopped at the first mismatching row: the second was never re-indexed.
        assert len([p for p in front.posts if "dry_run" not in p]) == 1

    def test_unreadable_chunk_owner_is_skipped(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        front.layers["episodic"] = [_row("episodic/ghost.pdf", ME)]
        front.chunks = []  # no chunk row visible: the invariant cannot be checked
        report = _run(client, apply=True, layers=("episodic",))
        assert report.rows[0].outcome == "skipped:chunk_owner_unreadable"
        assert not [p for p in front.posts if "dry_run" not in p]

    def test_dry_run_http_failure_blocks_the_real_write(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        _seed(front)
        front.dry_status = 422
        report = _run(client, apply=True)
        assert {r.outcome for r in report.rows} == {"failed:dry_run_http_422"}
        assert not [p for p in front.posts if "dry_run" not in p]

    def test_real_index_http_failure_is_recorded_and_the_run_continues(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        _seed(front)
        front.index_status = 500
        report = _run(client, apply=True)
        assert {r.outcome for r in report.rows} == {"failed:index_http_500"}
        assert len([p for p in front.posts if "dry_run" not in p]) == 2


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


class TestChunkOwnerReader:
    def test_distinct_owners_are_sorted_and_joined(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        front.chunks = [
            {"title": "d.pdf", "created_by_user_id": "b"},
            {"title": "d.pdf", "created_by_user_id": "a"},
            {"title": "other.pdf", "created_by_user_id": "z"},
        ]
        assert bf.read_chunk_owner(client, "episodic/d.pdf") == "a,b"

    def test_none_when_no_chunk_is_visible(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        assert bf.read_chunk_owner(client, "episodic/none.pdf") is None

    def test_none_when_the_read_fails(
        self, monkeypatch: pytest.MonkeyPatch, client: bf.Client
    ) -> None:
        monkeypatch.setattr(bf._mem, "_http_request", lambda *a, **k: (500, {}, b""))
        assert bf.read_chunk_owner(client, "episodic/d.pdf") is None

    def test_pagination(self, front: FakeFront, client: bf.Client) -> None:
        front.page = 1
        front.chunks = [
            {"title": "x.pdf", "created_by_user_id": "a"},
            {"title": "x.pdf", "created_by_user_id": "b"},
        ]
        assert bf.read_chunk_owner(client, "x.pdf") == "a,b"


class TestMain:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUDITTRACE_FRONT_DOOR", FRONT)
        monkeypatch.setattr(bf._mem, "_resolve_token", lambda t, f=None: TOKEN)

    def test_dry_run_prints_the_table_and_never_the_token(
        self, front: FakeFront, capsys: pytest.CaptureFixture[str], tmp_path: Any
    ) -> None:
        _seed(front)
        out = tmp_path / "report.json"
        code = bf.main(["--json-out", str(out)])
        captured = capsys.readouterr()
        assert code == bf.EXIT_OK
        assert "DRY RUN" in captured.out
        assert (
            "other-owner check_failed rows (listed, NEVER touched): 1" in captured.out
        )
        assert "episodic/papers/theirs.pdf" in captured.out
        assert TOKEN not in captured.out + captured.err
        assert ME in captured.out  # the sub is not a secret; the token is
        report = json.loads(out.read_text())
        assert report["applied"] is False
        assert TOKEN not in out.read_text()

    def test_apply_exits_ok(
        self, front: FakeFront, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(front)
        assert bf.main(["--apply", "--sleep", "0"]) == bf.EXIT_OK
        assert "APPLY" in capsys.readouterr().out

    def test_owner_mismatch_exits_3(
        self, front: FakeFront, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(front)
        front.owner_after = OTHER
        assert bf.main(["--apply", "--sleep", "0"]) == bf.EXIT_OWNER_MISMATCH
        assert "STOPPED" in capsys.readouterr().err

    def test_a_failed_row_exits_nonzero(self, front: FakeFront) -> None:
        _seed(front)
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

    def test_report_formats_an_empty_run(self) -> None:
        text = bf.format_report(bf.BackfillReport(caller_sub=ME, applied=False))
        assert "caller-owned check_failed rows: 0" in text
        assert "other-owner rows:" not in text


class TestPreWriteOwnerGuards:
    """Reviewer B3: the chunk owner is checked BEFORE any write.

    Individual neuters (each makes only its own test RED):
    * (a) drop the missing/None/empty-owner check   -> test_a_*
    * (b) drop the owner == caller check            -> test_b_*
    * (c) drop the multiple-owner check             -> test_c_multiple_owners
    * (c) drop the multiple-document check          -> test_c_multiple_documents
    """

    @staticmethod
    def _one_row(front: FakeFront, chunks: list[dict[str, Any]]) -> None:
        front.layers["episodic"] = [_row("episodic/legacy.pdf", ME)]
        front.chunks = chunks

    def _assert_no_write(
        self, front: FakeFront, client: bf.Client, outcome: str
    ) -> None:
        report = _run(client, apply=True, layers=("episodic",))
        assert report.rows[0].outcome == f"skipped:{outcome}"
        assert not [p for p in front.posts if "dry_run" not in p]

    @pytest.mark.parametrize("owner", [None, ""])
    def test_a_missing_chunk_owner_skips_without_a_write(
        self, front: FakeFront, client: bf.Client, owner: Any
    ) -> None:
        self._one_row(front, [{"title": "legacy.pdf", "created_by_user_id": owner}])
        self._assert_no_write(front, client, "chunk_owner_missing")

    def test_a_absent_owner_key_skips_without_a_write(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        self._one_row(front, [{"title": "legacy.pdf"}])
        self._assert_no_write(front, client, "chunk_owner_missing")

    def test_b_chunks_owned_by_someone_else_skip_without_a_write(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        self._one_row(front, [{"title": "legacy.pdf", "created_by_user_id": OTHER}])
        self._assert_no_write(front, client, "chunk_owner_not_caller")

    def test_c_multiple_owners_skip_without_a_write(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        self._one_row(
            front,
            [
                {"title": "legacy.pdf", "created_by_user_id": ME},
                {"title": "legacy.pdf", "created_by_user_id": OTHER},
            ],
        )
        self._assert_no_write(front, client, "title_matches_multiple_owners")

    def test_c_multiple_documents_skip_without_a_write(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        self._one_row(
            front,
            [
                {
                    "title": "legacy.pdf",
                    "created_by_user_id": ME,
                    "document_sha256": "a",
                },
                {
                    "title": "legacy.pdf",
                    "created_by_user_id": ME,
                    "document_sha256": "b",
                },
            ],
        )
        self._assert_no_write(front, client, "title_matches_multiple_documents")

    def test_caller_owned_chunks_of_one_document_pass(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        self._one_row(
            front,
            [
                {
                    "title": "legacy.pdf",
                    "created_by_user_id": ME,
                    "document_sha256": "a",
                },
                {
                    "title": "legacy.pdf",
                    "created_by_user_id": ME,
                    "document_sha256": "a",
                },
            ],
        )
        report = _run(client, apply=True, layers=("episodic",))
        assert report.rows[0].outcome == "applied"

    def test_missing_owner_is_not_conflated_with_the_string_none(
        self, front: FakeFront, client: bf.Client
    ) -> None:
        self._one_row(front, [{"title": "legacy.pdf", "created_by_user_id": None}])
        assert bf.read_chunk_owner(client, "episodic/legacy.pdf") == "<none>"
