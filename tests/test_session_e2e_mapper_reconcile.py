"""LC-10a r6 step 2: the session E2E client's audience mapper pins ALL FIVE
config keys, and the one Keycloak-24-defaults drift shape is reconciled.

Operator ruling "pin all 5": ``included.custom.audience``, ``id.token.claim``,
``access.token.claim``, ``introspection.token.claim`` (true) and
``userinfo.token.claim`` (false). Keycloak 24 adds the last two on create with
its own defaults; a client created earlier carries them.

* create sends all five keys;
* verify allows exactly those five VERBATIM lines (a sixth key or any wrong
  value is drift, fail closed, ``mapper-config-count`` is 5);
* a narrow reconcile runs ONLY when the mapper is the session client's
  ``oidc-audience-mapper``, the three original keys are exact, nothing else
  drifted and the only differences are the two defaulted keys (wrong value or
  absent): ONE ``kcadm update`` of that mapper, a ``code=reconciled`` line
  naming KEYS only, then a full re-read + re-verify that fails closed;
* the ACL e2e client has NO reconcile path.

Every guard's neuter (RED then GREEN on restore) is recorded in the build
record. The harness runs the REAL step text of both sites (script and chart
ConfigMap) under a small fake ``kcadm``.
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
    SCRIPT,
    _preamble_text,
)
from tests.test_chart_drift_guards_session_e2e import _pinned_json

PINNED_FIVE = {
    "included.custom.audience": "audittrace-server",
    "id.token.claim": "false",
    "access.token.claim": "true",
    "introspection.token.claim": "true",
    "userinfo.token.claim": "false",
}
MAPPER_ID = "mapper-uuid-1"
CLIENT_UUID = "uuid-session"

FAKE_KCADM = (
    textwrap.dedent(
        """\
    #!__PY__
    import json, os, re, sys
    args = sys.argv[1:]
    LOG, STATE = os.environ["FAKE_LOG"], os.environ["FAKE_STATE"]
    with open(LOG, "a") as fh:
        fh.write(" ".join(args) + "\\n")
    state = json.load(open(STATE))

    def save():
        json.dump(state, open(STATE, "w"))

    def flag(name):
        return args[args.index(name) + 1] if name in args else None

    def pretty(obj):
        return json.dumps(obj, indent=2, separators=(",", " : "))

    cmd = args[0]
    if cmd == "get" and args[1] == "clients":
        want = flag("-q").split("=", 1)[1]
        c = state.get("client")
        if c and c["body"]["clientId"] == want:
            print(CLIENT_UUID + "," + want)
        sys.exit(0)
    if cmd == "get" and args[1] == "client-scopes":
        for s in state["realm_scopes"]:
            print(s["id"] + "," + s["name"])
        sys.exit(0)
    if cmd == "get":
        parts = args[1].split("/")
        c = state["client"]
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
            elif c.get("raw_mappers_text"):
                print(c["raw_mappers_text"])
            else:
                print(pretty(c["mappers"]))
        sys.exit(0)
    if cmd == "create":
        body = json.load(sys.stdin)
        open(os.environ["FAKE_CREATED"], "w").write(json.dumps(body))
        stored = {k: v for k, v in body.items() if k not in
                  ("defaultClientScopes", "optionalClientScopes", "protocolMappers")}
        state["client"] = {
            "body": stored,
            "default": [{"id": "s-" + n, "name": n} for n in body["defaultClientScopes"]],
            "optional": [{"id": "s-" + n, "name": n} for n in body["optionalClientScopes"]],
            "mappers": [dict(m, id="mapper-uuid-1") for m in body["protocolMappers"]],
        }
        save()
        sys.exit(0)
    if cmd == "update":
        parts = args[1].split("/")
        if state.get("update_fail"):
            print("kcadm: simulated update failure", file=sys.stderr)
            sys.exit(1)
        if state.get("update_noop"):
            sys.exit(0)
        m = next(x for x in state["client"]["mappers"] if x["id"] == parts[-1])
        for i, a in enumerate(args):
            if a == "-s":
                mm = re.fullmatch(r'config\\."([^"]+)"="([^"]*)"', args[i + 1])
                assert mm, args[i + 1]
                m["config"][mm.group(1)] = mm.group(2)
        save()
        sys.exit(0)
    if cmd == "delete":
        sys.exit(0)
    sys.exit(0)
    """
    )
    .replace("__PY__", sys.executable)
    .replace("CLIENT_UUID", repr(CLIENT_UUID))
)


def _step_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index("SESSION_E2E_CLIENT_ID=")
    marker = 'deleted (read-back empty)"\n'
    end = text.index(marker, start) + len(marker)
    tail = text[end:]
    closing = tail[: tail.index("fi") + 2]
    closing += tail[len(closing) : tail.index("fi", len(closing)) + 2]
    return text[start : end + len(closing)]


def _client_state(
    config: dict[str, str],
    *,
    mapper_id: str = MAPPER_ID,
    protocol_mapper: str = "oidc-audience-mapper",
    **over: Any,
) -> dict[str, Any]:
    _raw, pinned = _pinned_json(SCRIPT)
    body = {
        k: v
        for k, v in pinned.items()
        if k not in ("defaultClientScopes", "optionalClientScopes", "protocolMappers")
    }
    client: dict[str, Any] = {
        "body": body,
        "default": [{"id": "s-" + n, "name": n} for n in pinned["defaultClientScopes"]],
        "optional": [
            {"id": "s-" + n, "name": n} for n in pinned["optionalClientScopes"]
        ],
        "mappers": [
            {
                "id": mapper_id,
                "name": "aud-audittrace-server",
                "protocol": "openid-connect",
                "protocolMapper": protocol_mapper,
                "config": dict(config),
            }
        ],
    }
    client.update(over)
    return client


class Run:
    def __init__(self, tmp: Path, site: Path) -> None:
        self.tmp = tmp
        self.site = site
        self.state_path = tmp / "state.json"
        self.log = tmp / "calls.log"
        self.created = tmp / "created.json"
        self.fake = tmp / "fake-kcadm"
        self.fake.write_text(FAKE_KCADM)
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)
        self.log.write_text("")

    def go(
        self,
        *,
        client: dict[str, Any] | None,
        step_text: str | None = None,
        **extra: Any,
    ) -> subprocess.CompletedProcess[str]:
        state = {
            "client": client,
            "realm_scopes": [
                {"id": "s-audittrace:query", "name": "audittrace:query"},
                {"id": "s-audittrace:audit", "name": "audittrace:audit"},
                {"id": "s-memory:session:write", "name": "memory:session:write"},
                {"id": "s-memory:session:read-own", "name": "memory:session:read-own"},
            ],
            **extra,
        }
        self.state_path.write_text(json.dumps(state))
        step = step_text if step_text is not None else _step_text(self.site)
        script = (
            "set -euo pipefail\n"
            + _preamble_text(self.site)
            + 'REALM="audittrace"\n'
            + f'acl_e2e_kc() {{ ACL_E2E_VIA_KC=1 "{self.fake}" "$@"; }}\n'
            + "declare -A SCOPE_ID\n"
            + "bind_scope() { :; }\n"
            + step
            + '\necho "STEP-COMPLETED"\n'
        )
        env = {
            **os.environ,
            "FAKE_STATE": str(self.state_path),
            "FAKE_LOG": str(self.log),
            "FAKE_CREATED": str(self.created),
            "AUDITTRACE_SESSION_E2E_CLIENT_ENABLED": "true",
        }
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60
        )

    def updates(self) -> list[str]:
        return [
            ln for ln in self.log.read_text().splitlines() if ln.startswith("update")
        ]

    def mapper_config(self) -> dict[str, str]:
        mappers = json.loads(self.state_path.read_text())["client"]["mappers"]
        return dict(mappers[0]["config"])


@pytest.fixture(params=[SCRIPT, CONFIGMAP], ids=["script", "configmap"])
def run(request: pytest.FixtureRequest, tmp_path: Path) -> Run:
    return Run(tmp_path, request.param)


def _raw_mapper(blocks: list[list[tuple[str, str]]]) -> str:
    """kcadm-style pretty text of ONE mapper whose `config` appears once per
    entry of `blocks` (a duplicated or re-shaped read), each block a list of
    (key, value) lines - duplicates allowed, unlike a JSON object."""
    out = [
        "[ {",
        f'  "id" : "{MAPPER_ID}",',
        '  "name" : "aud-audittrace-server",',
        '  "protocol" : "openid-connect",',
        '  "protocolMapper" : "oidc-audience-mapper",',
        '  "consentRequired" : false,',
    ]
    for lines in blocks:
        out.append('  "config" : {')
        out.extend(
            f'    "{k}" : "{v}"' + ("," if i < len(lines) - 1 else "")
            for i, (k, v) in enumerate(lines)
        )
        out.append("  },")
    out[-1] = "  }"
    out.append("} ]")
    return "\n".join(out)


def _without(key: str) -> dict[str, str]:
    return {k: v for k, v in PINNED_FIVE.items() if k != key}


class TestCreateAndVerify:
    def test_create_sends_all_five_keys(self, run: Run) -> None:
        proc = run.go(client=None)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        created = json.loads(run.created.read_text())
        assert created["protocolMappers"][0]["config"] == PINNED_FIVE
        assert run.updates() == []

    def test_verify_passes_with_exactly_the_five(self, run: Run) -> None:
        proc = run.go(client=_client_state(PINNED_FIVE))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "verified equal to the pinned block" in proc.stdout
        assert "reconciled" not in proc.stdout
        assert run.updates() == []

    def test_a_sixth_key_fails_closed_naming_the_key_only(self, run: Run) -> None:
        planted = "PLANTED-SIXTH-VALUE-9f3"
        proc = run.go(client=_client_state({**PINNED_FIVE, "sixth.key": planted}))
        assert proc.returncode != 0
        assert "code=drift" in proc.stdout
        assert "unexpected-mapper-config:count=1 keys=sixth.key" in proc.stdout
        assert planted not in proc.stdout + proc.stderr
        assert "STEP-COMPLETED" not in proc.stdout
        assert run.updates() == []

    @pytest.mark.parametrize("key", sorted(PINNED_FIVE))
    def test_a_wrong_value_for_each_of_the_five_fails_closed(
        self, run: Run, key: str
    ) -> None:
        """`update_noop` makes the (permitted) reconcile of the two defaulted
        keys ineffective, so the re-verify must still fail closed; for the
        three original keys no update is ever attempted."""
        planted = "PLANTED-WRONG-VALUE-7c1"
        proc = run.go(
            client=_client_state({**PINNED_FIVE, key: planted}), update_noop=True
        )
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert "code=drift" in proc.stdout
        assert f"keys={key}" in proc.stdout
        assert planted not in proc.stdout + proc.stderr
        assert "STEP-COMPLETED" not in proc.stdout
        expected = (
            1 if key in ("introspection.token.claim", "userinfo.token.claim") else 0
        )
        assert len(run.updates()) == expected

    def test_a_dropped_key_reports_the_count_of_five(self, run: Run) -> None:
        proc = run.go(client=_client_state(_without("id.token.claim")))
        assert proc.returncode != 0
        assert "mapper-config-count:4" in proc.stdout
        assert run.updates() == []


class TestNarrowReconcile:
    @pytest.mark.parametrize(
        ("config", "keys"),
        [
            (
                {
                    **PINNED_FIVE,
                    "introspection.token.claim": "false",
                    "userinfo.token.claim": "true",
                },
                "introspection.token.claim,userinfo.token.claim",
            ),
            (_without("introspection.token.claim"), "introspection.token.claim"),
            (
                {**PINNED_FIVE, "userinfo.token.claim": "true"},
                "userinfo.token.claim",
            ),
            (
                {
                    k: v
                    for k, v in PINNED_FIVE.items()
                    if k not in ("introspection.token.claim", "userinfo.token.claim")
                },
                "introspection.token.claim,userinfo.token.claim",
            ),
        ],
        ids=["both-wrong", "intro-absent", "userinfo-wrong", "both-absent"],
    )
    def test_the_two_key_shape_is_reconciled_once_then_reverified(
        self, run: Run, config: dict[str, str], keys: str
    ) -> None:
        proc = run.go(client=_client_state(config))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert (
            f"session-e2e: code=reconciled stage=mapper-config keys={keys}"
            in proc.stdout
        )
        updates = run.updates()
        assert len(updates) == 1
        assert (
            f"update clients/{CLIENT_UUID}/protocol-mappers/models/{MAPPER_ID}"
            in updates[0]
        )
        assert 'config."introspection.token.claim"="true"' in updates[0]
        assert 'config."userinfo.token.claim"="false"' in updates[0]
        assert run.mapper_config() == PINNED_FIVE
        assert "verified equal to the pinned block" in proc.stdout
        assert "STEP-COMPLETED" in proc.stdout

    def test_reconcile_logs_names_only_never_values(self, run: Run) -> None:
        v1, v2 = "PLANTED-INTRO-VALUE-4be", "PLANTED-USER-VALUE-8d0"
        proc = run.go(
            client=_client_state(
                {
                    **PINNED_FIVE,
                    "introspection.token.claim": v1,
                    "userinfo.token.claim": v2,
                }
            )
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "code=reconciled" in proc.stdout
        for stream in (proc.stdout, proc.stderr):
            assert v1 not in stream
            assert v2 not in stream

    @pytest.mark.parametrize(
        "key", ["included.custom.audience", "id.token.claim", "access.token.claim"]
    )
    def test_no_reconcile_when_an_original_key_differs(
        self, run: Run, key: str
    ) -> None:
        proc = run.go(
            client=_client_state(
                {**PINNED_FIVE, key: "other", "introspection.token.claim": "false"}
            )
        )
        assert proc.returncode != 0
        assert "code=drift" in proc.stdout
        assert "reconciled" not in proc.stdout
        assert run.updates() == []

    @pytest.mark.parametrize(
        "key", ["included.custom.audience", "id.token.claim", "access.token.claim"]
    )
    def test_no_reconcile_when_an_original_key_is_absent(
        self, run: Run, key: str
    ) -> None:
        proc = run.go(
            client=_client_state(
                {**_without(key), "introspection.token.claim": "false"}
            )
        )
        assert proc.returncode != 0
        assert "code=drift" in proc.stdout
        assert "reconciled" not in proc.stdout
        assert run.updates() == []

    def test_no_reconcile_when_an_unknown_key_is_present(self, run: Run) -> None:
        proc = run.go(
            client=_client_state(
                {**PINNED_FIVE, "unknown.key": "x", "userinfo.token.claim": "true"}
            )
        )
        assert proc.returncode != 0
        assert "code=drift" in proc.stdout
        assert "reconciled" not in proc.stdout
        assert run.updates() == []

    def test_no_reconcile_when_anything_else_drifted(self, run: Run) -> None:
        extra_scope = {"id": "s-offline_access", "name": "offline_access"}
        base = _client_state({**PINNED_FIVE, "userinfo.token.claim": "true"})
        base["default"] = base["default"] + [extra_scope]
        proc = run.go(client=base)
        assert proc.returncode != 0
        assert "unexpected-default-scope" in proc.stdout
        assert "reconciled" not in proc.stdout
        assert run.updates() == []

    def test_a_failing_reverify_fails_closed(self, run: Run) -> None:
        proc = run.go(
            client=_client_state({**PINNED_FIVE, "userinfo.token.claim": "true"}),
            update_noop=True,
        )
        assert proc.returncode != 0
        assert "code=reconciled" in proc.stdout
        assert "code=drift" in proc.stdout
        assert "STEP-COMPLETED" not in proc.stdout
        assert len(run.updates()) == 1

    def test_the_reverify_is_what_enforces_the_outcome(
        self, run: Run, tmp_path: Path
    ) -> None:
        """Neuter, in-test: skipping the re-read lets an ineffective update
        pass. The real step (previous test) fails; the mutated copy does not,
        so the re-verify is a real guard."""
        mutated = _skip_reverify(_step_text(run.site))
        proc = run.go(
            client=_client_state({**PINNED_FIVE, "userinfo.token.claim": "true"}),
            update_noop=True,
            step_text=mutated,
        )
        assert proc.returncode == 0, "the neuter should let the bad state through"


def _skip_reverify(step: str) -> str:
    marker = "SESSION_E2E_RECONCILED=1\n"
    i = step.index(marker) + len(marker)
    j = step.index("session_e2e_verify", i)
    k = step.index("\n", j)
    return step[:j] + "true" + step[k:]


class TestParity:
    def test_the_session_step_is_identical_in_script_and_configmap(self) -> None:
        script = _step_text(SCRIPT).splitlines()
        cm = _step_text(CONFIGMAP).splitlines()
        assert len(script) == len(cm)
        for a, b in zip(script, cm, strict=True):
            assert (a.strip() == b.strip()) and (b == a or b == "    " + a or not a)

    def test_the_acl_client_has_no_reconcile_path(self) -> None:
        for path in (SCRIPT, CONFIGMAP):
            text = path.read_text(encoding="utf-8")
            acl = text[
                text.index("acl_e2e_verify() {") : text.index("SESSION_E2E_CLIENT_ID=")
            ]
            assert "reconcile" not in acl, path.name
            assert "acl_e2e_kc update" not in acl, path.name


_TWO_KEY_SHAPE = {**PINNED_FIVE, "introspection.token.claim": "false"}


def _assert_closed_no_update(run: Run, proc: subprocess.CompletedProcess[str]) -> None:
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "STEP-COMPLETED" not in proc.stdout
    assert "code=reconciled" not in proc.stdout
    assert run.updates() == []


class TestReconcilePreconditionsAreEachEnforced:
    """r6 review B1-B5: each precondition and the failure paths, one test
    each, against BOTH sites. Every case is the Keycloak-24 two-key shape
    EXCEPT for the one property under test."""

    @pytest.mark.parametrize(
        "mapper_id",
        ["../x", "a/b", "../../uuid-session", ""],
        ids=lambda v: v or "empty",
    )
    def test_b1_a_bad_mapper_id_sends_no_update(self, run: Run, mapper_id: str) -> None:
        proc = run.go(client=_client_state(_TWO_KEY_SHAPE, mapper_id=mapper_id))
        _assert_closed_no_update(run, proc)
        assert "code=drift" in proc.stdout

    def test_b2_a_wrong_protocol_mapper_sends_no_update(self, run: Run) -> None:
        proc = run.go(
            client=_client_state(
                _TWO_KEY_SHAPE, protocol_mapper="oidc-hardcoded-claim-mapper"
            )
        )
        _assert_closed_no_update(run, proc)
        assert "missing-mapper-field:protocolMapper" in proc.stdout

    def test_b3_two_config_blocks_send_no_update(self, run: Run) -> None:
        first = [
            ("included.custom.audience", "audittrace-server"),
            ("id.token.claim", "false"),
            ("access.token.claim", "true"),
            ("introspection.token.claim", "false"),
        ]
        second = [("userinfo.token.claim", "false")]
        client = _client_state(_TWO_KEY_SHAPE)
        client["raw_mappers_text"] = _raw_mapper([first, second])
        proc = run.go(client=client)
        _assert_closed_no_update(run, proc)
        assert "mapper-config-absent" in proc.stdout

    def test_b4_a_failing_update_is_reconcile_failed_not_reconciled(
        self, run: Run
    ) -> None:
        proc = run.go(client=_client_state(_TWO_KEY_SHAPE), update_fail=True)
        assert proc.returncode != 0
        assert "code=reconcile-failed" in proc.stdout
        assert "code=reconciled" not in proc.stdout
        assert "STEP-COMPLETED" not in proc.stdout
        assert len(run.updates()) == 1

    @pytest.mark.parametrize(
        "dup", ["introspection.token.claim", "userinfo.token.claim"]
    )
    def test_b5_duplicated_key_lines_send_no_update_and_are_named(
        self, run: Run, dup: str
    ) -> None:
        """Five lines, one pinned key twice and the other absent: n == 5 and
        no unknown key, so ONLY the duplicate-key guards stand in the way of
        a reconcile (t_intro / t_user) and of a false 'verified equal'
        (mapper-config-duplicate-key)."""
        other = (
            "userinfo.token.claim"
            if dup == "introspection.token.claim"
            else "introspection.token.claim"
        )
        lines = [
            ("included.custom.audience", "audittrace-server"),
            ("id.token.claim", "false"),
            ("access.token.claim", "true"),
            (dup, PINNED_FIVE[dup]),
            (dup, PINNED_FIVE[dup]),
        ]
        assert other not in dict(lines)
        client = _client_state(_TWO_KEY_SHAPE)
        client["raw_mappers_text"] = _raw_mapper([lines])
        proc = run.go(client=client)
        _assert_closed_no_update(run, proc)
        assert "mapper-config-duplicate-key" in proc.stdout
        assert "verified equal" not in proc.stdout
