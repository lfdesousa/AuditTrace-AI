"""Drift guards for the LC-10a ``audittrace-session-e2e`` client, the
session-scope wall and the console's ``X-Session-Id`` header.

Sibling of ``test_chart_drift_guards.py`` (6.4k lines; N5) and a clone of the
four acl-e2e properties that carry the control set
(``tests/test_chart_drift_guards_acl_write.py``):

* ``TestSessionE2eClient`` - the pinned block in BOTH realm files, protocol
  mappers compared exactly, both session scopes OPTIONAL (C4), never default;
* ``TestSessionE2eClientFlag`` - the client is rendered iff
  ``keycloak.sessionE2eClient.enabled``, the hook Job passes the flag, the
  sunset is verify-then-delete in both provisioner sites;
* ``TestSessionE2eProvisionerParity`` - the pinned client JSON is
  byte-identical in the script and the ConfigMap and equals the realm block;
  the two session scopes are bound by the client's own block, NOT by the
  ``MEMORY_SESSION_*_SCOPES`` bind loops;
* ``TestSessionScopeWall`` - AC-16b: the realm-wide holders of
  ``memory:session:write`` equal the admitted set under both flag values;
* ``TestOpencodeClientScopesUnchangedBySession``;
* ``TestLibrechatYamlSessionHeader`` - the custom endpoint sends
  ``X-Session-Id: {{LIBRECHAT_BODY_CONVERSATIONID}}`` (AC-P2 P-1 was GREEN
  only WITH that line).

Every guard's neuter is recorded in the build record.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.test_chart_drift_guards import CHART_DIR, _rendered_realm_json
from tests.test_chart_drift_guards_acl_write import (
    COMPARED_KEYS,
    CONFIGMAP,
    DEV_REALM,
    JOB,
    PINNED_ATTRIBUTES,
    PINNED_FLAGS,
    PINNED_MAPPER,
    SCRIPT,
    _client,
    _holders,
    _render_with,
)
from tests.test_console_k3s_guards import _librechat_yaml_configmap_doc
from tests.test_console_k3s_guards import _render as _render_console

E2E = "audittrace-session-e2e"
LIBRECHAT = "audittrace-librechat"
WRITE = "memory:session:write"
READ = "memory:session:read-own"
ADMITTED_SESSION_WRITE_CLIENTS = (LIBRECHAT, E2E)
DEFAULT_SCOPES = {"audittrace:query", "audittrace:audit"}
OPTIONAL_SCOPES = [WRITE, READ]


@functools.lru_cache(maxsize=2)
def _chart_realm_cached(enabled: bool) -> str:
    docs = _render_with(f"keycloak.sessionE2eClient.enabled={str(enabled).lower()}")
    return json.dumps(_rendered_realm_json(docs))


def _chart_realm(enabled: bool) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(_chart_realm_cached(enabled))
    return result


def _dev_realm() -> dict[str, Any]:
    result: dict[str, Any] = json.loads(DEV_REALM.read_text(encoding="utf-8"))
    return result


def _both_enabled() -> list[tuple[str, dict[str, Any]]]:
    return [
        ("keycloak/realm-audittrace.json", _dev_realm()),
        ("charts/audittrace/files/realm-audittrace.json (enabled)", _chart_realm(True)),
    ]


class TestSessionE2eClient:
    @pytest.mark.parametrize("which", [0, 1], ids=["dev-realm", "chart-render"])
    def test_client_has_exactly_the_pinned_block(self, which: int) -> None:
        label, realm = _both_enabled()[which]
        c = _client(realm, E2E)
        assert c is not None, f"{label}: {E2E} missing"
        for key, value in PINNED_FLAGS.items():
            assert c.get(key) is value, f"{label}: {key}={c.get(key)!r}"
        assert c["enabled"] is True and c["protocol"] == "openid-connect"
        assert c["attributes"] == PINNED_ATTRIBUTES, label
        assert set(c["defaultClientScopes"]) == DEFAULT_SCOPES, label
        assert len(c["defaultClientScopes"]) == 2, label
        # C4: BOTH session scopes OPTIONAL (requested by name), never default.
        assert sorted(c["optionalClientScopes"]) == sorted(OPTIONAL_SCOPES), label
        assert len(c["optionalClientScopes"]) == 2, label
        assert WRITE not in c["defaultClientScopes"], label
        assert READ not in c["defaultClientScopes"], label
        assert c["redirectUris"] == ["urn:ietf:wg:oauth:2.0:oob"], label
        assert c["webOrigins"] == [], label
        assert len(c["description"]) <= 255

    @pytest.mark.parametrize("which", [0, 1], ids=["dev-realm", "chart-render"])
    def test_no_offline_access_no_profile_no_other_scope(self, which: int) -> None:
        label, realm = _both_enabled()[which]
        c = _client(realm, E2E)
        assert c is not None
        every = set(c["defaultClientScopes"]) | set(c["optionalClientScopes"])
        assert every == DEFAULT_SCOPES | set(OPTIONAL_SCOPES), label
        assert "offline_access" not in every

    @pytest.mark.parametrize("which", [0, 1], ids=["dev-realm", "chart-render"])
    def test_protocol_mappers_compared_exactly(self, which: int) -> None:
        label, realm = _both_enabled()[which]
        c = _client(realm, E2E)
        assert c is not None
        assert [m for m in c["protocolMappers"]] == [PINNED_MAPPER], label

    def test_realm_files_agree_on_the_compared_keys(self) -> None:
        dev, chart = (_client(r, E2E) for _l, r in _both_enabled())
        assert dev is not None and chart is not None
        for key in COMPARED_KEYS:
            assert dev[key] == chart[key], key
        assert set(dev["optionalClientScopes"]) == set(chart["optionalClientScopes"])
        assert set(dev["defaultClientScopes"]) == set(chart["defaultClientScopes"])

    def test_session_scopes_are_declared_in_both_realm_files(self) -> None:
        for label, realm in _both_enabled():
            names = {s["name"] for s in realm["clientScopes"]}
            assert {WRITE, READ} <= names, label


class TestSessionScopeWall:
    """AC-16b (C4b): the realm-wide holders of ``memory:session:write`` across
    ``defaultClientScopes`` UNION ``optionalClientScopes`` are exactly the
    admitted set (flag on) / the console client alone (flag off), and the
    write scope appears only under ``optionalClientScopes``.

    Neuters: grant it to ``audittrace-webui`` in one realm file -> RED;
    render it as a DEFAULT scope of any client -> RED."""

    def test_enabled_holders_equal_the_admitted_set(self) -> None:
        for label, realm in _both_enabled():
            assert _holders(realm, WRITE) == set(ADMITTED_SESSION_WRITE_CLIENTS), label

    def test_disabled_holders_from_the_second_render(self) -> None:
        assert _holders(_chart_realm(False), WRITE) == {LIBRECHAT}

    def test_write_scope_is_only_ever_optional(self) -> None:
        for label, realm in [*_both_enabled(), ("disabled", _chart_realm(False))]:
            for c in realm["clients"]:
                assert WRITE not in (c.get("defaultClientScopes") or []), (
                    label,
                    c["clientId"],
                )

    def test_read_own_holders_are_librechat_and_the_e2e_client_only(self) -> None:
        for label, realm in _both_enabled():
            assert _holders(realm, READ) == set(ADMITTED_SESSION_WRITE_CLIENTS), label
        assert _holders(_chart_realm(False), READ) == {LIBRECHAT}


class TestOpencodeClientScopesUnchangedBySession:
    """The operator's OpenCode client never gains a session scope. Neuter:
    add ``memory:session:read-own`` to ``audittrace-opencode`` in either
    realm -> RED."""

    @pytest.mark.parametrize("which", [0, 1], ids=["dev-realm", "chart-render"])
    def test_opencode_scope_set_never_gains_session_scopes(self, which: int) -> None:
        label, realm = _both_enabled()[which]
        c = _client(realm, "audittrace-opencode")
        assert c is not None, label
        held = set(c.get("defaultClientScopes") or []) | set(
            c.get("optionalClientScopes") or []
        )
        assert not {s for s in held if s.startswith("memory:session:")}, label


class TestSessionE2eClientFlag:
    def test_both_renders_parse_and_declare_the_client_iff_enabled(self) -> None:
        on, off = _chart_realm(True), _chart_realm(False)
        assert _client(on, E2E) is not None
        assert _client(off, E2E) is None
        assert [c["clientId"] for c in on["clients"]][:-1] == [
            c["clientId"] for c in off["clients"]
        ], "the flag must add ONLY the session E2E client, at the end"
        assert on["clients"][-1]["clientId"] == E2E

    def test_values_default_is_enabled(self) -> None:
        values = yaml.safe_load((CHART_DIR / "values.yaml").read_text())
        assert values["keycloak"]["sessionE2eClient"] == {"enabled": True}

    def test_hook_job_passes_the_flag_to_the_script(self) -> None:
        assert ".Values.keycloak.sessionE2eClient.enabled" in JOB.read_text()
        for flag in ("true", "false"):
            docs = _render_with(f"keycloak.sessionE2eClient.enabled={flag}")
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
            assert env["AUDITTRACE_SESSION_E2E_CLIENT_ENABLED"] == flag

    def test_script_defaults_the_flag_to_true_in_both_sites(self) -> None:
        for path in (SCRIPT, CONFIGMAP):
            assert (
                'SESSION_E2E_ENABLED="${AUDITTRACE_SESSION_E2E_CLIENT_ENABLED:-true}"'
                in path.read_text()
            )

    def test_dev_realm_follows_the_flag_default(self) -> None:
        values = yaml.safe_load((CHART_DIR / "values.yaml").read_text())
        enabled = values["keycloak"]["sessionE2eClient"]["enabled"]
        assert (_client(_dev_realm(), E2E) is not None) is enabled

    def test_sunset_delete_is_verify_then_delete_in_both_sites(self) -> None:
        """Exact-one + clientId-equal before ``delete``; never the
        ``head -1`` / 'skipped' tolerance on the destructive path. Neuter:
        ``update`` instead of fail on drift is covered by the fail-closed
        test below."""
        for path in (SCRIPT, CONFIGMAP):
            text = path.read_text()
            step = text[
                text.index("SESSION_E2E_CLIENT_ID=") : text.index(
                    "deleted (read-back empty)",
                    text.index("SESSION_E2E_CLIENT_ID="),
                )
            ]
            disabled = step[step.index("removing the dedicated session E2E client") :]
            before_delete = disabled[: disabled.index(' delete "clients/')]
            assert "exactly one" in before_delete, path.name
            assert "SESSION_E2E_FOUND#*," in before_delete, path.name
            assert "head -1" not in step, path.name
            assert "skipped" not in step, path.name

    def test_drift_fails_closed_and_never_updates_the_client(self) -> None:
        for path in (SCRIPT, CONFIGMAP):
            text = path.read_text()
            start = text.index("SESSION_E2E_CLIENT_ID=")
            step = text[start : text.rindex("deleted (read-back empty)")]
            assert "acl_e2e_fail drift" in step, path.name
            assert not re.search(r'acl_e2e_kc update "clients/\$\{(uuid|SESSION)', step)
            assert 'acl_e2e_kc update "clients/${uuid}"' not in step, path.name


def _pinned_json(path: Path) -> tuple[str, dict[str, Any]]:
    m = re.search(r"SESSION_E2E_CLIENT_JSON='(\{.*?\})'\n", path.read_text())
    assert m is not None, f"{path.name}: pinned SESSION_E2E_CLIENT_JSON not found"
    return m.group(1), json.loads(m.group(1))


class TestSessionE2eProvisionerParity:
    def test_pinned_json_is_byte_identical_in_both_sites(self) -> None:
        script_raw, _ = _pinned_json(SCRIPT)
        cm_raw, _ = _pinned_json(CONFIGMAP)
        assert script_raw == cm_raw

    @pytest.mark.parametrize("which", [0, 1], ids=["dev-realm", "chart-render"])
    def test_pinned_json_equals_the_realm_block(self, which: int) -> None:
        _raw, pinned = _pinned_json(SCRIPT)
        c = _client(_both_enabled()[which][1], E2E)
        assert c is not None
        for key in COMPARED_KEYS:
            assert pinned[key] == c[key], key
        assert set(pinned["defaultClientScopes"]) == set(c["defaultClientScopes"])
        assert (
            pinned["optionalClientScopes"]
            == c["optionalClientScopes"]
            == OPTIONAL_SCOPES
        )
        assert pinned["protocolMappers"] == c["protocolMappers"] == [PINNED_MAPPER]
        assert pinned["description"] == c["description"]

    def test_pinned_json_is_shell_and_keycloak_safe(self) -> None:
        raw, pinned = _pinned_json(SCRIPT)
        assert "'" not in raw
        assert len(pinned["description"]) <= 255

    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_session_scopes_are_not_bound_through_the_session_bind_loops(
        self, path: Path
    ) -> None:
        """C4: the e2e client's session scopes are pinned in ITS block; the
        ``MEMORY_SESSION_*_SCOPES`` loops stay librechat-only."""
        text = path.read_text()
        for arr in ("MEMORY_SESSION_WRITE_SCOPES", "MEMORY_SESSION_READ_SCOPES"):
            m = re.search(
                rf'for SCOPE in "\$\{{{arr}\[@\]\}}"; do(.*?)\bdone\b', text, re.S
            )
            assert m is not None, arr
            assert "e2e" not in m.group(1).lower(), arr
            assert "audittrace-librechat" in m.group(1), arr
        block = text[text.index("SESSION_E2E_CLIENT_ID=") :]
        assert 'bind_scope "${SESSION_E2E_CLIENT_ID}" "${SCOPE}" "optional"' in block, (
            "the e2e block must bind its two session scopes as OPTIONAL"
        )
        assert (
            'SESSION_E2E_DEFAULT_SCOPES=("audittrace:query" "audittrace:audit")'
            in block
        )
        assert (
            'SESSION_E2E_OPTIONAL_SCOPES=("memory:session:write" "memory:session:read-own")'
            in block
        )


class TestSessionBindLoopsAdmitOnlyTheConsoleClient:
    """AC-16 / AC-16a: SET EQUALITY on the clients the session bind loops
    name (script, ConfigMap, and the RENDERED Job script), optional-only for
    the write loop. The admitted set is wider than the loops by exactly the
    e2e client, which the loops never name (C4).

    Neuter: a third client in one bind loop -> RED; a ``"default"`` bind in
    the write loop -> RED; render the e2e client with the flag off -> the
    flag cell RED."""

    @staticmethod
    def _loop_clients(text: str, arr: str) -> set[str]:
        m = re.search(
            rf'for SCOPE in "\$\{{{arr}\[@\]\}}"; do(.*?)\bdone\b', text, re.S
        )
        assert m is not None, arr
        return set(re.findall(r'bind_scope "([^"]+)"', m.group(1)))

    @pytest.mark.parametrize("path", [SCRIPT, CONFIGMAP], ids=["script", "configmap"])
    def test_write_loop_clients_equal_the_admitted_set_minus_the_e2e_client(
        self, path: Path
    ) -> None:
        got = self._loop_clients(path.read_text(), "MEMORY_SESSION_WRITE_SCOPES")
        assert got == set(ADMITTED_SESSION_WRITE_CLIENTS) - {E2E}

    @pytest.mark.parametrize("flag", ["true", "false"])
    def test_rendered_job_admits_the_e2e_client_iff_the_flag(self, flag: str) -> None:
        docs = _render_with(f"keycloak.sessionE2eClient.enabled={flag}")
        cm = next(
            d
            for d in docs
            if d["kind"] == "ConfigMap"
            and d["metadata"]["name"].endswith("-memory-scopes-script")
        )
        script = cm["data"]["ensure-memory-scopes.sh"]
        loop = self._loop_clients(script, "MEMORY_SESSION_WRITE_SCOPES")
        job = next(
            d
            for d in docs
            if d["kind"] == "Job" and "ensure-memory-scopes" in d["metadata"]["name"]
        )
        env = {
            e["name"]: e.get("value")
            for e in job["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        admitted = loop | (
            {E2E} if env["AUDITTRACE_SESSION_E2E_CLIENT_ENABLED"] == "true" else set()
        )
        expect = set(ADMITTED_SESSION_WRITE_CLIENTS) if flag == "true" else {LIBRECHAT}
        assert admitted == expect
        assert '"default"' not in re.search(
            r'for SCOPE in "\$\{MEMORY_SESSION_WRITE_SCOPES\[@\]\}"; do(.*?)\bdone\b',
            script,
            re.S,
        ).group(1)  # type: ignore[union-attr]


class TestLibrechatYamlSessionHeader:
    """AC-P2 P-1: the custom endpoint resolves ``{{LIBRECHAT_BODY_CONVERSATIONID}}``
    into ``X-Session-Id`` only WITH the line below, so the line is pinned.

    Neuter: delete the ``X-Session-Id`` line from the ConfigMap template ->
    RED; rename the placeholder -> RED; drop ``Authorization`` -> RED."""

    def _headers(self) -> dict[str, str]:
        doc = _librechat_yaml_configmap_doc(_render_console(console_enabled=True))
        headers: dict[str, str] = doc["endpoints"]["custom"][0]["headers"]
        return headers

    def test_x_session_id_header_carries_the_body_conversation_id(self) -> None:
        assert self._headers()["X-Session-Id"] == "{{LIBRECHAT_BODY_CONVERSATIONID}}"

    def test_authorization_header_is_still_the_oidc_placeholder(self) -> None:
        assert (
            self._headers()["Authorization"]
            == "Bearer {{LIBRECHAT_OPENID_ACCESS_TOKEN}}"
        )

    def test_the_two_headers_are_the_whole_set(self) -> None:
        assert set(self._headers()) == {"Authorization", "X-Session-Id"}
