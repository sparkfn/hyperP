"""SQLAlchemy engine factory for the Fundbox MySQL database."""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL

from src.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return a process-wide SQLAlchemy engine for the Fundbox source DB.

    Cached so connectors share a single connection pool. ``pool_pre_ping`` is
    enabled because the source DB sits on the host network and may drop idle
    connections during long-running ingestions.
    """
    s = get_settings()
    url = URL.create(
        drivername="mysql+pymysql",
        username=s.fundbox_db_user,
        password=s.fundbox_db_password,
        host=s.fundbox_db_host,
        port=s.fundbox_db_port,
        database=s.fundbox_db_name,
        query={"charset": "utf8mb4"},
    )
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=1800,
        future=True,
    )
