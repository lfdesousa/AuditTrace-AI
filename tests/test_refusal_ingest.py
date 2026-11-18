"""BFF-CHAT-ROUTE-ERRORS ADDENDUM-2, item 2 (server side): ``POST
/interactions/refusals`` writes one failed-interaction row for a chat turn the
LibreChat BFF refused.

Lands as ``tests/test_refusal_ingest.py``. The row is an ordinary
``interactions`` row (no new table, column or event class): ``status`` failed,
``failure_class`` the refusal code. The user is NEVER taken from the request:
it is the ``sub`` of the subject token, which the server verifies (signature
and issuer; an expired token is still proof of who was refused).

Neuters (each alone): S1 take the user from a body field -> ``test_a_user_field
_is_refused``; S2 skip the signature check -> ``test_a_token_this_realm_did_not
_sign_writes_nothing[foreign_key]``; S3 skip the issuer check -> the
``foreign_issuer`` cell; S4 check expiry -> ``test_an_expired_token_is_still
_proof_of_identity``; S5 drop the enable switch -> ``test_the_route_does_not
_exist_unless_enabled``; S6 store the token in the row -> ``test_the_row``; S7
accept any code -> ``test_the_vocabulary_is_closed``.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jose import jwt

from audittrace.config import get_settings

ISSUER = "https://keycloak.test/realms/audittrace"
SUB = "0b0cdd4d-04c3-428f-ab9d-37b47429c381"
SESSION = "librechat-conversation-7"
INBOUND_TRACE = "0af7651916cd43dd8448eb211c80319c"


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


def _token(*, key: str = REALM_PRIVATE, exp_offset: int = 300, **claims: Any) -> str:
    now = int(time.time())
    payload = {"iss": ISSUER, "sub": SUB, "iat": now, "exp": now + exp_offset}
    payload.update(claims)
    return jwt.encode(payload, key, algorithm="RS256")


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


@pytest.fixture
def ingest(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The app with the route enabled and this test's realm key as the JWKS."""
    settings = get_settings().model_copy(
        update={
            "refusal_ingest_enabled": True,
            "keycloak_issuer": ISSUER,
            "keycloak_issuer_extras": [],
        }
    )
    monkeypatch.setattr("audittrace.routes.refusals.get_settings", lambda: settings)

    async def jwks(_url: str) -> list[Any]:
        return [REALM_PUBLIC]

    monkeypatch.setattr("audittrace.routes.refusals._get_jwks_keys", jwks)
    return client


def _rows(client: TestClient, session_id: str = SESSION) -> list[dict[str, Any]]:
    response = client.get(f"/interactions?session_id={session_id}")
    assert response.status_code == 200
    rows: list[dict[str, Any]] = response.json()["interactions"]
    return rows


def test_the_row(ingest: TestClient) -> None:
    """One ordinary failed-interaction row, attributed to the token's subject."""
    body = _body()
    response = ingest.post("/interactions/refusals", json=body)

    assert response.status_code == 201
    assert response.json() == {
        "recorded": True,
        "failure_class": "token_exchange_refused",
    }
    rows = _rows(ingest)
    assert len(rows) == 1
    row = rows[0]
    assert {
        key: row[key]
        for key in (
            "user_id",
            "session_id",
            "status",
            "failure_class",
            "source",
            "question",
            "answer",
            "event_class",
        )
    } == {
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


def test_a_user_field_is_refused(ingest: TestClient) -> None:
    """The writer cannot name the user: any such field is a 422, no row."""
    for field in ("user_id", "sub", "username"):
        response = ingest.post(
            "/interactions/refusals", json=_body(**{field: "mallory"})
        )
        assert response.status_code == 422
    assert _rows(ingest) == []


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(lambda: _token(key=FOREIGN_PRIVATE), id="foreign_key"),
        pytest.param(
            lambda: _token(iss="https://elsewhere/realms/x"), id="foreign_issuer"
        ),
        pytest.param(lambda: "not-a-jwt", id="not_a_jwt"),
        pytest.param(lambda: _token(sub=""), id="empty_subject"),
    ],
)
def test_a_token_this_realm_did_not_sign_writes_nothing(
    ingest: TestClient, token: Any
) -> None:
    response = ingest.post("/interactions/refusals", json=_body(subject_token=token()))

    assert response.status_code == 422
    assert _rows(ingest) == []


def test_an_expired_token_is_still_proof_of_identity(ingest: TestClient) -> None:
    """A turn can be refused BECAUSE the token expired between the BFF's check
    and the exchange; the record must still be attributable."""
    response = ingest.post(
        "/interactions/refusals",
        json=_body(
            subject_token=_token(exp_offset=-600), code="invalid_token", http_status=401
        ),
    )

    assert response.status_code == 201
    assert [row["user_id"] for row in _rows(ingest)] == [SUB]


@pytest.mark.parametrize(
    "override",
    [
        {"code": "anything_else"},
        {"route": "memory"},
        {"http_status": 200},
        {"session_id": "x code=ok"},
        {"inbound_trace_id": "not-hex"},
        {"subject_token": ""},
    ],
    ids=[
        "code",
        "route",
        "http_status",
        "session_id",
        "inbound_trace_id",
        "empty_token",
    ],
)
def test_the_vocabulary_is_closed(ingest: TestClient, override: dict[str, Any]) -> None:
    response = ingest.post("/interactions/refusals", json=_body(**override))

    assert response.status_code == 422
    assert _rows(ingest) == []


def test_the_route_does_not_exist_unless_enabled(client: TestClient) -> None:
    """Off by default: the route has no JWT of its writer, so it exists only
    where a mesh rule admits the BFF alone (the chart sets both together)."""
    assert get_settings().refusal_ingest_enabled is False
    response = client.post("/interactions/refusals", json=_body())

    assert response.status_code == 404
