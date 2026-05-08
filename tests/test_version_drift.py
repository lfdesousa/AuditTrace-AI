"""Drift guard for the package version string.

Background. Twelve releases (v1.0.1 through v1.0.12) silently misreported
the running version because the source-of-truth was duplicated across
four+ files and at least three drift incidents occurred:

* v1.0.10→v1.0.11 (2026-05-06): pyproject + models.py + server.py
  fallback drifted; OpenAPI spec self-identified as v1.0.10 while the
  running code was v1.0.11.
* OTEL service.version=1.0.0 frozen since v1.0.0; twelve releases
  shipped to Tempo + Langfuse self-identifying as v1.0.0.
* v1.0.13 image self-reported as v1.0.11 (caught during runbook
  validation) — Dockerfile never installed the package, stale
  ``src/audittrace_ai.egg-info`` from a developer-side ``pip install
  -e .`` got baked in, and ``importlib.metadata.version()`` happily
  returned the frozen 1.0.11 metadata.

ADR-055 (2026-05-09) consolidated the duplication. Two chart-side
sources of truth remain — ``pyproject.toml::version`` and
``charts/audittrace/Chart.yaml::appVersion`` — and they MUST equal
each other. Everything else (HealthResponse default, server fallback,
OTEL_RESOURCE_ATTRIBUTES, k8s ``app.kubernetes.io/version`` label) is
either dynamic (resolved via ``importlib.metadata``) or chart-templated
from ``.Chart.AppVersion``.

A companion CI job in ``.github/workflows/ci.yml`` asserts that on a
``v*`` tag push the tag matches pyproject — closing the
"tagged without bumping" loophole at release time.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
CHART_YAML = REPO_ROOT / "charts" / "audittrace" / "Chart.yaml"


def _pyproject_version() -> str:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data["project"]["version"]


def _chart_app_version() -> str:
    data = yaml.safe_load(CHART_YAML.read_text(encoding="utf-8"))
    return str(data["appVersion"])


def test_chart_appversion_matches_pyproject_version() -> None:
    """``Chart.yaml::appVersion`` MUST equal ``pyproject.toml::version``.

    These are the two single-source-of-truth pin sites that survived
    ADR-055's consolidation. Everything else (HealthResponse default,
    server fallback, OTEL_RESOURCE_ATTRIBUTES, app.kubernetes.io/version
    label) reads from one of these — directly via
    ``importlib.metadata.version()`` or via Helm's ``.Chart.AppVersion``
    interpolation.

    Bumping pyproject without bumping Chart.yaml (or vice versa)
    produces this hard CI failure before tag-push, by design.
    Use ``make release VERSION=X.Y.Z`` to bump both atomically."""
    pyproject = _pyproject_version()
    chart = _chart_app_version()
    assert chart == pyproject, (
        f"charts/audittrace/Chart.yaml::appVersion ({chart!r}) "
        f"!= pyproject.toml::version ({pyproject!r}). "
        "Bump both together — `make release VERSION=X.Y.Z` does this "
        "atomically (ADR-055)."
    )
