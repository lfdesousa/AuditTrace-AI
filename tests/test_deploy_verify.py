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

from scripts.deploy import mesh, registry, verify
from scripts.deploy.verify import (
    COMPONENT_SELECTORS,
    FAIL,
    PASS,
    VerifyConfig,
    VerifyRunner,
    component_readiness,
    container_is_ready,
    containers_with_exit_code,
    count_unhealthy_upstream,
    crashlooping_containers,
    is_recall_tool,
    load_token,
    pod_annotations,
    pod_has_container,
    pod_name,
    pod_ready,
    sidecar_cert_findings,
    ssl_context,
    strategy_matches_intent,
    unready_components,
    vault_render_findings,
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


class _TimeoutSpy:
    """Routes _http_request by (method, exact path) and RECORDS the ``timeout``
    kwarg used for every call.

    Distinct from ``_FakeHttp``: this fake exists specifically to prove the
    e2e-timeout-calibration invariant — that the recall-e2e CHAT probe gets a
    DIFFERENT (configurable) timeout from every other call made in the SAME
    probe run, and that fast probes never see it widened.
    """

    def __init__(self, routes):
        self.routes = routes
        self.timeouts: list[tuple[str, str, int]] = []  # (method, path, timeout)

    def __call__(self, method, url, headers=None, body=None, timeout=30, context=None):
        parsed = urlparse(url)
        self.timeouts.append((method, parsed.path, timeout))
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


# ── e2e-timeout calibration: _front_post default vs override ─────────────────
# (v1.17.0 SPEC — "calibrate probe only": the chat probe alone gets a wider,
# configurable timeout; every other request keeps FAST_PROBE_TIMEOUT.)


def test_front_post_default_timeout_is_the_fast_probe_bound(tmp_path, monkeypatch):
    seen = {}

    def spy(method, url, headers=None, body=None, timeout=30, context=None):
        seen["timeout"] = timeout
        return 200, {}, b"{}"

    monkeypatch.setattr(verify, "_http_request", spy)
    VerifyRunner(_cfg(tmp_path))._front_post("/x", token=None, payload={"a": 1})
    assert seen["timeout"] == verify.FAST_PROBE_TIMEOUT == 30


def test_front_post_honors_an_explicit_timeout_override(tmp_path, monkeypatch):
    seen = {}

    def spy(method, url, headers=None, body=None, timeout=30, context=None):
        seen["timeout"] = timeout
        return 200, {}, b"{}"

    monkeypatch.setattr(verify, "_http_request", spy)
    VerifyRunner(_cfg(tmp_path))._front_post(
        "/x", token=None, payload={"a": 1}, timeout=999
    )
    assert seen["timeout"] == 999


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


# ── e2e-timeout calibration: recall-e2e chat probe ONLY ───────────────────────
# The reviewer mandate (SPEC §4): confirm the calibrated timeout applies ONLY
# to the chat probe, confirm the override takes effect, and confirm the pass
# criteria are unchanged. This block is written so that hardcoding the chat
# call back to FAST_PROBE_TIMEOUT (neutering the calibration) turns it red.


def test_verify_config_default_e2e_timeout_is_120s(tmp_path):
    assert _cfg(tmp_path).e2e_timeout == 120 == verify.E2E_CHAT_TIMEOUT_DEFAULT


def test_recall_e2e_chat_probe_uses_the_configured_e2e_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "load_token", lambda tf: "tok")
    spy = _TimeoutSpy(_recall_routes())
    monkeypatch.setattr(verify, "_http_request", spy)
    res = VerifyRunner(_cfg(tmp_path, e2e_timeout=77)).probe_recall_e2e()
    assert res.status == PASS
    chat_timeout = next(t for m, p, t in spy.timeouts if p == "/v1/chat/completions")
    assert chat_timeout == 77
    # Falsifiable: every OTHER call in the SAME probe run (interactions list,
    # tool-calls) must keep the FAST bound, unwidened — hardcoding a single
    # shared timeout for the whole probe would turn this red.
    other_timeouts = [t for m, p, t in spy.timeouts if p != "/v1/chat/completions"]
    assert other_timeouts and all(
        t == verify.FAST_PROBE_TIMEOUT for t in other_timeouts
    )


def test_recall_e2e_default_run_uses_120s_for_the_chat_call_only(tmp_path, monkeypatch):
    # No e2e_timeout override — the calibrated DEFAULT (120s) must reach the
    # chat call, and only the chat call.
    monkeypatch.setattr(verify, "load_token", lambda tf: "tok")
    spy = _TimeoutSpy(_recall_routes())
    monkeypatch.setattr(verify, "_http_request", spy)
    VerifyRunner(_cfg(tmp_path)).probe_recall_e2e()
    chat_timeout = next(t for m, p, t in spy.timeouts if p == "/v1/chat/completions")
    assert chat_timeout == verify.E2E_CHAT_TIMEOUT_DEFAULT == 120
    other_timeouts = [t for m, p, t in spy.timeouts if p != "/v1/chat/completions"]
    assert other_timeouts and all(
        t == verify.FAST_PROBE_TIMEOUT for t in other_timeouts
    )


def test_recall_e2e_evidence_records_the_timeout_used_on_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "load_token", lambda tf: "tok")
    monkeypatch.setattr(verify, "_http_request", _FakeHttp(_recall_routes()))
    res = VerifyRunner(_cfg(tmp_path, e2e_timeout=55)).probe_recall_e2e()
    assert res.status == PASS and res.evidence["e2e_timeout_s"] == 55


def test_recall_e2e_evidence_records_the_timeout_used_on_chat_fail(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(verify, "load_token", lambda tf: "tok")
    routes = _recall_routes()
    routes[("POST", "/v1/chat/completions")] = (500, {})
    monkeypatch.setattr(verify, "_http_request", _FakeHttp(routes))
    res = VerifyRunner(_cfg(tmp_path, e2e_timeout=55)).probe_recall_e2e()
    assert res.status == FAIL and res.evidence["e2e_timeout_s"] == 55


def test_recall_e2e_pass_criteria_unchanged_by_calibration(tmp_path, monkeypatch):
    # SPEC §3/§4: this is CALIBRATION, not softening. A recorded interaction
    # with no recall_* tool call must still FAIL even with a huge e2e_timeout.
    monkeypatch.setattr(verify, "load_token", lambda tf: "tok")
    routes = _recall_routes(tool_names=("index_document",))
    monkeypatch.setattr(verify, "_http_request", _FakeHttp(routes))
    res = VerifyRunner(_cfg(tmp_path, e2e_timeout=600)).probe_recall_e2e()
    assert res.status == FAIL and "NO recall_*" in res.detail


def test_recall_e2e_transport_timeout_at_the_new_bound_still_fails(
    tmp_path, monkeypatch
):
    # A genuine transport timeout (real seam raises OSError -> _http_request's
    # status-0 sentinel, per the WS5 hardening) must still FAIL even against
    # the widened, configured bound -- the gate stays REAL, not softened.
    monkeypatch.setattr(verify, "load_token", lambda tf: "tok")

    def _timeout(*_a, **_k):
        raise TimeoutError("read timed out")

    monkeypatch.setattr("urllib.request.urlopen", _timeout)
    res = VerifyRunner(_cfg(tmp_path, e2e_timeout=120)).probe_recall_e2e()
    assert res.status == FAIL and "HTTP 0" in res.detail


def test_health_version_probe_timeout_unaffected_by_e2e_timeout_config(
    tmp_path, monkeypatch
):
    # Fast probes must NOT widen even when e2e_timeout is configured large --
    # scope of the calibration is the chat probe only (SPEC §2).
    spy = _TimeoutSpy({("GET", "/health"): (200, {"status": "ok", "version": "9.9.9"})})
    monkeypatch.setattr(verify, "_http_request", spy)
    VerifyRunner(_cfg(tmp_path, e2e_timeout=999)).probe_health_version()
    assert spy.timeouts == [("GET", "/health", verify.FAST_PROBE_TIMEOUT)]


# ── verdict aggregation + independence ───────────────────────────────────────


def _healthy_injected_memory_server():
    """A memory-server pod with a Ready istio-proxy + vault-agent, secret rendered.

    Carries the mesh decorations the four WS2 probes assert on (sidecar Ready,
    vault-annotated + Ready vault-agent, Running phase) on top of the label +
    Ready condition the core pods-ready probe needs.
    """
    key, value = COMPONENT_SELECTORS["memory-server"]
    return {
        "metadata": {
            "name": "audittrace-memory-server-0",
            "labels": {key: value},
            "annotations": {"vault.hashicorp.com/agent-inject": "true"},
        },
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [
                {"name": "memory-server", "ready": True, "state": {"running": {}}},
                {"name": "istio-proxy", "ready": True, "state": {"running": {}}},
                {"name": "vault-agent", "ready": True, "state": {"running": {}}},
            ],
        },
    }


def _mesh_healthy_pods():
    """All components Ready, with the memory-server pod fully mesh-healthy."""
    key, value = COMPONENT_SELECTORS["memory-server"]
    pods = [
        p for p in _all_components_ready() if p["metadata"]["labels"].get(key) != value
    ]
    pods.append(_healthy_injected_memory_server())
    return pods


def _istiod_ready_deployment():
    return _proc(
        0, json.dumps({"spec": {"replicas": 1}, "status": {"readyReplicas": 1}})
    )


def _healthy_kube_rules():
    """The full read-only cluster read set for an all-green deploy (all nine probes).

    The istiod-specific ``get deployment istiod`` rule MUST precede the generic
    ``get deployment`` rule (first-match-wins), and ``imageID`` MUST precede
    ``get pods`` (the digest read is also a ``get pods`` command).
    """
    return [
        ("imageID", _proc(0, "repo@sha256:pub")),
        ("get deployment istiod", _istiod_ready_deployment()),
        ("get endpoints", _proc(0, "10.42.0.9")),
        ("get pods", _pods_json(_mesh_healthy_pods())),
        ("helm status", _proc(0, json.dumps({"info": {"status": "deployed"}}))),
        ("get deployment", _surge_safe_deployment()),
        ("logs", _proc(0, "all quiet\nnothing to see here")),
    ]


def _all_pass_runner(tmp_path, monkeypatch, interaction_id=42):
    monkeypatch.setattr(registry, "resolve", lambda v, reg: _hub_ref("sha256:pub"))
    monkeypatch.setattr(verify, "load_token", lambda tf: "tok")
    monkeypatch.setattr(verify, "_run", _KubeDispatcher(rules=_healthy_kube_rules()))
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
    # Break exactly one probe: live digest no longer matches published. Every other
    # read stays mesh-healthy so ONLY the digest probe fails.
    rules = _healthy_kube_rules()
    rules[0] = ("imageID", _proc(0, "repo@sha256:DIFFERENT"))
    monkeypatch.setattr(verify, "_run", _KubeDispatcher(rules=rules))
    report = r.run()
    assert report["verdict"] == FAIL
    statuses = {p["name"]: p["status"] for p in report["probes"]}
    assert statuses["digest-matches-published"] == FAIL
    # every other probe (core AND mesh) still PASS — one FAIL fails the verdict
    assert statuses["pods-ready"] == PASS
    assert statuses["istiod-api-reachable"] == PASS
    assert statuses["sidecars-have-certs"] == PASS
    assert statuses["vault-secret-rendered"] == PASS
    assert statuses["no-unhealthy-upstream"] == PASS


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


# ── e2e-timeout calibration: CLI flag + env var wiring ────────────────────────
# Portability invariant (SPEC §2): a different target/model can tune the
# recall-e2e chat-probe timeout WITHOUT a code change, via --e2e-timeout or
# AUDITTRACE_VERIFY_E2E_TIMEOUT. Precedence: flag > env var > 120s default.


def _args(monkeypatch, extra=None):
    monkeypatch.delenv(verify.E2E_TIMEOUT_ENV_VAR, raising=False)
    return [
        "--target-version",
        "v9.9.9",
        "--front-door",
        "https://audittrace.example",
        *(extra or []),
    ]


def test_e2e_timeout_default_unset_env_falls_back_to_120(monkeypatch):
    monkeypatch.delenv(verify.E2E_TIMEOUT_ENV_VAR, raising=False)
    assert verify._e2e_timeout_default() == 120 == verify.E2E_CHAT_TIMEOUT_DEFAULT


def test_e2e_timeout_default_honors_the_env_var(monkeypatch):
    monkeypatch.setenv(verify.E2E_TIMEOUT_ENV_VAR, "45")
    assert verify._e2e_timeout_default() == 45


def test_e2e_timeout_default_invalid_env_var_falls_back_fail_closed(monkeypatch):
    monkeypatch.setenv(verify.E2E_TIMEOUT_ENV_VAR, "not-an-int")
    assert verify._e2e_timeout_default() == verify.E2E_CHAT_TIMEOUT_DEFAULT


def test_cli_e2e_timeout_defaults_to_120_without_flag_or_env(monkeypatch):
    args = verify.build_parser().parse_args(_args(monkeypatch))
    assert verify.config_from_args(args).e2e_timeout == 120


def test_cli_e2e_timeout_flag_overrides_the_default(monkeypatch):
    args = verify.build_parser().parse_args(_args(monkeypatch, ["--e2e-timeout", "55"]))
    assert verify.config_from_args(args).e2e_timeout == 55


def test_cli_e2e_timeout_env_var_sets_the_parser_default(monkeypatch):
    monkeypatch.setenv(verify.E2E_TIMEOUT_ENV_VAR, "99")
    args = verify.build_parser().parse_args(
        [
            "--target-version",
            "v9.9.9",
            "--front-door",
            "https://audittrace.example",
        ]
    )
    assert verify.config_from_args(args).e2e_timeout == 99


def test_cli_e2e_timeout_flag_wins_over_env_var(monkeypatch):
    monkeypatch.setenv(verify.E2E_TIMEOUT_ENV_VAR, "99")
    args = verify.build_parser().parse_args(
        [
            "--target-version",
            "v9.9.9",
            "--front-door",
            "https://audittrace.example",
            "--e2e-timeout",
            "12",
        ]
    )
    assert verify.config_from_args(args).e2e_timeout == 12


# ── front-door resolution: env var + CLI flag + hardcoded fallback ──────────
# Precedence: explicit --front-door arg > AUDITTRACE_FRONT_DOOR env var >
# DEFAULT_FRONT_DOOR constant. Falsifiable: neutering the resolution (returning
# a neutral value) must turn the fallback test RED.


def test_front_door_default_unset_env_falls_back_to_hardcoded(monkeypatch):
    monkeypatch.delenv(verify.FRONT_DOOR_ENV_VAR, raising=False)
    assert verify._front_door_default() == verify.DEFAULT_FRONT_DOOR


def test_front_door_default_honors_the_env_var(monkeypatch):
    monkeypatch.setenv(verify.FRONT_DOOR_ENV_VAR, "https://env.example.com")
    assert verify._front_door_default() == "https://env.example.com"


def test_front_door_default_empty_env_falls_back(monkeypatch):
    monkeypatch.setenv(verify.FRONT_DOOR_ENV_VAR, "")
    assert verify._front_door_default() == verify.DEFAULT_FRONT_DOOR


def test_cli_front_door_flag_overrides_env_var(monkeypatch):
    monkeypatch.setenv(verify.FRONT_DOOR_ENV_VAR, "https://env.example.com")
    args = verify.build_parser().parse_args(
        [
            "--target-version",
            "v9.9.9",
            "--front-door",
            "https://flag.example.com",
        ]
    )
    assert verify.config_from_args(args).front_door == "https://flag.example.com"


def test_cli_front_door_omission_honors_env_var(monkeypatch):
    monkeypatch.setenv(verify.FRONT_DOOR_ENV_VAR, "https://env.example.com")
    args = verify.build_parser().parse_args(
        [
            "--target-version",
            "v9.9.9",
        ]
    )
    assert verify.config_from_args(args).front_door == "https://env.example.com"


def test_cli_front_door_omission_uses_hardcoded_fallback(monkeypatch):
    """Proves the hardcoded fallback is actually exercised, not skipped."""
    monkeypatch.delenv(verify.FRONT_DOOR_ENV_VAR, raising=False)
    args = verify.build_parser().parse_args(
        [
            "--target-version",
            "v9.9.9",
        ]
    )
    assert verify.config_from_args(args).front_door == verify.DEFAULT_FRONT_DOOR


def test_front_door_resolution_neutered_fallback_goes_red(monkeypatch):
    """Neuter _front_door_default to return '' so the config front_door is ''
    instead of the hardcoded DEFAULT_FRONT_DOOR. A different expected value
    would mean the test is vacuous (it would still pass even if
    _front_door_default returned the correct DEFAULT_FRONT_DOOR).

    The _normalize_front_door stub avoids a ValueError on empty string so the
    assertion itself is what goes RED when the function is NOT neutered.
    """
    monkeypatch.delenv(verify.FRONT_DOOR_ENV_VAR, raising=False)
    monkeypatch.setattr(verify, "_front_door_default", lambda: "")
    # Stub the normalizer so empty string does not raise ValueError — the
    # assertion below is what proves the test is non-vacuous.
    monkeypatch.setattr(verify, "_normalize_front_door", lambda u: u)
    args = verify.build_parser().parse_args(
        [
            "--target-version",
            "v9.9.9",
        ]
    )
    # With the neutered function returning '', this MUST be '' — a different
    # expected value would mean the test is vacuous (it would still pass even
    # if _front_door_default returned the correct DEFAULT_FRONT_DOOR).
    assert verify.config_from_args(args).front_door == ""


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


# ════════════════════════════════════════════════════════════════════════════
# #384 WS2 — mesh-health probes. Four independent, fail-closed post-deploy
# certification signals. Each is exercised in BOTH directions (healthy PASS /
# degraded FAIL) and fail-closed on an unreadable/timing-out read, and each is
# proven to flip the overall VERDICT to FAIL. Every cluster read is mocked; the
# signal is re-derived from cluster state, NEVER from a deploy report.
# ════════════════════════════════════════════════════════════════════════════


def _injected_pod(
    name="audittrace-memory-server-0",
    *,
    proxy_ready=True,
    phase="Running",
    vault=True,
    vault_ready=True,
    exit_code=None,
    crashloop=False,
    has_proxy=True,
):
    """A configurable istio-injected / vault-annotated pod for the WS2 probes."""
    statuses = [{"name": "memory-server", "ready": True, "state": {"running": {}}}]
    if has_proxy:
        statuses.append(
            {"name": "istio-proxy", "ready": proxy_ready, "state": {"running": {}}}
        )
    if vault and vault_ready:
        statuses.append(
            {"name": "vault-agent", "ready": True, "state": {"running": {}}}
        )
    if exit_code is not None:
        statuses[0] = {
            "name": "memory-server",
            "ready": False,
            "state": {"waiting": {"reason": "CrashLoopBackOff"}}
            if crashloop
            else {"running": {}},
            "lastState": {"terminated": {"exitCode": exit_code}},
        }
    elif crashloop:
        statuses[0]["ready"] = False
        statuses[0]["state"] = {"waiting": {"reason": "CrashLoopBackOff"}}
    annotations = {"vault.hashicorp.com/agent-inject": "true"} if vault else {}
    return {
        "metadata": {"name": name, "annotations": annotations},
        "status": {"phase": phase, "containerStatuses": statuses},
    }


# ── pure helpers: container / pod introspection ──────────────────────────────


def test_pod_name_and_annotations_defaults():
    assert pod_name({}) == "<unknown>"
    assert pod_name({"metadata": {"name": "p"}}) == "p"
    assert pod_annotations({}) == {}
    assert pod_annotations({"metadata": {"annotations": {"a": "b"}}}) == {"a": "b"}
    # a non-dict annotations block degrades to {}
    assert pod_annotations({"metadata": {"annotations": "x"}}) == {}


def test_pod_has_container_spec_and_status_and_init():
    # present in status containerStatuses
    assert pod_has_container(_injected_pod(), "istio-proxy") is True
    # absent
    assert pod_has_container(_injected_pod(has_proxy=False), "istio-proxy") is False
    # present in spec.initContainers only (native-sidecar shape)
    native = {
        "spec": {"initContainers": [{"name": "istio-proxy"}]},
        "status": {},
    }
    assert pod_has_container(native, "istio-proxy") is True
    # a non-dict status must not crash the introspection
    assert pod_has_container({"status": "broken"}, "istio-proxy") is False
    # a non-dict spec, and a non-dict entry inside a container list, are both
    # skipped without crashing (fail-closed introspection).
    assert pod_has_container({"spec": "broken", "status": {}}, "istio-proxy") is False
    weird = {
        "spec": {"containers": ["not-a-dict", {"name": "istio-proxy"}]},
        "status": {},
    }
    assert pod_has_container(weird, "istio-proxy") is True


def test_container_is_ready_found_notfound_and_init():
    assert container_is_ready(_injected_pod(proxy_ready=True), "istio-proxy") is True
    assert container_is_ready(_injected_pod(proxy_ready=False), "istio-proxy") is False
    # absent container → False (fail-closed), never a crash
    assert container_is_ready(_injected_pod(has_proxy=False), "istio-proxy") is False
    # readiness reported in initContainerStatuses (native sidecar)
    native = {
        "status": {"initContainerStatuses": [{"name": "istio-proxy", "ready": True}]}
    }
    assert container_is_ready(native, "istio-proxy") is True


def test_containers_with_exit_code_and_crashloop():
    exited = _injected_pod(exit_code=79)
    assert containers_with_exit_code(exited, 79) == ["memory-server"]
    assert containers_with_exit_code(_injected_pod(), 79) == []
    # missing lastState must not crash
    assert containers_with_exit_code(_injected_pod(), 1) == []
    assert crashlooping_containers(_injected_pod(crashloop=True)) == ["memory-server"]
    assert crashlooping_containers(_injected_pod()) == []


# ── pure helpers: findings ───────────────────────────────────────────────────


def test_sidecar_cert_findings_clean_and_degraded():
    assert sidecar_cert_findings([_injected_pod()]) == []
    # pods with no sidecar are skipped (not part of the data plane)
    assert sidecar_cert_findings([_injected_pod(has_proxy=False)]) == []
    # proxy not ready → finding
    f = sidecar_cert_findings([_injected_pod(proxy_ready=False)])
    assert len(f) == 1 and "not Ready" in f[0]["reasons"][0]
    # wedged in Init (phase Pending) → finding
    f2 = sidecar_cert_findings([_injected_pod(phase="Pending")])
    assert len(f2) == 1 and any("wedged Init" in r for r in f2[0]["reasons"])
    # both problems at once → both reasons captured
    f3 = sidecar_cert_findings([_injected_pod(proxy_ready=False, phase="Pending")])
    assert len(f3[0]["reasons"]) == 2


def test_sidecar_cert_findings_exempts_completed_pods():
    """NIT 2: a finished (Succeeded/Completed) injected pod whose istio-proxy has
    terminated (ready=false) is NOT a cert failure — e.g. a Helm-hook Job — so it
    must be exempt and produce NO finding (no false-FAIL)."""
    for phase in ("Succeeded", "Completed"):
        pod = _injected_pod(proxy_ready=False, phase=phase, vault=False)
        assert sidecar_cert_findings([pod]) == []


def test_sidecar_cert_findings_non_dict_status_does_not_crash():
    """NIT 3: a non-dict ``status`` must be guarded (fail-closed), not crash."""
    pod = {"spec": {"containers": [{"name": "istio-proxy"}]}, "status": "broken"}
    findings = sidecar_cert_findings([pod])  # must not raise
    assert len(findings) == 1  # unreadable status → fail-closed finding


def test_vault_render_findings_clean_and_all_failure_modes():
    assert vault_render_findings([_injected_pod()]) == []
    # non-annotated pods are skipped
    assert vault_render_findings([_injected_pod(vault=False)]) == []
    # annotated but vault-agent not injected (mode 4a) → finding
    f = vault_render_findings([_injected_pod(vault=True, vault_ready=False)])
    assert len(f) == 1 and any("vault-agent" in r for r in f[0]["reasons"])
    # exit-79 (secret file absent) → finding
    f2 = vault_render_findings([_injected_pod(exit_code=79)])
    assert any("exited 79" in r for r in f2[0]["reasons"])
    # crashloop → finding
    f3 = vault_render_findings([_injected_pod(crashloop=True)])
    assert any("CrashLoopBackOff" in r for r in f3[0]["reasons"])


def test_count_unhealthy_upstream():
    logs = (
        "ok\nupstream connect error: no healthy upstream\nfine\nno healthy upstream\n"
    )
    assert count_unhealthy_upstream(logs) == 2
    assert count_unhealthy_upstream("all fine\nnothing here") == 0


# ── probe 6: istiod-api-reachable (PASS + FAIL + fail-closed) ─────────────────


def _istiod_router(logs="all quiet", dep_ready=True, endpoints="10.42.0.9"):
    dep = json.dumps(
        {"spec": {"replicas": 1}, "status": {"readyReplicas": 1 if dep_ready else 0}}
    )
    return _KubeDispatcher(
        rules=[
            ("get deployment istiod", _proc(0, dep)),
            ("get endpoints", _proc(0, endpoints)),
            ("logs", _proc(0, logs)),
        ]
    )


def test_istiod_api_reachable_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "_run", _istiod_router())
    res = VerifyRunner(_cfg(tmp_path)).probe_istiod_api_reachable()
    assert res.status == PASS


def test_istiod_api_reachable_fail_smoking_gun(tmp_path, monkeypatch):
    monkeypatch.setattr(
        verify,
        "_run",
        _istiod_router(logs="x\ndial tcp 10.43.0.1:443: no route to host"),
    )
    res = VerifyRunner(_cfg(tmp_path)).probe_istiod_api_reachable()
    assert res.status == FAIL and "istiod-api-unreachable" in res.detail


def test_istiod_api_reachable_fail_not_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "_run", _istiod_router(dep_ready=False))
    res = VerifyRunner(_cfg(tmp_path)).probe_istiod_api_reachable()
    assert res.status == FAIL and "istiod-not-ready" in res.detail


def test_istiod_api_reachable_fail_no_endpoints(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "_run", _istiod_router(endpoints=""))
    res = VerifyRunner(_cfg(tmp_path)).probe_istiod_api_reachable()
    assert res.status == FAIL and "istiod-no-endpoints" in res.detail


def test_istiod_api_reachable_fail_closed_logs_unreadable(tmp_path, monkeypatch):
    disp = _KubeDispatcher(
        rules=[
            (
                "get deployment istiod",
                _proc(
                    0,
                    json.dumps(
                        {"spec": {"replicas": 1}, "status": {"readyReplicas": 1}}
                    ),
                ),
            ),
            ("get endpoints", _proc(0, "10.42.0.9")),
            ("logs", _proc(returncode=1, stderr="no such pod")),
        ]
    )
    monkeypatch.setattr(verify, "_run", disp)
    res = VerifyRunner(_cfg(tmp_path)).probe_istiod_api_reachable()
    assert res.status == FAIL and "istiod-logs" in res.detail


def test_istiod_api_reachable_fail_closed_endpoints_unreadable(tmp_path, monkeypatch):
    disp = _KubeDispatcher(
        rules=[
            (
                "get deployment istiod",
                _proc(
                    0,
                    json.dumps(
                        {"spec": {"replicas": 1}, "status": {"readyReplicas": 1}}
                    ),
                ),
            ),
            ("get endpoints", _proc(returncode=1)),
            ("logs", _proc(0, "all quiet")),
        ]
    )
    monkeypatch.setattr(verify, "_run", disp)
    res = VerifyRunner(_cfg(tmp_path)).probe_istiod_api_reachable()
    assert res.status == FAIL and "istiod-endpoints" in res.detail


def test_istiod_api_reachable_fail_deployment_unreadable(tmp_path, monkeypatch):
    # deployment read fails → dep is None → evaluate_istiod_readiness yields a
    # fail-closed finding (logs + endpoints still readable, so not caught above).
    disp = _KubeDispatcher(
        rules=[
            ("get deployment istiod", _proc(returncode=1)),
            ("get endpoints", _proc(0, "10.42.0.9")),
            ("logs", _proc(0, "all quiet")),
        ]
    )
    monkeypatch.setattr(verify, "_run", disp)
    res = VerifyRunner(_cfg(tmp_path)).probe_istiod_api_reachable()
    assert res.status == FAIL and "istiod-unreadable" in res.detail


def test_istiod_deployment_read_non_dict_json(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "_run", lambda cmd: _proc(0, "[]"))
    assert VerifyRunner(_cfg(tmp_path))._istiod_deployment() is None


# ── probe 7: sidecars-have-certs (PASS + FAIL + fail-closed) ──────────────────


def test_sidecars_have_certs_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "_run", lambda cmd: _pods_json([_injected_pod()]))
    res = VerifyRunner(_cfg(tmp_path)).probe_sidecars_have_certs()
    assert res.status == PASS


def test_sidecars_have_certs_pass_with_completed_hook_pod(tmp_path, monkeypatch):
    """NIT 2 at probe level: a live Ready pod PLUS a finished Helm-hook Job pod
    (Succeeded, terminated not-ready istio-proxy) → the completed pod is exempt →
    PASS, not a false-FAIL."""
    live = _injected_pod()
    completed = _injected_pod(
        name="audittrace-migrate-hook",
        proxy_ready=False,
        phase="Succeeded",
        vault=False,
    )
    monkeypatch.setattr(verify, "_run", lambda cmd: _pods_json([live, completed]))
    res = VerifyRunner(_cfg(tmp_path)).probe_sidecars_have_certs()
    assert res.status == PASS


def test_sidecars_have_certs_fail_proxy_not_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(
        verify, "_run", lambda cmd: _pods_json([_injected_pod(proxy_ready=False)])
    )
    res = VerifyRunner(_cfg(tmp_path)).probe_sidecars_have_certs()
    assert res.status == FAIL and "cert-starved" in res.detail


def test_sidecars_have_certs_fail_closed_no_pods(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "_run", lambda cmd: _proc(returncode=1))
    res = VerifyRunner(_cfg(tmp_path)).probe_sidecars_have_certs()
    assert res.status == FAIL and "no pods" in res.detail


def test_sidecars_have_certs_fail_no_injected_pods(tmp_path, monkeypatch):
    # pods exist but none carries an istio-proxy → data plane not certifiable.
    monkeypatch.setattr(
        verify, "_run", lambda cmd: _pods_json([_injected_pod(has_proxy=False)])
    )
    res = VerifyRunner(_cfg(tmp_path)).probe_sidecars_have_certs()
    assert res.status == FAIL and "no istio-injected pods" in res.detail


# ── probe 8: vault-secret-rendered (PASS + FAIL + fail-closed) ────────────────


def test_vault_secret_rendered_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "_run", lambda cmd: _pods_json([_injected_pod()]))
    res = VerifyRunner(_cfg(tmp_path)).probe_vault_secret_rendered()
    assert res.status == PASS


def test_vault_secret_rendered_fail_exit_79(tmp_path, monkeypatch):
    monkeypatch.setattr(
        verify, "_run", lambda cmd: _pods_json([_injected_pod(exit_code=79)])
    )
    res = VerifyRunner(_cfg(tmp_path)).probe_vault_secret_rendered()
    assert res.status == FAIL and "not rendered" in res.detail


def test_vault_secret_rendered_fail_closed_no_pods(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "_run", lambda cmd: _proc(returncode=1))
    res = VerifyRunner(_cfg(tmp_path)).probe_vault_secret_rendered()
    assert res.status == FAIL and "no pods" in res.detail


def test_vault_secret_rendered_fail_no_annotated_pods(tmp_path, monkeypatch):
    monkeypatch.setattr(
        verify, "_run", lambda cmd: _pods_json([_injected_pod(vault=False)])
    )
    res = VerifyRunner(_cfg(tmp_path)).probe_vault_secret_rendered()
    assert res.status == FAIL and "no vault-annotated pods" in res.detail


# ── probe 9: no-unhealthy-upstream (PASS + FAIL + fail-closed) ────────────────


def test_no_unhealthy_upstream_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(
        verify,
        "_http_request",
        _FakeHttp({("GET", "/health"): (200, {"status": "ok"})}),
    )
    monkeypatch.setattr(verify, "_run", lambda cmd: _proc(0, "all quiet\nserving"))
    res = VerifyRunner(_cfg(tmp_path)).probe_no_unhealthy_upstream()
    assert res.status == PASS


def test_no_unhealthy_upstream_fail_health_down(tmp_path, monkeypatch):
    monkeypatch.setattr(
        verify, "_http_request", _FakeHttp({("GET", "/health"): (503, {})})
    )
    res = VerifyRunner(_cfg(tmp_path)).probe_no_unhealthy_upstream()
    assert res.status == FAIL and "503" in res.detail


def test_no_unhealthy_upstream_fail_health_not_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(
        verify,
        "_http_request",
        _FakeHttp({("GET", "/health"): (200, {"status": "degraded"})}),
    )
    res = VerifyRunner(_cfg(tmp_path)).probe_no_unhealthy_upstream()
    assert res.status == FAIL and "degraded" in res.detail


def test_no_unhealthy_upstream_fail_503_in_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(
        verify,
        "_http_request",
        _FakeHttp({("GET", "/health"): (200, {"status": "ok"})}),
    )
    monkeypatch.setattr(
        verify, "_run", lambda cmd: _proc(0, "x\nupstream: no healthy upstream\n")
    )
    res = VerifyRunner(_cfg(tmp_path)).probe_no_unhealthy_upstream()
    assert res.status == FAIL and "no healthy upstream" in res.detail


def test_no_unhealthy_upstream_fail_closed_logs_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        verify,
        "_http_request",
        _FakeHttp({("GET", "/health"): (200, {"status": "ok"})}),
    )
    monkeypatch.setattr(verify, "_run", lambda cmd: _proc(returncode=1))
    res = VerifyRunner(_cfg(tmp_path)).probe_no_unhealthy_upstream()
    assert res.status == FAIL and "could not read memory-server logs" in res.detail


# ── CodeQL fix: no raw log content leaks into the verify report ───────────────
#
# py/clear-text-logging-of-sensitive-data. memory-server / istiod logs can carry
# tokens / request content / PII; a FAIL must emit DERIVED signals only (counts,
# window, signature name), never the raw log tail. FALSIFIABLE: re-adding a
# ``log_tail`` (or any raw-content evidence value) turns these RED.

# A distinctive "secret" planted in the raw logs. If it ever appears in ANY probe
# evidence value or detail string, a raw-content leak has been reintroduced.
_PLANTED_SECRET = "Bearer eyJhbGciOiJIUZ.SUPERSECRET.TOKEN-should-never-surface"


def _evidence_as_text(evidence):
    return json.dumps(evidence, default=str)


def test_no_unhealthy_upstream_fail_emits_no_raw_log_content(tmp_path, monkeypatch):
    """The no-unhealthy-upstream FAIL evidence is EXACTLY {match_count, log_window}
    and carries none of the raw log tail (which here embeds a planted secret)."""
    monkeypatch.setattr(
        verify,
        "_http_request",
        _FakeHttp({("GET", "/health"): (200, {"status": "ok"})}),
    )
    noisy = f"req {_PLANTED_SECRET}\nupstream connect error: no healthy upstream\n"
    monkeypatch.setattr(verify, "_run", lambda cmd: _proc(0, noisy))
    res = VerifyRunner(_cfg(tmp_path)).probe_no_unhealthy_upstream()
    assert res.status == FAIL
    # evidence keys are EXACTLY the derived pair — no log_tail / raw content
    assert set(res.evidence) == {"match_count", "log_window"}
    assert res.evidence["match_count"] == 1
    # the planted secret appears in NO evidence value and NO detail string
    assert _PLANTED_SECRET not in _evidence_as_text(res.evidence)
    assert _PLANTED_SECRET not in res.detail


def test_istiod_api_reachable_fail_emits_no_raw_log_content(tmp_path, monkeypatch):
    """The istiod probe reuses mesh diagnosers whose finding carries a raw log_tail;
    the probe MUST strip it — no finding evidence value contains the planted
    secret, and no 'log_tail' key survives."""
    smoking = f"{_PLANTED_SECRET}\ndial tcp 10.43.0.1:443: no route to host\n"
    monkeypatch.setattr(verify, "_run", _istiod_router(logs=smoking))
    res = VerifyRunner(_cfg(tmp_path)).probe_istiod_api_reachable()
    assert res.status == FAIL
    findings = res.evidence["findings"]
    assert findings[0]["signal"] == "istiod-api-unreachable"
    # derived evidence survives, raw log content does not
    assert findings[0]["evidence"]["match_count"] == 1
    for finding in findings:
        assert "log_tail" not in finding["evidence"]
    assert _PLANTED_SECRET not in _evidence_as_text(res.evidence)
    assert _PLANTED_SECRET not in res.detail


def test_sanitized_finding_keeps_allowlisted_derived_keys():
    """The sanitizer keeps allowlisted derived evidence (and signal/detail)."""
    f = mesh.Finding(
        "istiod-api-unreachable",
        "1 smoking-gun line",
        {"match_count": 1, "window": "3m", "log_tail": _PLANTED_SECRET},
    )
    out = verify.sanitized_finding(f)
    assert out["signal"] == "istiod-api-unreachable"
    assert out["detail"] == "1 smoking-gun line"
    assert out["evidence"] == {"match_count": 1, "window": "3m"}
    assert "log_tail" not in out["evidence"]
    assert _PLANTED_SECRET not in json.dumps(out, default=str)
    # a finding with no evidence must not crash
    empty = verify.sanitized_finding(mesh.Finding("s", "d"))
    assert empty["evidence"] == {}


def test_sanitized_finding_allowlist_drops_unknown_and_raw_keys():
    """FAIL-CLOSED allowlist: known-raw keys (error / deployment_error /
    endpoints_error / log_tail) AND any UNKNOWN future key are DROPPED; only
    allowlisted derived keys survive. Falsifiable: switching back to a denylist,
    or widening the allowlist to include a raw key, turns this RED."""
    f = mesh.Finding(
        "istiod-unreadable",
        "could not read istiod readiness (fail-closed)",
        {
            "error": "raw stderr " + _PLANTED_SECRET,
            "deployment_error": "raw " + _PLANTED_SECRET,
            "endpoints_error": _PLANTED_SECRET,
            "log_tail": _PLANTED_SECRET,
            "future_unknown_key": _PLANTED_SECRET,
            "match_count": 1,
        },
    )
    out = verify.sanitized_finding(f)
    # only the allowlisted derived key survives; every raw/unknown key is dropped
    assert out["evidence"] == {"match_count": 1}
    for dropped in (
        "error",
        "deployment_error",
        "endpoints_error",
        "log_tail",
        "future_unknown_key",
    ):
        assert dropped not in out["evidence"]
    assert _PLANTED_SECRET not in json.dumps(out, default=str)


def test_allowed_evidence_keys_are_all_emitted_by_a_mesh_diagnoser():
    """Every allowlisted key is actually produced by a mesh diagnoser (no dead
    entries) — and no raw key sneaked into the allowlist."""
    emitted: set[str] = set()
    for finding in (
        *mesh.evaluate_nodes(""),
        *mesh.evaluate_nodes("True False"),
        *mesh.evaluate_istiod_logs("no route to host", "3m"),
        *mesh.evaluate_istiod_readiness(None, "10.0.0.1"),
        *mesh.evaluate_istiod_readiness({"spec": {"replicas": 1}, "status": {}}, ""),
        *mesh.evaluate_istiod_readiness(
            {"spec": {"replicas": 2}, "status": {"readyReplicas": 1}}, "10.0.0.1"
        ),
        *mesh.evaluate_istiod_readiness({"spec": {}, "status": {}}, "10.0.0.1"),
    ):
        emitted.update(finding.evidence)
    # allowlist contains no raw key, and every allowlisted key is genuinely emitted
    for raw in ("log_tail", "error", "deployment_error", "endpoints_error"):
        assert raw not in verify.ALLOWED_EVIDENCE_KEYS
    assert verify.ALLOWED_EVIDENCE_KEYS <= emitted


# ── verdict integration: each mesh probe flips the VERDICT to FAIL ────────────


@pytest.mark.parametrize(
    "broken_probe,detail_needle",
    [
        ("istiod-api-reachable", "istiod"),
        ("sidecars-have-certs", "cert-starved"),
        ("vault-secret-rendered", "not rendered"),
        ("no-unhealthy-upstream", "no healthy upstream"),
    ],
)
def test_degraded_mesh_flips_verdict_to_fail(
    tmp_path, monkeypatch, broken_probe, detail_needle
):
    """FALSIFIABLE: a single degraded mesh signal makes the WHOLE verdict FAIL,
    while every OTHER probe (core + mesh) still PASSes — proving each new probe
    genuinely gates the verdict."""
    r = _all_pass_runner(tmp_path, monkeypatch)
    rules = _healthy_kube_rules()
    if broken_probe == "istiod-api-reachable":
        rules[2] = ("get endpoints", _proc(0, ""))  # istiod has no endpoints
    elif broken_probe == "sidecars-have-certs":
        # memory-server proxy not ready → cert-starved data plane
        bad = _mesh_healthy_pods()
        bad[-1]["status"]["containerStatuses"][1]["ready"] = False
        rules[3] = ("get pods", _pods_json(bad))
    elif broken_probe == "vault-secret-rendered":
        bad = _mesh_healthy_pods()
        bad[-1]["status"]["containerStatuses"][0]["lastState"] = {
            "terminated": {"exitCode": 79}
        }
        # drop the ready vault-agent so the annotated pod also fails cleanly
        bad[-1]["status"]["containerStatuses"] = [
            c
            for c in bad[-1]["status"]["containerStatuses"]
            if c["name"] != "vault-agent"
        ]
        rules[3] = ("get pods", _pods_json(bad))
    monkeypatch.setattr(verify, "_run", _KubeDispatcher(rules=rules))
    if broken_probe == "no-unhealthy-upstream":
        monkeypatch.setattr(
            verify,
            "_http_request",
            _FakeHttp(
                {
                    ("GET", "/health"): (200, {"status": "ok", "version": "9.9.9"}),
                    **_recall_routes(),
                }
            ),
        )
        # override the memory-server log read to carry the 503 signature
        rules[6] = ("logs", _proc(0, "no healthy upstream"))
        monkeypatch.setattr(verify, "_run", _KubeDispatcher(rules=rules))

    report = r.run()
    statuses = {p["name"]: p["status"] for p in report["probes"]}
    assert report["verdict"] == FAIL
    assert statuses[broken_probe] == FAIL
    broken_detail = next(
        p["detail"] for p in report["probes"] if p["name"] == broken_probe
    )
    assert detail_needle in broken_detail
    # the OTHER mesh probes are unaffected — the failure is isolated
    others = {
        "istiod-api-reachable",
        "sidecars-have-certs",
        "vault-secret-rendered",
        "no-unhealthy-upstream",
    } - {broken_probe}
    assert all(statuses[o] == PASS for o in others)


def test_mesh_probes_reuse_pure_mesh_diagnosers_independently(tmp_path, monkeypatch):
    """The istiod probe REUSES mesh's pure diagnosers over reads THIS runner made:
    the same log window is single-sourced from the WS1 module, and the smoking-gun
    verdict matches mesh.evaluate_istiod_logs on the same input (no duplicated
    #307 signature)."""
    assert verify.MESH_LOG_WINDOW == mesh.DEFAULT_LOG_WINDOW
    monkeypatch.setattr(
        verify, "_run", _istiod_router(logs="tokenreviews Unauthenticated")
    )
    res = VerifyRunner(_cfg(tmp_path)).probe_istiod_api_reachable()
    assert res.status == FAIL
    assert res.evidence["findings"][0]["signal"] == "istiod-api-unreachable"


def test_evidence_sources_include_mesh_reads_and_stay_independent(tmp_path):
    """The four mesh reads are enumerated in evidence_sources (so the derived
    reads_deploy_report stays honest) and NONE is a deploy-report source."""
    r = VerifyRunner(_cfg(tmp_path))
    sources = r.evidence_sources()
    assert "cluster-api:kubectl:istiod-logs" in sources
    assert "cluster-api:kubectl:istiod-deployment" in sources
    assert "cluster-api:kubectl:istiod-endpoints" in sources
    assert "cluster-api:kubectl:memory-server-logs" in sources
    assert r.reads_deploy_report is False
    assert not any(s.startswith(verify.DEPLOY_REPORT_SOURCE_PREFIX) for s in sources)


# ── NIT 1: fail-closed hardening of the shared read-only _run seam ────────────
#
# A HUNG or MISSING kubectl — the exact #307 case (a wedged ClusterIP makes a
# kube-API read HANG) — must NOT hang or crash run(). These tests drive the REAL
# ``verify._run`` (patching ``subprocess.run`` to raise) and are FALSIFIABLE:
# remove the bounded timeout + the ``except (OSError, SubprocessError)`` and they
# raise instead of producing the clean non-zero-exit sentinel / FAIL verdict.


def test_run_passes_a_bounded_timeout(monkeypatch):
    # The bound is applied (single-sourced from the WS1 gate); a wedged read can
    # never hang unboundedly.
    assert verify.PROBE_TIMEOUT == mesh.DEFAULT_PROBE_TIMEOUT

    captured = {}

    def _spy(cmd, **kw):
        captured.update(kw)
        return _proc(0, "")

    monkeypatch.setattr(subprocess, "run", _spy)
    verify._run(["kubectl", "get", "pods"])
    assert captured["timeout"] == verify.PROBE_TIMEOUT


@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError("kubectl: command not found"),  # missing binary → OSError
        subprocess.TimeoutExpired(cmd=["kubectl"], timeout=30),  # hung read
        OSError("permission denied"),
    ],
)
def test_run_maps_subprocess_errors_to_failclosed_sentinel(monkeypatch, exc):
    def _raise(*a, **k):
        raise exc

    monkeypatch.setattr(subprocess, "run", _raise)
    proc = verify._run(["kubectl", "get", "pods"])
    # SAME sentinel a genuine non-zero exit produces: returncode!=0, empty stdout.
    assert proc.returncode == 1
    assert proc.stdout == ""


@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError("kubectl: command not found"),
        subprocess.TimeoutExpired(cmd=["kubectl"], timeout=30),
    ],
)
def test_kubectl_hang_or_missing_fails_closed_and_still_emits_report(
    tmp_path, monkeypatch, exc
):
    """A hung/missing kubectl at the REAL _run seam → every kubectl probe FAILs,
    run() STILL emits a full verdict + report bundle, NO hang, NO uncaught crash."""

    def _raise(*a, **k):
        raise exc

    monkeypatch.setattr(subprocess, "run", _raise)
    monkeypatch.setattr(registry, "resolve", lambda v, reg: _hub_ref("sha256:pub"))
    monkeypatch.setattr(verify, "load_token", lambda tf: "tok")
    monkeypatch.setattr(verify, "_http_request", lambda *a, **k: (0, {}, b""))

    report = VerifyRunner(_cfg(tmp_path)).run()  # must NOT hang or raise

    statuses = {p["name"]: p["status"] for p in report["probes"]}
    assert report["verdict"] == FAIL
    # every kubectl-backed probe degraded to a clean FAIL
    assert statuses["pods-ready"] == FAIL
    assert statuses["istiod-api-reachable"] == FAIL
    assert statuses["sidecars-have-certs"] == FAIL
    assert statuses["vault-secret-rendered"] == FAIL
    # the verdict is complete (all nine probes ran) and a bundle was written
    assert [p["name"] for p in report["probes"]] == list(verify.PROBES)
    assert list((tmp_path / "runs").glob("verify-v9.9.9-*.json"))
