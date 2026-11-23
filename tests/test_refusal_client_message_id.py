"""CONSOLE-AUDIT-EVIDENCE WU-A -- a refused turn carries the message id (A6 S1/Q7, A7 C3).

Memory-server half only (the BFF payload field is WU-C): ``RefusalIngestRequest`` gains
``client_message_id`` validated by a ``field_validator`` applying the chain predicate with
``re.fullmatch`` (``Field(pattern=...)`` cannot: the predicate has a lookahead), and ``_row``
stores it, unhashed by construction.

G29 (server half): a refusal posted with ``client_message_id: m`` -> row has it,
``status=failed``, ``failure_class == code``, ``GET /evidence/messages/m`` returns that row
and ``lookup.candidates`` counts it; without the key -> NULL.

Neuters (each alone): drop the field from ``_row`` -> ``test_the_row_carries_the_message_id``
RED; drop the validator -> ``test_an_invalid_id_is_a_422_that_names_the_field`` RED (``..``
accepted); ``fullmatch`` -> ``match`` -> the ``abc\\n`` cell RED; add the field to
``_CONTENT_FIELDS`` -> ``test_the_hash_is_unchanged_by_the_id`` RED.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from audittrace.integrity import verify_content_hash
from tests import test_refusal_ingest as _refusals
from tests._evidence_rig import all_rows
from tests.test_refusal_ingest import FROM_BFF, PATH, SUB, _body

#: Registers the refusal-ingest app fixture of ``test_refusal_ingest`` in this module
#: (a fixture is collected from the module namespace, by attribute name).
ingest = _refusals.ingest

MID = "b7b3f4e0-1c2d-4e5f-8a9b-0c1d2e3f4a5b"


def _evidence(client: TestClient, mid: str) -> Any:
    from unittest.mock import MagicMock, patch

    with (
        patch("audittrace.auth.get_settings") as s,
        patch("audittrace.auth._get_jwks_keys") as k,
        patch("audittrace.auth._decode_jwt_with_allowed_issuers") as d,
    ):
        s.return_value = MagicMock(auth_enabled=True, auth_required=True)
        k.return_value = ["fake-key"]
        d.return_value = {"sub": SUB, "scope": "audittrace:audit"}
        return client.get(
            f"/evidence/messages/{mid}", headers={"Authorization": "Bearer t"}
        )


async def test_the_row_carries_the_message_id(ingest: TestClient) -> None:
    response = ingest.post(PATH, json=_body(client_message_id=MID), headers=FROM_BFF)
    assert response.status_code == 201
    (row,) = await all_rows()
    assert row.client_message_id == MID
    assert row.status == "failed"
    assert row.failure_class == "token_exchange_refused"


async def test_without_the_key_the_column_is_null(ingest: TestClient) -> None:
    assert ingest.post(PATH, json=_body(), headers=FROM_BFF).status_code == 201
    (row,) = await all_rows()
    assert row.client_message_id is None


async def test_the_evidence_route_returns_the_refused_row(
    ingest: TestClient,
) -> None:
    ingest.post(PATH, json=_body(client_message_id=MID), headers=FROM_BFF)
    r = _evidence(ingest, MID)
    assert r.status_code == 200
    body = r.json()
    assert body["lookup"] == {"by": "client_message_id", "value": MID, "candidates": 1}
    assert body["interaction"]["status"] == "failed"
    assert body["interaction"]["failure_class"] == "token_exchange_refused"
    assert body["interaction"]["client_message_id"] == MID
    assert body["integrity"]["recomputed"] == "match"


async def test_the_hash_is_unchanged_by_the_id(ingest: TestClient) -> None:
    ingest.post(PATH, json=_body(client_message_id=MID), headers=FROM_BFF)
    (row,) = await all_rows()
    assert verify_content_hash(row) is True


@pytest.mark.parametrize(
    "bad",
    [
        "..",
        ".",
        "../x",
        "a/b",
        "abc\n",
        "a\nb",
        "x" * 256,
        "{{LIBRECHAT_BODY_MESSAGEID}}",
        "",
    ],
    ids=lambda v: repr(v)[:24],
)
async def test_an_invalid_id_is_a_422_that_names_the_field(
    ingest: TestClient, bad: str
) -> None:
    response = ingest.post(PATH, json=_body(client_message_id=bad), headers=FROM_BFF)
    assert response.status_code == 422
    assert response.json() == {"detail": {"invalid_fields": ["client_message_id"]}}
    assert await all_rows() == []


async def test_an_explicit_null_is_accepted_as_absent(
    ingest: TestClient,
) -> None:
    response = ingest.post(PATH, json=_body(client_message_id=None), headers=FROM_BFF)
    assert response.status_code == 201
    (row,) = await all_rows()
    assert row.client_message_id is None


def test_the_openapi_schema_declares_the_field() -> None:
    from audittrace.routes._refusal_request import RefusalIngestRequest

    props = RefusalIngestRequest.model_json_schema()["properties"]
    assert "client_message_id" in props
