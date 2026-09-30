"""One throwaway, non-durable Postgres per worker (SPEC v3 §8, §9; SF-3).

Harness containers run ``postgres:16 -c fsync=off -c synchronous_commit=off
-c full_page_writes=off`` -- crash-durability only; MVCC/RLS/rollback are
unaffected, no test restarts Postgres. The instrument never trusts the
launch flags: it reads the server back (``SHOW ...``) and refuses (exit 6)
if any of the three is not ``off``.

``fake=True`` (``--no-db``) is the self-proof seam (§11): it skips docker
and Postgres entirely, storing the "server" settings and catalog state in
a small JSON sidecar file instead, so proofs h/f exercise the exact same
read-back and catalog-diff code paths as a real run, deterministically and
without a container.
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

EXIT_NONDURABLE_SETTINGS = 6

NONDURABLE_FLAGS = [
    "-c",
    "fsync=off",
    "-c",
    "synchronous_commit=off",
    "-c",
    "full_page_writes=off",
]
REQUIRED_SETTINGS = ("fsync", "synchronous_commit", "full_page_writes")

_CATALOG_EXCLUDED_SCHEMAS = frozenset(
    {"pg_catalog", "information_schema", "pg_toast", "public"}
)


class NonDurableSettingsError(Exception):
    def __init__(self, settings: dict[str, str]) -> None:
        super().__init__(f"Postgres is not non-durable: {settings}")
        self.settings = settings


@dataclass(frozen=True)
class PgHandle:
    name: str
    dsn: str
    fake: bool = False
    fake_state_path: Path | None = None


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_container(
    run_id: str,
    worker_idx: int,
    *,
    tag: str = "postgres:16",
    tmpfs: bool = False,
    fake: bool = False,
    fake_dir: Path | None = None,
) -> PgHandle:
    """Start (or fake) one worker's throwaway Postgres.

    ``fake=True`` writes an initial "server settings" sidecar reflecting the
    requested (non-durable) flags -- exactly what a real ``postgres:16``
    would report back over ``SHOW`` -- and a stub catalog snapshot.
    """
    name = f"audittrace-neuter-{run_id}-w{worker_idx}"
    if fake:
        assert fake_dir is not None, "fake mode requires fake_dir"
        fake_dir.mkdir(parents=True, exist_ok=True)
        state_path = fake_dir / f"{name}.json"
        state_path.write_text(
            json.dumps(
                {
                    "settings": {
                        "fsync": "off",
                        "synchronous_commit": "off",
                        "full_page_writes": "off",
                    },
                    "schemata": [],
                    "sessions": 0,
                }
            )
        )
        return PgHandle(
            name=name, dsn=f"fake://{state_path}", fake=True, fake_state_path=state_path
        )

    port = free_port()
    password = "neuter_ephemeral_pw"  # noqa: S105 - throwaway container credential
    db = "audittrace_neuter"
    cmd = [
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
        f"127.0.0.1:{port}:5432",
    ]
    if tmpfs:
        cmd += ["--tmpfs", "/var/lib/postgresql/data"]
    cmd += [tag, *NONDURABLE_FLAGS]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    dsn = f"postgresql+psycopg2://postgres:{password}@127.0.0.1:{port}/{db}"
    _wait_ready(dsn)
    return PgHandle(name=name, dsn=dsn)


def stop_container(handle: PgHandle) -> None:
    if handle.fake:
        return
    subprocess.run(["docker", "rm", "-f", handle.name], capture_output=True)


def _wait_ready(dsn: str, timeout_s: float = 60.0) -> None:
    from sqlalchemy import create_engine, text

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            engine = create_engine(dsn, future=True)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            engine.dispose()
            return
        except Exception:  # noqa: BLE001 - retry loop, any connection failure is transient here
            time.sleep(0.5)
    raise TimeoutError(f"Postgres never became ready: {dsn}")


def read_settings(handle: PgHandle) -> dict[str, str]:
    """``SHOW fsync; SHOW synchronous_commit; SHOW full_page_writes`` (§8)."""
    if handle.fake:
        state = json.loads(handle.fake_state_path.read_text())  # type: ignore[union-attr]
        return dict(state["settings"])

    from sqlalchemy import create_engine, text

    engine = create_engine(handle.dsn, future=True)
    try:
        with engine.connect() as conn:
            return {
                name: conn.execute(text(f"SHOW {name}")).scalar()
                for name in REQUIRED_SETTINGS
            }
    finally:
        engine.dispose()


def assert_nondurable(settings: dict[str, str]) -> None:
    if any(settings.get(name) != "off" for name in REQUIRED_SETTINGS):
        raise NonDurableSettingsError(settings)


def db_snapshot(handle: PgHandle) -> tuple[frozenset[str], int]:
    """``(schemata, app-role session count)`` for catalog isolation (§9)."""
    if handle.fake:
        state = json.loads(handle.fake_state_path.read_text())  # type: ignore[union-attr]
        return frozenset(state["schemata"]), int(state["sessions"])

    from sqlalchemy import create_engine, text

    engine = create_engine(handle.dsn, future=True)
    try:
        with engine.connect() as conn:
            rows = (
                conn.execute(
                    text("SELECT schema_name FROM information_schema.schemata")
                )
                .scalars()
                .all()
            )
            schemata = frozenset(rows) - _CATALOG_EXCLUDED_SCHEMAS
            sessions = conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                )
            ).scalar()
        return schemata, int(sessions or 0)
    finally:
        engine.dispose()


def poll_db_leak(
    handle: PgHandle,
    before: tuple[frozenset[str], int],
    *,
    deadline_s: float = 5.0,
    interval_s: float = 0.25,
) -> bool:
    """Poll every ``interval_s`` up to ``deadline_s``; ``db_leak`` only if the
    catalog is still dirty at the deadline (§9)."""
    deadline = time.time() + deadline_s
    after = db_snapshot(handle)
    while after != before and time.time() < deadline:
        time.sleep(interval_s)
        after = db_snapshot(handle)
    return after != before


#: Product-owned, DURABLE ephemeral-Postgres container name prefixes (the
#: three RLS-proof test files' own bring-up, `tests/_pg_ephemeral.py`) --
#: none of these may exist during a neuter run. A mock-engine (or any)
#: worker that accidentally imports one of those test modules without the
#: worker's own DSN pre-set would otherwise start one of these as an
#: uncontrolled side effect of import (§9).
PRODUCT_PG_CONTAINER_PREFIXES = (
    "audittrace-acl-wu2a-pg-",
    "audittrace-rls-pg-",
    "audittrace-console-store-pg-",
)


def foreign_product_pg_container_running() -> bool:
    """``docker ps`` scan for any of :data:`PRODUCT_PG_CONTAINER_PREFIXES`
    (§9: "no such container appears during a neuter")."""
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True
    )
    if result.returncode != 0:
        return False
    names = result.stdout.splitlines()
    return any(
        name.startswith(prefix)
        for name in names
        for prefix in PRODUCT_PG_CONTAINER_PREFIXES
    )
