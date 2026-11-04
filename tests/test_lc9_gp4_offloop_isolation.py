"""LC-9 x G-P4: cross-user private-row isolation holds ACROSS the off-loop hop.

G-P4 (#428) moved ``list_for_layer``'s materialisation and owner-or-corpus
filter into a worker thread (``_rows_to_entries`` on the SQL service,
``_list_entries_sync`` on the Mock twin) and made the SQL query select plain
columns (``_MANIFEST_COLUMNS``). LC-9 added ``ManifestEntry.storage_key`` and
made ``from_row`` / ``_to_entry`` the single stored -> logical conversion.
These tests pin that both still hold together on BOTH backends:

* the caller crosses the hop and the worker filters by it (A sees her own row
  and the corpus row, never B's private row at the same logical key);
* every entry the worker builds carries the LOGICAL key and the STORED key;
* the hop really runs off the event-loop thread with the real caller;
* ``_MANIFEST_COLUMNS`` selects every column-backed field and not the derived
  ``storage_key``.

Neuters (each alone, restored afterwards):

* ``_rows_to_entries`` / ``_list_entries_sync`` drop the caller filter
  -> ``test_a_never_sees_b_row_after_the_hop`` RED (B's row listed);
* ``from_row`` / ``_to_entry`` set ``key=row.key``
  -> ``test_entries_carry_logical_and_storage_key_after_the_hop`` RED;
* ``list_for_layer`` passes ``None`` instead of ``caller`` to the hop
  -> ``test_hop_runs_off_loop_with_the_real_caller`` RED;
* ``_DERIVED_ENTRY_FIELDS`` gains a real column (``tier``)
  -> ``test_manifest_columns_are_the_column_backed_fields`` RED.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, fields
from typing import Any

import pytest
import pytest_asyncio

from audittrace.identity import UserContext
from audittrace.services import memory_manifest as mm
from audittrace.services.memory_manifest import (
    ManifestEntry,
    MemoryManifestService,
    MockMemoryManifestService,
)

A, B, CURATOR = "user-a", "user-b", "curator"


def _user(sub: str) -> UserContext:
    return UserContext(
        user_id=sub, username=sub, agent_type="test", scopes=[], is_admin=False
    )


@dataclass
class Env:
    svc: Any
    kind: str


@pytest_asyncio.fixture(params=["mock", "sql"])
async def env(request: pytest.FixtureRequest) -> Env:
    if request.param == "mock":
        return Env(MockMemoryManifestService(), "mock")
    from audittrace.db.postgres import InMemoryPostgresFactory

    factory = InMemoryPostgresFactory()
    await factory.create_schema()
    return Env(MemoryManifestService(factory.get_session_factory()), "sql")


async def _seed_two_owners_and_corpus(svc: Any) -> None:
    await svc.record_create("semantic", "decisions/same.md", "A's", 1, A)
    await svc.record_create("semantic", "decisions/same.md", "B's", 1, B)
    await svc.record_create(
        "semantic", "decisions/shared.md", "corpus", 1, CURATOR, tier="corpus"
    )


class TestIsolationAcrossTheOffLoopHop:
    async def test_a_never_sees_b_row_after_the_hop(self, env: Env) -> None:
        await _seed_two_owners_and_corpus(env.svc)
        listed = await env.svc.list_for_layer("semantic", caller=_user(A))
        owners = sorted((e.key, e.created_by_user_id) for e in listed)
        assert owners == [
            ("decisions/same.md", A),
            ("decisions/shared.md", CURATOR),
        ]
        assert all(e.created_by_user_id != B for e in listed)

    async def test_entries_carry_logical_and_storage_key_after_the_hop(
        self, env: Env
    ) -> None:
        await _seed_two_owners_and_corpus(env.svc)
        listed = await env.svc.list_for_layer("semantic", caller=_user(B))
        by_key = {e.key: e.storage_key for e in listed}
        assert by_key == {
            "decisions/same.md": f"{B}/decisions/same.md",
            "decisions/shared.md": "decisions/shared.md",
        }
        for e in listed:
            assert "storage_key" not in e.to_dict()
            assert not e.to_dict()["key"].startswith(f"{B}/")

    async def test_hop_runs_off_loop_with_the_real_caller(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _seed_two_owners_and_corpus(env.svc)
        seen: list[tuple[int, str | None]] = []
        loop_tid = threading.get_ident()

        if env.kind == "sql":
            orig = mm._rows_to_entries

            def spy_sql(rows: Any, caller: UserContext | None) -> Any:
                seen.append((threading.get_ident(), getattr(caller, "user_id", None)))
                return orig(rows, caller)

            monkeypatch.setattr(mm, "_rows_to_entries", spy_sql)
        else:
            orig_mock = env.svc._list_entries_sync

            def spy_mock(
                layer: str, include_deleted: bool, caller: UserContext | None
            ) -> Any:
                seen.append((threading.get_ident(), getattr(caller, "user_id", None)))
                return orig_mock(layer, include_deleted, caller)

            monkeypatch.setattr(env.svc, "_list_entries_sync", spy_mock)

        listed = await env.svc.list_for_layer("semantic", caller=_user(A))
        assert len(seen) == 1
        tid, caller_id = seen[0]
        assert tid != loop_tid
        assert caller_id == A
        assert {e.created_by_user_id for e in listed} == {A, CURATOR}


def test_manifest_columns_are_the_column_backed_fields() -> None:
    selected = {col.key for col in mm._MANIFEST_COLUMNS}
    expected = {f.name for f in fields(ManifestEntry)} - {"storage_key"}
    assert selected == expected
    assert "storage_key" not in selected
    assert {"key", "tier", "created_by_user_id"} <= selected
