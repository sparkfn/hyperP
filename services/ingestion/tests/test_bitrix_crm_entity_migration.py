"""Tests for the restart-safe, scoped Bitrix CRM entity ownership backfill."""

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
        cursor: str | None = None,
        scope_signature: str | None = None,
        entities: frozenset[str] = frozenset({"eko", "speedzone"}),
    ) -> None:
        self._batches = batches
        self._completed = completed
        self._cursor = cursor
        self._scope_signature = scope_signature
        self._entities = entities
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **params: object) -> _Result:
        self.calls.append((query, params))
        if query == crm_migration.START_MIGRATION:
            scope_signature = params["scope_signature"]
            assert isinstance(scope_signature, str)
            if self._scope_signature != scope_signature:
                self._completed = False
                self._cursor = None
                self._scope_signature = scope_signature
            return _Result(
                [
                    {
                        "completed_at": "done" if self._completed else None,
                        "last_source_record_pk": self._cursor,
                    }
                ]
            )
        if query == crm_migration.VALIDATE_MAPPED_ENTITIES:
            keys = params["entity_keys"]
            assert isinstance(keys, list)
            return _Result([{"entity_key": key} for key in keys if key not in self._entities])
        if query == crm_migration.LIST_RECORDS_FOR_BACKFILL:
            return _Result(self._batches.pop(0) if self._batches else [])
        if query == crm_migration.LINK_DEAL_TO_ENTITY:
            return _Result([{}])
        if query == crm_migration.PROPAGATE_ENTITY_TO_CHILDREN:
            return _Result([{"updated": 2}])
        if query == crm_migration.ADVANCE_MIGRATION_CURSOR:
            assert params["scope_signature"] == self._scope_signature
            cursor = params["last_source_record_pk"]
            assert isinstance(cursor, str)
            self._cursor = cursor
            return _Result([{"last_source_record_pk": cursor}])
        if query == crm_migration.COMPLETE_MIGRATION:
            assert params["scope_signature"] == self._scope_signature
            self._completed = True
            self._cursor = None
            return _Result([{"completed_at": "done"}])
        raise AssertionError(f"unexpected query: {query}")


class _Client:
    def __init__(self, tx: _Tx) -> None:
        self.tx = tx

    def execute_write(self, work: object, **_kwargs: object) -> object:
        return cast("object", work)(self.tx)  # type: ignore[operator]

    def execute_read(self, work: object, **_kwargs: object) -> object:
        return cast("object", work)(self.tx)  # type: ignore[operator]


def _deal(source_record_pk: str, source_record_id: str, category_id: object) -> dict[str, object]:
    return {
        "source_record_pk": source_record_pk,
        "source_record_id": source_record_id,
        "raw_payload": f'{{"category_id": {category_id!r}}}'.replace("'", '"'),
    }


def test_backfill_repairs_included_deals_and_skips_excluded_records() -> None:
    tx = _Tx(
        [
            [
                _deal("deal-eko", "bitrix-crm-deal-701", "1"),
                _deal("deal-excluded", "bitrix-crm-deal-703", "99"),
                _deal("deal-speedzone", "bitrix-crm-deal-702", "2"),
            ]
        ]
    )

    updated = crm_migration.migrate_bitrix_crm_entities(
        cast(Neo4jClient, _Client(tx)),
        {"1": "eko", "2": "speedzone"},
        ["1", "2"],
    )

    assert updated == 2
    deal_updates = [
        params for query, params in tx.calls if query == crm_migration.LINK_DEAL_TO_ENTITY
    ]
    assert deal_updates == [
        {"source_record_pk": "deal-eko", "entity_key": "eko"},
        {"source_record_pk": "deal-speedzone", "entity_key": "speedzone"},
    ]
    cursor_updates = [
        params for query, params in tx.calls if query == crm_migration.ADVANCE_MIGRATION_CURSOR
    ]
    assert cursor_updates == [
        {
            "migration_key": crm_migration.MIGRATION_KEY,
            "scope_signature": crm_migration._scope_signature(
                ["1", "2"], {"1": "eko", "2": "speedzone"}
            ),
            "last_source_record_pk": "deal-speedzone",
        }
    ]
    assert tx.calls[-1][0] == crm_migration.COMPLETE_MIGRATION


def test_candidate_query_scans_stably_after_a_persisted_cursor() -> None:
    query = crm_migration.LIST_RECORDS_FOR_BACKFILL

    assert "record.source_record_pk > $after_source_record_pk" in query
    assert "ORDER BY record.source_record_pk" in query
    assert "LIMIT $batch_size" in query
    assert "OWNED_BY" not in query


def test_backfill_resumes_from_an_incomplete_marker_cursor() -> None:
    mapping = {"2": "speedzone"}
    scope_signature = crm_migration._scope_signature(["2"], mapping)
    tx = _Tx(
        [[_deal("deal-2", "bitrix-crm-deal-702", "2")]],
        cursor="deal-1",
        scope_signature=scope_signature,
    )

    updated = crm_migration.migrate_bitrix_crm_entities(
        cast(Neo4jClient, _Client(tx)), mapping, ["2"]
    )

    assert updated == 1
    list_params = [
        params for query, params in tx.calls if query == crm_migration.LIST_RECORDS_FOR_BACKFILL
    ]
    assert list_params[0]["after_source_record_pk"] == "deal-1"
    assert tx._completed is True


def test_backfill_is_a_noop_after_completion() -> None:
    mapping = {"2": "speedzone"}
    tx = _Tx(
        [],
        completed=True,
        scope_signature=crm_migration._scope_signature(["2"], mapping),
    )

    assert (
        crm_migration.migrate_bitrix_crm_entities(
            cast(Neo4jClient, _Client(tx)), mapping, ["2"]
        )
        == 0
    )
    assert [query for query, _params in tx.calls] == [
        crm_migration.VALIDATE_MAPPED_ENTITIES,
        crm_migration.START_MIGRATION,
    ]


def test_backfill_does_not_start_until_at_least_one_category_is_included() -> None:
    tx = _Tx([])

    assert crm_migration.migrate_bitrix_crm_entities(cast(Neo4jClient, _Client(tx)), {}, []) == 0
    assert tx.calls == []


def test_backfill_restarts_when_the_included_mapping_scope_changes() -> None:
    old_mapping = {"2": "speedzone"}
    tx = _Tx(
        [[_deal("deal-2", "bitrix-crm-deal-702", "2")]],
        completed=True,
        scope_signature=crm_migration._scope_signature(["2"], old_mapping),
    )

    updated = crm_migration.migrate_bitrix_crm_entities(
        cast(Neo4jClient, _Client(tx)), {"2": "eko"}, ["2"]
    )

    assert updated == 1
    assert tx._completed is True
    assert tx._scope_signature == crm_migration._scope_signature(["2"], {"2": "eko"})


def test_backfill_rejects_an_included_category_without_a_mapping() -> None:
    tx = _Tx([])

    with pytest.raises(
        ValueError,
        match="Included Bitrix CRM categories have no entity mapping: 2",
    ):
        crm_migration.migrate_bitrix_crm_entities(cast(Neo4jClient, _Client(tx)), {}, ["2"])

    assert crm_migration.START_MIGRATION not in [query for query, _params in tx.calls]


def test_backfill_rejects_unknown_mapped_entities_before_scanning() -> None:
    tx = _Tx([], entities=frozenset({"eko"}))

    with pytest.raises(RuntimeError, match=r"unknown entities: speedzone \(categories 2\)"):
        crm_migration.migrate_bitrix_crm_entities(
            cast(Neo4jClient, _Client(tx)), {"2": "speedzone"}, ["2"]
        )

    assert crm_migration.LIST_RECORDS_FOR_BACKFILL not in [query for query, _params in tx.calls]
