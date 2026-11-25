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
from collections.abc import Iterator
from typing import Any

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
class TestAdminHardDeleteRemovesAnOwnOrphan:
    def test_it_removes_only_the_callers_private_object_and_audits_it(
        self, client: TestClient, layer: str
    ) -> None:
        seed_corpus_without_row(layer)
        seed_private_orphan(layer, USER, "orphan-v1")
        seed_private_orphan(layer, READER, "reader-private")
        with as_caller(client, USER, admin=True):
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

    def test_a_second_hard_delete_finds_nothing_and_is_404(
        self, client: TestClient, layer: str
    ) -> None:
        seed_corpus_without_row(layer)
        with as_caller(client, USER, admin=True):
            gone = client.delete(URL[layer], params={"hard": "true"})
        assert gone.status_code == 404
        assert stored(layer, USER) == ("corpus-v1", "corpus")
        assert audit_rows(KEY[layer]) == []

    def test_without_admin_the_hard_delete_is_403_and_the_orphan_stays(
        self, client: TestClient, layer: str
    ) -> None:
        seed_private_orphan(layer, USER, "orphan-v1")
        gone = client.delete(URL[layer], params={"hard": "true"})
        assert gone.status_code == 403
        assert stored(layer, USER) == ("orphan-v1", "private")

    def test_a_soft_delete_of_an_orphan_stays_404_and_removes_nothing(
        self, client: TestClient, layer: str
    ) -> None:
        seed_private_orphan(layer, USER, "orphan-v1")
        with as_caller(client, USER, admin=True):
            gone = client.delete(URL[layer])
        assert gone.status_code == 404
        assert stored(layer, USER) == ("orphan-v1", "private")


def test_control_semantic_put_with_no_row_is_404_and_writes_nothing(
    client: TestClient,
) -> None:
    put = client.put("/memory/semantic/decisions/doc-none", json={"text": "x"})
    assert put.status_code == 404
    assert put.json()["detail"].startswith("semantic doc has no manifest row")
    assert audit_rows("doc-none") == []
