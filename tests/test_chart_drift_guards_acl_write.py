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

import copy
import functools
import json
import re
import subprocess

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
        assert values["keycloak"]["aclE2eClient"] == {"enabled": True}

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


def test_mutating_the_realm_block_is_detected() -> None:
    """Self-attack on the instrument: a second mapper in a copy of the
    realm block changes the exact comparison."""
    c = copy.deepcopy(_client(_dev_realm(), E2E))
    assert c is not None
    c["protocolMappers"].append(
        {
            "name": "scope-injector",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-hardcoded-claim-mapper",
            "config": {"claim.name": "scope", "claim.value": "audittrace:admin"},
        }
    )
    assert {m["name"] for m in c["protocolMappers"]} != {"aud-audittrace-server"}
