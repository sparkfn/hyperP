"""Neo4j driver wrapper with managed sessions and connectivity checks."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Callable

from neo4j import Driver, GraphDatabase, ManagedTransaction, Session

from src.config import Settings

logger = logging.getLogger(__name__)


class Neo4jClient:
    """Thin wrapper around the official Neo4j Python driver.

    Provides:
    - Explicit session context manager
    - ``execute_write`` / ``execute_read`` helpers
    - Connectivity verification
    - Clean shutdown
    """

    def __init__(self, settings: Settings) -> None:
        self._driver: Driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )

    # -- session management ---------------------------------------------------

    @contextmanager
    def session(self, **kwargs: Any):
        """Yield a Neo4j session that is closed on exit."""
        sess: Session = self._driver.session(**kwargs)
        try:
            yield sess
        finally:
            sess.close()

    # -- transaction helpers --------------------------------------------------

    def execute_write(
        self,
        work: Callable[[ManagedTransaction], Any],
        **session_kwargs: Any,
    ) -> Any:
        """Run *work* inside a write transaction and return its result."""
        with self.session(**session_kwargs) as sess:
            return sess.execute_write(work)

    def execute_read(
        self,
        work: Callable[[ManagedTransaction], Any],
        **session_kwargs: Any,
    ) -> Any:
        """Run *work* inside a read transaction and return its result."""
        with self.session(**session_kwargs) as sess:
            return sess.execute_read(work)

    # -- lifecycle ------------------------------------------------------------

    def verify_connectivity(self) -> None:
        """Raise if the driver cannot reach Neo4j."""
        self._driver.verify_connectivity()
        logger.info("Neo4j connectivity verified at %s", self._driver._pool.address)

    def close(self) -> None:
        """Release all driver resources."""
        self._driver.close()
        logger.info("Neo4j driver closed")
