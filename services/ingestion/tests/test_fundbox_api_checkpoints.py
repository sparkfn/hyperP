from __future__ import annotations

from typing import cast

from redis import Redis
from src.connectors.fundbox_api.checkpoints import (
    load_source_ids,
    load_watermark,
    save_reconciliation_state,
    save_watermark,
)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def pipeline(self, *, transaction: bool) -> FakeRedis:
        assert transaction is True
        return self

    def execute(self) -> list[object]:
        return []


def test_checkpoint_round_trip_applies_overlap() -> None:
    redis = FakeRedis()
    store = cast(Redis, redis)

    save_watermark(store, "fundbox_consumer_backend", "2026-07-17T01:00:00Z")

    assert load_watermark(store, "fundbox_consumer_backend", 300) == ("2026-07-17T00:55:00+00:00")


def test_reconciliation_state_round_trip_sorts_source_ids() -> None:
    redis = FakeRedis()
    store = cast(Redis, redis)

    save_reconciliation_state(
        store,
        "fundbox_consumer_backend:sales",
        {9, 2, 5},
        "2026-07-17T01:00:00Z",
    )

    assert load_source_ids(store, "fundbox_consumer_backend:sales") == {2, 5, 9}
    assert (
        redis.values["profile_unifier:fundbox_api:source_ids:fundbox_consumer_backend:sales"]
        == "[2,5,9]"
    )


def test_missing_source_id_snapshot_is_distinct_from_empty_snapshot() -> None:
    redis = FakeRedis()
    store = cast(Redis, redis)

    assert load_source_ids(store, "fundbox_consumer_backend:contacts") is None
    save_reconciliation_state(store, "fundbox_consumer_backend:contacts", set(), None)
    assert load_source_ids(store, "fundbox_consumer_backend:contacts") == set()
