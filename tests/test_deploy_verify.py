"""Unit tests for the INDEPENDENT deploy-verify runner (Component 3 / WS3).

Every external effect is mocked: read-only kubectl/helm (``verify._run``), the
Docker Hub registry (``registry.resolve``), and the front door
(``verify._http_request``). No cluster, no network, no sleeps.

The reviewer's mandate is that every gate can actually FAIL, so each of the five
probes is exercised in BOTH directions — healthy inputs PASS, unhealthy inputs
FAIL — and the overall verdict is proven to go FAIL on any single probe failure.
URL routing in these tests uses ``urlparse(...).path`` exact matching, never a
substring check (the CodeQL ``py/incomplete-url-substring`` class the WS2 tests
tripped).
"""

from __future__ import annotations

import json
import ssl
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from scripts.deploy import registry, verify
from scripts.deploy.verify import (
    COMPONENT_SELECTORS,
    FAIL,
    PASS,
    VerifyConfig,
    VerifyRunner,
    component_readiness,
    is_recall_tool,
    load_token,
    pod_ready,
    ssl_context,
    strategy_matches_intent,
    unready_components,
)

# ── fakes ─────────────────────────────────────────────────────────────────────


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["x"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _cfg(tmp_path, **kw):
    kw.setdefault("out_dir", tmp_path / "runs")
    kw.setdefault("target_version", "v9.9.9")
    return VerifyConfig(**kw)


class _KubeDispatcher:
    """Routes verify._run calls to canned CompletedProcess by command-token match.

    (Command args, NOT URLs — token substring matching is fine here.)
    """

    def __init__(self, rules, default=None):
        self.rules = rules
        self.default = default or _proc(0, "")
        self.calls: list[list[str]] = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        joined = " ".join(cmd)
        for needle, result in self.rules:
            if needle in joined:
                return result
        return self.default


class _FakeHttp:
    """Routes _http_request by (method, exact path). Query parsed, never substringed.

    Records the SSL ``context`` kwarg so tests can prove --insecure selects an
    unverified context and the default (strict) does not.
    """

    def __init__(self, routes):
        # routes: dict[(method, path)] -> (status, json-able body)
        self.routes = routes
        self.calls: list[tuple[str, str]] = []
        self.contexts: list[ssl.SSLContext | None] = []

    def __call__(self, method, url, headers=None, body=None, timeout=30, context=None):
        self.calls.append((method, url))
        self.contexts.append(context)
        parsed = urlparse(url)
        status, obj = self.routes[(method, parsed.path)]
        return status, {}, json.dumps(obj).encode()


def _pod(name, labels=None, ready=True):
    return {
        "metadata": {"name": name, "labels": labels or {}},
        "status": {
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]
        },
    }


def _all_components_ready():
    # One correctly-LABELLED Ready pod per expected component (label match, not name).
    return [
        _pod(f"audittrace-{comp}-0", labels={key: value})
        for comp, (key, value) in COMPONENT_SELECTORS.items()
    ]


def _without_component(comp):
    key, value = COMPONENT_SELECTORS[comp]
    return [
        p for p in _all_components_ready() if p["metadata"]["labels"].get(key) != value
    ]


def _pods_json(pods):
    return _proc(0, json.dumps({"items": pods}))


def _surge_safe_deployment():
    return _proc(
        0,
        json.dumps(
            {
                "spec": {
                    "strategy": {
                        "type": "RollingUpdate",
                        "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1},
                    }
                }
            }
        ),
    )


# ── pure helpers ──────────────────────────────────────────────────────────────


def test_pod_ready_true_false_and_missing():
    assert pod_ready(_pod("x", ready=True)) is True
    assert pod_ready(_pod("x", ready=False)) is False
    assert pod_ready({"status": {"conditions": []}}) is False
    assert pod_ready({}) is False
    # A non-Ready condition BEFORE the Ready one must be skipped, not short-circuit.
    pod = {
        "status": {
            "conditions": [
                {"type": "Initialized", "status": "True"},
                {"type": "Ready", "status": "True"},
            ]
        }
    }
    assert pod_ready(pod) is True


def test_front_post_without_token_omits_auth_header(tmp_path, monkeypatch):
    seen = {}

    def spy(method, url, headers=None, body=None, timeout=30, context=None):
        seen["headers"] = headers
        return 200, {}, b"{}"

    monkeypatch.setattr(verify, "_http_request", spy)
    VerifyRunner(_cfg(tmp_path))._front_post("/x", token=None, payload={"a": 1})
    assert "Authorization" not in seen["headers"]


def test_component_readiness_matches_by_exact_label_not_name():
    selectors = {
        "redis": ("app.kubernetes.io/name", "redis"),
        "minio": ("app.kubernetes.io/component", "minio"),
        "postgresql": ("app.kubernetes.io/name", "postgresql"),
    }
    pods = [
        _pod("audittrace-redis-master-0", labels={"app.kubernetes.io/name": "redis"}),
        _pod(
            "audittrace-minio-0",
            labels={"app.kubernetes.io/component": "minio"},
            ready=False,
        ),
        # A rogue Ready pod whose NAME contains 'redis' but carries a DIFFERENT
        # label value — must NOT count toward redis (the false-PASS the label fix
        # closes). Also does not satisfy postgresql (absent).
        _pod(
            "audittrace-redis-exporter-9",
            labels={"app.kubernetes.io/name": "redis-exporter"},
        ),
    ]
    r = component_readiness(pods, selectors)
    assert r["redis"] == {"pods": 1, "ready": 1}  # only the labelled redis pod
    assert r["minio"] == {"pods": 1, "ready": 0}
    assert r["postgresql"] == {"pods": 0, "ready": 0}
    assert set(unready_components(r)) == {"minio", "postgresql"}


def test_strategy_matches_intent_ok_and_string_form():
    ok, reason = strategy_matches_intent(
        {
            "type": "RollingUpdate",
            "rollingUpdate": {"maxSurge": "0", "maxUnavailable": "1"},
        }
    )
    assert ok is True and "surge-safe" in reason


def test_strategy_matches_intent_wrong_type():
    ok, reason = strategy_matches_intent({"type": "Recreate"})
    assert ok is False and "strategy.type" in reason


def test_strategy_matches_intent_surge_drift():
    ok, reason = strategy_matches_intent(
        {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 1}}
    )
    assert ok is False and "maxSurge" in reason


def test_strategy_matches_intent_unavailable_drift():
    ok, reason = strategy_matches_intent(
        {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 0}}
    )
    assert ok is False and "maxUnavailable" in reason


def test_is_recall_tool():
    assert is_recall_tool("recall_semantic") is True
    assert is_recall_tool("recall_episodic") is True
    assert is_recall_tool("index_document") is False


def test_parse_json_valid_and_invalid():
    assert verify._parse_json(b'{"a":1}') == {"a": 1}
    assert verify._parse_json(b"not json") is None


def test_normalize_front_door_ok_strips_trailing_slash():
    assert (
        verify._normalize_front_door("https://audittrace.example/")
        == "https://audittrace.example"
    )


@pytest.mark.parametrize("bad", ["ftp://host", "https:///no-host", "not-a-url"])
def test_normalize_front_door_rejects_bad(bad):
    with pytest.raises(ValueError):
        verify._normalize_front_door(bad)


def test_ssl_context_strict_by_default_and_unverified_when_insecure():
    assert ssl_context(False) is None  # strict: system trust store, hostname verified
    ctx = ssl_context(True)
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


def test_load_token_env_first(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDITTRACE_TOKEN", "env-tok")
    assert load_token(tmp_path / "nope.json") == "env-tok"


def test_load_token_from_file(monkeypatch, tmp_path):
    monkeypatch.delenv("AUDITTRACE_TOKEN", raising=False)
    f = tmp_path / "tokens.json"
    f.write_text(json.dumps({"access_token": "file-tok"}))
    assert load_token(f) == "file-tok"


def test_load_token_missing_file_and_bad_json(monkeypatch, tmp_path):
    monkeypatch.delenv("AUDITTRACE_TOKEN", raising=False)
    assert load_token(tmp_path / "absent.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_token(bad) is None
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"refresh_token": "x"}))
    assert load_token(empty) is None


# ── probe 1: pods-ready (PASS + FAIL) ────────────────────────────────────────


def test_pods_ready_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "_run", lambda cmd: _pods_json(_all_components_ready()))
    res = VerifyRunner(_cfg(tmp_path)).probe_pods_ready()
    assert res.status == PASS


def test_pods_ready_fail_missing_component(tmp_path, monkeypatch):
    pods = _without_component("keycloak")
    monkeypatch.setattr(verify, "_run", lambda cmd: _pods_json(pods))
    res = VerifyRunner(_cfg(tmp_path)).probe_pods_ready()
    assert res.status == FAIL and "keycloak" in res.detail


def test_pods_ready_fail_not_ready(tmp_path, monkeypatch):
    pods = _without_component("memory-server")
    key, value = COMPONENT_SELECTORS["memory-server"]
    pods.append(_pod("audittrace-memory-server-0", labels={key: value}, ready=False))
    monkeypatch.setattr(verify, "_run", lambda cmd: _pods_json(pods))
    res = VerifyRunner(_cfg(tmp_path)).probe_pods_ready()
    assert res.status == FAIL and "memory-server" in res.detail


def test_pods_ready_rogue_same_name_pod_does_not_mask_down_component(
    tmp_path, monkeypatch
):
    """Certifier correctness: the REAL redis pod is down/absent, but a rogue Ready
    pod whose NAME contains 'redis' (a metrics exporter) carries a different label
    value. Name-substring matching would have counted it → false PASS. Exact-label
    matching must FAIL redis."""
    pods = _without_component("redis")
    pods.append(
        _pod(
            "audittrace-redis-exporter-0",
            labels={"app.kubernetes.io/name": "redis-exporter"},
            ready=True,
        )
    )
    monkeypatch.setattr(verify, "_run", lambda cmd: _pods_json(pods))
    res = VerifyRunner(_cfg(tmp_path)).probe_pods_ready()
    assert res.status == FAIL and "redis" in res.detail


def test_pods_ready_fail_no_pods(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "_run", lambda cmd: _proc(returncode=1))
    res = VerifyRunner(_cfg(tmp_path)).probe_pods_ready()
    assert res.status == FAIL and "no pods" in res.detail


def test_pods_json_handles_non_dict(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "_run", lambda cmd: _proc(0, "[]"))
    assert VerifyRunner(_cfg(tmp_path))._pods_json() == []
    monkeypatch.setattr(
        verify, "_run", lambda cmd: _proc(0, json.dumps({"items": "x"}))
    )
    assert VerifyRunner(_cfg(tmp_path))._pods_json() == []


# ── probe 2: digest-matches-published (PASS + FAIL) ──────────────────────────


def _hub_ref(digest="sha256:pub"):
    return registry.ImageRef(
        "docker.io/lfds/audittrace-memory-server", "9.9.9", digest, "hub"
    )


def test_digest_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "resolve", lambda v, reg: _hub_ref("sha256:pub"))
    monkeypatch.setattr(verify, "_run", lambda cmd: _proc(0, "repo@sha256:pub"))
    res = VerifyRunner(_cfg(tmp_path)).probe_digest_matches_published()
    assert res.status == PASS


def test_digest_fail_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(
        registry, "resolve", lambda v, reg: _hub_ref("sha256:PUBLISHED")
    )
    monkeypatch.setattr(verify, "_run", lambda cmd: _proc(0, "repo@sha256:LIVE"))
    res = VerifyRunner(_cfg(tmp_path)).probe_digest_matches_published()
    assert res.status == FAIL and "does NOT match" in res.detail


def test_digest_fail_resolution_error(tmp_path, monkeypatch):
    def boom(v, reg):
        raise registry.DigestResolutionError("HTTP 404")

    monkeypatch.setattr(registry, "resolve", boom)
    res = VerifyRunner(_cfg(tmp_path)).probe_digest_matches_published()
    assert res.status == FAIL and "could not resolve" in res.detail


def test_digest_fail_unpinned_local(tmp_path, monkeypatch):
    ref = registry.ImageRef(
        "localhost:5000/audittrace/memory-server", "9.9.9", None, "local"
    )
    monkeypatch.setattr(registry, "resolve", lambda v, reg: ref)
    res = VerifyRunner(
        _cfg(tmp_path, registry="local")
    ).probe_digest_matches_published()
    assert res.status == FAIL and "no digest" in res.detail


def test_digest_fail_no_live_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "resolve", lambda v, reg: _hub_ref("sha256:pub"))
    monkeypatch.setattr(verify, "_run", lambda cmd: _proc(returncode=1))
    res = VerifyRunner(_cfg(tmp_path)).probe_digest_matches_published()
    assert res.status == FAIL and "no live memory-server pod digest" in res.detail


def test_live_digest_none_when_no_sha(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "_run", lambda cmd: _proc(0, "repo:tag"))
    assert VerifyRunner(_cfg(tmp_path))._live_memory_server_digest() is None


# ── probe 3: no-drift (PASS + FAIL) ──────────────────────────────────────────


def _drift_dispatcher(status="deployed", deployment=None):
    return _KubeDispatcher(
        rules=[
            ("helm status", _proc(0, json.dumps({"info": {"status": status}}))),
            ("get deployment", deployment or _surge_safe_deployment()),
        ]
    )


def test_no_drift_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "_run", _drift_dispatcher())
    res = VerifyRunner(_cfg(tmp_path)).probe_no_drift()
    assert res.status == PASS


def test_no_drift_fail_release_not_deployed(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "_run", _drift_dispatcher(status="pending-upgrade"))
    res = VerifyRunner(_cfg(tmp_path)).probe_no_drift()
    assert res.status == FAIL and "pending-upgrade" in res.detail


def test_no_drift_fail_strategy_unreadable(tmp_path, monkeypatch):
    disp = _KubeDispatcher(
        rules=[
            ("helm status", _proc(0, json.dumps({"info": {"status": "deployed"}}))),
            ("get deployment", _proc(returncode=1)),
        ]
    )
    monkeypatch.setattr(verify, "_run", disp)
    res = VerifyRunner(_cfg(tmp_path)).probe_no_drift()
    assert res.status == FAIL and "could not read" in res.detail


def test_no_drift_fail_surge_reintroduced(tmp_path, monkeypatch):
    drifted = _proc(
        0,
        json.dumps(
            {
                "spec": {
                    "strategy": {
                        "type": "RollingUpdate",
                        "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 1},
                    }
                }
            }
        ),
    )
    monkeypatch.setattr(verify, "_run", _drift_dispatcher(deployment=drifted))
    res = VerifyRunner(_cfg(tmp_path)).probe_no_drift()
    assert res.status == FAIL and "drift" in res.detail


def test_helm_status_and_strategy_none_on_error(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "_run", lambda cmd: _proc(returncode=1))
    r = VerifyRunner(_cfg(tmp_path))
    assert r._helm_status() is None
    assert r._deployment_strategy() is None
    # non-dict json bodies also degrade to None
    monkeypatch.setattr(verify, "_run", lambda cmd: _proc(0, "[]"))
    assert r._helm_status() is None
    assert r._deployment_strategy() is None


# ── probe 4: health-version (PASS + FAIL) ────────────────────────────────────


def test_health_version_pass(tmp_path, monkeypatch):
    http = _FakeHttp({("GET", "/health"): (200, {"status": "ok", "version": "9.9.9"})})
    monkeypatch.setattr(verify, "_http_request", http)
    res = VerifyRunner(_cfg(tmp_path)).probe_health_version()
    assert res.status == PASS


def test_health_version_fail_http_error(tmp_path, monkeypatch):
    http = _FakeHttp({("GET", "/health"): (503, {})})
    monkeypatch.setattr(verify, "_http_request", http)
    res = VerifyRunner(_cfg(tmp_path)).probe_health_version()
    assert res.status == FAIL and "503" in res.detail


def test_health_version_fail_not_ok(tmp_path, monkeypatch):
    http = _FakeHttp(
        {("GET", "/health"): (200, {"status": "degraded", "version": "9.9.9"})}
    )
    monkeypatch.setattr(verify, "_http_request", http)
    res = VerifyRunner(_cfg(tmp_path)).probe_health_version()
    assert res.status == FAIL and "degraded" in res.detail


def test_health_version_fail_wrong_version(tmp_path, monkeypatch):
    http = _FakeHttp({("GET", "/health"): (200, {"status": "ok", "version": "1.2.3"})})
    monkeypatch.setattr(verify, "_http_request", http)
    res = VerifyRunner(_cfg(tmp_path)).probe_health_version()
    assert res.status == FAIL and "does NOT match" in res.detail


# ── probe 5: recall-e2e (PASS + FAIL) ────────────────────────────────────────


def _recall_routes(tool_names=("recall_semantic",), interaction_id=42):
    return {
        ("POST", "/v1/chat/completions"): (
            200,
            {"choices": [{"message": {"content": "hi"}}]},
        ),
        ("GET", "/interactions"): (200, {"interactions": [{"id": interaction_id}]}),
        ("GET", f"/interactions/{interaction_id}/tool-calls"): (
            200,
            {"tool_calls": [{"tool_name": n} for n in tool_names]},
        ),
    }


def test_recall_e2e_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "load_token", lambda tf: "tok")
    monkeypatch.setattr(verify, "_http_request", _FakeHttp(_recall_routes()))
    res = VerifyRunner(_cfg(tmp_path)).probe_recall_e2e()
    assert res.status == PASS and res.evidence["interaction_id"] == 42


def test_recall_e2e_fail_no_token(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "load_token", lambda tf: None)
    res = VerifyRunner(_cfg(tmp_path)).probe_recall_e2e()
    assert res.status == FAIL and "no scoped JWT" in res.detail


def test_recall_e2e_fail_chat_non_200(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "load_token", lambda tf: "tok")
    routes = _recall_routes()
    routes[("POST", "/v1/chat/completions")] = (500, {})
    monkeypatch.setattr(verify, "_http_request", _FakeHttp(routes))
    res = VerifyRunner(_cfg(tmp_path)).probe_recall_e2e()
    assert res.status == FAIL and "500" in res.detail


def test_recall_e2e_fail_no_interactions(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "load_token", lambda tf: "tok")
    routes = _recall_routes()
    routes[("GET", "/interactions")] = (200, {"interactions": []})
    monkeypatch.setattr(verify, "_http_request", _FakeHttp(routes))
    res = VerifyRunner(_cfg(tmp_path)).probe_recall_e2e()
    assert res.status == FAIL and "no interaction" in res.detail


def test_recall_e2e_fail_toolcalls_non_200(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "load_token", lambda tf: "tok")
    routes = _recall_routes()
    routes[("GET", "/interactions/42/tool-calls")] = (503, {})
    monkeypatch.setattr(verify, "_http_request", _FakeHttp(routes))
    res = VerifyRunner(_cfg(tmp_path)).probe_recall_e2e()
    assert res.status == FAIL and "503" in res.detail


def test_recall_e2e_fail_recall_not_recorded(tmp_path, monkeypatch):
    # The interaction is recorded but NO recall_* tool call landed in the audit trail.
    monkeypatch.setattr(verify, "load_token", lambda tf: "tok")
    routes = _recall_routes(tool_names=("index_document",))
    monkeypatch.setattr(verify, "_http_request", _FakeHttp(routes))
    res = VerifyRunner(_cfg(tmp_path)).probe_recall_e2e()
    assert res.status == FAIL and "NO recall_*" in res.detail


# ── verdict aggregation + independence ───────────────────────────────────────


def _all_pass_runner(tmp_path, monkeypatch, interaction_id=42):
    monkeypatch.setattr(registry, "resolve", lambda v, reg: _hub_ref("sha256:pub"))
    monkeypatch.setattr(verify, "load_token", lambda tf: "tok")
    kube = _KubeDispatcher(
        rules=[
            ("imageID", _proc(0, "repo@sha256:pub")),
            ("get pods", _pods_json(_all_components_ready())),
            ("helm status", _proc(0, json.dumps({"info": {"status": "deployed"}}))),
            ("get deployment", _surge_safe_deployment()),
        ]
    )
    monkeypatch.setattr(verify, "_run", kube)
    monkeypatch.setattr(
        verify,
        "_http_request",
        _FakeHttp(
            {
                ("GET", "/health"): (200, {"status": "ok", "version": "9.9.9"}),
                **_recall_routes(interaction_id=interaction_id),
            }
        ),
    )
    return VerifyRunner(_cfg(tmp_path))


def test_verdict_pass_when_all_probes_pass(tmp_path, monkeypatch):
    report = _all_pass_runner(tmp_path, monkeypatch).run()
    assert report["verdict"] == PASS
    assert [p["name"] for p in report["probes"]] == list(verify.PROBES)


def test_verdict_fail_when_single_probe_fails(tmp_path, monkeypatch):
    r = _all_pass_runner(tmp_path, monkeypatch)
    # Break exactly one probe: live digest no longer matches published.
    kube = _KubeDispatcher(
        rules=[
            ("imageID", _proc(0, "repo@sha256:DIFFERENT")),
            ("get pods", _pods_json(_all_components_ready())),
            ("helm status", _proc(0, json.dumps({"info": {"status": "deployed"}}))),
            ("get deployment", _surge_safe_deployment()),
        ]
    )
    monkeypatch.setattr(verify, "_run", kube)
    report = r.run()
    assert report["verdict"] == FAIL
    statuses = {p["name"]: p["status"] for p in report["probes"]}
    assert statuses["digest-matches-published"] == FAIL
    # the other four still PASS — one FAIL is enough to fail the verdict
    assert statuses["pods-ready"] == PASS


def test_verdict_is_fail_with_no_results(tmp_path):
    assert VerifyRunner(_cfg(tmp_path)).verdict == FAIL


def test_report_declares_independence_derived_from_sources(tmp_path, monkeypatch):
    report = _all_pass_runner(tmp_path, monkeypatch).run()
    # reads_deploy_report is DERIVED from the enumerated evidence sources, not a
    # hardcoded self-attestation — and none of those sources is a deploy report.
    assert report["reads_deploy_report"] is False
    assert report["evidence_sources"]
    assert not any(
        s.startswith(verify.DEPLOY_REPORT_SOURCE_PREFIX)
        for s in report["evidence_sources"]
    )
    assert "never read" in report["independence"]
    assert report["runner"] == "audittrace-verify-runner"


def test_reads_deploy_report_flips_true_if_a_deploy_source_is_added(
    tmp_path, monkeypatch
):
    """The derived flag is honest: if a deploy-report source were ever added to
    the evidence list, reads_deploy_report would compute True (it is not a
    hardcoded constant)."""
    r = VerifyRunner(_cfg(tmp_path))
    monkeypatch.setattr(
        r,
        "evidence_sources",
        lambda: ["cluster-api:kubectl:get-pods", "deploy-report:/runs/deploy.json"],
    )
    assert r.reads_deploy_report is True


def test_runner_never_reads_a_deploy_runner_report(tmp_path, monkeypatch):
    """Independence (spy, secondary guarantee): seed a deploy report on disk, run
    the verifier, prove it is never opened. The spy patches ALL of pathlib's read
    paths (``Path.read_text`` / ``Path.open``) AND ``builtins.open`` — the module
    reads via ``pathlib``, which bypasses a builtins-only spy, so a builtins-only
    spy was non-falsifiable (a ``Path.read_text`` of the report would slip past)."""
    runs = tmp_path / "runs"
    runs.mkdir()
    deploy_report = runs / "deploy-v9.9.9-fake.json"
    deploy_report.write_text(json.dumps({"certified": True, "verdict": "PASS"}))

    reads: list[str] = []
    real_open = open
    real_read_text = Path.read_text
    real_path_open = Path.open

    def spy_open(file, *a, **kw):
        reads.append(str(file))
        return real_open(file, *a, **kw)

    def spy_read_text(self, *a, **kw):
        reads.append(str(self))
        return real_read_text(self, *a, **kw)

    def spy_path_open(self, *a, **kw):
        reads.append(str(self))
        return real_path_open(self, *a, **kw)

    monkeypatch.setattr("builtins.open", spy_open)
    monkeypatch.setattr(Path, "read_text", spy_read_text)
    monkeypatch.setattr(Path, "open", spy_path_open)
    report = _all_pass_runner(tmp_path, monkeypatch).run()

    assert report["verdict"] == PASS
    # The deploy report path was NEVER read through ANY read path.
    assert not any(str(deploy_report) == p for p in reads)


def test_hostile_deploy_report_cannot_move_the_verdict(tmp_path, monkeypatch):
    """THE real independence guarantee (property test, stronger than the spy).

    A HOSTILE deploy-runner report on disk claims verdict PASS / certified true,
    while EVERY re-derived health signal is FAILING (pods down, digest mismatch,
    /health down, recall not recorded). The verify runner's verdict MUST be FAIL —
    proving the verdict is derived ONLY from re-derived health and is completely
    unmoved by any on-disk deploy report."""
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "deploy-v9.9.9-hostile.json").write_text(
        json.dumps({"certified": True, "verdict": "PASS", "healthy": True})
    )

    # All cluster reads fail; registry resolves but live digest unreadable; front
    # door is down; no token → every probe FAILs.
    monkeypatch.setattr(verify, "_run", lambda cmd: _proc(returncode=1))
    monkeypatch.setattr(registry, "resolve", lambda v, reg: _hub_ref("sha256:pub"))
    monkeypatch.setattr(verify, "load_token", lambda tf: None)
    monkeypatch.setattr(verify, "_http_request", lambda *a, **k: (0, {}, b""))

    report = VerifyRunner(_cfg(tmp_path)).run()

    assert report["verdict"] == FAIL
    # every single probe FAILed — the green deploy report moved nothing
    assert all(p["status"] == FAIL for p in report["probes"])
    assert report["reads_deploy_report"] is False


def test_write_report_emits_bundle(tmp_path, monkeypatch):
    report = _all_pass_runner(tmp_path, monkeypatch).run()
    jsons = list((tmp_path / "runs").glob("verify-v9.9.9-*.json"))
    txts = list((tmp_path / "runs").glob("verify-v9.9.9-*.txt"))
    assert len(jsons) == 1 and len(txts) == 1
    assert json.loads(jsons[0].read_text())["verdict"] == report["verdict"]


def test_human_summary_shows_verdict(tmp_path, monkeypatch):
    r = _all_pass_runner(tmp_path, monkeypatch)
    r.run()
    text = r.human_summary(r.build_report())
    assert "VERDICT: PASS" in text
    assert "reads deploy report : False" in text


# ── front-door query correctness (limit propagated) ──────────────────────────


def test_interactions_query_carries_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "load_token", lambda tf: "tok")
    http = _FakeHttp(_recall_routes())
    monkeypatch.setattr(verify, "_http_request", http)
    VerifyRunner(_cfg(tmp_path, interactions_limit=7)).probe_recall_e2e()
    # find the GET /interactions call and check its parsed query, not a substring.
    calls = [
        u for (m, u) in http.calls if m == "GET" and urlparse(u).path == "/interactions"
    ]
    assert calls and parse_qs(urlparse(calls[0]).query)["limit"] == ["7"]


# ── --insecure TLS opt-in (front-door only, strict by default) ───────────────


def test_default_uses_strict_tls_context(tmp_path, monkeypatch):
    http = _FakeHttp({("GET", "/health"): (200, {"status": "ok", "version": "9.9.9"})})
    monkeypatch.setattr(verify, "_http_request", http)
    VerifyRunner(_cfg(tmp_path)).probe_health_version()
    # strict: no custom SSL context passed → urllib verifies against system trust
    assert http.contexts == [None]


def test_insecure_selects_unverified_tls_context(tmp_path, monkeypatch):
    http = _FakeHttp({("GET", "/health"): (200, {"status": "ok", "version": "9.9.9"})})
    monkeypatch.setattr(verify, "_http_request", http)
    VerifyRunner(_cfg(tmp_path, insecure=True)).probe_health_version()
    assert len(http.contexts) == 1
    ctx = http.contexts[0]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is False and ctx.verify_mode == ssl.CERT_NONE


def test_cli_insecure_flag_sets_config(tmp_path):
    args = verify.build_parser().parse_args(
        [
            "--target-version",
            "v9.9.9",
            "--front-door",
            "https://audittrace.example",
            "--insecure",
        ]
    )
    assert verify.config_from_args(args).insecure is True
    # and the default omits it
    args2 = verify.build_parser().parse_args(
        ["--target-version", "v9.9.9", "--front-door", "https://audittrace.example"]
    )
    assert verify.config_from_args(args2).insecure is False


# ── CLI ───────────────────────────────────────────────────────────────────────


def test_cli_returns_zero_on_pass(tmp_path, monkeypatch, capsys):
    _all_pass_runner(tmp_path, monkeypatch)  # installs the mocks
    rc = verify.main(
        [
            "--target-version",
            "v9.9.9",
            "--front-door",
            "https://audittrace.example",
            "--out-dir",
            str(tmp_path / "runs"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "VERDICT: PASS" in out
    assert "reads_deploy_report=False" in out


def test_cli_returns_one_on_fail(tmp_path, monkeypatch):
    # No mocks for the cluster/front door → every probe fails → verdict FAIL → rc 1.
    monkeypatch.setattr(verify, "_run", lambda cmd: _proc(returncode=1))
    monkeypatch.setattr(registry, "resolve", lambda v, reg: _hub_ref("sha256:pub"))
    monkeypatch.setattr(verify, "load_token", lambda tf: None)
    monkeypatch.setattr(verify, "_http_request", lambda *a, **k: (0, {}, b""))
    rc = verify.main(
        [
            "--target-version",
            "v9.9.9",
            "--front-door",
            "https://audittrace.example",
            "--out-dir",
            str(tmp_path / "runs"),
        ]
    )
    assert rc == 1


def test_config_from_args_maps_and_normalizes(tmp_path):
    args = verify.build_parser().parse_args(
        [
            "--target-version",
            "v1.13.0",
            "--registry",
            "local",
            "--namespace",
            "ns",
            "--front-door",
            "https://audittrace.example/",
        ]
    )
    cfg = verify.config_from_args(args)
    assert cfg.registry == "local" and cfg.namespace == "ns"
    assert cfg.front_door == "https://audittrace.example"  # trailing slash stripped
    assert cfg.image_tag == "1.13.0" and cfg.deployment == "audittrace-memory-server"


# ── low-level indirections (executed once for coverage honesty) ──────────────


def test_run_executes_real_subprocess():
    proc = verify._run(["printf", "hi"])
    assert proc.returncode == 0 and proc.stdout == "hi"


def test_now_iso_has_t():
    assert "T" in verify._now_iso()


# ── WS5: transport-timeout hardening at the REAL egress seam ─────────────────
#
# The live fault-injection finding: a front-door READ timeout raises a bare
# ``TimeoutError`` (== ``socket.timeout``, an ``OSError`` that is NOT a
# ``URLError``), so the former ``except (HTTPError, URLError)`` let it escape and
# CRASHED the verify runner with no verdict. These tests drive the REAL
# ``_http_request`` (patching ``urllib.request.urlopen`` to raise) and are
# FALSIFIABLE: revert the seam to ``except URLError`` and they raise instead of
# producing the clean sentinel / FAIL verdict.


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("read timed out"),  # == socket.timeout on a read timeout
        ConnectionResetError("peer reset"),
        OSError("network unreachable"),
    ],
)
def test_http_request_maps_transport_errors_to_status_zero(monkeypatch, exc):
    def _raise(*a, **k):
        raise exc

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    status, headers, body = verify._http_request(
        "GET", "https://audittrace.example/health"
    )
    assert status == 0 and headers == {} and body == b""


def test_read_timeout_fails_probe_and_still_produces_verdict_and_report(
    tmp_path, monkeypatch
):
    """A front-door READ timeout at the REAL seam → the HTTP probes FAIL, the
    runner STILL emits a verdict (FAIL) and writes a report bundle, and NO
    exception escapes ``run()``. This is the WS5 crash, now a clean failure."""

    def _timeout(*a, **k):
        raise TimeoutError("read timed out")

    # urlopen times out for every front-door call; cluster reads fail; a token
    # exists so recall-e2e actually reaches the (timing-out) front door.
    monkeypatch.setattr("urllib.request.urlopen", _timeout)
    monkeypatch.setattr(verify, "_run", lambda cmd: _proc(returncode=1))
    monkeypatch.setattr(registry, "resolve", lambda v, reg: _hub_ref("sha256:pub"))
    monkeypatch.setattr(verify, "load_token", lambda tf: "tok")

    report = VerifyRunner(_cfg(tmp_path)).run()  # must NOT raise

    assert report["verdict"] == FAIL
    statuses = {p["name"]: p["status"] for p in report["probes"]}
    # both front-door probes fail cleanly on the transport timeout
    assert statuses["health-version"] == FAIL
    assert statuses["recall-e2e"] == FAIL
    # the verdict is complete (all five probes ran) and a bundle was written
    assert [p["name"] for p in report["probes"]] == list(verify.PROBES)
    assert list((tmp_path / "runs").glob("verify-v9.9.9-*.json"))


def test_health_probe_read_timeout_fails_via_real_seam(tmp_path, monkeypatch):
    """Narrow proof: a single probe whose HTTP call times out records FAIL (status
    sentinel 0), never an uncaught exception."""

    def _timeout(*a, **k):
        raise TimeoutError("read timed out")

    monkeypatch.setattr("urllib.request.urlopen", _timeout)
    res = VerifyRunner(_cfg(tmp_path)).probe_health_version()
    assert res.status == FAIL
    assert res.evidence["http_status"] == 0
