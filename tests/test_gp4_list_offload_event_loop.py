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
The REAL ``MemoryManifestService`` (production class) is guarded in
``test_services_memory_manifest.py::TestListForLayerOffLoopPostgres``; the
route is compared against the pre-change implementation below (r2 F4).
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from audittrace.db import rls
from audittrace.db.models import MemoryItem
from audittrace.identity import UserContext
from audittrace.routes.memory_semantic_documents import (
    dedupe_semantic_chunks,
    group_semantic_chunks_by_document,
)
from audittrace.services import memory_manifest as mm
from audittrace.services.memory_manifest import MockMemoryManifestService


def _row(i: int, *, owner: str, tier: str, deleted: bool = False) -> dict:
    return {
        "id": f"id-{i:04d}",
        "layer": "semantic",
        "key": f"decisions/doc-{i:04d}",
        "title": f"t{i // 3}",  # three chunks share a title: grouping matters
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


def _user(uid, scopes=(), admin=False) -> UserContext:
    return UserContext(
        user_id=uid, username=uid, agent_type="t", scopes=list(scopes), is_admin=admin
    )


async def _app_with_seeded_manifest():
    from audittrace import dependencies
    from audittrace.server import create_app

    test_container = dependencies.create_test_container()
    manifest = _seeded()
    test_container._instances["memory_manifest"] = manifest
    dependencies.container = test_container
    chroma = dependencies.get_chromadb()
    col = await chroma.get_or_create_collection(
        name="decisions_v2", embedding_function=None
    )
    # Discovered-only rows (some overlapping the manifest chunk ids).
    await col.add(
        ids=[f"doc-{i:04d}" for i in range(0, 60, 5)],
        documents=["x"] * 12,
        metadatas=[
            {"tier": ("corpus" if i % 2 else "private"), "user_id": "alice"}
            for i in range(12)
        ],
    )
    return create_app(), manifest


async def _legacy_response(
    manifest,
    user,
    *,
    collection,
    include_deleted,
    granularity,
    sort,
    order,
    limit,
    offset,
):
    """The PRE-CHANGE ``list_semantic`` pipeline, inline and synchronous
    (f708042): old inline ``list_for_layer`` filter, old inline prefix filter,
    then the old inline corpus-gate / dedupe / group / sort / paginate tail.
    Only ``_merge_semantic_with_chroma`` (whose two changed lines are
    trivially equal) is the live function."""
    from audittrace.routes import memory as rm

    entries = _reference(manifest, include_deleted=include_deleted, caller=user)
    if collection is not None:
        prefix = f"{collection}/"
        entries = [e for e in entries if e.key.startswith(prefix)]
    items = await rm._merge_semantic_with_chroma(entries, collection, user)
    items = rm._filter_corpus_read_gate(items, user)
    chunk_items = dedupe_semantic_chunks(items)
    total_chunks = len(chunk_items)
    if granularity == "chunk":
        view_items = chunk_items
        total_documents = len(group_semantic_chunks_by_document(chunk_items))
    else:
        view_items = group_semantic_chunks_by_document(chunk_items)
        total_documents = len(view_items)
    page, total = rm._sort_and_paginate(
        view_items, sort=sort, order=order, limit=limit, offset=offset
    )
    return {
        "items": page,
        "total": total,
        "limit": limit,
        "offset": offset,
        "granularity": granularity,
        "total_documents": total_documents,
        "total_chunks": total_chunks,
    }


@pytest.mark.asyncio
async def test_list_semantic_cpu_work_runs_off_loop(monkeypatch):
    """Through the real route (ASGI transport): every extracted CPU unit
    executes on a worker thread, not the loop thread. NEUTER: inline any
    ``asyncio.to_thread`` in ``list_semantic``/``_merge_semantic_with_chroma``
    -> that unit's thread id equals the loop thread -> RED."""
    import httpx

    from audittrace.routes import memory as routes_memory

    app, _ = await _app_with_seeded_manifest()
    loop_tid = threading.get_ident()
    seen: dict[str, int] = {}

    def spy(name):
        orig = getattr(routes_memory, name)

        def wrapper(*a, **k):
            seen[name] = threading.get_ident()
            return orig(*a, **k)

        monkeypatch.setattr(routes_memory, name, wrapper)

    names = (
        "entries_to_dicts",
        "entry_keys",
        "entries_with_prefix",
        "semantic_list_view",
    )
    for name in names:
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
    assert body["total"] > 0
    assert set(seen) == set(names)
    assert all(tid != loop_tid for tid in seen.values()), seen


@pytest.mark.asyncio
async def test_list_semantic_byte_identical_to_pre_change_implementation():
    """F4 (review r1): the route output equals, BYTE FOR BYTE (canonical
    JSON), the pre-change inline implementation across identities x
    collection x include_deleted x granularity x sort x order. NEUTER: flip
    the order, skip the dedupe, drop the gate or give a worker another user
    -> the bytes differ -> RED."""
    import itertools
    import json

    import httpx

    from audittrace.auth import require_user

    app, manifest = await _app_with_seeded_manifest()
    users = {
        "alice": _user("alice", ("memory:semantic:read",)),
        "alice_corpus": _user(
            "alice", ("memory:semantic:read", "memory:corpus:decisions:read")
        ),
        "bob": _user("bob", ("memory:semantic:read",)),
        "stranger": _user("zed", ("memory:semantic:read",)),
        "admin": _user("root", (), True),
    }
    nonempty = 0
    count = 0
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        for uname, user in users.items():
            app.dependency_overrides[require_user] = lambda u=user: u
            grid = itertools.product(
                [None, "decisions", "nope"],
                [False, True],
                ["document", "chunk"],
                ["created_at", "modified_at", "key", "size"],
                ["asc", "desc"],
            )
            for coll, inc, gran, sort, order in grid:
                params = {
                    "include_deleted": str(inc).lower(),
                    "granularity": gran,
                    "sort": sort,
                    "order": order,
                    "limit": 7,
                    "offset": 3,
                }
                if coll:
                    params["collection"] = coll
                resp = await c.get("/memory/semantic", params=params)
                assert resp.status_code == 200, (uname, params, resp.text)
                want = await _legacy_response(
                    manifest,
                    user,
                    collection=coll,
                    include_deleted=inc,
                    granularity=gran,
                    sort=sort,
                    order=order,
                    limit=7,
                    offset=3,
                )
                canon = lambda o: json.dumps(o, sort_keys=True).encode()  # noqa: E731
                assert canon(resp.json()) == canon(want), (uname, params)
                count += 1
                nonempty += bool(want["items"])
    app.dependency_overrides.clear()
    assert count == 5 * 3 * 2 * 2 * 4 * 2
    assert nonempty > count // 3  # the comparison is not vacuously all-empty
