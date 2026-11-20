"""BFF-CHAT-ROUTE-ERRORS ADDENDUM-1, Q8: the chat route's last resort.

Operator ruling Q8 = both: every failure the route foresees is classified where
it is raised (``tests/bff/test_chat_route_errors.py``). This file pins what
happens to the one nobody foresaw: the fixed ``internal_error`` answer (HTTP
500, OpenAI-shaped) instead of the server's ``text/plain`` 500, and one
``chat.refused`` event that names the exception CLASS and the chain of frames
(file:line each) it travelled through, never the exception's own text.

Neuters (each alone): L1 remove ``@last_resort`` -> every cell of
``test_an_unforeseen_exception_gets_the_fixed_answer``; L2 log ``str(exc)`` as
the cause -> ``test_the_exception_text_is_neither_returned_nor_logged``; L3 drop
``request.state.verified_sub`` -> the two ``alice`` cells; L4 catch
``ClientDisconnect`` like any other exception ->
``test_a_caller_that_went_away_is_not_a_refusal``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from audittrace.routes.chat import _openai_error_body
from tests.bff.test_chat_route_errors import (  # noqa: F401  (chat is a fixture)
    INBOUND_TRACE_ID,
    SESSION_ID,
    _caller_token,
    _post,
    _refusal_events,
    _Upstreams,
    chat,
)

INTERNAL_ERROR_BODY = _openai_error_body(
    "Chat is unavailable: the gateway failed unexpectedly. Try again in a moment. "
    "If this message returns, contact an administrator.",
    "internal_error",
    type_="api_error",
    status=500,
)
# seam in bff.app -> the ``sub`` the event must carry
SEAMS: dict[str, str] = {
    "validate_inbound_token": "-",
    "exchange_token": "alice",
    "proxy_chat_completions": "alice",
}


def _raising(exc: BaseException) -> Any:
    async def seam(*_args: Any, **_kwargs: Any) -> Any:
        raise exc

    return seam


@pytest.mark.parametrize("seam", sorted(SEAMS))
def test_an_unforeseen_exception_gets_the_fixed_answer(
    chat: tuple[TestClient, _Upstreams],  # noqa: F811
    seam: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, _upstreams = chat
    monkeypatch.setattr(f"bff.app.{seam}", _raising(RuntimeError("unforeseen")))
    with caplog.at_level(logging.INFO, logger="bff"):
        response = _post(client, "Bearer " + _caller_token())

    assert response.status_code == 500
    assert response.headers["content-type"] == "application/json"
    assert "www-authenticate" not in response.headers
    assert response.json() == INTERNAL_ERROR_BODY
    events = _refusal_events(caplog)
    assert len(events) == 1, [record.getMessage() for record in caplog.records]
    assert events[0].groupdict() == {
        "code": "internal_error",
        "status": "500",
        "sub": SEAMS[seam],
        "session_id": SESSION_ID,
        "inbound_trace_id": INBOUND_TRACE_ID,
        "cause": events[0]["cause"],
    }
    # Gate A9: a frame chain (file:line per frame, outermost first), never the
    # exception's text; the last frame is where the exception was raised.
    cause = events[0]["cause"]
    assert re.fullmatch(r"RuntimeError at \S+:\d+( > \S+:\d+)+", cause), cause
    frames = cause.removeprefix("RuntimeError at ").split(" > ")
    assert frames[-1].startswith(__file__.rsplit("/", 1)[1] + ":")
    assert any(frame.startswith("app.py:") for frame in frames)
    assert "unforeseen" not in cause
    loud = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert [record.levelno for record in loud] == [logging.ERROR]


def test_the_exception_text_is_neither_returned_nor_logged(
    chat: tuple[TestClient, _Upstreams],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nobody has vetted an unforeseen exception's message: here it carries the
    caller's token."""
    client, _upstreams = chat
    token = _caller_token()
    monkeypatch.setattr(
        "bff.app.exchange_token", _raising(RuntimeError(f"failed for {token}"))
    )
    with caplog.at_level(logging.DEBUG, logger="bff"):
        response = _post(client, "Bearer " + token)

    assert response.status_code == 500
    assert token not in response.text
    for record in caplog.records:
        assert token not in record.getMessage()
        assert record.exc_info is None


def test_a_caller_that_went_away_is_not_a_refusal(
    chat: tuple[TestClient, _Upstreams],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, _upstreams = chat
    monkeypatch.setattr("bff.app.proxy_chat_completions", _raising(ClientDisconnect()))
    with caplog.at_level(logging.INFO, logger="bff"):
        _post(client, "Bearer " + _caller_token())

    assert _refusal_events(caplog) == []
