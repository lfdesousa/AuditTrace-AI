"""The ACL WU-2c-A0 "provisioner diagnostics" mechanism (named failure
line on every exit path, the kcadm interruptible wrapper, read-only
shape capture) in BOTH sites — ``scripts/setup-memory-scopes.sh`` and
the chart's in-cluster ConfigMap script.

Contract: ACL WU-2c Addendum F (provisioner diagnostics) + Addenda
F1/F2/F3, folding the Addendum-F3 gate verdict's SF3-1..6.

What this file proves, per site:

* ``TestNamedFailureLineOnEveryExit`` — F-2a..k: every injected failure
  shape (a guarded ``acl_e2e_fail``, an unguarded command under
  ``set -e``, a kcadm timeout, the ``-u`` path) emits exactly one
  ``acl-e2e:`` line on stdout AND stderr, with the predicted
  ``code=``/``stage=``.
* ``TestSignalTermDuringLookup`` — F-2l: a real SIGTERM sent to the
  running bash process while a (fake) kcadm call hangs produces the
  named ``signal-term`` line, exit 143, and zero mutating calls.
* ``TestSubshellNestingSingleEmitter`` — F2 BF1-b: the 7 sites where
  ``acl_e2e_fail`` is reached inside a ``$(...)`` command substitution
  still print exactly once per stream, carrying the INNER (more
  specific) code.
* ``TestDynamicKcadmWrapperMarker`` — F3 §1.1 primary proof: a bare
  (unwrapped) kcadm invocation on an executed path fails loudly
  (``code=unwrapped-call``), never silently.
* ``TestStaticKcadmPrefilterPin`` — F3 §1.2 / gate SF3-1 secondary
  proof: the shape-complete, quote-state-tokenizing static pin (0 real
  invocations outside the wrapper block on a correct build; every
  planted neuter shape, including the ones that beat F2's withdrawn
  regex, reddens it).
* ``TestAclE2eFailPlacementPin`` — AC-F14 / gate S2-5: every
  ``acl_e2e_fail`` call sits where the subshell-aware emitter's
  invariant holds (never after ``|``, never inside ``( )``).
* ``TestSecretHygiene`` — AC-F2 amended by SF3-5: no echo/printf
  expansion of the admin password or ``$@``/``$*`` outside
  ``acl_e2e_fail``'s fixed format; ``set -x``/``printenv``/
  ``declare -p``/``cat <<<`` absent; a planted-secret runtime probe.
* ``TestShapeCapturePrinter`` — AC-F3 / F3 S2-6: the redaction
  allowlist (incl. trailing commas, inline arrays, bare array
  elements), fails closed on an unmatched line, GET-only.
* ``TestRunFullWholeScriptCoverage`` — SF3-3/SF3-4: the WHOLE script
  (not just the ACL-E2E step) run under the fake in two states
  (all-absent / all-present); coverage of every real invocation line
  is MEASURED (``ACL_E2E_CALL_LINE``), not asserted, and the build
  record lists what is unreached and why.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from tests._hermetic_env import hermetic_env
from tests.test_chart_drift_guards_acl_write import (
    CONFIGMAP,
    SCRIPT,
    _preamble_text,
    _real_function_text,
    _strip_wrapper_blocks,
    _wrapper_block,
)

# Resolved once, against the test runner's OWN ambient PATH — the
# behavioral tool-check tests below pass a deliberately tool-starved
# `env=` to the child, under which `subprocess.run(["bash", ...])`
# would fail its own PATH lookup for `bash` itself before ever
# reaching the script's `command -v timeout`/`command -v mktemp`.
_BASH = shutil.which("bash") or "/usr/bin/bash"


def _reset_sigint_to_default() -> None:
    """``preexec_fn`` for every ``Popen`` this file sends a real signal
    to. Root cause (corrected, 2026-10-07 re-review — the first fix
    round's "some other test module" theory was FALSE as measured):
    launching ``make test`` itself in the BACKGROUND (``nohup make
    test > file 2>&1 &``, this round's own invocation) is a POSIX
    asynchronous list (``cmd &``) — the shell sets SIGINT (and SIGQUIT)
    to ``SIG_IGN`` for an async list's commands, inherited down through
    every descendant process, INCLUDING the pytest process and every
    ``bash -c`` child it later spawns. A FOREGROUND ``--collect-only``
    of the full 7890-test suite measured SIGINT still at
    ``default_int_handler`` — no other module is implicated. A
    NON-interactive shell that inherits SIGINT already-ignored at
    invocation can never override it via its own `trap` (the same rule
    that keeps a backgrounded job immune to its parent terminal's
    Ctrl-C), so the production script's `trap ... INT` was silently
    ineffective whenever this test's `bash -c` child was spawned from
    a background-launched `make test`. Measured directly: a child
    `Popen`'d with SIGINT pre-ignored in the parent runs a `trap ...
    INT` script to completion, untrapped; the SAME script, `Popen`'d
    with this `preexec_fn`, traps correctly (rc 130) regardless of the
    parent's disposition — the FIX is sound and launch-independent
    even though the original write-up of WHY it was needed was wrong.
    SIGTERM is NOT subject to this POSIX carve-out (only SIGINT/SIGQUIT
    are), which is exactly why the SIGTERM sibling tests never showed
    this failure."""
    signal.signal(signal.SIGINT, signal.SIG_DFL)


# ─────────────────────────────────────────────────────────────────────
# The fake kcadm / kubectl — a general stateful simulator covering every
# kcadm subcommand both sites issue (client-scopes, clients, bindings,
# protocol-mappers, management/permissions, authz policy), PLUS the
# F3 §1.1 dynamic marker check and pattern-based failure/hang injection.
# ─────────────────────────────────────────────────────────────────────

FAKE_KCADM = textwrap.dedent(
    '''\
    #!__PY__
    """General stateful fake of every kcadm subcommand the provisioner
    issues, plus the F3 dynamic ACL_E2E_VIA_KC marker check and
    pattern-based failure/hang injection (FAKE_FAIL_MATCH)."""
    import json, os, sys, time, uuid

    STATE = os.environ["FAKE_STATE"]
    LOG = os.environ["FAKE_LOG"]
    args = sys.argv[1:]
    joined = " ".join(args)

    # Addendum F4: the proof RECORDS every call; it does not rely on an
    # exit code a tolerant call site (`2>/dev/null ... || true`) could
    # swallow. This line is written UNCONDITIONALLY, before any exit
    # decision, to a harness-owned log the production script never
    # learns the path of (the env var is absent in production).
    marker_log = os.environ.get("FAKE_MARKER_LOG")
    marker_state = "present" if os.environ.get("ACL_E2E_VIA_KC") == "1" else "absent"
    if marker_log:
        with open(marker_log, "a") as fh:
            fh.write(
                "marker=" + marker_state
                + " line=" + os.environ.get("ACL_E2E_CALL_LINE", "?")
                + " argv1=" + (args[0] if args else "")
                + "\\n"
            )

    # F3 §1.1 — the fake MAY still exit 97 when the marker is absent;
    # per F4 point 1, that exit code is NOT itself the proof (a
    # tolerant call site can swallow it) — the marker LOG above is.
    if marker_state == "absent":
        sys.stderr.write("fake-kcadm: unwrapped call\\n")
        sys.exit(97)

    with open(LOG, "a") as fh:
        fh.write(joined + " || line=" + os.environ.get("ACL_E2E_CALL_LINE", "?") + "\\n")

    state = json.load(open(STATE))

    def save():
        json.dump(state, open(STATE, "w"))

    def flag(name):
        return args[args.index(name) + 1] if name in args else None

    # Pattern-based failure/hang injection — checked on EVERY call,
    # before any real logic, so a single state dict can target exactly
    # one call (by substring, with an occurrence counter) without
    # needing to fake every surrounding call's shape.
    matched = []
    for rule in state.get("fail_match", []):
        if rule["substr"] in joined:
            matched.append(rule)
    for rule in matched:
        key = "_n_" + rule["substr"]
        state[key] = state.get(key, 0) + 1
        if state[key] == rule.get("nth", 1):
            save()
            if rule.get("hang_seconds"):
                marker = os.environ.get("FAKE_HANG_MARKER")
                if marker:
                    open(marker, "w").close()
                time.sleep(rule["hang_seconds"])
            if rule.get("stdout"):
                sys.stdout.write(rule["stdout"] + "\\n")
            if rule.get("stderr"):
                sys.stderr.write(rule["stderr"] + "\\n")
            sys.exit(rule.get("rc", 1))
    save()

    def pretty(obj):
        return json.dumps(obj, indent=2, separators=(",", " : "))

    def find_client(client_id=None, uid=None):
        for c in state["clients"]:
            if client_id is not None and c["clientId"] == client_id:
                return c
            if uid is not None and c["id"] == uid:
                return c
        return None

    cmd = args[0]

    if cmd == "get" and args[1] == "client-scopes":
        for s in state.setdefault("scopes", []):
            print(s["id"] + "," + s["name"])
        sys.exit(0)

    if cmd == "create" and args[1] == "client-scopes":
        if "-f" in args:
            body = json.load(sys.stdin)
            name = body["name"]
        else:
            name = flag("-s").split("=", 1)[1] if False else None
            for a in args:
                if a.startswith("name="):
                    name = a.split("=", 1)[1]
        sid = "scope-" + str(len(state.setdefault("scopes", [])) + 1)
        state["scopes"].append({"id": sid, "name": name})
        save()
        sys.exit(0)

    if cmd == "get" and args[1] == "clients":
        want = None
        for a in args:
            if a.startswith("clientId="):
                want = a.split("=", 1)[1]
        fields = flag("--fields")
        for c in state["clients"]:
            if want is not None and c["clientId"] != want:
                continue
            if fields == "id,clientId":
                print(c["id"] + "," + c["clientId"])
            else:
                print(c["id"])
        sys.exit(0)

    if cmd == "create" and args[1] == "clients":
        body = json.load(sys.stdin)
        uid = "client-" + str(len(state["clients"]) + 1)
        default_scopes = body.pop("defaultClientScopes", [])
        optional_scopes = body.pop("optionalClientScopes", [])
        mappers = body.pop("protocolMappers", [])
        state["clients"].append({
            "id": uid, "clientId": body["clientId"], "body": body,
            "default": [{"id": "s-" + n, "name": n} for n in default_scopes],
            "optional": [{"id": "s-" + n, "name": n} for n in optional_scopes],
            "mappers": mappers,
        })
        save()
        sys.exit(0)

    if cmd == "delete":
        parts = args[1].split("/")
        if len(parts) == 2:
            state["clients"] = [c for c in state["clients"] if c["id"] != parts[1]]
        else:
            key = "default" if parts[2].startswith("default") else "optional"
            c = find_client(uid=parts[1])
            if c is not None:
                c[key] = [s for s in c[key] if s["id"] != parts[3]]
        save()
        sys.exit(0)

    if cmd == "update" and len(args[1].split("/")) >= 3 and (
        "default-client-scopes" in args[1] or "optional-client-scopes" in args[1]
    ):
        parts = args[1].split("/")
        key = "default" if "default-client-scopes" in args[1] else "optional"
        c = find_client(uid=parts[1])
        scope_name = parts[3]
        for s in state.get("scopes", []):
            if s["id"] == parts[3]:
                scope_name = s["name"]
                break
        if c is not None and not any(s["name"] == scope_name for s in c[key]):
            c[key].append({"id": parts[3], "name": scope_name})
        save()
        sys.exit(0)

    if cmd == "get" and args[1].startswith("clients/") and (
        "protocol-mappers/models" not in args[1]
        and "management/permissions" not in args[1]
        and "authz/resource-server/policy" not in args[1]
        and "default-client-scopes" not in args[1]
        and "optional-client-scopes" not in args[1]
    ):
        # Single client's own body (acl_e2e_compact) — no sub-path.
        parts = args[1].split("/")
        c = find_client(uid=parts[1])
        if c is None:
            sys.exit(1)
        print(pretty(c["body"]))
        sys.exit(0)

    if cmd == "get" and args[1].startswith("clients/") and (
        "default-client-scopes" in args[1] or "optional-client-scopes" in args[1]
    ):
        parts = args[1].split("/")
        key = "default" if "default-client-scopes" in args[1] else "optional"
        c = find_client(uid=parts[1])
        items = c[key] if c is not None else []
        fields = flag("--fields")
        for s in items:
            if fields == "id,name":
                print(s["id"] + "," + s["name"])
            else:
                print(s["name"])
        sys.exit(0)

    if cmd == "get" and "protocol-mappers/models" in args[1]:
        parts = args[1].split("/")
        c = find_client(uid=parts[1]) or {"mappers": []}
        if "--fields" in args:
            for m in c.get("mappers", []):
                print(m["name"])
        else:
            print(pretty(c.get("mappers", [])))
        sys.exit(0)

    if cmd == "create" and "protocol-mappers/models" in args[1]:
        sys.exit(0)

    if cmd == "update" and "management/permissions" in args[1]:
        sys.exit(0)

    if cmd == "get" and "management/permissions" in args[1]:
        print('{\\n  "resource" : "res-1",\\n  "scope" : "scope-1",\\n  "token-exchange" : "perm-1"\\n}')
        sys.exit(0)

    if cmd == "get" and "authz/resource-server/policy" in args[1]:
        for p in state.setdefault("policies", []):
            print(p["id"] + "," + p["name"])
        sys.exit(0)

    if cmd == "create" and "authz/resource-server/policy/client" in args[1]:
        name = None
        for a in args:
            if a.startswith("name="):
                name = a.split("=", 1)[1]
        state.setdefault("policies", []).append(
            {"id": "policy-" + str(len(state["policies"]) + 1), "name": name}
        )
        save()
        sys.exit(0)

    if cmd == "update" and "authz/resource-server/permission/scope" in args[1]:
        sys.exit(0)

    if cmd == "update" and len(args[1].split("/")) == 2:
        # Step 6 — plain client-field update (redirectUris/webOrigins).
        sys.exit(0)

    if cmd == "config":
        sys.exit(0)

    sys.exit(0)
    '''
).replace("__PY__", sys.executable)

_SECRET_RE = re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}")


def _assert_no_secret_shape(*streams: str) -> None:
    for s in streams:
        assert not _SECRET_RE.search(s)
        assert "super-secret-password" not in s


def _acl_e2e_line_count(stream: str) -> int:
    """Count only LINES that start with the real marker — "acl-e2e:"
    is also a substring of the client id "audittrace-acl-e2e:" inside
    unrelated human-readable progress lines, so a bare substring count
    over-counts."""
    return sum(1 for ln in stream.splitlines() if ln.startswith("acl-e2e:"))


def _assert_exactly_one_line(proc: subprocess.CompletedProcess[str]) -> None:
    assert _acl_e2e_line_count(proc.stdout) == 1, proc.stdout
    assert _acl_e2e_line_count(proc.stderr) == 1, proc.stderr


_SITES = pytest.mark.parametrize(
    "site", [SCRIPT, CONFIGMAP], ids=["script", "configmap"]
)


def _base_clients() -> list[dict[str, Any]]:
    """Every client the whole script (both sites) looks up but never
    creates — must always be present for Steps 2-6 to proceed."""
    names = [
        "admin-client",
        "audittrace-opencode",
        "audittrace-webui",
        "audittrace-librechat",
        "audittrace-librechat-bff",
        "realm-management",
    ]
    return [
        {
            "id": f"preexisting-{i}",
            "clientId": n,
            "body": {"clientId": n},
            "default": [],
            "optional": [],
            "mappers": [],
        }
        for i, n in enumerate(names)
    ]


class StepHarness:
    """Runs ONE site's ACL-E2E step (same substring window the existing
    drift-logic harness uses) under the general fake, with FAIL_MATCH /
    hang injection and a REAL `acl_e2e_kc` wrapper (background + wait,
    so SIGTERM injection is meaningful)."""

    def __init__(self, tmp: Path, site: Path) -> None:
        self.tmp = tmp
        self.site = site
        self.state_path = tmp / "state.json"
        self.log = tmp / "calls.log"
        self.marker_log = tmp / "marker.log"
        self.fake = tmp / "fake-kcadm"
        self.fake.write_text(FAKE_KCADM)
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)
        self.log.write_text("")
        self.marker_log.write_text("")

    @staticmethod
    def _step_text(path: Path) -> str:
        text = path.read_text(encoding="utf-8")
        start = text.index("ACL_E2E_CLIENT_ID=")
        end_marker = 'deleted (read-back empty)"\n'
        end = text.index(end_marker) + len(end_marker)
        tail = text[end:]
        closing = tail[: tail.index("fi") + 2]
        closing += tail[len(closing) : tail.index("fi", len(closing)) + 2]
        return text[start : end + len(closing)]

    def _script_text(self, *, clients, enabled, extra_prelude="", **extra) -> str:
        self.state_path.write_text(
            json.dumps({"clients": clients, "scopes": [], **extra})
        )
        preamble = _preamble_text(self.site)
        step = self._step_text(self.site)
        wrapper = (
            _wrapper_block(self.site)
            .replace(
                'kubectl -n "${NAMESPACE}" exec -i "${KC_POD}" -c keycloak -- '
                "/opt/keycloak/bin/kcadm.sh",
                f'"{self.fake}"',
            )
            .replace("/opt/keycloak/bin/kcadm.sh", f'"{self.fake}"')
        )
        # F1 SF-3 / R2: the REAL production `find_client_id`/`bind_scope`
        # (verbatim from source — the ACL-E2E step's own bind loop calls
        # `bind_scope`, which goes through `acl_e2e_kc` like every other
        # call), never a bare-call stub that would silently bypass the
        # dynamic marker proof. `bind_scope` reads `SCOPE_ID[$SCOPE]`
        # (normally populated by Step 1); the two ACL scopes it is ever
        # called with here are pre-seeded.
        find_client_id = _real_function_text(self.site, "find_client_id")
        bind_scope = _real_function_text(self.site, "bind_scope")
        scope_id_seed = (
            "declare -A SCOPE_ID\n"
            'SCOPE_ID["memory:acl:read-own"]="scope-read"\n'
            'SCOPE_ID["memory:acl:write"]="scope-write"\n'
        )
        return (
            "set -euo pipefail\n"
            + preamble
            + 'REALM="audittrace"\n'
            + extra_prelude
            + wrapper
            + find_client_id
            + "\n"
            + scope_id_seed
            + bind_scope
            + "\n"
            + f"AUDITTRACE_ACL_E2E_CLIENT_ENABLED={enabled}\n"
            + step
            + '\necho "STEP-COMPLETED"\n'
        )

    def popen(self, **kwargs) -> subprocess.Popen[str]:
        script = self._script_text(**kwargs)
        env = {
            **os.environ,
            "FAKE_STATE": str(self.state_path),
            "FAKE_LOG": str(self.log),
            "FAKE_MARKER_LOG": str(self.marker_log),
            "FAKE_HANG_MARKER": str(self.tmp / "hang.marker"),
        }
        return subprocess.Popen(
            ["bash", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            preexec_fn=_reset_sigint_to_default,
        )

    def run(self, **kwargs) -> subprocess.CompletedProcess[str]:
        proc = self.popen(**kwargs)
        out, err = proc.communicate(timeout=30)
        self.assert_no_unwrapped_calls()
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)

    def assert_no_unwrapped_calls(self) -> None:
        """Addendum F4 point 2: EVERY run — regardless of the script's
        own exit code or a tolerant call site's `|| true` — must show
        zero `marker=absent` lines. This is the PRIMARY proof; it does
        not depend on the fake's exit(97), which a tolerant site can
        swallow."""
        lines = [ln for ln in self.marker_log.read_text().splitlines() if ln]
        absent = [ln for ln in lines if "marker=absent" in ln]
        assert absent == [], f"unwrapped kcadm call(s) detected: {absent}"

    def mutating_calls(self) -> list[str]:
        lines = [ln for ln in self.log.read_text().splitlines() if ln]
        return [ln for ln in lines if ln.split()[0] in ("create", "delete", "update")]

    def state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text())


@pytest.fixture(params=[SCRIPT, CONFIGMAP], ids=["script", "configmap"])
def harness(request: pytest.FixtureRequest, tmp_path: Path) -> StepHarness:
    return StepHarness(tmp_path, request.param)


# ─────────────────────────────────────────────────────────────────────
# F-2a..k — the named line on every exit path
# ─────────────────────────────────────────────────────────────────────


class TestNamedFailureLineOnEveryExit:
    def test_f2a_lookup_failed(self, harness: StepHarness) -> None:
        proc = harness.run(
            clients=[],
            enabled="true",
            fail_match=[
                {"substr": "clientId=audittrace-acl-e2e", "rc": 1, "stderr": "boom"}
            ],
        )
        assert proc.returncode != 0
        _assert_exactly_one_line(proc)
        assert "code=lookup-failed" in proc.stdout
        assert "stage=lookup" in proc.stdout
        assert harness.mutating_calls() == []
        _assert_no_secret_shape(proc.stdout, proc.stderr)

    def test_f2b_malformed_lookup_never_silently_succeeds_or_creates(
        self, harness: StepHarness
    ) -> None:
        """A malformed (but non-empty, non-erroring) lookup result must
        never read as a silent success and must never take the create
        branch (F-2b). The route it actually takes (here: the
        ambiguous-match guard — a single malformed, comma-free line
        still counts as "exactly one line", so the script treats it as
        an existing-client match and then fails closed on the read
        that follows) is MEASURED, not asserted to be one specific
        code — the spec itself allows either ``ambiguous-match`` or
        ``lookup-failed``."""
        proc = harness.run(
            clients=[],
            enabled="true",
            fail_match=[
                {
                    "substr": "clientId=audittrace-acl-e2e",
                    "rc": 0,
                    "stdout": "garbage-no-comma",
                }
            ],
        )
        assert proc.returncode != 0
        assert "acl-e2e:" in proc.stderr
        assert not any(c.startswith("create clients") for c in harness.mutating_calls())

    @pytest.mark.parametrize(
        ("label", "malformed_stdout"),
        [
            (
                "json-instead-of-csv",
                '{"id":"aaaaaaaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbbbbbbb.ccccccccccccccccccccc","clientId":"audittrace-acl-e2e"}',
            ),
            (
                "crlf",
                "id,clientId\r\n"
                "aaaaaaaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbbbbbbb.ccccccccccccccccccccc,audittrace-acl-e2e\r\n",
            ),
        ],
    )
    def test_f2b_malformed_lookup_never_leaks_a_planted_token(
        self, harness: StepHarness, label: str, malformed_stdout: str
    ) -> None:
        """R4: the review's own named rows — a planted three-segment
        base64url-shaped token riding in the malformed lookup body
        must appear in NEITHER stream, on EITHER site, whichever route
        the malformed body takes (ambiguous-match here, since a
        single, comma-free-enough line still counts as "one line")."""
        proc = harness.run(
            clients=[],
            enabled="true",
            fail_match=[
                {
                    "substr": "clientId=audittrace-acl-e2e",
                    "rc": 0,
                    "stdout": malformed_stdout,
                }
            ],
        )
        assert proc.returncode != 0
        _assert_no_secret_shape(proc.stdout, proc.stderr)

    def test_f2c_create_failed(self, harness: StepHarness) -> None:
        proc = harness.run(
            clients=[],
            enabled="true",
            fail_match=[
                {
                    "substr": "create clients -r audittrace -f -",
                    "rc": 1,
                    "stderr": "boom",
                }
            ],
        )
        assert proc.returncode != 0
        _assert_exactly_one_line(proc)
        assert "code=create-failed" in proc.stdout
        assert "stage=create" in proc.stdout
        # The create was ATTEMPTED (logged) and FAILED — the state the
        # fake persists must show no client landed.
        assert harness.state()["clients"] == []

    def test_f2d_id_unresolvable(self, harness: StepHarness) -> None:
        proc = harness.run(
            clients=[],
            enabled="true",
            fail_match=[
                {
                    "substr": "clientId=audittrace-acl-e2e",
                    "nth": 2,
                    "rc": 0,
                    "stderr": "",
                }
            ],
        )
        # nth=2 with rc=0 but we need the SECOND lookup to return empty,
        # not merely succeed: force it via lookup_returns_nothing on the
        # 2nd match using a dedicated flag our fake does not special
        # case — instead, make the create append nothing by also
        # failing the create's effect: simplest reliable path is to
        # fail-match the 2nd lookup with rc=0 and no matching client
        # (the fake prints nothing for an unmatched clientId already).
        assert proc.returncode != 0
        _assert_exactly_one_line(proc)
        assert "code=id-unresolvable" in proc.stdout
        # ACL_E2E_STAGE is "lookup" at the moment of failure (the
        # post-create re-lookup, not the create call itself).
        assert "stage=lookup" in proc.stdout

    def test_f2e_strip_list_failed(self, harness: StepHarness) -> None:
        proc = harness.run(
            clients=[],
            enabled="true",
            fail_match=[
                {
                    "substr": "default-client-scopes -r audittrace --fields id,name",
                    "rc": 1,
                    "stderr": "boom",
                }
            ],
        )
        assert proc.returncode != 0
        _assert_exactly_one_line(proc)
        assert "code=strip-list-failed" in proc.stdout
        assert "stage=strip" in proc.stdout

    def test_f2f_strip_delete_failed(self, harness: StepHarness) -> None:
        """R2: the mandated-but-omitted F-2f row. The created client's
        own ``defaultClientScopes`` always matches the pinned set, so
        a normal create never leaves anything to strip — the listing
        call is overridden to report an EXTRA, unpinned scope
        (``unwanted-scope``) that ``acl_e2e_strip_scopes`` must then
        try to delete; that delete is made to fail."""
        proc = harness.run(
            clients=[],
            enabled="true",
            fail_match=[
                {
                    "substr": (
                        "clients/client-1/default-client-scopes -r audittrace"
                        " --fields id,name"
                    ),
                    "rc": 0,
                    "stdout": (
                        "s-read,memory:acl:read-own\n"
                        "s-write,memory:acl:write\n"
                        "s-extra,unwanted-scope"
                    ),
                },
                {
                    "substr": "delete clients/client-1/default-client-scopes/s-extra",
                    "rc": 1,
                    "stderr": "boom",
                },
            ],
        )
        assert proc.returncode != 0
        _assert_exactly_one_line(proc)
        assert "code=strip-delete-failed" in proc.stdout
        assert "stage=strip" in proc.stdout

    def test_f2g_unguarded_bind_loop_failure(self, harness: StepHarness) -> None:
        """R2: the mandated-but-omitted F-2g row, AND F1 SF-3's own
        requirement that the fixture use the REAL ``bind_scope`` (via
        ``_real_function_text`` — never a bare-call stub that would
        bypass the dynamic marker proof entirely, review R2's named
        defect). ``bind_scope``'s own ``kcadm update`` call is guarded
        locally (``if ! ... ; then echo ✗ ; return 1; fi``), but the
        step-level call site (``bind_scope "${ACL_E2E_CLIENT_ID}"
        "${SCOPE}" "default"`` inside the ``for`` loop) is NOT guarded
        by ``||`` — a non-zero return there is an ordinary `set -e`
        command failure (not a `nounset` abort), so the ERR trap DOES
        fire with a REAL line number, landing on the generic
        ``unguarded-failure`` fallback rather than a bind-specific
        code — exactly as F §2 F-2g predicts (``the trap line
        exit=1 stage=bind line=<n>``)."""
        proc = harness.run(
            clients=[],
            enabled="true",
            fail_match=[
                {
                    "substr": "update clients/client-1/default-client-scopes/scope-read",
                    "rc": 1,
                    "stderr": "boom",
                }
            ],
        )
        assert proc.returncode != 0
        _assert_exactly_one_line(proc)
        assert "code=unguarded-failure" in proc.stdout
        assert "stage=bind" in proc.stdout
        m = re.search(r"line=(\d+)", proc.stdout)
        assert m is not None, proc.stdout
        assert "✗ audittrace-acl-e2e default ← memory:acl:read-own" in proc.stderr

    @pytest.mark.parametrize(
        "raw_stdout,expected_diff_fragment",
        [
            (
                '  "id" : "mapper-1",\n'
                '  "name" : "aud-audittrace-server",\n'
                '  "protocolMapper" : "oidc-audience-mapper",\n'
                '  "consentRequired" : false',
                "mapper-config-absent",
            ),
            (
                '  "protocolMapper" : "oidc-audience-mapper",\n'
                '  "config" : {\n'
                '    "included.custom.audience" : "audittrace-server",\n'
                '    "id.token.claim" : "false",\n'
                '    "access.token.claim" : "true"\n'
                "  },\n"
                '  "config" : {\n'
                '    "included.custom.audience" : "audittrace-server",\n'
                '    "id.token.claim" : "false",\n'
                '    "access.token.claim" : "true"\n'
                "  }",
                "mapper-config-absent",
            ),
            (
                '  "protocolMapper" : "oidc-audience-mapper",\n'
                '  "config" : {\n'
                '    "included.custom.audience" : "audittrace-server",\n'
                '    "id.token.claim" : "false",\n'
                '    "access.token.claim" : "true",\n'
                '    "extra.widening.key" : "true"\n'
                "  }",
                "unexpected-mapper-config",
            ),
        ],
        ids=["no-config-block", "two-config-blocks", "fourth-config-key"],
    )
    def test_f2h_mapper_config_drift_variants(
        self,
        harness: StepHarness,
        raw_stdout: str,
        expected_diff_fragment: str,
    ) -> None:
        """R2: the mandated-but-omitted F-2h row — the three drift
        shapes the spec names: no ``config`` block, two ``config``
        blocks, and a 4th config key (an audience-widening or
        hardcoded-claim write would be shaped like this). The verify
        function's name-listing call (``--fields name``) must still
        succeed normally so the mapper NAME-set check passes and
        execution reaches the content check; only the second,
        raw-body ``protocol-mappers/models`` read (``nth=2`` within
        this isolated step — the step's own window issues it exactly
        twice) is overridden."""
        proc = harness.run(
            clients=[],
            enabled="true",
            fail_match=[
                {
                    "substr": "protocol-mappers/models",
                    "nth": 2,
                    "rc": 0,
                    "stdout": raw_stdout,
                }
            ],
        )
        assert proc.returncode != 0
        _assert_exactly_one_line(proc)
        assert "code=drift" in proc.stdout
        assert expected_diff_fragment in proc.stdout
        assert "stage=verify-mapper-config" in proc.stdout

    @pytest.mark.parametrize("line_ending", ["\n", "\r\n"], ids=["lf", "crlf"])
    def test_f2h_drift_diff_never_leaks_a_planted_token_in_either_stream(
        self, harness: StepHarness, line_ending: str
    ) -> None:
        """R4: ``acl_e2e_verify``'s drift diff is the ONE confirmed
        vector where kcadm's own RESPONSE text (``${line}``, read
        verbatim from the mapper-config body) rides straight into
        ``detail=`` via ``acl_e2e_fail drift "...:${diffs}"`` — F
        section 1.2's rule that ``detail=`` never carries a kcadm
        response body. A 4th config key whose VALUE is a planted
        three-segment base64url-shaped token, in BOTH a plain-LF and a
        CRLF-terminated raw body, must be redacted (``acl_e2e_mask``)
        from BOTH stdout and stderr — not merely absent by the
        tokenizer dropping the line entirely."""
        planted = "aaaaaaaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbbbbbbb.ccccccccccccccccccccc"
        raw_stdout = line_ending.join(
            [
                '  "protocolMapper" : "oidc-audience-mapper",',
                '  "config" : {',
                '    "included.custom.audience" : "audittrace-server",',
                '    "id.token.claim" : "false",',
                '    "access.token.claim" : "true",',
                f'    "evil.widening.key" : "{planted}"',
                "  }",
            ]
        )
        proc = harness.run(
            clients=[],
            enabled="true",
            fail_match=[
                {
                    "substr": "protocol-mappers/models",
                    "nth": 2,
                    "rc": 0,
                    "stdout": raw_stdout,
                }
            ],
        )
        assert proc.returncode != 0
        assert "code=drift" in proc.stdout
        _assert_no_secret_shape(proc.stdout, proc.stderr)
        # R4 (2026-10-07 re-review): the fix is no longer a redaction
        # MASK (beatable by any secret shape it doesn't name) — the
        # response-derived value is OMITTED from detail= entirely,
        # replaced by a property (count=<n>). The planted token must
        # not appear in ANY form, masked or otherwise.
        assert "<redacted-token>" not in proc.stdout
        assert planted not in proc.stdout
        assert planted not in proc.stderr
        assert "unexpected-mapper-config:count=1" in proc.stdout

    @pytest.mark.parametrize(
        ("label", "planted"),
        [
            ("32-char-opaque", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"),
            ("short-segment-3-part", "ab12.cd34.ef56"),
            (
                "jwt-shaped",
                "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4eXoifQ.SflKxwRJSMeKKF2QT4f",
            ),
            ("client-secret-uuid", "550e8400-e29b-41d4-a716-446655440000"),
        ],
    )
    @pytest.mark.parametrize("line_ending", ["\n", "\r\n"], ids=["lf", "crlf"])
    def test_f2h_no_secret_shape_ever_appears_regardless_of_shape(
        self, harness: StepHarness, label: str, planted: str, line_ending: str
    ) -> None:
        """R4 (2026-10-07 re-review): a DENY-LIST on specific shapes
        (the round-1 fix) is beatable by any shape it doesn't name —
        measured: a 32-char non-JWT value and a short-segment 3-part
        token both leaked. The round-2 fix removes response-derived
        TEXT from detail= entirely (never masks it), so it must hold
        for EVERY shape, named or not."""
        raw_stdout = line_ending.join(
            [
                '  "protocolMapper" : "oidc-audience-mapper",',
                '  "config" : {',
                '    "included.custom.audience" : "audittrace-server",',
                '    "id.token.claim" : "false",',
                '    "access.token.claim" : "true",',
                f'    "evil.widening.key" : "{planted}"',
                "  }",
            ]
        )
        proc = harness.run(
            clients=[],
            enabled="true",
            fail_match=[
                {
                    "substr": "protocol-mappers/models",
                    "nth": 2,
                    "rc": 0,
                    "stdout": raw_stdout,
                }
            ],
        )
        assert proc.returncode != 0
        assert "code=drift" in proc.stdout
        assert planted not in proc.stdout, label
        assert planted not in proc.stderr, label

    @pytest.mark.parametrize(
        ("label", "planted"),
        [
            ("32-char-opaque", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"),
            ("short-segment-3-part", "ab12.cd34.ef56"),
            ("client-secret-uuid", "550e8400-e29b-41d4-a716-446655440000"),
        ],
    )
    def test_f2h_no_secret_shape_leaks_via_a_planted_scope_or_mapper_name(
        self, harness: StepHarness, label: str, planted: str
    ) -> None:
        """Same shapes, DIFFERENT vector: a secret-shaped value used as
        the NAME of an unexpected scope (the CSV-listing path, not the
        raw JSON mapper-config body) must also never appear in the ONE
        STRUCTURED ``acl-e2e:`` diagnostic line — this path was
        rewritten to a bare ``count=<n>`` too. (The ruling scopes
        ``detail=``, not every human-readable progress echo — a scope
        NAME is not a secret and `acl_e2e_strip_scopes`'s own
        "stripped unpinned ... scope <name>" success line legitimately
        still names it, pre-dating this round and tested by
        `test_absent_is_created_stripped_bound_and_verified`.)"""
        proc = harness.run(
            clients=[],
            enabled="true",
            fail_match=[
                {
                    "substr": "default-client-scopes -r audittrace --fields id,name",
                    "rc": 0,
                    "stdout": f"s-x,{planted}",
                }
            ],
        )
        assert proc.returncode != 0
        structured_out = [
            ln for ln in proc.stdout.splitlines() if ln.startswith("acl-e2e:")
        ]
        structured_err = [
            ln for ln in proc.stderr.splitlines() if ln.startswith("acl-e2e:")
        ]
        assert structured_out and structured_err, (proc.stdout, proc.stderr)
        assert all(planted not in ln for ln in structured_out), label
        assert all(planted not in ln for ln in structured_err), label
        assert "unexpected-default-scope:count=" in proc.stdout

    @pytest.mark.parametrize(
        ("label", "planted"),
        [
            ("32-char-opaque", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"),
            ("short-segment-3-part", "ab12.cd34.ef56"),
        ],
    )
    def test_f2h_no_secret_shape_leaks_via_stderr_on_a_guarded_failure(
        self, harness: StepHarness, label: str, planted: str
    ) -> None:
        """Stderr variant: a secret-shaped value in the FAKE's stderr
        output on an ordinary guarded failure must never reach
        detail= — every guarded ``acl_e2e_fail`` call's detail is a
        static, pinned string; the fake's own stderr is never captured
        into it. A regression check, not a newly-found vector."""
        proc = harness.run(
            clients=[],
            enabled="true",
            fail_match=[
                {
                    "substr": "clientId=audittrace-acl-e2e",
                    "rc": 1,
                    "stderr": f"boom: secret={planted}",
                }
            ],
        )
        assert proc.returncode != 0
        assert planted not in proc.stdout, label
        # The fake's raw stderr is inherited by the real process's own
        # stderr (unrelated to the structured acl-e2e: line) — assert
        # it never reaches the ONE STRUCTURED line this gate cares
        # about, not the whole stream.
        for ln in proc.stderr.splitlines():
            if ln.startswith("acl-e2e:"):
                assert planted not in ln, label

    @pytest.mark.parametrize(
        ("label", "planted"),
        [
            ("32-char-opaque", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"),
            ("short-segment-3-part", "ab12cd34.ef56gh78.ij90kl12"),
        ],
    )
    @pytest.mark.parametrize("line_ending", ["\n", "\r\n"], ids=["lf", "crlf"])
    def test_f2h_acl_e2e_compact_never_leaks_the_lookup_responses_own_id(
        self, harness: StepHarness, label: str, planted: str, line_ending: str
    ) -> None:
        """R4 re-review #3: ``acl_e2e_compact``'s own
        ``"cannot read $1"`` survived TWO prior rounds — `$1` is
        ``clients/<uuid>``, where `<uuid>` is parsed from the LOOKUP
        RESPONSE, not a constant. A secret-shaped value planted as the
        post-create lookup's own ``id`` field, then failing the
        subsequent whole-client read, must never surface it — JSON (an
        unquoted-id CSV cell) and CRLF variants, both sites, both
        streams."""
        proc = harness.run(
            clients=[],
            enabled="true",
            fail_match=[
                {
                    "substr": "clientId=audittrace-acl-e2e",
                    "nth": 2,
                    "rc": 0,
                    "stdout": f"{planted},audittrace-acl-e2e{line_ending}",
                },
                {
                    # Anchored with the trailing " -r" so this matches
                    # ONLY `acl_e2e_compact`'s bare-client read, never a
                    # sub-path call (`clients/<id>/default-client-scopes`
                    # etc.) that happens to contain the same prefix.
                    "substr": f"get clients/{planted} -r",
                    "rc": 1,
                    "stderr": "boom",
                },
            ],
        )
        assert proc.returncode != 0
        assert "code=read-failed" in proc.stdout
        assert "cannot read the client" in proc.stdout
        assert planted not in proc.stdout, label
        assert planted not in proc.stderr, label

    def test_echoing_any_response_substring_into_detail_reddens(
        self, harness: StepHarness
    ) -> None:
        """R4's own falsifiability requirement: re-introduce the OLD
        vulnerable line (``diffs+=" unexpected-mapper-config:${line}"``,
        echoing the raw response LINE into ``detail=``) in place of the
        fixed ``count``-only increment — this must turn the no-leak
        assertion RED, proving the fix is a REAL property this test
        suite enforces, not an accident of the current shapes tried.
        Uses a 32-char OPAQUE shape (not the 3-part base64url shape
        ``acl_e2e_mask`` also still redacts as defence in depth) so this
        test isolates the "never echo response content" property from
        the masking layer."""
        planted = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
        raw_stdout = "\n".join(
            [
                '  "protocolMapper" : "oidc-audience-mapper",',
                '  "config" : {',
                '    "included.custom.audience" : "audittrace-server",',
                '    "id.token.claim" : "false",',
                '    "access.token.claim" : "true",',
                f'    "evil.widening.key" : "{planted}"',
                "  }",
            ]
        )
        script = harness._script_text(
            clients=[],
            enabled="true",
            fail_match=[
                {
                    "substr": "protocol-mappers/models",
                    "nth": 2,
                    "rc": 0,
                    "stdout": raw_stdout,
                }
            ],
        )
        needle = "unexpected_config=$((unexpected_config + 1))"
        assert script.count(needle) == 1
        mutated = script.replace(
            needle, 'diffs+=" unexpected-mapper-config:${line}"', 1
        )
        env = {
            **os.environ,
            "FAKE_STATE": str(harness.state_path),
            "FAKE_LOG": str(harness.log),
            "FAKE_MARKER_LOG": str(harness.marker_log),
        }
        proc = subprocess.run(
            ["bash", "-c", mutated], capture_output=True, text=True, env=env, timeout=30
        )
        assert planted in proc.stdout, (
            "the neuter should have reopened the leak — detail= fix is unproven"
        )

    def test_f2i_kcadm_timeout_in_the_real_step(self, harness: StepHarness) -> None:
        """R3: a timed-out kcadm call INSIDE the real ACL-E2E step (the
        first lookup, genuinely `||`-guarded by ``acl_e2e_fail
        lookup-failed ...``) must surface ``code=kcadm-timeout rc=124``
        — never collapsed into the guard's own ``lookup-failed``. The
        real rc (124, captured from `$?` as `acl_e2e_fail`'s own first
        statement) overrides the guard's generic code; this is the REAL
        step text, never a synthetic call outside it."""
        proc = harness.run(
            clients=[],
            enabled="true",
            fail_match=[
                {
                    "substr": "clientId=audittrace-acl-e2e",
                    "rc": 0,
                    "hang_seconds": 3,
                }
            ],
            extra_prelude="ACL_E2E_KCADM_TIMEOUT=1\n",
        )
        assert proc.returncode != 0
        _assert_exactly_one_line(proc)
        assert "code=kcadm-timeout" in proc.stdout
        assert "rc=124" in proc.stdout
        assert "stage=lookup" in proc.stdout
        assert "lookup-failed" not in proc.stdout
        assert "rc=124" in proc.stdout

    def test_f2j_planted_false_is_an_unguarded_failure_with_a_real_line(
        self, harness: StepHarness
    ) -> None:
        proc = harness.run(clients=[], enabled="true", extra_prelude="false\n")
        assert proc.returncode != 0
        _assert_exactly_one_line(proc)
        assert "code=unguarded-failure" in proc.stdout
        m = re.search(r"line=(\d+)", proc.stdout)
        assert m is not None, proc.stdout

    def test_f2k_unset_client_id_hits_the_real_u_trap_path(
        self, harness: StepHarness
    ) -> None:
        """R7 fix: the previous version of this test stripped
        ``REALM="audittrace"``, but EVERY reference to ``${REALM}`` in
        the step sits inside a ``$(...)`` command substitution that is
        itself the left side of a ``||`` guard — under `set -u`, bash's
        own unbound-variable abort kills only that SUBSHELL (exit 127),
        and the PARENT shell's `||` catches the subshell's failure like
        any other command failure. Measured: that version emitted
        ``code=lookup-failed line=?`` — an ordinary GUARDED failure
        (already covered by F-2a) that happens to also show ``line=?``
        for the unrelated reason that the ERR trap never ran. It was
        never exercising "the -u path" distinctly from F-2a (review
        R7).

        ``ACL_E2E_CLIENT_ID`` is referenced UNGUARDED — a plain `echo`,
        not inside `$(...)`, not on the left of `||` — before the first
        guarded call. Measured: unsetting it makes bash's nounset abort
        kill the TOP-LEVEL shell directly; no ERR trap runs (confirmed:
        bash does not invoke a user ERR trap for its own nounset abort,
        only the EXIT trap, which never had ``ACL_E2E_ERR_LINE`` set);
        ``acl_e2e_on_exit`` falls back to ``code=unguarded-failure
        rc=1 line=? detail=unreported-exit`` — a result genuinely
        distinct from every guarded F-2 row."""
        script = harness._script_text(clients=[], enabled="true")
        assert script.count('ACL_E2E_CLIENT_ID="audittrace-acl-e2e"\n') == 1
        script = script.replace('ACL_E2E_CLIENT_ID="audittrace-acl-e2e"\n', "", 1)
        script = "set -u\n" + script
        env = {
            **os.environ,
            "FAKE_STATE": str(harness.state_path),
            "FAKE_LOG": str(harness.log),
            "FAKE_MARKER_LOG": str(harness.marker_log),
        }
        proc = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30
        )
        assert proc.returncode != 0
        _assert_exactly_one_line(proc)
        assert "code=unguarded-failure" in proc.stdout
        assert "line=?" in proc.stdout
        assert "code=lookup-failed" not in proc.stdout
        harness.assert_no_unwrapped_calls()


# ─────────────────────────────────────────────────────────────────────
# F-2l — SIGTERM during a hanging kcadm call (lookup, top level)
# ─────────────────────────────────────────────────────────────────────


class TestSignalTermDuringSunsetDelete:
    """F-2l's own spirit (a TOP-LEVEL call — not one nested inside a
    ``$(...)`` substitution NOR a pipeline element, both of which fork
    a subshell that defers signal delivery per F2 S-b's own finding).
    ``acl_e2e_lookup`` is always ``$(...)``-called and the ``create``
    call is the last stage of a ``printf | acl_e2e_kc`` pipeline — bash
    forks a subshell for EVERY pipeline element by default (no
    ``lastpipe``), so both are nested too. The sunset branch's plain
    ``acl_e2e_kc delete ... || acl_e2e_fail`` is genuinely top-level:
    no ``$()``, no ``|`` — the site where an instant TERM response is
    provable. (The auth loop, also top-level, is the live mechanism's
    other such site; covered by the real per-call-timeout cross-file
    pin instead, since the step harness has no pre-step auth loop.)"""

    def test_sigterm_during_sunset_delete_names_itself_and_mutates_nothing(
        self, harness: StepHarness
    ) -> None:
        marker = harness.tmp / "hang.marker"
        proc = harness.popen(
            clients=[
                {
                    "id": "uuid-e2e",
                    "clientId": "audittrace-acl-e2e",
                    "body": {"clientId": "audittrace-acl-e2e"},
                    "default": [],
                    "optional": [],
                    "mappers": [],
                }
            ],
            enabled="false",
            fail_match=[
                {"substr": "delete clients/uuid-e2e", "rc": 0, "hang_seconds": 30}
            ],
            # Widen the per-call timeout WELL past the 30s hang so the
            # production `timeout -k 5 20` wrapper can never race our
            # OWN injected signal under heavy scheduling delay on a
            # shared box (measured: a `rc=124 kcadm-timeout` instead of
            # the intended `rc=143 signal-term` under contention).
            extra_prelude="ACL_E2E_KCADM_TIMEOUT=120\n",
        )
        deadline = time.time() + 10
        while not marker.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "the fake never reached its hang point"
        signal_sent_at = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        # 60s is a HANG CEILING ONLY (communicate must eventually
        # return even in the worst case) — it is NOT the guard this
        # test proves. Re-review (d): a non-interruptible, FOREGROUNDED
        # wrapper (guard V), or a handler that never kills its child
        # (guard U), both let the fake's own 30s hang run to natural
        # completion and only THEN process the already-pending signal
        # — producing the CORRECT-LOOKING `code=signal-term rc=143`
        # well inside a 60s window despite being functionally broken.
        # The elapsed-time bound below (<5s) is what actually proves
        # prompt interruption; V and U went GREEN under the widened
        # timeout alone until this assertion existed.
        out, err = proc.communicate(timeout=60)
        elapsed = time.monotonic() - signal_sent_at
        harness.assert_no_unwrapped_calls()
        assert proc.returncode == 143
        assert elapsed < 5, f"took {elapsed:.1f}s — not promptly interrupted"
        assert _acl_e2e_line_count(out) == 1, out
        assert _acl_e2e_line_count(err) == 1, err
        assert "code=signal-term" in out
        assert "stage=sunset-delete" in out
        # The delete was ATTEMPTED (killed mid-flight) — the client
        # must still be present (the fake deletes only on a clean 0).
        assert harness.state()["clients"] != []
        _assert_no_secret_shape(out, err)

    def test_sigint_during_sunset_delete_names_itself_with_rc_130(
        self, harness: StepHarness
    ) -> None:
        """The SIGINT mirror of the test above — the closed code set
        names `signal-int` distinctly from `signal-term` (rc=130, not
        143); no mandated test exercised SIGINT at all before this."""
        marker = harness.tmp / "hang.marker"
        proc = harness.popen(
            clients=[
                {
                    "id": "uuid-e2e",
                    "clientId": "audittrace-acl-e2e",
                    "body": {"clientId": "audittrace-acl-e2e"},
                    "default": [],
                    "optional": [],
                    "mappers": [],
                }
            ],
            enabled="false",
            fail_match=[
                {"substr": "delete clients/uuid-e2e", "rc": 0, "hang_seconds": 30}
            ],
            extra_prelude="ACL_E2E_KCADM_TIMEOUT=120\n",
        )
        deadline = time.time() + 10
        while not marker.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "the fake never reached its hang point"
        signal_sent_at = time.monotonic()
        proc.send_signal(signal.SIGINT)
        # See the SIGTERM sibling above: 60s is a HANG CEILING ONLY —
        # the elapsed-time bound below is the real guard (re-review d).
        out, err = proc.communicate(timeout=60)
        elapsed = time.monotonic() - signal_sent_at
        harness.assert_no_unwrapped_calls()
        assert proc.returncode == 130
        assert elapsed < 5, f"took {elapsed:.1f}s — not promptly interrupted"
        assert _acl_e2e_line_count(out) == 1, out
        assert _acl_e2e_line_count(err) == 1, err
        assert "code=signal-int" in out
        assert "rc=130" in out
        _assert_no_secret_shape(out, err)


class TestSignalTermDuringLookupNested:
    """F-2l v1 — TERM during a call reached via `$(acl_e2e_lookup)` (the
    FIRST lookup, nested since every call to `acl_e2e_lookup` is itself
    inside a command substitution). F2 S-b's own finding holds: a
    signal to the main process is DEFERRED until the per-call timeout
    naturally unblocks the nested subshell (there is no bash-level way
    around this without a prompt response for every nested call — out
    of scope for this fix round, named honestly rather than claimed).
    MEASURED (this round's rc-override fix, R3): the line now correctly
    shows the REAL cause — `code=kcadm-timeout rc=124` — never
    `lookup-failed` — because `acl_e2e_fail` captures the real `$?`
    (124, from the per-call timeout that unblocked the subshell) before
    applying the guard's generic code. The signal's OWN rc (143) still
    wins the race for the FINAL PROCESS exit status (a second,
    independent effect: the deferred trap fires again at the process's
    own exit boundary and forces `exit 143`), producing a disclosed,
    measured mismatch between the printed `rc=124` and `proc.returncode
    == 143` — both are asserted here as MEASURED, not as a claim that
    the printed code is `signal-term` (F1 section 1.3's literal text,
    written before F2 S-b narrowed what is achievable for nested
    calls)."""

    def test_term_during_lookup_measured_kcadm_timeout_not_lookup_failed(
        self, harness: StepHarness
    ) -> None:
        marker = harness.tmp / "hang.marker"
        proc = harness.popen(
            clients=[],
            enabled="true",
            fail_match=[
                {
                    "substr": "clientId=audittrace-acl-e2e",
                    "rc": 0,
                    "hang_seconds": 30,
                }
            ],
        )
        deadline = time.time() + 10
        while not marker.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "the fake never reached its hang point"
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=30)
        harness.assert_no_unwrapped_calls()
        assert proc.returncode == 143
        assert _acl_e2e_line_count(out) == 1, out
        assert _acl_e2e_line_count(err) == 1, err
        # Never collapsed into the guard's own generic code (R3):
        assert "code=lookup-failed" not in out
        assert "code=kcadm-timeout" in out
        assert "rc=124" in out
        assert "stage=lookup" in out
        _assert_no_secret_shape(out, err)


class TestInitStateDeletedThenSignal:
    """F-2l v4 — with the handler-state initialisation (`ACL_E2E_REPORTED`,
    `ACL_E2E_REPORT_FILE`, ...) deleted, a TERM must NOT produce the
    named line (the handler itself would die on an unset variable under
    `set -u`, or reference an empty report-file path) — this is the RED
    the initialised build must never show, and proves init-before-trap
    actually matters rather than being redundant."""

    def test_removing_state_init_breaks_the_term_line(
        self, harness: StepHarness
    ) -> None:
        preamble = _preamble_text(harness.site)
        # Delete every ACL_E2E_* state-init assignment the preamble sets
        # BEFORE installing the traps (mirrors F3 S2-1's own neuter) —
        # scoped to the pre-function-definitions portion only, so this
        # does NOT also strip the SAME variable names' assignments
        # inside `acl_e2e_on_err`/`acl_e2e_kc`'s own bodies further down.
        split_at = preamble.index("acl_e2e_mask() {")
        init_block, rest = preamble[:split_at], preamble[split_at:]
        mutated_init = re.sub(
            r"^[ \t]*ACL_E2E_(REPORTED|CHILD|STAGE|ERR_LINE|ERR_CMD|REPORT_FILE)=.*$\n",
            "",
            init_block,
            flags=re.M,
        )
        assert mutated_init != init_block
        mutated_preamble = mutated_init + rest
        wrapper = (
            _wrapper_block(harness.site)
            .replace(
                'kubectl -n "${NAMESPACE}" exec -i "${KC_POD}" -c keycloak -- '
                "/opt/keycloak/bin/kcadm.sh",
                f'"{harness.fake}"',
            )
            .replace("/opt/keycloak/bin/kcadm.sh", f'"{harness.fake}"')
        )
        marker = harness.tmp / "preflight.marker"
        # A SHORT sleep: without the state init, the TERM trap itself
        # dies on the now-unset `ACL_E2E_CHILD` reference (an unbound-
        # variable error INSIDE the trap) and, because the trap never
        # reaches its own `exit`, bash simply resumes waiting for the
        # ORIGINAL child — the signal is silently swallowed rather than
        # terminating the process. A short sleep makes that "nothing
        # happened, ran to its natural end" outcome observable quickly
        # instead of needing to wait out a long hang.
        script = (
            "set -u\n"
            + mutated_preamble
            + 'REALM="audittrace"\n'
            + wrapper
            + f'touch "{marker}"\n'
            + "sleep 2 &\nwait $!\n"
        )
        harness.state_path.write_text(json.dumps({"clients": [], "scopes": []}))
        env = {
            **os.environ,
            "FAKE_STATE": str(harness.state_path),
            "FAKE_LOG": str(harness.log),
        }
        proc = subprocess.Popen(
            ["bash", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            preexec_fn=_reset_sigint_to_default,
        )
        deadline = time.time() + 10
        while not marker.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "the mutated script never reached the sleep"
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=10)
        # The RED this round's fix must NOT show: no named line, because
        # the handler itself cannot run without its state.
        assert _acl_e2e_line_count(out) == 0, out
        assert _acl_e2e_line_count(err) == 0, err
        assert proc.returncode != 143


class TestChildInitAloneIsLoadBearing:
    """Neuter-table F: the BATCHED removal above (every ACL_E2E_* state
    var at once) proves the init block matters SOMEWHERE, but does not
    isolate ``ACL_E2E_CHILD=""`` on its own — measured: once
    ``acl_e2e_kc`` has run at least once, ``ACL_E2E_CHILD`` is already
    assigned by the NORMAL code path, so removing only its init is
    invisible to a TERM arriving during a later hang. The isolating
    case is a TERM arriving BEFORE the first ``acl_e2e_kc`` call ever
    runs — there, under ``set -u``, the signal handler's own
    ``[[ -n "${ACL_E2E_CHILD}" ]]`` reference aborts on the unset
    variable, producing a WRONG ``code=unguarded-failure rc=127``
    instead of the correct ``code=signal-term rc=143``."""

    @pytest.mark.parametrize("site", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_term_before_the_first_kcadm_call_needs_the_child_init(
        self, tmp_path: Path, site: Path
    ) -> None:
        """Runs the REAL, UNMUTATED preamble (verbatim from source) —
        a neuter that deletes the ``ACL_E2E_CHILD=""`` init line must
        turn THIS test RED (measured: without it, the same scenario
        gives ``rc=127 code=unguarded-failure`` instead)."""
        preamble = _preamble_text(site)
        marker = tmp_path / "preflight.marker"
        script = (
            "set -u\n"
            + preamble
            + f'touch "{marker}"\n'
            + "sleep 10 &\nwait $!\necho after\n"
        )
        proc = subprocess.Popen(
            ["bash", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=_reset_sigint_to_default,
        )
        deadline = time.time() + 10
        while not marker.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "the script never reached the sleep"
        time.sleep(0.2)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=10)
        assert proc.returncode == 143
        assert "code=signal-term" in out
        assert "code=signal-term" in err


class TestPlantedSecondEmitter:
    """F1 section 2 (SF-1): a planted SECOND emitter call must be
    caught by the "exactly once" assertion the real tests already rely
    on — this proves the ASSERTION ITSELF is falsifiable, not just that
    the production code happens to emit once today."""

    def test_two_emit_calls_in_one_run_fail_the_exactly_once_assertion(self) -> None:
        preamble = _preamble_text(SCRIPT)
        script = (
            "set -euo pipefail\n"
            + preamble
            + 'REALM="audittrace"\n'
            + 'acl_e2e_emit "planted-first" 1 "?" "-" "first"\n'
            + 'acl_e2e_emit "planted-second" 1 "?" "-" "second"\n'
        )
        proc = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, timeout=10
        )
        with pytest.raises(AssertionError):
            _assert_exactly_one_line(proc)
        assert _acl_e2e_line_count(proc.stdout) == 2


class TestCodeSetEquality:
    """F2 section 7: the closed set of codes the emitter can ACTUALLY
    produce, pinned by source scan — never typed as a separate,
    independently-maintained list that could silently drift from what
    the scripts emit."""

    _EXPECTED_CODES = frozenset(
        {
            # The 11 original acl_e2e_fail codes (F section 1.2):
            "lookup-failed",
            "create-failed",
            "id-unresolvable",
            "strip-list-failed",
            "strip-delete-failed",
            "read-failed",
            "drift",
            "ambiguous-match",
            "sunset-wrong-client-id",
            "sunset-delete-failed",
            "sunset-readback-nonempty",
            # The rc-based overrides + bootstrap/handler codes:
            "kcadm-timeout",
            "unwrapped-call",
            "signal-term",
            "signal-int",
            "unguarded-failure",
            "timeout-missing",
            "mktemp-missing",
        }
    )

    @staticmethod
    def _codes_in_source(text: str) -> set[str]:
        """Every code the emitter can produce, by source shape:
        (a) the first argument of every ``acl_e2e_fail <code> ...``
        call site (bareword, not a variable); (b) the rc-based
        ``NNN) code="..." ;;`` overrides inside ``acl_e2e_fail`` AND
        ``acl_e2e_on_exit``'s own mapping; (c) ``acl_e2e_on_exit``'s
        default ``local code="unguarded-failure"``; (d) the two
        bootstrap ``code=<x>-missing`` lines; (e) ``acl_e2e_on_signal``'s
        dynamically-built ``signal-${sig,,}`` — not a literal string in
        source, so it is recognised by the construction shape itself,
        never typed as a bare assumption."""
        codes = set(re.findall(r"acl_e2e_fail\s+([a-z][a-z-]*)\s", text))
        codes |= set(re.findall(r'\)\s*code="([a-z][a-z-]*)"\s*;;', text))
        codes |= set(re.findall(r'local code="([a-z][a-z-]*)"', text))
        codes |= set(re.findall(r"code=([a-z]+-missing)", text))
        if 'acl_e2e_report "signal-${sig,,}"' in text:
            codes |= {"signal-term", "signal-int"}
        return codes

    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_emitted_code_set_matches_the_closed_set_exactly(self, path) -> None:
        text = path.read_text(encoding="utf-8")
        found = self._codes_in_source(text)
        assert found == self._EXPECTED_CODES, (
            f"{path.name}: emitted codes {sorted(found)} != "
            f"expected {sorted(self._EXPECTED_CODES)}"
        )


# ─────────────────────────────────────────────────────────────────────
# F2 BF1-b — the 7 subshell-nesting sites
# ─────────────────────────────────────────────────────────────────────


class TestSubshellNestingSingleEmitter:
    def test_compact_inside_verify_nested_once(self, harness: StepHarness) -> None:
        proc = harness.run(
            clients=[
                {
                    "id": "uuid-e2e",
                    "clientId": "audittrace-acl-e2e",
                    "body": {"clientId": "audittrace-acl-e2e"},
                    "default": [{"id": "s1", "name": "memory:acl:read-own"}],
                    "optional": [],
                    "mappers": [],
                }
            ],
            enabled="true",
            fail_match=[
                {
                    "substr": "get clients/uuid-e2e -r audittrace",
                    "rc": 1,
                    "stderr": "boom",
                }
            ],
        )
        assert proc.returncode != 0
        _assert_exactly_one_line(proc)
        assert "code=read-failed" in proc.stdout
        assert "stage=verify-client" in proc.stdout

    def test_scope_names_default_inside_verify_nested_once(
        self, harness: StepHarness
    ) -> None:
        proc = harness.run(
            clients=[
                {
                    "id": "uuid-e2e",
                    "clientId": "audittrace-acl-e2e",
                    "body": {"clientId": "audittrace-acl-e2e"},
                    "default": [{"id": "s1", "name": "memory:acl:read-own"}],
                    "optional": [],
                    "mappers": [],
                }
            ],
            enabled="true",
            fail_match=[
                {
                    "substr": "default-client-scopes -r audittrace --fields name",
                    "rc": 1,
                    "stderr": "boom",
                }
            ],
        )
        assert proc.returncode != 0
        _assert_exactly_one_line(proc)
        assert "code=read-failed" in proc.stdout
        assert "stage=verify-default-scopes" in proc.stdout

    def test_scope_names_optional_inside_verify_nested_once(
        self, harness: StepHarness
    ) -> None:
        proc = harness.run(
            clients=[
                {
                    "id": "uuid-e2e",
                    "clientId": "audittrace-acl-e2e",
                    "body": {"clientId": "audittrace-acl-e2e"},
                    "default": [{"id": "s1", "name": "memory:acl:read-own"}],
                    "optional": [],
                    "mappers": [],
                }
            ],
            enabled="true",
            fail_match=[
                {
                    "substr": "optional-client-scopes -r audittrace --fields name",
                    "rc": 1,
                    "stderr": "boom",
                }
            ],
        )
        assert proc.returncode != 0
        _assert_exactly_one_line(proc)
        assert "code=read-failed" in proc.stdout
        assert "stage=verify-optional-scopes" in proc.stdout

    def test_first_lookup_nested_once(self, harness: StepHarness) -> None:
        proc = harness.run(
            clients=[],
            enabled="true",
            fail_match=[
                {
                    "substr": "clientId=audittrace-acl-e2e",
                    "nth": 1,
                    "rc": 1,
                    "stderr": "boom",
                }
            ],
        )
        assert proc.returncode != 0
        _assert_exactly_one_line(proc)
        assert "code=lookup-failed" in proc.stdout
        assert "stage=lookup" in proc.stdout

    def test_post_create_lookup_nested_once(self, harness: StepHarness) -> None:
        proc = harness.run(
            clients=[],
            enabled="true",
            fail_match=[
                {
                    "substr": "clientId=audittrace-acl-e2e",
                    "nth": 2,
                    "rc": 1,
                    "stderr": "boom",
                }
            ],
        )
        assert proc.returncode != 0
        _assert_exactly_one_line(proc)
        assert "code=lookup-failed" in proc.stdout

    def test_sunset_lookup_nested_once(self, harness: StepHarness) -> None:
        proc = harness.run(
            clients=[
                {
                    "id": "uuid-e2e",
                    "clientId": "audittrace-acl-e2e",
                    "body": {"clientId": "audittrace-acl-e2e"},
                    "default": [],
                    "optional": [],
                    "mappers": [],
                }
            ],
            enabled="false",
            fail_match=[
                {
                    "substr": "clientId=audittrace-acl-e2e",
                    "nth": 1,
                    "rc": 1,
                    "stderr": "boom",
                }
            ],
        )
        assert proc.returncode != 0
        _assert_exactly_one_line(proc)
        assert "code=lookup-failed" in proc.stdout
        assert "stage=sunset-lookup" in proc.stdout

    def test_sunset_readback_lookup_nested_once(self, harness: StepHarness) -> None:
        proc = harness.run(
            clients=[
                {
                    "id": "uuid-e2e",
                    "clientId": "audittrace-acl-e2e",
                    "body": {"clientId": "audittrace-acl-e2e"},
                    "default": [],
                    "optional": [],
                    "mappers": [],
                }
            ],
            enabled="false",
            fail_match=[
                {
                    "substr": "clientId=audittrace-acl-e2e",
                    "nth": 2,
                    "rc": 1,
                    "stderr": "boom",
                }
            ],
        )
        assert proc.returncode != 0
        _assert_exactly_one_line(proc)
        assert "code=lookup-failed" in proc.stdout
        assert "stage=sunset-readback" in proc.stdout


# ─────────────────────────────────────────────────────────────────────
# F3 §1.1 — the dynamic ACL_E2E_VIA_KC marker
# ─────────────────────────────────────────────────────────────────────


class TestErrtraceIsLoadBearing:
    """Neuter-table C (2026-10-07 re-review #3): the round-2 version of
    this test used ``x=$(fn_with_false)`` — but a `$(...)`-called
    function's failure STILL fails the parent assignment under plain
    `set -e` (the review measured this directly: `set -e` alone, no
    `-E`, already aborts THAT shape via the PARENT's own ERR trap,
    reporting `unguarded-failure line=<n>` either way). That shape
    cannot tell `-E` from `+E` apart — it was vacuous, not a proof.

    The shape `-E` ACTUALLY governs: an unguarded failure in the
    MIDDLE of a function, called AT TOP LEVEL (not via `$(...)`) — a
    REAL, PRODUCTION shape: ``ensure_mapper``'s own unguarded
    ``printf ... | acl_e2e_kc create ... >/dev/null`` (never
    `||`-guarded), followed by an `echo`. Measured directly: WITHOUT
    `-E`, `set -e` still aborts the script when this fails (nothing
    guards the `ensure_mapper` call itself), but the ERR TRAP NEVER
    FIRES — `ACL_E2E_ERR_LINE` stays at its init value, so the emitted
    line carries `line=?`. WITH `-E` (the real preamble), the trap
    fires and the emitted line names the REAL failing line."""

    @staticmethod
    def _ensure_mapper_create_call_line(script_text: str) -> int:
        """The exact line BASH_LINENO reports for this call, computed
        from the SAME built text the harness executes, never retyped.
        Measured (differs from the `$(... \\` convention elsewhere in
        this file, SF3-3): for a multi-line PIPELINE, BASH_LINENO
        reports the line the failing pipeline ELEMENT's own token
        starts on (here, where `acl_e2e_kc create` itself appears),
        not the statement's last physical line."""
        lines = script_text.splitlines()
        matches = [
            i for i, ln in enumerate(lines, start=1) if "acl_e2e_kc create \\" in ln
        ]
        assert len(matches) == 1, (
            f"expected exactly one 'acl_e2e_kc create \\' line, found {matches}"
        )
        return matches[0]

    @pytest.mark.parametrize("site", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_unguarded_mid_function_failure_at_top_level_needs_errtrace(
        self, tmp_path: Path, site: Path
    ) -> None:
        srv, port = _listening_socket()
        try:
            h = RunFullHarness(tmp_path, site)
            script_text = h.build_script(tcp_port=port)
            expected_line = self._ensure_mapper_create_call_line(script_text)
            state = {
                **_run_full_state(scopes_present=True, e2e_present=False),
                "fail_match": [
                    {
                        "substr": "create clients/preexisting-1/protocol-mappers/models",
                        "rc": 1,
                        "stderr": "boom",
                    }
                ],
            }
            proc = h.run(state=state, tcp_port=port)
            assert proc.returncode != 0
            assert "code=unguarded-failure" in proc.stdout, proc.stdout
            assert "stage=mapper-ensure" in proc.stdout
            assert f"line={expected_line}" in proc.stdout, proc.stdout
            assert "cmd=acl_e2e_kc create" in proc.stdout
        finally:
            srv.close()


class TestDynamicKcadmWrapperMarker:
    def test_a_bare_call_on_an_executed_path_fails_loudly(
        self, harness: StepHarness
    ) -> None:
        """Plant a bare call (bypassing acl_e2e_kc) on an executed path
        and show it reddens with code=unwrapped-call, never a silent
        pass (F3 §1.1's own falsifiability demand on itself)."""
        script = harness._script_text(clients=[], enabled="true")
        # Insert a bare, unwrapped invocation of the fake right before
        # the lookup helper is defined — this exercises the SAME marker
        # check the wrapper relies on, proving the fake's own rule
        # fires when the marker is truly absent.
        bare_call = f'"{harness.fake}" get clients -r audittrace -q "clientId=x" --fields id --format csv --noquotes\n'
        script = script.replace(
            "acl_e2e_lookup() {", bare_call + "acl_e2e_lookup() {", 1
        )
        env = {
            **os.environ,
            "FAKE_STATE": str(harness.state_path),
            "FAKE_LOG": str(harness.log),
        }
        harness.state_path.write_text(json.dumps({"clients": [], "scopes": []}))
        proc = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30
        )
        assert proc.returncode != 0
        assert "code=unwrapped-call" in proc.stdout
        assert "rc=97" in proc.stdout

    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_startup_unset_is_load_bearing_against_an_inherited_marker(
        self, tmp_path: Path, path: Path
    ) -> None:
        """R7: the startup ``unset ACL_E2E_VIA_KC ACL_E2E_CALL_LINE ||
        true`` is proven here, not merely present. A caller (CI runner,
        parent script) that already exported ``ACL_E2E_VIA_KC=1`` into
        THIS process's environment, before this script even starts,
        would make the very first bare/unwrapped call falsely read as
        "present" — defeating the F3/F4 proof for exactly the call it
        exists to catch. With the ``unset`` line removed (the neuter),
        that is exactly what happens."""
        h = StepHarness(tmp_path, path)
        h.state_path.write_text(json.dumps({"clients": [], "scopes": []}))
        bare_call = (
            f'"{h.fake}" get clients -r audittrace -q "clientId=x" '
            "--fields id --format csv --noquotes\n"
        )
        env = {
            **os.environ,
            "FAKE_STATE": str(h.state_path),
            "FAKE_LOG": str(h.log),
            "ACL_E2E_VIA_KC": "1",  # simulates an externally-inherited marker
        }

        def _run(unset_removed: bool) -> subprocess.CompletedProcess[str]:
            script = h._script_text(clients=[], enabled="true")
            needle = "unset ACL_E2E_VIA_KC ACL_E2E_CALL_LINE || true\n"
            assert script.count(needle) == 1
            if unset_removed:
                script = script.replace(needle, "", 1)
            script = script.replace(
                "acl_e2e_lookup() {", bare_call + "acl_e2e_lookup() {", 1
            )
            return subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )

        normal = _run(unset_removed=False)
        assert "code=unwrapped-call" in normal.stdout
        assert "rc=97" in normal.stdout

        neutered = _run(unset_removed=True)
        assert "code=unwrapped-call" not in neutered.stdout


# ─────────────────────────────────────────────────────────────────────
# F3 §1.2 (gate SF3-1 fix) — the shape-complete static pin
# ─────────────────────────────────────────────────────────────────────

_SEPARATOR_RE = re.compile(r";|&&|\|\||\|")
_SINK_RE = re.compile(r"^\s*(?:echo|printf|acl_e2e_fail)\b")
# R1 (review): `bash -c`/`sh -c` AND `-ec`/`-ce`/any bundled short-option
# ending in `c` (`bash -euc`, `bash -ec`, ...), single OR double quoted.
_EVAL_OR_DASH_C_RE = re.compile(
    r"\b(?:eval|(?:bash|sh)\s+-[a-zA-Z]*c)\s+"
    r"(?:'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\")"
)
_KCADM_ERE = re.compile(
    r"(?:^|[^A-Za-z0-9_])(\$\{?KCADM\}?|kcadm(?:\.sh)?)(?:[^A-Za-z0-9_.\-]|$)"
)
# A quoted literal that is CODE, not text (F4 addendum point 3): the
# first word of a (sub)statement, or the right-hand side of a bare
# `NAME=` assignment (a path smuggled into a variable).
#
# K2/K13 re-review (2026-10-07): the first version required the NAME=
# to sit IMMEDIATELY at `^`/a separator, with no `\s*` in between — so
# an INDENTED assignment (e.g. the ConfigMap site's own 6-space body
# indentation, `      K='...'`) never matched at all, catching the
# shape only at the host script's zero-indent site. Both alternatives
# now explicitly allow leading whitespace before the name.
_COMMAND_OR_ASSIGNMENT_POSITION_RE = re.compile(
    r"(?:^|[;&|(`])\s*$"
    r"|(?:^|[;&|(`])\s*[A-Za-z_][A-Za-z0-9_]*=\s*$"
)


def _unwrap_eval_and_dash_c(line: str) -> str:
    def _sub(m: re.Match[str]) -> str:
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        return " " + inner + " "

    return _EVAL_OR_DASH_C_RE.sub(_sub, line)


def _is_command_or_assignment_position(prefix: str) -> bool:
    """True if ``prefix`` (everything already emitted on this line,
    left of the quote about to be examined) ends exactly at a place
    where the NEXT token is a command word or a bare assignment's
    value — never an ordinary argument."""
    if prefix.strip() == "":
        return True
    return bool(_COMMAND_OR_ASSIGNMENT_POSITION_RE.search(prefix))


def _tokenize_quotes(line: str) -> str:
    """Blank every $-free double-quoted literal and every single-quoted
    literal, tracking quote state CHARACTER BY CHARACTER so a closing
    quote can never pair with a later literal's opening quote (the F3
    SF3-1 defect in the withdrawn regex stripper).

    F4 addendum point 3 (R1): a quoted literal in COMMAND position —
    or on the right-hand side of a bare ``NAME=`` assignment — is CODE,
    never text, and is never blanked even when it is $-free
    (``"/opt/…/kcadm.sh"``, ``"kcadm.sh"``, ``K='…/kcadm.sh'``). An
    ANSI-C-quoted literal (``$'kcadm'``) is never blanked either,
    regardless of position — it is rare enough in real scripts that
    always treating it as code is the fail-closed default."""
    out = []
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c == "'":
            dollar_prefixed = i > 0 and line[i - 1] == "$"
            j = line.find("'", i + 1)
            j = n - 1 if j == -1 else j
            seg = line[i : j + 1]
            prefix = "".join(out)[:-1] if dollar_prefixed else "".join(out)
            if dollar_prefixed or _is_command_or_assignment_position(prefix):
                out.append(seg)
            else:
                out.append(" " * len(seg))
            i = j + 1
            continue
        if c == '"':
            j = line.find('"', i + 1)
            j = n - 1 if j == -1 else j
            seg = line[i : j + 1]
            prefix = "".join(out)
            if "$" in seg or _is_command_or_assignment_position(prefix):
                out.append(seg)
            else:
                out.append(" " * len(seg))
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _cut_message_sinks(line: str) -> str:
    """A message sink (echo/printf/acl_e2e_fail) is cut from its own
    command token up to (not including) the next ``;``/``&&``/``||``/
    ``|`` — never further; a sink with none left consumes the rest."""
    bounds = []
    pos = 0
    for m in _SEPARATOR_RE.finditer(line):
        bounds.append((pos, m.start(), m.end()))
        pos = m.end()
    bounds.append((pos, len(line), len(line)))
    pieces = []
    for start, end, next_pos in bounds:
        segment = line[start:end]
        pieces.append(" " * len(segment) if _SINK_RE.match(segment) else segment)
        pieces.append(line[end:next_pos])
    return "".join(pieces)


def _prefilter(text: str) -> str:
    text = _strip_wrapper_blocks(text)
    out_lines = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        line = _unwrap_eval_and_dash_c(line)
        line = _tokenize_quotes(line)
        line = _cut_message_sinks(line)
        out_lines.append(line)
    return "\n".join(out_lines)


def _kcadm_pin_hits(text: str) -> int:
    return len(_KCADM_ERE.findall(_prefilter(text)))


class TestStaticKcadmPrefilterPin:
    """F3 §1.2, fixed per gate SF3-1: quote-state tokenizer, sink cut at
    the next separator (not to end of line), eval/-c arguments as code."""

    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_correct_build_has_zero_hits_outside_the_wrapper(self, path) -> None:
        assert _kcadm_pin_hits(path.read_text(encoding="utf-8")) == 0, path.name

    @pytest.mark.parametrize(
        ("label", "shape"),
        [
            ("quoted-braced", '"${KCADM}" get clients -r "${REALM}"'),
            ("unquoted-braced", '${KCADM} get clients -r "${REALM}"'),
            ("quoted-bare-var", '"$KCADM" get clients -r "${REALM}"'),
            ("unquoted-bare-var", '$KCADM get clients -r "${REALM}"'),
            ("bare-kcadm", 'kcadm get clients -r "${REALM}"'),
            ("kcadm.sh", 'kcadm.sh get clients -r "${REALM}"'),
            ("full-path", '/opt/keycloak/bin/kcadm.sh get clients -r "${REALM}"'),
            ("alias-variable", 'K="$KCADM"'),
        ],
    )
    def test_every_planted_shape_reddens(self, label: str, shape: str) -> None:
        assert _kcadm_pin_hits(shape) >= 1, label

    @pytest.mark.parametrize(
        ("label", "shape"),
        [
            ("echo-then-semicolon", 'echo "x"; "${KCADM}" get clients -r "${REALM}"'),
            ("echo-then-and", 'echo "x" && "${KCADM}" get clients -r "${REALM}"'),
            (
                "guarded-echo-then-semicolon",
                'foo || echo "w"; "${KCADM}" get clients -r "${REALM}"',
            ),
            (
                "guarded-fail-then-semicolon",
                'x || acl_e2e_fail "y"; "${KCADM}" delete clients -r "${REALM}"',
            ),
            ("eval-single-quoted", 'eval \'"${KCADM}" get clients -r "${REALM}"\''),
            (
                "timeout-bash-dash-c",
                'timeout 5 bash -c \'"${KCADM}" get clients -r "${REALM}"\'',
            ),
            (
                "bare-kcadm-between-two-quoted-args",
                'printf \'%s\' "${ACL_E2E_CLIENT_JSON}" | kcadm create clients -r "${REALM}" -f -',
            ),
        ],
    )
    def test_shapes_that_beat_the_withdrawn_regex_are_now_caught(
        self, label: str, shape: str
    ) -> None:
        assert _kcadm_pin_hits(shape) >= 1, label

    @pytest.mark.parametrize(
        ("label", "shape"),
        [
            (
                "quoted-full-path-command-position",
                '"/opt/keycloak/bin/kcadm.sh" get clients -r "${REALM}"',
            ),
            (
                "quoted-kcadm.sh-command-position",
                '"kcadm.sh" get clients -r "${REALM}"',
            ),
            (
                "single-quoted-path-in-a-variable",
                "K='/opt/keycloak/bin/kcadm.sh'",
            ),
            (
                "double-quoted-bare-name-in-a-variable",
                'K="kcadm.sh"',
            ),
            (
                "bash-dash-ec-double-quoted",
                'timeout 5 bash -ec "\\"${KCADM}\\" get clients -r \\"${REALM}\\""',
            ),
            (
                "ansi-c-quoted-kcadm",
                "$'kcadm' get clients -r \"${REALM}\"",
            ),
        ],
    )
    def test_f4_point_3_quoted_literal_and_dynamic_shapes_are_caught(
        self, label: str, shape: str
    ) -> None:
        """Addendum F4 point 3 (review R1): a quoted literal in COMMAND
        position — or the right-hand side of a bare ``NAME=``
        assignment — is CODE, not text, and must never be hidden by
        the ``$``-free-quote blanking rule; ``bash -ec``/ANSI-C
        ``$'kcadm'`` quoting must also be unwrapped."""
        assert _kcadm_pin_hits(shape) >= 1, label

    @pytest.mark.parametrize(
        ("label", "shape"),
        [
            ("K1", '"/opt/keycloak/bin/kcadm.sh" get clients -r "${REALM}"'),
            (
                "K2",
                'K=\'/opt/keycloak/bin/kcadm.sh\'; "$K" get clients -r "${REALM}"',
            ),
            ("K3", "bash -ec 'kcadm.sh get clients -r \"${REALM}\"'"),
            ("K4", "$'kcadm' get clients -r \"${REALM}\""),
            ("K5", '"kcadm.sh" get clients -r "${REALM}"'),
            ("K13", 'K="/opt/keycloak/bin/kcadm.sh"'),
        ],
    )
    def test_reviewers_exact_k_shapes_from_2026_10_07_review_are_caught(
        self, label: str, shape: str
    ) -> None:
        """Re-run of the independent review's own named K1-K5/K13 static
        -pin beats (2026-10-07 verdict, R1), verbatim — not just my own
        paraphrase of each shape."""
        assert _kcadm_pin_hits(shape) >= 1, label

    @pytest.mark.parametrize(
        ("label", "shape"),
        [
            (
                "K2-site4-indented",
                '      K=\'/opt/keycloak/bin/kcadm.sh\'; "$K" get clients -r "${REALM}"',
            ),
            ("K13-site4-indented", '      K="/opt/keycloak/bin/kcadm.sh"'),
        ],
    )
    def test_k2_k13_caught_at_the_configmap_sites_own_indentation(
        self, label: str, shape: str
    ) -> None:
        """R1 re-review: K2/K13 were measured caught at site 5 (zero
        indent) only — the first version of the command-position regex
        required the assignment's NAME= to sit IMMEDIATELY at line-start
        with no whitespace in between, so the ConfigMap site's own
        6-space body indentation defeated it. Both alternatives now
        explicitly allow leading whitespace."""
        assert _kcadm_pin_hits(shape) >= 1, label

    @pytest.mark.parametrize(
        ("label", "shape"),
        [
            ("quoted-arg-not-command-position", '-r "audittrace"'),
            (
                "quoted-json-body-argument",
                "printf '%s' \"${ACL_E2E_CLIENT_JSON}\" | acl_e2e_kc create clients -f -",
            ),
        ],
    )
    def test_quoted_arguments_are_still_text_not_flagged(
        self, label: str, shape: str
    ) -> None:
        """The command-position carve-out must stay NARROW: an
        ordinary quoted ARGUMENT (never the command word, never a bare
        assignment's value) stays text and is not a false positive."""
        assert _kcadm_pin_hits(shape) == 0, label

    def test_a_comment_echo_message_and_printf_literal_are_not_false_positives(
        self,
    ) -> None:
        text = (
            "# this comment mentions kcadm and ${KCADM} too\n"
            'echo "calling kcadm.sh now, kcadm rocks"\n'
            "printf 'about to run \\${KCADM} for real'\n"
        )
        assert _kcadm_pin_hits(text) == 0

    def test_a_message_only_line_with_no_trailing_separator_is_fully_dropped(
        self,
    ) -> None:
        assert _kcadm_pin_hits('echo "kcadm failed somehow"\n') == 0


# ─────────────────────────────────────────────────────────────────────
# AC-F14 / gate S2-5 — acl_e2e_fail placement pin
# ─────────────────────────────────────────────────────────────────────

_ALLOWED_FAIL_HOMES = (
    "acl_e2e_compact",
    "acl_e2e_in_default_set",
    "acl_e2e_lookup",
    "acl_e2e_scope_names",
    "acl_e2e_strip_scopes",
    "acl_e2e_verify",
)


_FAIL_CALL_RE = re.compile(r"(?:^|[^A-Za-z0-9_])acl_e2e_fail\s+\S")
# R7/neuter-table B2: the ``function name { ... }`` keyword form is a
# SECOND way bash lets you define a function — the ``name() {`` form
# alone let a rogue function defined this other way go unrecognized
# (current_fn never updated -> read as "still top level").
_FN_DEF_RE = re.compile(
    r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(\))?\s*\{\s*$"
)
_HANDLER_FNS = (
    "acl_e2e_mask",
    "acl_e2e_emit",
    "acl_e2e_report",
    "acl_e2e_fail",
    "acl_e2e_on_err",
    "acl_e2e_on_exit",
    "acl_e2e_on_signal",
    "acl_e2e_kc",
    "acl_e2e_shape_line",
    "acl_e2e_shape_capture",
)


def _bare_paren_delta(tokenized_line: str, stack: list[str]) -> int:
    """Net change in BARE ``( )`` subshell-group depth contributed by
    one tokenized (quote-blanked) line — ``$(`` command substitution
    openers excluded (those are the already-handled F2 BF1-b case),
    everything else counted so a group spanning MULTIPLE lines is
    still visible at the line the call sits on (R7/neuter B3).

    ``stack`` is owned by and PERSISTS across lines in the caller
    (measured regression — an earlier version used a stack local to
    this function, so a ``$(...)``/array-literal opener on one line
    could never be matched to its closer on a LATER line; the closer
    then silently no-op'd against an empty stack instead of popping,
    so a bash array literal like ``NAME=(\\n  "a"\\n)`` permanently
    stair-stepped the depth upward by +1 per array, 22 arrays deep)."""
    depth = 0
    for i, c in enumerate(tokenized_line):
        if c == "(":
            if i > 0 and tokenized_line[i - 1] == "$":
                stack.append("d")
            else:
                stack.append("b")
                depth += 1
        elif c == ")":
            if stack:
                if stack.pop() == "b":
                    depth -= 1
            # An unmatched ")" (case-arm terminator this file's own
            # case-awareness didn't suppress, or similar) is ignored
            # rather than driving depth further negative.
    return depth


def _acl_e2e_fail_misplaced(text: str) -> list[str]:
    """Every ACTUAL CALL to ``acl_e2e_fail`` (not its own definition,
    not a comment mentioning it) must sit (i) at top level or inside
    one of the allow-listed helpers, and (ii) never after a ``|``, nor
    before one (B1: its own stdout piped into something else), nor
    inside a ``( )`` group — including one opened on an EARLIER line
    (B3) — and (iii) a rogue function defined via EITHER the
    ``name() {`` or the ``function name {`` keyword form (B2) must be
    recognized as a new, non-allow-listed scope."""
    stripped = _strip_wrapper_blocks(text)
    lines = stripped.splitlines()
    misplaced = []
    current_fn = None
    paren_depth = 0
    paren_stack: list[str] = []
    case_depth = 0
    for line in lines:
        m = _FN_DEF_RE.match(line)
        if m:
            current_fn = m.group(1)
            continue
        if line.strip() == "}":
            current_fn = None
            continue
        if line.lstrip().startswith("#"):
            continue
        # `case ... in` / `esac`: a bare `pattern)` arm terminator is a
        # CLOSING paren with no matching opener — left uncorrected, it
        # drives the cumulative bare-paren counter NEGATIVE over the
        # whole file (measured: this hid a planted B3 group appended
        # after the real script's own `case` blocks, since the running
        # depth never climbs back above the B3 group's own `+1`).
        # Bare-paren tracking is suspended entirely inside a case block
        # — this corpus never nests a real `( )` subshell group inside
        # a case arm.
        if re.match(r"^\s*case\b.*\bin\s*$", line):
            case_depth += 1
        elif re.match(r"^\s*esac\b", line):
            case_depth = max(0, case_depth - 1)
        line_start_paren_depth = paren_depth
        tokenized_whole_line = _tokenize_quotes(line)
        if case_depth == 0:
            paren_depth += _bare_paren_delta(tokenized_whole_line, paren_stack)
        if not _FAIL_CALL_RE.search(line):
            continue
        bare = tokenized_whole_line
        before_call = bare.split("acl_e2e_fail", 1)[0]
        after_call = bare.split("acl_e2e_fail", 1)[1]
        if "(" in before_call and ")" not in before_call.split("(", 1)[1]:
            misplaced.append(line)
            continue
        if line_start_paren_depth > 0:
            misplaced.append(line)
            continue
        stripped_before = before_call.rstrip()
        # A single trailing "|" is a real pipeline (bad); "||" is the
        # SANCTIONED logical-or guard (`cmd || acl_e2e_fail ...`) and
        # must not be confused with it.
        if stripped_before.endswith("|") and not stripped_before.endswith("||"):
            misplaced.append(line)
            continue
        # B1: the call's OWN stdout piped into something else (never
        # sanctioned — `acl_e2e_fail` always exits, so anything after
        # a real `|` here would exist only to consume its output).
        if "|" in after_call:
            misplaced.append(line)
            continue
        if (
            current_fn is not None
            and current_fn not in _ALLOWED_FAIL_HOMES
            and current_fn not in _HANDLER_FNS
        ):
            misplaced.append(line)
    return misplaced


class TestToolBootstrapChecksAreBehavioral:
    """R7: AC-F11's tool checks were proven only by TEXT PRESENCE/
    ORDERING (``test_tool_checks_precede_any_mktemp_or_timeout_use``)
    — dead code that always passes even if the ``if`` conditions never
    actually fire. These run the REAL preamble text with a PATH that
    genuinely lacks the tool, proving the check is live."""

    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_timeout_missing_exits_with_the_named_code(self, path: Path) -> None:
        preamble = _preamble_text(path)
        # Nothing the preamble itself runs needs an external binary
        # before the `timeout` check (command/printf/exit are bash
        # builtins) — an EMPTY PATH is sufficient and simplest. Routed
        # through `hermetic_env()` (test-parity ADDENDUM A2+) with an
        # EMPTY base: under the real CI shape this still yields
        # `PATH` == the frozen sentinel shim dir alone (which carries
        # no `timeout` binary either, so the test's intent — `timeout`
        # genuinely unreachable — holds); outside the CI shape (no
        # sentinel frozen) `hermetic_env()` adds nothing, and
        # `setdefault` restores the exact original `{"PATH": ""}`.
        env = hermetic_env(base={})
        env.setdefault("PATH", "")
        proc = subprocess.run(
            [_BASH, "-c", preamble],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert proc.returncode == 1
        assert "code=timeout-missing" in proc.stdout
        assert "code=timeout-missing" in proc.stderr

    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_mktemp_missing_exits_with_the_named_code(
        self, tmp_path: Path, path: Path
    ) -> None:
        preamble = _preamble_text(path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "timeout").symlink_to(shutil.which("timeout"))
        # Routed through `hermetic_env()`: under the real CI shape this
        # puts the frozen sentinel shim dir FIRST on PATH (satisfying
        # the guard's env-equality check) with `bin_dir` right after —
        # `timeout` (symlinked there) stays reachable, `mktemp` stays
        # absent from both, so the test's intent is unchanged. Outside
        # the CI shape, `hermetic_env()` is a no-op and this is exactly
        # the original `{"PATH": str(bin_dir)}`.
        env = hermetic_env(base={"PATH": str(bin_dir)})
        proc = subprocess.run(
            [_BASH, "-c", preamble],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert proc.returncode == 1
        assert "code=mktemp-missing" in proc.stdout
        assert "code=mktemp-missing" in proc.stderr

    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_both_present_clears_the_checks(self, tmp_path: Path, path: Path) -> None:
        """The differential half: with both tools genuinely on PATH,
        the preamble must run PAST both checks (reach ``set -E``,
        exit 0) — proving the two RED cases above are caused by the
        tool's absence, not by some unrelated preamble breakage."""
        preamble = _preamble_text(path)
        proc = subprocess.run(
            ["bash", "-c", preamble + "\necho PREAMBLE-OK\n"],
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            timeout=10,
        )
        assert proc.returncode == 0, proc.stderr
        assert "PREAMBLE-OK" in proc.stdout
        assert "timeout-missing" not in proc.stdout
        assert "mktemp-missing" not in proc.stdout


class TestTcpConnectAttemptIsBounded:
    """R7: the ConfigMap-only tcp-wait loop's ``timeout 5 bash -c "exec
    3<>/dev/tcp/..."`` must genuinely bound a connect attempt to ~5s —
    proven by connecting to a non-routable address (RFC 5737
    TEST-NET-1, guaranteed to never ACK/RST, so the OS's own SYN-retry
    timeout would otherwise run to its default ~130s)."""

    def test_unreachable_host_is_bounded_to_five_seconds(self) -> None:
        """Extracts the REAL statement from the ConfigMap source
        (never a hand-typed stand-in, which would be invisible to a
        neuter removing the production `timeout 5` — measured: an
        earlier version of this test used its own literal command and
        a `timeout 5` removal in the source went completely unnoticed,
        review neuter H)."""
        text = CONFIGMAP.read_text(encoding="utf-8")
        needle = (
            'timeout 5 bash -c "exec 3<>/dev/tcp/${KC_HOST}/${KC_PORT}" 2>/dev/null'
        )
        assert text.count(needle) == 1
        stmt = needle.replace("${KC_HOST}", "192.0.2.1").replace("${KC_PORT}", "80")
        start = time.monotonic()
        proc = subprocess.run(
            ["bash", "-c", stmt],
            capture_output=True,
            text=True,
            timeout=20,
        )
        elapsed = time.monotonic() - start
        assert proc.returncode != 0
        assert elapsed < 10, f"took {elapsed}s — the 5s bound did not hold"


class TestAclE2eFailPlacementPin:
    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_no_call_is_misplaced_on_the_real_tree(self, path) -> None:
        text = path.read_text(encoding="utf-8")
        assert _acl_e2e_fail_misplaced(text) == [], path.name

    def test_a_pipeline_element_call_is_flagged(self) -> None:
        text = 'true | acl_e2e_fail code "x"\n'
        assert _acl_e2e_fail_misplaced(text) != []

    def test_a_call_inside_a_new_function_not_on_the_allow_list_is_flagged(
        self,
    ) -> None:
        text = 'rogue_helper() {\n  acl_e2e_fail code "x"\n}\n'
        assert _acl_e2e_fail_misplaced(text) != []

    def test_b1_the_calls_own_stdout_piped_into_something_else_is_flagged(
        self,
    ) -> None:
        """Neuter-table B1: the ORIGINAL pin only checked for a pipe
        feeding INTO the call (B above) — never one consuming the
        call's OWN output, which would just as badly corrupt the
        emission."""
        text = 'acl_e2e_fail code "x" | cat\n'
        assert _acl_e2e_fail_misplaced(text) != []

    def test_b2_a_function_keyword_defined_rogue_scope_is_flagged(self) -> None:
        """Neuter-table B2: ``function name { ... }`` is a second,
        equally valid way to define a function — the original pin's
        ``_FN_DEF_RE`` matched ONLY the ``name() {`` form, so a rogue
        scope defined this other way read as "still top level"."""
        text = 'function rogue_helper {\n  acl_e2e_fail code "x"\n}\n'
        assert _acl_e2e_fail_misplaced(text) != []

    def test_b3_a_multi_line_paren_group_opened_on_an_earlier_line_is_flagged(
        self,
    ) -> None:
        """Neuter-table B3: the original pin only looked for an
        unclosed ``(`` on the CALL's OWN line — a bare ``( ... )``
        subshell group opened several lines earlier, with the call
        further down inside it, was invisible to a per-line-only
        check. Uses a REAL, valid code (``drift``) — the review's own
        re-review measured that an INVALID planted code ("x") only
        reddened via the UNRELATED code-set-equality test when this
        shape was run through the full battery, not via the placement
        pin itself; a valid code isolates the pin's own property."""
        text = '(\n  echo preamble\n  acl_e2e_fail drift "y"\n)\n'
        assert _acl_e2e_fail_misplaced(text) != []

    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_b3_planted_in_the_real_tree_reddens(self, path) -> None:
        """Same shape, planted into the REAL file text (matching the
        re-review's own methodology) — must turn the real-tree
        placement-pin check RED on its own, independent of any other
        test."""
        text = path.read_text(encoding="utf-8")
        planted = text + '\n(\n  echo preamble\n  acl_e2e_fail drift "y"\n)\n'
        assert _acl_e2e_fail_misplaced(planted) != []


# ─────────────────────────────────────────────────────────────────────
# AC-F2 (amended, SF3-5) — secret hygiene
# ─────────────────────────────────────────────────────────────────────


def _collapse_backslash_continuations(text: str) -> str:
    """R6: an obfuscated multi-line split (``set \\`` + newline +
    ``-x``) must read identically to the single-line form before any
    forbidden-shape check runs."""
    return re.sub(r"\\\n[ \t]*", " ", text)


def _code_statements(text: str) -> list[tuple[list[str], str]]:
    """Comments stripped, continuations collapsed, quoted literals
    blanked (so a separator inside a string never splits a statement),
    then split into ``(words, raw_statement)`` pairs on every
    statement separator — a TOKENIZED check (R6), never a single
    whole-text regex that a comment or a quoted string could dodge or
    that a backslash-split command could beat."""
    code_lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    code_only = _collapse_backslash_continuations("\n".join(code_lines))
    masked = _tokenize_quotes(code_only)
    out = []
    pos = 0
    # Capturing the separator keeps `raw` aligned regardless of its
    # length (1 char for `;`/`|`/newline, 2 for `&&`/`||`).
    pieces = re.split(r"(;|&&|\|\||\||\n)", masked)
    for part in pieces:
        if part in (";", "&&", "||", "|", "\n"):
            pos += len(part)
            continue
        raw = code_only[pos : pos + len(part)]
        pos += len(part)
        words = part.split()
        if words:
            out.append((words, raw))
    return out


# F4 §5 / R4 (2026-10-07 re-review #3): the CLOSED set of variables a
# `detail=` text may ever expand — every one is a CONSTANT or a COUNT,
# never a kcadm/Keycloak response VALUE. `2` covers the bare positional
# `$2` in `acl_e2e_scope_names` (the caller-supplied "default"/
# "optional" literal). Anything else appearing in an `acl_e2e_fail`
# call's detail text is, by construction, a new unreviewed vector.
_DETAIL_VAR_ALLOWLIST = frozenset({"kind", "ACL_E2E_CLIENT_ID", "diffs", "2"})

_ACL_E2E_FAIL_CALL_RE = re.compile(r'acl_e2e_fail\s+[a-z][a-z-]*\s+"((?:[^"\\]|\\.)*)"')
_DETAIL_EXPANSION_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*|\d+)\}?")


def _acl_e2e_fail_detail_disallowed_expansions(text: str) -> list[tuple[str, str]]:
    """Mechanically parses ONLY the shape
    ``acl_e2e_fail <code> "<detail>"`` — the first, single,
    double-quoted argument following the code word — and returns
    ``(detail_text, bad_variable)`` for every ``$``/``${...}``
    expansion in THAT text whose name is not in
    :data:`_DETAIL_VAR_ALLOWLIST`.

    NOT covered by this parser (2026-10-07 re-review #4 measured 6
    beating shapes, all planted in the real tree and left GREEN): a
    2nd positional argument (``"cannot read" "$1"``), an UNQUOTED
    detail word, a backslash-continued code/detail split across
    lines, a DIRECT ``acl_e2e_emit`` call (bypassing `acl_e2e_fail`
    entirely), concatenated-quote splicing (``'x'"$1"``), or an
    allowlisted NAME whose own assignment is re-bound to a response
    (that specific shape is instead covered by the separate provenance
    check, :func:`_detail_allowlist_names_only_bind_to_literals`).
    Only the real ``acl_e2e_fail`` call sites in the shipped script
    ever use this exact single-double-quoted-argument form (confirmed
    by the real-tree test below), which is why this narrow parser
    still has load-bearing value — it is not a "closed" class guard on
    its own. The RUNTIME plant test
    (``test_f2h_acl_e2e_compact_never_leaks_the_lookup_responses_own_id``
    in ``TestNamedFailureLineOnEveryExit``) is the guard that actually
    exercises the shipped mechanism end-to-end and is not fooled by
    shape; THIS parser is a narrower, faster, STATIC complement to it,
    not a replacement."""
    stripped = _strip_wrapper_blocks(text)
    violations = []
    for m in _ACL_E2E_FAIL_CALL_RE.finditer(stripped):
        detail = m.group(1)
        for var_m in _DETAIL_EXPANSION_RE.finditer(detail):
            name = var_m.group(1)
            if name not in _DETAIL_VAR_ALLOWLIST:
                violations.append((detail, name))
    return violations


# R4 re-review #4, item (b); re-review #5 (2026-10-07) measured this
# check's actual coverage and ruled it a HEURISTIC, not a proof — the
# REAL class guards are the runtime plant test
# (``test_f2h_acl_e2e_compact_never_leaks_the_lookup_responses_own_id``)
# and the 6 provisioner-drift tests in
# ``tests/test_acl_e2e_provisioner_script.py``, which exercise the
# shipped mechanism end-to-end and are not fooled by source-level
# shape. This static check catches the ONE shape it was built to
# catch (a `kind`/`ACL_E2E_CLIENT_ID` rebind via `for`/direct-assign/
# `read`, or a `$2` caller passing a non-literal) and is a faster,
# narrower, STATIC complement to those runtime guards — it is NOT a
# closed proof that every allowlisted NAME can never carry a
# response. Measured KNOWN GAPS (re-review #5):
#   - `printf -v kind ...` / `printf -v ACL_E2E_CLIENT_ID ...`, a
#     `read ... ACL_E2E_CLIENT_ID` assignment, and a SECOND
#     `for kind in ...`/`for item in ...` loop nested inside the first
#     (`_for_loop_literal_words` only inspects the first such loop per
#     name) — NONE of these are covered by any runtime test today
#     either (no fixture plants that specific shape); they are open
#     risk surface, mitigated only by the current production code not
#     using them.
#   - `diffs` itself — `local diffs=$x` or a `diffs+=" x:${name}"`
#     append of a response-derived scope/mapper NAME — which this
#     check never reads (only `item` and the five integer counters
#     feeding separate appends are). Re-review #5's OWN measurement:
#     this specific, NAME-shaped reachable leak IS caught at runtime
#     by the 6 provisioner-drift tests (they plant a response-derived
#     scope/mapper name and assert it absent from `diffs`'s own
#     output). Re-review #6 (2026-10-08) measured a SEPARATE shape —
#     a rebind/append of the client's own response-derived UUID (not
#     a name) — as GREEN at that same runtime suite; fix round 6 adds
#     2 dedicated tests (`TestDiffsRuntimeAttributionOfTheClientUuid`,
#     `tests/test_acl_e2e_provisioner_script.py`) that close that
#     specific gap. No OTHER response-derived value shape is claimed
#     covered.
_LITERAL_BAREWORD_RE = re.compile(r"^[A-Za-z0-9_:./@-]+$")
_COUNTER_INCREMENT_RE = re.compile(r"^\$\(\(\s*[A-Za-z_][A-Za-z0-9_]*\s*\+\s*1\s*\)\)$")
_DIFFS_COUNTER_NAMES = frozenset(
    {
        "n",
        "unexpected_default",
        "unexpected_optional",
        "unexpected_mapper",
        "unexpected_config",
    }
)


def _is_literal_or_counter_rhs(value: str) -> bool:
    value = value.strip()
    if value in ("0", '""', ""):
        return True
    if _COUNTER_INCREMENT_RE.match(value):
        return True
    # Single-quoted: bash never expands inside a '...' literal, so any
    # content is safe by construction, regardless of what it contains.
    if len(value) >= 2 and value.startswith("'") and value.endswith("'"):
        return True
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        inner = value[1:-1]
        return "$" not in inner and "`" not in inner
    return bool(_LITERAL_BAREWORD_RE.match(value))


_ARITH_RHS_RE = re.compile(r"\$\(\([^()]*\)\)")

# R4 re-review #6 (2026-10-08): the PRIOR version of this check
# anchored its regex on a REQUIRED double-quoted FIRST argument
# (`acl_e2e_scope_names\s+"[^"]*"\s+(...)`) — a caller whose first
# argument is unquoted (`acl_e2e_scope_names ${uuid} ${names}`) or
# single-quoted (`acl_e2e_scope_names 'u' "$names"`) was invisible to
# BOTH the per-caller literal check and the "exactly 2" count,
# measured GREEN by the re-review on both shapes. This replacement
# finds every `\bacl_e2e_scope_names\b` WORD occurrence first (not an
# argument-shape regex), excludes only the function's own definition
# line and comment lines, then tokenizes whatever follows with a
# quote-aware word splitter that accepts ANY quoting (or none) for
# either argument — unparseable text is a VIOLATION, not a skip.
_ACL_E2E_SCOPE_NAMES_WORD_RE = re.compile(r"\bacl_e2e_scope_names\b")
_ACL_E2E_SCOPE_NAMES_DEF_RE = re.compile(r"acl_e2e_scope_names\s*\(\)\s*\{")


def _tokenize_shell_words(text: str) -> list[str] | None:
    """Split `text` into shell words, respecting single- and
    double-quoted spans character-by-character (never regex-guessed),
    stopping at the first UNQUOTED `)`/`;`/`&`/`|`/newline (the usual
    terminators of a simple `$(fn arg1 arg2)` or bare `fn arg1 arg2`
    call). Returns ``None`` — a FAIL-CLOSED signal, not "no words" —
    if a quote is left unterminated before any such stop."""
    words: list[str] = []
    current: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t":
            if current:
                words.append("".join(current))
                current = []
            i += 1
            continue
        if c in ");&|\n":
            break
        if c in "'\"":
            j = text.find(c, i + 1)
            if j == -1:
                return None
            current.append(text[i : j + 1])
            i = j + 1
            continue
        current.append(c)
        i += 1
    if current:
        words.append("".join(current))
    return words


def _acl_e2e_scope_names_calls(text: str) -> list[tuple[str, list[str] | None]]:
    """Every CALL-SHAPED occurrence of the word `acl_e2e_scope_names`
    in `text`: excludes the function's own definition line and any
    line whose first non-whitespace character is `#`. For each
    remaining occurrence, returns ``(source_line, words_or_None)``,
    where `words` is the shell-tokenized argument list following the
    call on that SAME physical line (via
    :func:`_tokenize_shell_words`) — ``None`` means unparseable
    (fail-closed), never silently skipped."""
    calls = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if _ACL_E2E_SCOPE_NAMES_DEF_RE.search(line):
            continue
        for m in _ACL_E2E_SCOPE_NAMES_WORD_RE.finditer(line):
            calls.append((line, _tokenize_shell_words(line[m.end() :])))
    return calls


# Anchors on the REAL "optional" caller line to plant a neuter directly
# before it, capturing its own leading indentation (which differs
# between the standalone script and the YAML-block-scalar-indented
# ConfigMap copy) so the insertion lines up at either site.
_OPTIONAL_SCOPE_NAMES_LINE_RE = re.compile(
    r'^([ \t]*)names=\$\(acl_e2e_scope_names "\$\{uuid\}" optional\) \\\n', re.M
)


def _assignment_rhs_values(text: str, name: str) -> list[str]:
    """Every RHS `name` is ever bound to via a bare `name=value`
    assignment (found after `local`/`for`/anywhere else too — the
    regex only anchors on the preceding boundary, not the keyword).
    A `$((...))` arithmetic RHS (which legitimately contains spaces,
    e.g. ``$((n + 1))``) is captured whole, not truncated at its first
    internal space."""
    values = []
    for line in text.splitlines():
        for m in re.finditer(rf"(?:^|[\s;]){re.escape(name)}=", line):
            rest = line[m.end() :]
            arith = _ARITH_RHS_RE.match(rest)
            values.append(
                arith.group(0) if arith else rest.split()[0] if rest.split() else ""
            )
    return values


def _for_loop_literal_words(text: str, name: str) -> list[str] | None:
    """The word list bound by `for <name> in ...; do` (single- or
    multi-line, backslash-continued), tokenized respecting quotes —
    or ``None`` if no such loop exists for this name at all."""
    m = re.search(rf"for {re.escape(name)} in\b", text)
    if not m:
        return None
    collected = []
    for ln in text[m.end() :].splitlines():
        stop = re.search(r";\s*do\b", ln)
        if stop:
            collected.append(ln[: stop.start()])
            break
        collected.append(ln[:-1] if ln.rstrip().endswith("\\") else ln)
        if not ln.rstrip().endswith("\\"):
            break
    joined = " ".join(collected)
    return [wm.group(0) for wm in re.finditer(r"'[^']*'|\"[^\"]*\"|\S+", joined)]


def _function_body_or_whole(text: str, name: str) -> str:
    """The real function body named ``name`` (matching-brace
    extraction, same convention as ``_real_function_text``, but
    TEXT-based so synthetic test snippets that never define an actual
    function still work) — or the WHOLE text if no such function is
    defined in it. Scoping these searches to the right function body
    matters: the production script also has unrelated ``n=`` bindings
    (``awk -v n="${SCOPE}"``) outside `acl_e2e_verify` that a
    whole-file search would misattribute to the diffs counter."""
    marker = f"{name}() {{"
    if marker not in text:
        return text
    start = text.index(marker)
    line_start = text.rfind("\n", 0, start) + 1
    indent = text[line_start:start]
    closer = "\n" + indent + "}\n"
    end = text.index(closer, start) + len(closer)
    return text[start:end]


def _detail_allowlist_names_only_bind_to_literals(text: str) -> list[str]:
    """A HEURISTIC, not a proof (re-review #5): checks exactly these
    shapes and no others —

    - ``kind``: the word list of the FIRST ``for kind in ...; do``
      loop found in ``acl_e2e_strip_scopes`` (via
      :func:`_for_loop_literal_words`, which uses ``re.search`` and
      only ever inspects the first such loop — a second, nested
      ``for kind in ...`` would not be seen), any direct ``kind=``
      assignment, and a ``read ... kind`` grep. A ``printf -v kind
      ...`` assignment is NOT checked (measured GREEN by re-review
      #5).
    - ``ACL_E2E_CLIENT_ID``: only direct ``ACL_E2E_CLIENT_ID=``
      assignments. A ``read ... ACL_E2E_CLIENT_ID`` or
      ``printf -v ACL_E2E_CLIENT_ID ...`` assignment is NOT checked
      (measured GREEN by re-review #5).
    - the ``diffs`` ALLOWLIST ENTRY itself is never read here — only
      its building blocks ``item`` (a for-loop word list, same
      first-loop-only caveat as ``kind``) and the five integer
      counters (checked for ``0``/``$((counter + 1))`` self-increment
      only) are. A ``local diffs=$x`` rebind or a
      ``diffs+=" x:${name}"`` append of a RESPONSE-DERIVED SCOPE OR
      MAPPER NAME is NOT checked here (measured GREEN by re-review
      #5) — that specific, NAME-shaped reachable leak IS caught at
      RUNTIME by the 6 provisioner drift tests in
      ``tests/test_acl_e2e_provisioner_script.py``. A rebind/append of
      the client's own response-derived UUID (a DIFFERENT value, not a
      name) was measured by re-review #6 as a SEPARATE GREEN — now
      ALSO caught at runtime by the 2 dedicated tests
      ``test_diffs_rebind_with_the_client_uuid_reddens`` /
      ``test_diffs_append_with_the_client_uuid_reddens``
      (``TestDiffsRuntimeAttributionOfTheClientUuid``, same file) added
      in fix round 6. No OTHER response-derived value shape (if one
      existed) is claimed covered by either set of runtime tests.
    - ``acl_e2e_scope_names``'s 2nd positional argument (``$2``):
      every CALL found by a word-boundary match on
      ``acl_e2e_scope_names`` (excluding the function's own definition
      line and comment lines, via :func:`_acl_e2e_scope_names_calls`)
      must pass a literal 2nd argument, in ANY quoting (unquoted,
      single-, or double-quoted — re-review #6 measured the PRIOR
      version, which required a double-quoted FIRST argument, GREEN on
      both an unquoted and a single-quoted first argument); an
      unparseable call is itself a violation. The call COUNT must be
      exactly 2 (this script's own known call count), failing closed
      if it is not.

    Neuter (named by re-review #4):
    ``kind="$(acl_e2e_kc get clients)"`` must turn this RED."""
    stripped = _strip_wrapper_blocks(text)
    strip_scopes_body = _function_body_or_whole(stripped, "acl_e2e_strip_scopes")
    verify_body = _function_body_or_whole(stripped, "acl_e2e_verify")
    violations: list[str] = []

    # `kind`: bound ONLY by `for kind in <literal words>; do`, scoped
    # to `acl_e2e_strip_scopes` (the only place it is used).
    kind_words = _for_loop_literal_words(strip_scopes_body, "kind")
    if kind_words is None:
        violations.append("kind: no `for kind in ...` loop found")
    else:
        for w in kind_words:
            if not _is_literal_or_counter_rhs(w):
                violations.append(f"kind: non-literal for-loop word {w!r}")
    for rhs in _assignment_rhs_values(strip_scopes_body, "kind"):
        if not _is_literal_or_counter_rhs(rhs):
            violations.append(f"kind: non-literal direct assignment {rhs!r}")
    if re.search(r"\bread\b[^\n]*\bkind\b", strip_scopes_body):
        violations.append("kind: a `read` into kind was found")

    # `ACL_E2E_CLIENT_ID`: every direct assignment is a literal
    # (top-level, outside any function).
    client_id_rhss = _assignment_rhs_values(stripped, "ACL_E2E_CLIENT_ID")
    if not client_id_rhss:
        violations.append("ACL_E2E_CLIENT_ID: no assignment found")
    for rhs in client_id_rhss:
        if not _is_literal_or_counter_rhs(rhs):
            violations.append(f"ACL_E2E_CLIENT_ID: non-literal assignment {rhs!r}")

    # `diffs`'s own building blocks: `item` (a for-loop literal) and
    # the integer counters — each counter's OWN assignments must be
    # `0` or a `$((counter + 1))` self-increment, never anything else.
    # Both are scoped to `acl_e2e_verify`, the only function that uses
    # them — a whole-file search would also match unrelated bindings
    # like `awk -v n="${SCOPE}"` elsewhere in the script.
    item_words = _for_loop_literal_words(verify_body, "item")
    if item_words is None:
        violations.append("item: no `for item in ...` loop found")
    else:
        for w in item_words:
            if not _is_literal_or_counter_rhs(w):
                violations.append(f"item: non-literal for-loop word {w!r}")
    for counter in _DIFFS_COUNTER_NAMES:
        for rhs in _assignment_rhs_values(verify_body, counter):
            if not _is_literal_or_counter_rhs(rhs):
                violations.append(f"{counter}: non-literal assignment {rhs!r}")
    if re.search(
        r"\bread\b[^\n]*\b(?:" + "|".join(_DIFFS_COUNTER_NAMES) + r")\b", verify_body
    ):
        violations.append("a diffs counter: a `read` target was found")

    # `$2` positional (`acl_e2e_scope_names`'s 2nd argument): every
    # CALL found (word-boundary match, any quoting, per
    # :func:`_acl_e2e_scope_names_calls`) must pass a literal 2nd
    # argument, never a variable or substitution — and an UNPARSEABLE
    # call is a violation, not a skip. FAILS CLOSED on the call COUNT
    # too: this script has exactly 2 known calls per site
    # (`default`/`optional`); a count other than 2 means either a call
    # was ADDED (never inspected by this loop) or the finder itself
    # stopped matching an existing call — either way that must be a
    # visible violation, not a silently-smaller loop body.
    calls = _acl_e2e_scope_names_calls(stripped)
    if len(calls) != 2:
        violations.append(
            f"acl_e2e_scope_names: expected exactly 2 calls, found {len(calls)}"
        )
    for line, words in calls:
        if words is None:
            violations.append(f"acl_e2e_scope_names: unparseable call {line!r}")
            continue
        if len(words) < 2:
            violations.append(f"acl_e2e_scope_names: missing 2nd argument {line!r}")
            continue
        arg2 = words[1]
        if not _is_literal_or_counter_rhs(arg2):
            violations.append(f"acl_e2e_scope_names 2nd arg: non-literal {arg2!r}")

    return violations


class TestDetailNeverCarriesResponseContent:
    """R4 (2026-10-08 re-review #6 — orchestrator ruling carried from
    re-review #5: static enumeration of this shape has lost every
    round; the RUNTIME guards are the class guards, the static checks
    below are heuristics/complements, and prose may state only what
    an instrument measures):

    - ``test_every_acl_e2e_fail_detail_only_expands_the_allowlist`` is
      a STATIC PARSER over exactly one shape:
      ``acl_e2e_fail <code> "<detail>"`` (a single double-quoted
      argument). It does NOT parse `acl_e2e_emit`/`acl_e2e_report`
      directly, a 2nd positional argument, an unquoted detail,
      continuation-split code/detail, or concatenated-quote splicing
      — see :func:`_acl_e2e_fail_detail_disallowed_expansions`'s own
      docstring for the measured list of 6 shapes that beat it.
    - ``test_every_acl_e2e_fail_detail_allowlist_name_is_literal_or_counter``
      is a PROVENANCE HEURISTIC, not a proof (re-review #5/#6
      measured this): it catches a `kind`/`ACL_E2E_CLIENT_ID` rebind
      via the FIRST `for`/`local`/`read`-matching shape per name, and
      (after fix round 6's fix) every `acl_e2e_scope_names` CALL found
      by a word-boundary match, in any quoting of either argument,
      with the call count itself asserted exactly 2. It does NOT
      catch: a `printf -v kind`/`printf -v ACL_E2E_CLIENT_ID`
      assignment, a `read` into `ACL_E2E_CLIENT_ID`, a SECOND nested
      `for kind`/`for item` loop, or a `diffs` rebind/append of a
      value that is not a scope/mapper name — see
      :func:`_detail_allowlist_names_only_bind_to_literals`'s own
      docstring for the full measured gap list and which runtime
      guard covers each one.
    - The REAL, end-to-end class guards — exercising the shipped
      mechanism at runtime rather than pattern-matching source text —
      are ``test_f2h_acl_e2e_compact_never_leaks_the_lookup_responses_own_id``
      (`TestNamedFailureLineOnEveryExit`, this file), which plants a
      secret-shaped value in the lookup RESPONSE and asserts it is
      absent from the ACTUAL emitted line on both sites and both
      streams (acl_e2e_compact's own `$1`/response-derived-id leak,
      specifically); the 6 provisioner-drift tests in
      ``tests/test_acl_e2e_provisioner_script.py`` (parametrized
      `label`), which plant a response-derived SCOPE OR MAPPER NAME
      and assert it is absent from the actual `diffs` output; and (new
      in fix round 6) the 2 tests in
      ``TestDiffsRuntimeAttributionOfTheClientUuid`` (same provisioner
      test file), which plant a response-derived CLIENT UUID (a
      DIFFERENT value, not a name) and assert the UUID appears in the
      output once `diffs` is rebound/appended with it — re-review #6
      measured this UUID shape as a separate, previously-uncovered
      GREEN; these 2 tests close that specific gap. This does NOT
      extend to the `printf -v`/`read`-into-`ACL_E2E_CLIENT_ID` gaps or
      a second nested for-loop gap listed above — no runtime test
      plants those specific shapes today; they remain open risk
      surface, mitigated only by the fact that the current production
      code does not use them."""

    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_every_acl_e2e_fail_detail_only_expands_the_allowlist(self, path) -> None:
        text = path.read_text(encoding="utf-8")
        violations = _acl_e2e_fail_detail_disallowed_expansions(text)
        assert violations == [], (
            f"detail= expands a variable outside the allowlist "
            f"{sorted(_DETAIL_VAR_ALLOWLIST)}: {violations}"
        )

    def test_a_planted_dollar_one_in_detail_reddens(self) -> None:
        """Falsifiability: a new call site that interpolates `$1`
        (exactly the shape that survived two prior rounds) must turn
        the parser RED."""
        text = 'acl_e2e_fail read-failed "cannot read $1"\n'
        violations = _acl_e2e_fail_detail_disallowed_expansions(text)
        assert violations == [("cannot read $1", "1")]

    def test_an_allowlisted_detail_is_not_flagged(self) -> None:
        text = (
            'acl_e2e_fail strip-list-failed "cannot list ${kind} scopes"\n'
            'acl_e2e_fail sunset-wrong-client-id "returned clientId is not '
            '${ACL_E2E_CLIENT_ID}"\n'
            'acl_e2e_fail drift "drift:${diffs}"\n'
            'acl_e2e_fail read-failed "cannot read $2 scopes"\n'
        )
        assert _acl_e2e_fail_detail_disallowed_expansions(text) == []

    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_every_acl_e2e_fail_detail_allowlist_name_is_literal_or_counter(
        self, path
    ) -> None:
        """R4 re-review #4, item (b): PROVENANCE, not text — every
        allowlisted name (`kind`, `ACL_E2E_CLIENT_ID`, and the
        `item`/counter names `diffs` is built from) is bound ONLY to a
        literal, a fixed token, or a `$((counter + 1))` self-increment,
        never a command substitution. The detail-text parser above
        cannot see this; this is a separate check reading WHERE each
        value comes from."""
        text = path.read_text(encoding="utf-8")
        violations = _detail_allowlist_names_only_bind_to_literals(text)
        assert violations == [], violations

    def test_kind_rebound_to_a_response_reddens(self) -> None:
        """The re-review's own named neuter:
        ``kind="$(acl_e2e_kc get clients)"`` must turn the provenance
        check RED."""
        text = 'kind="$(acl_e2e_kc get clients)"\nfor kind in default optional; do\n'
        violations = _detail_allowlist_names_only_bind_to_literals(text)
        assert any("kind" in v for v in violations), violations

    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_kind_rebound_planted_in_the_real_tree_reddens(self, path) -> None:
        """Same neuter, planted directly into the REAL file text (not
        a synthetic snippet) — must turn the provenance check RED on
        its own, independent of any other test."""
        text = path.read_text(encoding="utf-8")
        needle = "for kind in default optional; do\n"
        assert text.count(needle) == 1
        planted = text.replace(needle, 'kind="$(acl_e2e_kc get clients)"\n' + needle, 1)
        violations = _detail_allowlist_names_only_bind_to_literals(planted)
        assert any("kind" in v for v in violations), violations

    def test_a_literal_provenance_tree_is_not_flagged(self) -> None:
        text = (
            "for kind in default optional; do\n"
            "  :\n"
            "done\n"
            'ACL_E2E_CLIENT_ID="audittrace-acl-e2e"\n'
            "for item in \\\n"
            "    'a' 'b'; do\n"
            "  :\n"
            "done\n"
            "n=0\n"
            "unexpected_default=0\n"
            "unexpected_optional=0\n"
            "unexpected_mapper=0\n"
            "unexpected_config=0\n"
            "n=$((n + 1))\n"
            'acl_e2e_scope_names "${uuid}" default\n'
            'acl_e2e_scope_names "${uuid}" optional\n'
        )
        assert _detail_allowlist_names_only_bind_to_literals(text) == []

    def test_acl_e2e_scope_names_one_line_variable_caller_reddens(self) -> None:
        """Re-review #5's first named neuter: a caller passing a
        VARIABLE instead of a literal, with NO trailing backslash
        continuation on its line (the exact shape the old,
        un-anchored `$2` check missed — it only ever matched callers
        ending in `\\`), must turn the provenance check RED."""
        text = (
            'acl_e2e_scope_names "${uuid}" default\n'
            'x=$(acl_e2e_scope_names "${uuid}" "${names}")\n'
        )
        violations = _detail_allowlist_names_only_bind_to_literals(text)
        assert any("acl_e2e_scope_names" in v for v in violations), violations

    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_acl_e2e_scope_names_one_line_variable_caller_planted_in_the_real_tree_reddens(
        self, path
    ) -> None:
        """Same neuter, planted directly into the REAL file text (not
        a synthetic snippet) as a one-line, non-continued caller."""
        text = path.read_text(encoding="utf-8")
        matches = list(_OPTIONAL_SCOPE_NAMES_LINE_RE.finditer(text))
        assert len(matches) == 1, matches
        m = matches[0]
        indent = m.group(1)
        inserted = f'{indent}x=$(acl_e2e_scope_names "${{uuid}}" "${{names}}")\n'
        planted = text[: m.start()] + inserted + text[m.start() :]
        violations = _detail_allowlist_names_only_bind_to_literals(planted)
        assert any("acl_e2e_scope_names" in v for v in violations), violations

    def test_acl_e2e_scope_names_third_caller_reddens(self) -> None:
        """Re-review #5's second named neuter: a THIRD caller added
        (count no longer the known 2) must turn the provenance check
        RED even though the extra caller itself passes a literal."""
        text = (
            'acl_e2e_scope_names "${uuid}" default\n'
            'acl_e2e_scope_names "${uuid}" optional\n'
            'acl_e2e_scope_names "${uuid}" default\n'
        )
        violations = _detail_allowlist_names_only_bind_to_literals(text)
        assert any("expected exactly 2 calls" in v for v in violations), violations

    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_acl_e2e_scope_names_third_caller_planted_in_the_real_tree_reddens(
        self, path
    ) -> None:
        """Same neuter, planted directly into the REAL file text (not
        a synthetic snippet) as a THIRD, literal-arg caller."""
        text = path.read_text(encoding="utf-8")
        matches = list(_OPTIONAL_SCOPE_NAMES_LINE_RE.finditer(text))
        assert len(matches) == 1, matches
        m = matches[0]
        indent = m.group(1)
        inserted = f'{indent}acl_e2e_scope_names "${{uuid}}" default\n'
        planted = text[: m.start()] + inserted + text[m.start() :]
        violations = _detail_allowlist_names_only_bind_to_literals(planted)
        assert any("expected exactly 2 calls" in v for v in violations), violations

    def test_acl_e2e_scope_names_unquoted_first_arg_caller_reddens(self) -> None:
        """Re-review #6's first named neuter: a caller whose FIRST
        argument is UNQUOTED (`${uuid}`, not `"${uuid}"`) — the exact
        shape the prior round's argument-shape regex (which required a
        double-quoted first argument) measured GREEN. The new
        word-boundary finder does not anchor on the first argument's
        quoting at all, so this call's unquoted, non-literal SECOND
        argument (`${names}`) must still turn the check RED."""
        text = (
            'acl_e2e_scope_names "${uuid}" default\n'
            "x=$(acl_e2e_scope_names ${uuid} ${names})\n"
        )
        violations = _detail_allowlist_names_only_bind_to_literals(text)
        assert any("acl_e2e_scope_names" in v for v in violations), violations

    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_acl_e2e_scope_names_unquoted_first_arg_caller_planted_in_the_real_tree_reddens(
        self, path
    ) -> None:
        """Same neuter, planted directly into the REAL file text (not
        a synthetic snippet)."""
        text = path.read_text(encoding="utf-8")
        matches = list(_OPTIONAL_SCOPE_NAMES_LINE_RE.finditer(text))
        assert len(matches) == 1, matches
        m = matches[0]
        indent = m.group(1)
        inserted = f"{indent}x=$(acl_e2e_scope_names ${{uuid}} ${{names}})\n"
        planted = text[: m.start()] + inserted + text[m.start() :]
        violations = _detail_allowlist_names_only_bind_to_literals(planted)
        assert any("acl_e2e_scope_names" in v for v in violations), violations

    def test_acl_e2e_scope_names_single_quoted_first_arg_caller_reddens(self) -> None:
        """Re-review #6's second named neuter: a caller whose FIRST
        argument is SINGLE-quoted (`'u'`) and whose second argument is
        a DOUBLE-quoted variable (`"$names"`) — also measured GREEN by
        the prior round's double-quote-anchored regex. Both arguments'
        quote shapes are irrelevant to the new finder; the non-literal
        second argument must still turn the check RED."""
        text = (
            'acl_e2e_scope_names "${uuid}" default\n'
            "x=$(acl_e2e_scope_names 'u' \"$names\")\n"
        )
        violations = _detail_allowlist_names_only_bind_to_literals(text)
        assert any("acl_e2e_scope_names" in v for v in violations), violations

    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_acl_e2e_scope_names_single_quoted_first_arg_caller_planted_in_the_real_tree_reddens(
        self, path
    ) -> None:
        """Same neuter, planted directly into the REAL file text (not
        a synthetic snippet)."""
        text = path.read_text(encoding="utf-8")
        matches = list(_OPTIONAL_SCOPE_NAMES_LINE_RE.finditer(text))
        assert len(matches) == 1, matches
        m = matches[0]
        indent = m.group(1)
        inserted = f"{indent}x=$(acl_e2e_scope_names 'u' \"$names\")\n"
        planted = text[: m.start()] + inserted + text[m.start() :]
        violations = _detail_allowlist_names_only_bind_to_literals(planted)
        assert any("acl_e2e_scope_names" in v for v in violations), violations

    def test_acl_e2e_scope_names_unparseable_call_fails_closed(self) -> None:
        """An UNTERMINATED quote on the call's own line is itself a
        violation (fail-closed), never a silent skip."""
        text = 'acl_e2e_scope_names "${uuid} default\n'
        violations = _detail_allowlist_names_only_bind_to_literals(text)
        assert any("unparseable" in v for v in violations), violations


class TestSecretHygiene:
    _EXPANSION_RE = re.compile(
        r"\b(echo|printf)\b(?!.*acl_e2e_fail).*\$(\{)?KEYCLOAK_ADMIN_PASSWORD\b"
    )

    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_no_password_expansion_outside_acl_e2e_fails_fixed_format(
        self, path
    ) -> None:
        """R6: the previous version exempted ANY line containing the
        substring ``--password`` — which also hid the exact shape the
        review named (``echo "--password ${KEYCLOAK_ADMIN_PASSWORD}"``,
        a debug line that both mentions the flag AND echoes the
        value). The only legitimate ``--password`` line is the kcadm
        argument itself, which never starts with ``echo``/``printf``
        and therefore never needs — or gets — an exemption here."""
        text = path.read_text(encoding="utf-8")
        # R6 re-review (P10): a bare per-line check never sees
        # ``echo \`` + newline + ``"$KEYCLOAK_ADMIN_PASSWORD"`` as one
        # statement — collapse backslash continuations FIRST (matching
        # test_no_xtrace_printenv_declare_p_or_cat_here_string's own
        # technique), so the obfuscation reads identically to the
        # single-line form before this check runs.
        collapsed = _collapse_backslash_continuations(text)
        for line in collapsed.splitlines():
            if "acl_e2e_fail" in line:
                continue
            assert not self._EXPANSION_RE.search(line), line

    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_p10_echo_continuation_planted_in_the_real_tree_reddens(self, path) -> None:
        """R6 re-review's own named neuter (P10), planted into the REAL
        file text (not a synthetic standalone string) — must turn the
        check above RED."""
        text = path.read_text(encoding="utf-8")
        planted = text + '\necho \\\n  "$KEYCLOAK_ADMIN_PASSWORD"\n'
        collapsed = _collapse_backslash_continuations(planted)
        found = False
        for line in collapsed.splitlines():
            if "acl_e2e_fail" in line:
                continue
            if self._EXPANSION_RE.search(line):
                found = True
                break
        assert found, "the planted echo-continuation was not caught"

    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_no_dollar_at_or_dollar_star_echoed(self, path) -> None:
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.search(r"\b(echo|printf)\b.*\$[@*]", stripped):
                assert "acl_e2e_fail" in stripped, stripped

    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_no_xtrace_printenv_declare_p_or_cat_here_string(self, path) -> None:
        """R6, tokenized (never a whole-text substring/regex a
        backslash-continuation or an unrelated identifier could beat):
        ``set`` with any flag bundle containing ``x`` or ``-o`` +
        (any whitespace) + ``xtrace``; bare ``env``/``printenv``;
        ``export -p``/``typeset -p``/``declare -p``; ``tee`` combined
        with a here-string (``<<<``); and the existing ``cat <<<
        $KEYCLOAK_ADMIN_PASSWORD`` shape, now continuation-aware too."""
        text = path.read_text(encoding="utf-8")
        for words, raw in _code_statements(text):
            head = words[0]
            if head == "set":
                for tok in words[1:]:
                    if tok.startswith("-") and tok != "-o" and "x" in tok:
                        raise AssertionError(f"forbidden xtrace shape: {raw!r}")
                if "-o" in words:
                    rest = words[words.index("-o") + 1 :]
                    assert "xtrace" not in rest, raw
            if head in ("env", "printenv"):
                raise AssertionError(f"forbidden env dump: {raw!r}")
            if head in ("export", "typeset", "declare") and "-p" in words:
                raise AssertionError(f"forbidden env dump: {raw!r}")
            if "tee" in words and "<<<" in raw:
                raise AssertionError(f"forbidden tee here-string: {raw!r}")
        collapsed = _collapse_backslash_continuations(
            "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        )
        assert not re.search(r"cat\s+<<<\s*\"?\$\{?KEYCLOAK_ADMIN_PASSWORD", collapsed)

    @pytest.mark.parametrize(
        ("label", "snippet"),
        [
            ("set-eux-bundled", "set -eux\n"),
            ("set-o-xtrace-double-space", "set -o  xtrace\n"),
            ("bare-env", "env\n"),
            ("export-dash-p", "export -p\n"),
            ("typeset-dash-p", "typeset -p\n"),
            ("tee-here-string", 'tee /tmp/x <<< "${KEYCLOAK_ADMIN_PASSWORD}"\n'),
            ("printenv-again", "printenv\n"),
            ("declare-dash-p-again", "declare -p\n"),
            (
                "backslash-continued-set-x",
                "set \\\n  -x\n",
            ),
            (
                "debug-echo-mentions-the-flag-and-the-value",
                'echo "--password ${KEYCLOAK_ADMIN_PASSWORD}"\n',
            ),
            (
                "cat-here-string-backslash-continued",
                'cat \\\n  <<< "${KEYCLOAK_ADMIN_PASSWORD}"\n',
            ),
        ],
    )
    def test_every_r6_named_shape_is_caught(self, label: str, snippet: str) -> None:
        """R6's own falsifiability requirement: each named obfuscation
        must independently turn one of the two checks above RED — a
        guard without this row is unproven."""
        caught = False
        try:
            for words, raw in _code_statements(snippet):
                head = words[0]
                if head == "set":
                    for tok in words[1:]:
                        if tok.startswith("-") and tok != "-o" and "x" in tok:
                            raise AssertionError(raw)
                    if "-o" in words and "xtrace" in words[words.index("-o") + 1 :]:
                        raise AssertionError(raw)
                if head in ("env", "printenv", "export", "typeset", "declare"):
                    if head in ("env", "printenv") or "-p" in words:
                        raise AssertionError(raw)
                if "tee" in words and "<<<" in raw:
                    raise AssertionError(raw)
        except AssertionError:
            caught = True
        collapsed = _collapse_backslash_continuations(snippet)
        if re.search(r"cat\s+<<<\s*\"?\$\{?KEYCLOAK_ADMIN_PASSWORD", collapsed):
            caught = True
        if self._EXPANSION_RE.search(snippet) and "acl_e2e_fail" not in snippet:
            caught = True
        assert caught, label

    def test_planted_secret_line_leaves_both_streams_clean(
        self, harness: StepHarness
    ) -> None:
        """Runtime half (F §1.3, kept by F1): a line containing the
        password VARIABLE triggers the trap; the value itself (never
        set to a literal secret-shaped string here — $BASH_COMMAND is
        source text) never appears."""
        script = harness._script_text(clients=[], enabled="true")
        script = script.replace(
            'REALM="audittrace"\n',
            'REALM="audittrace"\nKEYCLOAK_ADMIN_PASSWORD="super-secret-password"\n'
            'false "$KEYCLOAK_ADMIN_PASSWORD"\n',
            1,
        )
        env = {
            **os.environ,
            "FAKE_STATE": str(harness.state_path),
            "FAKE_LOG": str(harness.log),
        }
        harness.state_path.write_text(json.dumps({"clients": [], "scopes": []}))
        proc = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30
        )
        assert proc.returncode != 0
        assert "super-secret-password" not in proc.stdout
        assert "super-secret-password" not in proc.stderr
        assert "<redacted-var>" in proc.stdout


# ─────────────────────────────────────────────────────────────────────
# AC-F3 / F3 S2-6 — the shape-capture printer
# ─────────────────────────────────────────────────────────────────────


def _run_shape_line(site: Path, line: str) -> str:
    """Feed one line through the real ``acl_e2e_shape_line`` function,
    verbatim from source. Extracted by matching the CLOSING brace at
    the function declaration's own indentation (brace-counting breaks
    because the body contains literal ``{``/``}`` characters inside
    quoted case-pattern strings)."""
    text = site.read_text(encoding="utf-8")
    start = text.index("acl_e2e_shape_line() {")
    line_start = text.rfind("\n", 0, start) + 1
    indent = text[line_start:start]
    closer = "\n" + indent + "}\n"
    end = text.index(closer, start) + len(closer)
    func = text[start:end]
    script = (
        "shopt -s extglob 2>/dev/null; " + func + f"\nacl_e2e_shape_line {line!r}\n"
    )
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=10
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.rstrip("\n")


class TestShapeCapturePrinter:
    @pytest.mark.parametrize("site", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ('  "id" : "abc123",', '"id": <string:8>,'),
            ('  "enabled" : true,', '"enabled": <bool>,'),
            ('  "enabled" : false', '"enabled": <bool>'),
            ('  "something" : null,', '"something": <null>,'),
            ('  "count" : 42,', '"count": <number>,'),
            ('  "attributes" : {', '"attributes": {'),
            ('  "attributes" : {},', '"attributes": {},'),
            ('  "redirectUris" : [ ],', '"redirectUris": <array:0>,'),
            ('  "redirectUris" : [ "a", "b" ],', '"redirectUris": <array:2>,'),
            ('  "defaultClientScopes" : [', '"defaultClientScopes": ['),
            ('  "defaultClientScopes" : [],', '"defaultClientScopes": [],'),
            ('    "memory:acl:write",', "<string:18>,"),
            ('    "memory:acl:write"', "<string:18>"),
            ("  },", "},"),
            ("  ]", "]"),
            ("{", "{"),
            ("}", "}"),
        ],
    )
    def test_allowlisted_shapes(self, site: Path, line: str, expected: str) -> None:
        assert _run_shape_line(site, line) == expected

    @pytest.mark.parametrize("site", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_an_unrecognized_value_shape_fails_closed_inner_branch(
        self, site: Path
    ) -> None:
        """R7: the INNER fail-closed default — a line that DOES match
        the ``"key" : <value>`` shape, but whose value is none of the
        allowlisted ones (string/bool/null/object/array/number).
        Neutering ONLY this ``else`` (e.g. printing ``rest`` verbatim)
        must redden THIS test without touching the OUTER one below."""
        weird = '  "k" : something-not-json-shaped,'
        out = _run_shape_line(site, weird)
        assert out.startswith("<line:redacted len=")
        assert "something-not-json-shaped" not in out

    @pytest.mark.parametrize("site", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_an_unrecognized_line_shape_fails_closed_outer_branch(
        self, site: Path
    ) -> None:
        """R7: the OUTER fail-closed default — a line that matches
        NEITHER the ``"key" : <value>`` shape NOR the bare-quoted-
        string shape (no colon, no quotes at all). This is a
        DIFFERENT code path than the inner-branch test above — the
        function's own final fallback line, reached only when both
        earlier regexes fail to match. Neutering ONLY this final
        ``printf`` (e.g. printing ``line`` verbatim here while leaving
        the inner ``else`` alone) must redden THIS test without
        reddening the inner-branch one."""
        weird = "  totally-unquoted-no-colon-shape-here"
        out = _run_shape_line(site, weird)
        assert out.startswith("<line:redacted len=")
        assert "totally-unquoted-no-colon-shape-here" not in out

    @pytest.mark.parametrize("site", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    @pytest.mark.parametrize(
        "line",
        [
            '  "registrationAccessToken" : "top-secret-value-xyz",',
            '  "redirectUris" : [ "top-secret-value-xyz" ],',
            '    "top-secret-value-xyz",',
        ],
        ids=["object-value", "inline-array-element", "bare-array-element"],
    )
    def test_planted_secret_absent_in_every_position(
        self, site: Path, line: str
    ) -> None:
        out = _run_shape_line(site, line)
        assert "top-secret-value-xyz" not in out

    def test_default_off_and_get_only_when_on(self, harness: StepHarness) -> None:
        proc = harness.run(
            clients=[
                {
                    "id": "uuid-e2e",
                    "clientId": "audittrace-acl-e2e",
                    "body": {"clientId": "audittrace-acl-e2e"},
                    "default": [{"id": "s1", "name": "memory:acl:read-own"}],
                    "optional": [],
                    "mappers": [],
                }
            ],
            enabled="true",
            extra_prelude="AUDITTRACE_ACL_E2E_SHAPE_CAPTURE=1\n",
        )
        assert "acl-e2e: shape-begin" in proc.stdout
        assert "acl-e2e: shape-end" in proc.stdout
        assert harness.mutating_calls() == []

    def test_flag_off_by_default_emits_no_shape_markers(
        self, harness: StepHarness
    ) -> None:
        proc = harness.run(
            clients=[
                {
                    "id": "uuid-e2e",
                    "clientId": "audittrace-acl-e2e",
                    "body": {"clientId": "audittrace-acl-e2e"},
                    "default": [{"id": "s1", "name": "memory:acl:read-own"}],
                    "optional": [],
                    "mappers": [],
                }
            ],
            enabled="true",
        )
        assert "acl-e2e: shape-begin" not in proc.stdout


# ─────────────────────────────────────────────────────────────────────
# RunFull — the whole script, both states, measured coverage
# ─────────────────────────────────────────────────────────────────────


def _invocation_lines(text: str) -> list[tuple[int, int]]:
    """Every real ``acl_e2e_kc`` CALL SITE as a LINE RANGE (gate
    SF3-3): a call inside a ``$(... \\`` continuation reports
    ``BASH_LINENO`` as the LAST line of that continuation, not the
    line the token itself sits on — exact-line equality against the
    token's own line therefore misses every multi-line call. The
    range runs from the token's line through the last line of a
    backslash-continued statement (or itself, if not continued).
    Comment lines (which may mention ``acl_e2e_kc`` in prose) are
    excluded. Numbered against the text actually executed, matching
    what ``BASH_LINENO`` reports at runtime."""
    lines = text.splitlines()
    ranges: list[tuple[int, int]] = []
    for i, line in enumerate(lines, start=1):
        if line.lstrip().startswith("#"):
            continue
        if not (re.search(r"\bacl_e2e_kc\b", line) and "acl_e2e_kc()" not in line):
            continue
        end = i
        while lines[end - 1].rstrip().endswith("\\"):
            end += 1
        ranges.append((i, end))
    return ranges


class RunFullHarness:
    """Runs the WHOLE script (both sites), with the cluster-tool call
    shimmed (so site 5's pod-discovery and kcadm-exec calls both
    resolve to the fake, and the ``ACL_E2E_VIA_KC`` marker lands on the
    shim exactly as it does in production, per gate SF3-2).

    The shim file is deliberately NOT named ``kubectl`` (see
    ``build_script``'s own substitution, below): `tests/conftest.py`'s
    R3 hermetic-cluster-tool guard (PR #380) statically scans the TEXT
    handed to ``bash -c`` for a command whose name is literally
    ``kubectl``, before bash ever runs it — so a PATH-resolved stub
    alone (the ORIGINAL fix for gate SF3-2's "a shell function is
    never found by `timeout`'s own exec" problem) does not satisfy it:
    the guard fires on the literal word in the text itself, regardless
    of what PATH would resolve it to at runtime. Both needs are met by
    (a) giving the stub a name that contains neither ``kubectl`` nor
    ``helm`` and (b) ``build_script`` substituting every literal
    ``kubectl`` command-name occurrence in the constructed text with
    this stub's own path, so the guard never sees the forbidden word
    at all and the stub still runs as a real executable under
    `timeout`."""

    def __init__(self, tmp: Path, site: Path) -> None:
        self.tmp = tmp
        self.site = site
        self.state_path = tmp / "state.json"
        self.log = tmp / "calls.log"
        self.marker_log = tmp / "marker.log"
        self.fake = tmp / "fake-kcadm"
        self.fake.write_text(FAKE_KCADM)
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)
        self.log.write_text("")
        self.marker_log.write_text("")
        # A real EXECUTABLE, not a bash function: every real kcadm
        # invocation runs under `timeout`, which execs its argument as
        # a subprocess — a shell FUNCTION would never be found by that
        # exec (gate SF3-2, found the hard way — the function form
        # left `timeout kubectl ...` resolving the REAL cluster
        # kubectl, not the fake). Named to avoid the literal word
        # "kubectl" (see the class docstring).
        self.bin_dir = tmp / "bin"
        self.bin_dir.mkdir(exist_ok=True)
        self.kubectl_shim = self.bin_dir / "fake-cluster-tool"
        self.kubectl_shim.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env bash
                if [[ "$3" == "get" && "$4" == "pod" ]]; then
                  echo "fake-kc-pod"
                  exit 0
                fi
                rest=()
                seen_dd=0
                for x in "$@"; do
                  if [[ "${{seen_dd}}" -eq 1 ]]; then rest+=("$x"); fi
                  if [[ "$x" == "--" ]]; then seen_dd=1; fi
                done
                exec "{self.fake}" "${{rest[@]:1}}"
                """
            )
        )
        self.kubectl_shim.chmod(self.kubectl_shim.stat().st_mode | stat.S_IXUSR)

    def build_script(self, *, tcp_port: int) -> str:
        """The EXACT text passed to ``bash -c`` — ``BASH_LINENO`` is
        reported against THIS text, not the source file (SF3-3: for
        site 4 the source is ConfigMap-YAML, template-stripped and
        dedented here; coverage must be measured against what actually
        ran, never the source line numbers)."""
        text = self.site.read_text(encoding="utf-8")
        if self.site == CONFIGMAP:
            # Strip the Helm `{{ }}` templating to plain bash for a
            # direct local run (values substituted literally).
            body = re.search(r"ensure-memory-scopes\.sh: \|\n(.*)", text, re.S).group(1)
            # Drop the file-level `{{- end }}` that closes the OUTER
            # `.Values.keycloak.enabled` guard wrapping the whole
            # ConfigMap — it sits at column 0, outside the block
            # scalar's own indentation, and is not part of the script.
            body = re.sub(r"\n\{\{- end \}\}\s*$", "", body)
            lines = body.splitlines()
            indent = len(lines[0]) - len(lines[0].lstrip())
            script_text = "\n".join(
                ln[indent:] if ln[:indent] == " " * indent else ln.lstrip()
                for ln in lines
            )
            # Every `{{- if ... }} ... {{- end }}` conditional block is
            # treated as FALSE (vault/istio disabled) — this harness
            # supplies KEYCLOAK_ADMIN_PASSWORD directly and needs
            # neither block's content.
            script_text = re.sub(
                r"\{\{- if [^}]*\}\}\n.*?\{\{- end \}\}\n", "", script_text, flags=re.S
            )
            script_text = re.sub(r"\{\{[^}]*\}\}", "", script_text)
            script_text = script_text.replace(
                "/opt/keycloak/bin/kcadm.sh", str(self.fake)
            )
            prelude = (
                f'KC_BASE_URL="http://127.0.0.1:{tcp_port}"\n'
                f'KEYCLOAK_ADMIN_PASSWORD="test-password"\n'
            )
        else:
            script_text = text
            prelude = 'KEYCLOAK_ADMIN_PASSWORD="test-password"\n'
        # Hermetic-guard compliance (tests/conftest.py R3, PR #380;
        # the word-rule rebuild in PR #382 made this STRICTER): the
        # guard's word rule scans every spawned argv element — this
        # whole script text included — for the WORD `kubectl` or
        # `helm` occurring ANYWHERE at all, not just in command
        # position, before `bash -c` ever runs it; a bare mention
        # inside a comment (both real files have a couple, describing
        # the helm upgrade this script backstops) trips it exactly
        # like a real invocation would. So every occurrence of either
        # word — reachable at runtime or not, inside a comment or not,
        # SF3-3's own Vault-admin-password fallback included — must be
        # gone from the text itself, not just resolvable to a stub via
        # PATH. A word-boundary substitution (not a blind string
        # replace) touches nothing else in the surrounding text; it
        # changes no line's LINE NUMBER (same count of newlines in,
        # same count out), so BASH_LINENO coverage reporting
        # (SF3-3/SF3-4) is unaffected either way.
        script_text = re.sub(r"\bkubectl\b", str(self.kubectl_shim), script_text)
        script_text = re.sub(r"\bhelm\b", "chart-cli", script_text)
        return (
            "set -euo pipefail\n"
            + prelude
            + "ACL_E2E_TCP_BUDGET_S=5\n"
            + "ACL_E2E_AUTH_BUDGET_S=5\n"
            + script_text
        )

    def _env(self, extra_env: dict[str, str] | None = None) -> dict[str, str]:
        """Routed through ``hermetic_env()`` (test-parity ADDENDUM A2+):
        a hand-built ``PATH`` here would put ``self.bin_dir`` at
        ``PATH[0]``, failing the hermetic guard's env-equality check
        under the real CI shape (``HERMETIC_REQUIRED=1``), which
        requires the frozen sentinel shim dir there. `self.bin_dir` no
        longer needs to be on `PATH` at all for this harness to work:
        ``build_script()``'s own ``kubectl``-substitution already
        points directly at ``self.kubectl_shim``'s absolute path, and
        every ``self.fake`` (kcadm) invocation is likewise always by
        absolute path — nothing here still relies on a bare command
        name resolving via PATH."""
        return hermetic_env(
            FAKE_STATE=str(self.state_path),
            FAKE_LOG=str(self.log),
            FAKE_MARKER_LOG=str(self.marker_log),
            **(extra_env or {}),
        )

    def run(
        self,
        *,
        state: dict[str, Any],
        tcp_port: int,
        check_unwrapped: bool = True,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.state_path.write_text(json.dumps(state))
        script = self.build_script(tcp_port=tcp_port)
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=self._env(extra_env),
            timeout=60,
        )
        if check_unwrapped:
            self.assert_no_unwrapped_calls()
        return result

    def popen(
        self,
        *,
        state: dict[str, Any],
        tcp_port: int,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.Popen[str]:
        """F-2l v2/v3 (R2): a ``subprocess.run`` call cannot have a
        signal sent to it mid-flight — these need ``Popen`` the same
        way ``StepHarness`` does for its own SIGTERM/SIGINT rows."""
        self.state_path.write_text(json.dumps(state))
        script = self.build_script(tcp_port=tcp_port)
        return subprocess.Popen(
            ["bash", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._env(extra_env),
            preexec_fn=_reset_sigint_to_default,
        )

    def assert_no_unwrapped_calls(self) -> None:
        """Addendum F4 point 2 — same blanket guarantee as
        ``StepHarness``, for the WHOLE-script runs."""
        lines = [ln for ln in self.marker_log.read_text().splitlines() if ln]
        absent = [ln for ln in lines if "marker=absent" in ln]
        assert absent == [], f"unwrapped kcadm call(s) detected: {absent}"

    def logged_lines(self) -> set[int]:
        out = set()
        for ln in self.log.read_text().splitlines():
            m = re.search(r"line=(\d+)$", ln)
            if m:
                out.add(int(m.group(1)))
        return out

    def mutating_calls(self) -> list[str]:
        lines = [ln for ln in self.log.read_text().splitlines() if ln]
        return [ln for ln in lines if ln.split()[0] in ("create", "delete", "update")]


def _run_full_state(*, scopes_present: bool, e2e_present: bool) -> dict[str, Any]:
    scope_names = [
        "memory:episodic:write",
        "memory:procedural:write",
        "memory:semantic:write",
        "memory:decisions:write",
        "memory:skills:write",
        "audittrace:admin",
        "audittrace:assessment:ingest",
        "memory:corpus:decisions:read",
        "memory:corpus:decisions:write",
        "memory:corpus:skills:read",
        "memory:corpus:skills:write",
        "memory:corpus:semantic:read",
        "memory:corpus:semantic:write",
        "memory:session:write",
        "memory:session:read-own",
        "memory:conversations:write",
        "memory:conversations:read-own",
        "memory:presets:write",
        "memory:presets:read-own",
        "memory:prompts:write",
        "memory:prompts:read-own",
        "memory:chat_projects:write",
        "memory:chat_projects:read-own",
        "memory:files:write",
        "memory:files:read-own",
        "memory:agents:write",
        "memory:agents:read-own",
        "memory:conversation_tags:write",
        "memory:conversation_tags:read-own",
        "memory:tool_favorites:write",
        "memory:tool_favorites:read-own",
        "memory:acl:read-own",
        "memory:acl:write",
        "profile",
        "email",
        "offline_access",
    ]
    scopes = (
        [{"id": f"s-{i}", "name": n} for i, n in enumerate(scope_names)]
        if scopes_present
        else []
    )
    clients = _base_clients()
    if e2e_present:
        clients.append(
            {
                "id": "uuid-e2e",
                "clientId": "audittrace-acl-e2e",
                "body": {
                    "clientId": "audittrace-acl-e2e",
                    "publicClient": True,
                    "standardFlowEnabled": False,
                    "directAccessGrantsEnabled": False,
                    "implicitFlowEnabled": False,
                    "serviceAccountsEnabled": False,
                    "consentRequired": True,
                    "attributes": {
                        "oauth2.device.authorization.grant.enabled": "true",
                        "oauth2.device.polling.interval": "5",
                        "oauth2.device.code.lifespan": "120",
                        "use.refresh.tokens": "false",
                        "access.token.lifespan": "900",
                    },
                },
                "default": [
                    {"id": "s-read", "name": "memory:acl:read-own"},
                    {"id": "s-write", "name": "memory:acl:write"},
                ],
                "optional": [],
                "mappers": [
                    {
                        "name": "aud-audittrace-server",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-audience-mapper",
                        "config": {
                            "included.custom.audience": "audittrace-server",
                            "id.token.claim": "false",
                            "access.token.claim": "true",
                        },
                    }
                ],
            }
        )
    return {"clients": clients, "scopes": scopes}


import socket  # noqa: E402  (grouped near its sole use, below)


def _listening_socket() -> tuple[socket.socket, int]:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    return srv, srv.getsockname()[1]


class TestSignalTermDuringPreStepPhases:
    """F-2l v2/v3 (R2) — the mandated-but-omitted rows covering the
    ConfigMap-only pre-step retry loops (``pre-step:tcp-wait`` /
    ``pre-step:auth``); the host-run script has neither loop (a single
    unguarded call each), already covered by the top-level SIGTERM
    rows elsewhere in this file."""

    def test_f2l_v2_term_during_tcp_wait(self, tmp_path: Path) -> None:
        """TERM sent while the script is polling
        ``/dev/tcp/<host>/<port>`` for a Keycloak that is never
        started (no listener bound) — a plain foreground `sleep 2` in
        a `while` loop, not a backgrounded `acl_e2e_kc` call, so the
        signal is delivered promptly."""
        h = RunFullHarness(tmp_path, CONFIGMAP)
        # An unused, unbound port: nothing ever accepts the TCP
        # connection, so the script stays in the tcp-wait poll loop.
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.close()  # closed, never listened on — connections refuse immediately
        proc = h.popen(
            state=_run_full_state(scopes_present=False, e2e_present=False),
            tcp_port=port,
        )
        time.sleep(1.0)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=15)
        assert proc.returncode == 143
        assert _acl_e2e_line_count(out) == 1, out
        assert _acl_e2e_line_count(err) == 1, err
        assert "code=signal-term" in out
        assert "stage=pre-step:tcp-wait" in out
        assert "rc=143" in out
        h.assert_no_unwrapped_calls()

    def test_f2l_v3_term_during_hanging_auth_loop(self, tmp_path: Path) -> None:
        """F2's own spec text: the fake kcadm hangs inside the auth
        loop (`config credentials` sleeps); TERM → ``code=signal-term
        stage=pre-step:auth`` exactly once per stream, exit 143,
        ``mutating_calls() == []`` — the per-call wrapper's
        background+``wait`` makes the signal prompt even though this
        call sits inside a loop, not at the top level."""
        srv, port = _listening_socket()
        try:
            h = RunFullHarness(tmp_path, CONFIGMAP)
            proc = h.popen(
                state={
                    **_run_full_state(scopes_present=False, e2e_present=False),
                    "fail_match": [
                        {"substr": "config credentials", "hang_seconds": 30}
                    ],
                },
                tcp_port=port,
                # Same reasoning as the sunset-delete signal tests: keep
                # the per-call timeout WELL clear of the 30s hang so it
                # can never race the injected TERM under heavy
                # scheduling delay on a shared box.
                extra_env={"ACL_E2E_KCADM_TIMEOUT": "120"},
            )
            deadline = time.time() + 10
            while h.marker_log.read_text() == "" and time.time() < deadline:
                time.sleep(0.05)
            time.sleep(0.3)  # the call is backgrounded; give `wait` a moment
            signal_sent_at = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            # 60s is a HANG CEILING ONLY; the elapsed-time bound is the
            # real guard (re-review d — U/V re-opened by the widened
            # per-call timeout alone, see the sunset-delete siblings).
            out, err = proc.communicate(timeout=60)
            elapsed = time.monotonic() - signal_sent_at
            assert proc.returncode == 143
            assert elapsed < 5, f"took {elapsed:.1f}s — not promptly interrupted"
            assert _acl_e2e_line_count(out) == 1, out
            assert _acl_e2e_line_count(err) == 1, err
            assert "code=signal-term" in out
            assert "stage=pre-step:auth" in out
            assert h.mutating_calls() == []
            h.assert_no_unwrapped_calls()
        finally:
            srv.close()


class TestUnwrappedCallAtATolerantSite:
    """Addendum F4 point 2's own named neuter (the reviewer's Y1/Y2):
    an unwrapped (bare, never through ``acl_e2e_kc``) call planted at
    an existing ``2>/dev/null ... || true``-tolerant site must still
    turn the marker-log assertion RED — proving the mechanism does NOT
    depend on the fake's own ``exit(97)``, which such a site would
    otherwise swallow silently."""

    @staticmethod
    def _plant_bare_call_at_ensure_mapper(script: str, fake: Path) -> str:
        """``ensure_mapper``'s existing-mapper check
        (``2>/dev/null | grep -Fx ... || true``) is a genuinely
        tolerant site: a non-zero exit from the piped command is
        swallowed by ``|| true``. Replace its ``acl_e2e_kc`` call with
        a BARE invocation of the same fake — exactly the defect class
        F4 exists to catch."""
        needle = (
            "existing=$(acl_e2e_kc get \\\n"
            '               "clients/${CLIENT_UUID}/protocol-mappers/models" \\\n'
            '               -r "${REALM}" --fields name --format csv --noquotes \\\n'
            '               2>/dev/null | grep -Fx "${MAPPER_NAME}" || true)'
        )
        assert script.count(needle) == 1, "ensure_mapper's tolerant site text moved"
        replacement = needle.replace("acl_e2e_kc get", f'"{fake}" get')
        return script.replace(needle, replacement, 1)

    @pytest.mark.parametrize("site", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_unwrapped_call_at_ensure_mapper_turns_the_proof_red(
        self, tmp_path: Path, site: Path
    ) -> None:
        """The reviewer's Y1 (ConfigMap/site 4) AND Y2 (host
        script/site 5) — both named, both re-run individually, never
        batched."""
        h = RunFullHarness(tmp_path, site)
        srv, port = _listening_socket()
        try:
            mutated = self._plant_bare_call_at_ensure_mapper(
                h.build_script(tcp_port=port), h.fake
            )
            h.build_script = lambda *, tcp_port: mutated  # type: ignore[method-assign]
            with pytest.raises(AssertionError, match="unwrapped kcadm call"):
                h.run(
                    state=_run_full_state(scopes_present=True, e2e_present=True),
                    tcp_port=port,
                )
        finally:
            srv.close()


@pytest.mark.parametrize("site", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
class TestRunFullWholeScriptCoverage:
    """BF2-1 primary dynamic proof over the WHOLE script + SF3-3/SF3-4:
    coverage is MEASURED (printed + asserted against the static list),
    never asserted blind. A non-empty unreached set is a finding this
    test prints for the build record, not a silent pass."""

    def test_all_absent_state_reaches_the_end(self, tmp_path: Path, site: Path) -> None:
        srv, port = _listening_socket()
        try:
            h = RunFullHarness(tmp_path, site)
            proc = h.run(
                state=_run_full_state(scopes_present=False, e2e_present=False),
                tcp_port=port,
            )
            assert "memory-scopes provisioning complete" in proc.stdout, proc.stderr
            assert "acl-e2e:" not in proc.stdout or proc.returncode == 0
        finally:
            srv.close()

    def test_all_present_state_reaches_the_end(
        self, tmp_path: Path, site: Path
    ) -> None:
        srv, port = _listening_socket()
        try:
            h = RunFullHarness(tmp_path, site)
            proc = h.run(
                state=_run_full_state(scopes_present=True, e2e_present=True),
                tcp_port=port,
            )
            assert "memory-scopes provisioning complete" in proc.stdout, proc.stderr
        finally:
            srv.close()

    def test_coverage_is_measured_and_unreached_lines_are_named(
        self, tmp_path: Path, site: Path
    ) -> None:
        reached: set[int] = set()
        logs: list[str] = []
        ranges: list[tuple[int, int]] = []
        for scopes_present, e2e_present in ((False, False), (True, True)):
            srv, port = _listening_socket()
            try:
                case_dir = tmp_path / f"{scopes_present}-{e2e_present}"
                case_dir.mkdir(parents=True, exist_ok=True)
                h = RunFullHarness(case_dir, site)
                # The static list is measured against the EXACT text
                # executed (SF3-3): for the ConfigMap this is the
                # template-stripped, dedented script, not the YAML
                # source — its line numbers differ from the source
                # file's, and BASH_LINENO reports against what ran.
                ranges = _invocation_lines(h.build_script(tcp_port=port))
                h.run(
                    state=_run_full_state(
                        scopes_present=scopes_present, e2e_present=e2e_present
                    ),
                    tcp_port=port,
                )
                reached |= h.logged_lines()
                logs.append(h.log.read_text())
            finally:
                srv.close()
        # Should-fix: drive the auth loop's FINAL-ATTEMPT line (SF3-4) —
        # only the ConfigMap site has the bounded retry loop at all; the
        # host-run script's single unguarded auth call is already
        # covered by the two successful states above. Authentication is
        # made to fail for the whole `ACL_E2E_AUTH_BUDGET_S=5` budget so
        # the loop exhausts and falls through to the `|| true` final
        # attempt the normal happy-path two states never reach.
        if site == CONFIGMAP:
            srv, port = _listening_socket()
            try:
                case_dir = tmp_path / "auth-exhausted"
                case_dir.mkdir(parents=True, exist_ok=True)
                h = RunFullHarness(case_dir, site)
                # `fail_match`'s `nth` counter fires exactly once, so a
                # plain `rc: 1` would only fail the FIRST retry attempt
                # and the second would then authenticate normally (the
                # counter is never re-armed). Instead, the ONE call
                # that matches hangs for LONGER than the whole 5s auth
                # budget (under the per-call timeout's own 20s default,
                # so `timeout` never kills it first) — the loop's
                # deadline check, run right after that single attempt
                # returns, is already past due.
                proc = h.run(
                    state={
                        **_run_full_state(scopes_present=False, e2e_present=False),
                        "fail_match": [
                            {"substr": "config credentials", "hang_seconds": 8}
                        ],
                    },
                    tcp_port=port,
                )
                assert proc.returncode != 0
                assert "kcadm authentication failed" in proc.stderr
                reached |= h.logged_lines()
            finally:
                srv.close()
        # A call is "reached" if ANY logged line falls inside its
        # range (SF3-3: a multi-line `$(... \` continuation reports
        # BASH_LINENO as its LAST line, not the line the token sits
        # on — range membership, not exact-line equality).
        unreached = [
            start if start == end else (start, end)
            for start, end in ranges
            if not any(start <= r <= end for r in reached)
        ]
        # Every remaining range is a NAMED, MEASURED exception, never a
        # silent pass (should-fix item: this used to be a bare print).
        # `find_scope_id`/`find_client_id`/`find_policy_id` (ConfigMap
        # only — the host-run script has no process-substitution-sourced
        # call) run hundreds of times across the two states above (the
        # combined call log — asserted below — proves it), but bash
        # reports `BASH_LINENO` for a call inside `done < <(...)` as the
        # ENCLOSING FUNCTION's own `name() {` line, never the textual
        # call-site range `_invocation_lines` computes — a measured
        # range/report mismatch, not a coverage gap.
        combined_log = "\n".join(logs)
        if site == CONFIGMAP:
            for substr in (
                "get client-scopes -r audittrace --fields id,name",
                "get clients -r audittrace -q clientId=",
                "get clients/preexisting-5/authz/resource-server/policy",
            ):
                assert substr in combined_log, (
                    f"expected call never logged at all: {substr}"
                )
            expected_unreached = {
                (560, 561),
                (607, 609),
                (901, 903),
                1101,
                (1156, 1157),
                (1282, 1283),
            }
        else:
            expected_unreached = {(834, 836), 1034, (1089, 1090)}
        # Named per range (line numbers re-measured after every edit
        # that shifts them — e.g. this round's R4/B3 production fixes;
        # update HERE, never silently, per review should-fix #7):
        #  - (560,561)/(607,609)/(1282,1283) ConfigMap-only: the
        #    process-substitution BASH_LINENO mismatch above.
        #  - (834,836)/(901,903): `acl_e2e_strip_scopes`'s delete — the
        #    created client's `defaultClientScopes` always equals the
        #    pinned set exactly, so there is never anything to strip in
        #    a RunFull state; F-2f (StepHarness, with an overridden
        #    listing call) exercises it instead.
        #  - 1101/1034: `acl_e2e_shape_capture`'s read — gated behind
        #    `AUDITTRACE_ACL_E2E_SHAPE_CAPTURE` (default off); exercised
        #    separately by `TestShapeCapturePrinter`.
        #  - (1089,1090)/(1156,1157): the disabled/sunset branch's
        #    delete — RunFull always runs with the default
        #    `enabled=true`; exercised separately by `TestDisabledSunset`
        #    in test_acl_e2e_provisioner_script.py.
        assert set(unreached) == expected_unreached, (
            f"[{site.name}] unreached set changed — update the named "
            f"exceptions above or the build. unreached={unreached}"
        )
        print(f"[{site.name}] unreached acl_e2e_kc call lines (named): {unreached}")
