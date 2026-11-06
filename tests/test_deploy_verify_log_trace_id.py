"""AC13 (LOG-TRACE-ID D7): the ``log-trace-id`` verify probe.

Unit tests of ``scripts.deploy.verify`` with every external effect replaced:
the front door and Loki go through ``verify._http_request`` (one recording
fake that routes on the exact URL PATH, never a substring), the poll's sleep
goes through ``verify._sleep``. No socket, no real sleep, and nothing asserts
on elapsed time - only on attempt COUNTS.

Neuter N9: make the probe PASS on 0 lines -> the "no lines" cases go RED.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from scripts.deploy import verify
from scripts.deploy.verify import FAIL, PASS, VerifyConfig, VerifyRunner

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
LOKI_PATH = "/loki/api/v1/query_range"
MEMORY_READ = json.dumps(
    {"message": "memory.read | surface=tool collection=decisions", "trace_id": TRACE_ID}
)
OTHER_LINE = json.dumps({"message": "chat.completed", "trace_id": TRACE_ID})


def _answer(*lines: str) -> dict[str, Any]:
    result = (
        [
            {
                "stream": {"container": "memory-server"},
                "values": [[str(1_000 + i), ln] for i, ln in enumerate(lines)],
            }
        ]
        if lines
        else []
    )
    return {"status": "success", "data": {"resultType": "streams", "result": result}}


class _Net:
    """Routes ``_http_request`` by exact (method, path); Loki answers come from a
    script so a test can model ingestion lag; a ``0`` status is the transport
    failure sentinel."""

    def __init__(
        self,
        *,
        trace_id: str | None = TRACE_ID,
        loki: list[tuple[int, Any]] | None = None,
    ) -> None:
        self.trace_id = trace_id
        self.loki = list(loki if loki is not None else [(200, _answer(MEMORY_READ))])
        self.loki_urls: list[str] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: int = 30,
        context: Any = None,
    ) -> tuple[int, dict[str, str], bytes]:
        path = urlparse(url).path
        if (method, path) == ("POST", "/v1/chat/completions"):
            payload: Any = {"choices": [{"message": {"content": "hi"}}]}
        elif (method, path) == ("GET", "/interactions"):
            row: dict[str, Any] = {"id": 42}
            if self.trace_id is not None:
                row["trace_id"] = self.trace_id
            payload = {"interactions": [row]}
        elif (method, path) == ("GET", "/interactions/42/tool-calls"):
            payload = {"tool_calls": [{"tool_name": "recall_semantic"}]}
        elif (method, path) == ("GET", LOKI_PATH):
            self.loki_urls.append(url)
            status, obj = self.loki.pop(0) if len(self.loki) > 1 else self.loki[0]
            return status, {}, json.dumps(obj).encode()
        else:  # pragma: no cover - an unexpected route is a test bug
            raise AssertionError((method, url))
        return 200, {}, json.dumps(payload).encode()


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr(verify, "_sleep", calls.append)
    return calls


def _runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: _Net, **cfg: Any
) -> VerifyRunner:
    monkeypatch.setattr(verify, "load_token", lambda tf: "tok")
    monkeypatch.setattr(verify, "_http_request", net)
    cfg.setdefault("out_dir", tmp_path / "runs")
    cfg.setdefault("target_version", "v9.9.9")
    return VerifyRunner(VerifyConfig(**cfg))


def _probe(runner: VerifyRunner) -> Any:
    assert runner.probe_recall_e2e().status == PASS  # supplies the row's trace_id
    return runner.probe_log_trace_id()


def test_pass_when_loki_returns_a_memory_read_line_for_the_row_trace_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    net = _Net()
    res = _probe(_runner(tmp_path, monkeypatch, net))
    assert res.status == PASS
    assert res.evidence["memory_read_lines"] == 1 and res.evidence["attempts"] == 1
    assert sleeps == []
    # the query joins on the row's id, on the memory-server container, via json
    query = parse_qs(urlparse(net.loki_urls[0]).query)["query"][0]
    assert query == f'{{container="memory-server"}} | json | trace_id="{TRACE_ID}"'


def test_pass_after_ingestion_lag_polls_a_bounded_number_of_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    net = _Net(loki=[(200, _answer()), (200, _answer()), (200, _answer(MEMORY_READ))])
    res = _probe(_runner(tmp_path, monkeypatch, net))
    assert res.status == PASS and res.evidence["attempts"] == 3
    assert len(sleeps) == 2


@pytest.mark.parametrize(
    ("loki", "reason"),
    [
        ([(200, _answer())], "returned 0 line(s)"),
        ([(200, _answer(OTHER_LINE))], "none is a memory.read line"),
        ([(0, {})], "unreachable or errored (HTTP 0)"),
        ([(500, {})], "unreachable or errored (HTTP 500)"),
    ],
    ids=["zero-lines", "no-memory-read", "connection-refused", "server-error"],
)
def test_fail_closed_after_exactly_the_poll_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sleeps: list[float],
    loki: list[tuple[int, Any]],
    reason: str,
) -> None:
    """Mandate-to-fail: nothing joined means FAIL, never a skip, and the poll is
    bounded to ``LOKI_POLL_ATTEMPTS`` reads with one sleep between reads."""
    net = _Net(loki=loki)
    res = _probe(_runner(tmp_path, monkeypatch, net))
    assert res.status == FAIL
    assert reason in res.detail or reason in json.dumps(res.evidence)
    assert len(net.loki_urls) == verify.LOKI_POLL_ATTEMPTS
    assert len(sleeps) == verify.LOKI_POLL_ATTEMPTS - 1
    assert res.evidence["loki_url"] == verify.LOKI_URL_DEFAULT


def test_fail_when_the_audit_row_has_no_trace_id_and_loki_is_never_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    net = _Net(trace_id=None)
    res = _probe(_runner(tmp_path, monkeypatch, net))
    assert (
        res.status == FAIL and "no interaction row with a 32-hex trace_id" in res.detail
    )
    assert net.loki_urls == [] and sleeps == []


@pytest.mark.parametrize(
    "bad",
    ['x" or 1=1 | trace_id="y', TRACE_ID.upper(), TRACE_ID[:-1], ""],
    ids=["logql-injection", "uppercase", "short", "empty"],
)
def test_fail_when_the_row_trace_id_is_not_32_lowercase_hex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float], bad: str
) -> None:
    """The id is audit-row data placed inside a LogQL string: anything that is
    not 32 lowercase hex is refused before any query is built."""
    net = _Net(trace_id=bad)
    res = _probe(_runner(tmp_path, monkeypatch, net))
    assert res.status == FAIL
    assert net.loki_urls == []


def test_fail_without_a_recall_e2e_row_at_all(tmp_path: Path) -> None:
    res = VerifyRunner(
        VerifyConfig(out_dir=tmp_path / "runs", target_version="v9.9.9")
    ).probe_log_trace_id()
    assert res.status == FAIL


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://loki:3100", "loki:3100"])
def test_fail_on_a_non_http_loki_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float], url: str
) -> None:
    net = _Net()
    res = _probe(_runner(tmp_path, monkeypatch, net, loki_url=url))
    assert res.status == FAIL and "unsupported Loki URL scheme" in res.detail
    assert net.loki_urls == []


def test_the_configured_loki_url_is_the_one_queried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """Portability invariant: the Loki location is configuration, not a literal."""
    net = _Net()
    runner = _runner(
        tmp_path, monkeypatch, net, loki_url="https://loki.example.test:3100/"
    )
    assert _probe(runner).status == PASS
    parsed = urlparse(net.loki_urls[0])
    assert (parsed.scheme, parsed.netloc, parsed.path) == (
        "https",
        "loki.example.test:3100",
        LOKI_PATH,
    )
    assert "loki:https://loki.example.test:3100/" in runner.evidence_sources()


def test_probe_is_the_twelfth_and_last_and_the_report_keys_are_additive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    assert len(verify.PROBES) == 12 and verify.PROBES[-1] == "log-trace-id"
    assert verify.PROBES[:11][4] == "recall-e2e"  # earlier indices are stable
    runner = _runner(tmp_path, monkeypatch, _Net())
    runner.probe_recall_e2e()
    runner.probe_log_trace_id()
    report = runner.build_report()
    assert [p["name"] for p in report["probes"]][-1] == "log-trace-id"
    assert f"loki:{verify.LOKI_URL_DEFAULT}" in report["evidence_sources"]
    assert report["reads_deploy_report"] is False


def test_cli_loki_url_flag_env_var_and_laptop_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(verify.LOKI_URL_ENV_VAR, raising=False)
    parser = verify.build_parser()
    assert (
        parser.parse_args(["--target-version", "v1"]).loki_url
        == verify.LOKI_URL_DEFAULT
    )
    monkeypatch.setenv(verify.LOKI_URL_ENV_VAR, "http://loki.env.test:3100")
    assert (
        verify.build_parser().parse_args(["--target-version", "v1"]).loki_url
        == "http://loki.env.test:3100"
    )
    args = verify.build_parser().parse_args(
        ["--target-version", "v1", "--loki-url", "http://flag.test:3100"]
    )
    assert verify.config_from_args(args).loki_url == "http://flag.test:3100"


def test_loki_values_tolerates_malformed_answers() -> None:
    assert verify._loki_values(None) == []
    assert verify._loki_values({"data": {"result": "nope"}}) == []
    assert verify._loki_values({"data": {"result": [{"values": [["1"], "x"]}]}}) == []
    assert verify._loki_values({"data": {"result": ["not-a-stream"]}}) == []
