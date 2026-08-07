"""Alembic migration environment — ADR-020.

Reads database_url from audittrace-server Settings (12-factor).
Falls back to alembic.ini sqlalchemy.url if Settings not available.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from audittrace.db.models import Base

# Alembic Config object — provides access to .ini file values.
config = context.config

# Configure Python logging from .ini file.
#
# disable_existing_loggers=False (the non-default): fileConfig()'s DEFAULT
# (True) silently sets ``.disabled = True`` on every Python logger that
# already exists in the process at call time — e.g. every module-scoped
# ``logging.getLogger(__name__)`` created at import time. Under pytest, ALL
# test modules are imported during collection before any test body runs, so
# by the time the FIRST alembic upgrade/downgrade call reaches here (fired
# from any test using the ``alembic_cfg``/``engine`` fixtures), every OTHER
# module's logger already exists — and gets silently disabled for the rest
# of the session, breaking any later test's ``caplog`` assertions on THAT
# module with zero visible cause (discovered via #411 v2's mesh-heal ERROR
# log assertions going empty only when run after ``tests/test_alembic.py``,
# never in isolation). ``False`` scopes fileConfig() to configuring
# formatters/handlers as declared, without silently muting the rest of the
# process's loggers.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# SQLAlchemy MetaData for autogenerate support.
target_metadata = Base.metadata

# Override sqlalchemy.url from Settings if available.
#
# Alembic runs SYNCHRONOUSLY (``engine_from_config`` + ``connectable.connect()``
# in ``run_migrations_online``), so it must be handed the *sync* psycopg2 URL —
# NOT ``database_url`` (which is the asyncpg URL the request-loop engine uses).
# Passing the asyncpg URL here makes SQLAlchemy attempt async I/O on a plain
# sync connection → ``sqlalchemy.exc.MissingGreenlet`` at migration time, which
# crashes the entrypoint before the app can start. ``database_url_sync``
# normalises whichever driver ``AUDITTRACE_POSTGRES_URL`` carries down to
# ``postgresql+psycopg2://`` (ADR-020; psycopg2-binary is retained for exactly
# this path + the sync RLS oracle test).
try:
    from audittrace.config import get_settings

    settings = get_settings()
    if settings.database_url_sync:
        config.set_main_option("sqlalchemy.url", settings.database_url_sync)
except Exception:
    pass  # Fall back to alembic.ini value


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (no Engine needed)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (with Engine + connection)."""
    # Support passing a connection via config.attributes for testing.
    connectable = config.attributes.get("connection")

    if connectable is not None:
        context.configure(connection=connectable, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    else:
        connectable = engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )

        with connectable.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)

            with context.begin_transaction():
                context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
