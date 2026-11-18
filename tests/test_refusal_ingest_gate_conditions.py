"""BFF-CHAT-ROUTE-ERRORS ADDENDUM-3 gate conditions C1 and C4, through the real route.

C1: the caller's forwarded element leaves a quote open, so it would swallow the
element the sidecar appends and the caller's own ``URI=<BFF>`` would stand as
the sidecar's statement. Refused: 403, no row.

C4: python-jose accepts a token that has NO ``aud`` claim. The refusal route
requires the audience (``jwt_audience``) to be present and to contain this
server's identifier: 422, the same sentence as every other bad subject, no row.
A list holding the audience is a valid audience claim.

Neuters: C1 drop the open-quote refusal in ``mesh_peer`` -> the two ``open_quote``
cells; C4 drop the route's audience-present check -> ``test_a_token_without_an_audience``.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from jose import jwt

from tests import test_refusal_ingest as base
from tests.test_refusal_ingest import (
    AUDIENCE,
    BFF,
    FROM_BFF,
    GATEWAY,
    ISSUER,
    KID,
    LOGIN_CLIENT,
    PATH,
    REALM_PRIVATE,
    SUB,
    _body,
    _element,
    _rows,
)

# The fixture of the real-route tests, re-bound so pytest finds it here.
ingest = base.ingest

NOT_A_REALM_TOKEN = "subject_token is not a valid token of this realm's login client"


def _token_with(claims_without: tuple[str, ...] = (), **claims: Any) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "sub": SUB,
        "aud": AUDIENCE,
        "azp": LOGIN_CLIENT,
        "iat": now,
        "exp": now + 300,
        **claims,
    }
    for name in claims_without:
        payload.pop(name)
    return jwt.encode(payload, REALM_PRIVATE, algorithm="RS256", headers={"kid": KID})


OPEN_QUOTE_HEADERS: dict[str, dict[str, str]] = {
    "attribute_then_gateway": {
        "X-Forwarded-Client-Cert": f'URI={BFF};a="' + "," + _element(GATEWAY)
    },
    "subject_then_gateway": {
        "X-Forwarded-Client-Cert": f'By=x;URI={BFF};Subject="' + "," + _element(GATEWAY)
    },
}


@pytest.mark.parametrize("case", sorted(OPEN_QUOTE_HEADERS))
def test_an_open_quote_in_an_earlier_element_does_not_name_the_bff(
    ingest: TestClient, case: str
) -> None:
    response = ingest.post(PATH, json=_body(), headers=OPEN_QUOTE_HEADERS[case])

    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}
    assert _rows(ingest) == []


def test_a_token_without_an_audience(ingest: TestClient) -> None:
    """Absent (not null, not foreign): python-jose accepts it, the route must not."""
    token = _token_with(claims_without=("aud",))

    response = ingest.post(PATH, json=_body(subject_token=token), headers=FROM_BFF)

    assert response.status_code == 422
    assert response.json() == {"detail": NOT_A_REALM_TOKEN}
    assert _rows(ingest) == []


def test_an_audience_list_holding_the_server_is_accepted(ingest: TestClient) -> None:
    """Control: the check is containment, not equality."""
    token = _token_with(aud=[AUDIENCE, "account"])

    response = ingest.post(PATH, json=_body(subject_token=token), headers=FROM_BFF)

    assert response.status_code == 201
    assert len(_rows(ingest)) == 1
