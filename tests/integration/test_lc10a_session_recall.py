"""LC-10a LAYER I (integration): per-chat session recall on the REAL system.

Real routes through ``TestClient`` (real Starlette streaming for
``stream:true``), a real ``postgres:18.3`` schema built by ``alembic upgrade
head``, the app connected as a NOSUPERUSER NOBYPASSRLS role that does not
own the tables (so RLS is live for every route test), real
``recall_attachments`` dispatch and real ContextVars.

The ONE substitute is the LLM: a local HTTP server on 127.0.0.1 that answers
the first turn with a ``recall_attachments`` tool call and the second with a
final answer that ECHOES the tool message it received, so the response body
proves exactly what the server's tool returned. Memory layers this slice does
not touch are the inert doubles of ``create_test_container()``.

Postgres source order (S3): ``AUDITTRACE_TEST_POSTGRES_URL`` first, then an
ephemeral ``postgres:18.3``; when both are absent the module FAILS, it never
skips.

Neuters (break -> RED -> restore -> GREEN), recorded in the build record:
drop the ``session_id`` filter -> the ``s2`` cells; drop the upload bind ->
the ``s1`` cells; bare generator instead of ``stream_with_bound_session`` ->
the stream test; drop the ``with`` at ``routes/mcp.py`` -> the MCP test; omit
the WHOLE ``_is_postgres()`` block of migration 022 -> the RLS test (B sees
A's row); omit only CREATE POLICY -> the RLS owner read-back; run the check as
the superuser -> the RLS test; drop the index from 039 -> the PG-leg test.
"""

from __future__ import annotations

import importlib
import json
import uuid
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from sqlalchemy import create_engine, text

import audittrace.tools.memory_handlers as handlers_mod
from audittrace import config as config_mod
from audittrace import dependencies, instrumentation
from audittrace.dependencies import create_test_container
from audittrace.identity import SENTINEL_SUBJECT, sentinel_user_context
from audittrace.server import create_app
from audittrace.services.session_memory import PostgresSessionMemoryService
from audittrace.tools import reset_registry_for_tests
from tests.integration._lc10a_support import (
    _APP_ROLE,
    _READER_ROLE,
    _SENTINEL,
    _alembic,
    _role_url,
    fresh_db,
)

pytestmark = pytest.mark.integration

# ───────────────────────────────── the app ──────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_sqlalchemy_instrumentation() -> Iterator[None]:
    """Every engine the app opens registers with the process-wide
    SQLAlchemy instrumentor; hand it back as found so the modules that run
    after this one see a clean chokepoint (same cleanup as
    ``tests/test_sqlalchemy_spans.py``)."""
    inst = SQLAlchemyInstrumentor()

    def _clean() -> None:
        # Only the SQLAlchemy entry: the other instrumentors the app attaches
        # stay owned, exactly as after any other app-creating test.
        if inst.is_instrumented_by_opentelemetry:
            inst.uninstrument()
        instrumentation._SA_ENGINES.clear()
        instrumentation._OWNED.discard("SQLAlchemyInstrumentor")

    _clean()
    yield
    _clean()


@pytest.fixture
def client(
    db: dict[str, str], llama: ThreadingHTTPServer, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    reset_registry_for_tests()
    importlib.reload(handlers_mod)
    monkeypatch.setenv("AUDITTRACE_MEMORY_MODE", "tools")
    # The upload route builds its object-storage client before it looks at the
    # layer; the session layer never talks to it. A throwaway key lets the
    # client OBJECT exist (the SDK opens no connection on construction).
    monkeypatch.setenv("AUDITTRACE_MINIO_SECRET_KEY", "lc10a-unused-throwaway-key")
    monkeypatch.setenv(
        "AUDITTRACE_LLAMA_URL", f"http://127.0.0.1:{llama.server_port}/v1"
    )
    config_mod.get_settings.cache_clear()
    from audittrace.db.postgres import URLPostgresFactory

    container = create_test_container()
    factory = URLPostgresFactory(
        _role_url(db["admin"], _APP_ROLE, db["name"], "asyncpg")
    )
    container._instances["postgres_factory"] = factory
    container._instances["session_memory"] = PostgresSessionMemoryService(
        session_factory=factory.get_session_factory()
    )
    prior = dependencies.container
    dependencies.container = container
    try:
        with TestClient(create_app()) as c:
            yield c
    finally:
        dependencies.container = prior
        # Leave the tool registry as every other module expects to find it
        # (populated): this module runs FIRST alphabetically.
        reset_registry_for_tests()
        importlib.reload(handlers_mod)
        config_mod.get_settings.cache_clear()


# ───────────────────────────────── helpers ──────────────────────────────────


def _upload(c: TestClient, sid: str | None, name: str) -> None:
    headers = {} if sid is None else {"X-Session-Id": sid}
    r = c.post(
        "/memory/upload",
        params={"layer": "session"},
        files={"file": (name, _SENTINEL.encode(), "text/plain")},
        headers=headers,
    )
    assert r.status_code == 200, r.text


def _frames_text(body: str) -> str:
    out = []
    for line in body.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            for ch in json.loads(line[6:]).get("choices", []):
                out.append(str(ch.get("delta", {}).get("content") or ""))
    return "".join(out)


def _chat(c: TestClient, sid: str | None, *, stream: bool) -> str:
    headers = {} if sid is None else {"X-Session-Id": sid}
    r = c.post(
        "/v1/chat/completions",
        json={
            "model": "qwen3.5-35b",
            "messages": [{"role": "user", "content": "what did I just attach?"}],
            "stream": stream,
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return (
        _frames_text(r.text) if stream else r.json()["choices"][0]["message"]["content"]
    )


def _mcp(c: TestClient, sid: str | None) -> dict[str, Any]:
    headers = {} if sid is None else {"X-Session-Id": sid}
    r = c.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "recall_attachments", "arguments": {}},
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "error" not in body, body
    assert body["result"]["isError"] is False, body
    return body["result"]["structuredContent"]  # type: ignore[no-any-return]


def _assert_owner_sees_only_its_note(text_: str, name: str) -> None:
    assert text_.count(name) == 1, text_
    assert '"total": 1' in text_ or '"total":1' in text_, text_


def _assert_absent(text_: str, name: str) -> None:
    assert name not in text_, text_
    assert _SENTINEL[:12] not in text_ and _SENTINEL not in text_, text_


# ───────────────────────────────── the tests ────────────────────────────────


def test_upload_then_recall_same_session_only_nonstream(client: TestClient) -> None:
    """AC-11a + AC-18. s1 recalls its one note (200, owner read-back); s2 and
    a header-less recall get 200 with no trace of it (absence); a header-less
    UPLOAD is stored with a NULL compartment and recalled by no chat."""
    s1, s2 = f"s1-{uuid.uuid4().hex[:8]}", f"s2-{uuid.uuid4().hex[:8]}"
    name = f"note-{uuid.uuid4().hex[:8]}.txt"
    _upload(client, s1, name)
    _upload(client, None, f"headerless-{name}")
    _assert_owner_sees_only_its_note(_chat(client, s1, stream=False), name)
    _assert_absent(_chat(client, s2, stream=False), name)
    _assert_absent(_chat(client, None, stream=False), name)
    assert f"headerless-{name}" not in _chat(client, s1, stream=False)


def test_upload_then_recall_same_session_only_stream(client: TestClient) -> None:
    """AC-11b. The tools-mode stream runs in a COPIED context; the bound id
    must still be visible there. Every SSE frame is decoded."""
    s1, s2 = f"s1-{uuid.uuid4().hex[:8]}", f"s2-{uuid.uuid4().hex[:8]}"
    name = f"note-{uuid.uuid4().hex[:8]}.txt"
    _upload(client, s1, name)
    _assert_owner_sees_only_its_note(_chat(client, s1, stream=True), name)
    _assert_absent(_chat(client, s2, stream=True), name)
    _assert_absent(_chat(client, None, stream=True), name)


def test_upload_then_recall_same_session_only_mcp(client: TestClient) -> None:
    """AC-11c. ``POST /mcp`` tools/call: 200 with a JSON-RPC result."""
    s1, s2 = f"s1-{uuid.uuid4().hex[:8]}", f"s2-{uuid.uuid4().hex[:8]}"
    name = f"note-{uuid.uuid4().hex[:8]}.txt"
    _upload(client, s1, name)
    own = _mcp(client, s1)
    assert own["total"] == 1 and [m["title"] for m in own["matches"]] == [name]
    for other in (s2, None):
        res = _mcp(client, other)
        assert res["total"] == 0 and res["matches"] == []
        _assert_absent(json.dumps(res), name)


def test_rls_wall_holds_for_user_id_on_the_migrated_schema(
    db: dict[str, str], admin_url: str
) -> None:
    """AC-C5. RLS proven on the schema ``alembic upgrade head`` built, as a
    NOSUPERUSER NOBYPASSRLS role that does not own the table. A's row is
    written through the service as A; B sees the EMPTY set; A reads back
    exactly its row. (The ``session_id`` compartment is a service property,
    proven above; RLS compares ``user_id`` only, R5.)

    Neuters: omit the whole ``_is_postgres()`` block of 022 -> B sees 1 (RED);
    omit only CREATE POLICY -> A sees 0 (RED); read as the superuser -> RED."""
    import asyncio
    from dataclasses import replace

    from audittrace.db.postgres import URLPostgresFactory
    from audittrace.db.rls import install_rls_listener, set_current_user_id
    from audittrace.services.console_store._context import bound_session_id

    user_a = replace(sentinel_user_context(), user_id="user-a-c5", is_admin=False)
    name = f"c5-{uuid.uuid4().hex[:8]}.txt"
    install_rls_listener()

    async def write_as_a() -> None:
        # The write goes through the service as the table OWNER (a superuser
        # is exempt from RLS), so the row exists whatever the policy state is:
        # that is what lets the reads below, not the INSERT, be the thing a
        # neutered policy reddens.
        factory = URLPostgresFactory(
            db["admin"].replace("postgresql+psycopg2://", "postgresql+asyncpg://")
        )
        try:
            set_current_user_id("user-a-c5")
            with bound_session_id("s1"):
                await PostgresSessionMemoryService(factory.get_session_factory()).write(
                    user_a, name, _SENTINEL
                )
        finally:
            set_current_user_id(None)
            await factory.get_engine().dispose()

    asyncio.run(write_as_a())

    eng = create_engine(db["admin"], isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as c:
            c.execute(text(f'SET ROLE "{_READER_ROLE}"'))
            assert c.execute(text("SELECT current_user")).scalar() == _READER_ROLE
            c.execute(
                text("SELECT set_config('app.current_user_id','user-b-c5',false)")
            )
            assert (
                c.execute(text("SELECT * FROM session_memory_items")).fetchall() == []
            )
            c.execute(
                text("SELECT set_config('app.current_user_id','user-a-c5',false)")
            )
            rows = c.execute(
                text("SELECT filename, user_id FROM session_memory_items")
            ).fetchall()
            assert [tuple(r) for r in rows] == [(name, "user-a-c5")]
            c.execute(text("RESET ROLE"))
    finally:
        eng.dispose()


def test_migration_039_on_postgres_18(admin_url: str) -> None:
    """AC-10 (PG leg). On a FRESH database: column + index present after
    ``upgrade head``; downgrade removes both; upgrade again restores them.

    Neuter: drop the index from 039 -> the index assertions RED."""
    index = "ix_session_memory_items_user_session"

    def state(url: str) -> tuple[bool, str | None]:
        eng = create_engine(url)
        try:
            with eng.connect() as c:
                col = c.execute(
                    text(
                        "SELECT 1 FROM information_schema.columns WHERE "
                        "table_name='session_memory_items' AND column_name='session_id'"
                    )
                ).first()
                idx = c.execute(
                    text("SELECT indexdef FROM pg_indexes WHERE indexname=:i"),
                    {"i": index},
                ).scalar()
            return col is not None, idx
        finally:
            eng.dispose()

    with fresh_db(admin_url) as url:
        _alembic(url, "upgrade", "head")
        has_col, idx = state(url)
        assert has_col and idx is not None
        assert "user_id, session_id, created_at_ms DESC" in idx
        _alembic(url, "downgrade", "c76f1b005b75")
        assert state(url) == (False, None)
        _alembic(url, "upgrade", "head")
        assert state(url)[0] is True


def _rows_named(db: dict[str, str], name: str) -> list[tuple[str, str | None]]:
    """The owner-side read-back: ``(user_id, session_id)`` of every row with
    this filename, read as the superuser so RLS cannot hide a row."""
    eng = create_engine(db["admin"], isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as c:
            rows = c.execute(
                text(
                    "SELECT user_id, session_id FROM session_memory_items"
                    " WHERE filename = :f"
                ),
                {"f": name},
            ).fetchall()
    finally:
        eng.dispose()
    return [(r[0], r[1]) for r in rows]


@pytest.mark.parametrize(
    "bad_sid", ["x" * 256, "a b"], ids=["256-chars", "inner-space"]
)
def test_malformed_header_on_upload_is_stored_with_no_compartment(
    client: TestClient, db: dict[str, str], bad_sid: str
) -> None:
    """F1. The upload path's shape guard: a header that fails the accepted
    shape is ABSENT, so the route answers exactly 200 and the row lands with
    ``session_id IS NULL`` (never the raw value: 256 characters would not
    even fit VARCHAR(255)). A recall bound to that same header value gets
    nothing, and the owner read-back shows the row exists.

    Neuter: bind the raw header (``request.headers.get("x-session-id")``)
    in ``bound_upload_session`` -> the row carries the raw id (RED)."""
    name = f"bad-shape-{uuid.uuid4().hex[:8]}.txt"
    r = client.post(
        "/memory/upload",
        params={"layer": "session"},
        files={"file": (name, _SENTINEL.encode(), "text/plain")},
        headers={"X-Session-Id": bad_sid},
    )
    assert r.status_code == 200, r.text
    assert _rows_named(db, name) == [(SENTINEL_SUBJECT, None)]
    res = _mcp(client, bad_sid)
    assert res["total"] == 0 and res["matches"] == []
    _assert_absent(json.dumps(res), name)


def test_a_caller_naming_another_chat_of_another_user_sees_only_its_own_row(
    client: TestClient, db: dict[str, str]
) -> None:
    """A6 (two users, route level, RLS live, non-owner app role). A row of
    ``victim-user-0001`` lives under chat id ``sid``. The caller (the sentinel
    user) supplies that same chat id, uploads its own row under it, and
    recalls through the real chat route (stream false and true) and ``/mcp``:
    only its own row comes back.

    Neuters: drop the service ``user_id`` filter alone -> still GREEN (RLS is
    an independent wall); drop it AND the whole ``_is_postgres()`` block of
    migration 022 -> RED."""
    sid = f"shared-chat-{uuid.uuid4().hex[:8]}"
    victim_name = f"victim-{uuid.uuid4().hex[:8]}.txt"
    victim_content = f"VICTIM-CONTENT-{uuid.uuid4().hex}"
    eng = create_engine(db["admin"], isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as c:
            c.execute(
                text(
                    "INSERT INTO session_memory_items (id, user_id, filename,"
                    " content, size_bytes, created_at_ms, session_id) VALUES"
                    " (:i, :u, :f, :c, 1, 1, :s)"
                ),
                {
                    "i": str(uuid.uuid4()),
                    "u": "victim-user-0001",
                    "f": victim_name,
                    "c": victim_content,
                    "s": sid,
                },
            )
    finally:
        eng.dispose()
    own_name = f"own-{uuid.uuid4().hex[:8]}.txt"
    _upload(client, sid, own_name)
    for stream in (False, True):
        body = _chat(client, sid, stream=stream)
        assert own_name in body, body
        assert victim_name not in body and victim_content[:20] not in body, body
    res = _mcp(client, sid)
    assert res["total"] == 1, res
    assert [m["title"] for m in res["matches"]] == [own_name]
    dumped = json.dumps(res)
    assert victim_name not in dumped and victim_content[:20] not in dumped, dumped
