"""The BFF names the scopes a token exchange actually granted, and the
requested ones it dropped (Keycloak 24's legacy exchange drops optional
scopes the subject token lacks and still answers 200). Names only: the
token itself is never logged."""

from __future__ import annotations

import logging
import time

import httpx
import pytest
from jose import jwt

from bff.exchange import exchange_token
from tests.bff.conftest import (
    TEST_ISSUER,
    TEST_PRIVATE_PEM,
    TEST_PUBLIC_PEM,
    make_token,
)
from tests.bff.test_exchange import _settings


def _minted(scope: str) -> str:
    now = int(time.time())
    token: str = jwt.encode(
        {
            "iss": TEST_ISSUER,
            "sub": "alice",
            "aud": "audittrace-server",
            "scope": scope,
            "iat": now,
            "exp": now + 300,
        },
        TEST_PRIVATE_PEM,
        algorithm="RS256",
    )
    return token


async def _exchange(minted_scope: str, requested: str | None) -> str:
    minted = _minted(minted_scope)

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/certs"):
            return httpx.Response(200, json={"keys": [TEST_PUBLIC_PEM]})
        return httpx.Response(200, json={"access_token": minted})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await exchange_token(
            make_token(sub="alice"),
            "alice",
            _settings(),
            client,
            requested_scope=requested,
        )
    return minted


def _scope_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith("token_exchange.")]


class TestExchangeScopeLog:
    async def test_a_dropped_requested_scope_is_named_as_missing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="bff.exchange")
        minted = await _exchange(
            "audittrace:query memory:files:read-own",
            "memory:files:read-own memory:files:write",
        )
        records = _scope_records(caplog)
        assert len(records) == 1
        rec = records[0]
        assert rec.levelno == logging.WARNING
        text = rec.getMessage()
        assert text.startswith(
            "token_exchange.scopes_missing missing=memory:files:write "
        )
        assert "granted=audittrace:query,memory:files:read-own" in text
        assert minted not in caplog.text

    async def test_a_full_grant_logs_the_granted_names_only(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="bff.exchange")
        minted = await _exchange(
            "memory:files:read-own memory:files:write",
            "memory:files:read-own memory:files:write",
        )
        records = _scope_records(caplog)
        assert len(records) == 1
        assert records[0].levelno == logging.INFO
        assert records[0].getMessage() == (
            "token_exchange.scopes granted=memory:files:read-own,memory:files:write"
        )
        assert "missing" not in caplog.text
        assert minted not in caplog.text

    async def test_no_requested_scope_reports_the_granted_default(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="bff.exchange")
        await _exchange("audittrace:query", None)
        records = _scope_records(caplog)
        assert [r.getMessage() for r in records] == [
            "token_exchange.scopes granted=audittrace:query"
        ]
