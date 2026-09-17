"""Neo4j driver wrapper with managed and bounded no-retry transaction modes."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar, cast

from neo4j import GraphDatabase, ManagedTransaction, Session, unit_of_work

from src.config import Settings

logger = logging.getLogger(__name__)
T = TypeVar("T")


class Neo4jClient:
    """Driver wrapper; bounded clients use explicit transactions without retries."""

    def __init__(
        self,
        settings: Settings,
        *,
        bounded_timeout_seconds: float | None = None,
    ) -> None:
        self._bounded_timeout_seconds = bounded_timeout_seconds
        if bounded_timeout_seconds is None:
            self._driver = GraphDatabase.driver(
                settings.neo4j_uri,
                auth=(settings.neo4j_user, settings.neo4j_password),
            )
            return
        if bounded_timeout_seconds <= 0:
            raise ValueError("bounded timeout must be positive")
        self._driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            connection_timeout=bounded_timeout_seconds,
            connection_acquisition_timeout=bounded_timeout_seconds,
            max_transaction_retry_time=bounded_timeout_seconds,
        )

    @contextmanager
    def session(self, **kwargs: Any) -> Iterator[Session]:
        sess: Session = self._driver.session(**kwargs)
        try:
            yield sess
        finally:
            sess.close()

    def execute_write(
        self,
        work: Callable[[ManagedTransaction], T],
        *,
        transaction_timeout_seconds: float | None = None,
        **session_kwargs: Any,
    ) -> T:
        if self._bounded_timeout_seconds is not None:
            timeout = transaction_timeout_seconds or self._bounded_timeout_seconds
            if timeout <= 0:
                raise ValueError("transaction timeout must be positive")
            with self.session(**session_kwargs) as sess:
                with sess.begin_transaction(timeout=timeout) as tx:
                    result = work(cast(ManagedTransaction, tx))
                    tx.commit()
                    return result
        transaction_work = work
        if transaction_timeout_seconds is not None:
            if transaction_timeout_seconds <= 0:
                raise ValueError("transaction timeout must be positive")
            transaction_work = unit_of_work(timeout=transaction_timeout_seconds)(work)
        with self.session(**session_kwargs) as sess:
            return sess.execute_write(transaction_work)

    def execute_read(
        self,
        work: Callable[[ManagedTransaction], T],
        **session_kwargs: Any,
    ) -> T:
        with self.session(**session_kwargs) as sess:
            if self._bounded_timeout_seconds is None:
                return sess.execute_read(work)
            with sess.begin_transaction(timeout=self._bounded_timeout_seconds) as tx:
                result = work(cast(ManagedTransaction, tx))
                tx.commit()
                return result

    def verify_connectivity(self) -> None:
        self._driver.verify_connectivity()
        logger.info("Neo4j connectivity verified at %s", self._driver._pool.address)

    def close(self) -> None:
        self._driver.close()
        logger.info("Neo4j driver closed")
