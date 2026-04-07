"""Neo4j async driver lifecycle."""

from __future__ import annotations

from neo4j import AsyncDriver, AsyncGraphDatabase, AsyncSession

from src.config import config

_driver: AsyncDriver | None = None


def get_driver() -> AsyncDriver:
    """Return the singleton async Neo4j driver, creating it on first use."""
    global _driver
    if _driver is None:
        _driver = AsyncGraphDatabase.driver(
            config.neo4j_uri,
            auth=(config.neo4j_user, config.neo4j_password),
            max_connection_pool_size=50,
            connection_acquisition_timeout=30.0,
        )
    return _driver


def get_session(write: bool = False) -> AsyncSession:
    """Open a new async session in read or write mode."""
    return get_driver().session(default_access_mode="WRITE" if write else "READ")


async def close_driver() -> None:
    """Close the singleton driver."""
    global _driver
    if _driver is not None:
        await _driver.close()
        _driver = None


async def verify_connectivity() -> None:
    """Raise if the Neo4j cluster is unreachable."""
    await get_driver().verify_connectivity()
