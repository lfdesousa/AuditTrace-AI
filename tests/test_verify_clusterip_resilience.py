"""Unit tests for the SPEC #443 neuterable guard
(``scripts/network/verify_clusterip_resilience.py``).

Every external effect (``ip route get``, ``kubectl``, the front-door HTTP GET)
is monkeypatched — no real subprocess, no real socket. The point of this file
is to prove the guard is NON-VACUOUS (feedback_vacuous_neuter_test_antipattern):
it must go RED against the pre-fix, default-route-bound evidence (SPEC #443 s2,
the observed 2026-08-09 incident) and GREEN against the fixed, k3s0-bound
evidence (SPEC #443 s3) — both directions asserted explicitly, plus every
individual check's decision boundary.

The module lives outside ``src/`` (a host-networking operator tool, not part
of the shipped package), so it is loaded by file path rather than imported as
a package — same pattern as ``tests/test_adr049_evidence_check.py`` and
``tests/test_llm_stub_server.py``.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "network"
    / "verify_clusterip_resilience.py"
)

# ── real evidence strings, lifted verbatim from SPEC #443 s2 / s3 ─────────────

PRE_FIX_ROUTE_OUTPUT = (
    "10.43.0.1 via 192.168.1.1 dev enxa0cec8afb44d src 192.168.1.231 uid 1000\n"
    "    cache\n"
)
FIXED_ROUTE_OUTPUT = "10.43.0.1 dev k3s0 src 10.10.10.1 uid 1000\n    cache\n"

PRE_FIX_NODE_IP = "192.168.1.231"
FIXED_NODE_IP = "10.10.10.1"

SMOKING_GUN_LOGS = (
    "2026-08-09T10:00:01Z\tcitadelclient failed to sign CSR: dial tcp "
    "10.43.0.1:443: connect: no route to host\n"
    "2026-08-09T10:00:02Z\tKubeJWTAuthenticator: Post "
    '"https://10.43.0.1:443/apis/authentication.k8s.io/v1/tokenreviews": '
    "connect: connection refused\n"
)
CLEAN_LOGS = "2026-08-09T10:00:01Z\tready\n2026-08-09T10:00:02Z\tADS: watch ok\n"


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> Any:
    return subprocess.CompletedProcess(
        args=["probe"], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.fixture(scope="module")
def mod() -> Any:
    module_name = "audittrace_verify_clusterip_resilience"
    spec = importlib.util.spec_from_file_location(module_name, _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: the module's `@dataclass` decorators (used with
    # `from __future__ import annotations`) resolve string annotations via
    # `sys.modules[cls.__module__]` at class-definition time — without this,
    # exec_module raises `AttributeError: 'NoneType' object has no attribute
    # '__dict__'` the instant the first dataclass in the file is defined.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# ── parser: parse_route_get_iface ──────────────────────────────────────────────


def test_parse_route_get_iface_pre_fix_default_route(mod: Any) -> None:
    assert mod.parse_route_get_iface(PRE_FIX_ROUTE_OUTPUT) == "enxa0cec8afb44d"


def test_parse_route_get_iface_fixed_k3s0(mod: Any) -> None:
    assert mod.parse_route_get_iface(FIXED_ROUTE_OUTPUT) == "k3s0"


def test_parse_route_get_iface_unparseable_returns_none(mod: Any) -> None:
    assert mod.parse_route_get_iface("") is None
    assert (
        mod.parse_route_get_iface("RTNETLINK answers: Network is unreachable") is None
    )


# ── parser: count_no_route_lines ───────────────────────────────────────────────


def test_count_no_route_lines_counts_smoking_gun_lines(mod: Any) -> None:
    assert mod.count_no_route_lines(SMOKING_GUN_LOGS) == 2


def test_count_no_route_lines_zero_on_clean_logs(mod: Any) -> None:
    assert mod.count_no_route_lines(CLEAN_LOGS) == 0


def test_count_no_route_lines_dedupes_multi_pattern_line(mod: Any) -> None:
    # One line carrying BOTH "Unauthenticated" and "tokenreviews" counts once.
    line = "citadelclient: tokenreviews call failed: Unauthenticated\n"
    assert mod.count_no_route_lines(line) == 1


# ── decision: check_route_via_iface ────────────────────────────────────────────


def test_check_route_via_iface_fails_on_default_route_bound(mod: Any) -> None:
    result = mod.check_route_via_iface(PRE_FIX_ROUTE_OUTPUT)
    assert result.status == mod.FAIL
    assert result.evidence["iface"] == "enxa0cec8afb44d"


def test_check_route_via_iface_passes_on_k3s0_bound(mod: Any) -> None:
    result = mod.check_route_via_iface(FIXED_ROUTE_OUTPUT)
    assert result.status == mod.PASS
    assert result.evidence["iface"] == "k3s0"


def test_check_route_via_iface_fails_closed_on_unparseable(mod: Any) -> None:
    result = mod.check_route_via_iface("")
    assert result.status == mod.FAIL
    assert "could not parse" in result.detail


# ── decision: check_node_internal_ip ───────────────────────────────────────────


def test_check_node_internal_ip_fails_on_physical_ip(mod: Any) -> None:
    result = mod.check_node_internal_ip(PRE_FIX_NODE_IP)
    assert result.status == mod.FAIL


def test_check_node_internal_ip_passes_on_pinned_ip(mod: Any) -> None:
    result = mod.check_node_internal_ip(FIXED_NODE_IP)
    assert result.status == mod.PASS


def test_check_node_internal_ip_fails_closed_on_missing(mod: Any) -> None:
    result = mod.check_node_internal_ip(None)
    assert result.status == mod.FAIL
    assert "could not read" in result.detail


# ── decision: check_istiod_no_route ────────────────────────────────────────────


def test_check_istiod_no_route_fails_when_unreachable(mod: Any) -> None:
    result = mod.check_istiod_no_route(False, None)
    assert result.status == mod.FAIL
    assert "not reachable" in result.detail


def test_check_istiod_no_route_fails_closed_on_missing_logs(mod: Any) -> None:
    result = mod.check_istiod_no_route(True, None)
    assert result.status == mod.FAIL
    assert "could not be read" in result.detail


def test_check_istiod_no_route_fails_on_smoking_gun(mod: Any) -> None:
    result = mod.check_istiod_no_route(True, SMOKING_GUN_LOGS)
    assert result.status == mod.FAIL
    assert result.evidence["hits"] == 2


def test_check_istiod_no_route_passes_on_clean_logs(mod: Any) -> None:
    result = mod.check_istiod_no_route(True, CLEAN_LOGS)
    assert result.status == mod.PASS
    assert result.evidence["hits"] == 0


# ── decision: check_front_door ─────────────────────────────────────────────────


def test_check_front_door_fails_on_non_200(mod: Any) -> None:
    result = mod.check_front_door(503, b"")
    assert result.status == mod.FAIL


def test_check_front_door_fails_on_bad_body(mod: Any) -> None:
    result = mod.check_front_door(200, b"not json")
    assert result.status == mod.FAIL


def test_check_front_door_fails_on_status_not_ok(mod: Any) -> None:
    result = mod.check_front_door(200, b'{"status": "degraded"}')
    assert result.status == mod.FAIL


def test_check_front_door_passes_on_ok(mod: Any) -> None:
    result = mod.check_front_door(200, b'{"status": "ok", "version": "1.22.0"}')
    assert result.status == mod.PASS


# ── evidence-gathering seams ────────────────────────────────────────────────────


def test_gather_route_output_returns_stdout_on_success(mod: Any, monkeypatch) -> None:
    monkeypatch.setattr(mod, "_run", lambda cmd, **kw: _proc(0, FIXED_ROUTE_OUTPUT))
    assert mod.gather_route_output() == FIXED_ROUTE_OUTPUT


def test_gather_route_output_fails_closed_to_empty_on_error(
    mod: Any, monkeypatch
) -> None:
    monkeypatch.setattr(
        mod, "_run", lambda cmd, **kw: _proc(1, "", "network unreachable")
    )
    assert mod.gather_route_output() == ""


def test_gather_node_internal_ip_returns_stripped_ip(mod: Any, monkeypatch) -> None:
    monkeypatch.setattr(mod, "_run", lambda cmd, **kw: _proc(0, "10.10.10.1\n"))
    assert mod.gather_node_internal_ip() == "10.10.10.1"


def test_gather_node_internal_ip_none_on_kubectl_failure(mod: Any, monkeypatch) -> None:
    monkeypatch.setattr(
        mod, "_run", lambda cmd, **kw: _proc(1, "", "connection refused")
    )
    assert mod.gather_node_internal_ip() is None


def test_gather_node_internal_ip_none_on_empty_stdout(mod: Any, monkeypatch) -> None:
    monkeypatch.setattr(mod, "_run", lambda cmd, **kw: _proc(0, ""))
    assert mod.gather_node_internal_ip() is None


def test_gather_istiod_probe_reachable_with_logs(mod: Any, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> Any:
        calls.append(cmd)
        if "logs" in cmd:
            return _proc(0, CLEAN_LOGS)
        return _proc(0, "Running")

    monkeypatch.setattr(mod, "_run", fake_run)
    reachable, logs = mod.gather_istiod_probe()
    assert reachable is True
    assert logs == CLEAN_LOGS
    assert len(calls) == 2


def test_gather_istiod_probe_unreachable_skips_logs_call(mod: Any, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> Any:
        calls.append(cmd)
        return _proc(0, "Pending")

    monkeypatch.setattr(mod, "_run", fake_run)
    reachable, logs = mod.gather_istiod_probe()
    assert reachable is False
    assert logs is None
    assert len(calls) == 1, "must not spend a kubectl logs call once unreachable"


def test_gather_istiod_probe_reachable_but_logs_unreadable(
    mod: Any, monkeypatch
) -> None:
    def fake_run(cmd: list[str], **kw: Any) -> Any:
        if "logs" in cmd:
            return _proc(1, "", "error")
        return _proc(0, "Running")

    monkeypatch.setattr(mod, "_run", fake_run)
    reachable, logs = mod.gather_istiod_probe()
    assert reachable is True
    assert logs is None


def test_gather_front_door_delegates_to_http_get(mod: Any, monkeypatch) -> None:
    seen = {}

    def fake_get(url: str, *, insecure: bool, timeout: int = 10) -> tuple[int, bytes]:
        seen["url"] = url
        seen["insecure"] = insecure
        return 200, b'{"status": "ok"}'

    monkeypatch.setattr(mod, "_http_get", fake_get)
    status, body = mod.gather_front_door("https://audittrace.local", insecure=True)
    assert status == 200
    assert seen["url"] == "https://audittrace.local/health"
    assert seen["insecure"] is True


# ── the money-shot: run_all() RED (pre-fix) vs GREEN (fixed) ─────────────────


def _patch_pre_fix_state(mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Everything wired to the observed pre-fix, default-route-bound evidence."""

    def fake_run(cmd: list[str], **kw: Any) -> Any:
        if cmd[:3] == ["ip", "route", "get"]:
            return _proc(0, PRE_FIX_ROUTE_OUTPUT)
        if cmd[:3] == ["kubectl", "get", "node"]:
            return _proc(0, PRE_FIX_NODE_IP)
        if cmd[:3] == ["kubectl", "get", "pods"]:
            return _proc(0, "Running")
        if cmd[:2] == ["kubectl", "logs"]:
            return _proc(0, SMOKING_GUN_LOGS)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(mod, "_run", fake_run)
    monkeypatch.setattr(mod, "_http_get", lambda url, **kw: (200, b'{"status": "ok"}'))


def _patch_fixed_state(mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Everything wired to the SPEC #443 s3 fixed, k3s0-bound evidence."""

    def fake_run(cmd: list[str], **kw: Any) -> Any:
        if cmd[:3] == ["ip", "route", "get"]:
            return _proc(0, FIXED_ROUTE_OUTPUT)
        if cmd[:3] == ["kubectl", "get", "node"]:
            return _proc(0, FIXED_NODE_IP)
        if cmd[:3] == ["kubectl", "get", "pods"]:
            return _proc(0, "Running")
        if cmd[:2] == ["kubectl", "logs"]:
            return _proc(0, CLEAN_LOGS)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(mod, "_run", fake_run)
    monkeypatch.setattr(mod, "_http_get", lambda url, **kw: (200, b'{"status": "ok"}'))


def test_run_all_is_red_on_pre_fix_evidence(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_pre_fix_state(mod, monkeypatch)
    report = mod.run_all(front_door="https://audittrace.local", insecure=True)
    assert report.healthy is False
    failing = {c.name for c in report.checks if not c.ok}
    assert failing == {
        "route-via-dummy-iface",
        "node-internal-ip-pinned",
        "istiod-reachable-no-route",
    }


def test_run_all_is_green_on_fixed_evidence(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_fixed_state(mod, monkeypatch)
    report = mod.run_all(front_door="https://audittrace.local", insecure=True)
    assert report.healthy is True
    assert all(c.status == mod.PASS for c in report.checks)


def test_report_as_dict_round_trips_health_and_checks(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_fixed_state(mod, monkeypatch)
    report = mod.run_all(front_door="https://audittrace.local", insecure=True)
    payload = report.as_dict()
    assert payload["healthy"] is True
    assert len(payload["checks"]) == 4
    assert "generated_at" in payload


# ── CLI: main() exit codes + flags ─────────────────────────────────────────────


def test_main_exits_1_on_pre_fix_state(
    mod: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_pre_fix_state(mod, monkeypatch)
    rc = mod.main(["--front-door", "https://audittrace.local", "--insecure"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "UNHEALTHY" in out


def test_main_exits_0_on_fixed_state(
    mod: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_fixed_state(mod, monkeypatch)
    rc = mod.main(["--front-door", "https://audittrace.local", "--insecure", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "RESULT: healthy" in out
    assert '"healthy": true' in out


def test_resolve_front_door_precedence(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(mod.FRONT_DOOR_ENV_VAR, raising=False)
    assert mod._resolve_front_door(None) == mod.DEFAULT_FRONT_DOOR
    assert (
        mod._resolve_front_door("https://explicit.example")
        == "https://explicit.example"
    )
    monkeypatch.setenv(mod.FRONT_DOOR_ENV_VAR, "https://from-env.example")
    assert mod._resolve_front_door(None) == "https://from-env.example"
    assert (
        mod._resolve_front_door("https://explicit.example")
        == "https://explicit.example"
    )


# ── HTTP seam: _http_get transport-failure sentinel ────────────────────────────


def test_http_get_returns_zero_status_on_transport_failure(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_oserror(*args: Any, **kwargs: Any) -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr(mod.urllib.request, "urlopen", raise_oserror)
    status, body = mod._http_get("https://audittrace.local/health", insecure=True)
    assert status == 0
    assert body == b""


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def test_http_get_returns_status_and_body_on_success(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        mod.urllib.request,
        "urlopen",
        lambda *a, **kw: _FakeResponse(200, b'{"status": "ok"}'),
    )
    status, body = mod._http_get("https://audittrace.local/health", insecure=False)
    assert status == 200
    assert body == b'{"status": "ok"}'


def test_http_get_insecure_false_skips_ssl_context(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = {}

    def fake_urlopen(request: Any, timeout: int, context: Any) -> _FakeResponse:
        seen["context"] = context
        return _FakeResponse(200, b"{}")

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    mod._http_get("https://audittrace.local/health", insecure=False)
    assert seen["context"] is None


def test_http_get_maps_http_error_to_status_and_body(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    def raise_http_error(*args: Any, **kwargs: Any) -> Any:
        raise mod.HTTPError(
            "https://audittrace.local/health",
            503,
            "unavailable",
            None,
            io.BytesIO(b"service unavailable"),
        )

    monkeypatch.setattr(mod.urllib.request, "urlopen", raise_http_error)
    status, body = mod._http_get("https://audittrace.local/health", insecure=True)
    assert status == 503
    assert body == b"service unavailable"


def test_run_invokes_real_subprocess(mod: Any) -> None:
    # The sole subprocess seam, exercised for real (no cluster needed) — mirrors
    # scripts/deploy/mesh.py's own `mesh._run(["printf", "hi"])` self-test.
    proc = mod._run(["printf", "hi"])
    assert proc.returncode == 0
    assert proc.stdout == "hi"
