"""Unit tests for the deterministic deploy runner (Component 2 / WS2).

Every external effect — subprocess (helm/kubectl/make/preflight) and the Docker
Hub registry HTTP call — is mocked. No cluster, no network, no sleeps. The
reviewer's mandate is to prove each gate can FAIL, so the surge assertion and
the preflight abort paths are exercised in both directions.
"""

from __future__ import annotations

import json
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
    disp = _Dispatcher(
        rules=[
            # live pod imageID carries the SAME digest -> converged
            ("imageID", _proc(0, "docker.io/lfds/audittrace-memory-server@sha256:x")),
            ("helm status", _proc(0, json.dumps({"version": 7}))),
        ]
    )
    monkeypatch.setattr(runner, "_run", disp)
    r = DeployRunner(_cfg(tmp_path))
    r.image_ref = _hub_ref("sha256:x")
    r.phase_chart_apply()
    assert r.converged is True and r.records[0].status == "noop"
    assert not any("helm upgrade" in " ".join(c) for c in disp.calls)


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
    disp = _Dispatcher(
        rules=[
            ("get deployment", _proc(0, intended)),  # tag-string convergence path
            ("helm status", _proc(0, json.dumps({"version": 3}))),
        ]
    )
    monkeypatch.setattr(runner, "_run", disp)
    r = DeployRunner(_cfg(tmp_path, registry="local"))
    r.image_ref = registry.ImageRef(
        "localhost:5000/audittrace/memory-server", "9.9.9", None, "local"
    )
    r.phase_chart_apply()
    assert r.converged is True and r.records[0].status == "noop"


def test_apply_image_tag_pins_by_digest_and_local_plain():
    cfg = DeployConfig(target_version="v1.13.0")
    pinned = registry.ImageRef("repo", "1.13.0", "sha256:zz", "hub")
    assert runner.apply_image_tag(cfg, pinned) == "1.13.0@sha256:zz"
    unpinned = registry.ImageRef("repo", "1.13.0", None, "local")
    assert runner.apply_image_tag(cfg, unpinned) == "1.13.0"


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
