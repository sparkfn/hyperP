"""Synchronous SQLAlchemy engine and transaction boundaries."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from deal_intelligence.migrations.cli import ALEMBIC_VERSION_TABLE
from deal_intelligence.platform.schema import PLATFORM_SCHEMA
from deal_intelligence.settings import Settings, get_settings


class Database:
    def __init__(self, settings: Settings) -> None:
        self._engine: Engine = create_engine(
            settings.sqlalchemy_database_url(), echo=settings.sql_echo, pool_pre_ping=True
        )
        self._sessions: sessionmaker[Session] = sessionmaker(
            bind=self._engine, autoflush=False, expire_on_commit=False
        )

    @property
    def engine(self) -> Engine:
        return self._engine

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        session = self._sessions()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def database_revisions(self) -> frozenset[str] | None:
        try:
            with self._engine.connect() as connection:
                return frozenset(
                    str(value)
                    for value in connection.execute(
                        text(f"SELECT version_num FROM {ALEMBIC_VERSION_TABLE}")
                    ).scalars()
                )
        except SQLAlchemyError:
            return None

    def platform_table_names(self) -> frozenset[str] | None:
        statement = text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = :schema"
        )
        try:
            with self._engine.connect() as connection:
                return frozenset(
                    str(value)
                    for value in connection.execute(
                        statement, {"schema": PLATFORM_SCHEMA}
                    ).scalars()
                )
        except SQLAlchemyError:
            return None

    def can_connect(self) -> bool:
        try:
            with self._engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False


@lru_cache
def get_database() -> Database:
    return Database(get_settings())
