"""Drift-guards for chart label stability across helm upgrades.

Background — K8s ``Deployment.spec.selector`` is **immutable**: a
label change between helm upgrades fails with ``field is immutable``.
The chart's ``_helpers.tpl`` derives ``app.kubernetes.io/name`` from
``.Chart.Name``, so anything that mutates ``Chart.yaml::name``
between releases would break upgrades for every existing operator.

cc-repo (``audittrace-content-control``) mitigates this in
``_helpers.tpl`` by hardcoding the chart-identity name (image and
chart there share the name ``audittrace-content-control``, so a
push-time rename is required to avoid an OCI ref collision).

For ``audittrace`` the image is at ``lfds/audittrace-memory-server``
and the chart is at ``lfds/audittrace`` — distinct OCI repositories
already, no rename required. The defense is therefore one layer up:
``.github/workflows/publish.yml`` MUST NOT mutate ``Chart.yaml::name``,
and the chart's helpers stay free to use ``.Chart.Name`` dynamically.

These tests pin both halves of that contract:

1. The source-tree chart renders with the expected
   ``app.kubernetes.io/name: audittrace`` selector label on every
   workload kind (Deployment, StatefulSet, DaemonSet).
2. ``publish.yml`` does not contain any mutation of
   ``Chart.yaml::name`` (no ``sed`` rewrite, no rename of the chart
   directory before packaging).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART_DIR = REPO_ROOT / "charts" / "audittrace"
PUBLISH_YML = REPO_ROOT / ".github" / "workflows" / "publish.yml"

_HELM_SET_ARGS = (
    "--set",
    "vault.enabled=false",
    "--set",
    "secrets.minio.secretKey=ci-test",
    "--set",
    "secrets.minio.kmsKey=ci-test",
    "--set",
    "secrets.chromadb.token=ci-test",
    "--set",
    "secrets.keycloak.adminPassword=ci-test",
    "--set",
    "secrets.postgres.appPassword=ci-test",
    "--set",
    "secrets.postgres.password=ci-test",
    "--set",
    "secrets.redis.password=ci-test",
    "--set",
    "secrets.summariser.password=ci-test",
)


def _helm_template(chart_path: Path) -> list[dict]:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm not installed")
    # S603: helm path is from shutil.which (PATH-resolved, trusted);
    # all other args are static literals. Safe by construction.
    result = subprocess.run(  # noqa: S603
        [
            helm,
            "template",
            "audittrace",
            str(chart_path),
            "--namespace",
            "audittrace",
            *_HELM_SET_ARGS,
        ],
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
def rendered_source() -> list[dict]:
    return _helm_template(CHART_DIR)


class TestSourceChartLabels:
    """The source chart renders the expected stable identity label.

    These pins catch accidental changes to ``Chart.yaml::name`` in
    the source tree, and any change to ``_helpers.tpl`` that would
    skew ``app.kubernetes.io/name``.
    """

    def test_deployment_selector_label(self, rendered_source: list[dict]) -> None:
        deploy = _find(rendered_source, "Deployment", "audittrace-memory-server")
        labels = deploy["spec"]["selector"]["matchLabels"]
        assert labels["app.kubernetes.io/name"] == "audittrace"

    def test_statefulset_selector_label(self, rendered_source: list[dict]) -> None:
        sts = _find(rendered_source, "StatefulSet", "audittrace-chromadb")
        labels = sts["spec"]["selector"]["matchLabels"]
        assert labels["app.kubernetes.io/name"] == "audittrace"

    def test_daemonset_selector_label(self, rendered_source: list[dict]) -> None:
        ds = _find(rendered_source, "DaemonSet", "audittrace-otel-collector")
        labels = ds["spec"]["selector"]["matchLabels"]
        assert labels["app.kubernetes.io/name"] == "audittrace"

    def test_pod_template_label_matches_selector(
        self, rendered_source: list[dict]
    ) -> None:
        # Selector + pod template labels MUST agree in K8s; otherwise
        # the Deployment immediately rejects pods it spawns.
        deploy = _find(rendered_source, "Deployment", "audittrace-memory-server")
        selector = deploy["spec"]["selector"]["matchLabels"]
        pod_labels = deploy["spec"]["template"]["metadata"]["labels"]
        for k, v in selector.items():
            assert pod_labels.get(k) == v, (
                f"Pod template label {k}={pod_labels.get(k)!r} != "
                f"selector {k}={v!r} — selector/template mismatch."
            )


class TestPublishWorkflowDoesNotMutateChartName:
    """The publish workflow MUST NOT rename Chart.yaml::name.

    Renaming the chart name at publish time (e.g. appending a
    ``-chart`` suffix) propagates into ``app.kubernetes.io/name``
    via ``.Chart.Name``, which K8s rejects on Deployment upgrade
    because ``spec.selector`` is immutable. Live-incident anchor:
    2026-05-16 v1.1.1 helm upgrade failed with
    ``cannot patch ... DaemonSet/Deployment/StatefulSet ...
    field is immutable: matchLabels``.

    The fix removed the rename trick from publish.yml; this test
    pins that decision.
    """

    def test_publish_yml_has_no_chart_name_sed_rewrite(self) -> None:
        text = PUBLISH_YML.read_text()
        # Anti-pattern: sed replacing `name: audittrace` in Chart.yaml.
        # Matches typical forms with or without whitespace tolerance.
        pattern = re.compile(
            r"sed\s+[^|\n]*?['\"]?s/[^'\"]*name:\s*audittrace[^'\"]*?/[^'\"]*?name:",
            re.IGNORECASE,
        )
        match = pattern.search(text)
        assert match is None, (
            "publish.yml contains a `sed` that rewrites Chart.yaml's "
            f"`name:` field: {match.group(0)!r}. This is the cc-repo "
            "rename trick. AuditTrace-AI's image is at "
            "lfds/audittrace-memory-server (not lfds/audittrace), so "
            "there is no OCI ref collision and no rename is needed. "
            "Re-introducing the rename will break helm upgrade for "
            "every existing operator (immutable selector violation)."
        )

    def test_publish_yml_has_no_chart_directory_rename(self) -> None:
        text = PUBLISH_YML.read_text()
        # Anti-pattern: `mv audittrace audittrace-chart` (or any
        # equivalent rename of the chart directory before packaging).
        pattern = re.compile(r"\bmv\s+audittrace\s+audittrace-\w+", re.IGNORECASE)
        match = pattern.search(text)
        assert match is None, (
            "publish.yml renames the chart directory: "
            f"{match.group(0)!r}. Same regression class as the "
            "Chart.yaml::name sed rewrite — see "
            "test_publish_yml_has_no_chart_name_sed_rewrite."
        )

    def test_publish_yml_publishes_chart_at_natural_oci_ref(self) -> None:
        text = PUBLISH_YML.read_text()
        assert "oci://registry-1.docker.io/lfds/audittrace-chart" not in text, (
            "publish.yml still references the legacy "
            "`oci://registry-1.docker.io/lfds/audittrace-chart` ref "
            "introduced by the rename trick. The chart now publishes "
            "at `oci://registry-1.docker.io/lfds/audittrace` (natural "
            "name from Chart.yaml). Update any remaining strings."
        )
