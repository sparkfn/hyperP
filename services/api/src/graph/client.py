"""Neo4j async driver lifecycle."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any, TypeVar

from neo4j import (
    AsyncDriver,
    AsyncGraphDatabase,
    AsyncResult,
    AsyncSession,
    NotificationMinimumSeverity,
    Query,
)

from src.config import config
from src.request_timing import current_request_id, record_repository_duration

_driver: AsyncDriver | None = None
_T = TypeVar("_T")


def get_driver() -> AsyncDriver:
    """Return the singleton async Neo4j driver, creating it on first use."""
    global _driver
    if _driver is None:
        _driver = AsyncGraphDatabase.driver(
            config.neo4j_uri,
            auth=(config.neo4j_user, config.neo4j_password),
            max_connection_pool_size=50,
            connection_acquisition_timeout=30.0,
            notifications_min_severity=NotificationMinimumSeverity.WARNING,
        )
    return _driver


class TimedAsyncSession:
    """Session facade that bounds API read queries but leaves writes untouched.

    The API and ingestion services use independent graph clients. This affects
    repository-mediated API reads only, not ingestion or mutation work.
    """

    def __init__(self, session: AsyncSession, *, write: bool) -> None:
        self._session = session
        self._write = write

    async def __aenter__(self) -> TimedAsyncSession:
        await self._session.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._session.__aexit__(*args)

    async def run(
        self,
        query: str | Query,
        parameters: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncResult:
        started_at = monotonic()
        try:
            if self._write or isinstance(query, Query) or current_request_id() is None:
                return await self._session.run(query, parameters, **kwargs)
            return await self._session.run(
                Query(query, timeout=config.neo4j_web_read_transaction_timeout_seconds),
                parameters,
                **kwargs,
            )
        finally:
            record_repository_duration(started_at)

    async def execute_read(
        self,
        transaction_function: Callable[..., Awaitable[_T]],
        *args: Any,
        **kwargs: Any,
    ) -> _T:
        return await self._session.execute_read(transaction_function, *args, **kwargs)

    async def execute_write(
        self,
        transaction_function: Callable[..., Awaitable[_T]],
        *args: Any,
        **kwargs: Any,
    ) -> _T:
        return await self._session.execute_write(transaction_function, *args, **kwargs)


def get_session(write: bool = False) -> TimedAsyncSession:
    """Open an API graph session with a hard timeout for read queries only."""
    session = get_driver().session(default_access_mode="WRITE" if write else "READ")
    return TimedAsyncSession(session, write=write)


async def close_driver() -> None:
    """Close the singleton driver."""
    global _driver
    if _driver is not None:
        await _driver.close()
        _driver = None


async def verify_connectivity() -> None:
    """Raise if the Neo4j cluster is unreachable."""
    await get_driver().verify_connectivity()
