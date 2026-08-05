"""Tests for the idempotent Bitrix CRM entity ownership backfill."""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import pytest
from src.graph import bitrix_crm_entity_migration as crm_migration
from src.graph.client import Neo4jClient


class _Result:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._rows)

    def single(self) -> dict[str, object] | None:
        return self._rows[0] if self._rows else None


class _Tx:
    def __init__(
        self,
        batches: list[list[dict[str, object]]],
        *,
        completed: bool = False,
        entities: frozenset[str] = frozenset({"eko", "speedzone"}),
    ) -> None:
        self._batches = batches
        self._completed = completed
        self._entities = entities
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **params: object) -> _Result:
        self.calls.append((query, params))
        if query == crm_migration.START_MIGRATION:
            return _Result([{"completed_at": "done" if self._completed else None}])
        if query == crm_migration.LIST_RECORDS_FOR_BACKFILL:
            return _Result(self._batches.pop(0) if self._batches else [])
        if query == crm_migration.LINK_DEAL_TO_ENTITY:
            entity_key = params["entity_key"]
            return _Result([{}] if entity_key in self._entities else [])
        if query == crm_migration.PROPAGATE_ENTITY_TO_CHILDREN:
            return _Result([{"updated": 2}])
        if query == crm_migration.COMPLETE_MIGRATION:
            self._completed = True
            return _Result([{"completed_at": "done"}])
        raise AssertionError(f"unexpected query: {query}")


class _Client:
    def __init__(self, tx: _Tx) -> None:
        self.tx = tx

    def execute_write(self, work: object, **_kwargs: object) -> object:
        return cast("object", work)(self.tx)  # type: ignore[operator]


def _deal(source_record_pk: str, source_record_id: str, category_id: object) -> dict[str, object]:
    return {
        "source_record_pk": source_record_pk,
        "source_record_id": source_record_id,
        "raw_payload": f'{{"category_id": {category_id!r}}}'.replace("'", '"'),
    }


def test_backfill_repairs_deals_and_existing_history_call_children_in_place() -> None:
    tx = _Tx(
        [
            [
                _deal("deal-eko", "bitrix-crm-deal-701", "1"),
                _deal("deal-speedzone", "bitrix-crm-deal-702", "2"),
            ]
        ]
    )

    updated = crm_migration.migrate_bitrix_crm_entities(
        cast(Neo4jClient, _Client(tx)),
        {"1": "eko", "2": "speedzone"},
    )

    assert updated == 2
    deal_updates = [
        params for query, params in tx.calls if query == crm_migration.LINK_DEAL_TO_ENTITY
    ]
    assert deal_updates == [
        {"source_record_pk": "deal-eko", "entity_key": "eko"},
        {"source_record_pk": "deal-speedzone", "entity_key": "speedzone"},
    ]
    child_updates = [
        params for query, params in tx.calls if query == crm_migration.PROPAGATE_ENTITY_TO_CHILDREN
    ]
    assert child_updates == [
        {"deal_source_record_pk": "deal-eko", "entity_key": "eko"},
        {"deal_source_record_pk": "deal-speedzone", "entity_key": "speedzone"},
    ]
    assert tx.calls[-1][0] == crm_migration.COMPLETE_MIGRATION


def test_candidate_query_repairs_children_even_when_the_deal_is_already_owned() -> None:
    query = crm_migration.LIST_RECORDS_FOR_BACKFILL

    assert "child.record_type IN ['crm_history', 'call']" in query
    assert "inconsistent_child_ownership" in query
    assert "any(inconsistent IN inconsistent_child_ownership WHERE inconsistent)" in query
    assert "child.entity_key <> record.entity_key" in query
    assert "collect(owner.entity_key)" in query
    assert "collect(DISTINCT child_owner_relationship)" in query
    assert "size(child_owner_relationships) <> 1" in query
    assert "collect(DISTINCT owner.entity_key)" not in query
    assert "collect(DISTINCT stale_owner)" in crm_migration.PROPAGATE_ENTITY_TO_CHILDREN


def test_backfill_is_a_noop_after_completion() -> None:
    tx = _Tx([], completed=True)

    assert crm_migration.migrate_bitrix_crm_entities(cast(Neo4jClient, _Client(tx)), {}) == 0
    assert [query for query, _params in tx.calls] == [crm_migration.START_MIGRATION]


@pytest.mark.parametrize(
    "record",
    [
        _deal("deal-1", "bitrix-crm-deal-701", None),
        _deal("deal-1", "bitrix-crm-deal-701", "non-numeric"),
    ],
)
def test_backfill_refuses_to_complete_when_a_deal_category_is_unusable(
    record: dict[str, object],
) -> None:
    tx = _Tx([[record]])

    with pytest.raises(RuntimeError, match="no usable category ID"):
        crm_migration.migrate_bitrix_crm_entities(
            cast(Neo4jClient, _Client(tx)), {"2": "speedzone"}
        )

    assert crm_migration.COMPLETE_MIGRATION not in [query for query, _params in tx.calls]


def test_backfill_refuses_to_complete_when_a_category_is_unmapped() -> None:
    tx = _Tx([[_deal("deal-1", "bitrix-crm-deal-701", "2")]])

    with pytest.raises(RuntimeError, match="has no entity mapping"):
        crm_migration.migrate_bitrix_crm_entities(cast(Neo4jClient, _Client(tx)), {"1": "eko"})

    assert crm_migration.COMPLETE_MIGRATION not in [query for query, _params in tx.calls]


def test_backfill_refuses_to_complete_when_a_mapping_targets_an_unknown_entity() -> None:
    tx = _Tx([[_deal("deal-1", "bitrix-crm-deal-701", "2")]], entities=frozenset({"eko"}))

    with pytest.raises(RuntimeError, match="maps to unknown entity"):
        crm_migration.migrate_bitrix_crm_entities(
            cast(Neo4jClient, _Client(tx)),
            {"2": "speedzone"},
        )

    assert crm_migration.COMPLETE_MIGRATION not in [query for query, _params in tx.calls]
