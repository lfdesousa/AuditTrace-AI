"""Unit tests for the shared first-party image row predicate
(`scripts/deploy/runner/images.py`, spec 2026-10-07-SPEC-bff-bump-1.29.1-and-
stale-override-guard.md, Addendum A Rule B1).

Every external effect (subprocess) is mocked via the same `_Dispatcher`
pattern `tests/test_deploy_runner.py` uses. No cluster, no network.

Each guard below has an INDIVIDUAL neuter (flip, never delete) proving it
actually fires — see each test's "Falsifiable" note.
"""

from __future__ import annotations

import json
import subprocess

from scripts.deploy import registry, runner
from scripts.deploy.runner import images
from scripts.deploy.runner.images import (
    FIRST_PARTY_PREFIXES,
    _manifest_workload_docs,
    _parse_image_ref,
    _pod_spec_of,
    committed_first_party_digests,
    mismatched_components,
)

# ── helpers ───────────────────────────────────────────────────────────────


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["x"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _cfg(tmp_path, **kw):
    kw.setdefault("out_dir", tmp_path / "runs")
    kw.setdefault("target_version", "v9.9.9")
    return runner.DeployConfig(**kw)


def _hub_ref(digest="sha256:x", repository="docker.io/lfds/audittrace-memory-server"):
    return registry.ImageRef(repository, "9.9.9", digest, "hub")


def _local_ref_unpinned(repository="localhost:5000/audittrace/memory-server"):
    return registry.ImageRef(repository, "9.9.9", None, "local")


class _Dispatcher:
    """Routes runner._run calls to canned CompletedProcess results by token
    match — identical pattern to tests/test_deploy_runner.py."""

    def __init__(self, rules, default=None):
        self.rules = rules
        self.default = default or _proc(0, "")
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, env=None):
        self.calls.append(cmd)
        joined = " ".join(cmd)
        for needle, result in self.rules:
            if needle in joined:
                return result
        return self.default


_CHART_VALUES = {
    "console": {
        "enabled": True,
        "librechat": {
            "image": {
                "repository": "docker.io/lfds/audittrace-librechat",
                "tag": "768de61",
                "digest": "sha256:librechat-chart",
            }
        },
        "bff": {
            "image": {
                "repository": "docker.io/lfds/audittrace-librechat-bff",
                "tag": "1.29.1",
                "digest": "sha256:bff-chart",
            }
        },
    },
    "tests": {
        "image": {
            "repository": "docker.io/lfds/audittrace-tests",
            "tag": "1.29.1",
            "digest": "sha256:tests-chart",
        }
    },
}


def _deployment_doc(name, container, image, *, replicas=1, init_containers=None):
    containers = [{"name": container, "image": image}]
    pod_spec = {"containers": containers}
    if init_containers:
        pod_spec["initContainers"] = init_containers
    return {
        "kind": "Deployment",
        "metadata": {"name": name},
        "spec": {"replicas": replicas, "template": {"spec": pod_spec}},
    }


def _manifest_yaml(*docs):
    return "\n---\n".join(json.dumps(d) for d in docs)


def _pods_json(pods):
    return _proc(0, json.dumps({"items": pods}))


def _pod(container, digest, *, labels=None, deletion_timestamp=False, init=False):
    metadata = {"labels": labels or {}}
    if deletion_timestamp:
        metadata["deletionTimestamp"] = "2026-10-08T00:00:00Z"
    status_key = "initContainerStatuses" if init else "containerStatuses"
    return {
        "metadata": metadata,
        "status": {status_key: [{"name": container, "imageID": f"repo@{digest}"}]},
    }


# ── _parse_image_ref ─────────────────────────────────────────────────────


def test_parse_image_ref_repo_tag_digest():
    repo, tag, digest = _parse_image_ref("docker.io/lfds/x:1.0@sha256:abc")
    assert (repo, tag, digest) == ("docker.io/lfds/x", "1.0", "sha256:abc")


def test_parse_image_ref_repo_tag_only():
    assert _parse_image_ref("docker.io/lfds/x:1.0") == ("docker.io/lfds/x", "1.0", None)


def test_parse_image_ref_repo_digest_only_no_tag():
    repo, tag, digest = _parse_image_ref("docker.io/lfds/x@sha256:abc")
    assert (repo, tag, digest) == ("docker.io/lfds/x", None, "sha256:abc")


def test_parse_image_ref_bare_repo_no_tag_no_digest():
    assert _parse_image_ref("docker.io/lfds/x") == ("docker.io/lfds/x", None, None)


def test_parse_image_ref_port_in_repository_never_mistaken_for_tag():
    """The registry host port (`localhost:5000`) must never be read as a
    tag separator — only a `:` AFTER the final `/` is a tag boundary.

    Falsifiable: `rpartition(":")` over the WHOLE string (ignoring the
    final `/`) would split this into repo=`localhost` / tag=
    `5000/audittrace/memory-server` — wrong.
    """
    repo, tag, digest = _parse_image_ref(
        "localhost:5000/audittrace/memory-server:9.9.9"
    )
    assert repo == "localhost:5000/audittrace/memory-server"
    assert tag == "9.9.9"
    assert digest is None


def test_parse_image_ref_port_in_repository_no_tag_at_all():
    repo, tag, digest = _parse_image_ref("localhost:5000/audittrace/memory-server")
    assert repo == "localhost:5000/audittrace/memory-server"
    assert tag is None
    assert digest is None


# ── _pod_spec_of ─────────────────────────────────────────────────────────


def test_pod_spec_of_deployment():
    doc = _deployment_doc("x", "c", "repo:tag")
    assert _pod_spec_of(doc) == {"containers": [{"name": "c", "image": "repo:tag"}]}


def test_pod_spec_of_cronjob_nests_one_level_deeper():
    doc = {
        "kind": "CronJob",
        "metadata": {"name": "x"},
        "spec": {
            "jobTemplate": {
                "spec": {"template": {"spec": {"containers": [{"name": "c"}]}}}
            }
        },
    }
    assert _pod_spec_of(doc) == {"containers": [{"name": "c"}]}


def test_pod_spec_of_malformed_returns_none():
    assert _pod_spec_of({"kind": "Deployment"}) is None
    assert _pod_spec_of({"kind": "Deployment", "spec": "not-a-dict"}) is None
    assert (
        _pod_spec_of({"kind": "CronJob", "spec": {"jobTemplate": "not-a-dict"}}) is None
    )


# ── _manifest_workload_docs ──────────────────────────────────────────────


def test_manifest_workload_docs_filters_secret_and_unknown_kinds():
    """AC3e: a Secret doc is NEVER even visited, not filtered out of
    evidence after the fact. Falsifiable: include Secret in the kind
    allowlist and this goes RED (a Secret doc would survive the filter)."""
    docs = [
        {"kind": "Secret", "metadata": {"name": "s"}, "data": {"k": "v"}},
        {"kind": "ConfigMap", "metadata": {"name": "cm"}},
        {"kind": "Deployment", "metadata": {"name": "d"}},
        "not-a-dict",
        None,
    ]
    kept = _manifest_workload_docs(docs)
    assert [d["kind"] for d in kept] == ["Deployment"]


def test_manifest_workload_docs_none_input():
    assert _manifest_workload_docs(None) == []


# ── committed_first_party_digests ───────────────────────────────────────


def test_committed_first_party_digests_full_shape():
    committed = committed_first_party_digests(_CHART_VALUES, _hub_ref("sha256:ms"))
    assert committed["docker.io/lfds/audittrace-memory-server"] == {
        "key": "memory-server",
        "digest": "sha256:ms",
        "tag": "9.9.9",
    }
    assert committed["docker.io/lfds/audittrace-librechat"]["key"] == "librechat"
    assert committed["docker.io/lfds/audittrace-librechat-bff"]["key"] == "bff"
    assert committed["docker.io/lfds/audittrace-tests"]["key"] == "tests"


def test_committed_first_party_digests_no_image_ref():
    committed = committed_first_party_digests(_CHART_VALUES, None)
    assert "docker.io/lfds/audittrace-memory-server" not in committed
    assert "docker.io/lfds/audittrace-librechat" in committed


def test_committed_first_party_digests_unpinned_memory_server():
    committed = committed_first_party_digests({}, _local_ref_unpinned())
    entry = committed["localhost:5000/audittrace/memory-server"]
    assert entry["digest"] is None
    assert entry["tag"] == "9.9.9"


def test_committed_first_party_digests_malformed_console_and_tests_skip_cleanly():
    committed = committed_first_party_digests(
        {"console": "not-a-dict", "tests": {"image": "not-a-dict"}}, None
    )
    assert committed == {}


def test_committed_first_party_digests_missing_repository_is_skipped():
    values = {"console": {"librechat": {"image": {"tag": "t", "digest": "sha256:d"}}}}
    assert committed_first_party_digests(values, None) == {}


# ── mismatched_components ───────────────────────────────────────────────


def test_mismatched_components_dedupes_and_sorts():
    rows = [
        {"component": "bff", "equal": False},
        {"component": "librechat", "equal": False},
        {"component": "librechat", "equal": False},
        {"component": "memory-server", "equal": True},
    ]
    assert mismatched_components(rows) == ["bff", "librechat"]


def test_mismatched_components_empty_when_all_equal():
    assert mismatched_components([{"component": "bff", "equal": True}]) == []


# ── FirstPartyImagesMixin._first_party_image_rows — integration ─────────


def _runner(tmp_path, monkeypatch, disp, *, image_ref=None, **cfg_kw):
    monkeypatch.setattr(runner, "_run", disp)
    r = runner.DeployRunner(_cfg(tmp_path, **cfg_kw))
    r.image_ref = image_ref or _hub_ref("sha256:ms")
    return r


def test_rows_unreadable_manifest_yields_no_rows(tmp_path, monkeypatch):
    """`helm get manifest` failing is fail-safe UNKNOWN, not a crash — no
    rows means no mismatch is manufactured from nothing."""
    disp = _Dispatcher(rules=[("helm get manifest", _proc(returncode=1))])
    r = _runner(tmp_path, monkeypatch, disp)
    assert r._first_party_image_rows(chart_values=_CHART_VALUES) == []


def test_rows_unknown_first_party_image_is_flagged_s_f1(tmp_path, monkeypatch):
    """S-F1 completeness pin: a first-party image present in the manifest
    but absent from the committed map (e.g. a newly-enabled llm-stub) gets
    an `unknown_first_party` row, ALWAYS unequal — never silently skipped.

    Falsifiable (the neuter the gate asked for): drop the `info is None`
    branch (treat an unrecognised repository as simply out of scope,
    `continue`) and this goes RED — the unknown row disappears and
    `mismatched_components` returns `[]`.
    """
    manifest = _manifest_yaml(
        _deployment_doc(
            "audittrace-llm-stub",
            "llm-stub",
            "docker.io/lfds/audittrace-llm-stub:9.9.9@sha256:z",
        )
    )
    disp = _Dispatcher(rules=[("helm get manifest", _proc(0, manifest))])
    r = _runner(tmp_path, monkeypatch, disp)
    rows = r._first_party_image_rows(chart_values={})
    assert len(rows) == 1
    assert rows[0]["status"] == "unknown_first_party"
    assert rows[0]["equal"] is False
    assert mismatched_components(rows) == ["unknown"]


def test_rows_third_party_image_is_skipped_entirely(tmp_path, monkeypatch):
    """A third-party image (no first-party prefix) never becomes a row at
    all — not even an `unknown_first_party` one."""
    manifest = _manifest_yaml(
        _deployment_doc("postgres", "postgres", "docker.io/bitnami/postgresql:16")
    )
    disp = _Dispatcher(rules=[("helm get manifest", _proc(0, manifest))])
    r = _runner(tmp_path, monkeypatch, disp)
    assert r._first_party_image_rows(chart_values={}) == []


def test_rows_scaled_to_zero_live_not_compared_s_f3(tmp_path, monkeypatch):
    """S-F3: `spec.replicas == 0` marks the row `scaled_to_zero` — live is
    NOT compared (no pods call made), `equal` reflects the render-vs-chart
    comparison alone.

    Falsifiable: drop the `scaled_to_zero` short-circuit (always attempt
    the live read) and this goes RED — `disp.calls` would include a
    `get pods` call for bff, which it must not.
    """
    manifest = _manifest_yaml(
        _deployment_doc(
            "audittrace-librechat-bff",
            "bff",
            "docker.io/lfds/audittrace-librechat-bff:1.29.1@sha256:bff-chart",
            replicas=0,
        )
    )
    disp = _Dispatcher(rules=[("helm get manifest", _proc(0, manifest))])
    r = _runner(tmp_path, monkeypatch, disp)
    rows = r._first_party_image_rows(chart_values=_CHART_VALUES)
    assert len(rows) == 1
    assert rows[0]["status"] == "scaled_to_zero"
    assert rows[0]["live"] is None
    assert rows[0]["equal"] is True
    assert not any("get pods" in " ".join(c) for c in disp.calls)


def test_rows_unpinned_local_registry_tag_form_s3a(tmp_path, monkeypatch):
    """S3a: an unpinned (local-registry, soft-failed digest) memory-server
    reference falls back to TAG-FORM equality and skips the live read."""
    manifest = _manifest_yaml(
        _deployment_doc(
            "audittrace-memory-server",
            "memory-server",
            "localhost:5000/audittrace/memory-server:9.9.9",
        )
    )
    disp = _Dispatcher(rules=[("helm get manifest", _proc(0, manifest))])
    r = _runner(
        tmp_path, monkeypatch, disp, image_ref=_local_ref_unpinned(), registry="local"
    )
    rows = r._first_party_image_rows(chart_values={})
    assert len(rows) == 1
    assert rows[0]["unpinned"] is True
    assert rows[0]["equal"] is True
    assert rows[0]["live"] is None


def test_rows_render_only_when_no_selector_known(tmp_path, monkeypatch):
    """The `tests` component has no pod selector — its row stays
    `render_only`; equality is render-vs-chart alone."""
    manifest = _manifest_yaml(
        _deployment_doc(
            "audittrace-tests-hook",
            "tests",
            "docker.io/lfds/audittrace-tests:1.29.1@sha256:tests-chart",
        )
    )
    disp = _Dispatcher(rules=[("helm get manifest", _proc(0, manifest))])
    r = _runner(tmp_path, monkeypatch, disp)
    rows = r._first_party_image_rows(chart_values=_CHART_VALUES)
    assert len(rows) == 1
    assert rows[0]["status"] == "render_only"
    assert rows[0]["equal"] is True


# ── S1: live pod selection rule ──────────────────────────────────────────


def test_rows_live_excludes_terminating_pod(tmp_path, monkeypatch):
    """A Terminating OLD pod (stale digest) beside a Running NEW pod (fresh
    digest) must not mask the fresh one — `equal` is True.

    Falsifiable: drop the `deletionTimestamp` filter and this goes RED —
    the digest DISAGREEMENT between the two pods makes `len(digests) != 1`,
    flipping `equal` to False.
    """
    manifest = _manifest_yaml(
        _deployment_doc(
            "audittrace-librechat-bff",
            "bff",
            "docker.io/lfds/audittrace-librechat-bff:1.29.1@sha256:bff-chart",
        )
    )
    pods = [
        _pod("bff", "sha256:STALE", deletion_timestamp=True),
        _pod("bff", "sha256:bff-chart"),
    ]
    disp = _Dispatcher(
        rules=[
            ("helm get manifest", _proc(0, manifest)),
            ("get deployment audittrace-librechat-bff -n", _proc(0, "")),  # no revision
            ("component=librechat-bff", _pods_json(pods)),
        ]
    )
    r = _runner(tmp_path, monkeypatch, disp)
    rows = r._first_party_image_rows(chart_values=_CHART_VALUES)
    assert len(rows) == 1
    assert rows[0]["equal"] is True
    assert rows[0]["live"] == "sha256:bff-chart"


def test_rows_live_requires_all_remaining_pods_to_agree(tmp_path, monkeypatch):
    """Two non-Terminating pods at the SAME selector disagreeing on digest
    -> unreadable -> unequal (fail-safe, never a silent first-match).

    Falsifiable: take the first pod's digest instead of requiring
    agreement across all of them, and this goes RED (equal flips True).
    """
    manifest = _manifest_yaml(
        _deployment_doc(
            "audittrace-librechat-bff",
            "bff",
            "docker.io/lfds/audittrace-librechat-bff:1.29.1@sha256:bff-chart",
        )
    )
    pods = [_pod("bff", "sha256:bff-chart"), _pod("bff", "sha256:STALE")]
    disp = _Dispatcher(
        rules=[
            ("helm get manifest", _proc(0, manifest)),
            ("get deployment audittrace-librechat-bff -n", _proc(0, "")),
            ("component=librechat-bff", _pods_json(pods)),
        ]
    )
    r = _runner(tmp_path, monkeypatch, disp)
    rows = r._first_party_image_rows(chart_values=_CHART_VALUES)
    assert rows[0]["equal"] is False
    assert rows[0]["live"] is None


def test_rows_live_init_container_is_compared(tmp_path, monkeypatch):
    """A first-party INIT container (the librechat
    `wait-for-oidc-discovery`) is compared too, via
    `initContainerStatuses` — a stale init-container digest is unequal.

    Falsifiable: read only `containerStatuses` (never
    `initContainerStatuses`) and this goes RED — the stale init container
    digest would never be found, `live` stays `None`... but since this
    test asserts `equal is False` via an UNREADABLE live read in that
    case too, pair it with the neuter note: the row would then report
    `unreadable` for the WRONG reason (missing lookup, not a real
    mismatch) — caught by a direct assertion on `rows[0]["live"]`.
    """
    manifest = _manifest_yaml(
        _deployment_doc(
            "audittrace-librechat",
            "librechat",
            "docker.io/lfds/audittrace-librechat:768de61@sha256:librechat-chart",
            init_containers=[
                {
                    "name": "wait-for-oidc-discovery",
                    "image": "docker.io/lfds/audittrace-librechat:768de61@sha256:librechat-chart",
                }
            ],
        )
    )
    pods = [
        _pod("librechat", "sha256:librechat-chart"),
    ]
    pods[0]["status"]["initContainerStatuses"] = [
        {"name": "wait-for-oidc-discovery", "imageID": "repo@sha256:STALE-INIT"}
    ]
    disp = _Dispatcher(
        rules=[
            ("helm get manifest", _proc(0, manifest)),
            ("get deployment audittrace-librechat -n", _proc(0, "")),
            ("component=librechat-bff", _proc(returncode=1)),
            ("component=librechat", _pods_json(pods)),
        ]
    )
    r = _runner(tmp_path, monkeypatch, disp)
    rows = r._first_party_image_rows(chart_values=_CHART_VALUES)
    by_container = {(row["container"], row["is_init"]): row for row in rows}
    main_row = by_container[("librechat", False)]
    init_row = by_container[("wait-for-oidc-discovery", True)]
    assert main_row["equal"] is True
    assert init_row["equal"] is False
    assert init_row["live"] == "sha256:STALE-INIT"


# ── low-level fail-safe reads: unreadable cluster calls, malformed docs ──


def test_live_manifest_docs_none_on_unparsable_yaml(tmp_path, monkeypatch):
    disp = _Dispatcher(rules=[("helm get manifest", _proc(0, ": not: yaml: at: all:"))])
    r = _runner(tmp_path, monkeypatch, disp)
    assert r._live_manifest_docs() is None


def test_deployment_revision_none_on_kubectl_failure(tmp_path, monkeypatch):
    disp = _Dispatcher(rules=[("get deployment x -n", _proc(returncode=1))])
    r = _runner(tmp_path, monkeypatch, disp)
    assert r._deployment_revision("x") is None


def test_current_pod_template_hash_none_when_rs_list_unreadable(tmp_path, monkeypatch):
    disp = _Dispatcher(
        rules=[
            ("get deployment x -n", _proc(0, "3")),
            ("get rs -l", _proc(returncode=1)),
        ]
    )
    r = _runner(tmp_path, monkeypatch, disp)
    assert r._current_pod_template_hash("x", "sel") is None


def test_current_pod_template_hash_none_on_unparsable_rs_json(tmp_path, monkeypatch):
    disp = _Dispatcher(
        rules=[
            ("get deployment x -n", _proc(0, "3")),
            ("get rs -l", _proc(0, "not json")),
        ]
    )
    r = _runner(tmp_path, monkeypatch, disp)
    assert r._current_pod_template_hash("x", "sel") is None


def test_current_pod_template_hash_skips_malformed_items_and_missing_hash(
    tmp_path, monkeypatch
):
    """A non-dict RS item and an item whose matching revision carries no
    `pod-template-hash` label are both skipped, falling through to the
    NEXT candidate (or `None`) rather than crashing."""
    rs_list = {
        "items": [
            "not-a-dict",
            {
                "metadata": {
                    "annotations": {"deployment.kubernetes.io/revision": "3"},
                    "labels": {},
                }
            },
            {
                "metadata": {
                    "annotations": {"deployment.kubernetes.io/revision": "3"},
                    "labels": {"pod-template-hash": "the-hash"},
                }
            },
        ]
    }
    disp = _Dispatcher(
        rules=[
            ("get deployment x -n", _proc(0, "3")),
            ("get rs -l", _proc(0, json.dumps(rs_list))),
        ]
    )
    r = _runner(tmp_path, monkeypatch, disp)
    assert r._current_pod_template_hash("x", "sel") == "the-hash"


def test_selector_pods_empty_on_kubectl_failure(tmp_path, monkeypatch):
    disp = _Dispatcher(rules=[("get pods -l", _proc(returncode=1))])
    r = _runner(tmp_path, monkeypatch, disp)
    assert r._selector_pods("sel") == []


def test_live_row_digest_unreadable_when_container_status_missing(
    tmp_path, monkeypatch
):
    """A candidate pod exists but carries no status entry for the named
    container at all -> unreadable, never a silent `None`-digest match."""
    manifest = _manifest_yaml(
        _deployment_doc(
            "audittrace-librechat-bff",
            "bff",
            "docker.io/lfds/audittrace-librechat-bff:1.29.1@sha256:bff-chart",
        )
    )
    pod_missing_container_status = {
        "metadata": {"labels": {}},
        "status": {
            "containerStatuses": [
                {"name": "OTHER-CONTAINER", "imageID": "repo@sha256:x"}
            ]
        },
    }
    disp = _Dispatcher(
        rules=[
            ("helm get manifest", _proc(0, manifest)),
            ("get deployment audittrace-librechat-bff -n", _proc(0, "")),
            ("component=librechat-bff", _pods_json([pod_missing_container_status])),
        ]
    )
    r = _runner(tmp_path, monkeypatch, disp)
    rows = r._first_party_image_rows(chart_values=_CHART_VALUES)
    assert rows[0]["equal"] is False
    assert rows[0]["live"] is None


def test_rows_skips_doc_with_no_name_or_no_pod_spec(tmp_path, monkeypatch):
    """A workload-kind doc missing `metadata.name` or an unreadable pod
    spec contributes NO rows (never a crash)."""
    manifest = _manifest_yaml(
        {"kind": "Deployment", "spec": {"template": {"spec": {"containers": []}}}},
        {"kind": "Deployment", "metadata": {"name": "x"}, "spec": "not-a-dict"},
    )
    disp = _Dispatcher(rules=[("helm get manifest", _proc(0, manifest))])
    r = _runner(tmp_path, monkeypatch, disp)
    assert r._first_party_image_rows(chart_values=_CHART_VALUES) == []


def test_rows_skips_malformed_container_entries(tmp_path, monkeypatch):
    """A non-dict container entry, and a container missing `image` or
    `name`, are both skipped — never a crash, never a phantom row."""
    doc = _deployment_doc("audittrace-librechat-bff", "bff", "ignored")
    doc["spec"]["template"]["spec"]["containers"] = [
        "not-a-dict",
        {"name": "no-image"},
        {"image": "docker.io/lfds/audittrace-librechat-bff:1.29.1@sha256:x"},  # no name
    ]
    manifest = _manifest_yaml(doc)
    disp = _Dispatcher(rules=[("helm get manifest", _proc(0, manifest))])
    r = _runner(tmp_path, monkeypatch, disp)
    assert r._first_party_image_rows(chart_values=_CHART_VALUES) == []


# ── S-F2: current-ReplicaSet selection by revision annotation ───────────


def test_rows_current_rs_selected_by_revision_not_creation_time(tmp_path, monkeypatch):
    """S-F2: after a `rollout undo`, the OLD ReplicaSet is RE-SCALED (not
    re-created) so it can be the NEWEST by creation time while carrying an
    OLDER `deployment.kubernetes.io/revision`. Selecting by revision
    annotation match (never `--sort-by=.metadata.creationTimestamp`) picks
    the pod-template-hash that actually matches the Deployment's CURRENT
    revision.

    Falsifiable: select the RS by creation-timestamp order instead of the
    revision-annotation match and this goes RED under the fixture below —
    the stale-hash pod would be (wrongly) treated as current.
    """
    manifest = _manifest_yaml(
        _deployment_doc(
            "audittrace-librechat-bff",
            "bff",
            "docker.io/lfds/audittrace-librechat-bff:1.29.1@sha256:bff-chart",
        )
    )
    rs_list = {
        "items": [
            {
                "metadata": {
                    "annotations": {"deployment.kubernetes.io/revision": "3"},
                    "labels": {"pod-template-hash": "current-hash"},
                }
            },
            {
                "metadata": {
                    "annotations": {"deployment.kubernetes.io/revision": "4"},
                    "labels": {"pod-template-hash": "stale-hash"},
                }
            },
        ]
    }
    pods = [
        _pod("bff", "sha256:bff-chart", labels={"pod-template-hash": "current-hash"}),
        _pod("bff", "sha256:STALE", labels={"pod-template-hash": "stale-hash"}),
    ]
    disp = _Dispatcher(
        rules=[
            ("helm get manifest", _proc(0, manifest)),
            (
                "get deployment audittrace-librechat-bff -n",
                _proc(0, "3"),  # the Deployment's CURRENT revision is 3
            ),
            ("get rs -l", _proc(0, json.dumps(rs_list))),
            ("component=librechat-bff", _pods_json(pods)),
        ]
    )
    r = _runner(tmp_path, monkeypatch, disp)
    rows = r._first_party_image_rows(chart_values=_CHART_VALUES)
    assert rows[0]["equal"] is True
    assert rows[0]["live"] == "sha256:bff-chart"


# ── B1: the no-op bypass, reproduced AND closed ──────────────────────────


def test_b1_stale_stored_override_forces_apply_not_noop(tmp_path, monkeypatch):
    """The exact Rule-B1 fixture (Addendum A §1): memory-server already
    converged, helm `deployed`, no config drift, BFF present in the
    manifest at a STALE digest, file-side `console.enabled=False`, NO
    `-f` overlay -> `_is_converged()` must be False (so `helm upgrade`
    actually runs, carrying D2's unconditional console sets).

    Falsifiable: restore the `console.enabled` gate anywhere in this path
    (`_is_converged` or the row builder) and this goes RED —
    `check.converged` flips True, reproducing the rev-272 no-op live.
    """
    manifest = _manifest_yaml(
        _deployment_doc(
            "audittrace-memory-server",
            "memory-server",
            "docker.io/lfds/audittrace-memory-server:9.9.9@sha256:ms",
        ),
        _deployment_doc(
            "audittrace-librechat-bff",
            "bff",
            "docker.io/lfds/audittrace-librechat-bff:1.29.1@sha256:STALE-STORED",
        ),
    )
    disp = _Dispatcher(
        rules=[
            ("helm get manifest", _proc(0, manifest)),
            ('"memory-server")].imageID', _proc(0, "repo@sha256:ms")),
            ("get deployment audittrace-librechat-bff -n", _proc(0, "")),
            (
                "component=librechat-bff",
                _pods_json([_pod("bff", "sha256:STALE-STORED")]),
            ),
            (
                "helm status",
                _proc(0, json.dumps({"version": 272, "info": {"status": "deployed"}})),
            ),
        ]
    )
    r = _runner(
        tmp_path, monkeypatch, disp, image_ref=_hub_ref("sha256:ms"), values_files=()
    )
    disabled_console_values = {
        **_CHART_VALUES,
        "console": {**_CHART_VALUES["console"], "enabled": False},
    }
    check = r._is_converged(chart_values=disabled_console_values)
    assert check.converged is False
    assert "first-party mismatch: bff" in check.basis
    # Convergence stopped BEFORE reading helm status — a mismatch runs the
    # apply unconditionally (#451 efficiency, preserved).
    assert not any("helm status" in " ".join(c) for c in disp.calls)


# AC3d (the all-equal control that must stay GREEN under every neuter above)
# is covered end-to-end through the full `_is_converged` + config-drift path
# in tests/test_deploy_runner.py::test_is_converged_true_when_all_first_party_images_match
# — not duplicated here; this module stays focused on `images.py` itself.


# ── FIRST_PARTY_PREFIXES sanity ──────────────────────────────────────────


def test_first_party_prefixes_are_the_two_documented_ones():
    assert FIRST_PARTY_PREFIXES == (
        "docker.io/lfds/",
        "localhost:5000/audittrace/",
    )


def test_selector_for_component_unknown_returns_none():
    assert images._selector_for_component("postgres") is None
