"""Unit tests for the deploy-path mesh-health gate — #384 WS1.

Every external effect (the ``kubectl`` subprocess seam, the heal backoff sleep)
is mocked; no cluster, no network, no real sleeps. The reviewer's mandate is to
prove the gate can FAIL: a degraded/unreadable/timing-out mesh MUST return
UNSAFE, an unconfigured healer MUST fail closed, and a bounded heal that does not
converge MUST NOT loop forever — all asserted here in both directions.
"""

from __future__ import annotations

import json
import logging
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

SCRIPTS_DIR = Path(mesh.__file__).resolve().parents[1]
RESETTLE_SCRIPT = SCRIPTS_DIR / "audittrace-mesh-resettle.sh"
PATH_UNIT = SCRIPTS_DIR / "audittrace-mesh-healer.path"
SERVICE_UNIT = SCRIPTS_DIR / "audittrace-mesh-healer.service"
TMPFILES_RULE = SCRIPTS_DIR / "audittrace-mesh-heal.tmpfiles.conf"


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


def test_heal_prefers_host_root_over_rbac_when_co_present(monkeypatch, tmp_path):
    # WS5-HEALER-ESC-20260802: with BOTH a host-root and an RBAC finding
    # present (the real #307 F2 shape), heal() escalates straight to the
    # host-root action and does NOT touch the RBAC (kubectl) tier at all — an
    # RBAC rollout restart cannot fix a broken node route, so trying it first
    # would waste a bounded heal attempt on a tier that can never converge.
    healer = _rbac_ok_healer(tmp_path)

    def _explode(cmd, *, timeout=None):  # the RBAC (kubectl) tier must not run
        raise AssertionError("RBAC/kubectl tier reached despite a host-root finding")

    monkeypatch.setattr(mesh, "_run", _explode)
    seen = {}

    def fake_trigger(finding):
        seen["signal"] = finding.signal
        return HealAttempt(True, "host-resettle", "faked")

    monkeypatch.setattr(healer, "_trigger_host_resettle", fake_trigger)
    diag = Diagnosis(
        findings=[
            Finding("istiod-not-ready", "x"),
            Finding("istiod-api-unreachable", "no route to host"),
        ]
    )
    attempt = healer.heal(diag)
    assert attempt.action == "host-resettle"
    assert seen["signal"] == "istiod-api-unreachable"


def test_heal_no_fallthrough_when_host_root_genuinely_fires_even_if_not_performed(
    monkeypatch, tmp_path
):
    # WS5-HEALER-ESC-20260802, reviewer REJECT (2026-08-02, test-falsifiability
    # not a code bug): pins the exact distinction `heal()` uses to decide
    # fall-through -- `attempt.action != "none"`, NOT `attempt.performed`. A
    # host-root attempt that genuinely FIRED (timed out here) but did not
    # complete (`performed=False`) must NOT fall through to RBAC, even though
    # `performed` is False just like the self-gated "not installed" case.
    # Every pre-existing test either had action=="none" co-occurring with
    # performed=False, or action!="none" co-occurring with performed=True --
    # so neutering the guard to `if attempt.performed:` left the whole suite
    # green. This test would go RED under that neutered guard (see the
    # falsifiability self-check in the WS5 rework commit message).
    healer = _rbac_ok_healer(tmp_path)

    def _explode(cmd, *, timeout=None):  # the RBAC (kubectl) tier must not run
        raise AssertionError(
            "RBAC/kubectl tier reached despite a genuinely-fired host-root attempt"
        )

    monkeypatch.setattr(mesh, "_run", _explode)

    def fake_trigger(finding):
        # genuinely fired (timed out waiting on the root unit), NOT performed,
        # and NOT the "none" self-gate action.
        return HealAttempt(
            performed=False,
            action="host-resettle-timeout",
            detail="no result from the root re-settle unit within 1s",
        )

    monkeypatch.setattr(healer, "_trigger_host_resettle", fake_trigger)
    diag = Diagnosis(
        findings=[
            Finding("istiod-not-ready", "x"),  # co-occurring RBAC-tier signal
            Finding("istiod-api-unreachable", "no route to host"),
        ]
    )
    attempt = healer.heal(diag)
    # host-root owns the call: its own (non-performed) attempt is returned
    # unchanged, not superseded by an RBAC fallback.
    assert attempt.performed is False
    assert attempt.action == "host-resettle-timeout"
    assert not (tmp_path / "requests").exists()


def test_heal_falls_through_to_rbac_when_host_root_not_installed_and_co_present(
    monkeypatch, tmp_path
):
    # WS5-HEALER-ESC-20260802, reviewer REJECT item 4 (fixed): the missing
    # 4th cell of the 2x2. Co-occurring signals {istiod-api-unreachable,
    # istiod-not-ready} used to hit the host-root self-gate (Option-B NOT
    # installed) and return a silent no-op WITHOUT ever trying the RBAC
    # tier -- regressing the class-level `available` guarantee ("RBAC-tier
    # heal available on ANY reachable cluster, even before the units are
    # installed"). heal() must now fall through to a best-effort RBAC
    # rollout in this exact cell instead of a bare no-op.
    calls = []

    def fake_run(cmd, *, timeout=None):
        calls.append(cmd)
        return _proc(0, "rolled")

    monkeypatch.setattr(mesh, "_run", fake_run)
    healer = _rbac_ok_healer(tmp_path)  # heal_dir has no requests/ -> not installed
    diag = Diagnosis(
        findings=[
            Finding("istiod-not-ready", "readyReplicas=0"),
            Finding("istiod-api-unreachable", "no route to host"),
        ]
    )
    attempt = healer.heal(diag)
    assert attempt.performed is True
    assert attempt.action == "restart-istiod"
    # the fall-through is RBAC-only -- the host-root self-gate must NOT have
    # written a request file across the privilege boundary in this call.
    assert not (tmp_path / "requests").exists()
    assert any("rollout" in c and "restart" in c for c in calls)


def test_heal_host_root_only_not_installed_stays_noop_no_rbac_fallback(tmp_path):
    # Negative control for the fall-through: a host-root-ONLY diagnosis (no
    # RBAC-tier signal anywhere) on an uninstalled host has nothing to fall
    # back to, so heal() must return the original not-installed no-op
    # unchanged -- not synthesize an action that never ran.
    healer = _rbac_ok_healer(tmp_path)  # heal_dir has no requests/ -> not installed
    attempt = healer.heal(Diagnosis(findings=[Finding("node-not-ready", "kubelet")]))
    assert attempt.performed is False
    assert attempt.action == "none"
    assert "not installed on this host" in attempt.detail
    assert not (tmp_path / "requests").exists()


def test_heal_rbac_only_when_no_host_root_signal_present(monkeypatch, tmp_path):
    # F1 shape: an RBAC-tier signal with NO host-root signal anywhere in the
    # diagnosis still takes the RBAC action (least-privileged fix available).
    calls = []

    def fake_run(cmd, *, timeout=None):
        calls.append(cmd)
        return _proc(0, "rolled")

    monkeypatch.setattr(mesh, "_run", fake_run)
    healer = _rbac_ok_healer(tmp_path)

    def _explode(finding):  # the host-root seam must not be reached
        raise AssertionError("host-root seam reached despite no host-root finding")

    monkeypatch.setattr(healer, "_trigger_host_resettle", _explode)
    diag = Diagnosis(
        findings=[
            Finding("istiod-no-endpoints", "x"),
            Finding("istiod-not-ready", "y"),
        ]
    )
    attempt = healer.heal(diag)
    assert attempt.action == "restart-istiod"
    assert any("rollout" in c and "restart" in c for c in calls)


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


# ── WS5-HEALER-ESC-20260802: the escalation-ordering gap, at gate level ──────
#
# The real #307 (F2) fault always yields BOTH ``istiod-api-unreachable``
# (host-root, root cause) and ``istiod-not-ready`` (RBAC, symptom of the same
# break) at once. These tests prove the gate reaches HEALED via the host-root
# tier for that co-occurring shape (not stuck looping RBAC), while F1
# (RBAC-only) still heals via RBAC and N1 (RBAC-only, unfixable) still ends
# UNSAFE — the three cases the spec's acceptance criteria name explicitly.
#
# The reviewer's REJECT (item 4) added a 4th cell: co-occurring signals on a
# host where Option-B is NOT installed. `test_gate_co_occurring_not_installed_
# falls_back_to_rbac_healed` below proves the gate reaches HEALED via the
# RBAC fallback in that cell instead of exhausting max_heal_attempts on a
# silent host-root no-op.


def test_gate_f2_co_occurring_signals_reach_host_root_within_budget(
    monkeypatch, tmp_path
):
    # F2 shape: node/route fault manifests as BOTH signals simultaneously. The
    # host-root install IS wired here (Option-B present), so the gate must
    # reach HEALED via the host-root tier on the FIRST attempt — proving it no
    # longer starves in the RBAC tier across max_heal_attempts.
    monkeypatch.setattr(mesh, "_run", lambda cmd, *, timeout=None: _proc(0, "ok"))
    heal_dir = tmp_path / "wired"
    (heal_dir / "requests").mkdir(parents=True)

    def fake_sleep(_seconds):
        for req in (heal_dir / "requests").glob("*.json"):
            (heal_dir / "results" / f"{req.stem}.result.json").write_text(
                json.dumps({"performed": True, "status": "performed"})
            )

    monkeypatch.setattr(mesh, "_sleep", fake_sleep)
    gate = MeshGate(
        MeshGateConfig(max_heal_attempts=2),
        healer=PrivilegedHealer(
            heal_dir=heal_dir, resettle_timeout=5.0, resettle_poll=0.01
        ),
    )
    seq = iter(
        [
            Diagnosis(
                findings=[
                    Finding("istiod-not-ready", "readyReplicas=0"),
                    Finding("istiod-api-unreachable", "no route to host"),
                ]
            ),
            Diagnosis(findings=[]),
        ]
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
    # exactly ONE heal attempt was needed — the gate did not burn a bounded
    # attempt in the RBAC tier before reaching the tier that actually fixes it.
    assert len(result.heal_attempts) == 1
    assert result.heal_attempts[0].action == "host-resettle"


def test_gate_co_occurring_not_installed_falls_back_to_rbac_healed(
    monkeypatch, tmp_path
):
    # Gate-level companion to the fall-through fix: the same 4th cell (WS5-
    # HEALER-ESC-20260802, reviewer REJECT item 4) proven end-to-end through
    # MeshGate.evaluate() -- co-occurring signals with Option-B NOT installed
    # must reach HEALED via the RBAC fallback on attempt 1, not exhaust
    # max_heal_attempts on a silent host-root no-op and end UNSAFE.
    monkeypatch.setattr(mesh, "_sleep", lambda s: None)
    monkeypatch.setattr(mesh, "_run", lambda cmd, *, timeout=None: _proc(0, "ok"))
    gate = MeshGate(
        MeshGateConfig(max_heal_attempts=2),
        healer=PrivilegedHealer(heal_dir=tmp_path / "not-wired"),
    )
    seq = iter(
        [
            Diagnosis(
                findings=[
                    Finding("istiod-not-ready", "readyReplicas=0"),
                    Finding("istiod-api-unreachable", "no route to host"),
                ]
            ),
            Diagnosis(findings=[]),
        ]
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
    assert len(result.heal_attempts) == 1
    assert result.heal_attempts[0].action == "restart-istiod"


def test_gate_f1_rbac_only_signal_still_heals_via_rbac(monkeypatch, tmp_path):
    # F1 shape: istiod degraded with NO host-root signal anywhere in the
    # diagnosis. The escalation-order fix must not change this outcome.
    monkeypatch.setattr(mesh, "_sleep", lambda s: None)
    monkeypatch.setattr(mesh, "_run", lambda cmd, *, timeout=None: _proc(0, "ok"))
    gate = MeshGate(
        MeshGateConfig(max_heal_attempts=2),
        healer=PrivilegedHealer(heal_dir=tmp_path / "not-wired"),
    )
    seq = iter(
        [
            Diagnosis(findings=[Finding("istiod-degraded", "control plane flapping")]),
            Diagnosis(findings=[]),
        ]
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


def test_gate_n1_rbac_only_unfixable_stays_unsafe(monkeypatch, tmp_path):
    # N1 shape (negative control): istiod scaled to 0 → istiod-not-ready is
    # the ONLY signal, and it is UNFIXABLE by any tier (no host-root signal
    # is ever produced, so escalation never triggers; a rollout restart of an
    # intentionally-scaled-to-0 deployment cannot make it ready). The gate
    # must exhaust max_heal_attempts and end UNSAFE, never a false HEALED.
    monkeypatch.setattr(mesh, "_sleep", lambda s: None)
    monkeypatch.setattr(mesh, "_run", lambda cmd, *, timeout=None: _proc(0, "ok"))
    gate = MeshGate(
        MeshGateConfig(max_heal_attempts=2),
        healer=PrivilegedHealer(heal_dir=tmp_path / "not-wired"),
    )
    gate.diagnose_mesh = lambda: Diagnosis(  # type: ignore[method-assign]
        findings=[Finding("istiod-not-ready", "readyReplicas=0 of desired=0")]
    )
    result = gate.evaluate()
    assert result.outcome == UNSAFE
    assert len(result.heal_attempts) == 2
    assert all(a.action == "restart-istiod" for a in result.heal_attempts)


# ── WS5-HEALER-ESC-20260802: split RBAC/host-root timeouts, falsifiable ──────


def test_restart_istiod_uses_rbac_rollout_timeout_not_resettle_timeout(
    monkeypatch, tmp_path
):
    # Falsifiable: the RBAC rollout-status wait must use the SHORT
    # rbac_rollout_timeout, NOT the (much longer) host-root resettle_timeout.
    # If the two were still shared this assertion would fail.
    calls = []

    def fake_run(cmd, *, timeout=None):
        calls.append(cmd)
        return _proc(0, "rolled")

    monkeypatch.setattr(mesh, "_run", fake_run)
    healer = PrivilegedHealer(
        heal_dir=tmp_path, resettle_timeout=999.0, rbac_rollout_timeout=7.0
    )
    healer.heal(Diagnosis(findings=[Finding("istiod-not-ready", "x")]))
    status_calls = [c for c in calls if "status" in c]
    assert len(status_calls) == 1
    assert "--timeout=7s" in status_calls[0]
    assert "--timeout=999s" not in status_calls[0]


def test_rbac_rollout_timeout_defaults_shorter_than_resettle_timeout():
    # The whole point of the split: the RBAC wait must not be able to consume
    # the host-root budget. A regression that re-merges the two constants (or
    # sets RBAC >= host-root) breaks the "don't starve the other tier" fix.
    assert mesh.DEFAULT_RBAC_ROLLOUT_TIMEOUT < mesh.DEFAULT_RESETTLE_TIMEOUT


def test_privileged_healer_rbac_rollout_timeout_default(tmp_path):
    healer = PrivilegedHealer(heal_dir=tmp_path)
    assert healer._rbac_rollout_timeout == mesh.DEFAULT_RBAC_ROLLOUT_TIMEOUT


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


# ═══════════════════════════════════════════════════════════════════════════
# #411 v2 — durable watched-dir ownership (G1-G4)
#
# v1 was REJECTED: it wrote heal-state to an unwatched FALLBACK dir on
# PermissionError and called that "healed". Nothing watches a fallback dir, so
# the root .path unit never fires and the deploy still ends UNSAFE — just
# slower and silently. These tests are designed to KILL that exact vacuity:
# G1 proves the code writes into the SAME dir the .path unit watches (parsed
# from the real unit file, not asserted against a hardcoded literal); G2
# drives the REAL root executor script against that SAME dir (never a
# monkeypatched result-drop into an unwatched location); G3 proves an
# unwritable watched dir produces a loud ERROR + UNSAFE, never a false
# "healed"; G4 proves the shipped tmpfiles.d rule targets that SAME dir (AND
# its top-level parent — MESH-HEAL-DIR, 2026-08-22, see below) with runner
# ownership + 0775, so the rule cannot silently drift from the unit.
#
# MESH-HEAL-DIR (2026-08-22): G4 additionally covers the TOP-LEVEL heal_dir —
# `PrivilegedHealer._publish_request` writes its atomic-rename tmp file there
# (deliberately outside requests/, see #411 v2 above), but the ORIGINAL G4
# only asserted `d` lines for requests/ and results/. A tmpfiles-driven
# recreation of the top-level parent (e.g. after a reboot, if it didn't
# already exist) left it root-owned by systemd's default, PermissionError'ing
# the runner's tmp write before it ever reached `os.replace`. The guard below
# is non-vacuous: with the top-level `d` line present it is GREEN; delete
# that one line from the shipped tmpfiles.conf and it goes RED, naming the
# missing top-level dir.
# ═══════════════════════════════════════════════════════════════════════════


def _path_unit_watched_dir() -> Path:
    """Parse ``DirectoryNotEmpty=`` out of the REAL audittrace-mesh-healer.path
    unit — never a hardcoded literal, so this test cannot pass by coincidence.
    """
    for line in PATH_UNIT.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("DirectoryNotEmpty="):
            return Path(stripped.split("=", 1)[1].strip())
    raise AssertionError(f"DirectoryNotEmpty= not found in {PATH_UNIT}")


def _service_unit_heal_dir() -> Path:
    """Parse ``Environment=MESH_HEAL_DIR=`` out of the REAL .service unit."""
    for line in SERVICE_UNIT.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("Environment=MESH_HEAL_DIR="):
            return Path(stripped.split("=", 2)[2].strip())
    raise AssertionError(f"Environment=MESH_HEAL_DIR= not found in {SERVICE_UNIT}")


# ── G1 — THE anti-vacuity gate: written dir == watched dir ───────────────────


def test_g1_default_requests_dir_matches_path_units_watched_dir():
    # Neuter-and-restore proof (done by hand during the build, reverted): if
    # PrivilegedHealer's default heal_dir pointed at any OTHER path than
    # DEFAULT_MESH_HEAL_DIR, this assertion goes red. That is the exact
    # divergence that hid the v1 bug.
    watched = _path_unit_watched_dir()
    assert mesh.mesh_heal_dir() / "requests" == watched
    assert PrivilegedHealer()._requests_dir == watched


def test_g1_service_unit_heal_dir_is_the_single_source_of_truth():
    # The .service's MESH_HEAL_DIR (joined with "requests") must be
    # byte-identical to the .path unit's watched dir — both units and the
    # code all key off the ONE env var, never independent literals.
    assert _service_unit_heal_dir() / "requests" == _path_unit_watched_dir()
    assert _service_unit_heal_dir() == Path(mesh.DEFAULT_MESH_HEAL_DIR)


def test_g1_env_override_still_targets_requests_subdir_of_mesh_heal_dir(
    monkeypatch,
):
    # The env-override path (MESH_HEAL_DIR, what the .service actually sets)
    # must resolve to the same "<dir>/requests" shape the .path unit expects.
    monkeypatch.setenv("MESH_HEAL_DIR", "/tmp/custom-heal-dir")
    assert mesh.mesh_heal_dir() / "requests" == Path("/tmp/custom-heal-dir/requests")


# ── G2 — end-to-end via the REAL watched dir, REAL executor script ──────────


def test_g2_end_to_end_via_real_watched_dir_and_real_executor(monkeypatch, tmp_path):
    # Writes the request via the UNMOCKED code path (_trigger_host_resettle ->
    # _publish_request), into tmp_path/requests. The poll's sleep then drives
    # the REAL scripts/audittrace-mesh-resettle.sh (dry-run: selects the
    # bounded op, never executes systemctl) against that SAME dir — exactly
    # the .path unit's job in production. This is NOT a monkeypatched
    # result-drop into an unwatched dir: the result file comes from the real
    # script reading + writing the same watched dir the healer wrote into.
    (tmp_path / "requests").mkdir()
    healer = PrivilegedHealer(
        heal_dir=tmp_path, resettle_timeout=10.0, resettle_poll=1.0
    )
    finding = Finding("node-not-ready", "1 NotReady", {"ready_statuses": ["False"]})

    executor_runs = []

    def fake_sleep(_seconds):
        proc = subprocess.run(
            ["bash", str(RESETTLE_SCRIPT)],
            env={
                "MESH_HEAL_DIR": str(tmp_path),
                "MESH_RESETTLE_DRY_RUN": "1",
                "PATH": "/usr/bin:/bin",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        executor_runs.append(proc)

    monkeypatch.setattr(mesh, "_sleep", fake_sleep)
    attempt = healer._trigger_host_resettle(finding)

    assert len(executor_runs) == 1
    assert executor_runs[0].returncode == 0, executor_runs[0].stderr
    assert attempt.action == "host-resettle"
    # the bounded op was genuinely selected by the REAL whitelist boundary.
    assert attempt.evidence["result"]["would_run"] == "systemctl restart k3s"
    assert attempt.evidence["result"]["status"] == "dry-run"


# ── G3 — loud on an unwritable watched dir, never a false "healed" ───────────


def test_g3_unwritable_watched_dir_logs_error_names_dir_and_raises(caplog, tmp_path):
    heal_dir = tmp_path / "heal"
    (heal_dir / "requests").mkdir(parents=True)
    (heal_dir / "results").mkdir(parents=True)
    heal_dir.chmod(0o500)  # root re-recreated it without write perms (#411)
    try:
        healer = PrivilegedHealer(heal_dir=heal_dir)
        finding = Finding("node-not-ready", "1 NotReady")
        with caplog.at_level(logging.ERROR, logger="scripts.deploy.mesh"):
            with pytest.raises((PermissionError, OSError)):
                healer._trigger_host_resettle(finding)
        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records, "expected an ERROR log naming the unwritable dir"
        assert any(str(heal_dir) in r.getMessage() for r in error_records)
        assert any("tmpfiles" in r.getMessage() for r in error_records)
        # no half-published request left behind across the privilege boundary.
        assert not list((heal_dir / "requests").glob("*.json"))
    finally:
        heal_dir.chmod(0o775)  # restore so tmp_path teardown can remove it


def test_g3_gate_resolves_unsafe_not_false_healed_on_unwritable_dir(
    monkeypatch, caplog, tmp_path
):
    heal_dir = tmp_path / "heal"
    (heal_dir / "requests").mkdir(parents=True)
    (heal_dir / "results").mkdir(parents=True)
    heal_dir.chmod(0o500)
    try:
        monkeypatch.setattr(mesh, "_sleep", lambda s: None)
        monkeypatch.setattr(mesh, "_run", lambda cmd, *, timeout=None: _proc(0, "ok"))
        gate = MeshGate(
            MeshGateConfig(max_heal_attempts=2),
            healer=PrivilegedHealer(heal_dir=heal_dir),
        )
        gate.diagnose_mesh = lambda: Diagnosis(  # type: ignore[method-assign]
            findings=[Finding("node-not-ready", "kubelet")]
        )
        with caplog.at_level(logging.ERROR, logger="scripts.deploy.mesh"):
            result = gate.evaluate()
        assert result.outcome == UNSAFE
        assert result.safe is False
        # honest, not a false "healed": exactly ONE attempt (the raise stops
        # the bounded loop immediately) and it is recorded as an error.
        assert len(result.heal_attempts) == 1
        assert result.heal_attempts[0].action == "error"
        assert any(
            r.levelno == logging.ERROR and str(heal_dir) in r.getMessage()
            for r in caplog.records
        )
    finally:
        heal_dir.chmod(0o775)


# ── #411 v3 — no orphaned tmp file when ONLY requests/ is unwritable ────────
#
# G3 above makes heal_dir (the PARENT) unwritable, so the tmp-write inside
# heal_dir fails FIRST and never reaches os.replace — it never exercises the
# narrower gap the v2 reviewer found. audittrace-mesh-heal.tmpfiles.conf
# recreates heal_dir and heal_dir/requests as TWO SEPARATE `d` lines, so they
# can drift out of sync at exactly that granularity: heal_dir stays writable
# (the self-gate's os.access(reqs, W_OK) check would otherwise refuse to even
# attempt the write — see PrivilegedHealer._host_root_installed), but
# requests/ specifically is not. That is a TOCTOU: the self-gate's snapshot
# can pass and the dir can still turn unwritable by the time os.replace()
# actually runs (root re-recreated it, a race with the tmpfiles rule, ...).
# This test simulates that race deterministically by bypassing the snapshot
# check (monkeypatching _host_root_installed to True, exactly as its own
# docstring warns callers not to rely on it as a guarantee) while the real
# filesystem state has ONLY requests/ chmod'd unwritable.
def test_publish_request_cleans_up_orphan_tmp_when_only_requests_subdir_unwritable(
    monkeypatch, caplog, tmp_path
):
    heal_dir = tmp_path / "heal"
    (heal_dir / "requests").mkdir(parents=True)
    (heal_dir / "results").mkdir(parents=True)
    (heal_dir / "requests").chmod(0o500)  # ONLY requests/ unwritable
    try:
        healer = PrivilegedHealer(heal_dir=heal_dir)
        # Bypass the os.access snapshot so the TOCTOU race is exercised
        # deterministically instead of self-gating to a no-op before ever
        # reaching _publish_request.
        monkeypatch.setattr(healer, "_host_root_installed", lambda: True)
        gate = MeshGate(
            MeshGateConfig(max_heal_attempts=2),
            healer=healer,
        )
        monkeypatch.setattr(mesh, "_sleep", lambda s: None)
        monkeypatch.setattr(mesh, "_run", lambda cmd, *, timeout=None: _proc(0, "ok"))
        gate.diagnose_mesh = lambda: Diagnosis(  # type: ignore[method-assign]
            findings=[Finding("node-not-ready", "kubelet")]
        )
        with caplog.at_level(logging.ERROR, logger="scripts.deploy.mesh"):
            result = gate.evaluate()

        # (a) no orphaned .tmp file leaked into heal_dir across the failed
        # os.replace — the exact resource-cleanup gap the v2 reviewer flagged.
        leaked = list(heal_dir.glob(".*.json.tmp"))
        assert not leaked, f"orphaned tmp file(s) leaked: {leaked}"
        # (b) still honest: the gate resolves UNSAFE, never a false "healed",
        # exactly like the parent-unwritable G3 case.
        assert result.outcome == UNSAFE
        assert result.safe is False
        assert len(result.heal_attempts) == 1
        assert result.heal_attempts[0].action == "error"
    finally:
        (heal_dir / "requests").chmod(0o775)


def test_publish_request_reraises_permission_error_unchanged(tmp_path):
    # Direct unit test of _publish_request itself (the method the fix touches):
    # the write into heal_dir succeeds, os.replace into requests/ raises, the
    # SAME exception type propagates (never swallowed/wrapped by the cleanup).
    heal_dir = tmp_path / "heal"
    (heal_dir / "requests").mkdir(parents=True)
    (heal_dir / "requests").chmod(0o500)
    try:
        healer = PrivilegedHealer(heal_dir=heal_dir)
        with pytest.raises(PermissionError):
            healer._publish_request("req-direct", {"foo": "bar"})
        assert not list(heal_dir.glob(".*.json.tmp"))
    finally:
        (heal_dir / "requests").chmod(0o775)


# ── G4 — the tmpfiles.d rule targets the watched dir, can't drift ───────────


def _tmpfiles_rule_entries() -> dict[str, list[str]]:
    entries: dict[str, list[str]] = {}
    for line in TMPFILES_RULE.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if fields[0] == "d":
            entries[fields[1]] = fields
    return entries


def test_g4_tmpfiles_rule_targets_the_watched_dirs_with_0775():
    watched_requests = _path_unit_watched_dir()
    watched_results = watched_requests.parent / "results"
    watched_toplevel = watched_requests.parent
    entries = _tmpfiles_rule_entries()
    assert str(watched_toplevel) in entries
    assert str(watched_requests) in entries
    assert str(watched_results) in entries
    for fields in entries.values():
        assert fields[0] == "d"
        assert fields[2] == "0775"


def test_g4_tmpfiles_rule_owner_is_parameterised_not_hardcoded():
    # Portability invariant: the runner user/group are placeholders
    # substituted at INSTALL time (see audittrace-mesh-resettle.sh's header),
    # never a hardcoded username baked into the shipped file.
    rule_text = TMPFILES_RULE.read_text()
    assert "lfdesousa" not in rule_text
    entries = _tmpfiles_rule_entries()
    for fields in entries.values():
        assert fields[3] == "@RUNNER_USER@"
        assert fields[4] == "@RUNNER_GROUP@"


def test_g4_tmpfiles_rule_cannot_drift_from_env_default():
    # Ties the rule back to the SAME DEFAULT_MESH_HEAL_DIR the code and the
    # .service key off — a future edit to any ONE of the four (top-level +
    # requests + results + the code default) without the others goes red
    # here. The top-level assertion is the MESH-HEAL-DIR guard (2026-08-22):
    # `_publish_request`'s atomic-rename tmp write targets this exact dir
    # (see scripts/deploy/mesh.py), so it must be a durably runner-owned `d`
    # line here too, not just its requests/ and results/ children.
    entries = _tmpfiles_rule_entries()
    default_dir = Path(mesh.DEFAULT_MESH_HEAL_DIR)
    assert str(default_dir) in entries, (
        f"top-level heal_dir {default_dir} missing a tmpfiles `d` line — "
        "PrivilegedHealer._publish_request's atomic-rename tmp write "
        "targets this exact dir and will PermissionError on a "
        "tmpfiles-driven recreation that leaves it root-owned"
    )
    assert str(default_dir / "requests") in entries
    assert str(default_dir / "results") in entries


# ── install wiring: resettle.sh + units reference the tmpfiles rule ─────────


def test_resettle_script_install_block_references_tmpfiles_rule():
    content = RESETTLE_SCRIPT.read_text()
    assert "audittrace-mesh-heal.tmpfiles.conf" in content
    assert "systemd-tmpfiles" in content


def test_path_unit_references_tmpfiles_rule_as_durable_guarantee():
    content = PATH_UNIT.read_text()
    assert "tmpfiles" in content.lower()


def test_service_unit_references_tmpfiles_rule_as_durable_guarantee():
    content = SERVICE_UNIT.read_text()
    assert "tmpfiles" in content.lower()


# ── #411 v2: no unwatched-fallback path anywhere in mesh.py ─────────────────


def test_no_fallback_dir_symbol_exists_in_mesh_module():
    # Falsifiable regression guard against re-introducing the rejected v1
    # shape (a second, unwatched heal-state dir).
    assert not hasattr(mesh, "fallback_mesh_heal_dir")
    assert not hasattr(mesh, "MESH_HEAL_STATE_DIR_ENV")
