"""Regression: the memory-server's accepted-issuer set must ALWAYS include
the issuer derived from keycloak.hostnameUrl.

Tokens carry iss=<hostnameUrl>/realms/audittrace. Before this guard,
keycloak.externalIssuers had to be hand-synced with keycloak.hostnameUrl;
overriding hostnameUrl alone (e.g. to a :9443 operator-tunnel port) left
the token's iss out of the accept-list → 401. The chart now auto-derives
and merges the hostnameUrl issuer (audittrace.keycloakIssuerExtras), so
the two can never drift. 2026-05-28 cloud Tier-0 login burn.
"""

from __future__ import annotations

import json
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


def _issuer_extras(*extra: str) -> list[str]:
    """Render the memory-server Deployment and return the parsed
    AUDITTRACE_KEYCLOAK_ISSUER_EXTRAS env value (a JSON list)."""
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
            *_SECRETS,
            *extra,
            "--show-only",
            "templates/memory-server/deployment.yaml",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    doc = yaml.safe_load(result.stdout)
    env = doc["spec"]["template"]["spec"]["containers"][0]["env"]
    raw = next(
        e["value"] for e in env if e["name"] == "AUDITTRACE_KEYCLOAK_ISSUER_EXTRAS"
    )
    return json.loads(raw)


def test_hostname_issuer_auto_included_with_operator_port() -> None:
    # Operator overrides hostnameUrl to a :9443 tunnel port; the derived
    # :9443 issuer MUST appear in the accept-list alongside the explicit one.
    extras = _issuer_extras(
        "--set-string",
        "keycloak.hostnameUrl=https://audittrace-loadtest.allaboutdata.eu:9443",
        "--set-string",
        "keycloak.externalIssuers[0]=https://audittrace-loadtest.allaboutdata.eu/realms/audittrace",
    )
    assert (
        "https://audittrace-loadtest.allaboutdata.eu:9443/realms/audittrace" in extras
    ), f"hostnameUrl-derived :9443 issuer must be auto-merged; got {extras}"
    # the explicitly-configured issuer is preserved too
    assert "https://audittrace-loadtest.allaboutdata.eu/realms/audittrace" in extras


def test_hostname_issuer_auto_included_default() -> None:
    # With no externalIssuers override, the accept-list still carries the
    # issuer derived from the default hostnameUrl.
    extras = _issuer_extras()
    assert any(e.endswith("/realms/audittrace") for e in extras), extras


def test_issuer_list_deduped() -> None:
    # If externalIssuers already contains the hostnameUrl issuer, it must not
    # be duplicated by the auto-merge.
    extras = _issuer_extras(
        "--set-string",
        "keycloak.hostnameUrl=https://audittrace.local",
        "--set-string",
        "keycloak.externalIssuers[0]=https://audittrace.local/realms/audittrace",
    )
    assert extras.count("https://audittrace.local/realms/audittrace") == 1, extras
