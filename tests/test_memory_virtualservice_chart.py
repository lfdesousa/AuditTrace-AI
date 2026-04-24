"""Structural test for the memory-server Istio VirtualService.

Guards the ``/memory/index`` route timeout. That endpoint rebuilds
ChromaDB collections from every document in MinIO and routinely runs
longer than Istio's 15 s gateway default; without an explicit per-route
timeout the gateway cuts the request off mid-index on any populated
bucket, and the memory server never learns the client went away. The
fix is a dedicated route with ``timeout: 300s`` before the catch-all.
This test locks the shape so a future chart edit can't silently remove
the extended timeout.
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


class TestMemoryVirtualService:
    def test_memory_index_has_dedicated_long_timeout_route(self) -> None:
        """The VirtualService must define a /memory/index route with a
        timeout comfortably larger than Istio's 15 s default so the
        ingest endpoint does not get killed mid-index on a populated
        MinIO bucket."""
        vs = _find(_render_chart(), "VirtualService", "audittrace-memory")
        routes = vs["spec"]["http"]

        index_routes = [
            r
            for r in routes
            if any(
                m.get("uri", {}).get("prefix") == "/memory/index"
                for m in r.get("match", []) or []
            )
        ]
        assert len(index_routes) == 1, (
            "expected exactly one route matching /memory/index — found "
            f"{len(index_routes)}. Full http block: {routes}"
        )
        index_route = index_routes[0]
        assert "timeout" in index_route, (
            "/memory/index route must set an explicit timeout to override "
            "Istio's 15s gateway default"
        )
        # Parse the duration string — Istio accepts Go-duration syntax.
        timeout = index_route["timeout"]
        assert timeout.endswith("s") or timeout.endswith("m"), (
            f"timeout {timeout!r} must be a Go-duration string (e.g. 300s)"
        )
        seconds = int(timeout[:-1]) * (60 if timeout.endswith("m") else 1)
        assert seconds >= 60, (
            f"/memory/index timeout is {timeout} — must be at least 60s to "
            "cover a realistic ingest batch"
        )

    def test_memory_index_route_precedes_catchall(self) -> None:
        """Istio evaluates routes in declaration order; the /memory/index
        match must come before the catch-all or traffic falls through to
        the default timeout."""
        vs = _find(_render_chart(), "VirtualService", "audittrace-memory")
        routes = vs["spec"]["http"]

        index_idx = next(
            (
                i
                for i, r in enumerate(routes)
                if any(
                    m.get("uri", {}).get("prefix") == "/memory/index"
                    for m in r.get("match", []) or []
                )
            ),
            None,
        )
        catchall_idx = next(
            (i for i, r in enumerate(routes) if not r.get("match")),
            None,
        )
        assert index_idx is not None, "no /memory/index route found"
        assert catchall_idx is not None, "no catch-all route found"
        assert index_idx < catchall_idx, (
            f"catch-all route at index {catchall_idx} appears before the "
            f"/memory/index match at index {index_idx} — reorder so the "
            "specific match fires first"
        )
