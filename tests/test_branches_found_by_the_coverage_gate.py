"""BFF-CHAT-ROUTE-ERRORS: three guards the per-file coverage gate found unexercised.

* ``token_kid`` (the byte-equal twins in ``audittrace.realm_keys`` and
  ``bff.realm_keys``): a ``kid`` that is not a string is the caller's token, not
  the realm's, and raises ``JWTError``.
* ``bff.route_refusals.route_last_resort``: a client that disconnects is not an
  internal error; it is re-raised, never answered.
* ``bff.route_refusals.framework_error``: a framework error that is not a 404 or
  a 405 keeps its own status, ``detail`` and headers.

Neuters (each alone): K1 drop the ``kid`` type check in each twin ->
``test_a_kid_that_is_not_a_string_is_the_callers_token``; D1 turn the
``ClientDisconnect`` arm into the generic answer -> ``test_a_disconnect_is_not_
answered``; F1 route every status to the 404/405 shapes ->
``test_another_status_keeps_its_own_answer``.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest
from jose import JWTError, jwt
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect

from audittrace import realm_keys as server_keys
from bff import realm_keys as bff_keys
from bff.route_refusals import framework_error, route_last_resort


@pytest.mark.parametrize("keys", [server_keys, bff_keys], ids=["server", "bff"])
@pytest.mark.parametrize("kid", [5, ["a"], {"k": "v"}, True])
def test_a_kid_that_is_not_a_string_is_the_callers_token(
    keys: ModuleType, kid: Any
) -> None:
    token = jwt.encode({"sub": "x"}, "secret", algorithm="HS256", headers={"kid": kid})

    with pytest.raises(JWTError, match="kid"):
        keys.token_kid(token)


@pytest.mark.parametrize("keys", [server_keys, bff_keys], ids=["server", "bff"])
def test_a_string_kid_and_no_kid_are_read(keys: ModuleType) -> None:
    named = jwt.encode(
        {"sub": "x"}, "secret", algorithm="HS256", headers={"kid": "realm-1"}
    )
    anonymous = jwt.encode({"sub": "x"}, "secret", algorithm="HS256")

    assert keys.token_kid(named) == "realm-1"
    assert keys.token_kid(anonymous) is None


async def test_a_disconnect_is_not_answered() -> None:
    async def handler(*, request: Any) -> Any:
        raise ClientDisconnect()

    guarded = route_last_resort("memory")(handler)

    with pytest.raises(ClientDisconnect):
        await guarded(request=object())


async def test_another_status_keeps_its_own_answer() -> None:
    response = await framework_error(
        None,  # type: ignore[arg-type]
        StarletteHTTPException(status_code=418, detail="teapot", headers={"X-A": "b"}),
    )

    assert response.status_code == 418
    assert response.body == b'{"detail":"teapot"}'
    assert response.headers["x-a"] == "b"
