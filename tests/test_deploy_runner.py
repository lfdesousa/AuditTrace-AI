"""Unit tests for the deterministic deploy runner (Component 2 / WS2).

Every external effect — subprocess (helm/kubectl/make/preflight) and the Docker
Hub registry HTTP call — is mocked. No cluster, no network, no sleeps. The
reviewer's mandate is to prove each gate can FAIL, so the surge assertion and
the preflight abort paths are exercised in both directions.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from scripts.deploy import mesh, registry, runner
from scripts.deploy.runner import (
    DeployConfig,
    DeployRunner,
    MeshGateAbortError,
    PreflightAbortError,
    max_concurrent_running,
    within_surge_bound,
)


@pytest.fixture(autouse=True)
def _isolate_kubeconfig():
    """DeployRunner.__init__ may mutate ``os.environ["KUBECONFIG"]`` directly
    (#456, Part B) — a plain global write, not a ``monkeypatch.setenv`` call,
    so it would otherwise leak across every other test in the suite. Snapshot
    and restore around EVERY test in this module (not just the KUBECONFIG
    tests), since any ``DeployRunner(...)`` construction can trigger it."""
    saved = os.environ.get("KUBECONFIG")
    yield
    if saved is None:
        os.environ.pop("KUBECONFIG", None)
    else:
        os.environ["KUBECONFIG"] = saved


# ── helpers ───────────────────────────────────────────────────────────────────


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["x"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _cfg(tmp_path, **kw):
    kw.setdefault("out_dir", tmp_path / "runs")
    kw.setdefault("settle_interval", 0.0)
    kw.setdefault("target_version", "v9.9.9")
    return DeployConfig(**kw)


class _StubGate:
    """A MeshGate stand-in returning a canned result — lets the runner tests
    exercise the P0 mesh wiring without touching a cluster."""

    def __init__(self, result):
        self.result = result
        self.calls = 0

    def evaluate(self):
        self.calls += 1
        return self.result


def _healthy_gate():
    diag = mesh.Diagnosis(findings=[])
    return _StubGate(mesh.MeshGateResult(mesh.HEALTHY, diag, [], diag))


def _healed_gate():
    initial = mesh.Diagnosis(findings=[mesh.Finding("istiod-api-unreachable", "x")])
    final = mesh.Diagnosis(findings=[])
    attempt = mesh.HealAttempt(True, "restart-istiod", "healed")
    return _StubGate(mesh.MeshGateResult(mesh.HEALED, initial, [attempt], final))


def _unsafe_gate():
    initial = mesh.Diagnosis(findings=[mesh.Finding("istiod-api-unreachable", "x")])
    return _StubGate(mesh.MeshGateResult(mesh.UNSAFE, initial, [], initial))


class _Dispatcher:
    """Routes runner._run calls to canned CompletedProcess results by token match."""

    def __init__(self, rules, default=None):
        self.rules = rules  # list of (needle, CompletedProcess)
        self.default = default or _proc(0, "")
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, env=None):
        self.calls.append(cmd)
        joined = " ".join(cmd)
        for needle, result in self.rules:
            if needle in joined:
                return result
        return self.default


def _deployment_doc(
    name,
    container,
    *,
    env=None,
    args=None,
    command=None,
    resources=None,
):
    """A minimal Deployment manifest doc — the ``spec.template.spec.containers``
    shape shared by BOTH a `helm template` render and a live `kubectl get
    deployment -o json` read (spec 2026-09-10-SPEC-deploy-runner-convergence-
    config-drift)."""
    return {
        "kind": "Deployment",
        "metadata": {"name": name},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": container,
                            "env": env or [],
                            "args": args or [],
                            "command": command or [],
                            "resources": resources or {},
                        }
                    ]
                }
            }
        },
    }


def _render_manifest_yaml(*docs):
    """A multi-doc YAML string — the `helm template` stdout shape. JSON is
    valid YAML, so `json.dumps` per doc keeps this dependency-free (no extra
    `yaml.dump` import in the test module)."""
    return "\n---\n".join(json.dumps(d) for d in docs)


def _helm_template_rule(*docs):
    """A `_Dispatcher` rule matching the config-drift check's `helm template`
    render call, returning the given docs as its multi-doc stdout."""
    return ("helm template", _proc(0, _render_manifest_yaml(*docs)))


def _live_deployment_rule(name, doc):
    """A `_Dispatcher` rule matching the config-drift check's LIVE
    `kubectl get deployment <name> ... -o json` read for one workload. The
    needle includes the trailing `` -n`` so it can never collide with the
    older tag-convergence path's `-o jsonpath=...` read of the SAME
    deployment name (distinguished by the ``-o json`` vs ``-o jsonpath=``
    tail, which the needle stops short of)."""
    return (f"get deployment {name} -n", _proc(0, json.dumps(doc)))


def _no_config_drift_rules(*name_container_pairs):
    """Dispatch rules making the config-drift check report NO drift for each
    given ``(deployment_name, container_name)`` pair — one shared `helm
    template` render plus one matching LIVE read per pair. Longest
    deployment name first, mirroring the existing
    `component=librechat-bff`-before-`component=librechat` ordering rule
    elsewhere in this module: `audittrace-librechat-bff` must never be
    shadowed by the shorter `audittrace-librechat` needle."""
    docs = [
        _deployment_doc(name, container) for name, container in name_container_pairs
    ]
    rules = [_helm_template_rule(*docs)]
    for name, container in sorted(name_container_pairs, key=lambda nc: -len(nc[0])):
        rules.append(_live_deployment_rule(name, _deployment_doc(name, container)))
    return rules


# ── pure surge-assertion logic (the WS1 guarantee) ───────────────────────────


def test_max_concurrent_running_counts_peak():
    samples = [["Running"], ["Running", "Pending"], ["Running", "Running"]]
    assert max_concurrent_running(samples) == 2


def test_max_concurrent_running_empty():
    assert max_concurrent_running([]) == 0


def test_within_surge_bound_ok_single_pod():
    ok, peak = within_surge_bound([["Running"], ["Running"]], replicas=1)
    assert ok is True and peak == 1


def test_within_surge_bound_flags_surge():
    # Two memory-server pods Running where the chart guarantees at most one.
    ok, peak = within_surge_bound([["Running"], ["Running", "Running"]], replicas=1)
    assert ok is False and peak == 2


def test_within_surge_bound_ha_three_replicas():
    ok, peak = within_surge_bound([["Running", "Running", "Running"]], replicas=3)
    assert ok is True and peak == 3


# ── phase ORDER + non-self-certifying report (dry-run) ───────────────────────


def _pinned_ref(digest="sha256:abc"):
    return registry.ImageRef(
        "docker.io/lfds/audittrace-memory-server", "9.9.9", digest, "hub"
    )


def test_dry_run_phase_order_and_no_mutation(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(runner, "_run", lambda *a, **k: ran.append(a) or _proc())
    # Resolution is a READ (allowed in dry-run); it shows the digest apply line.
    monkeypatch.setattr(registry, "resolve", lambda v, reg: _pinned_ref())

    cfg = _cfg(tmp_path, dry_run=True)
    report = DeployRunner(cfg).run()

    names = [p["name"] for p in report["phases"]]
    assert names == list(runner.PHASES)  # exact ordered sequence
    # dry-run mutates NOTHING: no subprocess (the only mutation vector) is called.
    assert ran == []
    assert all(p["status"] in ("planned", "ok") for p in report["phases"][:-1])
    # the P2 plan line pins by digest
    p2 = next(p for p in report["phases"] if p["name"] == "P2-chart-apply")
    assert "@sha256:abc" in p2["command"]


def test_report_never_self_certifies(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc())
    monkeypatch.setattr(registry, "resolve", lambda v, reg: _pinned_ref())
    cfg = _cfg(tmp_path, dry_run=True)
    report = DeployRunner(cfg).run()
    assert report["certified"] is None
    assert "does not certify" in report["verification"]
    assert report["dry_run"] is True


def test_dry_run_writes_report_files(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc())
    monkeypatch.setattr(registry, "resolve", lambda v, reg: _pinned_ref())
    cfg = _cfg(tmp_path, dry_run=True)
    DeployRunner(cfg).run()
    out = list((tmp_path / "runs").glob("deploy-v9.9.9-*.json"))
    assert len(out) == 1
    data = json.loads(out[0].read_text())
    assert data["certified"] is None
    assert (out[0].with_suffix(".txt")).exists()


# ── P0 preflight abort paths (exit 3 injector / 4 istiod surfaced) ───────────


@pytest.mark.parametrize(
    "code,needle",
    [
        (3, "vault-injector"),
        (4, "istiod"),
        (1, "environment"),
        (2, "chart problem"),
        (5, "anti-affinity"),
        (99, "exit 99"),
    ],
)
def test_preflight_aborts_on_nonzero(tmp_path, monkeypatch, code, needle):
    monkeypatch.setattr(
        runner, "_run", lambda *a, **k: _proc(returncode=code, stderr="boom")
    )
    r = DeployRunner(_cfg(tmp_path))
    with pytest.raises(PreflightAbortError) as exc:
        r.phase_preflight()
    assert exc.value.exit_code == code
    assert needle in r.records[0].detail
    assert r.records[0].status == "aborted"


def test_preflight_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc(0))
    r = DeployRunner(_cfg(tmp_path), mesh_gate=_healthy_gate())
    r.phase_preflight()
    # P0 now emits two records: the preflight-script gate AND the mesh gate.
    assert r.records[0].status == "ok"
    assert r.records[1].status == "ok"
    assert "mesh healthy" in r.records[1].detail


# ── P0 mesh-health gate wiring (#384 WS1) ────────────────────────────────────


def test_preflight_records_mesh_gate_when_healthy(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc(0))
    gate = _healthy_gate()
    r = DeployRunner(_cfg(tmp_path), mesh_gate=gate)
    r.phase_preflight()
    assert gate.calls == 1
    assert [rec.status for rec in r.records] == ["ok", "ok"]
    assert r.records[1].evidence["outcome"] == mesh.HEALTHY


def test_preflight_proceeds_when_mesh_auto_healed(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc(0))
    r = DeployRunner(_cfg(tmp_path), mesh_gate=_healed_gate())
    r.phase_preflight()  # HEALED is safe → no abort
    assert r.records[1].status == "ok"
    assert "auto-healed" in r.records[1].detail
    assert r.records[1].evidence["outcome"] == mesh.HEALED


def test_preflight_aborts_when_mesh_unsafe(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc(0))
    r = DeployRunner(_cfg(tmp_path), mesh_gate=_unsafe_gate())
    with pytest.raises(MeshGateAbortError) as exc:
        r.phase_preflight()
    assert exc.value.exit_code == runner.MESH_UNSAFE_EXIT
    # The preflight-script record is "ok"; the mesh gate record is "aborted".
    assert r.records[0].status == "ok"
    assert r.records[1].status == "aborted"
    assert "MESH UNSAFE" in r.records[1].detail
    assert r.records[1].evidence["outcome"] == mesh.UNSAFE


def test_run_aborts_before_mutation_when_mesh_unsafe(tmp_path, monkeypatch):
    # Preflight SCRIPT passes; only the mesh gate blocks. No helm/kubectl mutation
    # command must run after the gate aborts — the only pod is never terminated.
    disp = _Dispatcher(rules=[("deploy-preflight", _proc(0))])
    monkeypatch.setattr(runner, "_run", disp)
    report = DeployRunner(_cfg(tmp_path), mesh_gate=_unsafe_gate()).run()
    assert report["aborted"] is True
    # exactly one external command attempted: the preflight probe (mesh gate is
    # a stub here; a real gate's reads are read-only, never a mutation).
    assert len(disp.calls) == 1 and "deploy-preflight" in " ".join(disp.calls[0])
    # phases: P0 script ok, P0 mesh aborted, P5 report.
    names = [p["name"] for p in report["phases"]]
    assert names == [runner.PHASES[0], runner.PHASES[0], runner.PHASES[5]]
    mesh_rec = report["phases"][1]
    assert mesh_rec["status"] == "aborted"
    assert mesh_rec["evidence"]["outcome"] == mesh.UNSAFE


def test_dry_run_skips_mesh_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "resolve", lambda v, reg: _pinned_ref())
    gate = _unsafe_gate()  # would abort IF called
    report = DeployRunner(_cfg(tmp_path, dry_run=True), mesh_gate=gate).run()
    assert gate.calls == 0  # dry-run mutates nothing and never runs the gate
    assert report["aborted"] is False


def test_run_aborts_before_mutation_on_preflight_fail(tmp_path, monkeypatch):
    # Only the preflight command should ever run; nothing after P0.
    disp = _Dispatcher(
        rules=[("deploy-preflight", _proc(returncode=3, stderr="injector down"))]
    )
    monkeypatch.setattr(runner, "_run", disp)
    report = DeployRunner(_cfg(tmp_path)).run()
    assert report["aborted"] is True
    # exactly one external command attempted: the preflight probe
    assert len(disp.calls) == 1 and "deploy-preflight" in " ".join(disp.calls[0])
    # phases recorded: P0 aborted + P5 report only
    assert [p["name"] for p in report["phases"]] == [runner.PHASES[0], runner.PHASES[5]]


# ── P1 resolve ───────────────────────────────────────────────────────────────


def test_resolve_pinned_ok(tmp_path, monkeypatch):
    ref = registry.ImageRef(
        "docker.io/lfds/audittrace-memory-server", "v9.9.9", "sha256:abc", "hub"
    )
    monkeypatch.setattr(registry, "resolve", lambda v, reg: ref)
    r = DeployRunner(_cfg(tmp_path))
    r.phase_resolve()
    assert r.records[0].status == "ok"
    assert r.image_ref.digest == "sha256:abc"


def test_resolve_unpinned_flagged(tmp_path, monkeypatch):
    ref = registry.ImageRef(
        "localhost:5000/audittrace/memory-server", "v9.9.9", None, "local"
    )
    monkeypatch.setattr(registry, "resolve", lambda v, reg: ref)
    r = DeployRunner(_cfg(tmp_path, registry="local"))
    r.phase_resolve()
    assert r.records[0].status == "flagged"
    assert "UNRESOLVED" in r.records[0].detail


def test_resolve_dry_run_pinned(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "resolve", lambda v, reg: _pinned_ref("sha256:d"))
    r = DeployRunner(_cfg(tmp_path, dry_run=True))
    r.phase_resolve()
    assert r.image_ref.digest == "sha256:d"
    assert r.records[0].status == "planned"


def test_resolve_dry_run_soft_on_unreachable(tmp_path, monkeypatch):
    def boom(v, reg):
        raise registry.DigestResolutionError("registry unreachable")

    monkeypatch.setattr(registry, "resolve", boom)
    r = DeployRunner(_cfg(tmp_path, dry_run=True))
    r.phase_resolve()  # must NOT raise during planning
    assert r.records[0].status == "planned"
    assert r.image_ref.digest is None


def test_resolve_failure_records_failed_and_reraises(tmp_path, monkeypatch):
    def boom(v, reg):
        raise registry.DigestResolutionError("HTTP 404 bad tag")

    monkeypatch.setattr(registry, "resolve", boom)
    r = DeployRunner(_cfg(tmp_path))
    with pytest.raises(registry.DigestResolutionError):
        r.phase_resolve()
    assert r.records[0].status == "failed"
    assert "404" in r.records[0].detail


def test_run_still_emits_report_on_resolve_failure(tmp_path, monkeypatch):
    # preflight ok, resolve raises -> run() catches, later phases skipped, report emitted.
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc(0))

    def boom(v, reg):
        raise registry.DigestResolutionError("429 rate limited")

    monkeypatch.setattr(registry, "resolve", boom)
    report = DeployRunner(_cfg(tmp_path), mesh_gate=_healthy_gate()).run()
    assert report["aborted"] is True
    names = [p["name"] for p in report["phases"]]
    # P0 twice (preflight-script + mesh gate), P1 failed, then P5 report.
    assert names == [
        runner.PHASES[0],
        runner.PHASES[0],
        runner.PHASES[1],
        runner.PHASES[5],
    ]  # P2..P4 not run
    assert report["phases"][2]["status"] == "failed"  # P1 resolve failed
    assert report["certified"] is None


def test_run_still_emits_report_on_registry_read_timeout(tmp_path, monkeypatch):
    """WS5 E2E: a registry READ timeout at P1 now surfaces as a clean
    ``DigestResolutionError`` upstream (registry seam hardening), so the runner
    aborts the mutation sequence but STILL emits a report — no bare TimeoutError
    escapes ``run()``. Falsifiable: revert the registry caller catches and this
    raises TimeoutError instead of producing an aborted report."""
    # preflight ok (subprocess), but the registry egress seam TIMES OUT.
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc(0))

    def _timeout(url, headers=None):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(registry, "_http_get", _timeout)
    report = DeployRunner(_cfg(tmp_path), mesh_gate=_healthy_gate()).run()  # no raise
    assert report["aborted"] is True
    # phases[2] is P1 resolve (phases[0]/[1] are the two P0 records).
    assert report["phases"][2]["name"] == runner.PHASES[1]
    assert report["phases"][2]["status"] == "failed"
    assert "read timed out" in report["phases"][2]["detail"]
    assert report["certified"] is None


# ── version normalization ─────────────────────────────────────────────────────


def test_normalize_version_strips_single_v():
    assert runner.normalize_version("v1.13.0") == "1.13.0"
    assert runner.normalize_version("1.13.0") == "1.13.0"
    assert runner.normalize_version("vault") == "vault"  # not a version, untouched


def test_v_and_bare_version_resolve_same_tag(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        registry, "resolve", lambda v, reg: seen.append(v) or _pinned_ref()
    )
    DeployRunner(_cfg(tmp_path, dry_run=True, target_version="v1.13.0")).phase_resolve()
    DeployRunner(_cfg(tmp_path, dry_run=True, target_version="1.13.0")).phase_resolve()
    assert seen == ["1.13.0", "1.13.0"]  # both normalized before resolve


# ── convergence / idempotency (keyed on DIGEST) ──────────────────────────────


def _hub_ref(digest="sha256:x"):
    return registry.ImageRef(
        "docker.io/lfds/audittrace-memory-server", "9.9.9", digest, "hub"
    )


def test_chart_apply_noop_when_digest_converged(tmp_path, monkeypatch):
    # digest match + helm release status "deployed" + no config drift -> the
    # unchanged control case from the #451 falsifiable acceptance list
    # (extended 2026-09-10 with the config-drift no-op control).
    disp = _Dispatcher(
        rules=[
            # live pod imageID carries the SAME digest -> converged
            ("imageID", _proc(0, "docker.io/lfds/audittrace-memory-server@sha256:x")),
            (
                "helm status",
                _proc(0, json.dumps({"version": 7, "info": {"status": "deployed"}})),
            ),
            *_no_config_drift_rules(("audittrace-memory-server", "memory-server")),
        ]
    )
    monkeypatch.setattr(runner, "_run", disp)
    r = DeployRunner(_cfg(tmp_path))
    r.image_ref = _hub_ref("sha256:x")
    r.phase_chart_apply()
    assert r.converged is True and r.records[0].status == "noop"
    assert not any("helm upgrade" in " ".join(c) for c in disp.calls)
    assert "helm status=deployed" in r.records[0].detail


def test_chart_apply_upgrades_when_digest_differs(tmp_path, monkeypatch):
    # tag re-pushed to a NEW digest -> live digest differs -> NOT converged.
    disp = _Dispatcher(
        rules=[
            ("imageID", _proc(0, "docker.io/lfds/audittrace-memory-server@sha256:OLD")),
            ("helm upgrade", _proc(0, "deployed")),
            ("helm status", _proc(0, json.dumps({"version": 8}))),
        ]
    )
    monkeypatch.setattr(runner, "_run", disp)
    r = DeployRunner(_cfg(tmp_path))
    r.image_ref = _hub_ref("sha256:NEW")
    r.phase_chart_apply()
    assert r.converged is False and r.records[0].status == "ok"
    # deployed by immutable digest
    upgrade = next(c for c in disp.calls if "helm upgrade" in " ".join(c))
    assert "memoryServer.image.tag=9.9.9@sha256:NEW" in " ".join(upgrade)


def test_chart_apply_local_unpinned_uses_tag_convergence(tmp_path, monkeypatch):
    intended = "localhost:5000/audittrace/memory-server:9.9.9"
    live_doc = _deployment_doc("audittrace-memory-server", "memory-server")

    def fake_run(cmd, *, env=None):
        # `_running_image()` (tag-string convergence, `-o jsonpath=...`) and
        # the config-drift check's LIVE read (`-o json`) both target
        # `kubectl get deployment audittrace-memory-server ...` — disambiguate
        # by the actual last argv element rather than a substring needle,
        # since "-o json" is itself a substring of "-o jsonpath=...".
        if cmd[:3] == ["kubectl", "get", "deployment"]:
            return (
                _proc(0, json.dumps(live_doc))
                if cmd[-1] == "json"
                else _proc(0, intended)
            )
        joined = " ".join(cmd)
        if "helm template" in joined:
            return _proc(0, _render_manifest_yaml(live_doc))
        if "helm status" in joined:
            return _proc(0, json.dumps({"version": 3, "info": {"status": "deployed"}}))
        return _proc(0, "")

    monkeypatch.setattr(runner, "_run", fake_run)
    r = DeployRunner(_cfg(tmp_path, registry="local"))
    r.image_ref = registry.ImageRef(
        "localhost:5000/audittrace/memory-server", "9.9.9", None, "local"
    )
    r.phase_chart_apply()
    assert r.converged is True and r.records[0].status == "noop"


# ── #451: helm-status-aware re-convergence (digest match alone insufficient) ─


def test_chart_apply_reconverges_when_status_failed(tmp_path, monkeypatch):
    """Digest already matches, but the Helm release is stuck `failed` — the
    runner must run `helm upgrade` to reconcile, NOT record `noop`.

    Falsifiable: neuter `_is_converged` back to digest-only convergence and
    this test goes RED (records[0].status becomes "noop", no helm upgrade
    call is made).
    """
    disp = _Dispatcher(
        rules=[
            ("imageID", _proc(0, "docker.io/lfds/audittrace-memory-server@sha256:x")),
            (
                "helm status",
                _proc(0, json.dumps({"version": 7, "info": {"status": "failed"}})),
            ),
            ("helm upgrade", _proc(0, "deployed")),
        ]
    )
    monkeypatch.setattr(runner, "_run", disp)
    r = DeployRunner(_cfg(tmp_path))
    r.image_ref = _hub_ref("sha256:x")
    r.phase_chart_apply()
    assert r.converged is False
    assert r.records[0].status in ("ok", "flagged")
    assert r.records[0].status != "noop"
    assert any("helm upgrade" in " ".join(c) for c in disp.calls)
    assert (
        "digest matches but helm release status='failed' "
        "→ re-running helm upgrade to reconcile release state" in r.records[0].detail
    )


def test_chart_apply_reconverges_when_status_pending_upgrade(tmp_path, monkeypatch):
    """Digest matches, status `pending-upgrade` -> helm upgrade RUNS."""
    disp = _Dispatcher(
        rules=[
            ("imageID", _proc(0, "docker.io/lfds/audittrace-memory-server@sha256:x")),
            (
                "helm status",
                _proc(
                    0, json.dumps({"version": 7, "info": {"status": "pending-upgrade"}})
                ),
            ),
            ("helm upgrade", _proc(0, "deployed")),
        ]
    )
    monkeypatch.setattr(runner, "_run", disp)
    r = DeployRunner(_cfg(tmp_path))
    r.image_ref = _hub_ref("sha256:x")
    r.phase_chart_apply()
    assert r.converged is False
    assert r.records[0].status == "ok"
    assert any("helm upgrade" in " ".join(c) for c in disp.calls)
    assert "status='pending-upgrade'" in r.records[0].detail


def test_chart_apply_reconverges_when_status_unreadable(tmp_path, monkeypatch):
    """Digest matches but `helm status` itself is unreadable (non-zero exit)
    -> fail-safe NOT converged, helm upgrade RUNS.

    Falsifiable: neuter the fail-safe (treat an unreadable/None status as
    converged) and this test goes RED.
    """
    disp = _Dispatcher(
        rules=[
            ("imageID", _proc(0, "docker.io/lfds/audittrace-memory-server@sha256:x")),
            ("helm status", _proc(returncode=1, stderr="Error: release: not found")),
            ("helm upgrade", _proc(0, "deployed")),
        ]
    )
    monkeypatch.setattr(runner, "_run", disp)
    r = DeployRunner(_cfg(tmp_path))
    r.image_ref = _hub_ref("sha256:x")
    r.phase_chart_apply()
    assert r.converged is False
    assert r.records[0].status != "noop"
    assert any("helm upgrade" in " ".join(c) for c in disp.calls)
    assert "digest matches but helm release status=None" in r.records[0].detail


def test_chart_apply_upgrade_runs_on_digest_mismatch_no_reconcile_note(
    tmp_path, monkeypatch
):
    """Digest MISMATCH -> helm upgrade runs (unchanged); no reconcile note is
    added since the reason is an ordinary digest mismatch, not a stale
    release status."""
    disp = _Dispatcher(
        rules=[
            ("imageID", _proc(0, "docker.io/lfds/audittrace-memory-server@sha256:OLD")),
            ("helm upgrade", _proc(0, "deployed")),
            (
                "helm status",
                _proc(0, json.dumps({"version": 8, "info": {"status": "deployed"}})),
            ),
        ]
    )
    monkeypatch.setattr(runner, "_run", disp)
    r = DeployRunner(_cfg(tmp_path))
    r.image_ref = _hub_ref("sha256:NEW")
    r.phase_chart_apply()
    assert r.converged is False and r.records[0].status == "ok"
    assert "reconcile release state" not in r.records[0].detail


# ── first-party-image-complete convergence (spec 2026-09-10) ────────────────
# `_is_converged()` keying on the memory-server digest alone let the v1.26.0
# WU-6 Part C.4 redeploy no-op while the `librechat` console pod stayed on a
# stale digest (origin finding
# `finding-deploy-runner-convergence-noop-console-20260910.md`): memory-server
# was already converged, so P2 skipped `helm upgrade` and the already-merged
# console `--set` fix (`console_image_set_args`) never ran to correct the
# drift. `_is_converged()` must ALSO compare each ENABLED console image's live
# pod `imageID` against the digest pinned in the committed chart
# `values.yaml` (the same source `console_image_set_args` reads).

# Reuses `_CONSOLE_VALUES_ENABLED` / `_CONSOLE_VALUES_DISABLED` defined below
# (librechat pinned to sha256:d1, bff pinned to sha256:d2) — module-level
# constants, resolved at call time so the forward reference is safe.


def _console_dispatch_rules(
    *, librechat_digest="sha256:d1", bff_digest="sha256:d2", helm_status="deployed"
):
    """Shared dispatcher rules: memory-server digest matches, console images
    running the given digests. Order matters — `component=librechat-bff` MUST
    be checked before the plainer `component=librechat` needle, since the
    latter is a substring of the former."""
    return [
        (
            "component=memory-server",
            _proc(0, "docker.io/lfds/audittrace-memory-server@sha256:x"),
        ),
        ("component=librechat-bff", _proc(0, f"repo/bff@{bff_digest}")),
        ("component=librechat", _proc(0, f"repo/librechat@{librechat_digest}")),
        (
            "helm status",
            _proc(0, json.dumps({"version": 265, "info": {"status": helm_status}})),
        ),
        ("helm upgrade", _proc(0, "deployed")),
    ]


def test_is_converged_false_when_console_image_mismatches(tmp_path, monkeypatch):
    """Memory-server digest matches + helm `deployed`, but the live
    `librechat` pod imageID != the chart-pinned digest -> NOT converged.

    Falsifiable / neuter: revert `_is_converged()` to memory-server-only
    convergence (drop the console check) and this test goes RED —
    `check.converged` flips to True, exactly the v1.26.0 WU-6 Part C.4 defect
    reproduced hermetically (guard name matches behaviour,
    `feedback_vacuous_neuter_test_antipattern`).
    """
    disp = _Dispatcher(rules=_console_dispatch_rules(librechat_digest="sha256:STALE"))
    monkeypatch.setattr(runner, "_run", disp)
    r = DeployRunner(_cfg(tmp_path))
    r.image_ref = _hub_ref("sha256:x")
    check = r._is_converged(chart_values=_CONSOLE_VALUES_ENABLED)
    assert check.converged is False
    assert "console mismatch: librechat" in check.basis
    # bff matched -> only librechat is named as the mismatch.
    assert "bff" not in check.basis.split("console mismatch:")[1]
    # No `helm status` read once a console mismatch is found — mirrors the
    # existing "digest mismatch skips helm status" efficiency (#451).
    assert not any("helm status" in " ".join(c) for c in disp.calls)


def test_chart_apply_runs_upgrade_when_console_image_mismatches(tmp_path, monkeypatch):
    """End-to-end through `phase_chart_apply`: a console-image mismatch
    means `helm upgrade` actually RUNS (not skipped as a no-op) — the fix
    for the v1.26.0 WU-6 Part C.4 no-op."""
    disp = _Dispatcher(rules=_console_dispatch_rules(librechat_digest="sha256:STALE"))
    monkeypatch.setattr(runner, "_run", disp)
    monkeypatch.setattr(
        runner, "_read_chart_values", lambda *a, **k: _CONSOLE_VALUES_ENABLED
    )
    r = DeployRunner(_cfg(tmp_path))
    r.image_ref = _hub_ref("sha256:x")
    r.phase_chart_apply()
    assert r.converged is False
    assert r.records[0].status != "noop"
    assert any("helm upgrade" in " ".join(c) for c in disp.calls)


def test_is_converged_true_when_all_first_party_images_match(tmp_path, monkeypatch):
    """All first-party images (memory-server + both console images) match
    their pins, helm `deployed` -> converged (the true no-op case is
    preserved, not just any mismatch making everything NOT-converged)."""
    disp = _Dispatcher(
        rules=[
            *_console_dispatch_rules(),
            *_no_config_drift_rules(
                ("audittrace-memory-server", "memory-server"),
                ("audittrace-librechat", "librechat"),
                ("audittrace-librechat-bff", "bff"),
            ),
        ]
    )
    monkeypatch.setattr(runner, "_run", disp)
    r = DeployRunner(_cfg(tmp_path))
    r.image_ref = _hub_ref("sha256:x")
    check = r._is_converged(chart_values=_CONSOLE_VALUES_ENABLED)
    assert check.converged is True
    assert "console matched: bff, librechat" in check.basis


def test_chart_apply_noop_when_all_first_party_images_match(tmp_path, monkeypatch):
    """End-to-end: with every first-party image matched, P2 still records the
    true no-op (`helm upgrade` never called) — the console check must not
    turn every deploy into a spurious upgrade."""
    disp = _Dispatcher(
        rules=[
            *_console_dispatch_rules(),
            *_no_config_drift_rules(
                ("audittrace-memory-server", "memory-server"),
                ("audittrace-librechat", "librechat"),
                ("audittrace-librechat-bff", "bff"),
            ),
        ]
    )
    monkeypatch.setattr(runner, "_run", disp)
    monkeypatch.setattr(
        runner, "_read_chart_values", lambda *a, **k: _CONSOLE_VALUES_ENABLED
    )
    r = DeployRunner(_cfg(tmp_path))
    r.image_ref = _hub_ref("sha256:x")
    r.phase_chart_apply()
    assert r.converged is True and r.records[0].status == "noop"
    assert not any("helm upgrade" in " ".join(c) for c in disp.calls)


def test_is_converged_ignores_console_when_disabled(tmp_path, monkeypatch):
    """`console.enabled=false` -> convergence keys on memory-server alone;
    no console kubectl calls are made and a "would-be mismatch" digest is
    never even read, so a disabled (absent) component can never falsely
    report NOT-converged."""
    disp = _Dispatcher(
        rules=[
            (
                "component=memory-server",
                _proc(0, "docker.io/lfds/audittrace-memory-server@sha256:x"),
            ),
            (
                "helm status",
                _proc(0, json.dumps({"version": 1, "info": {"status": "deployed"}})),
            ),
            *_no_config_drift_rules(("audittrace-memory-server", "memory-server")),
        ]
    )
    monkeypatch.setattr(runner, "_run", disp)
    r = DeployRunner(_cfg(tmp_path))
    r.image_ref = _hub_ref("sha256:x")
    check = r._is_converged(chart_values=_CONSOLE_VALUES_DISABLED)
    assert check.converged is True
    assert "console" not in check.basis
    assert not any("librechat" in " ".join(c) for c in disp.calls)


def test_is_converged_defaults_to_reading_real_chart_values_file(tmp_path, monkeypatch):
    """No explicit `chart_values` -> `_is_converged` reads from disk via
    `_read_chart_values`, exactly like `_helm_apply_cmd` — the production
    path never passes `chart_values` explicitly."""
    calls: list[tuple] = []
    real_read = runner._read_chart_values

    def _spy(*a, **k):
        calls.append((a, k))
        return real_read(*a, **k)

    monkeypatch.setattr(runner, "_read_chart_values", _spy)
    disp = _Dispatcher(
        rules=[
            (
                "component=memory-server",
                _proc(0, "docker.io/lfds/audittrace-memory-server@sha256:x"),
            ),
            (
                "helm status",
                _proc(0, json.dumps({"version": 1, "info": {"status": "deployed"}})),
            ),
        ]
    )
    monkeypatch.setattr(runner, "_run", disp)
    r = DeployRunner(_cfg(tmp_path))
    r.image_ref = _hub_ref("sha256:x")
    r._is_converged()
    assert calls, (
        "_is_converged must call _read_chart_values() when chart_values is None"
    )


# ── config-drift convergence (spec 2026-09-10-SPEC-deploy-runner-convergence-
# config-drift) ───────────────────────────────────────────────────────────
# `_is_converged()` keyed on first-party IMAGE digests alone let a
# config-only chart change (env vars, args/command, resource limits — no
# image moves) silently no-op: observed live 2026-09-10, the WU-5
# sources-trailer redeploy (`AUDITTRACE_RESPONSE_SOURCES: off->trailer`, all
# images unchanged) no-op'd — rev stayed 266, the live env stayed `off`.
# `_is_converged()` must ALSO compare the INTENDED (`helm template`-rendered)
# container spec of each ENABLED first-party workload against its LIVE
# `kubectl get deployment` container spec.


def test_is_converged_false_when_config_env_differs(tmp_path, monkeypatch):
    """Images all match + helm `deployed`, but the INTENDED (rendered) env
    differs from the LIVE deployment's env -> NOT converged (so P2 runs
    `helm upgrade`) — the exact WU-5 sources-trailer no-op this spec closes.

    Falsifiable / neuter: drop the config-drift check from `_is_converged`
    (revert to image-digest + helm-status-only convergence, the pre-fix
    behaviour) and this test goes RED — `check.converged` flips to True,
    silently reproducing the live no-op observed 2026-09-10.
    """
    intended_doc = _deployment_doc(
        "audittrace-memory-server",
        "memory-server",
        env=[{"name": "AUDITTRACE_RESPONSE_SOURCES", "value": "trailer"}],
    )
    live_doc = _deployment_doc(
        "audittrace-memory-server",
        "memory-server",
        env=[{"name": "AUDITTRACE_RESPONSE_SOURCES", "value": "off"}],
    )
    disp = _Dispatcher(
        rules=[
            ("component=memory-server", _proc(0, "repo@sha256:x")),
            (
                "helm status",
                _proc(0, json.dumps({"version": 266, "info": {"status": "deployed"}})),
            ),
            _helm_template_rule(intended_doc),
            _live_deployment_rule("audittrace-memory-server", live_doc),
        ]
    )
    monkeypatch.setattr(runner, "_run", disp)
    r = DeployRunner(_cfg(tmp_path))
    r.image_ref = _hub_ref("sha256:x")
    check = r._is_converged(chart_values=_CONSOLE_VALUES_DISABLED)
    assert check.converged is False
    assert "config drift: memory-server" in check.basis
    assert check.helm_status == "deployed"
    # No reconcile-note flag: config drift explains the non-convergence on
    # its own, distinct from the #451 "digest matches but status != deployed"
    # case.
    assert check.digest_matched is False


def test_chart_apply_runs_upgrade_when_config_env_differs(tmp_path, monkeypatch):
    """End-to-end through `phase_chart_apply`: a config-only env drift means
    `helm upgrade` actually RUNS (not skipped as a no-op) — the fix for the
    WU-5 sources-trailer no-op."""
    intended_doc = _deployment_doc(
        "audittrace-memory-server",
        "memory-server",
        env=[{"name": "AUDITTRACE_RESPONSE_SOURCES", "value": "trailer"}],
    )
    live_doc = _deployment_doc(
        "audittrace-memory-server",
        "memory-server",
        env=[{"name": "AUDITTRACE_RESPONSE_SOURCES", "value": "off"}],
    )
    disp = _Dispatcher(
        rules=[
            ("component=memory-server", _proc(0, "repo@sha256:x")),
            (
                "helm status",
                _proc(0, json.dumps({"version": 266, "info": {"status": "deployed"}})),
            ),
            _helm_template_rule(intended_doc),
            _live_deployment_rule("audittrace-memory-server", live_doc),
            ("helm upgrade", _proc(0, "deployed")),
        ]
    )
    monkeypatch.setattr(runner, "_run", disp)
    monkeypatch.setattr(
        runner, "_read_chart_values", lambda *a, **k: _CONSOLE_VALUES_DISABLED
    )
    r = DeployRunner(_cfg(tmp_path))
    r.image_ref = _hub_ref("sha256:x")
    r.phase_chart_apply()
    assert r.converged is False
    assert r.records[0].status != "noop"
    assert any("helm upgrade" in " ".join(c) for c in disp.calls)


def test_is_converged_false_when_config_resources_differ(tmp_path, monkeypatch):
    """A `resources` diff (not just env) also trips NOT-converged — the
    config-drift check compares env AND args/command AND resources."""
    intended_doc = _deployment_doc(
        "audittrace-memory-server",
        "memory-server",
        resources={"requests": {"cpu": "500m"}, "limits": {"cpu": "1"}},
    )
    live_doc = _deployment_doc(
        "audittrace-memory-server",
        "memory-server",
        resources={"requests": {"cpu": "250m"}, "limits": {"cpu": "1"}},
    )
    disp = _Dispatcher(
        rules=[
            ("component=memory-server", _proc(0, "repo@sha256:x")),
            (
                "helm status",
                _proc(0, json.dumps({"version": 1, "info": {"status": "deployed"}})),
            ),
            _helm_template_rule(intended_doc),
            _live_deployment_rule("audittrace-memory-server", live_doc),
        ]
    )
    monkeypatch.setattr(runner, "_run", disp)
    r = DeployRunner(_cfg(tmp_path))
    r.image_ref = _hub_ref("sha256:x")
    check = r._is_converged(chart_values=_CONSOLE_VALUES_DISABLED)
    assert check.converged is False
    assert "config drift: memory-server" in check.basis


def test_is_converged_false_when_config_args_differ(tmp_path, monkeypatch):
    """An `args` diff also trips NOT-converged."""
    intended_doc = _deployment_doc(
        "audittrace-memory-server",
        "memory-server",
        command=["/bin/sh", "-c"],
        args=["exec new-thing"],
    )
    live_doc = _deployment_doc(
        "audittrace-memory-server",
        "memory-server",
        command=["/bin/sh", "-c"],
        args=["exec old-thing"],
    )
    disp = _Dispatcher(
        rules=[
            ("component=memory-server", _proc(0, "repo@sha256:x")),
            (
                "helm status",
                _proc(0, json.dumps({"version": 1, "info": {"status": "deployed"}})),
            ),
            _helm_template_rule(intended_doc),
            _live_deployment_rule("audittrace-memory-server", live_doc),
        ]
    )
    monkeypatch.setattr(runner, "_run", disp)
    r = DeployRunner(_cfg(tmp_path))
    r.image_ref = _hub_ref("sha256:x")
    check = r._is_converged(chart_values=_CONSOLE_VALUES_DISABLED)
    assert check.converged is False
    assert "config drift: memory-server" in check.basis


def test_is_converged_true_when_config_also_matches(tmp_path, monkeypatch):
    """Nothing changed — images, console, AND config all match — is still a
    TRUE no-op: the config-drift check must not turn every unchanged deploy
    into a spurious upgrade."""
    doc = _deployment_doc(
        "audittrace-memory-server",
        "memory-server",
        env=[{"name": "AUDITTRACE_RESPONSE_SOURCES", "value": "trailer"}],
    )
    disp = _Dispatcher(
        rules=[
            ("component=memory-server", _proc(0, "repo@sha256:x")),
            (
                "helm status",
                _proc(0, json.dumps({"version": 266, "info": {"status": "deployed"}})),
            ),
            _helm_template_rule(doc),
            _live_deployment_rule("audittrace-memory-server", doc),
        ]
    )
    monkeypatch.setattr(runner, "_run", disp)
    r = DeployRunner(_cfg(tmp_path))
    r.image_ref = _hub_ref("sha256:x")
    check = r._is_converged(chart_values=_CONSOLE_VALUES_DISABLED)
    assert check.converged is True
    assert "config drift" not in check.basis


def test_config_drift_check_skipped_when_helm_status_not_deployed(
    tmp_path, monkeypatch
):
    """The config-drift check runs LAST: when the Helm release status is
    itself not `deployed`, `_is_converged` returns NOT-converged from the
    existing #451 status check WITHOUT ever calling `helm template` or
    reading the live Deployment spec — preserves the #451 efficiency note
    ("no helm status call once mismatch found") symmetrically for config."""
    disp = _Dispatcher(
        rules=[
            ("component=memory-server", _proc(0, "repo@sha256:x")),
            (
                "helm status",
                _proc(0, json.dumps({"version": 1, "info": {"status": "failed"}})),
            ),
        ]
    )
    monkeypatch.setattr(runner, "_run", disp)
    r = DeployRunner(_cfg(tmp_path))
    r.image_ref = _hub_ref("sha256:x")
    check = r._is_converged(chart_values=_CONSOLE_VALUES_DISABLED)
    assert check.converged is False
    assert not any("helm template" in " ".join(c) for c in disp.calls)
    assert not any("get deployment" in " ".join(c) for c in disp.calls)


def test_config_drifted_workloads_fail_safe_on_unreadable_render(tmp_path, monkeypatch):
    """An unreadable `helm template` (non-zero exit) counts as drift, never a
    silent match — fail-safe, mirrors `_mismatched_console_images`'s own
    unknown-state handling."""
    disp = _Dispatcher(
        rules=[
            ("helm template", _proc(returncode=1, stderr="boom")),
            _live_deployment_rule(
                "audittrace-memory-server",
                _deployment_doc("audittrace-memory-server", "memory-server"),
            ),
        ]
    )
    monkeypatch.setattr(runner, "_run", disp)
    r = DeployRunner(_cfg(tmp_path))
    drifted = r._config_drifted_workloads(_CONSOLE_VALUES_DISABLED)
    assert drifted == ["memory-server"]


def test_config_drifted_workloads_fail_safe_on_unreadable_live(tmp_path, monkeypatch):
    """An unreadable LIVE `kubectl get deployment` (non-zero exit) also
    counts as drift, never a silent match."""
    doc = _deployment_doc("audittrace-memory-server", "memory-server")
    disp = _Dispatcher(
        rules=[
            _helm_template_rule(doc),
            ("get deployment audittrace-memory-server -n", _proc(returncode=1)),
        ]
    )
    monkeypatch.setattr(runner, "_run", disp)
    r = DeployRunner(_cfg(tmp_path))
    drifted = r._config_drifted_workloads(_CONSOLE_VALUES_DISABLED)
    assert drifted == ["memory-server"]


def test_config_drifted_workloads_multi_workload_partial_drift(tmp_path, monkeypatch):
    """Only the drifted component is named — a match on one first-party
    workload does not mask a drift on another."""
    disp = _Dispatcher(
        rules=[
            *_no_config_drift_rules(
                ("audittrace-memory-server", "memory-server"),
                ("audittrace-librechat-bff", "bff"),
            ),
            _live_deployment_rule(
                "audittrace-librechat",
                _deployment_doc(
                    "audittrace-librechat",
                    "librechat",
                    env=[{"name": "DRIFTED", "value": "yes"}],
                ),
            ),
        ]
    )
    monkeypatch.setattr(runner, "_run", disp)
    r = DeployRunner(_cfg(tmp_path))
    drifted = r._config_drifted_workloads(_CONSOLE_VALUES_ENABLED)
    assert drifted == ["librechat"]


def test_render_chart_manifest_none_on_helm_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc(returncode=1))
    r = DeployRunner(_cfg(tmp_path))
    assert r._render_chart_manifest() is None


def test_render_chart_manifest_none_on_unparsable_yaml(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner, "_run", lambda *a, **k: _proc(0, ": not: yaml: at: all:")
    )
    r = DeployRunner(_cfg(tmp_path))
    assert r._render_chart_manifest() is None


def test_render_chart_manifest_skips_non_mapping_docs(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner, "_run", lambda *a, **k: _proc(0, "- a\n- b\n---\nkind: Deployment\n")
    )
    r = DeployRunner(_cfg(tmp_path))
    docs = r._render_chart_manifest()
    assert docs == [{"kind": "Deployment"}]


def test_live_deployment_container_spec_none_on_error(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc(returncode=1))
    r = DeployRunner(_cfg(tmp_path))
    assert r._live_deployment_container_spec("audittrace-memory-server", "x") is None


def test_live_deployment_container_spec_none_on_unparsable_json(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc(0, "not json"))
    r = DeployRunner(_cfg(tmp_path))
    assert r._live_deployment_container_spec("audittrace-memory-server", "x") is None


def test_first_party_config_workloads_memory_server_only_when_console_disabled(
    tmp_path,
):
    r = DeployRunner(_cfg(tmp_path))
    workloads = r._first_party_config_workloads(_CONSOLE_VALUES_DISABLED)
    assert workloads == [("memory-server", "audittrace-memory-server", "memory-server")]


def test_first_party_config_workloads_includes_console_when_enabled(tmp_path):
    r = DeployRunner(_cfg(tmp_path))
    workloads = r._first_party_config_workloads(_CONSOLE_VALUES_ENABLED)
    assert workloads == [
        ("memory-server", "audittrace-memory-server", "memory-server"),
        ("librechat", "audittrace-librechat", "librechat"),
        ("bff", "audittrace-librechat-bff", "bff"),
    ]


# ── config-drift normalisation (pure helpers) ────────────────────────────────


def test_normalize_env_handles_value_and_valuefrom_and_skips_malformed():
    env = [
        {"name": "A", "value": "1"},
        {"name": "B", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}},
        {"value": "no-name"},  # skipped: no name
        "not-a-dict",  # skipped: malformed
    ]
    assert runner._normalize_env(env) == {
        "A": "1",
        "B": {"valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}},
    }
    assert runner._normalize_env(None) == {}


def test_normalize_resources_defaults_and_malformed():
    assert runner._normalize_resources({"requests": {"cpu": "1"}}) == {
        "requests": {"cpu": "1"},
        "limits": {},
    }
    assert runner._normalize_resources("not-a-dict") == {"requests": {}, "limits": {}}
    assert runner._normalize_resources({"requests": "bad", "limits": None}) == {
        "requests": {},
        "limits": {},
    }


def test_normalize_container_spec_shape():
    container = {
        "name": "memory-server",
        "env": [{"name": "A", "value": "1"}],
        "args": ["x"],
        "command": ["/bin/sh"],
        "resources": {"requests": {"cpu": "1"}},
        "image": "repo:tag",  # deliberately ignored — digest convergence covers it
    }
    assert runner._normalize_container_spec(container) == {
        "env": {"A": "1"},
        "args": ["x"],
        "command": ["/bin/sh"],
        "resources": {"requests": {"cpu": "1"}, "limits": {}},
    }


def test_find_container_returns_none_when_absent_or_malformed():
    assert runner._find_container(None, "x") is None
    assert runner._find_container([{"name": "y"}], "x") is None
    assert runner._find_container(["not-a-dict"], "x") is None
    assert runner._find_container([{"name": "x", "env": []}], "x") == {
        "name": "x",
        "env": [],
    }


def test_container_spec_from_deployment_doc_missing_pieces():
    assert runner._container_spec_from_deployment_doc(None, "x") is None
    assert (
        runner._container_spec_from_deployment_doc({"spec": "not-a-dict"}, "x") is None
    )
    assert (
        runner._container_spec_from_deployment_doc(
            {"spec": {"template": {"spec": {"containers": [{"name": "other"}]}}}},
            "x",
        )
        is None
    )


def test_find_deployment_doc_matches_by_kind_and_name():
    docs = [
        {"kind": "Service", "metadata": {"name": "audittrace-memory-server"}},
        {"kind": "Deployment", "metadata": {"name": "other"}},
        {"kind": "Deployment", "metadata": {"name": "audittrace-memory-server"}},
    ]
    found = runner._find_deployment_doc(docs, "audittrace-memory-server")
    assert found is docs[2]
    assert runner._find_deployment_doc(None, "x") is None
    assert runner._find_deployment_doc([], "x") is None


def test_console_image_digests_empty_when_disabled_or_malformed():
    assert runner.console_image_digests({"console": {"enabled": False}}) == {}
    assert runner.console_image_digests({}) == {}
    assert runner.console_image_digests({"console": "not-a-dict"}) == {}


def test_console_image_digests_full_shape():
    assert runner.console_image_digests(_CONSOLE_VALUES_ENABLED) == {
        "librechat": "sha256:d1",
        "bff": "sha256:d2",
    }


def test_console_image_digests_omits_component_missing_digest():
    values = {
        "console": {
            "enabled": True,
            "librechat": {"image": {"repository": "repo/lc", "tag": "t"}},
            "bff": "not-a-dict",
        }
    }
    assert runner.console_image_digests(values) == {}


def test_running_component_digest_generalizes_memory_server(tmp_path, monkeypatch):
    """`_running_image_digest` delegates to `_running_component_digest` with
    the memory-server selector/container — proves the refactor preserves the
    original behaviour exactly (same public method, same result)."""
    monkeypatch.setattr(
        runner, "_run", lambda *a, **k: _proc(0, "repo:tag repo@sha256:live")
    )
    r = DeployRunner(_cfg(tmp_path))
    assert (
        r._running_component_digest(
            runner._COMPONENT_SELECTOR, runner.MEMORY_SERVER_CONTAINER
        )
        == r._running_image_digest()
    )


def test_helm_status_info_parsers(tmp_path, monkeypatch):
    r = DeployRunner(_cfg(tmp_path))
    monkeypatch.setattr(
        runner,
        "_run",
        lambda *a, **k: _proc(
            0, json.dumps({"version": 3, "info": {"status": "deployed"}})
        ),
    )
    assert r._helm_status_info() == (3, "deployed")
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc(0, "not json"))
    assert r._helm_status_info() == (None, None)
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc(returncode=1))
    assert r._helm_status_info() == (None, None)
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc(0, json.dumps([1, 2])))
    assert r._helm_status_info() == (None, None)
    monkeypatch.setattr(
        runner, "_run", lambda *a, **k: _proc(0, json.dumps({"version": "x"}))
    )
    assert r._helm_status_info() == (None, None)
    monkeypatch.setattr(
        runner,
        "_run",
        lambda *a, **k: _proc(0, json.dumps({"version": 3, "info": "not-a-dict"})),
    )
    assert r._helm_status_info() == (3, None)
    monkeypatch.setattr(
        runner,
        "_run",
        lambda *a, **k: _proc(0, json.dumps({"version": 3, "info": {"status": 42}})),
    )
    assert r._helm_status_info() == (3, None)


def test_apply_image_tag_pins_by_digest_and_local_plain():
    cfg = DeployConfig(target_version="v1.13.0")
    pinned = registry.ImageRef("repo", "1.13.0", "sha256:zz", "hub")
    assert runner.apply_image_tag(cfg, pinned) == "1.13.0@sha256:zz"
    unpinned = registry.ImageRef("repo", "1.13.0", None, "local")
    assert runner.apply_image_tag(cfg, unpinned) == "1.13.0"


# ── console image pins (spec 2026-09-10) ────────────────────────────────────
# `console.librechat.image` / `console.bff.image` are chart-`values.yaml`-
# driven; `--reset-then-reuse-values` re-applies any stale stored per-image
# `--set` from a pre-D2-5C deploy over the chart default forever (#394-class
# defect, observed live at the v1.26.0 WU-6 Part C deploy — origin finding
# `finding-deploy-runner-stale-setoverride-shadows-chart-20260910.md`). The
# runner now reads the committed values.yaml (SSOT) and asserts explicit
# --set args for both images on every apply so they always outrank a stale
# stored override. Real-`helm template` neuter proof (that a stale stored
# override actually loses to these --set args) lives in
# tests/test_deploy_runner_console_image_pins.py; these are the hermetic
# unit-level guards over the pure derivation + disk read.


def test_console_image_set_args_empty_when_console_disabled():
    assert runner.console_image_set_args({"console": {"enabled": False}}) == []
    assert runner.console_image_set_args({}) == []
    assert runner.console_image_set_args({"console": "not-a-dict"}) == []


def test_console_image_set_args_full_shape():
    values = {
        "console": {
            "enabled": True,
            "librechat": {
                "image": {
                    "repository": "docker.io/lfds/audittrace-librechat",
                    "tag": "c879b74",
                    "digest": "sha256:aaaa",
                }
            },
            "bff": {
                "image": {
                    "repository": "docker.io/lfds/audittrace-librechat-bff",
                    "tag": "1.26.0",
                    "digest": "sha256:bbbb",
                }
            },
        }
    }
    args = runner.console_image_set_args(values)
    assert args == [
        "--set",
        "console.librechat.image.repository=docker.io/lfds/audittrace-librechat",
        "--set",
        "console.librechat.image.tag=c879b74",
        "--set",
        "console.librechat.image.digest=sha256:aaaa",
        "--set",
        "console.bff.image.repository=docker.io/lfds/audittrace-librechat-bff",
        "--set",
        "console.bff.image.tag=1.26.0",
        "--set",
        "console.bff.image.digest=sha256:bbbb",
    ]


def test_console_image_set_args_skips_missing_fields_and_bad_block():
    values = {
        "console": {
            "enabled": True,
            "librechat": {
                "image": {"repository": "repo/lc", "tag": "", "digest": None}
            },
            "bff": {"image": "not-a-dict"},
        }
    }
    args = runner.console_image_set_args(values)
    # empty tag / None digest / non-dict bff image block all skipped —
    # never a broken `--set console.librechat.image.tag=`.
    assert args == ["--set", "console.librechat.image.repository=repo/lc"]


def test_read_chart_values_real_committed_file_has_console_block():
    values = runner._read_chart_values()
    assert isinstance(values.get("console"), dict)
    assert "librechat" in values["console"]
    assert "bff" in values["console"]


def test_read_chart_values_missing_file_returns_empty(tmp_path):
    assert runner._read_chart_values(tmp_path / "does-not-exist.yaml") == {}


def test_read_chart_values_malformed_yaml_returns_empty(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("console: [unterminated\n  - a: b\n:::")
    assert runner._read_chart_values(bad) == {}


def test_read_chart_values_non_mapping_returns_empty(tmp_path):
    p = tmp_path / "list.yaml"
    p.write_text("- a\n- b\n")
    assert runner._read_chart_values(p) == {}


_CONSOLE_VALUES_ENABLED = {
    "console": {
        "enabled": True,
        "librechat": {
            "image": {"repository": "r1", "tag": "t1", "digest": "sha256:d1"}
        },
        "bff": {"image": {"repository": "r2", "tag": "t2", "digest": "sha256:d2"}},
    }
}
_CONSOLE_VALUES_DISABLED = {"console": {"enabled": False}}


def test_helm_apply_cmd_includes_console_sets_when_enabled():
    cfg = DeployConfig(target_version="v9.9.9")
    ref = registry.ImageRef("repo", "9.9.9", "sha256:x", "hub")
    cmd = runner._helm_apply_cmd(cfg, ref, chart_values=_CONSOLE_VALUES_ENABLED)
    joined = " ".join(cmd)
    assert "console.librechat.image.repository=r1" in joined
    assert "console.librechat.image.tag=t1" in joined
    assert "console.librechat.image.digest=sha256:d1" in joined
    assert "console.bff.image.repository=r2" in joined
    assert "console.bff.image.tag=t2" in joined
    assert "console.bff.image.digest=sha256:d2" in joined


def test_helm_apply_cmd_no_console_sets_when_disabled():
    cfg = DeployConfig(target_version="v9.9.9")
    ref = registry.ImageRef("repo", "9.9.9", "sha256:x", "hub")
    cmd = runner._helm_apply_cmd(cfg, ref, chart_values=_CONSOLE_VALUES_DISABLED)
    assert not any(str(a).startswith("console.") for a in cmd)


def test_helm_apply_cmd_defaults_to_reading_real_chart_values_file(monkeypatch):
    """No explicit `chart_values` -> reads from disk via `_read_chart_values`
    — the production path (`phase_chart_apply` never passes `chart_values`)
    actually wires through to the on-disk SSOT, not just the injectable test
    seam. Deliberately does NOT assert on `console.enabled`'s current value
    (mutable chart state) — only that the disk-read function is the one
    consulted."""
    calls: list[tuple] = []
    real_read = runner._read_chart_values

    def _spy(*a, **k):
        calls.append((a, k))
        return real_read(*a, **k)

    monkeypatch.setattr(runner, "_read_chart_values", _spy)
    cfg = DeployConfig(target_version="v9.9.9")
    ref = registry.ImageRef("repo", "9.9.9", "sha256:x", "hub")
    runner._helm_apply_cmd(cfg, ref)
    assert calls, (
        "_helm_apply_cmd must call _read_chart_values() when chart_values is None"
    )


def test_chart_apply_dry_run_includes_console_pins_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner, "_read_chart_values", lambda *a, **k: _CONSOLE_VALUES_ENABLED
    )
    r = DeployRunner(_cfg(tmp_path, dry_run=True))
    r.image_ref = _hub_ref("sha256:abc")
    r.phase_chart_apply()
    assert "console.librechat.image.tag=t1" in r.records[0].command
    assert "console.bff.image.digest=sha256:d2" in r.records[0].command


def test_chart_apply_dry_run_no_console_pins_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner, "_read_chart_values", lambda *a, **k: _CONSOLE_VALUES_DISABLED
    )
    r = DeployRunner(_cfg(tmp_path, dry_run=True))
    r.image_ref = _hub_ref("sha256:abc")
    r.phase_chart_apply()
    assert "console." not in r.records[0].command


def test_chart_apply_flags_helm_failure(tmp_path, monkeypatch):
    disp = _Dispatcher(
        rules=[
            ("imageID", _proc(0, "repo@sha256:OLD")),
            ("helm upgrade", _proc(returncode=1, stderr="timed out")),
        ]
    )
    monkeypatch.setattr(runner, "_run", disp)
    r = DeployRunner(_cfg(tmp_path))
    r.image_ref = _hub_ref("sha256:NEW")
    r.phase_chart_apply()
    assert r.records[0].status == "flagged"


# ── P2 Option A1: reactive adopt of out-of-band Helm objects (#456) ─────────

_OWNERSHIP_STDERR = (
    'Error: INSTALLATION FAILED: Deployment "audittrace-memory-server" in namespace '
    '"audittrace" exists and cannot be imported into the current release: '
    "invalid ownership metadata; annotation validation error: "
    '"meta.helm.sh/release-name" annotation must be "audittrace"\n'
    'ClusterRole "audittrace" exists and cannot be imported into the current '
    "release: invalid ownership metadata"
)


def test_parse_ownership_conflicts_extracts_namespaced_and_cluster_scoped():
    conflicts = runner.parse_ownership_conflicts(_OWNERSHIP_STDERR)
    assert conflicts == [
        ("Deployment", "audittrace-memory-server", "audittrace"),
        ("ClusterRole", "audittrace", None),
    ]


def test_parse_ownership_conflicts_empty_without_marker():
    # Neuter-guard check: a quoted `Kind "name"` substring alone must NOT
    # trigger an adopt — only the tight "invalid ownership metadata" marker.
    assert runner.parse_ownership_conflicts('Deployment "x" in namespace "y"') == []


def test_chart_apply_a1_adopts_both_objects_and_retries_once(tmp_path, monkeypatch):
    helm_calls: list[str] = []
    kubectl_calls: list[list[str]] = []

    def fake_run(cmd, *, env=None):
        joined = " ".join(cmd)
        if "imageID" in joined:
            return _proc(0, "repo@sha256:OLD")
        if "helm upgrade" in joined:
            helm_calls.append(joined)
            if len(helm_calls) == 1:
                return _proc(returncode=1, stderr=_OWNERSHIP_STDERR)
            return _proc(0, "Release has been upgraded")
        if "helm status" in joined:
            return _proc(0, json.dumps({"version": 9}))
        if cmd[0] == "kubectl":
            kubectl_calls.append(cmd)
            return _proc(0)
        return _proc(0)

    monkeypatch.setattr(runner, "_run", fake_run)
    r = DeployRunner(_cfg(tmp_path))
    r.image_ref = _hub_ref("sha256:NEW")
    r.phase_chart_apply()

    assert len(helm_calls) == 2  # exactly one bounded retry
    assert kubectl_calls == [
        [
            "kubectl",
            "label",
            "deployment",
            "audittrace-memory-server",
            "-n",
            "audittrace",
            "app.kubernetes.io/managed-by=Helm",
            "--overwrite",
        ],
        [
            "kubectl",
            "annotate",
            "deployment",
            "audittrace-memory-server",
            "-n",
            "audittrace",
            "meta.helm.sh/release-name=audittrace",
            "--overwrite",
        ],
        [
            "kubectl",
            "annotate",
            "deployment",
            "audittrace-memory-server",
            "-n",
            "audittrace",
            "meta.helm.sh/release-namespace=audittrace",
            "--overwrite",
        ],
        [
            "kubectl",
            "label",
            "clusterrole",
            "audittrace",
            "app.kubernetes.io/managed-by=Helm",
            "--overwrite",
        ],
        [
            "kubectl",
            "annotate",
            "clusterrole",
            "audittrace",
            "meta.helm.sh/release-name=audittrace",
            "--overwrite",
        ],
        [
            "kubectl",
            "annotate",
            "clusterrole",
            "audittrace",
            "meta.helm.sh/release-namespace=audittrace",
            "--overwrite",
        ],
    ]
    assert r.records[0].status == "ok"
    assert r.records[0].evidence["adopted"] == [
        {
            "kind": "Deployment",
            "name": "audittrace-memory-server",
            "namespace": "audittrace",
        },
        {"kind": "ClusterRole", "name": "audittrace", "namespace": None},
    ]


def test_chart_apply_a1_bounded_single_retry_then_flagged(tmp_path, monkeypatch):
    helm_calls: list[str] = []
    kubectl_calls: list[list[str]] = []

    def fake_run(cmd, *, env=None):
        joined = " ".join(cmd)
        if "imageID" in joined:
            return _proc(0, "repo@sha256:OLD")
        if "helm upgrade" in joined:
            helm_calls.append(joined)
            return _proc(returncode=1, stderr=_OWNERSHIP_STDERR)  # fails BOTH times
        if cmd[0] == "kubectl":
            kubectl_calls.append(cmd)
            return _proc(0)
        return _proc(0)

    monkeypatch.setattr(runner, "_run", fake_run)
    r = DeployRunner(_cfg(tmp_path))
    r.image_ref = _hub_ref("sha256:NEW")
    r.phase_chart_apply()

    assert len(helm_calls) == 2  # bounded: retried once, never looped
    assert len(kubectl_calls) == 6  # adopt fired exactly once (not a second time)
    assert r.records[0].status == "flagged"
    assert len(r.records[0].evidence["adopted"]) == 2
    assert "adopted 2 out-of-band object(s)" in r.records[0].detail


def test_chart_apply_a1_no_match_on_unrelated_error(tmp_path, monkeypatch):
    helm_calls: list[str] = []
    kubectl_calls: list[list[str]] = []

    def fake_run(cmd, *, env=None):
        joined = " ".join(cmd)
        if "imageID" in joined:
            return _proc(0, "repo@sha256:OLD")
        if "helm upgrade" in joined:
            helm_calls.append(joined)
            return _proc(
                returncode=1, stderr="Error: timed out waiting for the condition"
            )
        if cmd[0] == "kubectl":
            kubectl_calls.append(cmd)
            return _proc(0)
        return _proc(0)

    monkeypatch.setattr(runner, "_run", fake_run)
    r = DeployRunner(_cfg(tmp_path))
    r.image_ref = _hub_ref("sha256:NEW")
    r.phase_chart_apply()

    assert len(helm_calls) == 1  # no retry — unrelated failure, behaves as today
    assert kubectl_calls == []  # no adopt attempted
    assert r.records[0].status == "flagged"
    assert "adopted" not in r.records[0].evidence


def test_chart_apply_dry_run(tmp_path):
    r = DeployRunner(_cfg(tmp_path, dry_run=True))
    r.image_ref = _hub_ref("sha256:abc")
    r.phase_chart_apply()
    assert r.records[0].status == "planned"
    assert "helm upgrade --install" in r.records[0].command
    assert "9.9.9@sha256:abc" in r.records[0].command


def test_extract_digest_variants():
    assert runner.extract_digest("repo@sha256:abc") == "sha256:abc"
    assert runner.extract_digest("sha256:def") == "sha256:def"
    assert runner.extract_digest("repo:tag") is None
    assert runner.extract_digest(None) is None
    assert runner.extract_digest("") is None


def test_running_image_digest_parsing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner, "_run", lambda *a, **k: _proc(0, "repo:tag repo@sha256:live")
    )
    r = DeployRunner(_cfg(tmp_path))
    assert r._running_image_digest() == "sha256:live"


def test_running_image_digest_none_on_error(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc(returncode=1))
    r = DeployRunner(_cfg(tmp_path))
    assert r._running_image_digest() is None


def test_running_image_digest_none_when_no_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc(0, "repo:tag"))
    r = DeployRunner(_cfg(tmp_path))
    assert r._running_image_digest() is None


def test_running_image_none_on_error(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc(returncode=1))
    r = DeployRunner(_cfg(tmp_path))
    assert r._running_image() is None


def test_running_image_empty_stdout(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc(0, "   "))
    r = DeployRunner(_cfg(tmp_path))
    assert r._running_image() is None


def test_helm_revision_parsers(tmp_path, monkeypatch):
    r = DeployRunner(_cfg(tmp_path))
    monkeypatch.setattr(
        runner, "_run", lambda *a, **k: _proc(0, json.dumps({"version": 3}))
    )
    assert r._helm_revision() == 3
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc(0, "not json"))
    assert r._helm_revision() is None
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc(returncode=1))
    assert r._helm_revision() is None


# ── P3 bootstrap guard ────────────────────────────────────────────────────────


def test_bootstrap_skipped_without_vault_token(tmp_path, monkeypatch):
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc())
    r = DeployRunner(_cfg(tmp_path))
    r.phase_bootstrap()
    assert r.records[0].status == "skipped"


def test_bootstrap_runs_with_vault_token(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_TOKEN", "s.xxx")
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc(0))
    r = DeployRunner(_cfg(tmp_path))
    r.phase_bootstrap()
    assert r.records[0].status == "ok"


def test_bootstrap_flags_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_TOKEN", "s.xxx")
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc(returncode=2))
    r = DeployRunner(_cfg(tmp_path))
    r.phase_bootstrap()
    assert r.records[0].status == "flagged"


def test_bootstrap_dry_run(tmp_path):
    r = DeployRunner(_cfg(tmp_path, dry_run=True))
    r.phase_bootstrap()
    assert r.records[0].status == "planned"


# ── P4 settle + surge assertion ──────────────────────────────────────────────


def test_settle_ok_within_bound(tmp_path, monkeypatch):
    disp = _Dispatcher(
        rules=[
            ("rollout status", _proc(0)),
            ("jsonpath={.spec.replicas}", _proc(0, "1")),
            ("status.phase", _proc(0, "Running")),
        ]
    )
    monkeypatch.setattr(runner, "_run", disp)
    monkeypatch.setattr(runner, "_sleep", lambda s: None)
    r = DeployRunner(_cfg(tmp_path, settle_samples=3))
    r.phase_settle()
    assert r.records[0].status == "ok"
    assert r.surge["peak_running"] == 1 and r.surge["within_bound"] is True


def test_settle_flags_surge(tmp_path, monkeypatch):
    # replicas=1 but two pods Running mid-settle → WS1 violation, must flag.
    disp = _Dispatcher(
        rules=[
            ("rollout status", _proc(0)),
            ("jsonpath={.spec.replicas}", _proc(0, "1")),
            ("status.phase", _proc(0, "Running Running")),
        ]
    )
    monkeypatch.setattr(runner, "_run", disp)
    monkeypatch.setattr(runner, "_sleep", lambda s: None)
    r = DeployRunner(_cfg(tmp_path, settle_samples=2))
    r.phase_settle()
    assert r.records[0].status == "flagged"
    assert r.surge["within_bound"] is False and r.surge["peak_running"] == 2


def test_settle_dry_run(tmp_path):
    r = DeployRunner(_cfg(tmp_path, dry_run=True))
    r.phase_settle()
    assert r.records[0].status == "planned"


def test_deployment_replicas_fallbacks(tmp_path, monkeypatch):
    r = DeployRunner(_cfg(tmp_path))
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc(0, "3"))
    assert r._deployment_replicas() == 3
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc(0, ""))
    assert r._deployment_replicas() == 1
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc(returncode=1))
    assert r._deployment_replicas() == 1
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc(0, "notanint"))
    assert r._deployment_replicas() == 1


def test_sample_pod_phases_error(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc(returncode=1))
    r = DeployRunner(_cfg(tmp_path))
    assert r._sample_pod_phases() == []


# ── report / summary ──────────────────────────────────────────────────────────


def test_human_summary_contains_key_fields(tmp_path):
    r = DeployRunner(_cfg(tmp_path, dry_run=True))
    r.image_ref = registry.ImageRef("repo", "v9.9.9", "sha256:z", "hub")
    r._record("P2-chart-apply", "planned", command="helm upgrade", detail="d")
    report = r.build_report()
    text = r.human_summary(report)
    assert "NEVER self-certifies" in text
    assert "helm upgrade" in text


# ── CLI ───────────────────────────────────────────────────────────────────────


def test_cli_dry_run_returns_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner, "_run", lambda *a, **k: _proc())
    monkeypatch.setattr(registry, "resolve", lambda v, reg: _pinned_ref())
    rc = runner.main(
        ["--target-version", "v9.9.9", "--dry-run", "--out-dir", str(tmp_path / "runs")]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "deploy plan" in out
    assert "P0-preflight" in out


def test_cli_abort_returns_three(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner, "_run", lambda *a, **k: _proc(returncode=4, stderr="istiod down")
    )
    rc = runner.main(
        ["--target-version", "v9.9.9", "--out-dir", str(tmp_path / "runs")]
    )
    assert rc == 3


def test_config_from_args_maps_fields(tmp_path):
    args = runner.build_parser().parse_args(
        [
            "--target-version",
            "v1",
            "--registry",
            "local",
            "--namespace",
            "ns",
            "--timeout",
            "42",
        ]
    )
    cfg = runner.config_from_args(args)
    assert cfg.registry == "local" and cfg.namespace == "ns" and cfg.timeout == 42
    assert cfg.deployment == "audittrace-memory-server"


# ── low-level indirections (executed at least once for coverage honesty) ─────


def test_run_executes_real_subprocess():
    proc = runner._run(["printf", "hi"])
    assert proc.returncode == 0 and proc.stdout == "hi"
    proc2 = runner._run(["printf", "hi"], env={"X": "1"})
    assert proc2.returncode == 0


def test_sleep_and_now_iso():
    runner._sleep(0.0)
    assert "T" in runner._now_iso()


# ── __init__ KUBECONFIG default (#456, Part B) ────────────────────────────────


def test_init_seeds_kubeconfig_when_unset_and_file_present(tmp_path, monkeypatch):
    monkeypatch.delenv("KUBECONFIG", raising=False)
    fake_home = tmp_path / "home"
    (fake_home / ".kube").mkdir(parents=True)
    (fake_home / ".kube" / "config").write_text("apiVersion: v1\n")
    monkeypatch.setattr(runner.Path, "home", classmethod(lambda cls: fake_home))
    DeployRunner(_cfg(tmp_path), mesh_gate=_healthy_gate())
    assert os.environ["KUBECONFIG"] == str(fake_home / ".kube" / "config")


def test_init_does_not_overwrite_already_set_kubeconfig(tmp_path, monkeypatch):
    monkeypatch.setenv("KUBECONFIG", "/custom/kubeconfig")
    fake_home = tmp_path / "home"
    (fake_home / ".kube").mkdir(parents=True)
    (fake_home / ".kube" / "config").write_text("apiVersion: v1\n")
    monkeypatch.setattr(runner.Path, "home", classmethod(lambda cls: fake_home))
    DeployRunner(_cfg(tmp_path), mesh_gate=_healthy_gate())
    assert os.environ["KUBECONFIG"] == "/custom/kubeconfig"


def test_init_leaves_kubeconfig_unset_when_no_file_present(tmp_path, monkeypatch):
    monkeypatch.delenv("KUBECONFIG", raising=False)
    fake_home = tmp_path / "home-without-kube"
    monkeypatch.setattr(runner.Path, "home", classmethod(lambda cls: fake_home))
    DeployRunner(_cfg(tmp_path), mesh_gate=_healthy_gate())
    assert "KUBECONFIG" not in os.environ
