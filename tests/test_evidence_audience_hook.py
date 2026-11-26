"""CONSOLE-AUDIT-EVIDENCE WU-C (ADDENDUM-A6 section 3 items 2 and 3, G13''(a)):
the provisioning hook and the dev provisioner create, converge and authorize the
evidence token-exchange audience client on an EXISTING realm, and the BFF
Deployment is told its name.

Sibling of ``test_chat_audience_hook.py``: the client block and Step 4c are run
VERBATIM from both sites (the dev provisioner script and the chart ConfigMap)
under the same small fake ``kcadm``; the pinned JSON is compared to the realm
blocks and to ``bff.console_evidence_scopes``.

Neuters (each RED, then GREEN on restore, recorded in the build record): drop
Step 4c; drop the strip; drop the bind; drop a verify item; add
``audittrace:query`` to the pinned JSON in one site; drop the Deployment env var;
drop the chart value.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bff.config import Settings
from bff.console_evidence_scopes import EVIDENCE_SCOPES
from tests.test_chart_drift_guards_acl_write import (
    CONFIGMAP,
    SCRIPT,
    _client,
)
from tests.test_chart_drift_guards_session_e2e import _both_enabled
from tests.test_chat_audience_hook import Run
from tests.test_console_k3s_guards import _container_env, _find, _render

EVIDENCE = "audittrace-librechat-evidence"
EVIDENCE_SORTED = sorted(EVIDENCE_SCOPES)


def _pinned(path: Path) -> tuple[str, dict[str, Any]]:
    m = re.search(r"EVID_AUD_CLIENT_JSON='(\{.*?\})'\n", path.read_text())
    assert m is not None, f"{path.name}: pinned EVID_AUD_CLIENT_JSON not found"
    return m.group(1), json.loads(m.group(1))


def _block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index("EVID_AUD_CLIENT_ID=")
    marker = 'evid_aud_verify "${EVID_AUD_UUID}"\n'
    return text[start : text.rindex(marker) + len(marker)]


def _step4c(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index("TOKEN_EXCHANGE_EVIDENCE_TARGET_CLIENT=")
    marker = 'authorized to exchange for ${TOKEN_EXCHANGE_EVIDENCE_TARGET_CLIENT}"\n'
    return text[start : text.index(marker, start) + len(marker)]


def _state(**over: Any) -> dict[str, Any]:
    _raw, pinned = _pinned(SCRIPT)
    body = {
        k: v
        for k, v in pinned.items()
        if k not in ("defaultClientScopes", "optionalClientScopes", "protocolMappers")
    }
    client: dict[str, Any] = {
        "body": body,
        "default": [{"id": "s-" + n, "name": n} for n in EVIDENCE_SORTED],
        "optional": [],
        "mappers": ["aud-audittrace-server"],
    }
    client.update(over)
    return client


class EvidenceRun(Run):
    """The chat suite's rig, pointed at the evidence block and its scopes."""

    def go(  # type: ignore[override]
        self, *, client: dict[str, Any] | None, block: str | None = None, **extra: Any
    ) -> subprocess.CompletedProcess[str]:
        extra.setdefault(
            "realm_scopes", [{"id": "s-" + s, "name": s} for s in EVIDENCE_SORTED]
        )
        return super().go(
            client=client,
            block=block if block is not None else _block(self.site),
            **extra,
        )


@pytest.fixture(params=[SCRIPT, CONFIGMAP], ids=["script", "configmap"])
def run(request: pytest.FixtureRequest, tmp_path: Path) -> EvidenceRun:
    return EvidenceRun(tmp_path, request.param)


class TestTheClientIsEnsuredOnAnExistingRealm:
    def test_an_absent_client_is_created_stripped_bound_and_verified(
        self, run: EvidenceRun
    ) -> None:
        proc = run.go(client=None)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "BLOCK-COMPLETED" in proc.stdout
        assert json.loads(run.created.read_text())["clientId"] == EVIDENCE
        final = run.final()
        assert sorted(s["name"] for s in final["default"]) == EVIDENCE_SORTED
        assert final["optional"] == []
        assert "verified equal to the pinned block" in proc.stdout

    def test_a_correct_client_is_verified_and_never_updated(
        self, run: EvidenceRun
    ) -> None:
        proc = run.go(client=_state())
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert run.calls("create") == []
        assert run.calls("update") == []

    def test_extra_default_and_optional_scopes_are_stripped_on_every_run(
        self, run: EvidenceRun
    ) -> None:
        planted = _state()
        planted["default"].append({"id": "s-q", "name": "audittrace:query"})
        planted["optional"].append({"id": "s-f", "name": "memory:files:read-own"})
        proc = run.go(client=planted)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        final = run.final()
        assert sorted(s["name"] for s in final["default"]) == EVIDENCE_SORTED
        assert final["optional"] == []
        assert "stripped unpinned default scope audittrace:query" in proc.stdout
        assert "stripped unpinned optional scope memory:files:read-own" in proc.stdout

    def test_a_missing_audit_default_is_bound_back(self, run: EvidenceRun) -> None:
        short = _state()
        short["default"] = []
        proc = run.go(client=short)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert sorted(s["name"] for s in run.final()["default"]) == EVIDENCE_SORTED

    @pytest.mark.parametrize(
        "flag", ["fullScopeAllowed", "publicClient", "serviceAccountsEnabled"]
    )
    def test_a_drifted_flag_fails_closed_and_is_never_updated(
        self, run: EvidenceRun, flag: str
    ) -> None:
        drifted = _state()
        drifted["body"][flag] = not drifted["body"][flag]
        proc = run.go(client=drifted)
        assert proc.returncode != 0
        assert "code=drift" in proc.stdout
        assert f'missing:"{flag}"' in proc.stdout
        assert "BLOCK-COMPLETED" not in proc.stdout
        assert run.calls("update") == []

    @pytest.mark.parametrize(
        ("kind", "name", "expected"),
        [
            ("optional", "memory:files:read-own", "unexpected-optional-scope:count=1"),
            ("default", "audittrace:query", "unexpected-default-scope:count=1"),
        ],
    )
    def test_verify_refuses_an_extra_scope_the_strip_failed_to_remove(
        self, run: EvidenceRun, kind: str, name: str, expected: str
    ) -> None:
        planted = _state()
        planted[kind].append({"id": "s-x", "name": name})
        proc = run.go(client=planted, delete_noop=True)
        assert proc.returncode != 0
        assert "code=drift" in proc.stdout
        assert expected in proc.stdout
        assert "BLOCK-COMPLETED" not in proc.stdout

    def test_an_extra_mapper_fails_closed(self, run: EvidenceRun) -> None:
        proc = run.go(client=_state(mappers=["aud-audittrace-server", "roles"]))
        assert proc.returncode != 0
        assert "unexpected-mapper:count=1" in proc.stdout

    def test_a_missing_mapper_fails_closed(self, run: EvidenceRun) -> None:
        proc = run.go(client=_state(mappers=[]))
        assert proc.returncode != 0
        assert "mapper-count:0" in proc.stdout

    def test_a_lost_authorized_source_attribute_fails_closed(
        self, run: EvidenceRun
    ) -> None:
        drifted = _state()
        drifted["body"]["attributes"] = {}
        proc = run.go(client=drifted)
        assert proc.returncode != 0
        assert "token-exchange.authorized-source-client" in proc.stdout

    def test_two_matching_clients_are_refused(self, run: EvidenceRun) -> None:
        proc = run.go(client=_state(), clients=[{"id": "uuid-other"}])
        assert proc.returncode != 0
        assert "code=ambiguous-match" in proc.stdout
        assert run.calls("update") == []

    def test_the_audit_scope_absent_from_the_realm_fails_closed(
        self, run: EvidenceRun
    ) -> None:
        """audittrace:audit is realm-defined (never created by the script): an
        absent one must stop the block, never bind a partial set."""
        proc = run.go(client=_state(), realm_scopes=[])
        assert proc.returncode != 0
        assert "code=id-unresolvable" in proc.stdout
        assert "BLOCK-COMPLETED" not in proc.stdout


class TestStep4cAuthorizesTheSourceClientForTheEvidenceAudience:
    @staticmethod
    def _harness(site: Path, *, missing: bool) -> str:
        return (
            "set -euo pipefail\n"
            'REALM="audittrace"\n'
            'TOKEN_EXCHANGE_SOURCE_CLIENT="audittrace-librechat-bff"\n'
            'REALM_MGMT_UUID="fake-uuid-realm-management"\n'
            'TOKEN_EXCHANGE_POLICY_ID="fake-policy-id"\n'
            "find_client_id() {\n"
            f'  if [[ "{int(missing)}" == "1" && "$1" == "{EVIDENCE}" ]]; then return 1; fi\n'
            '  echo "fake-uuid-$1"\n'
            "}\n"
            "acl_e2e_kc() {\n"
            '  echo "$*" >> "${FAKE_LOG}"\n'
            '  case "$1 $2" in\n'
            '    "get clients/"*"/management/permissions")\n'
            '      printf \'{\\n  "scopePermissions" : {\\n    "token-exchange" : "evid-perm-id"\\n  }\\n}\\n\' ;;\n'
            "  esac\n"
            "}\n" + _step4c(site)
        )

    def _run(
        self, tmp_path: Path, site: Path, *, missing: bool = False
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        log = tmp_path / "calls.log"
        log.write_text("")
        proc = subprocess.run(
            ["bash", "-c", self._harness(site, missing=missing)],
            capture_output=True,
            text=True,
            env={**os.environ, "FAKE_LOG": str(log)},
            timeout=60,
        )
        return proc, log.read_text().splitlines()

    @pytest.mark.parametrize("site", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_the_evidence_audience_permission_gets_the_same_policy(
        self, tmp_path: Path, site: Path
    ) -> None:
        proc, calls = self._run(tmp_path, site)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert (
            f"update clients/fake-uuid-{EVIDENCE}/management/permissions -r audittrace -s enabled=true"
            in calls
        )
        assert any(
            c.startswith(
                "update clients/fake-uuid-realm-management/authz/resource-server/"
                "permission/scope/evid-perm-id"
            )
            and 'policies=["fake-policy-id"]' in c
            for c in calls
        ), calls
        assert "authorized to exchange for audittrace-librechat-evidence" in proc.stdout

    @pytest.mark.parametrize("site", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_a_missing_evidence_client_fails_closed_before_any_update(
        self, tmp_path: Path, site: Path
    ) -> None:
        proc, calls = self._run(tmp_path, site, missing=True)
        assert proc.returncode == 1
        assert "not found" in proc.stderr
        assert calls == []

    @pytest.mark.parametrize("site", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_step4c_sits_beside_step4b_inside_the_guarded_window(
        self, site: Path
    ) -> None:
        text = site.read_text(encoding="utf-8")
        assert text.index("TOKEN_EXCHANGE_CHAT_TARGET_CLIENT=") < text.index(
            "TOKEN_EXCHANGE_EVIDENCE_TARGET_CLIENT="
        )
        assert text.index("TOKEN_EXCHANGE_EVIDENCE_TARGET_CLIENT=") < text.index(
            "memory-scopes provisioning complete"
        )

    @pytest.mark.parametrize("site", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_step4c_runs_after_the_evidence_client_is_ensured(self, site: Path) -> None:
        text = site.read_text(encoding="utf-8")
        assert text.index('evid_aud_verify "${EVID_AUD_UUID}"') < text.index(
            "TOKEN_EXCHANGE_EVIDENCE_TARGET_CLIENT="
        )


class TestTheTwoSitesAndTheRealmsAgree:
    def test_pinned_json_is_byte_identical_in_both_sites(self) -> None:
        assert _pinned(SCRIPT)[0] == _pinned(CONFIGMAP)[0]

    @pytest.mark.parametrize("which", [0, 1], ids=["dev-realm", "chart-render"])
    def test_pinned_json_equals_the_realm_block(self, which: int) -> None:
        pinned = _pinned(SCRIPT)[1]
        assert _client(_both_enabled()[which][1], EVIDENCE) == pinned

    def test_pinned_defaults_are_exactly_the_bff_evidence_scopes(self) -> None:
        pinned = _pinned(SCRIPT)[1]
        assert tuple(pinned["defaultClientScopes"]) == EVIDENCE_SCOPES
        assert pinned["optionalClientScopes"] == []
        assert pinned["fullScopeAllowed"] is False

    @pytest.mark.parametrize("site", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_the_hook_scope_array_and_audience_constants(self, site: Path) -> None:
        text = site.read_text(encoding="utf-8")
        arr = re.search(r"^\s*EVID_AUD_DEFAULT_SCOPES=\((.*?)\)$", text, re.M)
        assert arr is not None
        assert re.findall(r'"([^"]+)"', arr.group(1)) == list(EVIDENCE_SCOPES)
        for name in ("EVID_AUD_CLIENT_ID", "TOKEN_EXCHANGE_EVIDENCE_TARGET_CLIENT"):
            m = re.search(rf'{name}="([^"]+)"', text)
            assert m is not None and m.group(1) == EVIDENCE, name

    @pytest.mark.parametrize("site", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_the_evidence_scopes_are_bound_as_default_to_the_evidence_audience(
        self, site: Path
    ) -> None:
        text = site.read_text(encoding="utf-8")
        assert re.search(
            r'for SCOPE in "\$\{EVID_AUD_DEFAULT_SCOPES\[@\]\}"; do\s*\n'
            r'\s*evid_aud_resolve_scope "\$\{SCOPE\}"\s*\n'
            r'\s*bind_scope "\$\{EVID_AUD_CLIENT_ID\}" "\$\{SCOPE\}" "default"',
            text,
        )

    def test_the_bff_default_audience_is_the_hooked_client(self) -> None:
        assert (
            Settings(exchange_client_secret="x").evidence_exchange_audience == EVIDENCE  # noqa: S106
        )

    def test_the_chat_audience_block_is_untouched(self) -> None:
        """Nothing may change the chat client: its pinned JSON still names the
        six chat scopes and no ``audittrace:audit``."""
        text = SCRIPT.read_text(encoding="utf-8")
        m = re.search(r"CHAT_AUD_CLIENT_JSON='(\{.*?\})'\n", text)
        assert m is not None
        chat = json.loads(m.group(1))
        assert "audittrace:audit" not in chat["defaultClientScopes"]
        assert len(chat["defaultClientScopes"]) == 6


class TestTheDeploymentCarriesTheAudience:
    def test_the_bff_deployment_renders_the_env_var(self) -> None:
        docs = _render(console_enabled=True)
        env = _container_env(_find(docs, "Deployment", "-librechat-bff"), "bff")
        assert env["AUDITTRACE_BFF_EVIDENCE_EXCHANGE_AUDIENCE"] == EVIDENCE

    def test_the_value_is_the_values_key(self) -> None:
        import yaml

        from tests.test_chart_drift_guards import CHART_DIR

        values = yaml.safe_load((CHART_DIR / "values.yaml").read_text())
        assert values["console"]["bff"]["evidenceExchangeAudience"] == EVIDENCE

    def test_the_compose_edge_twin_sets_it_with_the_same_default(self) -> None:
        from tests.test_chart_drift_guards import REPO_ROOT

        text = (REPO_ROOT / "docker-compose.edge.yml").read_text()
        assert (
            "AUDITTRACE_BFF_EVIDENCE_EXCHANGE_AUDIENCE="
            f"${{AUDITTRACE_BFF_EVIDENCE_EXCHANGE_AUDIENCE:-{EVIDENCE}}}" in text
        )
