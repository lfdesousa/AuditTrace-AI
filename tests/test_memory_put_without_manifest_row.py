"""#61 WU-2 (live finding, rc2): a PUT must never write an object without a manifest row.

A corpus skill indexed from the shared store has NO manifest row. A ``PUT`` over
it resolved no tier (``private``), wrote a private object, THEN failed on the
missing row (404): the object stayed, shadowed the corpus skill for the caller,
and a soft delete could not reach it (404, no row). Now:

* PUT over a corpus file with no manifest row: 409 before any write;
* PUT over a key nothing stores, or over an own private object with no row: 404
  before any write (never an object without a row);
* the admin hard delete removes the caller's OWN private object that has no row
  (the repair path), audited, never another user's, never the corpus object.

Run on both manifest backends and both file layers (episodic, procedural).
Semantic is pinned as the control: its PUT already checked the row first.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from audittrace import dependencies
from tests.memory_truncation_support import USER, memory_client
from tests.memory_truncation_support_a1 import manifest
from tests.test_memory_update_corpus_document import (
    BODY,
    FILE_LAYERS,
    KEY,
    READER,
    URL,
    as_caller,
    audit_rows,
    use_sql_manifest,
)

NOUN = {"episodic": "ADR", "procedural": "SKILL"}
CORPUS_REFUSAL = (
    "{key} is a corpus-tier {layer} document. Not supported: "
    "updating a corpus document of the {layer} layer."
)
NO_ROW = "{noun} has no manifest row: no manifest row for layer={layer!r} key={key!r}"


@pytest.fixture(params=["mock", "sql"])
def client(request: pytest.FixtureRequest) -> Iterator[TestClient]:
    with memory_client() as c:
        if request.param == "sql":
            use_sql_manifest()
        yield c


def seed_corpus_without_row(layer: str) -> None:
    """The live shape: a corpus object in the shared store, no manifest row."""
    dependencies.container._instances[layer].add_document(
        "corpus-v1", file=KEY[layer], tier="corpus"
    )


def seed_private_orphan(layer: str, user_id: str, text: str) -> None:
    """A private object of *user_id* with no manifest row (what the bug left)."""
    dependencies.container._instances[layer].add_document(
        text, file=KEY[layer], tier="private", user_id=user_id
    )


def has_row(layer: str) -> bool:
    return asyncio.run(_row(layer)) is not None


async def _row(layer: str) -> Any:
    from audittrace.identity import UserContext

    user = UserContext(
        user_id=USER, username="u", agent_type="test", scopes=(), is_admin=True
    )
    return await manifest().resolve_read(layer, KEY[layer], user)


def stored(layer: str, user_id: str) -> tuple[str, str] | None:
    from audittrace.identity import UserContext

    user = UserContext(
        user_id=user_id, username="u", agent_type="test", scopes=(), is_admin=False
    )
    doc = asyncio.run(dependencies.container._instances[layer].read(user, KEY[layer]))
    return None if doc is None else (doc.page_content, doc.metadata["tier"])


def audit_details(key: str) -> list[dict[str, Any]]:
    """The stored ``detail`` of every DELETE audit row of *key* (op, tier dropped)."""
    from sqlalchemy import select

    from audittrace.db.models import InteractionRecord
    from audittrace.dependencies import get_postgres_factory
    from audittrace.services.memory_audit import MEMORY_AUDIT_EVENT_CLASS

    async def read() -> list[dict[str, Any]]:
        factory = get_postgres_factory().get_session_factory()
        async with factory() as db:
            found = await db.execute(
                select(InteractionRecord.error_detail)
                .where(InteractionRecord.event_class == MEMORY_AUDIT_EVENT_CLASS)
                .order_by(InteractionRecord.id)
            )
            return [json.loads(detail) for (detail,) in found.all()]

    return [
        {"hard": d.get("hard"), "orphan": d.get("orphan"), "layer": d["layer"]}
        for d in asyncio.run(read())
        if d.get("key") == key and d.get("op") == "delete"
    ]


@pytest.mark.parametrize("layer", FILE_LAYERS)
class TestPutOverTheCallersOwnSoftDeletedRow:
    def test_is_409_before_any_write_and_the_content_stays(
        self, client: TestClient, layer: str
    ) -> None:
        made = client.post(
            f"/memory/{layer}", json={"filename": KEY[layer], "content": "v1"}
        )
        assert made.status_code == 200, made.text
        assert client.delete(URL[layer]).status_code == 200
        rows = audit_rows(KEY[layer])
        put = client.put(URL[layer], json={BODY[layer]: "v2-after-tombstone"})
        assert put.status_code == 409
        assert put.json()["detail"] == (
            f"manifest row for layer={layer!r} key={KEY[layer]!r} is soft-deleted; "
            "use record_create to revive"
        )
        assert stored(layer, USER) == ("v1", "private")
        assert audit_rows(KEY[layer]) == rows


@pytest.mark.parametrize("layer", FILE_LAYERS)
class TestPutWritesNothingWithoutARow:
    @pytest.mark.parametrize("admin", [False, True], ids=["user", "admin"])
    def test_put_over_a_corpus_file_with_no_row_is_409_and_writes_nothing(
        self, client: TestClient, layer: str, admin: bool
    ) -> None:
        seed_corpus_without_row(layer)
        with as_caller(client, USER, admin=admin):
            put = client.put(URL[layer], json={BODY[layer]: "shadow"})
        assert put.status_code == 409
        assert put.json()["detail"] == CORPUS_REFUSAL.format(
            key=KEY[layer], layer=layer
        )
        assert stored(layer, USER) == ("corpus-v1", "corpus")
        assert not has_row(layer)
        assert audit_rows(KEY[layer]) == []

    def test_put_over_a_key_nothing_stores_is_404_and_writes_nothing(
        self, client: TestClient, layer: str
    ) -> None:
        put = client.put(URL[layer], json={BODY[layer]: "nothing"})
        assert put.status_code == 404
        assert put.json()["detail"] == NO_ROW.format(
            noun=NOUN[layer], layer=layer, key=KEY[layer]
        )
        assert stored(layer, USER) is None
        assert not has_row(layer)
        assert audit_rows(KEY[layer]) == []

    def test_put_over_an_own_private_object_with_no_row_is_404_and_unchanged(
        self, client: TestClient, layer: str
    ) -> None:
        seed_private_orphan(layer, USER, "orphan-v1")
        put = client.put(URL[layer], json={BODY[layer]: "orphan-v2"})
        assert put.status_code == 404
        assert stored(layer, USER) == ("orphan-v1", "private")
        assert audit_rows(KEY[layer]) == []

    def test_control_put_over_an_own_file_with_a_row_still_writes_it(
        self, client: TestClient, layer: str
    ) -> None:
        made = client.post(
            f"/memory/{layer}", json={"filename": KEY[layer], "content": "v1"}
        )
        assert made.status_code == 200, made.text
        put = client.put(URL[layer], json={"content": "v2"})
        assert put.status_code == 200, put.text
        assert stored(layer, USER) == ("v2", "private")
        assert has_row(layer)


@pytest.mark.parametrize("layer", FILE_LAYERS)
class TestOwnerHardDeleteRemovesAnOwnOrphan:
    @pytest.mark.parametrize("admin", [False, True], ids=["owner", "admin"])
    def test_it_removes_only_the_callers_private_object_and_audits_it(
        self, client: TestClient, layer: str, admin: bool
    ) -> None:
        seed_corpus_without_row(layer)
        seed_private_orphan(layer, USER, "orphan-v1")
        seed_private_orphan(layer, READER, "reader-private")
        with as_caller(client, USER, admin=admin):
            gone = client.delete(URL[layer], params={"hard": "true"})
        assert gone.status_code == 200, gone.text
        assert gone.json() == {
            "layer": layer,
            "key": KEY[layer],
            "tier": "private",
            "deleted": True,
            "orphan": True,
        }
        # The shadow is gone and the corpus object reads again for the caller.
        assert stored(layer, USER) == ("corpus-v1", "corpus")
        # Another user's object of the same name, and the corpus object, survive.
        assert stored(layer, READER) == ("reader-private", "private")
        assert not has_row(layer)
        assert audit_rows(KEY[layer]) == [("delete", "private")]
        assert audit_details(KEY[layer]) == [
            {"hard": True, "orphan": True, "layer": layer}
        ]

    def test_a_second_hard_delete_finds_nothing_and_is_404(
        self, client: TestClient, layer: str
    ) -> None:
        seed_corpus_without_row(layer)
        with as_caller(client, USER, admin=True):
            gone = client.delete(URL[layer], params={"hard": "true"})
        assert gone.status_code == 404
        assert stored(layer, USER) == ("corpus-v1", "corpus")
        assert audit_rows(KEY[layer]) == []

    def test_a_hard_delete_of_a_row_that_exists_still_needs_admin(
        self, client: TestClient, layer: str
    ) -> None:
        made = client.post(
            f"/memory/{layer}", json={"filename": KEY[layer], "content": "v1"}
        )
        assert made.status_code == 200, made.text
        gone = client.delete(URL[layer], params={"hard": "true"})
        assert gone.status_code == 403
        assert gone.json()["detail"] == "?hard=true requires audittrace:admin scope"
        assert stored(layer, USER) == ("v1", "private")
        assert audit_rows(KEY[layer]) == [("write", "private")]

    def test_the_route_needs_the_layers_write_scope(self, layer: str) -> None:
        """The orphan delete is open to the owner with the layer's write scope
        and to nobody without it: the scope is the route's own dependency."""
        from audittrace.routes.memory import router

        (route,) = [
            r
            for r in router.routes
            if getattr(r, "path", "") == f"/{layer}/{{filename}}"
            and "DELETE" in getattr(r, "methods", set())
        ]
        scopes = {
            scope
            for dep in route.dependant.dependencies  # type: ignore[attr-defined]
            for scope in (dep.own_oauth_scopes or [])
        }
        assert scopes == {f"memory:{layer}:write"}

    def test_a_store_failure_is_502_and_audits_nothing(
        self, client: TestClient, layer: str
    ) -> None:
        seed_private_orphan(layer, USER, "orphan-v1")
        service = dependencies.container._instances[layer]
        with (
            patch.object(
                type(service), "delete", AsyncMock(side_effect=RuntimeError("down"))
            ),
            as_caller(client, USER, admin=True),
        ):
            gone = client.delete(URL[layer], params={"hard": "true"})
        assert gone.status_code == 502
        assert gone.json()["detail"] == "down"
        assert stored(layer, USER) == ("orphan-v1", "private")
        assert audit_rows(KEY[layer]) == []

    def test_a_soft_delete_of_an_orphan_stays_404_and_removes_nothing(
        self, client: TestClient, layer: str
    ) -> None:
        seed_private_orphan(layer, USER, "orphan-v1")
        with as_caller(client, USER, admin=True):
            gone = client.delete(URL[layer])
        assert gone.status_code == 404
        assert stored(layer, USER) == ("orphan-v1", "private")


def reader_row(layer: str) -> dict[str, Any] | None:
    """The OTHER user's manifest row of the key, read as that user (None if none)."""
    from audittrace.identity import UserContext

    reader = UserContext(
        user_id=READER, username="r", agent_type="test", scopes=(), is_admin=False
    )
    row = asyncio.run(manifest().resolve_read(layer, KEY[layer], reader))
    return None if row is None else dict(row.to_dict())


@pytest.mark.parametrize("layer", FILE_LAYERS)
@pytest.mark.parametrize("hard", [True, False], ids=["hard", "soft"])
class TestNonAdminDeleteLeaksNothingAboutAnotherUsersFile:
    """R2-B1: ``DELETE`` by a NON-admin who holds the layer write scope must not
    tell an unknown key from another user's row-less object or from another
    user's row + object: same 404, byte-equal body, nothing of the other user
    touched, no audit row. Falsifiability: a path that answers 403 (or a
    different body) once another user holds that name turns this RED."""

    @pytest.mark.parametrize("other", ["row-less-object", "row-and-object"])
    def test_every_case_is_the_unknown_key_404_and_touches_nothing(
        self, client: TestClient, layer: str, hard: bool, other: str
    ) -> None:
        params = {"hard": "true"} if hard else {}
        with as_caller(client, USER):
            unknown = client.delete(URL[layer], params=params)
        assert unknown.status_code == 404, unknown.text
        assert unknown.json() == {
            "detail": f"no manifest row for layer={layer!r} key={KEY[layer]!r}"
        }

        if other == "row-less-object":
            seed_private_orphan(layer, READER, "reader-private")
            audit_before: list[tuple[str, str]] = []
        else:
            with as_caller(client, READER):
                made = client.post(
                    f"/memory/{layer}",
                    json={"filename": KEY[layer], "content": "reader-private"},
                )
            assert made.status_code == 200, made.text
            audit_before = [("write", "private")]
        row_before = reader_row(layer)
        assert (row_before is None) == (other == "row-less-object")

        with as_caller(client, USER):
            answer = client.delete(URL[layer], params=params)

        assert answer.status_code == 404
        assert answer.content == unknown.content
        # The other user's object and row read back unchanged.
        assert stored(layer, READER) == ("reader-private", "private")
        assert reader_row(layer) == row_before
        # The caller stored nothing, and no audit row was written by the delete.
        assert stored(layer, USER) is None
        assert audit_rows(KEY[layer]) == audit_before


def test_control_semantic_put_with_no_row_is_404_and_writes_nothing(
    client: TestClient,
) -> None:
    put = client.put("/memory/semantic/decisions/doc-none", json={"text": "x"})
    assert put.status_code == 404
    assert put.json()["detail"].startswith("semantic doc has no manifest row")
    assert audit_rows("doc-none") == []
