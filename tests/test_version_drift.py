"""Drift guard for the package version string.

Background. v1.0.11 was tagged on 2026-05-06 without bumping
``pyproject.toml::version`` (still read 1.0.10), the
``HealthResponse.version`` model default in ``src/audittrace/models.py``
(1.0.10), or the dev fallback constant in
``src/audittrace/server.py::_resolve_version`` (1.0.10). The
OpenAPI spec consequently self-identified as 1.0.10 while the
running code was v1.0.11. This test pins the three sites so future
releases can't repeat the drift.

A companion CI job in ``.github/workflows/ci.yml`` asserts that on
a ``v*`` tag push the tag matches pyproject — closing the
"tagged without bumping" loophole at release time.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
MODELS_PY = REPO_ROOT / "src" / "audittrace" / "models.py"
SERVER_PY = REPO_ROOT / "src" / "audittrace" / "server.py"


def _pyproject_version() -> str:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data["project"]["version"]


def _models_health_default() -> str:
    """Find the HealthResponse.version model-field default literal."""
    text = MODELS_PY.read_text(encoding="utf-8")
    m = re.search(r'version:\s*str\s*=\s*"([\d.]+)"', text)
    assert m, "Could not locate HealthResponse.version default in models.py"
    return m.group(1)


def _server_fallback() -> str:
    """Find the dev-fallback constant in server._resolve_version."""
    text = SERVER_PY.read_text(encoding="utf-8")
    m = re.search(
        r"except \(PackageNotFoundError, ImportError\)[^:]*:[^\n]*\n\s*return\s+\"([\d.]+)\"",
        text,
    )
    assert m, "Could not locate _resolve_version fallback constant in server.py"
    return m.group(1)


def test_pyproject_matches_models_health_default() -> None:
    """HealthResponse.version default must equal pyproject version."""
    pyproject = _pyproject_version()
    models = _models_health_default()
    assert models == pyproject, (
        f"models.py HealthResponse.version default ({models!r}) "
        f"!= pyproject.toml version ({pyproject!r}). "
        "Bump both together (v1.0.10→v1.0.11 drift class)."
    )


def test_pyproject_matches_server_fallback() -> None:
    """server._resolve_version fallback must equal pyproject version.

    The fallback only fires in dev trees without ``pip install -e``;
    even so, an out-of-date constant lies to a developer running from
    source and lands in the OpenAPI spec as ``info.version`` when the
    package metadata is unavailable.
    """
    pyproject = _pyproject_version()
    fallback = _server_fallback()
    assert fallback == pyproject, (
        f"server.py _resolve_version fallback ({fallback!r}) "
        f"!= pyproject.toml version ({pyproject!r}). "
        "Bump both together (v1.0.10→v1.0.11 drift class)."
    )
