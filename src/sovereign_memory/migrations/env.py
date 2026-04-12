"""Alembic migration environment — ADR-020.

Reads database_url from sovereign-memory-server Settings (12-factor).
Falls back to alembic.ini sqlalchemy.url if Settings not available.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from sovereign_memory.db.models import Base

# Alembic Config object — provides access to .ini file values.
config = context.config

# Configure Python logging from .ini file.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# SQLAlchemy MetaData for autogenerate support.
target_metadata = Base.metadata

# Override sqlalchemy.url from Settings if available.
try:
    from sovereign_memory.config import get_settings

    settings = get_settings()
    if settings.database_url:
        config.set_main_option("sqlalchemy.url", settings.database_url)
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
