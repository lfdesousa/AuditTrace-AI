"""Shared scaffolding for the LC-10a layer-I module (not collected by pytest).

* ``admin_url`` / ``db`` - the postgres:18.3 source (env first, then ephemeral,
  else the module FAILS) and a database built by ``alembic upgrade head`` with
  its two NOSUPERUSER NOBYPASSRLS roles;
* ``llama`` - the one substitute: a local HTTP server standing in for the LLM.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

_REPO = Path(__file__).resolve().parents[2]
_PW = "lc10a_it_pw"  # noqa: S105 - throwaway role credential for a throwaway database
_SENTINEL = "LC10A-SENTINEL-CONTENT-7f3a91c2d4"  # fixed so the 12-char check is exact
_PREFIX = "audittrace-lc10a-it-pg-"
_APP_ROLE, _READER_ROLE = "lc10a_app", "lc10a_rls_reader"


# ───────────────────────────── Postgres scaffolding ─────────────────────────


def _with_db(url: str, db: str) -> str:
    return url.rsplit("/", 1)[0] + "/" + db


def _role_url(url: str, role: str, db: str, driver: str) -> str:
    host = url.split("@", 1)[1].rsplit("/", 1)[0]
    return f"postgresql+{driver}://{role}:{_PW}@{host}/{db}"


def _alembic(url: str, action: str, rev: str) -> None:
    cfg = Config(str(_REPO / "alembic.ini"))
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            getattr(command, action)(cfg, rev)
    finally:
        engine.dispose()


@contextmanager
def fresh_db(admin: str) -> Iterator[str]:
    """A brand-new database (so nothing from another test or the server's
    default schema can colour the result), dropped on exit."""
    name = f"lc10a_{uuid.uuid4().hex[:10]}"
    eng = create_engine(admin, isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = _with_db(admin, name)
    try:
        yield url
    finally:
        with eng.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        eng.dispose()


def _create_role(c: Any, role: str, grants: str) -> None:
    if _role_exists(c, role):
        c.execute(text(f'DROP OWNED BY "{role}"'))
    c.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
    c.execute(
        text(
            f'CREATE ROLE "{role}" NOSUPERUSER NOBYPASSRLS NOCREATEDB LOGIN '
            f"PASSWORD '{_PW}'"
        )
    )
    c.execute(text(grants.format(role=role)))


def _role_exists(c: Any, role: str) -> bool:
    return bool(
        c.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:r"), {"r": role}).first()
    )


@contextmanager
def serving(srv: ThreadingHTTPServer) -> Iterator[ThreadingHTTPServer]:
    """Serve ``srv`` on a thread for the block; shut it down and join on every
    exit path. (Close the socket with ``with srv:`` around this.)"""
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        thread.join(timeout=5)


# ───────────────────────────── the LLM substitute ───────────────────────────


class _Llama(BaseHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:  # noqa: D401 - silence the server
        return

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        tool_msgs = [m for m in body["messages"] if m.get("role") == "tool"]
        self.server.seen.append(body)  # type: ignore[attr-defined]
        if tool_msgs:
            answer = "RECALL=" + str(tool_msgs[-1]["content"])
            chunk: dict[str, Any] = {"role": "assistant", "content": answer}
            finish, tool_calls = "stop", None
        else:
            chunk = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "recall_attachments", "arguments": "{}"},
                    }
                ],
            }
            finish, tool_calls = "tool_calls", chunk["tool_calls"]
        if body.get("stream"):
            delta = (
                {"content": chunk["content"]}
                if tool_calls is None
                else {"tool_calls": [{"index": 0, **tool_calls[0]}]}
            )
            frames = [
                {
                    "id": "c",
                    "created": 1,
                    "model": "m",
                    "choices": [{"index": 0, "delta": delta}],
                },
                {
                    "id": "c",
                    "created": 1,
                    "model": "m",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            ]
            payload = (
                "".join(f"data: {json.dumps(f)}\n\n" for f in frames)
                + "data: [DONE]\n\n"
            )
            ctype = "text/event-stream"
        else:
            payload = json.dumps(
                {
                    "id": "c",
                    "object": "chat.completion",
                    "model": "m",
                    "choices": [
                        {"index": 0, "message": chunk, "finish_reason": finish}
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }
            )
            ctype = "application/json"
        raw = payload.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
