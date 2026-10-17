"""Shared throwaway-``postgres:16`` scaffolding for the RLS proof suites
(SPEC v3 §8: ``tests/test_acl_ownership_rls.py``, ``test_console_store_rls_postgres.py``,
``test_rls_isolation.py``).

Prefixed with ``_`` so pytest never collects it as a test module (it has no
``test_*`` names and doesn't match ``python_files``). Container names and
DSN shapes are unchanged from what each file used to build inline; the only
behavioural change is the three non-durable flags (crash-durability only --
MVCC/RLS/rollback are unaffected, and no test restarts Postgres), so these
throwaway, single-use RLS proof containers commit faster without weakening
what they prove.
"""

from __future__ import annotations

import atexit
import os
import shutil
import socket
import subprocess
import time

from sqlalchemy import create_engine, text

#: SPEC v3 §8 -- crash-durability only. Kept as one named tuple of flags so
#: every ephemeral-Postgres caller in this repo uses the identical set (the
#: deployables guard's positive control asserts this file matches its
#: regex).
NONDURABLE_FLAGS = (
    "-c",
    "fsync=off",
    "-c",
    "synchronous_commit=off",
    "-c",
    "full_page_writes=off",
)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_ephemeral_postgres(name_prefix: str, password: str, db: str) -> str | None:
    """Spin a throwaway, non-durable ``postgres:16`` container.

    Returns the ``postgresql+psycopg2://`` DSN, or ``None`` (caller should
    skip) when Docker is unavailable. The container is ``--rm`` AND
    force-removed via ``atexit`` so nothing survives the session.
    """
    if shutil.which("docker") is None:
        return None
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=10)
    except Exception:
        return None

    port = free_port()
    name = f"{name_prefix}{os.getpid()}"
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                name,
                "-e",
                f"POSTGRES_PASSWORD={password}",
                "-e",
                f"POSTGRES_DB={db}",
                "-p",
                f"{port}:5432",
                "postgres:16",
                *NONDURABLE_FLAGS,
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except Exception:
        return None

    atexit.register(
        lambda: subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    )

    dsn = f"postgresql+psycopg2://postgres:{password}@127.0.0.1:{port}/{db}"
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            engine = create_engine(dsn, future=True)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            engine.dispose()
            return dsn
        except Exception:
            time.sleep(0.5)
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    return None
