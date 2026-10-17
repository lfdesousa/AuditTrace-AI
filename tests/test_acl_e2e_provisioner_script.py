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

import ast
import contextlib
import json
import os
import re
import stat
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.test_chart_drift_guards import REPO_ROOT
from tests.test_chart_drift_guards_acl_write import (
    CONFIGMAP,
    E2E,
    PINNED_ATTRIBUTES,
    PINNED_FLAGS,
    PINNED_MAPPER,
    SCRIPT,
    _pinned_json,
    _preamble_text,
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
            elif c.get("raw_mappers_text"):
                print(c["raw_mappers_text"])
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
        self,
        *,
        clients: list[dict[str, Any]],
        enabled: str = "true",
        step_source: Path | None = None,
        **extra: Any,
    ) -> subprocess.CompletedProcess[str]:
        """`step_source`, if given, OVERRIDES which file's text the
        step/preamble are read from for this ONE call — ``self.site``
        (the real, TRACKED script/ConfigMap path) stays the default
        and is NEVER written to by this harness. R7 re-review #7
        (2026-10-08) SAFETY requirement: a test that wants to exercise
        a MUTATED step body must build that mutated text under
        `tmp_path` (see :func:`_mutated_copy_with_line_before_diffs_check`)
        and pass its path here — never write to a tracked file."""
        source = step_source if step_source is not None else self.site
        self.state_path.write_text(json.dumps({"clients": clients, **extra}))
        step = _step_text(source)
        # The real preamble (trap/emit/report/fail machinery), verbatim
        # from source — this harness exercises the EMITTED artefact for
        # the ACL-E2E step's drift/CRUD logic; a direct, non-background
        # fake `acl_e2e_kc` is enough here (the interruptible/timeout
        # mechanics have their own harness in
        # tests/test_acl_e2e_failure_diagnostics.py).
        preamble = _preamble_text(source)
        prelude = f'acl_e2e_kc() {{ ACL_E2E_VIA_KC=1 "{self.fake}" "$@"; }}\n'
        script = (
            "set -euo pipefail\n"
            + preamble
            + 'REALM="audittrace"\n'
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


_TRACKED_SITE_FILES = (SCRIPT, CONFIGMAP)


@pytest.fixture(scope="module", autouse=True)
def _tracked_site_files_never_mutated_by_this_module():  # type: ignore[no-untyped-def]
    """R7 re-review #7 (2026-10-08) SAFETY guard: assert the two
    shipped, TRACKED files this module's harness points at
    (``SCRIPT``/``CONFIGMAP``) are byte-identical BEFORE and AFTER
    every test in this module runs, and that `git` itself reports no
    change to either. This is the standing tripwire for the EXACT
    defect this round closes:
    ``_temporarily_inject_line_before_diffs_check`` (removed this
    round) used to write a leak line into these tracked files with
    only a `finally` to restore it — which a SIGKILL, OOM kill,
    watchdog kill, or APU hard-reset does not run, as re-review #7
    measured directly. Fails the whole module's teardown if either
    file differs from its state at collection time."""
    before = {p: p.read_bytes() for p in _TRACKED_SITE_FILES}
    yield
    for p in _TRACKED_SITE_FILES:
        after = p.read_bytes()
        assert after == before[p], (
            f"{p} was mutated during this test module's run — no test may "
            "write to a tracked script or chart file, even temporarily"
        )
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--", *(str(p) for p in _TRACKED_SITE_FILES)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    # R8 re-review #8 should-fix: the PRIOR version only checked
    # `stdout` — outside a git checkout (or on any other `git` failure)
    # `git status` exits non-zero with an EMPTY stdout, which the old
    # check could not distinguish from "confirmed clean". Checking the
    # return code closes that fail-open gap.
    assert proc.returncode == 0, (
        f"`git status` itself failed (exit {proc.returncode}) — this is "
        f"not a git checkout, or git errored; cannot confirm the tracked "
        f"site files are clean: {proc.stderr!r}"
    )
    assert proc.stdout.strip() == "", (
        f"tracked site files show as dirty in git after this module's test "
        f"run: {proc.stdout!r}"
    )


def _mutated_copy_with_line_before_diffs_check(
    path: Path, inserted_line: str, tmp_path: Path
) -> Path:
    """R7 re-review #7 (2026-10-08) SAFETY fix: the ROUND-6 version of
    this helper wrote `inserted_line` directly into the TRACKED,
    shipped file at `path` and restored it in a `finally` — re-review
    #7 measured that a SIGKILL sent mid-write left the shipped script
    PERMANENTLY mutated with a leak line (`finally` never runs on
    SIGKILL, an OOM kill, a watchdog kill, or an APU hard-reset, all
    of which this fleet has had). **No test may write a tracked
    script or chart path — ever, not even "temporarily".** This
    version never opens `path` for writing at all: it reads the real
    shipped text, computes the mutated text IN MEMORY, and writes it
    ONLY to a throwaway file under `tmp_path`. Callers pass the
    returned path to ``Run.go(..., step_source=...)``, which reads it
    instead of the real `path` for that one call."""
    original = path.read_text(encoding="utf-8")
    needle = 'if [[ -n "${diffs}" ]]; then\n'
    assert original.count(needle) == 1, f"{path}: expected exactly 1 occurrence"
    idx = original.index(needle)
    line_start = original.rfind("\n", 0, idx) + 1
    indent = original[line_start:idx]
    mutated = (
        original[:line_start] + indent + inserted_line + "\n" + original[line_start:]
    )
    copy_path = tmp_path / f"mutated-{path.name}"
    copy_path.write_text(mutated, encoding="utf-8")
    return copy_path


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
        # R4 (2026-10-07 re-review): detail= is a property, never the
        # response-derived mapper NAME itself.
        assert "unexpected-mapper:count=1" in proc.stderr
        assert "injected" not in proc.stderr
        assert "STEP-COMPLETED" not in proc.stdout


SPLIT_BLOCKS_RAW = """[
  {
    "id" : "m1",
    "name" : "aud-audittrace-server",
    "protocol" : "openid-connect",
    "protocolMapper" : "oidc-audience-mapper",
    "config" : {
      "included.custom.audience" : "audittrace-server",
      "id.token.claim" : "false"
    },
    "config" : {
      "access.token.claim" : "true"
    }
  }
]"""


class TestSplitConfigBlocks:
    """B1 (round-3 review): TWO ``config`` blocks that together hold exactly
    the three pinned pairs (3 pairs in total, every pair allowed, count 3)
    are not "exactly one config block" and must fail closed. Only the
    block-count guard can see it - weakening ``-ne 1`` to ``-lt 1`` (the
    reviewer's neuter) leaves every other provisioner test green."""

    def test_two_config_blocks_fail_closed_with_zero_mutation(self, run: Run) -> None:
        state = _pinned_client_state(raw_mappers_text=SPLIT_BLOCKS_RAW)
        proc = run.go(clients=[state])
        assert proc.returncode != 0
        assert "mapper-config-absent" in proc.stderr
        assert "unexpected-mapper-config" not in proc.stderr, (
            "the pairs themselves are all allowed: only the block count can catch this"
        )
        assert "mapper-config-count" not in proc.stderr, "count is 3 across blocks"
        assert "STEP-COMPLETED" not in proc.stdout
        assert run.mutating_calls() == []


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
    # R4 re-review #2 advisory: the response-derived VALUE each
    # `count=1`-rewritten needle used to carry, verbatim — asserted
    # ABSENT from both streams alongside the count-based needle, so
    # "detection is not weakened" is a measured property, not a claim.
    _R4_LEAKED_VALUES = {
        "extra optional scope (offline_access)": "offline_access",
        "extra default scope": "audittrace:admin",
        "SC-1 second mapper": "scope-injector",
        "SC-1 mapper config changed": "some-other-api",
        "N1 whitespace INSIDE a config value (audience)": "audittrace -server",
        "N1 trailing space in the mapper name": "aud-audittrace-server ",
    }

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
                "unexpected-optional-scope:count=1",  # R4: property, not the name
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
                "unexpected-default-scope:count=1",  # R4: property, not the name
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
                "unexpected-mapper:count=1",  # R4: property, not the name
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
                "unexpected-mapper-config:count=1",  # R4: property, not the value
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
                "N1 whitespace INSIDE a config value (audience)",
                {
                    "mappers": [
                        {
                            **PINNED_MAPPER,
                            "config": {
                                **PINNED_MAPPER["config"],
                                "included.custom.audience": "audittrace -server",
                            },
                        }
                    ]
                },
                "unexpected-mapper-config:count=1",  # R4: property, not the value
            ),
            (
                "N1 whitespace inside the claim flag value",
                {
                    "mappers": [
                        {
                            **PINNED_MAPPER,
                            "config": {
                                **PINNED_MAPPER["config"],
                                "access.token.claim": "tr ue",
                            },
                        }
                    ]
                },
                "unexpected-mapper-config:",
            ),
            (
                "N1 trailing space in the protocolMapper value",
                {
                    "mappers": [
                        {**PINNED_MAPPER, "protocolMapper": "oidc-audience-mapper "}
                    ]
                },
                "missing-mapper-field:protocolMapper",
            ),
            (
                "N1 trailing space in the mapper name",
                {"mappers": [{**PINNED_MAPPER, "name": "aud-audittrace-server "}]},
                "unexpected-mapper:count=1",  # R4: property, not the name
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
                "missing-mapper-field:protocolMapper",
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
        leaked = self._R4_LEAKED_VALUES.get(label)
        if leaked is not None:
            assert leaked not in proc.stdout, (label, proc.stdout)
            assert leaked not in proc.stderr, (label, proc.stderr)

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


class TestDiffsRuntimeAttributionOfTheClientUuid:
    """R6/R7 (2026-10-08, re-review #7 correction): the static
    provenance check in ``tests/test_acl_e2e_failure_diagnostics.py``
    (``_detail_allowlist_names_only_bind_to_literals``) does not read
    `diffs`'s own assignments at all — a `local diffs=$x` rebind or a
    `diffs+=" x:${name}"` append of a RESPONSE-DERIVED value is
    invisible to it. The 6 existing drift tests in
    `TestEnabledPresent` close that gap for a response-derived
    SCOPE/MAPPER NAME (the fixtures plant one and assert it absent
    from `diffs`'s own output).

    **The REAL guard for the client's own response-derived UUID (a
    different value, not a name) is
    ``test_without_the_neuter_the_client_uuid_does_not_leak`` — it
    reads the shipped script AS-IS (no override) and asserts the
    secret-shaped uuid is ABSENT; re-review #7 measured it turn RED
    under a genuine production-level neuter (the rebind/append edited
    directly into the real script), which is exactly what a guard
    must do.**

    The other two tests below
    (``test_diffs_rebind_with_the_client_uuid_is_reachable_demonstration``
    / ``test_diffs_append_with_the_client_uuid_is_reachable_demonstration``)
    are NOT guards — they inject the leak THEMSELVES (into a throwaway
    copy, never the tracked file) and assert it appears; they pass
    whether or not the SHIPPED production script leaks anything, so
    they cannot regress-test production. Re-review #7 measured this
    directly: a real production-level neuter leaves them exactly as
    GREEN as an unneutered tree. Their only value is demonstrating
    that the shape IS reachable if it existed — i.e., that the
    control test's RED, were it to happen, would mean something real,
    not an artefact of the harness being unable to print a leak at
    all. No OTHER response-derived value shape is claimed covered by
    this class."""

    # 32-char opaque, non-JWT — one of the shapes re-review #3/#4
    # measured as beating a JWT-only mask; reused here for the same
    # reason (a long JWT would also be caught by unrelated hygiene
    # checks elsewhere, which would make this test's own RED
    # ambiguous about which guard actually fired).
    _SECRET_UUID = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"

    def test_diffs_rebind_with_the_client_uuid_is_reachable_demonstration(
        self, run: Run, tmp_path: Path
    ) -> None:
        """Demonstration only — see the class docstring. Builds a
        MUTATED COPY under `tmp_path` (never writes the tracked
        file) and points `Run` at it for this one call via
        `step_source`."""
        mutated = _mutated_copy_with_line_before_diffs_check(
            run.site, 'diffs=" id:${uuid}${diffs}"', tmp_path
        )
        proc = run.go(
            clients=[_pinned_client_state(id=self._SECRET_UUID, consentRequired=False)],
            step_source=mutated,
        )
        assert self._SECRET_UUID in proc.stdout or self._SECRET_UUID in proc.stderr, (
            proc.stdout,
            proc.stderr,
        )

    def test_diffs_append_with_the_client_uuid_is_reachable_demonstration(
        self, run: Run, tmp_path: Path
    ) -> None:
        """Demonstration only — see the class docstring. Builds a
        MUTATED COPY under `tmp_path` (never writes the tracked
        file) and points `Run` at it for this one call via
        `step_source`."""
        mutated = _mutated_copy_with_line_before_diffs_check(
            run.site, 'diffs+=" id:${uuid}"', tmp_path
        )
        proc = run.go(
            clients=[_pinned_client_state(id=self._SECRET_UUID, consentRequired=False)],
            step_source=mutated,
        )
        assert self._SECRET_UUID in proc.stdout or self._SECRET_UUID in proc.stderr, (
            proc.stdout,
            proc.stderr,
        )

    def test_without_the_neuter_the_client_uuid_does_not_leak(self, run: Run) -> None:
        """THE GUARD (see the class docstring): the SAME secret-shaped
        uuid, the SAME genuine drift, reading the shipped script
        AS-IS (no override) — it must not leak the uuid. Re-review #7
        measured this test alone turn RED when the rebind/append was
        edited directly into the real production script; the two
        demonstration tests above stayed GREEN either way."""
        proc = run.go(
            clients=[_pinned_client_state(id=self._SECRET_UUID, consentRequired=False)]
        )
        assert self._SECRET_UUID not in proc.stdout
        assert self._SECRET_UUID not in proc.stderr


_WRITE_TEXT_CALL_RE = re.compile(r"\.write_text\s*\(")


def _ast_functions_writing_a_tracked_path(
    tree: ast.AST, tracked_names: frozenset[str]
) -> list[str]:
    """R7/R8 (re-review #8, 2026-10-08, measured this a HEURISTIC, not
    a proof — see the orchestrator ruling in the round-8 build
    record): walk every function definition in `tree`; for each
    ``X.write_text(...)`` call where `X` is a bare name, collect every
    `X = <expr>` assignment ANYWHERE in that same function and render
    each RHS via :func:`ast.unparse`. If any such RHS — or the
    receiver expression itself, for a non-`Name` receiver like
    `SCRIPT.write_text(...)` directly — textually contains one of
    `tracked_names` (``SCRIPT``/``CONFIGMAP``, the module-level
    constants for the two shipped, tracked files), report it.

    Measured KNOWN GAPS (re-review #8, all GREEN — none of these are
    caught by this function): a write through a FUNCTION PARAMETER
    fed from `SCRIPT`/`CONFIGMAP`/`run.site` by a CALLER elsewhere
    (the exact shape of the real round-6 defect — this check only
    traces same-function `X = …` assignments, never a parameter's own
    call-site argument); `open(path, "w"/"a"/"r+")`; `.write_bytes(...)`;
    `shutil.copy*`/`os.replace` onto the tracked path. **This function
    is NOT the safety proof** — the session-scoped `chmod a-w` fixture
    in `tests/conftest.py`
    (`_acl_wu2c_tracked_files_are_read_only_for_the_whole_session`) is.
    MEASURED (re-review #9, 2026-10-11): that fixture stops every
    IN-PLACE open-for-write shape — `open(path, "w"/"a"/"r+")`,
    `.write_text(...)`, `.write_bytes(...)`, a shell `>`/`>>` — with
    `PermissionError`. It does NOT stop `os.replace`/`shutil.move`
    (rename/replace onto the path), a `sed -i` subprocess,
    unlink-and-recreate, or chmod-then-write — the directory stays
    writable under the lock, so those succeed; see that fixture's own
    module comment for the measured gap list. This AST check remains
    only as a cheap, fast, STATIC pre-commit-time signal for the ONE
    shape it was originally built to catch — a direct, same-function
    `NAME = SCRIPT; NAME.write_text(...)` assignment."""
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        rhs_by_name: dict[str, list[str]] = {}
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Assign)
                and len(sub.targets) == 1
                and isinstance(sub.targets[0], ast.Name)
            ):
                rhs_by_name.setdefault(sub.targets[0].id, []).append(
                    ast.unparse(sub.value)
                )
        for sub in ast.walk(node):
            if not (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "write_text"
            ):
                continue
            receiver = sub.func.value
            candidates = [ast.unparse(receiver)]
            if isinstance(receiver, ast.Name):
                candidates.extend(rhs_by_name.get(receiver.id, []))
            if any(
                any(name in candidate for name in tracked_names)
                for candidate in candidates
            ):
                violations.append(f"{node.name}: {ast.unparse(sub)}")
    return violations


def test_no_test_in_this_repo_writes_to_a_tracked_script_or_chart_path() -> None:
    """R7/R8, a cheap STATIC heuristic, not the safety proof (re-review
    #8 measured this — see
    :func:`_ast_functions_writing_a_tracked_path`'s own docstring for
    the full, measured gap list; the actual proof is the `chmod a-w`
    runtime fixture in `tests/conftest.py`). Scans every `tests/*.py`
    file's AST for a `.write_text(...)` call that traces — directly,
    or through a same-function variable assignment — to the
    `SCRIPT`/`CONFIGMAP` constants. See
    :func:`test_ast_guard_catches_the_old_vulnerable_write_shape` for
    this check's own falsifiability proof (of the ONE shape it
    catches), and
    :func:`test_real_round6_helper_now_raises_permissionerror_via_the_tracked_file_lock`
    for the proof that actually matters: the real round-6 defect,
    verbatim, fails at RUNTIME regardless of what this static check
    can or cannot see."""
    offenders = []
    for test_file in sorted((REPO_ROOT / "tests").glob("*.py")):
        text = test_file.read_text(encoding="utf-8")
        if not _WRITE_TEXT_CALL_RE.search(text):
            continue  # cheap pre-filter; ast.parse every file regardless below
        tree = ast.parse(text, filename=str(test_file))
        for v in _ast_functions_writing_a_tracked_path(
            tree, frozenset({"SCRIPT", "CONFIGMAP"})
        ):
            offenders.append(f"{test_file.name}: {v}")
    assert offenders == [], offenders


def test_ast_guard_catches_the_old_vulnerable_write_shape() -> None:
    """Falsifiability of the ONE shape this static heuristic covers: a
    SYNTHETIC function with a DIRECT, same-function assignment
    (`path = SCRIPT`) followed by `path.write_text(...)`. **This is
    NOT shaped like the real round-6 defect** — re-review #8 measured
    that the real defect (`path` as a FUNCTION PARAMETER, fed
    `run.site` by a caller elsewhere) escapes this exact check (see
    the gap list in :func:`_ast_functions_writing_a_tracked_path`'s
    docstring). This test only proves the narrower, same-function-
    assignment shape is not vacuous; it proves nothing about the real
    defect — that proof is
    :func:`test_real_round6_helper_now_raises_permissionerror_via_the_tracked_file_lock`."""
    source = (
        "def _old_vulnerable_helper(inserted_line):\n"
        "    path = SCRIPT\n"
        "    original = path.read_text()\n"
        "    mutated = original + inserted_line\n"
        "    path.write_text(mutated)\n"
    )
    tree = ast.parse(source)
    violations = _ast_functions_writing_a_tracked_path(
        tree, frozenset({"SCRIPT", "CONFIGMAP"})
    )
    assert violations, (
        "the AST guard failed to catch path.write_text() traced to SCRIPT"
    )


def test_ast_guard_does_not_flag_a_tmp_path_derived_write() -> None:
    """Negative control: the CURRENT, fixed shape
    (`copy_path.write_text(...)` where `copy_path` is derived from
    `tmp_path`, never from `SCRIPT`/`CONFIGMAP`) must NOT be flagged —
    otherwise the guard would be too broad to ever pass."""
    source = (
        "def _mutated_copy_with_line_before_diffs_check(path, inserted_line, tmp_path):\n"
        "    original = path.read_text()\n"
        "    mutated = original + inserted_line\n"
        "    copy_path = tmp_path / f'mutated-{path.name}'\n"
        "    copy_path.write_text(mutated)\n"
        "    return copy_path\n"
    )
    tree = ast.parse(source)
    violations = _ast_functions_writing_a_tracked_path(
        tree, frozenset({"SCRIPT", "CONFIGMAP"})
    )
    assert violations == [], violations


# ───── R8 re-review #8 (2026-10-08): the REAL runtime proof ─────
# The `chmod a-w` session fixture in `tests/conftest.py`
# (`_acl_wu2c_tracked_files_are_read_only_for_the_whole_session`) is
# the stronger of the two checks, not the AST heuristic above. These
# 3 tests prove it catches every IN-PLACE open-for-write shape
# re-review #8 measured as GREEN against the AST check (a function
# parameter fed `run.site`, `open(path, "w")`, `.write_bytes(...)`),
# PLUS the real round-6 defect verbatim. MEASURED (re-review #9,
# 2026-10-11): it stops those because each opens the EXISTING
# directory entry; it does NOT stop a write that replaces or
# recreates the entry instead (`os.replace`/`shutil.move`, `sed -i`,
# unlink-and-recreate, chmod-then-write) — see the fixture's own
# module comment in `tests/conftest.py` for the measured gap list.


# The EXACT, VERBATIM round-6 defect (commit 426fbab) — reintroduced
# ONLY as a falsifiability fixture for this proof, never called by any
# other test or production code. `path` is a FUNCTION PARAMETER, fed
# `run.site` by the caller below — the precise shape that escaped the
# AST heuristic (re-review #8's N1).
@contextlib.contextmanager
def _real_round6_vulnerable_helper_verbatim_from_426fbab(
    path: Path, inserted_line: str
) -> Iterator[None]:
    """R6 re-review #6 (2026-10-08): temporarily insert `inserted_line`
    immediately before the REAL, single `if [[ -n "${diffs}" ]]; then`
    check in `acl_e2e_verify` (matching that line's own indentation —
    the standalone script and the YAML-block-scalar-indented ConfigMap
    copy differ), for the duration of the `with` block. ``Run.go``
    re-reads the step text from `path` on disk on every call, so this
    is enough to exercise the REAL shipped mechanism, mutated, through
    the REAL harness. Restores the ORIGINAL text byte-for-byte on exit
    (even on test failure, via ``finally``) — never `git checkout`."""
    original = path.read_text(encoding="utf-8")
    needle = 'if [[ -n "${diffs}" ]]; then\n'
    assert original.count(needle) == 1, f"{path}: expected exactly 1 occurrence"
    idx = original.index(needle)
    line_start = original.rfind("\n", 0, idx) + 1
    indent = original[line_start:idx]
    mutated = (
        original[:line_start] + indent + inserted_line + "\n" + original[line_start:]
    )
    try:
        path.write_text(mutated, encoding="utf-8")
        yield
    finally:
        path.write_text(original, encoding="utf-8")
        assert path.read_text(encoding="utf-8") == original, f"RESTORE FAILED: {path}"


def test_real_round6_helper_now_raises_permissionerror_via_the_tracked_file_lock(
    run: Run,
) -> None:
    """THE real falsifiability proof (re-review #8's required neuter):
    calling the REAL, VERBATIM round-6 helper with `run.site` — the
    exact shape that escaped the AST heuristic — must now raise
    `PermissionError`, because the session-scoped `chmod a-w` fixture
    in `tests/conftest.py` makes the tracked file un-writable for
    this one shape: an in-place `.write_text(...)` call that opens the
    EXISTING directory entry for writing (re-review #9, 2026-10-11 —
    the lock does NOT stop a rename/replace, `sed -i`, unlink-and-
    recreate, or chmod-then-write; see that fixture's module comment).
    If this test ever fails (no exception, or the wrong exception),
    the tracked-file write protection itself is broken."""
    with pytest.raises(PermissionError):
        with _real_round6_vulnerable_helper_verbatim_from_426fbab(
            run.site, 'diffs=" id:${uuid}${diffs}"'
        ):
            pass


def test_open_write_mode_on_a_tracked_path_now_raises_permissionerror(
    run: Run,
) -> None:
    """A shape the AST heuristic never even attempted to catch:
    `open(path, "w")` directly. Must raise `PermissionError` via the
    same runtime lock."""
    with pytest.raises(PermissionError):
        open(run.site, "w", encoding="utf-8")  # noqa: SIM115


def test_write_bytes_on_a_tracked_path_now_raises_permissionerror(
    run: Run,
) -> None:
    """Another shape the AST heuristic never caught: `.write_bytes(...)`
    directly. Must raise `PermissionError` via the same runtime lock."""
    with pytest.raises(PermissionError):
        run.site.write_bytes(b"leak")
