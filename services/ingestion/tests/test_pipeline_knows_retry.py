from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import cast

from neo4j import ManagedTransaction
from neo4j.exceptions import TransientError
from src.pipeline_knows import (
    materialize_knows_from_contacts,
)


class _EmptyResult:
    def __iter__(self) -> object:
        return iter(())


class _Tx:
    def run(self, query: str, **kwargs: object) -> _EmptyResult:
        _ = query, kwargs
        return _EmptyResult()


class _Session:
    def __init__(self, client: _Client) -> None:
        self._client = client

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute_write(self, work: object) -> object:
        self._client.attempts += 1
        if self._client.attempts == 1:
            error = TransientError._hydrate_neo4j(
                code="Neo.TransientError.Transaction.DeadlockDetected",
                message="deadlock",
            )
            raise error
        return work(cast(ManagedTransaction, _Tx()))  # type: ignore[operator]


class _Client:
    def __init__(self) -> None:
        self.attempts = 0

    def session(self) -> _Session:
        return _Session(self)


def test_contact_materialization_retries_deadlocked_batch() -> None:
    client = _Client()

    linked = materialize_knows_from_contacts(
        cast(object, client),
        batch_size=25,
        max_deadlock_retries=2,
        retry_base_delay_seconds=0,
    )

    assert linked == 0
    assert client.attempts == 2


class _ConcurrentSession:
    def __init__(self, client: _ConcurrentClient) -> None:
        self._client = client

    def __enter__(self) -> _ConcurrentSession:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute_write(self, work: object) -> object:
        work_name = getattr(work, "__qualname__", "")
        kind = "contacts" if "contacts" in work_name else "lifecycle"
        with self._client.lock:
            self._client.attempts[kind] += 1
            attempt = self._client.attempts[kind]
        if attempt == 1:
            self._client.overlap.wait(timeout=2)
        if kind == "contacts" and attempt == 1:
            raise TransientError._hydrate_neo4j(
                code="Neo.TransientError.Transaction.DeadlockDetected",
                message="overlapping lifecycle write",
            )
        return work(cast(ManagedTransaction, _Tx()))  # type: ignore[operator]


class _ConcurrentClient:
    def __init__(self) -> None:
        self.attempts = {"contacts": 0, "lifecycle": 0}
        self.lock = threading.Lock()
        self.overlap = threading.Barrier(2)

    def session(self) -> _ConcurrentSession:
        return _ConcurrentSession(self)

    def run_lifecycle_reconciliation(self) -> object:
        def lifecycle_work(_tx: ManagedTransaction) -> str:
            return "reconciled"

        with self.session() as session:
            return session.execute_write(lifecycle_work)


def test_contact_materialization_overlapping_lifecycle_reconciliation_retries_loser() -> None:
    client = _ConcurrentClient()
    with ThreadPoolExecutor(max_workers=2) as executor:
        contacts = executor.submit(
            materialize_knows_from_contacts,
            cast(object, client),
            max_deadlock_retries=2,
            retry_base_delay_seconds=0,
        )
        lifecycle = executor.submit(client.run_lifecycle_reconciliation)

    assert contacts.result() == 0
    assert lifecycle.result() == "reconciled"
    assert client.attempts == {"contacts": 2, "lifecycle": 1}
