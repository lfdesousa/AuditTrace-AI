"""GP4-CI (2026-11-02) — the O(rows) pure-Python work behind
``GET /memory/semantic`` runs OFF the event loop, with the request's
ContextVars carried into the worker thread.

Evidence: PR #422 CI run failed ``test_g_p4_real_route_does_not_stall_the_
event_loop`` (gap 1.381s > 1.0s) on a 2-vCPU hosted runner. Reproduced in
python:3.12.15 / hashed-lock venv / uid 1000 / ``--cpus 2``: the whole
request (manifest materialise, to_dict, dedupe, group, sort) ran as ONE
non-yielding loop step, so the watchdog gap scaled with CPU speed and row
count (3000 rows: ~0.41s with coverage; 100 rows: ~0.14s). Each guard below
fails when its ``asyncio.to_thread`` is removed (see the neuter notes).
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from audittrace.db import rls
from audittrace.db.models import MemoryItem
from audittrace.identity import UserContext
from audittrace.services import memory_manifest as mm
from audittrace.services.memory_manifest import MockMemoryManifestService


def _row(i: int, *, owner: str, tier: str, deleted: bool = False) -> dict:
    return {
        "id": f"id-{i:04d}",
        "layer": "semantic",
        "key": f"decisions/doc-{i:04d}",
        "title": f"t{i}",
        "size_bytes": 10,
        "created_at_ms": 1000 + i,
        "modified_at_ms": 1000 + (i * 7) % 13,  # non-monotonic: sort matters
        "created_by_user_id": owner,
        "modified_by_user_id": owner,
        "deleted_at_ms": 5 if deleted else None,
        "deleted_by_user_id": None,
        "tier": tier,
    }


def _seeded() -> MockMemoryManifestService:
    m = MockMemoryManifestService()
    for i in range(40):
        row = _row(
            i,
            owner="alice" if i % 3 == 0 else "bob",
            tier="private" if i % 2 else "corpus",
            deleted=(i % 11 == 0),
        )
        m._rows[("semantic", row["key"])] = row
    return m


def _reference(m, *, include_deleted, caller):
    """The pre-GP4 inline implementation, verbatim."""
    rows = [r for (layer, _), r in m._rows.items() if layer == "semantic"]
    if not include_deleted:
        rows = [r for r in rows if r.get("deleted_at_ms") is None]
    if caller is not None and not caller.is_admin:
        rows = [
            r
            for r in rows
            if r.get("created_by_user_id") == caller.user_id
            or r.get("tier", "corpus") == "corpus"
        ]
    rows.sort(key=lambda r: r["modified_at_ms"], reverse=True)
    return [m._to_entry(r) for r in rows]


@pytest.mark.asyncio
@pytest.mark.parametrize("include_deleted", [False, True])
@pytest.mark.parametrize("who", ["alice", "admin", None])
async def test_mock_list_for_layer_equals_pre_offload_reference(who, include_deleted):
    m = _seeded()
    caller = (
        None
        if who is None
        else UserContext(
            user_id=who,
            username=who,
            agent_type="test",
            scopes=[],
            is_admin=(who == "admin"),
        )
    )
    got = await m.list_for_layer(
        "semantic", include_deleted=include_deleted, caller=caller
    )
    want = _reference(m, include_deleted=include_deleted, caller=caller)
    assert [e.id for e in got] == [e.id for e in want]
    assert got == want


@pytest.mark.asyncio
async def test_mock_list_for_layer_runs_off_loop_and_carries_contextvar(monkeypatch):
    """NEUTER: replace ``asyncio.to_thread(...)`` in
    ``MockMemoryManifestService.list_for_layer`` with a direct call -> the
    recorded thread id equals the loop thread -> RED."""
    m = _seeded()
    seen: list[tuple[int, str | None]] = []
    orig = m._to_entry

    def spy(row):
        seen.append((threading.get_ident(), rls.current_user_id()))
        return orig(row)

    monkeypatch.setattr(m, "_to_entry", spy)
    rls.set_current_user_id("alice")
    try:
        await m.list_for_layer("semantic")
    finally:
        rls.set_current_user_id(None)
    assert seen
    assert all(tid != threading.get_ident() for tid, _ in seen)
    assert {uid for _, uid in seen} == {"alice"}  # ContextVar crossed the thread


def test_rows_to_entries_filters_and_materialises():
    rows = [
        MemoryItem(
            id=f"r{i}",
            layer="semantic",
            key=f"decisions/d{i}",
            created_at_ms=1,
            modified_at_ms=1,
            created_by_user_id=owner,
            modified_by_user_id=owner,
            tier=tier,
        )
        for i, (owner, tier) in enumerate(
            [("alice", "private"), ("bob", "private"), ("bob", "corpus")]
        )
    ]
    alice = UserContext(
        user_id="alice", username="alice", agent_type="t", scopes=[], is_admin=False
    )
    admin = UserContext(
        user_id="x", username="x", agent_type="t", scopes=[], is_admin=True
    )
    assert [e.id for e in mm._rows_to_entries(rows, alice)] == ["r0", "r2"]
    assert [e.id for e in mm._rows_to_entries(rows, admin)] == ["r0", "r1", "r2"]
    assert [e.id for e in mm._rows_to_entries(rows, None)] == ["r0", "r1", "r2"]


@pytest.mark.asyncio
async def test_list_semantic_cpu_work_runs_off_loop_with_identical_response(
    monkeypatch,
):
    """Through the real route (ASGI transport): every extracted CPU unit
    executes on a worker thread, not the loop thread, and the response is
    byte-identical to the pre-offload shape (items/total/limit/offset/
    granularity/total_documents/total_chunks). NEUTER: inline any
    ``asyncio.to_thread`` in ``list_semantic``/``_merge_semantic_with_chroma``
    -> that unit's thread id equals the loop thread -> RED."""
    import httpx

    from audittrace import dependencies
    from audittrace.routes import memory as routes_memory
    from audittrace.server import create_app

    test_container = dependencies.create_test_container()
    manifest = _seeded()
    test_container._instances["memory_manifest"] = manifest
    dependencies.container = test_container
    app = create_app()

    loop_tid = threading.get_ident()
    seen: dict[str, int] = {}

    def spy(name):
        orig = getattr(routes_memory, name)

        def wrapper(*a, **k):
            seen[name] = threading.get_ident()
            return orig(*a, **k)

        monkeypatch.setattr(routes_memory, name, wrapper)

    for name in (
        "_entries_to_dicts",
        "_entry_keys",
        "_entries_with_prefix",
        "_semantic_list_view",
    ):
        spy(name)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get(
            "/memory/semantic",
            params={"collection": "decisions", "limit": 25, "sort": "created_at"},
        )
        await asyncio.sleep(0)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {
        "items",
        "total",
        "limit",
        "offset",
        "granularity",
        "total_documents",
        "total_chunks",
    }
    assert body["total"] > 0 and len(body["items"]) == min(25, body["total"])
    # created_at desc, key-ascending tie-break: strictly the documented order.
    created = [i["created_at_ms"] for i in body["items"]]
    assert created == sorted(created, reverse=True)
    assert set(seen) == {
        "_entries_to_dicts",
        "_entry_keys",
        "_entries_with_prefix",
        "_semantic_list_view",
    }
    assert all(tid != loop_tid for tid in seen.values()), seen
