"""BEHAVIOUR of the ``ensure_client_audittrace_acl_e2e`` provisioner step
(ACL WU-2c-A, Addenda B §3 SA-4 / §4, C §2 SB-1..SB-3, gate round-4 SC-1)
in BOTH sites — ``scripts/setup-memory-scopes.sh`` and the chart's
in-cluster ConfigMap script — executed with real ``bash`` against a FAKE
``kcadm`` (a small stateful stub; no Keycloak is touched).

What this proves, per site: create-when-absent (including stripping the
realm-default scopes a real create may add, by rule), no-op when present
and equal, **fail closed on every kind of drift without ever calling
``kcadm update``** (extra optional scope, widened flag, dropped consent,
lifespan drift, a second protocol mapper — SC-1 —, a changed mapper
config, a missing default scope), the sunset branch (verify exactly-one +
clientId-equal BEFORE delete, empty read-back after), and fail-closed on
a failed lookup.

What it does NOT prove: that real Keycloak behaves like the stub (the
``kcadm get`` output shapes, whether realm default scopes are added on
REST create, whether the per-client attributes are honoured). Those are
``Keycloak-asserted`` and proven only by the live read-back (the runbook).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from tests.test_chart_drift_guards_acl_write import (
    CONFIGMAP,
    E2E,
    PINNED_ATTRIBUTES,
    PINNED_FLAGS,
    PINNED_MAPPER,
    SCRIPT,
    _pinned_json,
)

FAKE_KCADM = textwrap.dedent(
    '''\
    #!__PY__
    """Stateful fake of the few kcadm calls the step makes."""
    import json, os, sys, uuid

    STATE = os.environ["FAKE_STATE"]
    LOG = os.environ["FAKE_LOG"]
    args = sys.argv[1:]
    with open(LOG, "a") as fh:
        fh.write(" ".join(args) + "\\n")
    state = json.load(open(STATE))

    def save():
        json.dump(state, open(STATE, "w"))

    def flag(name):
        return args[args.index(name) + 1] if name in args else None

    def pretty(obj):
        return json.dumps(obj, indent=2, separators=(",", " : "))

    def client(uid):
        return next(c for c in state["clients"] if c["id"] == uid)

    cmd = args[0]
    if state.get("fail_all"):
        print("kcadm: simulated failure", file=sys.stderr)
        sys.exit(1)
    if cmd == "get" and args[1] == "clients":
        if state.get("fail_lookup"):
            print("kcadm: lookup failed", file=sys.stderr)
            sys.exit(1)
        want = flag("-q").split("=", 1)[1]
        for c in state["clients"]:
            if c["body"]["clientId"] == want or state.get("lookup_returns_all"):
                print(c["id"] + "," + c["body"]["clientId"])
        sys.exit(0)
    if cmd == "get":
        parts = args[1].split("/")
        c = client(parts[1])
        if len(parts) == 2:
            print(pretty(c["body"]))
        elif parts[2] in ("default-client-scopes", "optional-client-scopes"):
            key = "default" if parts[2].startswith("default") else "optional"
            fields = flag("--fields")
            for s in c[key]:
                print(s["id"] + "," + s["name"] if fields == "id,name" else s["name"])
        elif parts[2] == "protocol-mappers":
            if "--fields" in args:
                for m in c["mappers"]:
                    print(m["name"])
            else:
                print(pretty(c["mappers"]))
        sys.exit(0)
    if cmd == "create":
        body = json.load(sys.stdin)
        uid = str(uuid.uuid4())
        attrs = dict(body.get("attributes", {}))
        attrs.update(state.get("server_added_attributes", {}))
        stored = {k: v for k, v in body.items() if k not in
                  ("defaultClientScopes", "optionalClientScopes", "protocolMappers")}
        stored["attributes"] = attrs
        state["clients"].append({
            "id": uid, "body": stored,
            "default": [dict(s) for s in state.get("realm_default_scopes", [])],
            "optional": [dict(s) for s in state.get("realm_optional_scopes", [])],
            "mappers": [
                dict(m, config=dict(m.get("config", {}), **state.get("extra_mapper_config_on_create", {})))
                for m in body.get("protocolMappers", [])
            ] + state.get("extra_mappers_on_create", []),
        })
        save()
        sys.exit(0)
    if cmd == "delete":
        parts = args[1].split("/")
        if len(parts) == 2:
            if not state.get("delete_noop"):
                state["clients"] = [c for c in state["clients"] if c["id"] != parts[1]]
        else:
            key = "default" if parts[2].startswith("default") else "optional"
            c = client(parts[1])
            c[key] = [s for s in c[key] if s["id"] != parts[3]]
        save()
        sys.exit(0)
    if cmd == "__bind":
        _, cid, scope, kind = args
        c = next(x for x in state["clients"] if x["body"]["clientId"] == cid)
        c[kind].append({"id": "bound-" + scope, "name": scope})
        save()
        sys.exit(0)
    sys.exit(0)  # update / anything else: logged only
    '''
).replace("__PY__", sys.executable)


def _step_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index("ACL_E2E_CLIENT_ID=")
    end_marker = 'deleted (read-back empty)"\n'
    end = text.index(end_marker) + len(end_marker)
    # the closing `  fi\nfi` of the two nested ifs
    tail = text[end:]
    closing = tail[: tail.index("fi") + 2]
    closing += tail[len(closing) : tail.index("fi", len(closing)) + 2]
    return text[start : end + len(closing)]


def _pinned_client_state(**drift: Any) -> dict[str, Any]:
    body = {
        "clientId": E2E,
        **PINNED_FLAGS,
        "enabled": True,
        "protocol": "openid-connect",
        "attributes": dict(PINNED_ATTRIBUTES),
    }
    state: dict[str, Any] = {
        "id": "uuid-e2e",
        "body": body,
        "default": [
            {"id": "s-read", "name": "memory:acl:read-own"},
            {"id": "s-write", "name": "memory:acl:write"},
        ],
        "optional": [],
        "mappers": [json.loads(json.dumps(PINNED_MAPPER))],
    }
    for key, value in drift.items():
        if key in body:
            body[key] = value
        elif key == "attribute":
            body["attributes"].update(value)
        else:
            state[key] = value
    return state


class Run:
    def __init__(self, tmp: Path, site: Path) -> None:
        self.tmp = tmp
        self.site = site
        self.state_path = tmp / "state.json"
        self.log = tmp / "calls.log"
        self.fake = tmp / "fake-kcadm"
        self.fake.write_text(FAKE_KCADM)
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)
        self.log.write_text("")

    def go(
        self, *, clients: list[dict[str, Any]], enabled: str = "true", **extra: Any
    ) -> subprocess.CompletedProcess[str]:
        self.state_path.write_text(json.dumps({"clients": clients, **extra}))
        step = _step_text(self.site)
        if self.site == CONFIGMAP:
            prelude = f'KCADM="{self.fake}"\n'
        else:
            prelude = f'kcadm() {{ "{self.fake}" "$@"; }}\n'
        script = (
            "set -euo pipefail\n"
            'REALM="audittrace"\n'
            + prelude
            + f'bind_scope() {{ "{self.fake}" __bind "$1" "$2" "$3"; }}\n'
            + step
            + '\necho "STEP-COMPLETED"\n'
        )
        env = {
            **os.environ,
            "FAKE_STATE": str(self.state_path),
            "FAKE_LOG": str(self.log),
            "AUDITTRACE_ACL_E2E_CLIENT_ENABLED": enabled,
        }
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60
        )

    def state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text())

    def calls(self) -> list[str]:
        return [ln for ln in self.log.read_text().splitlines() if ln]

    def mutating_calls(self) -> list[str]:
        return [
            c for c in self.calls() if c.split()[0] in ("create", "delete", "update")
        ]


@pytest.fixture(params=[SCRIPT, CONFIGMAP], ids=["script", "configmap"])
def run(request: pytest.FixtureRequest, tmp_path: Path) -> Run:
    return Run(tmp_path, request.param)


REALM_DEFAULTS = {
    "realm_default_scopes": [
        {"id": "rd-profile", "name": "profile"},
        {"id": "rd-email", "name": "email"},
    ],
    "realm_optional_scopes": [{"id": "ro-offline", "name": "offline_access"}],
    "server_added_attributes": {"pkce.code.challenge.method": "S256"},
}


class TestEnabledCreate:
    def test_absent_is_created_stripped_bound_and_verified(self, run: Run) -> None:
        proc = run.go(clients=[], **REALM_DEFAULTS)
        assert proc.returncode == 0, proc.stderr
        assert "STEP-COMPLETED" in proc.stdout
        (client,) = run.state()["clients"]
        assert {s["name"] for s in client["default"]} == {
            "memory:acl:read-own",
            "memory:acl:write",
        }
        assert client["optional"] == [], "offline_access must be stripped by rule"
        assert {m["name"] for m in client["mappers"]} == {"aud-audittrace-server"}
        assert any(c.startswith("create clients") for c in run.calls())
        assert not any(c.startswith("update") for c in run.calls())
        assert "stripped unpinned default scope profile" in proc.stdout
        assert "stripped unpinned optional scope offline_access" in proc.stdout

    def test_created_client_carries_the_pinned_json_verbatim(self, run: Run) -> None:
        run.go(clients=[], **REALM_DEFAULTS)
        (client,) = run.state()["clients"]
        raw, pinned = _pinned_json(SCRIPT)
        assert client["body"]["clientId"] == pinned["clientId"]
        for key in PINNED_FLAGS:
            assert client["body"][key] == pinned[key]
        for key, value in pinned["attributes"].items():
            assert client["body"]["attributes"][key] == value
        assert "'" not in raw

    def test_extra_mapper_added_by_the_server_on_create_fails_closed(
        self, run: Run
    ) -> None:
        """SC-1 on the create path: a server-added second mapper survives
        no strip (mappers are not stripped) and the verify fails."""
        proc = run.go(
            clients=[],
            extra_mappers_on_create=[{"name": "injected", "config": {}}],
            **REALM_DEFAULTS,
        )
        assert proc.returncode != 0
        assert "unexpected-mapper:injected" in proc.stderr
        assert "STEP-COMPLETED" not in proc.stdout


class TestEnabledCreateServerAddedConfig:
    def test_server_added_mapper_config_key_on_create_fails_closed(
        self, run: Run
    ) -> None:
        """SC-1 EXACT: a config key the SERVER adds on create (modelled by
        the fake) is not tolerated — no allow-list. If a real Keycloak does
        this, the live read-back fails and the spec is amended."""
        proc = run.go(
            clients=[],
            extra_mapper_config_on_create={"introspection.token.claim": "true"},
            **REALM_DEFAULTS,
        )
        assert proc.returncode != 0
        assert "unexpected-mapper-config:" in proc.stderr
        assert "STEP-COMPLETED" not in proc.stdout


class TestEnabledPresent:
    def test_equal_is_a_no_op(self, run: Run) -> None:
        proc = run.go(clients=[_pinned_client_state()])
        assert proc.returncode == 0, proc.stderr
        assert "verified equal to the pinned block" in proc.stdout
        assert run.mutating_calls() == []

    def test_server_added_attribute_keys_are_tolerated_subset_compare(
        self, run: Run
    ) -> None:
        proc = run.go(clients=[_pinned_client_state(attribute={"some.other.key": "x"})])
        assert proc.returncode == 0, proc.stderr

    @pytest.mark.parametrize(
        ("label", "drift", "needle"),
        [
            (
                "extra optional scope (offline_access)",
                {"optional": [{"id": "o", "name": "offline_access"}]},
                "unexpected-optional-scope:offline_access",
            ),
            (
                "extra default scope",
                {
                    "default": [
                        {"id": "a", "name": "memory:acl:read-own"},
                        {"id": "b", "name": "memory:acl:write"},
                        {"id": "c", "name": "audittrace:admin"},
                    ]
                },
                "unexpected-default-scope:audittrace:admin",
            ),
            (
                "missing default scope",
                {"default": [{"id": "a", "name": "memory:acl:read-own"}]},
                "default-scope-count:1",
            ),
            (
                "standard flow widened",
                {"standardFlowEnabled": True},
                '"standardFlowEnabled":false',
            ),
            ("consent dropped", {"consentRequired": False}, '"consentRequired":true'),
            ("not public", {"publicClient": False}, '"publicClient":true'),
            (
                "lifespan drift",
                {"attribute": {"access.token.lifespan": "86400"}},
                '"access.token.lifespan":"900"',
            ),
            (
                "refresh tokens on",
                {"attribute": {"use.refresh.tokens": "true"}},
                '"use.refresh.tokens":"false"',
            ),
            (
                "device grant off",
                {"attribute": {"oauth2.device.authorization.grant.enabled": "false"}},
                "oauth2.device.authorization.grant.enabled",
            ),
            (
                "SC-1 second mapper",
                {
                    "mappers": [
                        PINNED_MAPPER,
                        {
                            "name": "scope-injector",
                            "protocolMapper": "oidc-hardcoded-claim-mapper",
                            "config": {"claim.name": "scope"},
                        },
                    ]
                },
                "unexpected-mapper:scope-injector",
            ),
            (
                "SC-1 mapper config changed",
                {
                    "mappers": [
                        {
                            **PINNED_MAPPER,
                            "config": {
                                **PINNED_MAPPER["config"],
                                "included.custom.audience": "some-other-api",
                            },
                        }
                    ]
                },
                'unexpected-mapper-config:"included.custom.audience":"some-other-api"',
            ),
            (
                "SC-1 extra config key: audience widening",
                {
                    "mappers": [
                        {
                            **PINNED_MAPPER,
                            "config": {
                                **PINNED_MAPPER["config"],
                                "included.client.audience": "audittrace-librechat",
                            },
                        }
                    ]
                },
                "unexpected-mapper-config:",
            ),
            (
                "SC-1 extra config key: userinfo claim",
                {
                    "mappers": [
                        {
                            **PINNED_MAPPER,
                            "config": {
                                **PINNED_MAPPER["config"],
                                "userinfo.token.claim": "true",
                            },
                        }
                    ]
                },
                "mapper-config-count:4",
            ),
            (
                "SC-1 config key missing",
                {
                    "mappers": [
                        {
                            **PINNED_MAPPER,
                            "config": {
                                "included.custom.audience": "audittrace-server",
                                "id.token.claim": "false",
                            },
                        }
                    ]
                },
                "mapper-config-count:2",
            ),
            (
                "SC-1 config absent",
                {
                    "mappers": [
                        {k: v for k, v in PINNED_MAPPER.items() if k != "config"}
                    ]
                },
                "mapper-config-absent",
            ),
            (
                "SC-1 mapper type changed",
                {
                    "mappers": [
                        {
                            **PINNED_MAPPER,
                            "protocolMapper": "oidc-hardcoded-claim-mapper",
                        }
                    ]
                },
                '"protocolMapper":"oidc-audience-mapper"',
            ),
            ("no mappers", {"mappers": []}, "mapper-count:0"),
        ],
    )
    def test_drift_fails_closed_without_any_mutation(
        self, run: Run, label: str, drift: dict[str, Any], needle: str
    ) -> None:
        proc = run.go(clients=[_pinned_client_state(**drift)])
        assert proc.returncode != 0, label
        assert "STEP-COMPLETED" not in proc.stdout, label
        assert needle in proc.stderr, (label, proc.stderr)
        assert "drift from the pinned client" in proc.stderr
        assert run.mutating_calls() == [], f"{label}: a drifted client was mutated"

    def test_two_matching_clients_refused(self, run: Run) -> None:
        second = _pinned_client_state()
        second["id"] = "uuid-two"
        proc = run.go(clients=[_pinned_client_state(), second])
        assert proc.returncode != 0
        assert "more than one client" in proc.stderr
        assert run.mutating_calls() == []

    @pytest.mark.parametrize("failure", ["fail_lookup", "fail_all"])
    def test_a_failed_read_is_fatal_never_a_pass(self, run: Run, failure: str) -> None:
        proc = run.go(clients=[_pinned_client_state()], **{failure: True})
        assert proc.returncode != 0
        assert "STEP-COMPLETED" not in proc.stdout
        assert run.mutating_calls() == []


class TestDisabledSunset:
    def test_present_is_verified_then_deleted_with_empty_readback(
        self, run: Run
    ) -> None:
        proc = run.go(clients=[_pinned_client_state()], enabled="false")
        assert proc.returncode == 0, proc.stderr
        assert run.state()["clients"] == []
        deletes = [c for c in run.calls() if c.startswith("delete")]
        assert deletes == ["delete clients/uuid-e2e -r audittrace"]
        assert "deleted (read-back empty)" in proc.stdout

    def test_absent_is_a_success_with_no_delete(self, run: Run) -> None:
        proc = run.go(clients=[], enabled="false")
        assert proc.returncode == 0, proc.stderr
        assert "absent (empty query result)" in proc.stdout
        assert run.mutating_calls() == []

    def test_two_matches_refuse_to_delete(self, run: Run) -> None:
        second = _pinned_client_state()
        second["id"] = "uuid-two"
        proc = run.go(clients=[_pinned_client_state(), second], enabled="false")
        assert proc.returncode != 0
        assert "not return exactly one" in proc.stderr
        assert run.mutating_calls() == []

    def test_a_delete_that_did_not_remove_the_client_fails_the_readback(
        self, run: Run
    ) -> None:
        """The read-back is the proof: ``kcadm delete`` returning 0 is not
        enough — the lookup after it must be empty."""
        proc = run.go(
            clients=[_pinned_client_state()], enabled="false", delete_noop=True
        )
        assert proc.returncode != 0
        assert "read-back after delete is not empty" in proc.stderr
        assert "STEP-COMPLETED" not in proc.stdout

    def test_wrong_client_id_refuses_to_delete(self, run: Run) -> None:
        other = _pinned_client_state()
        other["body"]["clientId"] = "audittrace-librechat"
        proc = run.go(clients=[other], enabled="false", lookup_returns_all=True)
        assert proc.returncode != 0
        assert "is not audittrace-acl-e2e" in proc.stderr
        assert run.mutating_calls() == []

    def test_lookup_failure_is_not_treated_as_absent(self, run: Run) -> None:
        proc = run.go(
            clients=[_pinned_client_state()], enabled="false", fail_lookup=True
        )
        assert proc.returncode != 0
        assert run.mutating_calls() == []

    def test_any_value_other_than_true_takes_the_disabled_branch(
        self, run: Run
    ) -> None:
        """Fail toward REMOVAL, never toward silently keeping a
        grant-authority client alive on a typo'd flag."""
        proc = run.go(clients=[_pinned_client_state()], enabled="yes")
        assert proc.returncode == 0
        assert run.state()["clients"] == []
