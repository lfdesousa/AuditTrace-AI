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
        [
            helm,
            "template",
            "audittrace",
            str(CHART_DIR),
            "--namespace",
            "audittrace",
            "--set",
            "secrets.summariser.password=dummy-pw-for-render",
            "--set",
            "secrets.minio.secretKey=dummy-minio-secret-for-render",
            "--set",
            "secrets.minio.kmsKey=dummy-minio-kms-for-render",
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


def _render_collector_config(**extra_sets: str) -> dict:
    """Render the chart with extra --set overrides; return the parsed
    otel-collector-config.yaml as a dict."""
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm not installed")
    args = [
        helm,
        "template",
        "audittrace",
        str(CHART_DIR),
        "--namespace",
        "audittrace",
        "--set",
        "secrets.summariser.password=dummy-pw-for-render",
        "--set",
        "secrets.minio.secretKey=dummy-minio-secret-for-render",
        "--set",
        "secrets.minio.kmsKey=dummy-minio-kms-for-render",
        # Once we set ANY FQDN key, conftest's auto-injection backs off
        # (it sees us as "FQDN-aware"), so we must satisfy ALL the chart's
        # `required` FQDN guards ourselves (langfuse/loki/externalLLM).
        "--set",
        "externalLLM.host=llm.test.invalid",
        "--set",
        "observability.external.langfuseHost=langfuse.test.invalid",
        "--set",
        "observability.external.lokiHost=loki.test.invalid",
    ]
    for k, v in extra_sets.items():
        args += ["--set", f"{k.replace('__', '.')}={v}"]
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    resources = [d for d in yaml.safe_load_all(result.stdout) if d]
    cm = _find(resources, "ConfigMap", "audittrace-otel-collector-config")
    return yaml.safe_load(cm["data"]["otel-collector-config.yaml"])


def test_metrics_no_remote_write_when_prometheus_host_empty() -> None:
    """Laptop dev path: empty prometheusHost keeps the scrape-only model
    (the :8889 exporter), with NO remote-write exporter — so the local obs
    Prometheus that scrapes the NodePort isn't double-counted. Regression
    guard for #217."""
    config = _render_collector_config(
        **{
            "observability__otelCollector__enabled": "true",
            "observability__external__tempoHost": "tempo.local",
            "observability__external__prometheusHost": "",
        }
    )
    assert "prometheusremotewrite/obs" not in config["exporters"], (
        "no remote-write exporter when prometheusHost is empty (laptop scrape model)"
    )
    metrics_exporters = config["service"]["pipelines"]["metrics"]["exporters"]
    assert metrics_exporters == ["prometheus"], (
        f"metrics pipeline must be scrape-only on the laptop, got {metrics_exporters}"
    )


def test_metrics_remote_write_when_prometheus_host_set() -> None:
    """Cloud path: a remote Prometheus that cannot scrape into the cluster
    receives application metrics via remote-write (mirrors the Tempo trace
    push). Endpoint must honour scheme + port and hit /api/v1/write. The fix
    for the 2026-05-27 'HTTP-server panels show No data in cloud' gap (#217):
    prometheusHost was wired into values but never consumed by the pipeline."""
    config = _render_collector_config(
        **{
            "observability__otelCollector__enabled": "true",
            "observability__external__tempoHost": "obs.example.eu",
            "observability__external__prometheusHost": "obs.example.eu",
            "observability__external__scheme": "https",
        }
    )
    rw = config["exporters"].get("prometheusremotewrite/obs")
    assert rw is not None, "remote-write exporter must exist when prometheusHost is set"
    assert rw["endpoint"] == "https://obs.example.eu:19090/api/v1/write", (
        f"remote-write endpoint must use scheme+port+/api/v1/write, got {rw['endpoint']}"
    )
    metrics_exporters = config["service"]["pipelines"]["metrics"]["exporters"]
    assert metrics_exporters == ["prometheus", "prometheusremotewrite/obs"], (
        "metrics pipeline must keep the local scrape exporter AND add remote-write, "
        f"got {metrics_exporters}"
    )
    # Traces unchanged — still push to Tempo over the same scheme.
    assert config["service"]["pipelines"]["traces"]["exporters"] == ["otlphttp/tempo"]
