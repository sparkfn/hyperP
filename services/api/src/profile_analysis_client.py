"""Short-lived synchronous Neo4j client for direct profile-analysis runs.

The application normally uses the async driver in :mod:`src.graph.client`.
Profile-analysis generation is CPU/network-bound and runs in a worker thread so
that an HTTP request can await it without blocking the FastAPI event loop. This
small client keeps the copied domain runtime on the synchronous Neo4j API while
all route-level graph access continues through repository protocols.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TypeVar

from neo4j import Driver, GraphDatabase, ManagedTransaction, Session

from src.config import AppConfig

logger = logging.getLogger(__name__)

# Result type for transaction work functions. Bound to object so callers must
# pick something concrete; the helpers below thread it through generically so
# `execute_write(lambda tx: ...)` is typed by what the lambda returns.
T = TypeVar("T")


class Neo4jClient:
    """Thin wrapper around the official Neo4j Python driver.

    Provides:
    - Explicit session context manager
    - ``execute_write`` / ``execute_read`` helpers
    - Connectivity verification
    - Clean shutdown
    """

    def __init__(self, settings: AppConfig) -> None:
        self._driver: Driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )

    # -- session management ---------------------------------------------------

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Yield a Neo4j session that is closed on exit."""
        sess: Session = self._driver.session()
        try:
            yield sess
        finally:
            sess.close()

    # -- transaction helpers --------------------------------------------------

    def execute_write(
        self,
        work: Callable[[ManagedTransaction], T],
    ) -> T:
        """Run *work* inside a write transaction and return its result."""
        with self.session() as sess:
            return sess.execute_write(work)

    def execute_read(
        self,
        work: Callable[[ManagedTransaction], T],
    ) -> T:
        """Run *work* inside a read transaction and return its result."""
        with self.session() as sess:
            return sess.execute_read(work)

    # -- lifecycle ------------------------------------------------------------

    def verify_connectivity(self) -> None:
        """Raise if the driver cannot reach Neo4j."""
        self._driver.verify_connectivity()
        logger.info("Neo4j connectivity verified")

    def close(self) -> None:
        """Release all driver resources."""
        self._driver.close()
        logger.info("Neo4j driver closed")
