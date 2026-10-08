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

import pytest

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


def _deployment_doc(
    name,
    container,
    image,
    *,
    replicas=1,
    init_containers=None,
    selector_labels=None,
    kind="Deployment",
):
    """A minimal workload manifest doc (``kind="Deployment"`` by default),
    including ``spec.selector.matchLabels`` (fix-round B-1: the live read
    is now keyed on THIS, never a component lookup table). Defaults to
    ``{app.kubernetes.io/component: <name without the "audittrace-"
    prefix>}`` — e.g. ``audittrace-librechat-bff`` -> ``librechat-bff`` —
    which matches every existing ``component=...`` dispatch needle in this
    module; pass ``selector_labels`` explicitly to model a workload whose
    selector does NOT follow that convention (the pod-reaper tests do).

    Pass ``kind="Job"`` (or ``"CronJob"``) to model a workload Kubernetes
    does NOT require a selector on (fix round 2, R2-2) — the only kinds a
    missing/undetectable selector legitimately means ``render_only``
    rather than ``unreadable``."""
    containers = [{"name": container, "image": image}]
    pod_spec = {"containers": containers}
    if init_containers:
        pod_spec["initContainers"] = init_containers
    if selector_labels is None:
        selector_labels = {
            "app.kubernetes.io/component": name.removeprefix("audittrace-")
        }
    return {
        "kind": kind,
        "metadata": {"name": name},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": selector_labels},
            "template": {"spec": pod_spec},
        },
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


def test_rows_unreadable_manifest_is_an_unequal_sentinel(tmp_path, monkeypatch):
    """B-2 (fix round 1): `helm get manifest` failing must NOT be read as
    "nothing to compare" ([], trivially convergeable) — that let a stale
    BFF ride through a transient helm hiccup with exit 0 (the rev-272
    outcome reached through a new door, per the reviewer's repro). It is
    its own unequal sentinel row instead.

    Falsifiable: revert to returning `[]` here and this goes RED (the
    sentinel row disappears, `mismatched_components` returns `[]`).
    """
    disp = _Dispatcher(rules=[("helm get manifest", _proc(returncode=1))])
    r = _runner(tmp_path, monkeypatch, disp)
    rows = r._first_party_image_rows(chart_values=_CHART_VALUES)
    assert len(rows) == 1
    assert rows[0]["status"] == "manifest_unreadable"
    assert rows[0]["equal"] is False
    assert mismatched_components(rows) == ["manifest"]


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


def test_rows_unpinned_local_registry_tag_form_mismatch_is_unequal(
    tmp_path, monkeypatch
):
    """V-4 (fix round 1): the unpinned tag-form comparison is a REAL
    comparison, not a rubber stamp — a rendered tag that does NOT match
    `image_ref.tag` is unequal. Paired with the positive case above so the
    CONTRAST, not one isolated assertion, proves the comparator fires."""
    manifest = _manifest_yaml(
        _deployment_doc(
            "audittrace-memory-server",
            "memory-server",
            "localhost:5000/audittrace/memory-server:STALE-TAG",
        )
    )
    disp = _Dispatcher(rules=[("helm get manifest", _proc(0, manifest))])
    r = _runner(
        tmp_path, monkeypatch, disp, image_ref=_local_ref_unpinned(), registry="local"
    )
    rows = r._first_party_image_rows(chart_values={})
    assert len(rows) == 1
    assert rows[0]["unpinned"] is True
    assert rows[0]["equal"] is False


def test_rows_render_only_when_no_selector_known(tmp_path, monkeypatch):
    """A Job (the ONLY kind, with CronJob, a missing selector is
    legitimate for — R2-2, fix round 2: Kubernetes does not require one,
    and a Helm-test hook has no standing pods to compare live) has no
    selector this runner can read live pods through — its row stays
    `render_only`; equality is render-vs-chart alone."""
    manifest = _manifest_yaml(
        _deployment_doc(
            "audittrace-tests-hook",
            "tests",
            "docker.io/lfds/audittrace-tests:1.29.1@sha256:tests-chart",
            selector_labels={},
            kind="Job",
        )
    )
    disp = _Dispatcher(rules=[("helm get manifest", _proc(0, manifest))])
    r = _runner(tmp_path, monkeypatch, disp)
    rows = r._first_party_image_rows(chart_values=_CHART_VALUES)
    assert len(rows) == 1
    assert rows[0]["status"] == "render_only"
    assert rows[0]["equal"] is True


def test_rows_render_only_mismatch_is_unequal(tmp_path, monkeypatch):
    """V-5 (fix round 1): a `render_only` row (a Job, no selector known) is
    STILL a real comparison — a rendered digest that disagrees with the
    chart is unequal, never a silent pass just because live can't be
    checked. Paired with the positive case above."""
    manifest = _manifest_yaml(
        _deployment_doc(
            "audittrace-tests-hook",
            "tests",
            "docker.io/lfds/audittrace-tests:1.29.1@sha256:STALE-TESTS",
            selector_labels={},
            kind="Job",
        )
    )
    disp = _Dispatcher(rules=[("helm get manifest", _proc(0, manifest))])
    r = _runner(tmp_path, monkeypatch, disp)
    rows = r._first_party_image_rows(chart_values=_CHART_VALUES)
    assert len(rows) == 1
    assert rows[0]["status"] == "render_only"
    assert rows[0]["equal"] is False


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


# ── R2-2 (fix round 2): a long-lived kind with NO DERIVABLE selector ─────
# must be `unreadable` (unequal), never `render_only` — Kubernetes
# REQUIRES a selector on Deployment/StatefulSet/DaemonSet, so a selector
# this runner cannot derive from the manifest means "cannot read", not
# "nothing to read" (reviewer repro:
# 2026-10-08-REVIEW-bff-bump-fix1-repro_selector_edges.py).


@pytest.mark.parametrize("kind", ["Deployment", "StatefulSet", "DaemonSet"])
@pytest.mark.parametrize(
    "selector_labels",
    [
        {},  # no matchLabels at all (e.g. matchExpressions-only in the real doc)
        {"app": "bff"},  # matchLabels present, but no component key
    ],
)
def test_rows_long_lived_kind_without_derivable_selector_is_unreadable(
    tmp_path, monkeypatch, kind, selector_labels
):
    """Deployment/StatefulSet/DaemonSet with a selector this runner
    cannot derive a component from -> `unreadable`, equal=False, even
    though the chart-vs-rendered comparison alone would otherwise say
    equal (the live digest is STALE and must never be silently skipped).

    Falsifiable: drop the ``kind in _LONG_LIVED_SELECTOR_KINDS`` branch in
    `_live_row_digest` (treat every `None` selector as `render_only`) and
    this goes RED — `equal` flips to `True` despite the stale live pod.
    """
    manifest = _manifest_yaml(
        _deployment_doc(
            "audittrace-librechat-bff",
            "bff",
            "docker.io/lfds/audittrace-librechat-bff:1.29.1@sha256:bff-chart",
            selector_labels=selector_labels,
            kind=kind,
        )
    )
    # Live pods, if they were ever read, would be STALE — proving this is
    # about the selector being undetectable, not about there being no
    # live pods to find.
    disp = _Dispatcher(
        rules=[
            ("helm get manifest", _proc(0, manifest)),
            ("get pods", _pods_json([_pod("bff", "sha256:STALE")])),
        ]
    )
    r = _runner(tmp_path, monkeypatch, disp)
    rows = r._first_party_image_rows(chart_values=_CHART_VALUES)
    assert len(rows) == 1
    assert rows[0]["status"] == "unreadable"
    assert rows[0]["equal"] is False
    assert rows[0]["live"] is None


def test_rows_job_without_selector_stays_render_only_not_unreadable(
    tmp_path, monkeypatch
):
    """The CONTROL: a Job (never required to carry a selector) with no
    `spec.selector` stays `render_only` — the R2-2 fix must not
    over-correct into flagging every selector-less workload."""
    manifest = _manifest_yaml(
        _deployment_doc(
            "audittrace-tests-hook",
            "tests",
            "docker.io/lfds/audittrace-tests:1.29.1@sha256:tests-chart",
            selector_labels={},
            kind="Job",
        )
    )
    disp = _Dispatcher(rules=[("helm get manifest", _proc(0, manifest))])
    r = _runner(tmp_path, monkeypatch, disp)
    rows = r._first_party_image_rows(chart_values=_CHART_VALUES)
    assert len(rows) == 1
    assert rows[0]["status"] == "render_only"
    assert rows[0]["equal"] is True


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
        # The STALE RS listed FIRST — a neuter that matches unconditionally
        # (ignoring the revision annotation) would pick THIS one, proving the
        # test is not order-dependent-vacuous.
        "items": [
            {
                "metadata": {
                    "annotations": {"deployment.kubernetes.io/revision": "4"},
                    "labels": {"pod-template-hash": "stale-hash"},
                }
            },
            {
                "metadata": {
                    "annotations": {"deployment.kubernetes.io/revision": "3"},
                    "labels": {"pod-template-hash": "current-hash"},
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


# ── R2-1 (fix round 2): AUDITTRACE_FIRST_PARTY_IMAGE_PREFIXES + the
# committed-repository gate — each closes a reviewer-named vacuous neuter
# (E1/E2/E3) that stayed GREEN with zero tests in round 1.


def test_env_prefix_is_additive_default_prefix_still_recognizes_images(
    tmp_path, monkeypatch
):
    """E1: with AUDITTRACE_FIRST_PARTY_IMAGE_PREFIXES set to an UNRELATED
    prefix, an image under the DEFAULT `docker.io/lfds/` prefix — NOT in
    the committed map, so its recognition depends PURELY on the prefix
    match, never on the `repository in committed` escape hatch — is still
    recognised as first-party (an `unknown_first_party` row). The env var
    must ADD to the defaults, never REPLACE them.

    Falsifiable: `return extra or FIRST_PARTY_PREFIXES` (env narrows/
    replaces the defaults whenever it is set) and this goes RED — the
    image no longer matches ANY configured prefix (and is not committed
    either), so it is read as third-party and produces NO row at all.
    """
    monkeypatch.setenv(images.FIRST_PARTY_PREFIXES_ENV_VAR, "x.example/")
    manifest = _manifest_yaml(
        _deployment_doc(
            "audittrace-llm-stub",
            "llm-stub",
            "docker.io/lfds/audittrace-llm-stub:1.29.1@sha256:unknown",
        )
    )
    disp = _Dispatcher(rules=[("helm get manifest", _proc(0, manifest))])
    r = _runner(tmp_path, monkeypatch, disp)
    rows = r._first_party_image_rows(chart_values={})  # nothing committed
    assert len(rows) == 1
    assert rows[0]["status"] == "unknown_first_party"


def test_env_prefix_extends_recognition_for_a_new_repository(tmp_path, monkeypatch):
    """E2: with AUDITTRACE_FIRST_PARTY_IMAGE_PREFIXES set to
    `ecr.example/ns/`, an image under that repository (NOT in the
    committed map) is recognised as first-party — an `unknown_first_party`
    row — proving the env var actually extends recognition rather than
    existing unused.

    Falsifiable: `return FIRST_PARTY_PREFIXES` (ignore the env var
    entirely) and this goes RED — the image matches neither a default
    prefix nor any committed repository, so it is read as third-party and
    produces no row at all.
    """
    monkeypatch.setenv(images.FIRST_PARTY_PREFIXES_ENV_VAR, "ecr.example/ns/")
    manifest = _manifest_yaml(
        _deployment_doc(
            "ecr-mirror-workload",
            "foo",
            "ecr.example/ns/foo:1.0@sha256:unknown",
            selector_labels={"app.kubernetes.io/component": "ecr-mirror-workload"},
        )
    )
    disp = _Dispatcher(rules=[("helm get manifest", _proc(0, manifest))])
    r = _runner(tmp_path, monkeypatch, disp)
    rows = r._first_party_image_rows(chart_values=_CHART_VALUES)
    assert len(rows) == 1
    assert rows[0]["status"] == "unknown_first_party"
    assert rows[0]["equal"] is False


def test_committed_repository_is_first_party_regardless_of_prefix(
    tmp_path, monkeypatch
):
    """E3: a committed overlay repository (e.g. a mirror outside BOTH the
    default prefixes and any configured env prefix) is STILL recognised
    as first-party and compared — at a stale digest, unequal.

    Falsifiable: drop `repository in committed or` from the gating check
    and this goes RED — the row disappears (the mirror repository
    matches no prefix at all, default or configured).
    """
    monkeypatch.delenv(images.FIRST_PARTY_PREFIXES_ENV_VAR, raising=False)
    chart_values = {
        "console": {
            "bff": {
                "image": {
                    "repository": "mirror.example/audittrace-librechat-bff",
                    "tag": "1.29.1",
                    "digest": "sha256:bff-chart",
                }
            }
        }
    }
    manifest = _manifest_yaml(
        _deployment_doc(
            "audittrace-librechat-bff",
            "bff",
            "mirror.example/audittrace-librechat-bff:1.29.1@sha256:STALE",
        )
    )
    disp = _Dispatcher(rules=[("helm get manifest", _proc(0, manifest))])
    r = _runner(tmp_path, monkeypatch, disp)
    rows = r._first_party_image_rows(chart_values=chart_values)
    assert len(rows) == 1
    assert rows[0]["equal"] is False


# ── FIRST_PARTY_PREFIXES sanity ──────────────────────────────────────────


def test_first_party_prefixes_are_the_two_documented_ones():
    assert FIRST_PARTY_PREFIXES == (
        "docker.io/lfds/",
        "localhost:5000/audittrace/",
    )


def test_selector_from_manifest_doc_uses_the_component_label_only():
    """The EXTRA `name`/`instance` labels this chart's
    `audittrace.selectorLabels` also sets are deliberately NOT included —
    `app.kubernetes.io/component` alone is already unique per workload in
    this chart, and a single-label selector stays simple to reason about
    and to test against."""
    doc = {
        "spec": {
            "selector": {
                "matchLabels": {
                    "app.kubernetes.io/component": "memory-server",
                    "app.kubernetes.io/instance": "audittrace",
                    "app.kubernetes.io/name": "audittrace",
                }
            }
        }
    }
    assert (
        images._selector_from_manifest_doc(doc)
        == "app.kubernetes.io/component=memory-server"
    )


def test_selector_from_manifest_doc_none_when_absent_or_malformed():
    assert images._selector_from_manifest_doc({}) is None
    assert (
        images._selector_from_manifest_doc(
            {"spec": {"selector": {"matchLabels": {"other-key": "x"}}}}
        )
        is None
    )
    assert images._selector_from_manifest_doc({"spec": "not-a-dict"}) is None
    assert images._selector_from_manifest_doc({"spec": {"selector": {}}}) is None
    assert (
        images._selector_from_manifest_doc({"spec": {"selector": {"matchLabels": {}}}})
        is None
    )
