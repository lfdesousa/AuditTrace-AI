"""Regression: the llmStub templates must be nil-safe.

A ``helm upgrade --reuse-values`` from a pre-1.5.0 release leaves
``.Values.llmStub`` entirely absent (nil). The 1.5.0 guards did a bare
``.Values.llmStub.enabled``, which nil-pointered:

    nil pointer evaluating interface {}.enabled

The guards now use ``(.Values.llmStub | default dict).enabled`` (and the
same for externalLLM in the mutual-exclusion assert). We simulate the
absent key with ``--set-json llmStub=null`` and assert the chart still
renders, with the stub off. (chart 1.5.2 hardening — surfaced by the
2026-05-27 local 1.3.1 -> 1.5.1 upgrade dry-run.)
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART_DIR = Path(__file__).resolve().parent.parent / "charts" / "audittrace"
_SECRETS = [
    "--set",
    "secrets.summariser.password=dummy",
    "--set",
    "secrets.minio.secretKey=dummy",
    "--set",
    "secrets.minio.kmsKey=dummy",
]


def _render(*extra: str) -> subprocess.CompletedProcess:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm not installed")
    return subprocess.run(
        [
            helm,
            "template",
            "audittrace",
            str(CHART_DIR),
            "--namespace",
            "audittrace",
            *_SECRETS,
            *extra,
        ],
        capture_output=True,
        text=True,
    )


def _names(stdout: str) -> set[tuple[str, str]]:
    return {
        (d.get("kind"), d.get("metadata", {}).get("name"))
        for d in yaml.safe_load_all(stdout)
        if d
    }


def test_llmstub_absent_renders_without_nil_pointer() -> None:
    # Simulate helm upgrade --reuse-values from pre-1.5.0: llmStub key gone.
    r = _render("--set-json", "llmStub=null")
    assert r.returncode == 0, (
        "chart must render when .Values.llmStub is nil (helm upgrade "
        f"--reuse-values from a pre-1.5.0 release); stderr:\n{r.stderr}"
    )
    assert ("Deployment", "audittrace-llm-stub") not in _names(r.stdout), (
        "stub Deployment must be absent when llmStub is unset"
    )


def test_llmstub_enabled_still_renders_stub_resources() -> None:
    # The fix must not regress the enabled path.
    r = _render("--set", "llmStub.enabled=true", "--set", "externalLLM.enabled=false")
    assert r.returncode == 0, r.stderr
    names = _names(r.stdout)
    assert ("Deployment", "audittrace-llm-stub") in names
    services = {n for (k, n) in names if k == "Service"}
    assert {
        "audittrace-llm-chat",
        "audittrace-llm-embed",
        "audittrace-llm-summarizer",
    } <= services
