"""LC10A-CHAT-EXCHANGE-SCOPE (ADDENDUM-1): the chat exchange-audience client in
BOTH realm files (the compose twin and the rendered chart realm).

The client is the token-exchange audience of the chat turn. Keycloak mints its
DEFAULT scopes on every exchange (measured, K15-K24), so its scope arrays ARE
the chat grant: they are pinned by equality to the set derived from the server.
"""

from __future__ import annotations

from typing import Any

import pytest

import audittrace.tools.memory_handlers  # noqa: F401  (registers the memory tools)
from audittrace.tools import MEMORY_TOOL_REGISTRY
from tests.test_chart_drift_guards_acl_write import PINNED_MAPPER, _client, _holders
from tests.test_chart_drift_guards_session_e2e import (
    E2E,
    LIBRECHAT,
    READ,
    _both_enabled,
    _chart_realm,
)

CHAT = "audittrace-librechat-chat"
BFF = "audittrace-librechat-bff"
CHAT_ROUTE_SCOPE = "audittrace:query"
PINNED_CHAT_FLAGS = {
    "enabled": True,
    "publicClient": True,
    "standardFlowEnabled": False,
    "directAccessGrantsEnabled": False,
    "implicitFlowEnabled": False,
    "serviceAccountsEnabled": False,
    "fullScopeAllowed": False,
}


def _expected_chat_scopes() -> list[str]:
    assert len(MEMORY_TOOL_REGISTRY) >= 7, sorted(MEMORY_TOOL_REGISTRY)
    tools = {tool.required_scope for tool in MEMORY_TOOL_REGISTRY.values()}
    return sorted(tools | {CHAT_ROUTE_SCOPE})


def _renders() -> list[tuple[str, dict[str, Any]]]:
    return [
        *_both_enabled(),
        ("chart realm (session e2e disabled)", _chart_realm(False)),
    ]


@pytest.mark.parametrize("which", [0, 1, 2], ids=["dev-realm", "chart-on", "chart-off"])
class TestChatAudienceClient:
    def test_scope_arrays_are_exactly_the_chat_set(self, which: int) -> None:
        label, realm = _renders()[which]
        c = _client(realm, CHAT)
        assert c is not None, f"{label}: {CHAT} missing"
        assert sorted(c["defaultClientScopes"]) == _expected_chat_scopes(), label
        assert len(c["defaultClientScopes"]) == len(set(c["defaultClientScopes"])), (
            label
        )
        assert c["optionalClientScopes"] == [], label

    def test_it_is_an_audience_only_client_with_no_role_claims(
        self, which: int
    ) -> None:
        label, realm = _renders()[which]
        c = _client(realm, CHAT)
        assert c is not None, f"{label}: {CHAT} missing"
        for key, value in PINNED_CHAT_FLAGS.items():
            assert c.get(key) is value, f"{label}: {key}={c.get(key)!r}"
        assert c["protocol"] == "openid-connect", label
        assert c.get("redirectUris", []) == [], label
        assert c["attributes"] == {"token-exchange.authorized-source-client": BFF}, (
            label
        )
        assert c["protocolMappers"] == [PINNED_MAPPER], label

    def test_it_sits_right_after_the_bff_client(self, which: int) -> None:
        label, realm = _renders()[which]
        ids = [c["clientId"] for c in realm["clients"]]
        assert ids.count(CHAT) == 1, label
        assert ids.index(CHAT) == ids.index(BFF) + 1, (label, ids)


class TestSessionReadOwnHolders:
    """Supersedes ``TestSessionScopeWall::test_read_own_holders_are_librechat_
    and_the_e2e_client_only``: three holders with the e2e flag on, two off."""

    def test_enabled_holders(self) -> None:
        for label, realm in _both_enabled():
            assert _holders(realm, READ) == {LIBRECHAT, CHAT, E2E}, label

    def test_disabled_holders(self) -> None:
        assert _holders(_chart_realm(False), READ) == {LIBRECHAT, CHAT}
