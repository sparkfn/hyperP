"""Deterministic schema-inventory coverage for standalone CRM census readiness."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import TypeVar, cast

import pytest
from neo4j import ManagedTransaction, Record
from src.graph.client import Neo4jClient
from src.graph.queries.standalone_crm_census_schema import CENSUS_INDEX_SPECS
from src.graph.standalone_crm_census_schema import (
    assert_standalone_census_indexes,
    show_indexes,
)

_T = TypeVar("_T")


class _Result:
    def __init__(self, records: tuple[Record, ...]) -> None:
        self._records = records

    def __iter__(self) -> Iterator[Record]:
        return iter(self._records)


class _Transaction:
    def __init__(self, records: tuple[Record, ...]) -> None:
        self._records = records
        self.query = ""
        self.params: dict[str, object] = {}

    def run(self, query: str, **params: object) -> _Result:
        self.query = query
        self.params = params
        return _Result(self._records)


class _Client:
    def __init__(self, transaction: _Transaction) -> None:
        self._transaction = transaction

    def execute_read(self, work: Callable[[ManagedTransaction], _T]) -> _T:
        return work(cast(ManagedTransaction, self._transaction))


def _record(name: str, index_type: str, label: object, properties: object) -> Record:
    return cast(
        Record,
        {
            "name": name,
            "type": index_type,
            "entityType": "NODE",
            "labelsOrTypes": label,
            "properties": properties,
        },
    )


def _expected_records() -> tuple[Record, ...]:
    return tuple(
        _record(name, "RANGE", [label], list(properties))
        for name, label, properties in CENSUS_INDEX_SPECS
    )


def test_index_inventory_ignores_builtin_lookup_null_metadata() -> None:
    lookup = _record("index_343aff4e", "LOOKUP", None, None)
    transaction = _Transaction((lookup, *_expected_records()))
    client = cast(Neo4jClient, _Client(transaction))

    indexes = show_indexes(client)

    assert set(indexes) == {name for name, _label, _properties in CENSUS_INDEX_SPECS}
    assert "WHERE name IN $names" in transaction.query
    assert transaction.params["names"] == sorted(
        name for name, _label, _properties in CENSUS_INDEX_SPECS
    )
    assert_standalone_census_indexes(client)


def test_expected_named_index_keeps_wrong_definition_and_ambiguity_fail_closed() -> None:
    records = list(_expected_records())
    name, _label, properties = CENSUS_INDEX_SPECS[0]
    records[0] = _record(name, "TEXT", ["StandaloneCrmCensusAttempt"], list(properties))
    client = cast(Neo4jClient, _Client(_Transaction(tuple(records))))
    with pytest.raises(RuntimeError, match="unexpected definition"):
        assert_standalone_census_indexes(client)

    duplicate = _expected_records()[0]
    duplicate_client = cast(
        Neo4jClient, _Client(_Transaction((duplicate, duplicate, *_expected_records()[1:])))
    )
    with pytest.raises(RuntimeError, match="inventory is ambiguous"):
        show_indexes(duplicate_client)
