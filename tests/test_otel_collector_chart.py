"""Structural tests for the OTel Collector Helm chart template.

Guards against silent regressions in the collector ports exposure. The
collector binary (otelcol-contrib 0.102) exposes its own self-telemetry
on :8888 by default; if that port is not propagated through the
DaemonSet and Service, the `otelcol_*` metrics cannot be scraped from
outside the cluster and the "OTel Collector Queue Saturation" panel
goes dark.
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


def test_collector_daemonset_exposes_self_metrics_port(rendered: list[dict]) -> None:
    ds = _find(rendered, "DaemonSet", "audittrace-otel-collector")
    ports = ds["spec"]["template"]["spec"]["containers"][0]["ports"]
    port_names = {p["name"]: p["containerPort"] for p in ports}
    assert port_names.get("self-metrics") == 8888, (
        "collector DaemonSet must expose containerPort 8888 as 'self-metrics' "
        "so Prometheus can scrape otelcol_* metrics (Queue Saturation panel)"
    )
    assert port_names.get("otlp-http") == 4318
    assert port_names.get("prometheus") == 8889


def test_collector_service_exposes_self_metrics_nodeport(rendered: list[dict]) -> None:
    svc = _find(rendered, "Service", "audittrace-otel-collector")
    ports = {p["name"]: p for p in svc["spec"]["ports"]}
    assert "self-metrics" in ports, "Service must expose 'self-metrics' port"
    assert ports["self-metrics"]["port"] == 8888
    assert ports["self-metrics"]["nodePort"] == 30888, (
        "NodePort 30888 is hard-coded into prometheus.yml scrape job "
        "'k3s-otel-collector' (obs-stack repo); both must stay in sync"
    )
    # Existing ports preserved.
    assert ports["prometheus"]["nodePort"] == 30889
    assert ports["otlp-http"]["port"] == 4318


def test_collector_config_exports_to_tempo(rendered: list[dict]) -> None:
    cm = _find(rendered, "ConfigMap", "audittrace-otel-collector-config")
    config = yaml.safe_load(cm["data"]["otel-collector-config.yaml"])
    pipelines = config["service"]["pipelines"]
    assert "otlphttp/tempo" in pipelines["traces"]["exporters"], (
        "trace pipeline must still export to Tempo"
    )
    assert "prometheus" in pipelines["metrics"]["exporters"]
