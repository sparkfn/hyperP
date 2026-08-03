from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar, cast

from neo4j import ManagedTransaction
from src.graph.client import Neo4jClient
from src.graph.incremental_checkpoints import Neo4jCheckpointRedis

T = TypeVar("T")


class _Result:
    def __init__(self, value: str | None = None) -> None:
        self._value = value

    def single(self) -> dict[str, str] | None:
        return {"value": self._value} if self._value is not None else None


class _Transaction:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **parameters: object) -> _Result:
        self.calls.append((query, parameters))
        key = parameters.get("checkpoint_key")
        return _Result(self.values.get(key) if isinstance(key, str) else None)


class _Client:
    def __init__(self, tx: _Transaction) -> None:
        self.tx = tx
        self.write_count = 0

    def execute_read(self, work: Callable[[ManagedTransaction], T], **_kwargs: object) -> T:
        return work(cast(ManagedTransaction, self.tx))

    def execute_write(self, work: Callable[[ManagedTransaction], T], **_kwargs: object) -> T:
        self.write_count += 1
        return work(cast(ManagedTransaction, self.tx))


class _LegacyClient:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.closed = False

    def get(self, name: str) -> object:
        return self.values.get(name)

    def close(self) -> None:
        self.closed = True


def _store(
    tx: _Transaction,
    *,
    legacy: _LegacyClient | None = None,
    active_ingest_run_id: str | None = None,
) -> tuple[Neo4jCheckpointRedis, _Client]:
    client = _Client(tx)
    store = Neo4jCheckpointRedis(
        cast(Neo4jClient, client),
        "fundbox",
        legacy=legacy,
        active_ingest_run_id=active_ingest_run_id,
    )
    return store, client


def test_durable_value_takes_precedence_over_legacy_redis() -> None:
    tx = _Transaction({"checkpoint": "durable"})
    legacy = _LegacyClient({"checkpoint": "legacy"})
    store, client = _store(tx, legacy=legacy)

    assert store.get("checkpoint") == "durable"
    assert client.write_count == 0


def test_legacy_value_is_migrated_immediately_when_durable_state_is_missing() -> None:
    tx = _Transaction()
    legacy = _LegacyClient({"checkpoint": "legacy"})
    store, client = _store(tx, legacy=legacy)

    assert store.get("checkpoint") == "legacy"
    assert client.write_count == 1
    _, parameters = tx.calls[-1]
    assert parameters["status"] == "migrated"
    assert parameters["value"] == "legacy"


def test_successful_watermark_is_staged_until_ingest_run_finalization() -> None:
    tx = _Transaction()
    store, client = _store(tx)
    key = "profile_unifier:bitrix_openlines:watermark"

    store.set(key, "2026-08-03T01:00:00+00:00")

    assert client.write_count == 0
    store.flush(cast(ManagedTransaction, tx), "run-1", "completed")
    _, parameters = tx.calls[-1]
    assert parameters["checkpoint_key"] == key
    assert parameters["ingest_run_id"] == "run-1"
    assert parameters["status"] == "completed"


def test_staged_operations_survive_transaction_retry_until_explicitly_cleared() -> None:
    tx = _Transaction()
    store, _client = _store(tx)
    store.set("profile_unifier:bitrix_openlines:watermark", "value")

    store.flush(cast(ManagedTransaction, tx), "run-1", "completed")
    first_count = len(tx.calls)
    store.flush(cast(ManagedTransaction, tx), "run-1", "completed")
    assert len(tx.calls) == first_count + 1

    store.clear_staged()
    store.flush(cast(ManagedTransaction, tx), "run-1", "completed")
    assert len(tx.calls) == first_count + 1


def test_resume_page_and_retry_state_are_written_without_waiting_for_completion() -> None:
    tx = _Transaction()
    store, client = _store(tx, active_ingest_run_id="run-1")

    store.set("profile_unifier:whatsadmin-api:whatsapp_chat:eko:s1:page", "page")
    store.set("profile_unifier:whatsadmin-api:whatsapp_chat:eko:s1:retries", "[]")

    assert client.write_count == 2
    assert [parameters["status"] for _, parameters in tx.calls[-2:]] == ["resume", "resume"]
    assert [parameters["ingest_run_id"] for _, parameters in tx.calls[-2:]] == [
        "run-1",
        "run-1",
    ]


def test_closing_store_closes_only_the_legacy_client() -> None:
    tx = _Transaction()
    legacy = _LegacyClient({})
    store, _client = _store(tx, legacy=legacy)

    store.close()
    store.close()

    assert legacy.closed is True
