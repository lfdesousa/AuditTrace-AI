"""MEMORY-SERVER-SILENT-TRUNCATION ADDENDUM-1 (gate r1 A4): a refused write
changes no record, so it writes NO memory-access audit row; an accepted one
writes exactly one."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from audittrace.config import get_settings
from tests.memory_truncation_support import memory_client
from tests.memory_truncation_support_a1 import memory_audit_details

LIMIT = 4096


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("AUDITTRACE_MEMORY_MAX_DOCUMENT_BYTES", str(LIMIT))
    get_settings.cache_clear()
    with memory_client() as c:
        yield c
    get_settings.cache_clear()


class TestMemoryRefusalAudit:
    def test_a_413_writes_no_audit_row(self, client: TestClient) -> None:
        refused = client.post(
            "/memory/episodic",
            json={"filename": "big.md", "content": "x" * (LIMIT + 1)},
        )

        assert refused.status_code == 413
        assert memory_audit_details() == []

    def test_control_an_accepted_write_writes_one(self, client: TestClient) -> None:
        accepted = client.post(
            "/memory/episodic", json={"filename": "ok.md", "content": "x" * LIMIT}
        )

        assert accepted.status_code == 200
        details = memory_audit_details()
        assert [(d["op"], d["layer"], d["key"]) for d in details] == [
            ("write", "episodic", "ok.md")
        ]
