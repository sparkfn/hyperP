"""SQLAlchemy engine factory for the Fundbox MySQL database."""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import Engine

from src.connectors.db_factory import create_mysql_engine


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return a process-wide SQLAlchemy engine for the Fundbox source DB."""
    return create_mysql_engine("fundbox_consumer_backend")
