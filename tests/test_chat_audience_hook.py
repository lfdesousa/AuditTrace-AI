"""LC10A-CHAT-EXCHANGE-SCOPE (C6, C7): the provisioning hook and the dev
provisioner create, converge and authorize the chat token-exchange audience
client on an EXISTING realm, and the BFF Deployment is told its name.

The client block and Step 4b are run VERBATIM from both sites (the dev
provisioner script and the chart ConfigMap) under a small fake ``kcadm``; the
pinned JSON is compared to the realm blocks and to ``bff.chat_scopes``.
Neuters (each RED, then GREEN on restore, recorded in the build record):
drop Step 4b; drop the strip; drop a bind; drop a verify item; change the
pinned JSON in one site; drop the Deployment env var.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from bff.chat_scopes import CHAT_SCOPES
from bff.config import Settings
from tests.test_chart_drift_guards_acl_write import (
    CONFIGMAP,
    SCRIPT,
    _client,
    _preamble_text,
)
from tests.test_chart_drift_guards_session_e2e import _both_enabled
from tests.test_console_k3s_guards import _container_env, _find, _render

CHAT = "audittrace-librechat-chat"
CLIENT_UUID = "uuid-chat"
CHAT_SORTED = sorted(CHAT_SCOPES)

FAKE_KCADM = (
    textwrap.dedent(
        """\
    #!__PY__
    import json, os, sys
    args = sys.argv[1:]
    LOG, STATE = os.environ["FAKE_LOG"], os.environ["FAKE_STATE"]
    with open(LOG, "a") as fh:
        fh.write(" ".join(args) + "\\n")
    state = json.load(open(STATE))

    def save():
        json.dump(state, open(STATE, "w"))

    def flag(name):
        return args[args.index(name) + 1] if name in args else None

    cmd = args[0]
    if cmd == "bind":
        c = state["client"]
        kind, name = args[3], args[2]
        if name not in [s["name"] for s in c[kind]]:  # a PUT bind is idempotent
            c[kind].append({"id": "s-" + name, "name": name})
        save()
        sys.exit(0)
    if cmd == "get" and args[1] == "client-scopes":
        for s in state["realm_scopes"]:
            print(s["id"] + "," + s["name"])
        sys.exit(0)
    if cmd == "get" and args[1] == "clients":
        want = flag("-q").split("=", 1)[1]
        for c in state["clients"] if "clients" in state else []:
            print(c["id"] + "," + want)
        c = state.get("client")
        if c and c["body"]["clientId"] == want:
            print(CLIENT_UUID + "," + want)
        sys.exit(0)
    if cmd == "get":
        parts = args[1].split("/")
        c = state["client"]
        if len(parts) == 2:
            print(json.dumps(c["body"], indent=2, separators=(",", " : ")))
        elif parts[2] in ("default-client-scopes", "optional-client-scopes"):
            key = "default" if parts[2].startswith("default") else "optional"
            for s in c[key]:
                print(s["id"] + "," + s["name"] if flag("--fields") == "id,name" else s["name"])
        elif parts[2] == "protocol-mappers":
            for m in c["mappers"]:
                print(m)
        sys.exit(0)
    if cmd == "create":
        body = json.load(sys.stdin)
        open(os.environ["FAKE_CREATED"], "w").write(json.dumps(body))
        stored = {k: v for k, v in body.items() if k not in
                  ("defaultClientScopes", "optionalClientScopes", "protocolMappers")}
        # Keycloak adds realm-default scopes on create: model it.
        state["client"] = {
            "body": stored,
            "default": [{"id": "s-profile", "name": "profile"}],
            "optional": [{"id": "s-offline_access", "name": "offline_access"}],
            "mappers": [m["name"] for m in body["protocolMappers"]],
        }
        save()
        sys.exit(0)
    if cmd == "delete" and state.get("delete_noop"):
        sys.exit(0)  # a strip that does not take effect: verify must still refuse
    if cmd == "delete":
        parts = args[1].split("/")
        key = "default" if parts[2].startswith("default") else "optional"
        c = state["client"]
        c[key] = [s for s in c[key] if s["id"] != parts[3]]
        save()
        sys.exit(0)
    sys.exit(0)
    """
    )
    .replace("__PY__", sys.executable)
    .replace("CLIENT_UUID", repr(CLIENT_UUID))
)


def _pinned(path: Path) -> tuple[str, dict[str, Any]]:
    m = re.search(r"CHAT_AUD_CLIENT_JSON='(\{.*?\})'\n", path.read_text())
    assert m is not None, f"{path.name}: pinned CHAT_AUD_CLIENT_JSON not found"
    return m.group(1), json.loads(m.group(1))


def _block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index("CHAT_AUD_CLIENT_ID=")
    marker = 'chat_aud_verify "${CHAT_AUD_UUID}"\n'
    return text[start : text.rindex(marker) + len(marker)]


def _step4b(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index("TOKEN_EXCHANGE_CHAT_TARGET_CLIENT=")
    marker = 'authorized to exchange for ${TOKEN_EXCHANGE_CHAT_TARGET_CLIENT}"\n'
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
        "default": [{"id": "s-" + n, "name": n} for n in CHAT_SORTED],
        "optional": [],
        "mappers": ["aud-audittrace-server"],
    }
    client.update(over)
    return client


class Run:
    def __init__(self, tmp: Path, site: Path) -> None:
        self.site = site
        self.state_path = tmp / "state.json"
        self.log = tmp / "calls.log"
        self.created = tmp / "created.json"
        self.fake = tmp / "fake-kcadm"
        self.fake.write_text(FAKE_KCADM)
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)
        self.log.write_text("")

    def go(
        self, *, client: dict[str, Any] | None, block: str | None = None, **extra: Any
    ) -> subprocess.CompletedProcess[str]:
        self.state_path.write_text(
            json.dumps(
                {
                    "client": client,
                    "realm_scopes": [{"id": "s-" + s, "name": s} for s in CHAT_SORTED],
                    **extra,
                }
            )
        )
        script = (
            "set -euo pipefail\n"
            + _preamble_text(self.site)
            + 'REALM="audittrace"\n'
            + f'acl_e2e_kc() {{ ACL_E2E_VIA_KC=1 "{self.fake}" "$@"; }}\n'
            + "declare -A SCOPE_ID\n"
            + 'bind_scope() { "'
            + str(self.fake)
            + '" bind "$1" "$2" "$3"; }\n'
            + (block if block is not None else _block(self.site))
            + '\necho "BLOCK-COMPLETED"\n'
        )
        env = {
            **os.environ,
            "FAKE_STATE": str(self.state_path),
            "FAKE_LOG": str(self.log),
            "FAKE_CREATED": str(self.created),
        }
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60
        )

    def calls(self, verb: str) -> list[str]:
        return [ln for ln in self.log.read_text().splitlines() if ln.startswith(verb)]

    def final(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text())["client"]


@pytest.fixture(params=[SCRIPT, CONFIGMAP], ids=["script", "configmap"])
def run(request: pytest.FixtureRequest, tmp_path: Path) -> Run:
    return Run(tmp_path, request.param)


class TestTheClientIsEnsuredOnAnExistingRealm:
    def test_an_absent_client_is_created_stripped_bound_and_verified(
        self, run: Run
    ) -> None:
        proc = run.go(client=None)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "BLOCK-COMPLETED" in proc.stdout
        assert json.loads(run.created.read_text())["clientId"] == CHAT
        final = run.final()
        assert sorted(s["name"] for s in final["default"]) == CHAT_SORTED
        assert final["optional"] == []
        assert "verified equal to the pinned block" in proc.stdout

    def test_a_correct_client_is_verified_and_never_updated(self, run: Run) -> None:
        proc = run.go(client=_state())
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert run.calls("create") == []
        assert run.calls("update") == []

    def test_extra_default_and_optional_scopes_are_stripped_on_every_run(
        self, run: Run
    ) -> None:
        planted = _state()
        planted["default"].append({"id": "s-w", "name": "memory:episodic:write"})
        planted["optional"].append({"id": "s-f", "name": "memory:files:read-own"})
        proc = run.go(client=planted)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        final = run.final()
        assert sorted(s["name"] for s in final["default"]) == CHAT_SORTED
        assert final["optional"] == []
        assert "stripped unpinned default scope memory:episodic:write" in proc.stdout
        assert "stripped unpinned optional scope memory:files:read-own" in proc.stdout

    def test_a_missing_chat_default_is_bound_back(self, run: Run) -> None:
        short = _state()
        short["default"] = short["default"][:-1]
        proc = run.go(client=short)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert sorted(s["name"] for s in run.final()["default"]) == CHAT_SORTED

    @pytest.mark.parametrize(
        "flag", ["fullScopeAllowed", "publicClient", "serviceAccountsEnabled"]
    )
    def test_a_drifted_flag_fails_closed_and_is_never_updated(
        self, run: Run, flag: str
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
            ("default", "memory:episodic:write", "unexpected-default-scope:count=1"),
        ],
    )
    def test_verify_refuses_an_extra_scope_the_strip_failed_to_remove(
        self, run: Run, kind: str, name: str, expected: str
    ) -> None:
        """The strip runs first, so verify's scope checks are the second line
        of defence: with a strip that takes no effect they must still stop it."""
        planted = _state()
        planted[kind].append({"id": "s-x", "name": name})
        proc = run.go(client=planted, delete_noop=True)
        assert proc.returncode != 0
        assert "code=drift" in proc.stdout
        assert expected in proc.stdout
        assert "BLOCK-COMPLETED" not in proc.stdout

    def test_an_extra_mapper_fails_closed(self, run: Run) -> None:
        proc = run.go(client=_state(mappers=["aud-audittrace-server", "roles"]))
        assert proc.returncode != 0
        assert "unexpected-mapper:count=1" in proc.stdout
        assert "mapper-count:2" in proc.stdout

    def test_a_missing_mapper_fails_closed(self, run: Run) -> None:
        proc = run.go(client=_state(mappers=[]))
        assert proc.returncode != 0
        assert "mapper-count:0" in proc.stdout

    def test_a_lost_authorized_source_attribute_fails_closed(self, run: Run) -> None:
        drifted = _state()
        drifted["body"]["attributes"] = {}
        proc = run.go(client=drifted)
        assert proc.returncode != 0
        assert "token-exchange.authorized-source-client" in proc.stdout

    def test_two_matching_clients_are_refused(self, run: Run) -> None:
        proc = run.go(client=_state(), clients=[{"id": "uuid-other"}])
        assert proc.returncode != 0
        assert "code=ambiguous-match" in proc.stdout
        assert run.calls("update") == []

    def test_a_chat_scope_absent_from_the_realm_fails_closed(self, run: Run) -> None:
        """audittrace:query is realm-defined (never created by the script): an
        absent one must stop the block, never bind a partial set."""
        proc = run.go(
            client=_state(),
            realm_scopes=[
                {"id": "s-" + n, "name": n}
                for n in CHAT_SORTED
                if n != "audittrace:query"
            ],
        )
        assert proc.returncode != 0
        assert "code=id-unresolvable" in proc.stdout
        assert "BLOCK-COMPLETED" not in proc.stdout


class TestStep4bAuthorizesTheSourceClientForTheChatAudience:
    @staticmethod
    def _harness(site: Path, *, missing: bool) -> str:
        return (
            "set -euo pipefail\n"
            'REALM="audittrace"\n'
            'TOKEN_EXCHANGE_SOURCE_CLIENT="audittrace-librechat-bff"\n'
            'REALM_MGMT_UUID="fake-uuid-realm-management"\n'
            'TOKEN_EXCHANGE_POLICY_ID="fake-policy-id"\n'
            "find_client_id() {\n"
            f'  if [[ "{int(missing)}" == "1" && "$1" == "{CHAT}" ]]; then return 1; fi\n'
            '  echo "fake-uuid-$1"\n'
            "}\n"
            "acl_e2e_kc() {\n"
            '  echo "$*" >> "${FAKE_LOG}"\n'
            '  case "$1 $2" in\n'
            '    "get clients/"*"/management/permissions")\n'
            '      printf \'{\\n  "scopePermissions" : {\\n    "token-exchange" : "chat-perm-id"\\n  }\\n}\\n\' ;;\n'
            "  esac\n"
            "}\n" + _step4b(site)
        )

    def _run(self, tmp_path: Path, site: Path, *, missing: bool = False):
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
    def test_the_chat_audience_permission_gets_the_same_policy(
        self, tmp_path: Path, site: Path
    ) -> None:
        proc, calls = self._run(tmp_path, site)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert (
            f"update clients/fake-uuid-{CHAT}/management/permissions -r audittrace -s enabled=true"
            in calls
        )
        assert any(
            c.startswith(
                "update clients/fake-uuid-realm-management/authz/resource-server/"
                "permission/scope/chat-perm-id"
            )
            and 'policies=["fake-policy-id"]' in c
            for c in calls
        ), calls
        assert "authorized to exchange for audittrace-librechat-chat" in proc.stdout

    @pytest.mark.parametrize("site", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_a_missing_chat_client_fails_closed_before_any_update(
        self, tmp_path: Path, site: Path
    ) -> None:
        proc, calls = self._run(tmp_path, site, missing=True)
        assert proc.returncode == 1
        assert "not found" in proc.stderr
        assert calls == []

    @pytest.mark.parametrize("site", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_step4b_sits_inside_the_window_the_existing_step4_guards_run(
        self, site: Path
    ) -> None:
        text = site.read_text(encoding="utf-8")
        assert text.index("find_policy_id() {") < text.index(
            "TOKEN_EXCHANGE_CHAT_TARGET_CLIENT="
        )
        assert text.index("TOKEN_EXCHANGE_CHAT_TARGET_CLIENT=") < text.index(
            "memory-scopes provisioning complete"
        )


class TestTheTwoSitesAndTheRealmsAgree:
    def test_pinned_json_is_byte_identical_in_both_sites(self) -> None:
        assert _pinned(SCRIPT)[0] == _pinned(CONFIGMAP)[0]

    @pytest.mark.parametrize("which", [0, 1], ids=["dev-realm", "chart-render"])
    def test_pinned_json_equals_the_realm_block(self, which: int) -> None:
        pinned = _pinned(SCRIPT)[1]
        assert _client(_both_enabled()[which][1], CHAT) == pinned

    def test_pinned_defaults_are_exactly_the_bff_chat_scopes(self) -> None:
        pinned = _pinned(SCRIPT)[1]
        assert sorted(pinned["defaultClientScopes"]) == CHAT_SORTED
        assert pinned["optionalClientScopes"] == []
        assert pinned["fullScopeAllowed"] is False

    @pytest.mark.parametrize("site", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_the_hook_scope_array_and_audience_constants(self, site: Path) -> None:
        text = site.read_text(encoding="utf-8")
        arr = re.search(r"^\s*CHAT_AUD_DEFAULT_SCOPES=\((.*?)\)$", text, re.M)
        assert arr is not None
        assert sorted(re.findall(r'"([^"]+)"', arr.group(1))) == CHAT_SORTED
        for name in ("CHAT_AUD_CLIENT_ID", "TOKEN_EXCHANGE_CHAT_TARGET_CLIENT"):
            m = re.search(rf'{name}="([^"]+)"', text)
            assert m is not None and m.group(1) == CHAT, name

    def test_the_bff_default_audience_is_the_hooked_client(self) -> None:
        assert (
            Settings(exchange_client_secret="x").chat_exchange_audience == CHAT  # noqa: S106
        )


class TestTheDeploymentCarriesTheAudience:
    def test_the_bff_deployment_renders_the_env_var(self) -> None:
        docs = _render(console_enabled=True)
        env = _container_env(_find(docs, "Deployment", "-librechat-bff"), "bff")
        assert env["AUDITTRACE_BFF_CHAT_EXCHANGE_AUDIENCE"] == CHAT

    def test_the_value_is_the_values_key(self) -> None:
        import yaml

        from tests.test_chart_drift_guards import CHART_DIR

        values = yaml.safe_load((CHART_DIR / "values.yaml").read_text())
        assert values["console"]["bff"]["chatExchangeAudience"] == CHAT

    def test_the_compose_edge_twin_sets_it_with_the_same_default(self) -> None:
        from tests.test_chart_drift_guards import REPO_ROOT

        text = (REPO_ROOT / "docker-compose.edge.yml").read_text()
        assert (
            f"AUDITTRACE_BFF_CHAT_EXCHANGE_AUDIENCE=${{AUDITTRACE_BFF_CHAT_EXCHANGE_AUDIENCE:-{CHAT}}}"
            in text
        )
