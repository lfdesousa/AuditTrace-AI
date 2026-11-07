"""Fixtures for the LC-10a layer-I module (``test_lc10a_session_recall.py``).

* ``admin_url`` - the postgres:18.3 server: ``AUDITTRACE_TEST_POSTGRES_URL``
  first, then an ephemeral container, else the module FAILS (never skips);
* ``db`` - a database built by ``alembic upgrade head`` plus its two
  NOSUPERUSER NOBYPASSRLS roles, dropped at teardown;
* ``llama`` - the one substitute: a local HTTP server standing in for the LLM.
"""

from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer

import pytest
from sqlalchemy import create_engine, text

from tests.integration._lc10a_support import (
    _APP_ROLE,
    _PREFIX,
    _READER_ROLE,
    _alembic,
    _create_role,
    _Llama,
    fresh_db,
)


@pytest.fixture(scope="module")
def admin_url() -> Iterator[str]:
    """Server-level DSN (psycopg2). Env first, then ephemeral, else FAIL."""
    env_url = os.environ.get("AUDITTRACE_TEST_POSTGRES_URL")
    if env_url:
        yield env_url
        return
    from tests._pg_ephemeral import start_ephemeral_postgres

    url = start_ephemeral_postgres(_PREFIX, "lc10a_it_admin_pw", "postgres")
    if url is None:
        pytest.fail(
            "AUDITTRACE_TEST_POSTGRES_URL unset and no ephemeral postgres:18.3 "
            "(Docker absent?) - the LC-10a layer-I proof must run, never skip",
            pytrace=False,
        )
    try:
        yield url
    finally:
        subprocess.run(["docker", "rm", "-fv", f"{_PREFIX}{os.getpid()}"], check=False)


@pytest.fixture(scope="module")
def db(admin_url: str) -> Iterator[dict[str, str]]:
    """A migrated database (``alembic upgrade head``) plus the two roles."""
    with fresh_db(admin_url) as url:
        _alembic(url, "upgrade", "head")
        name = url.rsplit("/", 1)[1]
        eng = create_engine(url, isolation_level="AUTOCOMMIT")
        with eng.connect() as c:
            _create_role(
                c,
                _APP_ROLE,
                'GRANT ALL ON ALL TABLES IN SCHEMA public TO "{role}"; '
                'GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO "{role}"',
            )
            _create_role(
                c, _READER_ROLE, 'GRANT SELECT ON session_memory_items TO "{role}"'
            )
        eng.dispose()
        try:
            yield {"admin": url, "name": name}
        finally:
            ceng = create_engine(admin_url, isolation_level="AUTOCOMMIT")
            with ceng.connect() as c:
                c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
                for role in (_APP_ROLE, _READER_ROLE):
                    c.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
            ceng.dispose()


@pytest.fixture(scope="module")
def llama() -> Iterator[ThreadingHTTPServer]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Llama)
    srv.seen = []  # type: ignore[attr-defined]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)
