"""Structural tests for the Promtail Helm chart templates.

Promtail ships the memory-server stdout (and every other audittrace
container) to the sibling Loki instance. If any of DaemonSet,
ConfigMap, ServiceAccount, ClusterRole, ClusterRoleBinding are missing
or mis-wired, log shipping silently breaks and the Container Logs
panel on "Sovereign AI Operations" goes dark.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART_DIR = Path(__file__).resolve().parent.parent / "charts" / "audittrace"


def _render_chart() -> list[dict]:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm not installed")
    result = subprocess.run(
        [helm, "template", "audittrace", str(CHART_DIR), "--namespace", "audittrace"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def _find(resources: list[dict], kind: str, name: str) -> dict:
    for r in resources:
        if r.get("kind") == kind and r.get("metadata", {}).get("name") == name:
            return r
    raise AssertionError(f"{kind}/{name} not found in rendered chart")


@pytest.fixture(scope="module")
def rendered() -> list[dict]:
    return _render_chart()


def test_promtail_daemonset_exists_and_wires_rbac(rendered: list[dict]) -> None:
    ds = _find(rendered, "DaemonSet", "audittrace-promtail")
    spec = ds["spec"]["template"]["spec"]
    assert spec["serviceAccountName"] == "audittrace-promtail"
    assert spec.get("tolerations"), "must tolerate NoSchedule so it lands on every node"
    container = spec["containers"][0]
    assert container["name"] == "promtail"
    mounts = {m["name"]: m for m in container["volumeMounts"]}
    # These read-only host mounts are mandatory for log discovery.
    assert "pods" in mounts and mounts["pods"]["readOnly"] is True
    assert "containers" in mounts and mounts["containers"]["readOnly"] is True
    assert "config" in mounts


def test_promtail_rbac_bindings_are_consistent(rendered: list[dict]) -> None:
    sa = _find(rendered, "ServiceAccount", "audittrace-promtail")
    cr = _find(rendered, "ClusterRole", "audittrace-promtail")
    crb = _find(rendered, "ClusterRoleBinding", "audittrace-promtail")
    # ClusterRole must permit pod/node discovery for kubernetes_sd_configs.
    rules = cr["rules"]
    resources = {res for rule in rules for res in rule.get("resources", [])}
    verbs = {v for rule in rules for v in rule.get("verbs", [])}
    assert "pods" in resources
    assert "nodes" in resources
    assert {"get", "list", "watch"}.issubset(verbs)
    # Binding links the correct SA to the correct CR.
    assert crb["roleRef"]["name"] == cr["metadata"]["name"]
    subjects = crb["subjects"]
    assert any(
        s.get("kind") == "ServiceAccount" and s.get("name") == sa["metadata"]["name"]
        for s in subjects
    )


def test_promtail_config_targets_loki_and_filters_namespace(
    rendered: list[dict],
) -> None:
    cm = _find(rendered, "ConfigMap", "audittrace-promtail-config")
    cfg = yaml.safe_load(cm["data"]["promtail.yaml"])
    clients = cfg["clients"]
    assert len(clients) == 1
    url = clients[0]["url"]
    assert url.startswith("http://"), (
        "Loki endpoint must be HTTP (host-resident, not mesh)"
    )
    assert "/loki/api/v1/push" in url
    # namespace filter keeps log volume bounded to audittrace.
    scrape = cfg["scrape_configs"][0]
    keep_rule = next(
        (r for r in scrape["relabel_configs"] if r.get("action") == "keep"),
        None,
    )
    assert keep_rule is not None, "config must drop non-audittrace namespaces"
    assert keep_rule["regex"] == "audittrace"
