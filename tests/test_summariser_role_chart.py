"""Structural tests for the dedicated summariser-role Helm chart.

Guards the RLS architecture (ADR-026 §RLS posture, ADR-030): the
session summariser MUST run under its own minimum-privilege role
(`audittrace_summariser`) with `BYPASSRLS`. The user-facing
`audittrace_app` role must stay NOSUPERUSER + NOBYPASSRLS so RLS
continues to enforce per-user isolation on the chat path.

These tests lock the shape of the chart so a future edit can't
silently grant BYPASSRLS to the generic app role (which would blow
the isolation model apart) and can't accidentally remove the
minimum-privilege grants on the summariser role.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART_DIR = Path(__file__).resolve().parent.parent / "charts" / "audittrace"


def _render(overrides: list[str] | None = None) -> list[dict]:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm not installed")
    cmd = [helm, "template", "audittrace", str(CHART_DIR), "--namespace", "audittrace"]
    cmd.extend(overrides or [])
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def _find(resources: list[dict], kind: str, name: str) -> dict | None:
    for r in resources:
        if r.get("kind") == kind and r.get("metadata", {}).get("name") == name:
            return r
    return None


@pytest.fixture(scope="module")
def rendered() -> list[dict]:
    # Password must be provided for manageRole=true path; use a dummy.
    return _render(["--set", "secrets.summariser.password=dummy-pw-for-render"])


def test_summariser_role_job_is_rendered_and_is_a_helm_hook(
    rendered: list[dict],
) -> None:
    job = _find(rendered, "Job", "audittrace-ensure-summariser-role")
    assert job is not None, "the ensure-summariser-role Job must render"
    ann = job["metadata"].get("annotations", {})
    hook = ann.get("helm.sh/hook", "")
    # Must fire on both install and upgrade so existing clusters get
    # the role provisioned on first helm upgrade without losing data.
    assert "post-install" in hook and "post-upgrade" in hook
    # Hook-succeeded cleanup keeps Job rows from piling up and lets
    # subsequent upgrades re-create the immutable Job.
    assert "hook-succeeded" in ann.get("helm.sh/hook-delete-policy", "")


def test_summariser_role_job_targets_the_dedicated_role(rendered: list[dict]) -> None:
    """Job's command references the dedicated role via the sidecar ConfigMap,
    not the generic app role. Role-posture SQL lives in the ConfigMap."""
    job = _find(rendered, "Job", "audittrace-ensure-summariser-role")
    assert job is not None
    cm = _find(rendered, "ConfigMap", "audittrace-ensure-summariser-sql")
    assert cm is not None, "the Job must ship its SQL via a ConfigMap"
    sql = cm["data"]["ensure-role.sql"]
    # Dedicated role name, never the generic app role.
    assert "audittrace_summariser" in sql
    assert "audittrace_app" not in sql, (
        "the summariser Job must never touch audittrace_app — isolating "
        "elevated privilege to its own role is the architectural invariant"
    )
    # The exact posture that matches ADR-026 §RLS: LOGIN NOSUPERUSER BYPASSRLS.
    assert "LOGIN NOSUPERUSER BYPASSRLS" in sql


def test_summariser_role_has_minimum_grants(rendered: list[dict]) -> None:
    cm = _find(rendered, "ConfigMap", "audittrace-ensure-summariser-sql")
    assert cm is not None
    sql = cm["data"]["ensure-role.sql"]
    # Read cross-user on interactions ONLY (no write).
    assert "GRANT SELECT ON TABLE public.interactions TO audittrace_summariser" in sql
    # Summaries are written back to sessions — SELECT + INSERT + UPDATE, no DELETE.
    assert (
        "GRANT SELECT, INSERT, UPDATE ON TABLE public.sessions TO audittrace_summariser"
        in sql
    )
    # Explicit negative: no GRANT statement ever targets tool_calls.
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            # comments may mention tool_calls to document the exclusion
            continue
        assert "tool_calls" not in stripped, (
            "summariser must not be granted any access to tool_calls"
        )


def test_summariser_role_not_rendered_when_disabled() -> None:
    """manageRole=false must produce no Job + no Secret + no ConfigMap
    so external role-management workflows (Terraform, dbmate) can own it."""
    resources = _render(["--set", "memoryServer.summariser.manageRole=false"])
    assert _find(resources, "Job", "audittrace-ensure-summariser-role") is None
    assert _find(resources, "Secret", "audittrace-summariser-db") is None
    assert _find(resources, "ConfigMap", "audittrace-ensure-summariser-sql") is None


def test_summariser_url_uses_dedicated_role_when_manage_role_true(
    rendered: list[dict],
) -> None:
    """memory-server Deployment must point at audittrace_summariser
    via AUDITTRACE_SUMMARIZER_POSTGRES_URL — not the generic owner role."""
    dep = _find(rendered, "Deployment", "audittrace-memory-server")
    assert dep is not None
    env = dep["spec"]["template"]["spec"]["containers"][0].get("env", [])
    url_entry = next(
        (e for e in env if e.get("name") == "AUDITTRACE_SUMMARIZER_POSTGRES_URL"),
        None,
    )
    assert url_entry is not None
    url = url_entry.get("value", "")
    assert "audittrace_summariser:" in url, (
        "summariser URL must use the dedicated role, never a generic one"
    )
    # Main app URL stays on audittrace_app.
    app_url_entry = next(
        (e for e in env if e.get("name") == "AUDITTRACE_POSTGRES_URL"), None
    )
    assert app_url_entry is not None
    assert "audittrace_app:" in app_url_entry.get("value", "")


def test_summariser_secret_requires_password() -> None:
    """Helm template must fail hard when the password is empty — no
    guessable default for a BYPASSRLS role."""
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm not installed")
    proc = subprocess.run(
        [
            helm,
            "template",
            "audittrace",
            str(CHART_DIR),
            "--namespace",
            "audittrace",
            "--set",
            "secrets.summariser.password=",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0, "empty password must error, not silently render"
    assert "summariser.password is required" in proc.stderr
