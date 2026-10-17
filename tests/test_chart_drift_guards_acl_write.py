"""Drift guards for the ACL write scope and the dedicated E2E client
(ACL WU-2c-A, spec §4 + Addenda A S4/S12, B §1/§3/§4, C §2 SB-1..SB-3,
gate round-4 SC-1).

Sibling of ``test_chart_drift_guards.py`` (which is 5.6k lines; this
keeps the new classes in a module of their own and reuses its helpers).

* ``TestKeycloakAclWriteScopeGovernance`` — the write array in both
  provisioners: exact, librechat-only OPTIONAL bind loop, nothing forbidden.
* ``TestAclE2eClient`` — the dedicated ``audittrace-acl-e2e`` client in
  BOTH realm files with EXACTLY the pinned fields; the protocol mappers
  compared EXACTLY (SC-1: the mapper name set is ``{aud-audittrace-server}``
  and the pinned mapper's ``protocolMapper`` + ``config`` are equal — a
  by-name presence check would pass a second mapper that writes ``scope``).
* ``TestAclWriteScopeHolders`` — holders of ``memory:acl:write`` (and of
  ``memory:acl:read-own``) are exactly ``{audittrace-librechat,
  audittrace-acl-e2e}`` when the sunset flag is on and
  ``{audittrace-librechat}`` when it is off — asserted from BOTH renders.
* ``TestAclE2eClientFlag`` — the sunset MECHANISM: the chart realm
  declares the client iff ``keycloak.aclE2eClient.enabled`` (both renders
  must parse as JSON — that is what catches the comma hazard), the hook
  Job passes the flag, and the dev realm copy follows the flag's default.
* ``TestAclE2eProvisionerParity`` — the pinned client JSON in the script
  and the ConfigMap is byte-identical and equals the realm block on the
  compared keys.

Every guard's neuter (recorded in the build record) turns it RED.
"""

from __future__ import annotations

import functools
import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.test_chart_drift_guards import (
    _LINT_SECRETS,
    CHART_DIR,
    NAMESPACE,
    RELEASE,
    REPO_ROOT,
    _rendered_realm_json,
)

E2E = "audittrace-acl-e2e"
LIBRECHAT = "audittrace-librechat"
WRITE = "memory:acl:write"
READ = "memory:acl:read-own"

SCRIPT = REPO_ROOT / "scripts" / "setup-memory-scopes.sh"
CONFIGMAP = CHART_DIR / "templates" / "keycloak" / "configmap-memory-scopes-script.yaml"
JOB = CHART_DIR / "templates" / "keycloak" / "job-memory-scopes.yaml"
DEV_REALM = REPO_ROOT / "keycloak" / "realm-audittrace.json"

# The pinned block — written literally so the realm files are compared to
# an independent statement of intent, not to themselves.
PINNED_FLAGS = {
    "publicClient": True,
    "standardFlowEnabled": False,
    "directAccessGrantsEnabled": False,
    "implicitFlowEnabled": False,
    "serviceAccountsEnabled": False,
    "consentRequired": True,
}
PINNED_ATTRIBUTES = {
    "oauth2.device.authorization.grant.enabled": "true",
    "oauth2.device.polling.interval": "5",
    "oauth2.device.code.lifespan": "120",
    "use.refresh.tokens": "false",
    "access.token.lifespan": "900",
}
PINNED_MAPPER = {
    "name": "aud-audittrace-server",
    "protocol": "openid-connect",
    "protocolMapper": "oidc-audience-mapper",
    "config": {
        "included.custom.audience": "audittrace-server",
        "id.token.claim": "false",
        "access.token.claim": "true",
    },
}
COMPARED_KEYS = ("clientId", *PINNED_FLAGS, "attributes")

# ── shared extraction helpers for the F/F1/F2/F3 diagnostics (reused by
# tests/test_acl_e2e_provisioner_script.py and
# tests/test_acl_e2e_failure_diagnostics.py — kept here so both import
# from ONE place and never duplicate the extraction logic) ────────────

_PREAMBLE_START = "# Duplicate the REAL stdout/stderr to fd 3/4 BEFORE"
_PREAMBLE_END = "set -E\n"
_WRAPPER_BEGIN = "# acl-e2e-kc-begin"
_WRAPPER_END = "# acl-e2e-kc-end\n"


def _dedent_lines(text: str) -> str:
    """Strip each line's OWN leading whitespace — not a common prefix —
    so the ConfigMap's extra 4-space YAML-block-scalar indentation never
    counts as a difference from the plain script's indentation, while a
    REAL content difference (a token, an extra line) still shows."""
    return "\n".join(line.lstrip() for line in text.splitlines())


def _preamble_text(path) -> str:
    """The script-wide trap/emit/report/fail diagnostics block, verbatim
    from the real source (never re-typed — this is what makes the new
    failure-line tests exercise the EMITTED artefact, not a re-
    implementation of it)."""
    text = path.read_text(encoding="utf-8")
    start = text.index(_PREAMBLE_START)
    end = text.index(_PREAMBLE_END, start) + len(_PREAMBLE_END)
    return text[start:end]


def _wrapper_block(path) -> str:
    """The ``acl_e2e_kc()`` wrapper, marker-delimited, verbatim."""
    text = path.read_text(encoding="utf-8")
    start = text.index(_WRAPPER_BEGIN)
    end = text.index(_WRAPPER_END, start) + len(_WRAPPER_END)
    return text[start:end]


def _strip_wrapper_blocks(text: str) -> str:
    out = text
    while _WRAPPER_BEGIN in out:
        start = out.index(_WRAPPER_BEGIN)
        end = out.index(_WRAPPER_END, start) + len(_WRAPPER_END)
        out = out[:start] + out[end:]
    return out


def _real_function_text(path, name: str) -> str:
    """The REAL, verbatim body of a top-level ``name() { ... }``
    function from source — never a stub, never re-typed (F1 SF-3:
    ``bind_scope`` must be the real production function in a test
    harness, not a bare-call stand-in that would silently defeat the
    dynamic marker proof)."""
    text = path.read_text(encoding="utf-8")
    marker = f"{name}() {{"
    start = text.index(marker)
    line_start = text.rfind("\n", 0, start) + 1
    indent = text[line_start:start]
    closer = "\n" + indent + "}\n"
    end = text.index(closer, start) + len(closer)
    return text[start:end]


class TestPreambleAndWrapperMirror:
    """F §1.4 / F2 S-c: both sites stay byte-mirrored — the trap/stage
    machinery is identical, and the ``acl_e2e_kc`` wrapper differs ONLY
    in the executable prefix."""

    def test_preamble_byte_identical_after_dedent(self) -> None:
        script_pre = _dedent_lines(_preamble_text(SCRIPT))
        cm_pre = _dedent_lines(_preamble_text(CONFIGMAP))
        assert script_pre == cm_pre

    def test_wrapper_blocks_differ_only_by_exec_prefix(self) -> None:
        script_wrap = _dedent_lines(_wrapper_block(SCRIPT))
        cm_wrap = _dedent_lines(_wrapper_block(CONFIGMAP))
        # The ONLY site-5-specific text is the kubectl-exec prefix in
        # front of the real kcadm.sh path; stripping it must make the
        # two blocks identical.
        kubectl_prefix = 'kubectl -n "${NAMESPACE}" exec -i "${KC_POD}" -c keycloak -- '
        assert kubectl_prefix in script_wrap
        normalised = script_wrap.replace(kubectl_prefix, "")
        assert normalised == cm_wrap, (
            "the two acl_e2e_kc() blocks diverge beyond the exec prefix"
        )

    def test_no_bare_kcadm_invocation_survives_outside_the_wrapper(self) -> None:
        """Quick sanity check (the SHAPE-COMPLETE static pin lives in
        ``tests/test_acl_e2e_failure_diagnostics.py``): after stripping
        the marker block, neither site has a real ``"${KCADM}"`` token
        or a ``kcadm <subcommand> `` invocation left outside it — every
        call goes through ``acl_e2e_kc`` (F2 BF1-a)."""
        for path in (SCRIPT, CONFIGMAP):
            stripped = _strip_wrapper_blocks(path.read_text(encoding="utf-8"))
            assert "${KCADM}" not in stripped, path.name
            assert not re.search(
                r"\bkcadm (get|create|update|delete|config) ", stripped
            ), path.name


def _render_with(*extra: str) -> list[dict]:
    """``_render`` of the drift module plus extra ``--set`` flags (the
    same ``--set`` style)."""
    cmd = [
        "helm",
        "template",
        RELEASE,
        str(CHART_DIR),
        "-n",
        NAMESPACE,
        "--set",
        "vault.enabled=true",
        "--set",
        "istio.enabled=true",
        *_LINT_SECRETS,
    ]
    for assignment in extra:
        cmd += ["--set", assignment]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return [
        d
        for d in yaml.safe_load_all(result.stdout)
        if isinstance(d, dict) and d.get("kind")
    ]


@functools.lru_cache(maxsize=2)
def _chart_realm_cached(enabled: bool) -> str:
    realm = _rendered_realm_json(
        _render_with(f"keycloak.aclE2eClient.enabled={str(enabled).lower()}")
    )
    return json.dumps(realm)


def _chart_realm(enabled: bool) -> dict:
    """The rendered chart realm for the flag value (rendered once per
    value per process; callers get a fresh deep copy)."""
    return json.loads(_chart_realm_cached(enabled))


def _dev_realm() -> dict:
    return json.loads(DEV_REALM.read_text(encoding="utf-8"))


def _client(realm: dict, client_id: str) -> dict | None:
    for c in realm.get("clients", []):
        if c.get("clientId") == client_id:
            return c
    return None


def _holders(realm: dict, scope: str) -> set[str]:
    return {
        c["clientId"]
        for c in realm.get("clients", [])
        if scope in (c.get("defaultClientScopes") or [])
        or scope in (c.get("optionalClientScopes") or [])
    }


def _both_realms_enabled() -> list[tuple[str, dict]]:
    return [
        ("keycloak/realm-audittrace.json", _dev_realm()),
        (
            "charts/audittrace/files/realm-audittrace.json (rendered, enabled)",
            _chart_realm(True),
        ),
    ]


# ── the write array in the two provisioners ────────────────────────────────


class TestKeycloakAclWriteScopeGovernance:
    _EXPECTED = frozenset({WRITE})
    _FORBIDDEN = frozenset(
        {
            READ,
            "audittrace:admin",
            "audittrace:audit",
            "audittrace:assessment:ingest",
            "audittrace:scan:retrigger",
            "memory:episodic:write",
            "memory:procedural:write",
            "memory:semantic:write",
            "memory:agents:write",
            "memory:prompts:write",
            "memory:corpus:decisions:write",
            "memory:corpus:skills:write",
            "memory:corpus:semantic:write",
        }
    )

    @staticmethod
    def _array(text: str) -> set[str]:
        m = re.search(r"MEMORY_ACL_WRITE_SCOPES=\(([^)]*)\)", text)
        assert m is not None, "MEMORY_ACL_WRITE_SCOPES=( ... ) block not found"
        # EVERY quoted element, not only ``memory:``-prefixed ones — a
        # prefix filter would make an ``audittrace:admin`` addition
        # invisible (found by the neuter run).
        return set(re.findall(r'"([^"]+)"', m.group(1)))

    @staticmethod
    def _bind_loop(text: str) -> str:
        m = re.search(
            r'for SCOPE in "\$\{MEMORY_ACL_WRITE_SCOPES\[@\]\}"; do(.*?)\bdone\b',
            text,
            re.S,
        )
        assert m is not None, "MEMORY_ACL_WRITE_SCOPES bind loop not found"
        return m.group(1)

    def test_arrays_match_and_exact(self) -> None:
        script = self._array(SCRIPT.read_text())
        cm = self._array(CONFIGMAP.read_text())
        assert script == cm == self._EXPECTED

    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_bind_loop_targets_librechat_only_as_optional(self, path) -> None:
        body = self._bind_loop(path.read_text())
        assert "audittrace-librechat" in body
        assert '"optional"' in body and '"default"' not in body
        for other in ("audittrace-opencode", "audittrace-webui", "admin-client", E2E):
            assert other not in body, other

    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_never_forbidden_scope_in_write_array(self, path) -> None:
        assert not (self._array(path.read_text()) & self._FORBIDDEN)

    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_e2e_step_sits_outside_the_acl_bind_loops(self, path) -> None:
        """SA-7: the ``ensure`` step is not folded into either ACL bind
        loop (so the loop guards and this one stay GREEN)."""
        text = path.read_text()
        for loop in (
            re.search(
                r'for SCOPE in "\$\{MEMORY_ACL_READ_SCOPES\[@\]\}"; do(.*?)\bdone\b',
                text,
                re.S,
            ),
            re.search(
                r'for SCOPE in "\$\{MEMORY_ACL_WRITE_SCOPES\[@\]\}"; do(.*?)\bdone\b',
                text,
                re.S,
            ),
        ):
            assert loop is not None and E2E not in loop.group(1)

    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_ensure_loop_creates_the_write_scope(self, path) -> None:
        text = path.read_text()
        header = re.search(
            r"for SCOPE in ((?:\"\$\{[A-Z_]+\[@\]\}\"\s*){2,}); do", text
        )
        assert header is not None
        assert '"${MEMORY_ACL_WRITE_SCOPES[@]}"' in header.group(1)


# ── the dedicated client, in both realm files ──────────────────────────────


class TestAclE2eClient:
    @pytest.mark.parametrize("which", [0, 1], ids=["dev-realm", "chart-render"])
    def test_client_has_exactly_the_pinned_block(self, which: int) -> None:
        label, realm = _both_realms_enabled()[which]
        c = _client(realm, E2E)
        assert c is not None, f"{label}: {E2E} missing"
        for key, value in PINNED_FLAGS.items():
            assert c.get(key) is value, f"{label}: {key}={c.get(key)!r}"
        assert c["enabled"] is True and c["protocol"] == "openid-connect"
        assert c["attributes"] == PINNED_ATTRIBUTES, label
        assert set(c["defaultClientScopes"]) == {READ, WRITE}, label
        assert len(c["defaultClientScopes"]) == 2, label
        assert c["optionalClientScopes"] == [], f"{label}: optional must be empty"
        assert c["redirectUris"] == ["urn:ietf:wg:oauth:2.0:oob"], label
        assert c["webOrigins"] == [], label
        assert len(c["description"]) <= 255 and "WU-4" in c["description"]

    @pytest.mark.parametrize("which", [0, 1], ids=["dev-realm", "chart-render"])
    def test_no_offline_access_no_profile_no_other_scope(self, which: int) -> None:
        label, realm = _both_realms_enabled()[which]
        c = _client(realm, E2E)
        assert c is not None
        every = set(c["defaultClientScopes"]) | set(c["optionalClientScopes"])
        assert every == {READ, WRITE}, f"{label}: widened to {sorted(every)}"
        assert "offline_access" not in every

    @pytest.mark.parametrize("which", [0, 1], ids=["dev-realm", "chart-render"])
    def test_sc1_protocol_mappers_compared_exactly(self, which: int) -> None:
        """SC-1: name SET equality (a second mapper — e.g. an
        ``oidc-hardcoded-claim-mapper`` writing ``scope`` — reddens it)
        AND the pinned mapper's ``protocolMapper`` + ``config`` equal."""
        label, realm = _both_realms_enabled()[which]
        c = _client(realm, E2E)
        assert c is not None
        mappers = c["protocolMappers"]
        assert {m["name"] for m in mappers} == {"aud-audittrace-server"}, label
        assert len(mappers) == 1, f"{label}: duplicate mapper names"
        assert mappers[0] == PINNED_MAPPER, label

    def test_realm_files_agree_on_the_compared_keys(self) -> None:
        dev, chart = (_client(r, E2E) for _l, r in _both_realms_enabled())
        assert dev is not None and chart is not None
        for key in COMPARED_KEYS:
            assert dev[key] == chart[key], key

    def test_write_scope_is_declared_in_both_realm_files(self) -> None:
        for label, realm in _both_realms_enabled():
            names = {s["name"] for s in realm["clientScopes"]}
            assert {READ, WRITE} <= names, label


class TestAclWriteScopeHolders:
    """S12 + SB-3: ``memory:acl:write`` is held by exactly the console's
    client and (while the sunset flag is on) the dedicated E2E client."""

    def test_enabled_holders(self) -> None:
        for label, realm in _both_realms_enabled():
            assert _holders(realm, WRITE) == {LIBRECHAT, E2E}, label
            assert _holders(realm, READ) == {LIBRECHAT, E2E}, label

    def test_disabled_holders_from_the_second_render(self) -> None:
        realm = _chart_realm(False)
        assert _holders(realm, WRITE) == {LIBRECHAT}
        assert _holders(realm, READ) == {LIBRECHAT}

    def test_librechat_holds_write_as_optional_never_default(self) -> None:
        for _label, realm in _both_realms_enabled():
            c = _client(realm, LIBRECHAT)
            assert c is not None
            assert WRITE in c["optionalClientScopes"]
            assert WRITE not in c["defaultClientScopes"]

    def test_restricted_and_opencode_never_hold_it(self) -> None:
        for _label, realm in _both_realms_enabled():
            for cid in (
                "audittrace-restricted",
                "audittrace-opencode",
                "audittrace-webui",
            ):
                assert WRITE not in _holders_of_client(realm, cid)


def _holders_of_client(realm: dict, cid: str) -> set[str]:
    c = _client(realm, cid) or {}
    return set(c.get("defaultClientScopes") or []) | set(
        c.get("optionalClientScopes") or []
    )


# ── WU 2c-A0 provisioner diagnostics: the Job's deadline/backoff/grace
# values (Addendum F §4, superseded by F1 §6 / F2 §4 / F3 S2-2 / S2-3)
# ────────────────────────────────────────────────────────────────────────


_RUNNER_CONFIG = REPO_ROOT / "scripts" / "deploy" / "runner" / "config.py"
# F3 S2-2: unmeasured — Steps 1-6's own processing time, never bounded
# by a named budget variable in source; AC-F12 closes this live. This
# ONE number stays a disclosed estimate, never claimed as sourced.
_ACL_E2E_POST_AUTH_BUDGET_S = 50


class TestAclE2eProvisionerDiagnosticsJobValues:
    """AC-F4 (superseded 240→280→**290**, F3 S2-2), AC-F9, AC-F8
    (superseded ``20 < 30`` → ``20 + 5 < 30``, F2 §3 / F3 S2-3) — every
    value READ FROM SOURCE (review R5), never typed as a Python
    constant, so a revert in the source reddens this test instead of
    the two numbers silently drifting apart."""

    @staticmethod
    def _job_spec() -> dict:
        text = JOB.read_text(encoding="utf-8")
        m = re.search(r"backoffLimit:\s*(\d+)", text)
        assert m is not None
        backoff = int(m.group(1))
        m = re.search(r"activeDeadlineSeconds:\s*(\d+)", text)
        assert m is not None
        deadline = int(m.group(1))
        m = re.search(r"restartPolicy:\s*(\w+)", text)
        assert m is not None
        restart = m.group(1)
        m = re.search(r"terminationGracePeriodSeconds:\s*(\d+)", text)
        assert m is not None
        grace = int(m.group(1))
        return {
            "backoffLimit": backoff,
            "activeDeadlineSeconds": deadline,
            "restartPolicy": restart,
            "terminationGracePeriodSeconds": grace,
        }

    @staticmethod
    def _runner_helm_timeout_s() -> int:
        text = _RUNNER_CONFIG.read_text(encoding="utf-8")
        m = re.search(r"\btimeout:\s*int\s*=\s*(\d+)", text)
        assert m is not None, _RUNNER_CONFIG
        return int(m.group(1))

    @staticmethod
    def _pre_step_budgets_s(path: Path) -> dict[str, int]:
        """ConfigMap-only (the host-run script has no retry loop at
        all — SF3-3): the TCP and auth budgets' own `:-<default>`
        values, and the loop's own `sleep <n>` literal, read straight
        from the text rather than retyped."""
        text = path.read_text(encoding="utf-8")
        m = re.search(r'ACL_E2E_TCP_BUDGET_S:-(\d+)\}"', text)
        assert m is not None, path.name
        tcp_budget = int(m.group(1))
        m = re.search(r'ACL_E2E_AUTH_BUDGET_S:-(\d+)\}"', text)
        assert m is not None, path.name
        auth_budget = int(m.group(1))
        start = text.index("pre-step:tcp-wait")
        end = text.index("pre-step:init", start)
        pre_step_text = text[start:end]
        sleeps = {int(n) for n in re.findall(r"\n\s*sleep (\d+)\n", pre_step_text)}
        assert sleeps == {2}, (
            f"expected exactly one distinct retry-sleep literal (2s) in "
            f"the tcp-wait/auth pre-step block, found {sleeps}"
        )
        m = re.search(r'timeout (\d+) bash -c "exec 3<>/dev/tcp', pre_step_text)
        assert m is not None, path.name
        tcp_connect_bound = int(m.group(1))
        return {
            "tcp_budget": tcp_budget,
            "auth_budget": auth_budget,
            "retry_sleep": sleeps.pop(),
            "tcp_connect_bound": tcp_connect_bound,
        }

    @staticmethod
    def _kcadm_timeout_and_kill_after_s(path: Path) -> tuple[int, int]:
        text = _wrapper_block(path)
        m = re.search(r'timeout -k (\d+) "\$\{ACL_E2E_KCADM_TIMEOUT:-(\d+)\}"', text)
        assert m is not None, path.name
        kill_after, default_timeout = int(m.group(1)), int(m.group(2))
        return kill_after, default_timeout

    def test_backoff_and_restart_policy(self) -> None:
        spec = self._job_spec()
        assert spec["backoffLimit"] == 2
        assert spec["restartPolicy"] == "Never"

    def test_grace_period_is_explicit(self) -> None:
        assert self._job_spec()["terminationGracePeriodSeconds"] == 30

    def test_deadline_is_290_and_fits_the_runner_timeout(self) -> None:
        deadline = self._job_spec()["activeDeadlineSeconds"]
        assert deadline == 290
        assert deadline < self._runner_helm_timeout_s()

    def test_deadline_closes_on_the_measured_worst_case(self) -> None:
        """F3 S2-2, deadline arithmetic corrected (review R5/should-fix
        — the review measured ~284s): 120 (TCP budget) + 5 (one
        in-flight ``timeout 5`` connect attempt) + 2 (ONE tcp-wait
        retry sleep before the reachable attempt) + 80 (auth budget) +
        20 + 5 (one in-flight kcadm call, timeout + kill-after) + 2
        (ONE auth retry sleep) + 50 (post-auth, unmeasured — AC-F12
        closes it) = 284 <= 290. Both retry sleeps, AND the tcp
        connect attempt's own bound, were missing from the original
        arithmetic (should-fix item)."""
        deadline = self._job_spec()["activeDeadlineSeconds"]
        budgets = self._pre_step_budgets_s(CONFIGMAP)
        kill_after, default_timeout = self._kcadm_timeout_and_kill_after_s(SCRIPT)
        worst_case = (
            budgets["tcp_budget"]
            + budgets["tcp_connect_bound"]
            + budgets["retry_sleep"]
            + budgets["auth_budget"]
            + default_timeout
            + kill_after
            + budgets["retry_sleep"]
            + _ACL_E2E_POST_AUTH_BUDGET_S
        )
        assert worst_case == 284
        assert worst_case <= deadline < self._runner_helm_timeout_s()

    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_kcadm_timeout_and_kill_after_beat_the_grace_period(self, path) -> None:
        """F2 S-b / F3 S2-3: ``ACL_E2E_KCADM_TIMEOUT`` default 20 +
        ``timeout -k 5`` kill-after must sit strictly below the pod's
        (now explicit) 30s grace period — the ONLY mechanism for a
        kcadm call nested inside a ``$(...)`` substitution, where a
        trapped signal is deferred until that subshell returns."""
        kill_after, default_timeout = self._kcadm_timeout_and_kill_after_s(path)
        grace = self._job_spec()["terminationGracePeriodSeconds"]
        assert kill_after + default_timeout < grace

    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_tool_checks_precede_any_mktemp_or_timeout_use(self, path) -> None:
        """AC-F11 / F3 S2-1: ``command -v timeout``/``command -v mktemp``
        are checked, named, and precede the state that depends on them."""
        text = path.read_text(encoding="utf-8")
        assert "command -v timeout" in text
        assert "command -v mktemp" in text
        assert "code=timeout-missing" in text
        assert "code=mktemp-missing" in text
        assert text.index("command -v timeout") < text.index(
            'ACL_E2E_REPORT_FILE="$(mktemp)"'
        )

    def test_shape_capture_flag_defaults_off_and_is_wired(self) -> None:
        values = yaml.safe_load((CHART_DIR / "values.yaml").read_text())
        assert values["keycloak"]["aclE2eClient"]["shapeCapture"] is False
        job_text = JOB.read_text(encoding="utf-8")
        assert "AUDITTRACE_ACL_E2E_SHAPE_CAPTURE" in job_text
        assert ".Values.keycloak.aclE2eClient.shapeCapture" in job_text
        for flag, expect in ((True, "1"), (False, "0")):
            docs = _render_with(
                f"keycloak.aclE2eClient.shapeCapture={str(flag).lower()}"
            )
            job = next(
                d
                for d in docs
                if d["kind"] == "Job"
                and "ensure-memory-scopes" in d["metadata"]["name"]
            )
            env = {
                e["name"]: e.get("value")
                for e in job["spec"]["template"]["spec"]["containers"][0]["env"]
            }
            assert env["AUDITTRACE_ACL_E2E_SHAPE_CAPTURE"] == expect


# ── the sunset mechanism ───────────────────────────────────────────────────


class TestAclE2eClientFlag:
    def test_both_renders_parse_and_declare_the_client_iff_enabled(self) -> None:
        """Each render must parse as JSON (the comma hazard: a trailing or
        missing comma makes one branch invalid) and contain the client iff
        the flag is true."""
        on, off = _chart_realm(True), _chart_realm(False)
        assert _client(on, E2E) is not None
        assert _client(off, E2E) is None
        assert [c["clientId"] for c in on["clients"]][:-1] == [
            c["clientId"] for c in off["clients"]
        ], "the flag must add ONLY the E2E client, at the end"
        assert on["clients"][-1]["clientId"] == E2E

    def test_values_default_is_enabled_in_2c_a(self) -> None:
        values = yaml.safe_load((CHART_DIR / "values.yaml").read_text())
        assert values["keycloak"]["aclE2eClient"] == {
            "enabled": True,
            "shapeCapture": False,
        }

    def test_hook_job_passes_the_flag_to_the_script(self) -> None:
        text = JOB.read_text()
        assert "AUDITTRACE_ACL_E2E_CLIENT_ENABLED" in text
        assert ".Values.keycloak.aclE2eClient.enabled" in text
        for flag, expect in (("true", "true"), ("false", "false")):
            docs = _render_with(f"keycloak.aclE2eClient.enabled={flag}")
            job = next(
                d
                for d in docs
                if d["kind"] == "Job"
                and "ensure-memory-scopes" in d["metadata"]["name"]
            )
            env = {
                e["name"]: e.get("value")
                for e in job["spec"]["template"]["spec"]["containers"][0]["env"]
            }
            assert env["AUDITTRACE_ACL_E2E_CLIENT_ENABLED"] == expect

    def test_script_defaults_the_flag_to_true_in_both_sites(self) -> None:
        for path in (SCRIPT, CONFIGMAP):
            assert (
                'ACL_E2E_ENABLED="${AUDITTRACE_ACL_E2E_CLIENT_ENABLED:-true}"'
                in path.read_text()
            )

    def test_dev_realm_follows_the_flag_default(self) -> None:
        """The forcing function for the manual sunset edit: when the flag's
        default flips to false, the hand-edited dev copy must stop
        declaring the client (and the two-realm guards redden until it
        does). No date-based test — a time bomb is the T12 class."""
        values = yaml.safe_load((CHART_DIR / "values.yaml").read_text())
        enabled = values["keycloak"]["aclE2eClient"]["enabled"]
        assert (_client(_dev_realm(), E2E) is not None) is enabled

    def test_sunset_delete_is_verify_then_delete_in_both_sites(self) -> None:
        """SB-2: exact-one + clientId-equal before ``delete``; never the
        ``head -1`` / 'not found (skipped)' tolerance on the destructive
        path (the disabled branch is sliced out of the step itself)."""
        for path in (SCRIPT, CONFIGMAP):
            text = path.read_text()
            step = text[
                text.index("ACL_E2E_CLIENT_ID=") : text.index(
                    "deleted (read-back empty)"
                )
            ]
            disabled = step[step.index("removing the dedicated E2E client") :]
            before_delete = disabled[: disabled.index(' delete "clients/')]
            assert "exactly one" in before_delete, path.name
            assert "ACL_E2E_FOUND#*," in before_delete, path.name
            assert "head -1" not in step, path.name
            assert "skipped" not in step, path.name


# ── the pinned JSON: parity between the two provisioners and the realm ────


def _pinned_json(path) -> tuple[str, dict]:
    m = re.search(r"ACL_E2E_CLIENT_JSON='(\{.*?\})'\n", path.read_text())
    assert m is not None, f"{path.name}: pinned ACL_E2E_CLIENT_JSON not found"
    return m.group(1), json.loads(m.group(1))


class TestAclE2eProvisionerParity:
    def test_pinned_json_is_byte_identical_in_both_sites(self) -> None:
        script_raw, _ = _pinned_json(SCRIPT)
        cm_raw, _ = _pinned_json(CONFIGMAP)
        assert script_raw == cm_raw

    @pytest.mark.parametrize("which", [0, 1], ids=["dev-realm", "chart-render"])
    def test_pinned_json_equals_the_realm_block_on_the_compared_keys(
        self, which: int
    ) -> None:
        _raw, pinned = _pinned_json(SCRIPT)
        realm_client = _both_realms_enabled()[which][1]
        c = _client(realm_client, E2E)
        assert c is not None
        for key in COMPARED_KEYS:
            assert pinned[key] == c[key], key
        assert set(pinned["defaultClientScopes"]) == set(c["defaultClientScopes"])
        assert set(pinned["optionalClientScopes"]) == set(c["optionalClientScopes"])
        assert pinned["optionalClientScopes"] == []
        assert {m["name"] for m in pinned["protocolMappers"]} == {
            m["name"] for m in c["protocolMappers"]
        }
        assert pinned["protocolMappers"] == c["protocolMappers"] == [PINNED_MAPPER]

    def test_pinned_json_description_fits_keycloaks_255_limit(self) -> None:
        _raw, pinned = _pinned_json(SCRIPT)
        assert len(pinned["description"]) <= 255

    def test_pinned_json_is_valid_for_a_printf_percent_free_create(self) -> None:
        """The JSON is fed through ``printf '%s'``; a ``%`` would be inert
        there but a single quote would break the shell literal."""
        raw, _ = _pinned_json(SCRIPT)
        assert "'" not in raw
