"""Unit tests for the deploy-path mesh-health gate — #384 WS1.

Every external effect (the ``kubectl`` subprocess seam, the heal backoff sleep)
is mocked; no cluster, no network, no real sleeps. The reviewer's mandate is to
prove the gate can FAIL: a degraded/unreadable/timing-out mesh MUST return
UNSAFE, an unconfigured healer MUST fail closed, and a bounded heal that does not
converge MUST NOT loop forever — all asserted here in both directions.
"""

from __future__ import annotations

import subprocess

import pytest

from scripts.deploy import mesh
from scripts.deploy.mesh import (
    HEALED,
    HEALTHY,
    UNSAFE,
    Diagnosis,
    Finding,
    HealAttempt,
    MeshGate,
    MeshGateConfig,
    MeshGateResult,
    MeshUnsafeError,
    UnconfiguredHealer,
    assert_safe_to_deploy,
    count_smoking_gun,
    evaluate_istiod_logs,
    evaluate_istiod_readiness,
    evaluate_nodes,
)


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["kubectl"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ── fake healers (the injected privilege seam) ───────────────────────────────


class _FakeHealer:
    """A healer that flips the gate's diagnosis to healthy after ``heal_on`` calls.

    Drives the auto-heal path deterministically by mutating a shared gate handle:
    after ``heal_on`` attempts it replaces the gate's diagnose with a healthy one.
    """

    available = True

    def __init__(self, gate, heal_on=1):
        self.gate = gate
        self.heal_on = heal_on
        self.calls = 0

    def heal(self, diagnosis):
        self.calls += 1
        if self.calls >= self.heal_on:
            self.gate.diagnose_mesh = lambda: Diagnosis(findings=[])
        return HealAttempt(True, "restart-istiod", f"attempt {self.calls}")


class _NeverHealer:
    available = True

    def __init__(self):
        self.calls = 0

    def heal(self, diagnosis):
        self.calls += 1
        return HealAttempt(True, "restart-istiod", f"attempt {self.calls} (no effect)")


class _RaisingHealer:
    available = True

    def heal(self, diagnosis):
        raise RuntimeError("privileged recovery blew up")


# ── pure diagnosers: nodes ────────────────────────────────────────────────────


def test_evaluate_nodes_all_ready():
    assert evaluate_nodes("True True True") == []


def test_evaluate_nodes_flags_not_ready():
    findings = evaluate_nodes("True False")
    assert len(findings) == 1
    assert findings[0].signal == "node-not-ready"
    assert "1 of 2" in findings[0].detail


def test_evaluate_nodes_empty_is_unreadable():
    findings = evaluate_nodes("")
    assert findings[0].signal == "nodes-unreadable"


# ── pure diagnosers: istiod logs (the #307 smoking gun) ──────────────────────


def test_count_smoking_gun_counts_lines_not_matches():
    logs = "\n".join(
        [
            "connection ok",
            "dial tcp 10.43.0.1:443: connect: no route to host",
            "KubeJWTAuthenticator ... tokenreviews Unauthenticated",  # 2 patterns, 1 line
            "healthy",
        ]
    )
    assert count_smoking_gun(logs) == 2


def test_evaluate_istiod_logs_clean():
    assert evaluate_istiod_logs("all good\nnothing to see", "3m") == []


def test_evaluate_istiod_logs_flags_fault():
    findings = evaluate_istiod_logs("x\nno route to host\n", "3m")
    assert len(findings) == 1
    assert findings[0].signal == "istiod-api-unreachable"
    assert findings[0].evidence["match_count"] == 1
    assert findings[0].evidence["window"] == "3m"


# ── pure diagnosers: istiod readiness ────────────────────────────────────────


def _dep(ready, desired):
    d = {"spec": {}, "status": {}}
    if desired is not None:
        d["spec"]["replicas"] = desired
    if ready is not None:
        d["status"]["readyReplicas"] = ready
    return d


def test_evaluate_istiod_readiness_healthy():
    assert evaluate_istiod_readiness(_dep(1, 1), "10.0.0.1") == []


def test_evaluate_istiod_readiness_none_deployment():
    findings = evaluate_istiod_readiness(None, "10.0.0.1")
    assert findings[0].signal == "istiod-unreadable"


def test_evaluate_istiod_readiness_non_dict_deployment():
    findings = evaluate_istiod_readiness(["not", "a", "dict"], "10.0.0.1")
    assert findings[0].signal == "istiod-unreadable"


def test_evaluate_istiod_readiness_desired_unreadable():
    findings = evaluate_istiod_readiness(_dep(1, None), "10.0.0.1")
    assert findings[0].signal == "istiod-unreadable"


def test_evaluate_istiod_readiness_not_ready():
    # readyReplicas absent (k8s omits it at 0) → treated as 0 < desired.
    findings = evaluate_istiod_readiness(_dep(None, 1), "10.0.0.1")
    assert [f.signal for f in findings] == ["istiod-not-ready"]


def test_evaluate_istiod_readiness_zero_desired_is_degraded():
    findings = evaluate_istiod_readiness(_dep(0, 0), "10.0.0.1")
    assert [f.signal for f in findings] == ["istiod-not-ready"]


def test_evaluate_istiod_readiness_no_endpoints():
    findings = evaluate_istiod_readiness(_dep(1, 1), "")
    assert [f.signal for f in findings] == ["istiod-no-endpoints"]


def test_evaluate_istiod_readiness_ready_non_int_treated_as_zero():
    findings = evaluate_istiod_readiness(_dep("notanint", 1), "10.0.0.1")
    assert [f.signal for f in findings] == ["istiod-not-ready"]


def test_evaluate_istiod_readiness_null_spec_fails_closed():
    # A present-but-NULL spec must degrade to a fail-closed finding, NOT crash
    # with AttributeError (which would escape run() and emit no report).
    findings = evaluate_istiod_readiness({"spec": None, "status": {}}, "10.0.0.1")
    assert [f.signal for f in findings] == ["istiod-unreadable"]


def test_evaluate_istiod_readiness_null_status_fails_closed():
    # A present-but-NULL status: spec still readable, status treated as 0 ready.
    findings = evaluate_istiod_readiness(
        {"spec": {"replicas": 1}, "status": None}, "10.0.0.1"
    )
    assert [f.signal for f in findings] == ["istiod-not-ready"]


# ── diagnose_mesh: the gathering layer (mocked kubectl seam) ─────────────────


class _Router:
    """Routes mesh._run calls to canned results (or exceptions) by token."""

    def __init__(self, rules, default=None):
        self.rules = rules  # list of (needle, result_or_exc)
        self.default = default if default is not None else _proc(0, "")

    def __call__(self, cmd, *, timeout=None):
        joined = " ".join(cmd)
        for needle, result in self.rules:
            if needle in joined:
                if isinstance(result, Exception):
                    raise result
                return result
        if isinstance(self.default, Exception):
            raise self.default
        return self.default


def _healthy_cluster_router():
    return _Router(
        rules=[
            ("get nodes", _proc(0, "True")),
            ("logs", _proc(0, "all quiet")),
            (
                "get deployment istiod",
                _proc(0, '{"spec":{"replicas":1},"status":{"readyReplicas":1}}'),
            ),
            ("get endpoints", _proc(0, "10.42.0.5")),
        ]
    )


def test_diagnose_mesh_healthy(monkeypatch):
    monkeypatch.setattr(mesh, "_run", _healthy_cluster_router())
    diag = MeshGate().diagnose_mesh()
    assert diag.healthy is True
    assert diag.signals == []


def test_diagnose_mesh_flags_smoking_gun(monkeypatch):
    router = _healthy_cluster_router()
    router.rules = [
        (n, r) if n != "logs" else ("logs", _proc(0, "no route to host"))
        for n, r in router.rules
    ]
    monkeypatch.setattr(mesh, "_run", router)
    diag = MeshGate().diagnose_mesh()
    assert "istiod-api-unreachable" in diag.signals


def test_diagnose_mesh_nonzero_exit_is_finding(monkeypatch):
    # kubectl get nodes returns non-zero → fail-closed nodes-unreadable finding.
    router = _Router(
        rules=[("get nodes", _proc(1, "", "the connection was refused"))],
        default=_proc(0, ""),
    )
    monkeypatch.setattr(mesh, "_run", router)
    diag = MeshGate().diagnose_mesh()
    assert "nodes-unreadable" in diag.signals


def test_diagnose_mesh_logs_unreadable(monkeypatch):
    router = _healthy_cluster_router()
    router.rules = [
        (n, r) if n != "logs" else ("logs", _proc(1, "", "no such pod"))
        for n, r in router.rules
    ]
    monkeypatch.setattr(mesh, "_run", router)
    diag = MeshGate().diagnose_mesh()
    assert "istiod-logs-unreadable" in diag.signals


def test_diagnose_mesh_istiod_unreadable_when_deployment_fails(monkeypatch):
    router = _healthy_cluster_router()
    router.rules = [
        (n, r)
        if n != "get deployment istiod"
        else ("get deployment istiod", _proc(1, "", "not found"))
        for n, r in router.rules
    ]
    monkeypatch.setattr(mesh, "_run", router)
    diag = MeshGate().diagnose_mesh()
    assert "istiod-unreadable" in diag.signals


def test_diagnose_mesh_istiod_unreadable_when_endpoints_fail(monkeypatch):
    router = _healthy_cluster_router()
    router.rules = [
        (n, r) if n != "get endpoints" else ("get endpoints", _proc(1, "", "not found"))
        for n, r in router.rules
    ]
    monkeypatch.setattr(mesh, "_run", router)
    diag = MeshGate().diagnose_mesh()
    assert "istiod-unreadable" in diag.signals


def test_probe_fail_closed_on_missing_binary(monkeypatch):
    # kubectl not on PATH → OSError → fail-closed, NEVER an uncaught crash.
    def _boom(cmd, *, timeout=None):
        raise FileNotFoundError("kubectl: command not found")

    monkeypatch.setattr(mesh, "_run", _boom)
    diag = MeshGate().diagnose_mesh()
    assert diag.healthy is False
    # every probe failed closed → multiple unreadable findings, no exception.
    assert "nodes-unreadable" in diag.signals


def test_probe_fail_closed_on_timeout(monkeypatch):
    # A hanging kubectl (the wedged-ClusterIP symptom) must time out into a
    # finding, not block. TimeoutExpired is a subprocess.SubprocessError.
    def _timeout(cmd, *, timeout=None):
        raise subprocess.TimeoutExpired(cmd, timeout or 30)

    monkeypatch.setattr(mesh, "_run", _timeout)
    diag = MeshGate().diagnose_mesh()
    assert diag.healthy is False
    assert "nodes-unreadable" in diag.signals


# ── evaluate(): the fail-closed gate ─────────────────────────────────────────


def test_evaluate_healthy_proceeds(monkeypatch):
    monkeypatch.setattr(mesh, "_run", _healthy_cluster_router())
    result = MeshGate().evaluate()
    assert result.outcome == HEALTHY
    assert result.safe is True
    assert result.heal_attempts == []


def test_evaluate_degraded_unconfigured_healer_fails_closed(monkeypatch):
    monkeypatch.setattr(mesh, "_run", _Router(rules=[("get nodes", _proc(0, "False"))]))
    result = MeshGate().evaluate()  # default UnconfiguredHealer
    assert result.outcome == UNSAFE
    assert result.safe is False
    assert "not configured" in result.reason
    assert result.heal_attempts == []


def test_evaluate_auto_heals(monkeypatch):
    monkeypatch.setattr(mesh, "_sleep", lambda s: None)
    gate = MeshGate(MeshGateConfig(max_heal_attempts=2))
    gate.diagnose_mesh = lambda: Diagnosis(
        findings=[Finding("istiod-api-unreachable", "x")]
    )
    healer = _FakeHealer(gate, heal_on=1)
    gate.healer = healer
    result = gate.evaluate()
    assert result.outcome == HEALED
    assert result.safe is True
    assert healer.calls == 1
    assert len(result.heal_attempts) == 1


def test_evaluate_heal_is_bounded_then_unsafe(monkeypatch):
    monkeypatch.setattr(mesh, "_sleep", lambda s: None)
    gate = MeshGate(MeshGateConfig(max_heal_attempts=3))
    gate.diagnose_mesh = lambda: Diagnosis(
        findings=[Finding("istiod-api-unreachable", "x")]
    )
    healer = _NeverHealer()
    gate.healer = healer
    result = gate.evaluate()
    assert result.outcome == UNSAFE
    assert result.safe is False
    # bounded: the healer is called at most max_heal_attempts times, never more.
    assert healer.calls == 3
    assert len(result.heal_attempts) == 3


def test_evaluate_healer_exception_fails_closed(monkeypatch):
    monkeypatch.setattr(mesh, "_sleep", lambda s: None)
    gate = MeshGate(MeshGateConfig(max_heal_attempts=2))
    gate.diagnose_mesh = lambda: Diagnosis(
        findings=[Finding("istiod-api-unreachable", "x")]
    )
    gate.healer = _RaisingHealer()
    result = gate.evaluate()  # must NOT raise
    assert result.outcome == UNSAFE
    assert result.heal_attempts[-1].action == "error"
    assert "blew up" in result.heal_attempts[-1].detail


# ── assert_safe_to_deploy convenience ────────────────────────────────────────


def test_assert_safe_to_deploy_returns_on_safe(monkeypatch):
    monkeypatch.setattr(mesh, "_run", _healthy_cluster_router())
    result = assert_safe_to_deploy(MeshGate())
    assert result.safe is True


def test_assert_safe_to_deploy_raises_on_unsafe(monkeypatch):
    monkeypatch.setattr(mesh, "_run", _Router(rules=[("get nodes", _proc(0, "False"))]))
    with pytest.raises(MeshUnsafeError) as exc:
        assert_safe_to_deploy(MeshGate())
    assert exc.value.result.outcome == UNSAFE


# ── records / serialisation / default reasons ────────────────────────────────


def test_unconfigured_healer_reports_unavailable():
    healer = UnconfiguredHealer()
    assert healer.available is False
    attempt = healer.heal(Diagnosis(findings=[]))
    assert attempt.performed is False
    assert attempt.action == "none"


def test_result_default_reasons():
    diag = Diagnosis(findings=[Finding("istiod-api-unreachable", "x")])
    healthy = MeshGateResult(HEALTHY, Diagnosis(findings=[]))
    healed = MeshGateResult(HEALED, diag)
    unsafe = MeshGateResult(UNSAFE, diag)
    assert "safe to proceed" in healthy.reason
    assert "auto-healed" in healed.reason
    assert "failing closed" in unsafe.reason


def test_result_explicit_reason_is_kept():
    r = MeshGateResult(UNSAFE, Diagnosis(findings=[]), reason="custom")
    assert r.reason == "custom"


def test_result_as_dict_round_trips():
    diag = Diagnosis(findings=[Finding("istiod-api-unreachable", "x", {"k": "v"})])
    attempt = HealAttempt(True, "a", "d", {"e": 1})
    r = MeshGateResult(HEALED, diag, [attempt], Diagnosis(findings=[]))
    d = r.as_dict()
    assert d["outcome"] == HEALED
    assert d["safe"] is True
    assert d["initial_diagnosis"]["findings"][0]["evidence"] == {"k": "v"}
    assert d["heal_attempts"][0]["performed"] is True
    assert d["final_diagnosis"]["healthy"] is True


def test_result_as_dict_none_final():
    r = MeshGateResult(UNSAFE, Diagnosis(findings=[]), final_diagnosis=None)
    assert r.as_dict()["final_diagnosis"] is None


def test_diagnosis_and_finding_as_dict():
    diag = Diagnosis(findings=[Finding("s", "d", {"x": 1})])
    d = diag.as_dict()
    assert d["healthy"] is False
    assert d["findings"][0] == {"signal": "s", "detail": "d", "evidence": {"x": 1}}


# ── low-level indirections (exercised once for coverage honesty) ─────────────


def test_run_executes_real_subprocess():
    proc = mesh._run(["printf", "hi"])
    assert proc.returncode == 0 and proc.stdout == "hi"


def test_sleep_and_now_iso():
    mesh._sleep(0.0)
    assert "T" in mesh._now_iso()


def test_parse_json_bad_payload():
    assert mesh._parse_json("{not json") is None
