"""MEMORY-SERVER-SILENT-TRUNCATION, WU-3: ``POST /memory/upload`` of a text
document records the same manifest row as ``POST /memory/<layer>``.

At 1f44bf3e the upload route stores the object and returns ``200`` but writes
no manifest row: the layer's read route answers ``"manifest": null``, so the
stored object's size is recorded nowhere a later read can compare it with.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from tests.memory_truncation_support import USER, memory_client, upload

TEXT = "# uploaded\n\nvoilà, a body with a two-byte character.\n"


@pytest.fixture
def client() -> Iterator[TestClient]:
    with memory_client() as c:
        yield c


class TestMemoryUploadManifestRow:
    @pytest.mark.parametrize("layer", ["episodic", "procedural"])
    def test_upload_records_a_manifest_row_with_the_stored_size(
        self, client: TestClient, layer: str
    ) -> None:
        uploaded = upload(client, layer, "note.md", TEXT)
        assert uploaded.status_code == 200, uploaded.text

        read = client.get(f"/memory/{layer}/note.md")

        assert read.status_code == 200
        manifest = read.json()["manifest"]
        assert manifest is not None
        assert manifest["key"] == "note.md"
        assert manifest["size_bytes"] == len(TEXT.encode("utf-8"))
        assert manifest["created_by_user_id"] == USER
        assert manifest["tier"] == "private"

    @pytest.mark.parametrize("layer", ["episodic", "procedural"])
    def test_control_create_route_records_that_row_today(
        self, client: TestClient, layer: str
    ) -> None:
        created = client.post(
            f"/memory/{layer}", json={"filename": "note.md", "content": TEXT}
        )
        assert created.status_code == 200, created.text

        manifest = client.get(f"/memory/{layer}/note.md").json()["manifest"]

        assert manifest["key"] == "note.md"
        assert manifest["size_bytes"] == len(TEXT.encode("utf-8"))
        assert manifest["created_by_user_id"] == USER
        assert manifest["tier"] == "private"
