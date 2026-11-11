"""LC10A-CHAT-EXCHANGE-SCOPE (gate r3 A1): the ceiling's caller term is live.

Once the caller pre-check has passed, the required set is a subset of the
caller's scopes and the ``(inbound_scopes | OIDC_PROTOCOL_SCOPES)`` term of the
ceiling changes nothing. That left the term with no test of its own (neuters
N17 and N24 stayed GREEN). This test bypasses the pre-check, so the term is the
only thing standing between an under-scoped caller and the grant: dropping it
(N17) or widening it (N18) makes this test RED.
"""

from __future__ import annotations

import logging

import pytest

from tests.bff.test_chat_exchange_scope import (  # noqa: F401  (chat is a fixture)
    _chat_turn,
    chat,
    restricted_token_scopes,
)


class TestTheCeilingBoundsTheGrantByTheCallerAsABackstop:
    def test_an_under_scoped_caller_is_refused_when_the_pre_check_is_bypassed(
        self,
        chat,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, backend = chat
        monkeypatch.setattr("bff.app.missing_chat_scopes", lambda _scopes: [])
        with caplog.at_level(logging.INFO):
            response = _chat_turn(client, restricted_token_scopes())
        assert response.status_code == 502
        assert response.json() == {"detail": "Upstream authentication service error"}
        assert len(backend.exchange_forms) == 1
        assert backend.orchestrator_requests == []
        messages = [r.getMessage() for r in caplog.records]
        assert any("scope ceiling exceeded: unexpected=" in m for m in messages)
        assert not [
            m for m in messages if m.startswith("token_exchange.scopes granted=")
        ]
