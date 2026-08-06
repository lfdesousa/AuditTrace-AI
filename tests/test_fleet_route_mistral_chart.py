"""FLEET-ROUTE-ENABLE (2026-08-06, #229 follow-up) — chart-rendering tests.

Falsifiable per the SPEC (`2026-08-06-SPEC-fleet-route-enable-mistral.md`):

* G2 — with ``externalLLM.mistral.enabled=true`` the chart renders the
  ``-llm-mistral`` ExternalName Service (externalName
  ``host.audittrace.local``, port 11438), the matching no-mTLS
  DestinationRule entry, and ``AUDITTRACE_MODEL_ROUTES`` containing the
  mistral upstream routed through the in-cluster Service (mesh-addressed,
  not a ``host.docker.internal`` bypass).
* G3 — with the mistral value UNSET (the chart default), ``AUDITTRACE_
  MODEL_ROUTES`` renders ``{}`` and no ``-llm-mistral`` Service or
  DestinationRule appears — proving additive/default-safe (setting the
  mistral route must not change routing for any non-``mistral*`` model,
  and Qwen never gets a redundant ``modelRoutes`` entry — it stays the
  default fall-through upstream).

Mirrors the ``_render``/``_docs``/``_find_workload`` helper shape already
established in ``tests/test_chart_rendering.py`` and
``tests/test_summariser_role_chart.py`` — no new rendering harness.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART_DIR = Path(__file__).resolve().parent.parent / "charts" / "audittrace"

# Mirrors tests/test_chart_rendering.py::_LINT_SECRETS — the throwaway
# secrets + FQDN-only host fields the chart requires to render at all
# (ADR-045 FQDN-only `required` guards fire regardless of productionMode).
_BASE_ARGS = [
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
    "--set",
    "externalLLM.host=host.audittrace.local",
    "--set",
    "observability.external.langfuseHost=langfuse.test.invalid",
    "--set",
    "observability.external.tempoHost=tempo.test.invalid",
    "--set",
    "observability.external.lokiHost=loki.test.invalid",
]

_MISTRAL_SERVICE = "audittrace-llm-mistral"
_MISTRAL_DR = "audittrace-llm-mistral-no-mtls"


def _helm_available() -> bool:
    return shutil.which("helm") is not None


pytestmark = pytest.mark.skipif(
    not _helm_available(),
    reason="helm CLI not on PATH — chart-rendering tests need it",
)


def _render(extra_args: list[str] | None = None) -> list[dict]:
    """Run `helm template` and return the parsed manifest documents.

    Raises ``AssertionError`` with full helm output if rendering fails so
    a chart error surfaces directly in pytest output.
    """
    cmd = [
        "helm",
        "template",
        "audittrace",
        str(CHART_DIR),
        "-n",
        "audittrace",
        *_BASE_ARGS,
        *(extra_args or []),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(
            f"helm template failed (rc={result.returncode}):\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )
    return [d for d in yaml.safe_load_all(result.stdout) if isinstance(d, dict)]


def _find(docs: list[dict], kind: str, name: str) -> dict | None:
    for doc in docs:
        if doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name:
            return doc
    return None


def _model_routes_env(docs: list[dict]) -> str:
    deployment = _find(docs, "Deployment", "audittrace-memory-server")
    assert deployment is not None, "audittrace-memory-server Deployment not rendered"
    containers = deployment["spec"]["template"]["spec"]["containers"]
    container = next(c for c in containers if c["name"] == "memory-server")
    env_pairs = {e["name"]: e.get("value") for e in container.get("env", [])}
    assert "AUDITTRACE_MODEL_ROUTES" in env_pairs, (
        "memory-server deployment lost the AUDITTRACE_MODEL_ROUTES env var"
    )
    return env_pairs["AUDITTRACE_MODEL_ROUTES"]


class TestMistralRouteDefaultOff:
    """G3 — default-off preserved. The mistral value being unset is the
    chart's shipped default (``externalLLM.mistral.enabled: false``); no
    extra ``--set`` is needed to exercise this path."""

    @pytest.fixture(scope="class")
    def rendered(self) -> list[dict]:
        return _render()

    def test_model_routes_renders_empty_map(self, rendered: list[dict]) -> None:
        assert _model_routes_env(rendered) == "{}", (
            "AUDITTRACE_MODEL_ROUTES must stay '{}' when "
            "externalLLM.mistral.enabled is unset — any other value "
            "changes routing for every model, not just mistral*."
        )

    def test_no_mistral_service_rendered(self, rendered: list[dict]) -> None:
        assert _find(rendered, "Service", _MISTRAL_SERVICE) is None, (
            f"{_MISTRAL_SERVICE} Service must not render with the "
            "mistral value unset (additive/default-safe)."
        )

    def test_no_mistral_destinationrule_rendered(self, rendered: list[dict]) -> None:
        assert _find(rendered, "DestinationRule", _MISTRAL_DR) is None, (
            f"{_MISTRAL_DR} DestinationRule must not render with the "
            "mistral value unset (additive/default-safe)."
        )

    def test_chat_embed_summarizer_services_unaffected(
        self, rendered: list[dict]
    ) -> None:
        """Setting nothing must not perturb the three existing model
        Services either — the no-drift invariant applies both ways."""
        for name in (
            "audittrace-llm-chat",
            "audittrace-llm-embed",
            "audittrace-llm-summarizer",
        ):
            assert _find(rendered, "Service", name) is not None, (
                f"{name} Service missing from the default render"
            )


class TestMistralRouteEnabled:
    """G2 — chart renders the service + route when the mistral value is
    set (``--set externalLLM.mistral.enabled=true``)."""

    @pytest.fixture(scope="class")
    def rendered(self) -> list[dict]:
        return _render(["--set", "externalLLM.mistral.enabled=true"])

    def test_mistral_service_renders_externalname_and_port(
        self, rendered: list[dict]
    ) -> None:
        svc = _find(rendered, "Service", _MISTRAL_SERVICE)
        assert svc is not None, f"{_MISTRAL_SERVICE} Service not rendered"
        assert svc["spec"]["type"] == "ExternalName"
        assert svc["spec"]["externalName"] == "host.audittrace.local"
        ports = svc["spec"]["ports"]
        assert len(ports) == 1
        assert ports[0]["port"] == 11438
        assert ports[0]["targetPort"] == 11438

    def test_mistral_service_labels_match_sibling_pattern(
        self, rendered: list[dict]
    ) -> None:
        """No-drift check: the mistral Service must carry the same label
        shape as the existing three (feedback_no_more_drifts)."""
        mistral = _find(rendered, "Service", _MISTRAL_SERVICE)
        summarizer = _find(rendered, "Service", "audittrace-llm-summarizer")
        assert mistral is not None and summarizer is not None
        mistral_labels = dict(mistral["metadata"]["labels"])
        summarizer_labels = dict(summarizer["metadata"]["labels"])
        assert mistral_labels.pop("app.kubernetes.io/component") == "llm-mistral"
        assert summarizer_labels.pop("app.kubernetes.io/component") == "llm-summarizer"
        assert mistral_labels == summarizer_labels, (
            "mistral Service labels drifted from the summarizer pattern"
        )

    def test_mistral_destinationrule_no_mtls(self, rendered: list[dict]) -> None:
        dr = _find(rendered, "DestinationRule", _MISTRAL_DR)
        assert dr is not None, f"{_MISTRAL_DR} DestinationRule not rendered"
        assert (
            dr["spec"]["host"] == "audittrace-llm-mistral.audittrace.svc.cluster.local"
        )
        assert dr["spec"]["trafficPolicy"]["tls"]["mode"] == "DISABLE"

    def test_model_routes_contains_mesh_addressed_mistral_upstream(
        self, rendered: list[dict]
    ) -> None:
        routes = _model_routes_env(rendered)
        assert routes == '{"mistral":"http://audittrace-llm-mistral:11438/v1"}', (
            f"unexpected AUDITTRACE_MODEL_ROUTES rendering: {routes!r}"
        )

    def test_no_redundant_qwen_route(self, rendered: list[dict]) -> None:
        """Qwen stays the default fall-through — it must never get an
        explicit modelRoutes entry, redundant or otherwise."""
        assert '"qwen"' not in _model_routes_env(rendered)

    def test_chat_embed_summarizer_still_render(self, rendered: list[dict]) -> None:
        """Enabling mistral must not remove or alter the sibling
        Services — additive only."""
        for name in (
            "audittrace-llm-chat",
            "audittrace-llm-embed",
            "audittrace-llm-summarizer",
        ):
            assert _find(rendered, "Service", name) is not None, (
                f"{name} Service missing after enabling externalLLM.mistral"
            )


class TestMistralRouteOperatorOverrideWins:
    """An operator-set ``memoryServer.modelRoutes.mistral`` value must
    take precedence over the chart-computed mesh default — the merge
    helper's documented precedence, falsifiable independently of G2/G3."""

    def test_explicit_route_overrides_computed_default(self) -> None:
        rendered = _render(
            [
                "--set",
                "externalLLM.mistral.enabled=true",
                "--set",
                "memoryServer.modelRoutes.mistral=http://custom-override:9999/v1",
            ]
        )
        routes = _model_routes_env(rendered)
        assert routes == '{"mistral":"http://custom-override:9999/v1"}', (
            f"operator override did not win: {routes!r}"
        )
