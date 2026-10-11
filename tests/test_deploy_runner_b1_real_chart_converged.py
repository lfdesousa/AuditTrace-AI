"""B-1 converged-control, permanent test (fix round 1, review verdict
2026-10-08-REVIEW-bff-bump-stale-override.md).

The pod-reaper (`charts/audittrace/templates/reaper/deployment.yaml`) runs
the MEMORY-SERVER image in a container named `pod-reaper`, under its OWN
Deployment with its OWN, DISTINCT selector
(`app.kubernetes.io/component=pod-reaper`) — never memory-server's
(`...=memory-server`). Keying the live read off a component->selector
LOOKUP TABLE (the component identity of the IMAGE, not the workload's own
manifest selector) reads the wrong pods for the reaper's row: zero pods
match `component=memory-server` filtered for a `pod-reaper` container, so
the row comes back `unreadable` — mismatched — and EVERY real deploy exits
8, even when the cluster is perfectly converged. This is the reviewer's
`repro_converged.py`, ported to a permanent pytest assertion against the
REAL `helm template` render of this commit's chart — no synthetic
stand-in for the manifest shape.

Falsifiable: key the live read off `_selector_for_component(component)`
(a component->selector table) instead of
`_selector_from_manifest_doc(doc)` (the workload's OWN
`spec.selector.matchLabels`) and this goes RED — the pod-reaper row
becomes `unreadable`, `mismatched_components` includes `"memory-server"`,
and the CLI exit flips from 0 to 8.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.deploy import registry, runner
from scripts.deploy.runner import images

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART_DIR = REPO_ROOT / "charts" / "audittrace"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None,
    reason="helm CLI not on PATH — this control needs a real helm template render",
)

_MS_DIGEST = "sha256:" + "6b" * 32

_LINT_SECRETS: list[str] = []
for _kv in (
    "secrets.minio.secretKey=ci-test",
    "secrets.minio.kmsKey=ci-test",
    "secrets.chromadb.token=ci-test",
    "secrets.keycloak.adminPassword=ci-test",
    "secrets.postgres.appPassword=ci-test",
    "secrets.postgres.password=ci-test",
    "secrets.redis.password=ci-test",
    "secrets.summariser.password=ci-test",
    "secrets.console.bffExchangeClientSecret=ci-test",
    "externalLLM.host=llm.test.invalid",
    "observability.external.langfuseHost=l.invalid",
    "observability.external.tempoHost=t.invalid",
    "observability.external.lokiHost=k.invalid",
    "console.enabled=true",
    "console.frontDoorNodeName=test-node",
    "memoryServer.image.repository=docker.io/lfds/audittrace-memory-server",
    f"memoryServer.image.tag=1.29.1@{_MS_DIGEST}",
):
    _LINT_SECRETS.extend(["--set", _kv])


def _render_real_chart() -> tuple[list[dict], dict]:
    """The REAL `helm template` render of this commit's chart (console
    enabled, the runner's own D2 sets, the memory-server digest) ->
    (docs, chart_values)."""
    chart_values = runner._read_chart_values(values_files=())
    d2_sets = runner.console_image_set_args(chart_values)
    cmd = [
        "helm",
        "template",
        "audittrace",
        str(CHART_DIR),
        "-n",
        "audittrace",
        *_LINT_SECRETS,
        *d2_sets,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"helm template failed:\n{result.stderr}")
    docs = [d for d in yaml.safe_load_all(result.stdout) if isinstance(d, dict)]
    return docs, chart_values


def _converged_fake_cluster(docs: list[dict]):
    """Builds a fully-converged fake cluster from the rendered docs: one
    pod per workload, every container reporting exactly its rendered
    digest, selected via the workload's OWN `spec.selector.matchLabels` —
    the real selector a kubectl -l query would actually need."""
    pods_by_selector: dict[str, list[dict]] = {}
    revisions: dict[str, str] = {}

    def _digest_of(image: str) -> str:
        if "@" in image:
            return image.split("@", 1)[1]
        return "sha256:" + "00" * 32

    for doc in docs:
        if doc.get("kind") not in ("Deployment", "StatefulSet"):
            continue
        selector = images._selector_from_manifest_doc(doc)
        if selector is None:
            continue
        pod_spec = doc["spec"]["template"]["spec"]
        containers = pod_spec.get("containers") or []
        init_containers = pod_spec.get("initContainers") or []
        pod = {
            "metadata": {
                "labels": {"pod-template-hash": "h-" + doc["metadata"]["name"]}
            },
            "status": {
                "containerStatuses": [
                    {
                        "name": c["name"],
                        "imageID": "docker.io/x@" + _digest_of(c["image"]),
                    }
                    for c in containers
                ],
                "initContainerStatuses": [
                    {
                        "name": c["name"],
                        "imageID": "docker.io/x@" + _digest_of(c["image"]),
                    }
                    for c in init_containers
                ],
            },
        }
        pods_by_selector.setdefault(selector, []).append(pod)
        revisions[doc["metadata"]["name"]] = selector

    return pods_by_selector, revisions


def _fake_run_factory(manifest_text: str, pods_by_selector: dict, revisions: dict):
    def fake_run(cmd, *, env=None):
        joined = " ".join(cmd)
        ok = lambda stdout: subprocess.CompletedProcess(cmd, 0, stdout, "")  # noqa: E731
        if cmd[:3] == ["helm", "get", "manifest"]:
            return ok(manifest_text)
        if "get pods" in joined and "-l" in cmd:
            selector = cmd[cmd.index("-l") + 1]
            return ok(json.dumps({"items": pods_by_selector.get(selector, [])}))
        if "get rs" in joined:
            selector = cmd[cmd.index("-l") + 1]
            items = [
                {
                    "metadata": {
                        "annotations": {"deployment.kubernetes.io/revision": "1"},
                        "labels": {"pod-template-hash": "h-" + name},
                    }
                }
                for name, sel in revisions.items()
                if sel == selector
            ]
            return ok(json.dumps({"items": items}))
        if "jsonpath={.metadata.annotations" in joined:
            return ok("1")
        return ok("")

    return fake_run


def test_converged_control_real_chart_all_rows_equal(tmp_path, monkeypatch):
    docs, chart_values = _render_real_chart()
    manifest_text = "\n---\n".join(json.dumps(d) for d in docs)
    pods_by_selector, revisions = _converged_fake_cluster(docs)

    # Sanity precondition: the pod-reaper really is in this render, as its
    # OWN Deployment, distinct from memory-server's.
    names = {d["metadata"]["name"] for d in docs if d.get("kind") == "Deployment"}
    assert "audittrace-pod-reaper" in names
    assert "audittrace-memory-server" in names

    monkeypatch.setattr(
        runner, "_run", _fake_run_factory(manifest_text, pods_by_selector, revisions)
    )
    cfg = runner.DeployConfig(target_version="v1.29.1", out_dir=tmp_path / "runs")
    r = runner.DeployRunner(cfg)
    r.image_ref = registry.ImageRef(
        "docker.io/lfds/audittrace-memory-server", "1.29.1", _MS_DIGEST, "hub"
    )
    rows = r._first_party_image_rows(chart_values=chart_values, manifest_docs=docs)

    by_workload = {
        (row["workload"], row["container"], row["is_init"]): row for row in rows
    }
    assert ("audittrace-pod-reaper", "pod-reaper", False) in by_workload, (
        "expected a row for the pod-reaper's own container"
    )
    for key, row in by_workload.items():
        assert row["equal"] is True, f"row {key} unexpectedly unequal: {row}"
    assert images.mismatched_components(rows) == []


def test_neuter_component_selector_table_breaks_the_pod_reaper_row(
    tmp_path, monkeypatch
):
    """The exact neuter the review asked for: key the live read off a
    component->selector TABLE (the pre-fix mechanism) instead of the
    workload's own manifest selector, on the SAME converged fixture above
    — the pod-reaper row must go RED (unreadable, mismatched)."""
    docs, chart_values = _render_real_chart()
    manifest_text = "\n---\n".join(json.dumps(d) for d in docs)
    pods_by_selector, revisions = _converged_fake_cluster(docs)
    monkeypatch.setattr(
        runner, "_run", _fake_run_factory(manifest_text, pods_by_selector, revisions)
    )
    cfg = runner.DeployConfig(target_version="v1.29.1", out_dir=tmp_path / "runs")
    r = runner.DeployRunner(cfg)
    r.image_ref = registry.ImageRef(
        "docker.io/lfds/audittrace-memory-server", "1.29.1", _MS_DIGEST, "hub"
    )

    # NEUTER: for the pod-reaper's row, resolve the selector by the IMAGE's
    # component identity (memory-server) instead of the WORKLOAD's own
    # manifest selector — exactly the pre-fix `_selector_for_component`
    # mechanism's effect (it keyed on the row's assigned component, which
    # for the reaper's row IS "memory-server", not on which Deployment the
    # row actually came from). Every other workload keeps its real,
    # unmodified selector.
    _real_selector = images._selector_from_manifest_doc

    def _neutered_selector(doc):
        if (doc.get("metadata") or {}).get("name") == "audittrace-pod-reaper":
            return "app.kubernetes.io/component=memory-server"
        return _real_selector(doc)

    monkeypatch.setattr(images, "_selector_from_manifest_doc", _neutered_selector)
    rows = r._first_party_image_rows(chart_values=chart_values, manifest_docs=docs)
    reaper_rows = [r2 for r2 in rows if r2["workload"] == "audittrace-pod-reaper"]
    assert reaper_rows, "expected a pod-reaper row"
    assert any(not row["equal"] for row in reaper_rows), (
        "expected the pod-reaper row to be unequal under the component-table neuter"
    )
    assert "memory-server" in images.mismatched_components(rows)
