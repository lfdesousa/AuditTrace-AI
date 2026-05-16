"""Drift-guard for the ``scripts/audittrace-login`` default
``KEYCLOAK_BASE``.

The historical default was ``https://localhost`` (Traefik-fronted,
docker-compose era). Post-k3s-migration, nothing listens on
host:443 — the Istio Gateway exposes Keycloak on NodePort 30952.
Bare ``./scripts/audittrace-login`` against the stale default
gave a 404 from a phantom listener and an unhelpful error
message; surfaced 2026-05-16 mid-session when the refresh token
expired and a full re-login was required.

This test pins the post-migration default in the script AND in
the --help text. Drift here breaks first-time developer login
in a way that is operationally tedious to diagnose (we just
diagnosed it).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "audittrace-login"

EXPECTED_DEFAULT = "https://audittrace.local:30952"


class TestAudittraceLoginDefaults:
    def test_script_exists(self) -> None:
        assert SCRIPT_PATH.is_file()

    def test_keycloak_base_default_uses_istio_gateway_nodeport(self) -> None:
        """The shell default for KEYCLOAK_BASE MUST point at the
        Istio Gateway NodePort, not the dead localhost:443."""
        text = SCRIPT_PATH.read_text()
        # Look for the parameter-expansion default literal.
        m = re.search(
            r'KEYCLOAK_BASE="\$\{KEYCLOAK_BASE:-(?P<default>[^}]+)\}"',
            text,
        )
        assert m is not None, (
            "could not find KEYCLOAK_BASE default-assignment line — "
            "the script's config block was restructured. Update this "
            "test to match the new shape."
        )
        actual = m.group("default")
        assert actual == EXPECTED_DEFAULT, (
            f"KEYCLOAK_BASE default = {actual!r}; expected "
            f"{EXPECTED_DEFAULT!r}. Stale default re-introduces the "
            "first-run-broken friction from the 2026-05-16 incident. "
            "Override via env for non-local clusters; the script's "
            "DEFAULT is for the dev cluster's Istio Gateway NodePort."
        )

    def test_help_text_default_matches_script_default(self) -> None:
        """The --help text MUST advertise the same default as the
        actual code path. Drift between code and docs is the
        original sin that landed us in the broken-out-of-the-box
        state."""
        text = SCRIPT_PATH.read_text()
        # Look for the --help block's KEYCLOAK_BASE line.
        m = re.search(
            r"KEYCLOAK_BASE\s+\(default\s+(?P<default>https://[^\s)]+)",
            text,
        )
        assert m is not None, (
            "could not find the --help text default line for "
            "KEYCLOAK_BASE — was the help block restructured?"
        )
        help_default = m.group("default")
        assert help_default == EXPECTED_DEFAULT, (
            f"--help text default = {help_default!r}; expected "
            f"{EXPECTED_DEFAULT!r}. Code-vs-docs drift will mislead "
            "the next developer."
        )

    def test_localhost_443_default_does_not_creep_back(self) -> None:
        """No code path in the script should fall back to
        https://localhost without a port — that's the historical
        broken default."""
        text = SCRIPT_PATH.read_text()
        # Allow `localhost` in comments and inside the `case` arm
        # that toggles --insecure (which keeps both hostnames for
        # operators who deliberately override). Forbid only as a
        # bare default assignment.
        forbidden = re.search(
            r'KEYCLOAK_BASE="\$\{KEYCLOAK_BASE:-https://localhost\}"',
            text,
        )
        assert forbidden is None, (
            "stale `KEYCLOAK_BASE=...https://localhost` default has "
            "regressed — re-introduces the 2026-05-16 first-run-"
            "broken friction"
        )
