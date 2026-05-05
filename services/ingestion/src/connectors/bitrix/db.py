"""SQLAlchemy engine factory for the Bitrix24 MariaDB chat database.

Uses the same SSH-tunnel pattern as the MySQL factory —
``create_mysql_engine`` handles the SSH tunnel automatically via
``BITRIX_CHAT_SSH_HOST``.
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import Engine

from src.connectors.db_factory import create_mysql_engine


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return a process-wide SQLAlchemy engine for the Bitrix24 source DB."""
    return create_mysql_engine("bitrix_chat")
