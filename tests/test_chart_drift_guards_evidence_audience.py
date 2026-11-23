"""CONSOLE-AUDIT-EVIDENCE WU-C (ADDENDUM-A6 section 3 item 1, G13''(a)): the
evidence exchange-audience client in BOTH realm files (the compose twin and the
rendered chart realm).

Sibling of ``test_chart_drift_guards_chat_audience.py``. The client is the
token-exchange audience of the evidence proxy. Keycloak mints its DEFAULT scopes
on every exchange, so its scope arrays ARE the evidence grant: exactly
``["audittrace:audit"]``, no optional scope.

Neuter (recorded): add ``audittrace:query`` to either realm file's default scopes
-> ``test_scope_arrays_are_exactly_audit`` RED; add an optional scope ->
the same test RED; set ``fullScopeAllowed`` true -> the flags test RED.
"""

from __future__ import annotations

import pytest

from bff.console_evidence_scopes import EVIDENCE_SCOPES
from tests.test_chart_drift_guards_acl_write import PINNED_MAPPER, _client
from tests.test_chart_drift_guards_chat_audience import (
    CHAT,
    PINNED_CHAT_FLAGS,
    _renders,
)

EVIDENCE = "audittrace-librechat-evidence"
BFF = "audittrace-librechat-bff"


@pytest.mark.parametrize("which", [0, 1, 2], ids=["dev-realm", "chart-on", "chart-off"])
class TestEvidenceAudienceClient:
    def test_scope_arrays_are_exactly_audit(self, which: int) -> None:
        label, realm = _renders()[which]
        c = _client(realm, EVIDENCE)
        assert c is not None, f"{label}: {EVIDENCE} missing"
        assert c["defaultClientScopes"] == ["audittrace:audit"], label
        assert tuple(c["defaultClientScopes"]) == EVIDENCE_SCOPES, label
        assert c["optionalClientScopes"] == [], label

    def test_it_is_an_audience_only_client_with_no_role_claims(
        self, which: int
    ) -> None:
        label, realm = _renders()[which]
        c = _client(realm, EVIDENCE)
        assert c is not None, f"{label}: {EVIDENCE} missing"
        for key, value in PINNED_CHAT_FLAGS.items():
            assert c.get(key) is value, f"{label}: {key}={c.get(key)!r}"
        assert c["protocol"] == "openid-connect", label
        assert c.get("redirectUris", []) == [], label
        assert c["attributes"] == {"token-exchange.authorized-source-client": BFF}, (
            label
        )
        assert c["protocolMappers"] == [PINNED_MAPPER], label

    def test_it_is_byte_for_byte_the_chat_clients_shape(self, which: int) -> None:
        """Same keys, same flags, same attributes and mapper as the chat
        audience; only the identity and the scope arrays differ."""
        label, realm = _renders()[which]
        chat, evid = _client(realm, CHAT), _client(realm, EVIDENCE)
        assert chat is not None and evid is not None, label
        differing = {k for k in chat.keys() | evid.keys() if chat.get(k) != evid.get(k)}
        assert differing == {
            "clientId",
            "description",
            "defaultClientScopes",
        }, (label, differing)

    def test_it_sits_right_after_the_chat_client(self, which: int) -> None:
        label, realm = _renders()[which]
        ids = [c["clientId"] for c in realm["clients"]]
        assert ids.count(EVIDENCE) == 1, label
        assert ids.index(EVIDENCE) == ids.index(CHAT) + 1, (label, ids)
        assert ids.index(CHAT) == ids.index(BFF) + 1, (label, ids)

    def test_the_realm_defines_the_scope_it_binds(self, which: int) -> None:
        label, realm = _renders()[which]
        assert "audittrace:audit" in {s["name"] for s in realm["clientScopes"]}, label


class TestNoOtherClientGainedAudit:
    def test_the_evidence_client_is_a_new_holder_only_as_default(self) -> None:
        for label, realm in _renders():
            holders = {
                c["clientId"]
                for c in realm["clients"]
                if "audittrace:audit" in c.get("defaultClientScopes", [])
            }
            assert EVIDENCE in holders, label
            assert CHAT not in holders, label
