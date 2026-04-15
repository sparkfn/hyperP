"""SQLAlchemy engine factory for the Eko POS MySQL database."""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import Engine

from src.connectors.db_factory import create_mysql_engine


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return a process-wide SQLAlchemy engine for the Eko POS DB."""
    return create_mysql_engine("eko_phppos")
