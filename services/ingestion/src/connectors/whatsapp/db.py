"""SQLAlchemy engine factory for the WhatsApp PostgreSQL database.

Uses the same SSH-tunnel pattern as the MySQL factory — ``create_postgres_engine``
tunnels through ``host.docker.internal`` when ``WHATSAPP_CHAT_SSH_HOST`` is set.
"""

from __future__ import annotations

from functools import lru_cache

import sqlalchemy
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine

from src.config import get_settings

_engine: sqlalchemy.Engine | None = None


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return a process-wide SQLAlchemy engine for the WhatsApp source DB."""
    settings = get_settings()
    if settings.whatsapp_chat_ssh_host:
        from sshtunnel import SSHTunnelForwarder

        tunnel = SSHTunnelForwarder(
            (settings.whatsapp_chat_ssh_host, settings.whatsapp_chat_ssh_port),
            ssh_username=settings.whatsapp_chat_ssh_user,
            ssh_password=settings.whatsapp_chat_ssh_password,
            remote_bind_address=(
                settings.whatsapp_chat_db_host,
                settings.whatsapp_chat_db_port or 5432,
            ),
        )
        tunnel.start()
        local_port = tunnel.local_bind_port
        host = "127.0.0.1"
        port = local_port
    else:
        host = settings.whatsapp_chat_db_host
        port = settings.whatsapp_chat_db_port or 5432

    url = URL.create(
        drivername="postgresql+psycopg2",
        username=settings.whatsapp_chat_db_user,
        password=settings.whatsapp_chat_db_password,
        host=host,
        port=port,
        database=settings.whatsapp_chat_db_name,
    )
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=1800,
        poolclass=sqlalchemy.pool.NullPool,
    )
