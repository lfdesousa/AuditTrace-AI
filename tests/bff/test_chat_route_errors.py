"""BFF-CHAT-ROUTE-ERRORS: every answer the BFF itself gives on
``POST /v1/chat/completions`` is explicit, OpenAI-shaped and RFC 6750-correct.

Two backlog items, one contract (spec 2026-11-15-SPEC-bff-chat-route-errors):

* BFF-EXCHANGE-FAILURE-EXPLICIT-ERROR: a failed RFC 8693 token exchange, an
  unreachable sign-in service and an unreachable memory server each answer
  with an OpenAI error object that names the cause class. A body without an
  ``error`` object is printed by OpenAI clients as "502 status code (no body)".
* BFF-CHAT-401-WWW-AUTHENTICATE: a 401 carries the RFC 6750 section 3
  ``WWW-Authenticate: Bearer`` challenge, and the route has ONE error shape.

Every case goes through the real route (``create_app()`` + ``TestClient``);
``httpx.MockTransport`` plays Keycloak (JWKS, token endpoint) and the memory
server. Statuses, headers and bodies are asserted by EQUALITY, so no text from
the token, from Keycloak or from a request header can enter an answer.

Neuters (each alone must redden the named cells; recorded in the build record):
N1 drop the ``WWW-Authenticate`` header of the 401 -> every ``missing`` and
``invalid`` cell of ``test_refusal_status_header_and_body``; N2 answer a JWKS
failure with the ``invalid_token`` refusal -> the two ``jwks_`` cells; N3 map a
Keycloak 4xx to the ``unavailable`` class -> the three ``kc_4xx`` cells; N4
relay Keycloak's ``error_description`` in the message -> the same three cells;
N5 let the minted-token re-validation raise on a JWKS failure -> the
``jwks_second_fetch`` cell (HTTP 500); N6 log the raw ``X-Session-Id`` ->
``test_request_headers_cannot_forge_log_fields``; N7 drop the log event ->
every cell of ``test_refusal_logs_exactly_one_event``; N8 add the challenge to a
relayed memory-server 401 -> ``test_memory_server_answers_are_relayed_untouched``.

ADDENDUM-1 (gate r1). B1: an upstream answer of an unexpected SHAPE (a JWKS or
token-endpoint body that is not a JSON object, a realm key python-jose cannot
build, a claim of a type it cannot compare) is classified where it is raised
and answered like its class, never as an unhandled exception; the same shapes
on ``/memory`` answer in that route's own shape. S3: the event is one physical
line and its level follows the status class. A5: it is the only record at
WARNING or above of a refused turn. Further neuters: N12 log ``cause``
unflattened -> ``test_the_cause_is_one_physical_line``; N13 log every refusal at
WARNING -> the 502 cells of ``test_refusal_logs_exactly_one_event``; N14 restore
the second WARNING of ``bff/auth.py`` -> the ``invalid`` cells of the same test;
N15-N23 remove one shape guard each -> the cells of that shape (build record).
Gate r2 S6: G5 drop ``ValueError`` from the minted-token decode guard -> the
``jwks_second_key_malformed`` cell; G6 class a non-JSON 400 as a subject-token
refusal -> ``kc_4xx_400_not_json``; G7 accept status 401 as the subject-token
refusal -> ``kc_4xx_401_error_invalid_token``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import re
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import httpx2
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from fastapi.testclient import TestClient
from jose import jwt

from audittrace.routes.chat import _openai_error_body
from bff.app import create_app, get_http_client
from bff.chat_scopes import CHAT_SCOPE_STRING, CHAT_SCOPES
from bff.config import Settings, get_settings
from tests.bff.conftest import (
    TEST_ISSUER,
    TEST_JWK,
    TEST_KID,
    TEST_PRIVATE_PEM,
    make_token,
)
from tests.bff.test_app import (
    KEYCLOAK_JWKS_URL,
    KEYCLOAK_TOKEN_URL,
    ORCHESTRATOR_BASE,
    _settings,
    _sub_from_form_body,
)

CHAT_BODY = b'{"model":"m","messages":[{"role":"user","content":"hi"}]}'
CLIENT_SECRET = "s3cr3t"  # the fake of tests/bff/test_app.py::_settings
SESSION_ID = "librechat-conversation-7"
INBOUND_TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
TRACEPARENT = f"00-{INBOUND_TRACE_ID}-b7ad6b7169203331-01"
KEYCLOAK_SAYS = "Client not allowed to exchange"

# ── The contract: six refusals the BFF itself produces (plus LC10A's 403) ────
# (HTTP status, error.type, error.code, error.message, WWW-Authenticate | None)
Refusal = tuple[int, str, str, str, str | None]

MISSING_TOKEN: Refusal = (
    401,
    "invalid_request_error",
    "missing_token",
    "Sign-in required: this request carries no bearer access token. Sign in and "
    "try again.",
    "Bearer",
)
INVALID_TOKEN: Refusal = (
    401,
    "invalid_request_error",
    "invalid_token",
    "Your sign-in is no longer valid: the access token is invalid or expired. "
    "Sign out and sign in again.",
    'Bearer error="invalid_token", '
    'error_description="The access token is invalid or expired"',
)
AUTH_SERVICE_UNAVAILABLE: Refusal = (
    502,
    "api_error",
    "auth_service_unavailable",
    "Chat is temporarily unavailable: the sign-in service cannot be reached or "
    "its answer cannot be used. Try again in a moment. If this message returns, "
    "contact an administrator.",
    None,
)
TOKEN_EXCHANGE_REFUSED: Refusal = (
    502,
    "api_error",
    "token_exchange_refused",
    "Chat is unavailable: the sign-in service refused to issue the access token "
    "for this chat turn. Contact an administrator: the cause is the server "
    "configuration, not your message.",
    None,
)
TOKEN_EXCHANGE_INVALID: Refusal = (
    502,
    "api_error",
    "token_exchange_invalid",
    "Chat is unavailable: the sign-in service issued an access token that the "
    "gateway refused. Contact an administrator: the cause is the server "
    "configuration, not your message.",
    None,
)
UPSTREAM_UNAVAILABLE: Refusal = (
    502,
    "api_error",
    "upstream_unavailable",
    "Chat is temporarily unavailable: the AuditTrace memory server cannot be "
    "reached. Try again in a moment. If this message returns, contact an "
    "administrator.",
    None,
)

_other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_PEM = _other_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()

Respond = Callable[[httpx.Request], httpx.Response]


class _Upstreams:
    """Keycloak and the memory server behind one ``MockTransport``."""

    def __init__(self) -> None:
        self.jwks: Respond | None = None
        self.exchange: Respond | None = None
        self.memory_server: Respond | None = None
        self.jwks_calls = 0
        self.exchange_calls = 0
        self.memory_server_calls = 0
        self.minted: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == KEYCLOAK_JWKS_URL:
            self.jwks_calls += 1
            if self.jwks is not None:
                return self.jwks(request)
            return httpx.Response(200, json={"keys": [TEST_JWK]})
        if url == KEYCLOAK_TOKEN_URL:
            self.exchange_calls += 1
            if self.exchange is not None:
                return self.exchange(request)
            return self.mint(request)
        if url == f"{ORCHESTRATOR_BASE}/v1/chat/completions":
            self.memory_server_calls += 1
            if self.memory_server is not None:
                return self.memory_server(request)
            return httpx.Response(200, content=b'{"id":"chatcmpl-1","choices":[]}')
        if url.startswith(f"{ORCHESTRATOR_BASE}/memory"):
            self.memory_server_calls += 1
            return httpx.Response(200, content=b'{"items":[]}')
        raise AssertionError(f"unexpected upstream call: {url}")

    def mint(self, request: httpx.Request, **claims: Any) -> httpx.Response:
        now = int(time.time())
        pem = claims.pop("pem", TEST_PRIVATE_PEM)
        payload: dict[str, Any] = {
            "iss": TEST_ISSUER,
            "sub": _sub_from_form_body(request.read().decode()),
            "aud": "audittrace-server",
            "scope": CHAT_SCOPE_STRING,
            "iat": now,
            "exp": now + 300,
        }
        payload.update(claims)
        token = jwt.encode(payload, pem, algorithm="RS256")
        self.minted.append(token)
        return httpx.Response(200, json={"access_token": token})


def _raises(exc: Exception) -> Respond:
    def respond(_request: httpx.Request) -> httpx.Response:
        raise exc

    return respond


def _answers(status: int, **kwargs: Any) -> Respond:
    return lambda _request: httpx.Response(status, **kwargs)


def _caller_token(**kwargs: Any) -> str:
    return make_token(sub="alice", extra_claims={"scope": CHAT_SCOPE_STRING}, **kwargs)


def _unsigned_claims_token(algorithm: str, key: str, **claims: Any) -> str:
    now = int(time.time())
    payload = {"iss": TEST_ISSUER, "iat": now, "exp": now + 300}
    payload.update({"scope": CHAT_SCOPE_STRING, **claims})
    return str(jwt.encode(payload, key, algorithm=algorithm))


Arrange = Callable[[_Upstreams], None]


def _jwks(respond: Respond) -> Arrange:
    return lambda upstreams: setattr(upstreams, "jwks", respond)


def _exchange(respond: Respond) -> Arrange:
    return lambda upstreams: setattr(upstreams, "exchange", respond)


def _memory_server(respond: Respond) -> Arrange:
    return lambda upstreams: setattr(upstreams, "memory_server", respond)


def _minted_with(**claims: Any) -> Arrange:
    def arrange(upstreams: _Upstreams) -> None:
        upstreams.exchange = lambda request: upstreams.mint(request, **claims)

    return arrange


def _jwks_fails_from_second_fetch(upstreams: _Upstreams) -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        if upstreams.jwks_calls >= 2:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={"keys": [TEST_JWK]})

    upstreams.jwks = respond


def _kc_error(error: str, description: str) -> dict[str, Any]:
    return {"json": {"error": error, "error_description": description}}


# id -> (Authorization header | None, upstream arrangement | None, expected
# refusal, expected ``sub`` in the log event, expected token-endpoint calls,
# expected memory-server calls)
Case = tuple[Callable[[], str] | None, Arrange | None, Refusal, str, int, int]


def _bearer(token: Callable[[], str]) -> Callable[[], str]:
    return lambda: "Bearer " + token()


CASES: dict[str, Case] = {
    "missing_no_header": (None, None, MISSING_TOKEN, "-", 0, 0),
    "missing_basic_scheme": (
        lambda: "Basic dXNlcjpwYXNz",
        None,
        MISSING_TOKEN,
        "-",
        0,
        0,
    ),
    "missing_empty_bearer": (lambda: "Bearer ", None, MISSING_TOKEN, "-", 0, 0),
    "invalid_not_a_jwt": (lambda: "Bearer not-a-jwt", None, INVALID_TOKEN, "-", 0, 0),
    "invalid_foreign_key": (
        _bearer(lambda: _caller_token(private_pem=OTHER_PEM)),
        None,
        INVALID_TOKEN,
        "-",
        0,
        0,
    ),
    "invalid_hs256": (
        _bearer(lambda: _unsigned_claims_token("HS256", "shared", sub="alice")),
        None,
        INVALID_TOKEN,
        "-",
        0,
        0,
    ),
    "invalid_expired": (
        _bearer(lambda: _caller_token(exp_offset=-60)),
        None,
        INVALID_TOKEN,
        "-",
        0,
        0,
    ),
    "invalid_foreign_issuer": (
        _bearer(lambda: _caller_token(issuer="http://elsewhere/realms/x")),
        None,
        INVALID_TOKEN,
        "-",
        0,
        0,
    ),
    "invalid_no_sub": (
        _bearer(lambda: _unsigned_claims_token("RS256", TEST_PRIVATE_PEM)),
        None,
        INVALID_TOKEN,
        "-",
        0,
        0,
    ),
    "jwks_unreachable": (
        _bearer(_caller_token),
        _jwks(_raises(httpx.ConnectError("connection refused"))),
        AUTH_SERVICE_UNAVAILABLE,
        "-",
        0,
        0,
    ),
    "jwks_http_500": (
        _bearer(_caller_token),
        _jwks(_answers(500)),
        AUTH_SERVICE_UNAVAILABLE,
        "-",
        0,
        0,
    ),
    "jwks_second_fetch": (
        _bearer(_caller_token),
        _jwks_fails_from_second_fetch,
        AUTH_SERVICE_UNAVAILABLE,
        "alice",
        1,
        0,
    ),
    "kc_4xx_403_not_allowed": (
        _bearer(_caller_token),
        _exchange(_answers(403, **_kc_error("access_denied", KEYCLOAK_SAYS))),
        TOKEN_EXCHANGE_REFUSED,
        "alice",
        1,
        0,
    ),
    "kc_4xx_400_invalid_token": (
        _bearer(_caller_token),
        _exchange(_answers(400, **_kc_error("invalid_request", "Invalid token"))),
        TOKEN_EXCHANGE_REFUSED,
        "alice",
        1,
        0,
    ),
    "kc_4xx_401_client": (
        _bearer(_caller_token),
        _exchange(_answers(401, **_kc_error("unauthorized_client", "Invalid client"))),
        TOKEN_EXCHANGE_REFUSED,
        "alice",
        1,
        0,
    ),
    "kc_503": (
        _bearer(_caller_token),
        _exchange(_answers(503)),
        AUTH_SERVICE_UNAVAILABLE,
        "alice",
        1,
        0,
    ),
    "kc_unreachable": (
        _bearer(_caller_token),
        _exchange(_raises(httpx.ConnectError("connection refused"))),
        AUTH_SERVICE_UNAVAILABLE,
        "alice",
        1,
        0,
    ),
    "kc_timeout": (
        _bearer(_caller_token),
        _exchange(_raises(httpx.ReadTimeout("timed out"))),
        AUTH_SERVICE_UNAVAILABLE,
        "alice",
        1,
        0,
    ),
    "kc_200_not_json": (
        _bearer(_caller_token),
        _exchange(_answers(200, content=b"<html>login</html>")),
        TOKEN_EXCHANGE_INVALID,
        "alice",
        1,
        0,
    ),
    "kc_200_no_access_token": (
        _bearer(_caller_token),
        _exchange(_answers(200, json={})),
        TOKEN_EXCHANGE_INVALID,
        "alice",
        1,
        0,
    ),
    "kc_200_other_subject": (
        _bearer(_caller_token),
        _minted_with(sub="mallory"),
        TOKEN_EXCHANGE_INVALID,
        "alice",
        1,
        0,
    ),
    "kc_200_over_ceiling": (
        _bearer(_caller_token),
        _minted_with(scope=CHAT_SCOPE_STRING + " memory:episodic:write"),
        TOKEN_EXCHANGE_INVALID,
        "alice",
        1,
        0,
    ),
    "kc_200_foreign_signature": (
        _bearer(_caller_token),
        _minted_with(pem=OTHER_PEM),
        TOKEN_EXCHANGE_INVALID,
        "alice",
        1,
        0,
    ),
    "memory_server_unreachable": (
        _bearer(_caller_token),
        _memory_server(_raises(httpx.ConnectError("connection refused"))),
        UPSTREAM_UNAVAILABLE,
        "alice",
        1,
        1,
    ),
    "memory_server_timeout": (
        _bearer(_caller_token),
        _memory_server(_raises(httpx.ReadTimeout("timed out"))),
        UPSTREAM_UNAVAILABLE,
        "alice",
        1,
        1,
    ),
}


# ── ADDENDUM-1 (gate r1 B1): upstream answers of an unexpected shape ─────────

OKP_JWK: dict[str, str] = {
    "kty": "OKP",
    "crv": "Ed25519",
    "use": "sig",
    "alg": "EdDSA",
    "kid": "okp-1",
    "x": base64.urlsafe_b64encode(
        ed25519.Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    .rstrip(b"=")
    .decode(),
}
# A realm that lists a key python-jose cannot build for RS256 BEFORE its RSA key.
JWKS_UNUSABLE_KEY_FIRST: dict[str, Any] = {"keys": [OKP_JWK]}
JWKS_MALFORMED_RSA_KEY: dict[str, Any] = {"keys": [{"kty": "RSA", "kid": TEST_KID}]}


def _jwks_from_second_fetch(respond: Respond) -> Arrange:
    def arrange(upstreams: _Upstreams) -> None:
        def answer(request: httpx.Request) -> httpx.Response:
            if upstreams.jwks_calls >= 2:
                return respond(request)
            return httpx.Response(200, json={"keys": [TEST_JWK]})

        upstreams.jwks = answer

    return arrange


def _realm_signed(**claims: Any) -> Callable[[], str]:
    """A token only the realm could have issued, with the given claims."""
    return _bearer(
        lambda: _unsigned_claims_token(
            "RS256", TEST_PRIVATE_PEM, **{"sub": "alice", **claims}
        )
    )


def _kc_400(body: Any) -> Arrange:
    return _exchange(_answers(400, json=body))


CASES.update(
    {
        "jwks_not_an_object": (
            _bearer(_caller_token),
            _jwks(_answers(200, json=[])),
            AUTH_SERVICE_UNAVAILABLE,
            "-",
            0,
            0,
        ),
        "jwks_key_unusable_first": (
            _bearer(_caller_token),
            _jwks(_answers(200, json=JWKS_UNUSABLE_KEY_FIRST)),
            AUTH_SERVICE_UNAVAILABLE,
            "-",
            0,
            0,
        ),
        "jwks_key_malformed": (
            _bearer(_caller_token),
            _jwks(_answers(200, json=JWKS_MALFORMED_RSA_KEY)),
            AUTH_SERVICE_UNAVAILABLE,
            "-",
            0,
            0,
        ),
        "jwks_second_not_an_object": (
            _bearer(_caller_token),
            _jwks_from_second_fetch(_answers(200, json=[])),
            AUTH_SERVICE_UNAVAILABLE,
            "alice",
            1,
            0,
        ),
        "jwks_second_key_unusable": (
            _bearer(_caller_token),
            _jwks_from_second_fetch(_answers(200, json=JWKS_UNUSABLE_KEY_FIRST)),
            AUTH_SERVICE_UNAVAILABLE,
            "alice",
            1,
            0,
        ),
        "kc_200_json_array": (
            _bearer(_caller_token),
            _exchange(_answers(200, json=[])),
            TOKEN_EXCHANGE_INVALID,
            "alice",
            1,
            0,
        ),
        "kc_200_json_string": (
            _bearer(_caller_token),
            _exchange(_answers(200, json="token")),
            TOKEN_EXCHANGE_INVALID,
            "alice",
            1,
            0,
        ),
        "kc_200_minted_exp_null": (
            _bearer(_caller_token),
            _minted_with(exp=None),
            AUTH_SERVICE_UNAVAILABLE,
            "alice",
            1,
            0,
        ),
        "invalid_issuer_not_a_string": (
            _realm_signed(iss=[TEST_ISSUER]),
            None,
            INVALID_TOKEN,
            "-",
            0,
            0,
        ),
        "realm_signed_exp_null": (
            _realm_signed(exp=None),
            None,
            AUTH_SERVICE_UNAVAILABLE,
            "-",
            0,
            0,
        ),
        "kc_4xx_400_other_error": (
            _bearer(_caller_token),
            _kc_400(_kc_error("invalid_client", "Audience not found")["json"]),
            TOKEN_EXCHANGE_REFUSED,
            "alice",
            1,
            0,
        ),
        "kc_4xx_400_error_not_a_string": (
            _bearer(_caller_token),
            _kc_400({"error": ["invalid_token"]}),
            TOKEN_EXCHANGE_REFUSED,
            "alice",
            1,
            0,
        ),
        "kc_4xx_400_not_an_object": (
            _bearer(_caller_token),
            _kc_400(["invalid_token"]),
            TOKEN_EXCHANGE_REFUSED,
            "alice",
            1,
            0,
        ),
        "kc_4xx_403_error_invalid_token": (
            _bearer(_caller_token),
            _exchange(_answers(403, **_kc_error("invalid_token", "Invalid token"))),
            TOKEN_EXCHANGE_REFUSED,
            "alice",
            1,
            0,
        ),
    }
)
# Gate r2 S6: three guards of ADDENDUM-1's own contract, each with a cell.
# G5 the minted-token decode catches ``ValueError`` (an RSA key without its
# numbers); G6 a 400 whose body is not JSON is the configuration class; G7 the
# subject-token class needs the exact status 400, not any 4xx and not 401.
CASES.update(
    {
        "jwks_second_key_malformed": (
            _bearer(_caller_token),
            _jwks_from_second_fetch(_answers(200, json=JWKS_MALFORMED_RSA_KEY)),
            AUTH_SERVICE_UNAVAILABLE,
            "alice",
            1,
            0,
        ),
        "kc_4xx_400_not_json": (
            _bearer(_caller_token),
            _exchange(_answers(400, content=b"<html>bad request</html>")),
            TOKEN_EXCHANGE_REFUSED,
            "alice",
            1,
            0,
        ),
        "kc_4xx_401_error_invalid_token": (
            _bearer(_caller_token),
            _exchange(_answers(401, **_kc_error("invalid_token", "Invalid token"))),
            TOKEN_EXCHANGE_REFUSED,
            "alice",
            1,
            0,
        ),
    }
)
# Q1 = (d): Keycloak's ``400`` + ``"error": "invalid_token"`` is its refusal of the
# SUBJECT token (measured on Keycloak 24: expired, user disabled, session gone).
# RFC 6750 section 3.1: the caller's token is invalid, so 401, not 502.
CASES["kc_4xx_400_invalid_token"] = (
    _bearer(_caller_token),
    _exchange(_answers(400, **_kc_error("invalid_token", "Invalid token"))),
    INVALID_TOKEN,
    "alice",
    1,
    0,
)


def _test_settings() -> Settings:
    # TTL 0: every ``get_jwks_keys`` call fetches, so the second fetch (the
    # minted-token re-validation) is reachable in ``jwks_second_fetch``.
    return _settings().model_copy(update={"jwks_cache_ttl_seconds": 0})


@pytest.fixture
def chat() -> Iterator[tuple[TestClient, _Upstreams]]:
    upstreams = _Upstreams()
    with contextlib.ExitStack() as stack:
        mock_client = httpx.AsyncClient(
            transport=httpx.MockTransport(upstreams.handler)
        )
        stack.callback(lambda: asyncio.run(mock_client.aclose()))
        app = create_app()
        app.dependency_overrides[get_settings] = _test_settings
        app.dependency_overrides[get_http_client] = lambda: mock_client
        # An exception escaping the handler is answered as a real server
        # answers it (HTTP 500), so the test compares statuses.
        client = stack.enter_context(TestClient(app, raise_server_exceptions=False))
        yield client, upstreams


def _post(
    client: TestClient, authorization: str | None, **extra: str
) -> httpx2.Response:
    headers = {
        "Content-Type": "application/json",
        "traceparent": TRACEPARENT,
        "X-Session-Id": SESSION_ID,
    }
    headers.update(extra)
    if authorization is not None:
        headers["Authorization"] = authorization
    return client.post("/v1/chat/completions", content=CHAT_BODY, headers=headers)


def _run(
    chat: tuple[TestClient, _Upstreams], case_id: str, **extra: str
) -> tuple[httpx2.Response, _Upstreams, str | None]:
    client, upstreams = chat
    authorization, arrange, _refusal, _sub, _kc, _ms = CASES[case_id]
    if arrange is not None:
        arrange(upstreams)
    header = authorization() if authorization is not None else None
    return _post(client, header, **extra), upstreams, header


def _expected_body(refusal: Refusal) -> dict[str, Any]:
    status, error_type, code, message, _challenge = refusal
    body: dict[str, Any] = _openai_error_body(
        message, code, type_=error_type, status=status
    )
    return body


_EVENT = re.compile(
    r"^chat\.refused code=(?P<code>\S+) status=(?P<status>\d{3}) sub=(?P<sub>\S+) "
    r"session_id=(?P<session_id>\S+) inbound_trace_id=(?P<inbound_trace_id>\S+) "
    r"cause=(?P<cause>.*)$",
    re.DOTALL,
)


def _refusal_events(caplog: pytest.LogCaptureFixture) -> list[re.Match[str]]:
    matches = [_EVENT.match(record.getMessage()) for record in caplog.records]
    return [match for match in matches if match is not None]


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_refusal_status_header_and_body(
    chat: tuple[TestClient, _Upstreams], case_id: str
) -> None:
    """The exact status, the exact challenge (or none) and the exact body."""
    refusal = CASES[case_id][2]
    response, upstreams, _header = _run(chat, case_id)

    assert response.status_code == refusal[0]
    assert response.headers.get("www-authenticate") == refusal[4]
    assert response.headers["content-type"] == "application/json"
    assert response.json() == _expected_body(refusal)
    assert upstreams.exchange_calls == CASES[case_id][4]
    assert upstreams.memory_server_calls == CASES[case_id][5]


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_refusal_logs_exactly_one_event(
    chat: tuple[TestClient, _Upstreams],
    case_id: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One ``chat.refused`` event per refused turn: cause class, user,
    session and the caller's trace id (EU AI Act Art 12 traceability)."""
    refusal, expected_sub = CASES[case_id][2], CASES[case_id][3]
    with caplog.at_level(logging.INFO, logger="bff"):
        _run(chat, case_id)

    events = _refusal_events(caplog)
    assert len(events) == 1, [record.getMessage() for record in caplog.records]
    assert events[0].groupdict() | {"cause": ""} == {
        "code": refusal[2],
        "status": str(refusal[0]),
        "sub": expected_sub,
        "session_id": SESSION_ID,
        "inbound_trace_id": INBOUND_TRACE_ID,
        "cause": "",
    }
    assert events[0]["cause"].strip() != ""
    # S3: one physical line, and the level follows the status class.
    assert len(events[0].string.splitlines()) == 1
    # A5: the event is the only record at WARNING or above of a refused turn.
    loud = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert [record.getMessage() for record in loud] == [events[0].string]
    assert loud[0].levelno == (logging.WARNING if refusal[0] < 500 else logging.ERROR)


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_no_credential_reaches_the_answer_or_the_log(
    chat: tuple[TestClient, _Upstreams],
    case_id: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Neither the caller's token, the minted token nor the client secret is
    in the response or in any log record."""
    with caplog.at_level(logging.DEBUG, logger="bff"):
        response, upstreams, header = _run(chat, case_id)

    inbound = (header or "").removeprefix("Bearer ").strip()
    credentials = [CLIENT_SECRET, *upstreams.minted]
    if len(inbound) >= 8:  # "" and the 0-length bearer cannot be searched for
        credentials.append(inbound)
    surface = response.text + repr(sorted(response.headers.items()))
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for credential in credentials:
        assert credential not in surface
        assert credential not in logged


def test_request_headers_cannot_forge_log_fields(
    chat: tuple[TestClient, _Upstreams], caplog: pytest.LogCaptureFixture
) -> None:
    """A session id or traceparent that is not in the accepted form is logged
    as ``-``: a caller cannot write fields into the event."""
    with caplog.at_level(logging.INFO, logger="bff"):
        _run(
            chat,
            "kc_4xx_403_not_allowed",
            **{
                "X-Session-Id": "x code=ok sub=root",
                "traceparent": "00-not-hex sub=root-01",
            },
        )

    events = _refusal_events(caplog)
    assert len(events) == 1, [record.getMessage() for record in caplog.records]
    fields = events[0].groupdict()
    assert (fields["code"], fields["sub"]) == ("token_exchange_refused", "alice")
    assert (fields["session_id"], fields["inbound_trace_id"]) == ("-", "-")


def test_the_keycloak_refusal_is_logged_not_returned(
    chat: tuple[TestClient, _Upstreams], caplog: pytest.LogCaptureFixture
) -> None:
    """The operator finds Keycloak's own words in the log; the caller does not
    get them (they describe the realm's configuration)."""
    with caplog.at_level(logging.INFO, logger="bff"):
        response, _upstreams, _header = _run(chat, "kc_4xx_403_not_allowed")

    assert KEYCLOAK_SAYS not in response.text
    assert "access_denied" not in response.text
    events = _refusal_events(caplog)
    assert len(events) == 1, [record.getMessage() for record in caplog.records]
    assert "HTTP 403" in events[0]["cause"]
    assert KEYCLOAK_SAYS in events[0]["cause"]


# ── Controls: green before and after; they fence the change ─────────────────


def test_control_a_good_turn_is_relayed_and_logs_no_refusal(
    chat: tuple[TestClient, _Upstreams], caplog: pytest.LogCaptureFixture
) -> None:
    client, upstreams = chat
    with caplog.at_level(logging.INFO, logger="bff"):
        response = _post(client, "Bearer " + _caller_token())

    assert response.status_code == 200
    assert response.content == b'{"id":"chatcmpl-1","choices":[]}'
    assert (upstreams.exchange_calls, upstreams.memory_server_calls) == (1, 1)
    assert _refusal_events(caplog) == []


def test_control_insufficient_scope_keeps_its_403(
    chat: tuple[TestClient, _Upstreams],
) -> None:
    """LC10A's refusal is unchanged: RFC 6750 section 3.1, exact header."""
    client, upstreams = chat
    response = _post(client, "Bearer " + make_token(sub="alice"))

    assert response.status_code == 403
    assert (
        response.headers["www-authenticate"]
        == (
            'Bearer error="insufficient_scope", scope="' + " ".join(sorted(CHAT_SCOPES))
        )
        + '"'
    )
    error = response.json()["error"]
    assert (error["code"], error["status"]) == ("insufficient_scope", 403)
    assert error["missing_scopes"] == sorted(CHAT_SCOPES)
    assert (upstreams.exchange_calls, upstreams.memory_server_calls) == (0, 0)


RELAYED: dict[str, tuple[int, bytes, str]] = {
    "401_detail": (401, b'{"detail":"Invalid or expired token"}', "application/json"),
    "403_detail": (403, b'{"detail":"Insufficient scope"}', "application/json"),
    "500_openai": (
        500,
        b'{"error":{"message":"boom","type":"api_error","param":null,"code":"x"}}',
        "application/json",
    ),
    "503_mesh_text": (503, b"no healthy upstream", "text/plain"),
}


@pytest.mark.parametrize("relayed_id", sorted(RELAYED))
def test_memory_server_answers_are_relayed_untouched(
    chat: tuple[TestClient, _Upstreams],
    relayed_id: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """What the memory server (or the mesh) answers is not the BFF's to
    rewrite: status, bytes, content type and trace header pass through, no
    challenge is added, and it is not logged as a BFF refusal."""
    client, upstreams = chat
    status, body, content_type = RELAYED[relayed_id]
    upstreams.memory_server = _answers(
        status,
        content=body,
        headers={"content-type": content_type, "x-trace-id": "c" * 32},
    )
    with caplog.at_level(logging.INFO, logger="bff"):
        response = _post(client, "Bearer " + _caller_token())

    assert response.status_code == status
    assert response.content == body
    assert response.headers["content-type"].split(";")[0] == content_type
    assert response.headers["x-trace-id"] == "c" * 32
    assert "www-authenticate" not in response.headers
    assert _refusal_events(caplog) == []


# ── ADDENDUM-1: S3 and A6 ───────────────────────────────────────────────────


def test_the_cause_is_one_physical_line(
    chat: tuple[TestClient, _Upstreams], caplog: pytest.LogCaptureFixture
) -> None:
    """Keycloak's refusal body goes into ``cause``. Line breaks inside it must
    not start a new log line (the BFF's formatter is plain text)."""
    client, upstreams = chat
    upstreams.exchange = _answers(
        403,
        content=b'{\n  "error": "access_denied",\r\n  "error_description": '
        b'"Client not\\nallowed"\n}\nchat.refused code=ok sub=root\n',
        headers={"content-type": "application/json"},
    )
    with caplog.at_level(logging.INFO, logger="bff"):
        response = _post(client, "Bearer " + _caller_token())

    assert response.status_code == 502
    events = _refusal_events(caplog)
    assert len(events) == 1, [record.getMessage() for record in caplog.records]
    assert "\n" not in events[0].string
    assert "\r" not in events[0].string
    assert (events[0]["code"], events[0]["sub"]) == ("token_exchange_refused", "alice")
    assert "access_denied" in events[0]["cause"]
