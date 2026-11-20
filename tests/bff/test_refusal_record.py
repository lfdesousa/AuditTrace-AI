"""BFF-CHAT-ROUTE-ERRORS ADDENDUM-2, item 2 (BFF side): a chat turn refused
after the caller was verified leaves a durable record at the memory server.

Lands as ``tests/bff/test_refusal_record.py``. The BFF posts the record to
``/interactions/refusals`` AFTER the answer is sent. It carries the caller's
own token as the evidence of identity and asserts no user: the server derives
the user from the token. A turn refused before the caller was verified (no
token, an invalid token) is not recorded: there is nobody to attribute it to.

Neuters (each alone): A1 never attach the record -> every ``recorded`` cell; A2
record unverified callers too -> the two ``not_recorded`` cells; A3 send a
``sub`` field -> ``test_the_record_asserts_no_user``; A4 let a failing record
change the answer -> ``test_a_failing_record_never_changes_the_answer``; A5 log
the subject token on failure -> the same test; A6 ignore the setting ->
``test_nothing_is_sent_unless_the_deployment_enables_it``; A8 (ADDENDUM-3)
remove the per-user bound -> ``test_one_user_cannot_fill_their_own_audit_trail``;
A9 key the bound on nothing (one bucket for everyone) ->
``test_the_bound_is_per_user``. Recording refusals
of other routes: a control, no single-edit neuter exists.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient

from bff.app import get_http_client
from bff.chat_scopes import CHAT_SCOPE_STRING
from bff.config import Settings, get_settings
from tests.bff.conftest import make_token
from tests.bff.test_app import ORCHESTRATOR_BASE, _settings
from tests.bff.test_chat_route_errors import (  # noqa: F401  (chat is a fixture)
    INBOUND_TRACE_ID,
    SESSION_ID,
    _post,
    _Upstreams,
    chat,
)

RECORD_URL = f"{ORCHESTRATOR_BASE}/interactions/refusals"
PER_MINUTE = 12


@pytest.fixture(autouse=True)
def _fresh_rate_state() -> None:
    """The bound's table is process-wide; every test starts with it empty.
    (At the commit this test is red on, the module does not exist yet.)"""
    with contextlib.suppress(ImportError):
        importlib.import_module("bff.refusal_record").reset_rate_state_for_tests()


def _enabled() -> Settings:
    return _settings().model_copy(update={"refusal_record_enabled": True})


class _Recorder:
    """Stands in for the memory server's refusal route; keeps what it got."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answer: Callable[[], httpx.Response] = lambda: httpx.Response(
            201, json={"recorded": True}
        )


@pytest.fixture
def recording(
    chat: tuple[TestClient, _Upstreams],  # noqa: F811
) -> Iterator[tuple[TestClient, _Upstreams, _Recorder]]:
    """The chat fixture with the record enabled and a recorder at the
    memory server's refusal route."""
    client, upstreams = chat
    recorder = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == RECORD_URL:
            recorder.requests.append(request)
            return recorder.answer()
        return upstreams.handler(request)

    with contextlib.ExitStack() as stack:
        wrapped = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        stack.callback(lambda: asyncio.run(wrapped.aclose()))
        overrides = client.app.dependency_overrides  # type: ignore[attr-defined]
        overrides[get_settings] = _enabled
        overrides[get_http_client] = lambda: wrapped
        yield client, upstreams, recorder


def _kc(status: int, error: str) -> Any:
    return lambda _request: httpx.Response(status, json={"error": error})


def _raises(exc: Exception) -> Any:
    def respond(_request: httpx.Request) -> httpx.Response:
        raise exc

    return respond


def _caller(**claims: Any) -> str:
    return make_token(sub="alice", extra_claims={"scope": CHAT_SCOPE_STRING, **claims})


# id -> (upstream attribute, respond, token, expected status, expected code)
RECORDED: dict[str, tuple[str, Any, Any, int, str]] = {
    "exchange_refused": (
        "exchange",
        _kc(403, "access_denied"),
        _caller,
        502,
        "token_exchange_refused",
    ),
    "subject_refused": (
        "exchange",
        _kc(400, "invalid_token"),
        _caller,
        401,
        "invalid_token",
    ),
    "exchange_unavailable": (
        "exchange",
        _raises(httpx.ConnectError("refused")),
        _caller,
        502,
        "auth_service_unavailable",
    ),
    "exchange_invalid": (
        "exchange",
        lambda _request: httpx.Response(200, json=[]),
        _caller,
        502,
        "token_exchange_invalid",
    ),
    "insufficient_scope": (
        "",
        None,
        lambda: make_token(sub="alice"),
        403,
        "insufficient_scope",
    ),
}


@pytest.mark.parametrize("case", sorted(RECORDED))
def test_a_refused_turn_of_a_verified_caller_is_recorded(
    recording: tuple[TestClient, _Upstreams, _Recorder], case: str
) -> None:
    client, upstreams, recorder = recording
    attribute, respond, token, status, code = RECORDED[case]
    if attribute:
        setattr(upstreams, attribute, respond)
    bearer = token()
    response = _post(client, "Bearer " + bearer)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert len(recorder.requests) == 1
    assert json.loads(recorder.requests[0].content) == {
        "route": "chat",
        "code": code,
        "http_status": status,
        "subject_token": bearer,
        "session_id": SESSION_ID,
        "inbound_trace_id": INBOUND_TRACE_ID,
    }
    assert recorder.requests[0].method == "POST"
    assert "authorization" not in recorder.requests[0].headers


def test_an_unforeseen_failure_after_verification_is_recorded(
    recording: tuple[TestClient, _Upstreams, _Recorder],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _upstreams, recorder = recording

    async def boom(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("unforeseen")

    monkeypatch.setattr("bff.app.exchange_token", boom)
    response = _post(client, "Bearer " + _caller())

    assert response.status_code == 500
    assert [json.loads(r.content)["code"] for r in recorder.requests] == [
        "internal_error"
    ]


@pytest.mark.parametrize(
    ("authorization", "status"),
    [(None, 401), ("Bearer not-a-jwt", 401)],
    ids=["not_recorded_missing", "not_recorded_invalid"],
)
def test_a_caller_nobody_verified_is_not_recorded(
    recording: tuple[TestClient, _Upstreams, _Recorder],
    authorization: str | None,
    status: int,
) -> None:
    client, _upstreams, recorder = recording
    response = _post(client, authorization)

    assert response.status_code == status
    assert recorder.requests == []


def test_a_good_turn_is_not_recorded(
    recording: tuple[TestClient, _Upstreams, _Recorder],
) -> None:
    client, _upstreams, recorder = recording
    assert _post(client, "Bearer " + _caller()).status_code == 200
    assert recorder.requests == []


def test_the_record_asserts_no_user(
    recording: tuple[TestClient, _Upstreams, _Recorder],
) -> None:
    """The server derives the user from the token; the BFF names none."""
    client, upstreams, recorder = recording
    upstreams.exchange = _kc(403, "access_denied")
    _post(client, "Bearer " + _caller())

    assert set(json.loads(recorder.requests[0].content)) == {
        "route",
        "code",
        "http_status",
        "subject_token",
        "session_id",
        "inbound_trace_id",
    }


@pytest.mark.parametrize(
    "outcome",
    ["HTTP 500", "ConnectError"],
)
def test_a_failing_record_never_changes_the_answer(
    recording: tuple[TestClient, _Upstreams, _Recorder],
    outcome: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, upstreams, recorder = recording
    upstreams.exchange = _kc(403, "access_denied")
    bearer = _caller()
    reference = _post(client, "Bearer " + bearer)
    if outcome == "HTTP 500":
        recorder.answer = lambda: httpx.Response(500, content=b"boom")
    else:
        recorder.answer = _raises_now(httpx.ConnectError("refused"))
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="bff"):
        response = _post(client, "Bearer " + bearer)

    assert response.status_code == reference.status_code == 502
    assert response.content == reference.content
    failed = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("chat.refusal_record_failed")
    ]
    assert failed == [
        f"chat.refusal_record_failed code=token_exchange_refused outcome={outcome}"
    ]
    for record in caplog.records:
        assert bearer not in record.getMessage()


def _raises_now(exc: Exception) -> Any:
    def answer() -> httpx.Response:
        raise exc

    return answer


def test_nothing_is_sent_unless_the_deployment_enables_it(
    recording: tuple[TestClient, _Upstreams, _Recorder],
) -> None:
    """Default settings: the record is off (compose and dev have no mesh rule
    that guards the server route)."""
    client, upstreams, recorder = recording
    client.app.dependency_overrides[get_settings] = _settings  # type: ignore[attr-defined]
    upstreams.exchange = _kc(403, "access_denied")

    assert _post(client, "Bearer " + _caller()).status_code == 502
    assert recorder.requests == []
    assert Settings(exchange_client_secret="x").refusal_record_enabled is False


def test_other_routes_are_not_recorded(
    recording: tuple[TestClient, _Upstreams, _Recorder],
) -> None:
    """The record is of a refused chat TURN. A refused console or memory call
    is logged (``route.refused``), not recorded. Control: no neuter (the
    other routes never arm the record)."""
    client, upstreams, recorder = recording
    upstreams.exchange = _kc(403, "access_denied")
    response = client.get(
        "/memory/episodic", headers={"Authorization": "Bearer " + _caller()}
    )

    assert response.status_code == 502
    assert recorder.requests == []


# ── ADDENDUM-3 (gate A5): a rate bound on records ───────────────────────────


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [5000.0]
    monkeypatch.setattr("bff.refusal_record._monotonic", lambda: now[0], raising=False)
    return now


def _refuse_as(client: TestClient, sub: str) -> httpx2.Response:
    token = make_token(sub=sub, extra_claims={"scope": CHAT_SCOPE_STRING})
    return _post(client, "Bearer " + token)


def test_one_user_cannot_fill_their_own_audit_trail(
    recording: tuple[TestClient, _Upstreams, _Recorder],
    clock: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refused turn costs the user nothing. Beyond the bound a turn is still
    answered and logged, but not recorded, until the minute has passed."""
    client, upstreams, recorder = recording
    upstreams.exchange = _kc(403, "access_denied")
    with caplog.at_level(logging.INFO, logger="bff"):
        statuses = {
            _refuse_as(client, "alice").status_code for _ in range(PER_MINUTE + 3)
        }

    assert statuses == {502}
    assert len(recorder.requests) == PER_MINUTE
    skipped = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("chat.refusal_record_skipped")
    ]
    assert (
        skipped
        == ["chat.refusal_record_skipped code=token_exchange_refused reason=rate"] * 3
    )
    clock[0] += 60
    _refuse_as(client, "alice")
    assert len(recorder.requests) == PER_MINUTE + 1


def test_the_bound_is_per_user(
    recording: tuple[TestClient, _Upstreams, _Recorder], clock: list[float]
) -> None:
    client, upstreams, recorder = recording
    upstreams.exchange = _kc(403, "access_denied")
    for _ in range(PER_MINUTE + 1):
        _refuse_as(client, "alice")
    _refuse_as(client, "bob")

    assert len(recorder.requests) == PER_MINUTE + 1
    assert (
        Settings(exchange_client_secret="x").refusal_record_max_per_minute == PER_MINUTE
    )
