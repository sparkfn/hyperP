"""Shared MySQL engine factory with optional SSH tunnel support.

Each connector's ``db.py`` calls :func:`create_mysql_engine` with a
``prefix`` that maps to environment variables. For example, prefix
``"fundbox"`` reads ``FUNDBOX_SSH_HOST``, ``FUNDBOX_DB_HOST``, etc.

When the ``*_SSH_HOST`` variable is non-empty the connection is tunnelled
through SSH.  Otherwise the engine connects directly.
"""

from __future__ import annotations

import atexit

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL
from sshtunnel import SSHTunnelForwarder

from src.config import Settings, get_settings

# One tunnel per prefix, kept alive for the process lifetime.
_tunnels: dict[str, SSHTunnelForwarder] = {}


def _shutdown_tunnels() -> None:
    for tunnel in _tunnels.values():
        tunnel.stop()
    _tunnels.clear()


atexit.register(_shutdown_tunnels)


def create_mysql_engine(prefix: str) -> Engine:
    """Build a SQLAlchemy engine for the connector identified by *prefix*.

    Reads settings attributes named ``{prefix}_ssh_host``,
    ``{prefix}_db_host``, ``{prefix}_db_port``, etc.
    """
    s = get_settings()
    ssh_host: str = getattr(s, f"{prefix}_ssh_host")
    db_host: str = getattr(s, f"{prefix}_db_host")
    db_port: int = getattr(s, f"{prefix}_db_port")
    db_user: str = getattr(s, f"{prefix}_db_user")
    db_password: str = getattr(s, f"{prefix}_db_password")
    db_name: str = getattr(s, f"{prefix}_db_name")

    if ssh_host.strip():
        tunnel = _get_or_create_tunnel(prefix, s, ssh_host, db_host, db_port)
        host = "127.0.0.1"
        port = tunnel.local_bind_port
    else:
        host = db_host
        port = db_port

    url = URL.create(
        drivername="mysql+pymysql",
        username=db_user,
        password=db_password,
        host=host,
        port=port,
        database=db_name,
        query={"charset": "utf8mb4"},
    )
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=1800,
        future=True,
    )


def _get_or_create_tunnel(
    prefix: str,
    s: Settings,
    ssh_host: str,
    remote_host: str,
    remote_port: int,
) -> SSHTunnelForwarder:
    existing = _tunnels.get(prefix)
    if existing is not None and existing.is_active:
        return existing

    ssh_port: int = getattr(s, f"{prefix}_ssh_port")
    ssh_user: str = getattr(s, f"{prefix}_ssh_user")
    ssh_password: str = getattr(s, f"{prefix}_ssh_password")

    tunnel = SSHTunnelForwarder(
        (ssh_host, ssh_port),
        ssh_username=ssh_user,
        ssh_password=ssh_password,
        remote_bind_address=(remote_host, remote_port),
    )
    tunnel.start()
    _tunnels[prefix] = tunnel
    return tunnel
