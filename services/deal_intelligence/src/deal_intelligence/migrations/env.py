"""Alembic runtime environment for explicit migration commands only."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import Connection

from deal_intelligence.migrations.cli import configured_version_table
from deal_intelligence.platform.schema import metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata


def run_migrations_offline() -> None:
    """Run migrations without a database connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        version_table=configured_version_table(config),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations through a synchronous SQLAlchemy connection."""
    url = config.get_main_option("sqlalchemy.url")
    if url is None:
        raise RuntimeError("Alembic requires an explicit sqlalchemy.url")
    connectable = create_engine(url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        _run_migrations(connection)


def _run_migrations(connection: Connection) -> None:
    """Configure and execute the Alembic migration transaction."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table=configured_version_table(config),
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
