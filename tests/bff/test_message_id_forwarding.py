"""CONSOLE-AUDIT-EVIDENCE WU-C (ADDENDUM-A6 section 4 and 5, A7 C3): the BFF
forwards ``X-AuditTrace-Message-Id`` on the chat path and carries it in the
refusal record.

G12'' (grid on the ``build_request`` headers dict): neither header -> 3 keys;
``X-Session-Id`` only -> 4; ``X-AuditTrace-Message-Id`` only -> 4; both -> 5;
blank or whitespace values -> absent; hostile inbound ``X-Source`` /
``Authorization`` / ``X-Evil`` never forwarded.

Neuters (each alone, recorded in the build record): forward ``request.headers``
wholesale -> every G12'' row RED; drop the ``.strip()`` guard -> the blank row
RED; drop the ``message_id=`` argument at the route -> the through-the-route
row RED; drop ``client_message_id`` from the refusal payload -> the G29 BFF
half RED; send the key even when None -> the omitted-key row RED; replace
``fullmatch`` with ``match`` in ``_message_id`` -> the trailing-newline row RED.
"""

# ruff: noqa: F811  (pytest fixtures imported from sibling modules and re-bound as params)

from __future__ import annotations

import json

import httpx
import pytest
from starlette.responses import StreamingResponse

from bff import chat_refusals
from bff.message_id import is_valid_message_id
from bff.proxy import proxy_chat_completions
from tests.bff.test_chat_route_errors import _post, chat  # noqa: F401  (fixture)
from tests.bff.test_proxy import _client, _collect, _settings
from tests.bff.test_refusal_record import (  # noqa: F401  (fixtures)
    RECORD_URL,
    _caller,
    _fresh_rate_state,
    _kc,
    recording,
)

MESSAGE_ID = "b7b3f4e0-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
HEADER = "X-AuditTrace-Message-Id"


async def _upstream_headers(**kwargs: str | None) -> httpx.Headers:
    captured: dict[str, httpx.Headers] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        return httpx.Response(200, json={"ok": True})

    response: StreamingResponse = await proxy_chat_completions(
        b"{}",
        "application/json",
        "minted-token-abc",
        _settings(),
        _client(handler),
        **kwargs,
    )
    await _collect(response)
    return captured["headers"]


def _our_keys(headers: httpx.Headers) -> set[str]:
    """The keys the BFF put there (httpx adds host, accept, user-agent, ...)."""
    return {k for k in headers if k.lower().startswith(("x-", "authorization"))} | {
        k for k in headers if k.lower() == "content-type"
    }


class TestHeaderGrid:
    async def test_neither_header_is_three_keys(self) -> None:
        keys = _our_keys(await _upstream_headers())
        assert len(keys) == 3, keys

    async def test_session_id_only_is_four_keys(self) -> None:
        keys = _our_keys(await _upstream_headers(session_id="s-1"))
        assert len(keys) == 4, keys

    async def test_message_id_only_is_four_keys(self) -> None:
        headers = await _upstream_headers(message_id=MESSAGE_ID)
        assert len(_our_keys(headers)) == 4
        assert headers[HEADER] == MESSAGE_ID
        assert "x-session-id" not in headers

    async def test_both_is_five_keys(self) -> None:
        headers = await _upstream_headers(session_id="s-1", message_id=MESSAGE_ID)
        assert {k.lower() for k in _our_keys(headers)} == {
            "authorization",
            "content-type",
            "x-source",
            "x-session-id",
            "x-audittrace-message-id",
        }

    @pytest.mark.parametrize("blank", ["", " ", "\t  "], ids=["empty", "sp", "tab"])
    async def test_a_blank_value_is_absent(self, blank: str) -> None:
        headers = await _upstream_headers(session_id=blank, message_id=blank)
        assert len(_our_keys(headers)) == 3
        assert HEADER.lower() not in {k.lower() for k in headers}

    async def test_the_value_is_forwarded_trimmed(self) -> None:
        headers = await _upstream_headers(message_id=f"  {MESSAGE_ID}  ")
        assert headers[HEADER] == MESSAGE_ID


class TestHostileInboundNeverForwarded:
    """The route reads exactly two inbound headers by name."""

    def test_only_the_two_correlation_headers_ride_upstream(
        self,
        recording: tuple,  # noqa: F811
    ) -> None:
        client, upstreams, _recorder = recording
        seen: list[httpx.Headers] = []

        def spy(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers)
            return httpx.Response(200, content=b'{"id":"chatcmpl-1","choices":[]}')

        upstreams.memory_server = spy
        inbound = _caller()
        response = _post(
            client,
            "Bearer " + inbound,
            **{
                HEADER: MESSAGE_ID,
                "X-Source": "evil",
                "X-Evil": "1",
                "X-Forwarded-User": "mallory",
            },
        )
        assert response.status_code == 200
        assert len(seen) == 1
        lowered = {k.lower(): v for k, v in seen[0].items()}
        assert lowered[HEADER.lower()] == MESSAGE_ID
        assert lowered["x-source"] == "librechat"
        assert lowered["authorization"].startswith("Bearer ")
        assert lowered["authorization"] != "Bearer " + inbound  # minted, not inbound
        assert "x-evil" not in lowered
        assert "x-forwarded-user" not in lowered


class TestMessageIdPredicate:
    """``_message_id`` applies the chain's id predicate (A1 section 3.2) with
    ``re.fullmatch``; None when absent or rejected."""

    @pytest.mark.parametrize(
        "value",
        [
            MESSAGE_ID,
            "x",
            "...",
            "a..b",
            "msg:1_2.3-4",
            "x" * 255,
        ],
    )
    def test_accepted(self, value: str) -> None:
        assert chat_refusals._message_id({"x-audittrace-message-id": value}) == value

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            ".",
            "..",
            "../x",
            "a/b",
            "x" * 256,
            "a\nb",
            "a b",
            "{{LIBRECHAT_BODY_MESSAGEID}}",
        ],
    )
    def test_rejected(self, value: str) -> None:
        assert chat_refusals._message_id({"x-audittrace-message-id": value}) is None

    @pytest.mark.parametrize("value", ["abc\n", "a\nb", "\nabc", ".\n", "..\n"])
    def test_the_predicate_is_a_fullmatch_not_a_match(self, value: str) -> None:
        """A-1: ``re.match`` would accept a trailing newline; ``fullmatch`` does
        not. Neuter: ``fullmatch`` -> ``match`` in ``bff.message_id`` -> RED."""
        assert is_valid_message_id(value) is False

    @pytest.mark.parametrize("value", [MESSAGE_ID, "x", "...", "a..b", "x" * 255])
    def test_the_predicate_accepts_the_grid(self, value: str) -> None:
        assert is_valid_message_id(value) is True

    def test_a_trailing_newline_header_is_trimmed_like_session_id(self) -> None:
        assert chat_refusals._message_id({"x-audittrace-message-id": "abc\n"}) == "abc"

    def test_absent_is_none(self) -> None:
        assert chat_refusals._message_id({}) is None

    def test_surrounding_whitespace_is_trimmed_first(self) -> None:
        assert (
            chat_refusals._message_id({"x-audittrace-message-id": f" {MESSAGE_ID} "})
            == MESSAGE_ID
        )


class TestRefusalRecordCarriesTheMessageId:
    """G29, BFF half: the refused-turn record names the message."""

    def test_the_payload_carries_the_id(self, recording: tuple) -> None:  # noqa: F811
        client, upstreams, recorder = recording
        upstreams.exchange = _kc(403, "access_denied")
        response = _post(client, "Bearer " + _caller(), **{HEADER: MESSAGE_ID})
        assert response.status_code == 502
        body = json.loads(recorder.requests[0].content)
        assert body["client_message_id"] == MESSAGE_ID
        assert body["code"] == "token_exchange_refused"

    @pytest.mark.parametrize(
        "extra",
        [{}, {HEADER: "../x"}, {HEADER: "a b"}],
        ids=["absent", "rejected-dots", "rejected-space"],
    )
    def test_the_key_is_omitted_when_none(
        self,
        recording: tuple,
        extra: dict[str, str],  # noqa: F811
    ) -> None:
        client, upstreams, recorder = recording
        upstreams.exchange = _kc(403, "access_denied")
        _post(client, "Bearer " + _caller(), **extra)
        body = json.loads(recorder.requests[0].content)
        assert "client_message_id" not in body

    def test_the_value_is_never_logged(
        self,
        recording: tuple,
        caplog: pytest.LogCaptureFixture,  # noqa: F811
    ) -> None:
        client, upstreams, _recorder = recording
        upstreams.exchange = _kc(403, "access_denied")
        with caplog.at_level("DEBUG", logger="bff"):
            _post(client, "Bearer " + _caller(), **{HEADER: MESSAGE_ID})
        assert all(MESSAGE_ID not in r.getMessage() for r in caplog.records)
