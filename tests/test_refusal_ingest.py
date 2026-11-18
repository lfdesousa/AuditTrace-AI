"""BFF-CHAT-ROUTE-ERRORS ADDENDUM-3 (supersedes ADDENDUM-2's file of the same
name): ``POST /interactions/refusals`` writes one failed-interaction row for a
chat turn the LibreChat BFF refused.

Lands as ``tests/test_refusal_ingest.py``. Two questions are answered
separately, and neither by how the path is spelled:

* WHO WRITES: the route is mounted only when its one writer is named, and it
  admits a request only when this workload's sidecar says that SPIFFE identity
  is the immediate peer (the LAST element of ``X-Forwarded-Client-Cert``). The
  request line must spell the path exactly: the server decodes ``%2F``, the
  mesh's path match does not.
* WHOSE TURN: the ``sub`` of the subject token, verified like every token of
  this server (signature, issuer, audience, expiry) and issued to the login
  client. Never a field of the request.

Neuters (each alone): W1 drop the peer check -> the ``other_writer`` cells and
both ``encoded`` cells' status; W2 read the FIRST element of the header ->
``forwarded_then_gateway``; W3 drop the exact-path check ->
``test_a_path_spelled_otherwise_is_not_this_route``; W4 mount the route
unconditionally -> ``test_without_a_named_writer_the_route_is_absent``; W5 skip
the audience check -> ``foreign_audience``; W6 skip expiry -> ``expired``; W7
skip the client check -> ``other_client``; W8 echo the body in the 422 ->
``test_a_body_that_does_not_validate_is_answered_without_its_values``; W9 take
the user from a body field -> ``test_a_user_field_is_refused``; W10 store the
token in the row -> ``test_the_row``; W11 accept any code -> ``test_the_
vocabulary_is_closed``; W12 let the framework parse the body (it does so before any
dependency) -> ``test_the_writer_is_checked_before_the_body``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jose import jwk, jwt

from audittrace import config, dependencies
from audittrace.server import create_app

ISSUER = "https://keycloak.test/realms/audittrace"
AUDIENCE = "audittrace-server"
LOGIN_CLIENT = "audittrace-librechat"
SUB = "0b0cdd4d-04c3-428f-ab9d-37b47429c381"
SESSION = "librechat-conversation-7"
INBOUND_TRACE = "0af7651916cd43dd8448eb211c80319c"
KID = "realm-rs256-1"
BFF = "spiffe://cluster.local/ns/audittrace/sa/audittrace-librechat-bff"
GATEWAY = (
    "spiffe://cluster.local/ns/istio-system/sa/istio-ingressgateway-service-account"
)
SELF = "spiffe://cluster.local/ns/audittrace/sa/audittrace-memory-server"
PATH = "/interactions/refusals"


def _key() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )
    return private, public


REALM_PRIVATE, REALM_PUBLIC = _key()
FOREIGN_PRIVATE, _FOREIGN_PUBLIC = _key()
REALM_JWK = {**jwk.construct(REALM_PUBLIC, "RS256").to_dict(), "kid": KID, "use": "sig"}


def _token(
    *, key: str = REALM_PRIVATE, exp_offset: int | None = 300, **claims: Any
) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "sub": SUB,
        "aud": AUDIENCE,
        "azp": LOGIN_CLIENT,
        "iat": now,
    }
    if exp_offset is not None:
        payload["exp"] = now + exp_offset
    payload.update(claims)
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": KID})


def _body(**override: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "route": "chat",
        "code": "token_exchange_refused",
        "http_status": 502,
        "subject_token": _token(),
        "session_id": SESSION,
        "inbound_trace_id": INBOUND_TRACE,
    }
    body.update(override)
    return body


def _element(uri: str, **extra: str) -> str:
    """One hop as Envoy writes it."""
    fields = {"By": SELF, "Hash": "9f" * 32, "Subject": '""', "URI": uri, **extra}
    return ";".join(f"{name}={value}" for name, value in fields.items())


def _peer(*elements: str) -> dict[str, str]:
    return {"X-Forwarded-Client-Cert": ",".join(elements)}


FROM_BFF = _peer(_element(BFF))


@pytest.fixture
def ingest(
    test_container: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """The real app, built with the route's one writer named, and this test's
    realm key as the JWKS."""
    monkeypatch.setenv("AUDITTRACE_REFUSAL_INGEST_WRITER", BFF)
    monkeypatch.setenv("AUDITTRACE_KEYCLOAK_ISSUER", ISSUER)
    monkeypatch.setenv("AUDITTRACE_KEYCLOAK_JWKS_URL", "http://keycloak.test/jwks")
    monkeypatch.setenv("AUDITTRACE_JWT_AUDIENCE", AUDIENCE)
    config.get_settings.cache_clear()

    async def jwks(_url: str) -> list[Any]:
        return [REALM_JWK]

    monkeypatch.setattr("audittrace.auth._fetch_jwks_keys", jwks)
    monkeypatch.setattr("audittrace.auth._jwks_cache", {})
    monkeypatch.setattr("audittrace.auth._jwks_fetch_lock", None)
    dependencies.container = test_container
    with TestClient(create_app()) as client:
        yield client
    config.get_settings.cache_clear()


def _rows(client: TestClient) -> list[dict[str, Any]]:
    response = client.get(f"/interactions?session_id={SESSION}")
    assert response.status_code == 200
    rows: list[dict[str, Any]] = response.json()["interactions"]
    return rows


def test_the_row(ingest: TestClient) -> None:
    """One ordinary failed-interaction row, attributed to the token's subject."""
    body = _body()
    response = ingest.post(PATH, json=body, headers=FROM_BFF)

    assert response.status_code == 201
    assert response.json() == {
        "recorded": True,
        "failure_class": "token_exchange_refused",
    }
    rows = _rows(ingest)
    assert len(rows) == 1
    row = rows[0]
    wanted = (
        "user_id",
        "session_id",
        "status",
        "failure_class",
        "source",
        "question",
        "answer",
        "event_class",
    )
    assert {key: row[key] for key in wanted} == {
        "user_id": SUB,
        "session_id": SESSION,
        "status": "failed",
        "failure_class": "token_exchange_refused",
        "source": "librechat-bff",
        "question": "refused_turn",
        "answer": "token_exchange_refused",
        "event_class": None,
    }
    assert json.loads(row["error_detail"]) == {
        "row_type": "refused_turn",
        "route": "chat",
        "code": "token_exchange_refused",
        "http_status": 502,
        "inbound_trace_id": INBOUND_TRACE,
    }
    assert body["subject_token"] not in json.dumps(row)


# ── WHO WRITES ──────────────────────────────────────────────────────────────

OTHER_WRITERS: dict[str, dict[str, str]] = {
    "no_header": {},
    "gateway": _peer(_element(GATEWAY)),
    # A caller wrote the BFF's identity into the header; the sidecar appended
    # the real peer (the gateway) AFTER it. Only the last element counts.
    "forwarded_then_gateway": _peer(_element(BFF), _element(GATEWAY)),
    "two_uris_in_the_last_element": _peer(_element(GATEWAY) + f";URI={BFF}"),
    "uri_inside_a_quoted_subject": _peer(
        _element(GATEWAY, Subject=f'"CN=x,URI={BFF};URI={BFF}"')
    ),
    "almost_the_bff": _peer(_element(BFF + "-2")),
    "empty_header": {"X-Forwarded-Client-Cert": ""},
}


@pytest.mark.parametrize("who", sorted(OTHER_WRITERS))
def test_other_writer_is_refused(ingest: TestClient, who: str) -> None:
    """Whatever it sends, a valid body included: 403 and no row."""
    response = ingest.post(PATH, json=_body(), headers=OTHER_WRITERS[who])

    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}
    assert _rows(ingest) == []


def test_the_bff_behind_an_earlier_hop_is_admitted(ingest: TestClient) -> None:
    """Elements before the last travelled with the request; the last one is
    this sidecar's own statement, and it names the BFF."""
    response = ingest.post(
        PATH, json=_body(), headers=_peer(_element(GATEWAY), _element(BFF))
    )

    assert response.status_code == 201


def test_the_writer_is_checked_before_the_body(ingest: TestClient) -> None:
    """A body that is not JSON, sent AS JSON: the framework would answer its
    own 422 before any dependency ran. Another peer gets 403; the BFF gets a
    422 that names ``body`` and nothing of what was sent."""
    as_json = {"Content-Type": "application/json"}
    other = ingest.post(
        PATH, content=b'{"x": ', headers={**_peer(_element(GATEWAY)), **as_json}
    )
    bff = ingest.post(PATH, content=b'{"x": ', headers={**FROM_BFF, **as_json})

    assert other.status_code == 403
    assert bff.status_code == 422
    assert bff.json() == {"detail": {"invalid_fields": ["body"]}}


@pytest.mark.parametrize(
    "spelling", ["/interactions%2Frefusals", "/interactions%2frefusals"]
)
@pytest.mark.parametrize("who", ["bff", "gateway"])
def test_a_path_spelled_otherwise_is_not_this_route(
    ingest: TestClient, spelling: str, who: str
) -> None:
    """The server decodes the encoded slash and would route it here; the mesh's
    path match would not have recognised it. It is answered as if the route
    did not exist, for any peer: never 201, never the body's 422."""
    headers = FROM_BFF if who == "bff" else _peer(_element(GATEWAY))
    response = ingest.post(spelling, json=_body(), headers=headers)

    assert response.status_code == 404
    assert _rows(ingest) == []


def test_without_a_named_writer_the_route_is_absent(client: TestClient) -> None:
    """Default settings: not mounted. A valid body, a malformed one and the
    OpenAPI document all say so."""
    assert config.get_settings().refusal_ingest_writer == ""
    assert client.post(PATH, json=_body(), headers=FROM_BFF).status_code == 404
    assert client.post(PATH, content=b"{", headers=FROM_BFF).status_code == 404
    assert PATH not in client.get("/openapi.json").json()["paths"]


# ── WHOSE TURN ──────────────────────────────────────────────────────────────

BAD_SUBJECTS: dict[str, Any] = {
    "foreign_key": lambda: _token(key=FOREIGN_PRIVATE),
    "foreign_issuer": lambda: _token(iss="https://elsewhere/realms/x"),
    "foreign_audience": lambda: _token(aud="some-other-api"),
    "no_audience": lambda: _token(aud=None),
    "expired": lambda: _token(exp_offset=-600),
    "no_expiry": lambda: _token(exp_offset=None),
    "other_client": lambda: _token(azp="audittrace-opencode"),
    "empty_subject": lambda: _token(sub=""),
    "not_a_jwt": lambda: "not-a-jwt",
}


@pytest.mark.parametrize("what", sorted(BAD_SUBJECTS))
def test_a_subject_token_that_does_not_verify_writes_nothing(
    ingest: TestClient, what: str
) -> None:
    token = BAD_SUBJECTS[what]()
    response = ingest.post(PATH, json=_body(subject_token=token), headers=FROM_BFF)

    assert response.status_code == 422
    assert response.json() == {
        "detail": "subject_token is not a valid token of this realm's login client"
    }
    assert _rows(ingest) == []


def test_a_user_field_is_refused(ingest: TestClient) -> None:
    """The writer cannot name the user: any such field is a 422, no row."""
    for field in ("user_id", "sub", "username"):
        response = ingest.post(PATH, json=_body(**{field: "mallory"}), headers=FROM_BFF)
        assert response.status_code == 422
        assert response.json() == {"detail": {"invalid_fields": [field]}}
    assert _rows(ingest) == []


CLOSED: dict[str, dict[str, Any]] = {
    "code": {"code": "anything_else"},
    "route": {"route": "memory"},
    "http_status": {"http_status": 200},
    "session_id": {"session_id": "x code=ok"},
    "inbound_trace_id": {"inbound_trace_id": "not-hex"},
    "subject_token": {"subject_token": ""},
}


@pytest.mark.parametrize("field", sorted(CLOSED))
def test_the_vocabulary_is_closed(ingest: TestClient, field: str) -> None:
    response = ingest.post(PATH, json=_body(**CLOSED[field]), headers=FROM_BFF)

    assert response.status_code == 422
    assert response.json() == {"detail": {"invalid_fields": [field]}}
    assert _rows(ingest) == []


def test_a_body_that_does_not_validate_is_answered_without_its_values(
    ingest: TestClient,
) -> None:
    """The framework's own 422 echoes the input, the token included."""
    body = _body(code="anything_else")
    for sent in (body, [body], {k: v for k, v in body.items() if k != "route"}):
        response = ingest.post(PATH, json=sent, headers=FROM_BFF)
        assert response.status_code == 422
        assert body["subject_token"] not in response.text
        assert "anything_else" not in response.text
