"""Unit tests for the deploy-path mesh-health gate — #384 WS1.

Every external effect (the ``kubectl`` subprocess seam, the heal backoff sleep)
is mocked; no cluster, no network, no real sleeps. The reviewer's mandate is to
prove the gate can FAIL: a degraded/unreadable/timing-out mesh MUST return
UNSAFE, an unconfigured healer MUST fail closed, and a bounded heal that does not
converge MUST NOT loop forever — all asserted here in both directions.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

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
    PrivilegedHealer,
    UnconfiguredHealer,
    assert_safe_to_deploy,
    count_smoking_gun,
    evaluate_istiod_logs,
    evaluate_istiod_readiness,
    evaluate_nodes,
    is_host_resettle_allowed,
)

RESETTLE_SCRIPT = (
    Path(mesh.__file__).resolve().parents[1] / "audittrace-mesh-resettle.sh"
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


# ═══════════════════════════════════════════════════════════════════════════
# WS5 — the PrivilegedHealer (real two-tier heal) + the root-executor whitelist
# ═══════════════════════════════════════════════════════════════════════════


# ── the whitelist pure function (the Python-side boundary mirror) ────────────


def test_is_host_resettle_allowed_whitelisted():
    assert is_host_resettle_allowed("node-not-ready") is True
    assert is_host_resettle_allowed("nodes-unreadable") is True
    assert is_host_resettle_allowed("istiod-api-unreachable") is True


def test_is_host_resettle_allowed_rejects_everything_else():
    # Falsifiable: RBAC-tier signals are NOT host-resettle signals, and near
    # misses / empty / arbitrary strings must all be refused.
    for sig in ("istiod-not-ready", "istiod-no-endpoints", "", "restart-k3s", "node"):
        assert is_host_resettle_allowed(sig) is False


def test_mesh_heal_dir_env_override(monkeypatch):
    monkeypatch.setenv("MESH_HEAL_DIR", "/tmp/custom-heal")
    assert mesh.mesh_heal_dir() == Path("/tmp/custom-heal")


# ── available reflects KUBECTL REACHABILITY, decoupled from the host-root install ─


def test_privileged_healer_available_when_kubectl_reachable(monkeypatch, tmp_path):
    # DECOUPLED (Luis 2026-08-01): available is True when kubectl reaches the
    # cluster REGARDLESS of whether the Option-B host-root dir is wired — so the
    # safe RBAC-tier restart works on any cluster. Here the dir is ABSENT yet
    # available is True.
    monkeypatch.setattr(mesh, "_run", lambda cmd, *, timeout=None: _proc(0, "{}"))
    healer = PrivilegedHealer(heal_dir=tmp_path / "not-wired")
    assert healer.available is True


def test_privileged_healer_unavailable_when_kubectl_unreachable(monkeypatch, tmp_path):
    # kubectl version non-zero (cluster unreachable) → unavailable → the gate
    # fails closed with no heal. The dir being wired does NOT make it available.
    (tmp_path / "requests").mkdir()
    monkeypatch.setattr(
        mesh, "_run", lambda cmd, *, timeout=None: _proc(1, "", "connection refused")
    )
    healer = PrivilegedHealer(heal_dir=tmp_path)
    assert healer.available is False


def test_privileged_healer_unavailable_when_kubectl_missing(monkeypatch, tmp_path):
    def _boom(cmd, *, timeout=None):
        raise FileNotFoundError("kubectl: not found")

    monkeypatch.setattr(mesh, "_run", _boom)
    assert PrivilegedHealer(heal_dir=tmp_path).available is False


# ── heal(): finding → action routing ─────────────────────────────────────────


def _rbac_ok_healer(tmp_path):
    return PrivilegedHealer(heal_dir=tmp_path, resettle_timeout=1.0, resettle_poll=0.0)


def test_heal_rbac_finding_restarts_istiod(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, *, timeout=None):
        calls.append(cmd)
        return _proc(0, "rolled")

    monkeypatch.setattr(mesh, "_run", fake_run)
    healer = _rbac_ok_healer(tmp_path)
    diag = Diagnosis(findings=[Finding("istiod-not-ready", "readyReplicas=0")])
    attempt = healer.heal(diag)
    assert attempt.performed is True
    assert attempt.action == "restart-istiod"
    # the least-privileged action is an in-cluster rollout restart of istiod
    assert any("rollout" in c and "restart" in c for c in calls)
    assert all("kubectl" == c[0] for c in calls)


def test_heal_rbac_restart_failure_fails_closed(monkeypatch, tmp_path):
    # kubectl rollout restart returns non-zero → performed=False, NEVER raises.
    monkeypatch.setattr(mesh, "_run", lambda cmd, *, timeout=None: _proc(1, "", "boom"))
    healer = _rbac_ok_healer(tmp_path)
    attempt = healer.heal(Diagnosis(findings=[Finding("istiod-not-ready", "x")]))
    assert attempt.performed is False
    assert attempt.action == "restart-istiod"


def test_heal_rbac_kubectl_missing_fails_closed(monkeypatch, tmp_path):
    # kubectl not on PATH (OSError) is "couldn't fix", not a crash.
    def _boom(cmd, *, timeout=None):
        raise FileNotFoundError("kubectl: not found")

    monkeypatch.setattr(mesh, "_run", _boom)
    healer = _rbac_ok_healer(tmp_path)
    attempt = healer.heal(Diagnosis(findings=[Finding("istiod-no-endpoints", "x")]))
    assert attempt.performed is False


def test_heal_host_root_finding_triggers_resettle(monkeypatch, tmp_path):
    seen = {}

    def fake_trigger(finding):
        seen["signal"] = finding.signal
        return HealAttempt(True, "host-resettle", "faked")

    healer = _rbac_ok_healer(tmp_path)
    monkeypatch.setattr(healer, "_trigger_host_resettle", fake_trigger)
    attempt = healer.heal(Diagnosis(findings=[Finding("node-not-ready", "kubelet")]))
    assert attempt.performed is True
    assert attempt.action == "host-resettle"
    assert seen["signal"] == "node-not-ready"


def test_heal_prefers_rbac_over_host_root(monkeypatch, tmp_path):
    # least-privilege first: with BOTH an RBAC and a host-root finding present,
    # heal() does the RBAC action and does NOT cross the privilege boundary.
    monkeypatch.setattr(mesh, "_run", lambda cmd, *, timeout=None: _proc(0, "ok"))
    healer = _rbac_ok_healer(tmp_path)

    def _explode(finding):  # the host-root seam must not be reached
        raise AssertionError("host-root seam reached despite an RBAC finding")

    monkeypatch.setattr(healer, "_trigger_host_resettle", _explode)
    diag = Diagnosis(
        findings=[
            Finding("node-not-ready", "kubelet"),
            Finding("istiod-not-ready", "x"),
        ]
    )
    attempt = healer.heal(diag)
    assert attempt.action == "restart-istiod"


def test_heal_single_op_per_call(monkeypatch, tmp_path):
    # idempotency/boundedness: one heal() call performs exactly ONE action even
    # with several RBAC findings — the gate owns the retry loop.
    restarts = []
    healer = _rbac_ok_healer(tmp_path)
    monkeypatch.setattr(
        healer,
        "_restart_istiod",
        lambda f: restarts.append(f.signal) or HealAttempt(True, "restart-istiod", "x"),
    )
    diag = Diagnosis(
        findings=[Finding("istiod-not-ready", "a"), Finding("istiod-no-endpoints", "b")]
    )
    healer.heal(diag)
    assert len(restarts) == 1


def test_heal_no_actionable_finding_is_noop_fail_closed(tmp_path):
    healer = _rbac_ok_healer(tmp_path)
    # a signal no tier maps to → performed=False so the gate fails closed.
    attempt = healer.heal(Diagnosis(findings=[Finding("istiod-logs-unreadable", "x")]))
    assert attempt.performed is False
    assert attempt.action == "none"


# ── _trigger_host_resettle(): the Option-B request/result/poll boundary ──────


def test_trigger_host_resettle_refuses_non_whitelisted(tmp_path):
    healer = _rbac_ok_healer(tmp_path)
    attempt = healer._trigger_host_resettle(Finding("istiod-not-ready", "x"))
    assert attempt.performed is False
    assert attempt.action == "host-resettle-refused"
    # refusal writes NO request file across the privilege boundary.
    assert not (tmp_path / "requests").exists() or not list(
        (tmp_path / "requests").glob("*.json")
    )


def test_trigger_host_resettle_not_installed_is_noop(tmp_path):
    # host-root fault but the Option-B dir is NOT wired: self-gate → no-op,
    # NO request file written, NO raise. (available may be True via kubectl, but
    # the host-root tier stays fail-closed until the units are installed.)
    healer = PrivilegedHealer(heal_dir=tmp_path / "not-wired")
    attempt = healer._trigger_host_resettle(Finding("node-not-ready", "kubelet"))
    assert attempt.performed is False
    assert attempt.action == "none"
    assert "not installed on this host" in attempt.detail
    assert not (tmp_path / "not-wired" / "requests").exists()


def test_trigger_host_resettle_writes_validated_request(monkeypatch, tmp_path):
    (tmp_path / "requests").mkdir()  # host-root install wired
    healer = PrivilegedHealer(
        heal_dir=tmp_path, resettle_timeout=5.0, resettle_poll=1.0
    )

    # simulate the root executor firing during the poll's sleep: write a result
    # for whatever request appears.
    def fake_sleep(_seconds):
        for req in (tmp_path / "requests").glob("*.json"):
            body = json.loads(req.read_text())
            # the request must carry the signal + evidence for the root unit.
            assert body["signal"] == "node-not-ready"
            assert body["evidence"] == {"ready_statuses": ["False"]}
            (tmp_path / "results" / f"{req.stem}.result.json").write_text(
                json.dumps({"performed": True, "status": "performed"})
            )

    monkeypatch.setattr(mesh, "_sleep", fake_sleep)
    finding = Finding("node-not-ready", "1 NotReady", {"ready_statuses": ["False"]})
    attempt = healer._trigger_host_resettle(finding)
    assert attempt.performed is True
    assert attempt.action == "host-resettle"


def test_trigger_host_resettle_result_not_performed(monkeypatch, tmp_path):
    (tmp_path / "requests").mkdir()  # host-root install wired
    healer = PrivilegedHealer(
        heal_dir=tmp_path, resettle_timeout=5.0, resettle_poll=1.0
    )

    def fake_sleep(_seconds):
        for req in (tmp_path / "requests").glob("*.json"):
            (tmp_path / "results" / f"{req.stem}.result.json").write_text(
                json.dumps({"performed": False, "status": "refused"})
            )

    monkeypatch.setattr(mesh, "_sleep", fake_sleep)
    attempt = healer._trigger_host_resettle(Finding("nodes-unreadable", "empty"))
    assert attempt.performed is False
    assert attempt.action == "host-resettle"


def test_trigger_host_resettle_times_out(monkeypatch, tmp_path):
    # no result file ever appears → bounded timeout, performed=False, no raise.
    (tmp_path / "requests").mkdir()  # host-root install wired
    monkeypatch.setattr(mesh, "_sleep", lambda s: None)
    healer = PrivilegedHealer(
        heal_dir=tmp_path, resettle_timeout=0.05, resettle_poll=0.02
    )
    attempt = healer._trigger_host_resettle(Finding("node-not-ready", "kubelet"))
    assert attempt.performed is False
    assert attempt.action == "host-resettle-timeout"


# ── the healer satisfies the gate contract (non-vacuous, fail-closed) ────────


def test_gate_with_privileged_healer_unavailable_fails_closed(monkeypatch, tmp_path):
    # kubectl unreachable → healer.available False → UNSAFE, no heal attempts.
    monkeypatch.setattr(
        mesh, "_run", lambda cmd, *, timeout=None: _proc(1, "", "connection refused")
    )
    gate = MeshGate(healer=PrivilegedHealer(heal_dir=tmp_path))
    result = gate.evaluate()
    assert result.outcome == UNSAFE
    assert result.heal_attempts == []


def test_gate_rbac_heals_without_host_root_install(monkeypatch, tmp_path):
    # DECOUPLING PROOF #1: an istiod (RBAC-tier) fault heals via the in-cluster
    # rollout restart even though the Option-B host-root dir is NOT installed.
    monkeypatch.setattr(mesh, "_sleep", lambda s: None)
    monkeypatch.setattr(mesh, "_run", lambda cmd, *, timeout=None: _proc(0, "ok"))
    gate = MeshGate(
        MeshGateConfig(max_heal_attempts=2),
        healer=PrivilegedHealer(heal_dir=tmp_path / "not-wired"),
    )
    seq = iter(
        [Diagnosis(findings=[Finding("istiod-not-ready", "x")]), Diagnosis(findings=[])]
    )
    last: dict[str, Diagnosis] = {}

    def diag() -> Diagnosis:
        try:
            last["d"] = next(seq)
        except StopIteration:
            pass
        return last["d"]

    gate.diagnose_mesh = diag  # type: ignore[method-assign]
    result = gate.evaluate()
    assert result.outcome == HEALED
    assert result.heal_attempts[0].action == "restart-istiod"


def test_gate_host_root_fails_closed_without_install(monkeypatch, tmp_path):
    # DECOUPLING PROOF #2: a node (host-root) fault with the Option-B dir ABSENT
    # → available True (kubectl reachable) so heal IS attempted, but the host-root
    # tier self-gates (not installed) → no-op → still degraded → UNSAFE.
    monkeypatch.setattr(mesh, "_sleep", lambda s: None)
    monkeypatch.setattr(mesh, "_run", lambda cmd, *, timeout=None: _proc(0, "ok"))
    gate = MeshGate(
        MeshGateConfig(max_heal_attempts=2),
        healer=PrivilegedHealer(heal_dir=tmp_path / "not-wired"),
    )
    gate.diagnose_mesh = lambda: Diagnosis(  # type: ignore[method-assign]
        findings=[Finding("node-not-ready", "kubelet")]
    )
    result = gate.evaluate()
    assert result.outcome == UNSAFE
    assert len(result.heal_attempts) >= 1
    assert result.heal_attempts[0].action == "none"
    assert "not installed" in result.heal_attempts[0].detail


# ── the root executor script: the REAL whitelist boundary (dry-run) ──────────


def _run_executor(heal_dir: Path, signal: str | None, *, raw: str | None = None):
    """Run the real root executor in dry-run against a crafted request file."""
    (heal_dir / "requests").mkdir(parents=True, exist_ok=True)
    (heal_dir / "results").mkdir(parents=True, exist_ok=True)
    req = heal_dir / "requests" / "req-test-0001.json"
    if raw is not None:
        req.write_text(raw)
    else:
        req.write_text(json.dumps({"request_id": "req-test-0001", "signal": signal}))
    proc = subprocess.run(
        ["bash", str(RESETTLE_SCRIPT)],
        env={
            "MESH_HEAL_DIR": str(heal_dir),
            "MESH_RESETTLE_DRY_RUN": "1",
            "PATH": "/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    result_path = heal_dir / "results" / "req-test-0001.result.json"
    result = json.loads(result_path.read_text()) if result_path.exists() else None
    return proc, result


@pytest.mark.parametrize(
    "signal", ["node-not-ready", "nodes-unreadable", "istiod-api-unreachable"]
)
def test_executor_dry_run_performs_for_whitelisted_signal(tmp_path, signal):
    proc, result = _run_executor(tmp_path, signal)
    assert proc.returncode == 0, proc.stderr
    assert result is not None
    # dry-run selects the bounded op but does NOT execute it.
    assert result["status"] == "dry-run"
    assert result["performed"] is False
    assert result["would_run"] == "systemctl restart k3s"
    # request consumed.
    assert not (tmp_path / "requests" / "req-test-0001.json").exists()


@pytest.mark.parametrize("signal", ["istiod-not-ready", "restart-k3s", "wipe-disk", ""])
def test_executor_refuses_non_whitelisted_signal(tmp_path, signal):
    # THE falsifiable security test: a signal outside the whitelist must be
    # refused and perform NOTHING — no op selected, no would_run.
    proc, result = _run_executor(tmp_path, signal)
    assert proc.returncode == 0, proc.stderr
    assert result is not None
    assert result["status"] == "refused"
    assert result["performed"] is False
    assert "would_run" not in result
    assert not (tmp_path / "requests" / "req-test-0001.json").exists()


def test_executor_refuses_unparseable_request(tmp_path):
    proc, result = _run_executor(tmp_path, None, raw="{ not json")
    assert proc.returncode == 0, proc.stderr
    assert result["status"] == "refused"
    assert result["performed"] is False


def test_executor_noop_when_no_requests(tmp_path):
    (tmp_path / "requests").mkdir(parents=True)
    proc = subprocess.run(
        ["bash", str(RESETTLE_SCRIPT)],
        env={"MESH_HEAL_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "no pending requests" in proc.stderr
