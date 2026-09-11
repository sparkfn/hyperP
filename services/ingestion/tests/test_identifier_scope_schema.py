"""Unit coverage for the fail-closed scoped-Identifier schema transition."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast

import pytest
from src.graph.client import Neo4jClient
from src.graph.identifier_scope_schema import (
    BRIDGE_INDEX_NAME,
    BRIDGE_PROPERTIES,
    IDENTIFIER_SCOPE_CONSTRAINT_NAME,
    IDENTIFIER_SCOPE_PROPERTIES,
    LEGACY_INDEX_NAME,
    apply_identifier_scope_schema_transition,
)
from src.graph.queries.identifier_scope_schema import (
    CREATE_IDENTIFIER_IDENTITY_SCOPE_UNIQUE,
    CREATE_IDENTIFIER_SCOPE_BRIDGE_INDEX,
    DROP_IDENTIFIER_SCOPE_BRIDGE_INDEX,
    DROP_LEGACY_IDENTIFIER_SCOPE_INDEX,
    SHOW_IDENTIFIER_SCOPE_INDEXES,
)


class _Result:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._rows)

    def consume(self) -> None:
        return None


class _Transaction:
    def __init__(self, client: _Client) -> None:
        self._client = client

    def run(self, query: str) -> _Result:
        if query.lstrip().startswith("SHOW INDEXES"):
            assert query == SHOW_IDENTIFIER_SCOPE_INDEXES
            assert "WHERE labelsOrTypes IS NOT NULL AND properties IS NOT NULL" in query
            return _Result(list(self._client.indexes.values()))
        if query.lstrip().startswith("SHOW CONSTRAINTS"):
            return _Result(list(self._client.constraints.values()))
        raise AssertionError(f"unexpected transaction query: {query}")


class _Session:
    def __init__(self, client: _Client) -> None:
        self._client = client

    def run(self, query: str) -> _Result:
        self._client.ddl.append(query)
        if query == CREATE_IDENTIFIER_SCOPE_BRIDGE_INDEX:
            self._client.indexes[BRIDGE_INDEX_NAME] = _index(
                BRIDGE_INDEX_NAME,
                BRIDGE_PROPERTIES,
            )
        elif query == DROP_LEGACY_IDENTIFIER_SCOPE_INDEX:
            self._client.indexes.pop(LEGACY_INDEX_NAME, None)
        elif query == CREATE_IDENTIFIER_IDENTITY_SCOPE_UNIQUE:
            if self._client.fail_constraint_creation:
                raise RuntimeError("duplicate tuple")
            self._client.constraints[IDENTIFIER_SCOPE_CONSTRAINT_NAME] = _constraint(
                IDENTIFIER_SCOPE_CONSTRAINT_NAME,
                IDENTIFIER_SCOPE_CONSTRAINT_NAME,
            )
            self._client.indexes[IDENTIFIER_SCOPE_CONSTRAINT_NAME] = _index(
                IDENTIFIER_SCOPE_CONSTRAINT_NAME,
                IDENTIFIER_SCOPE_PROPERTIES,
                owning_constraint=IDENTIFIER_SCOPE_CONSTRAINT_NAME,
            )
        elif query == DROP_IDENTIFIER_SCOPE_BRIDGE_INDEX:
            self._client.indexes.pop(BRIDGE_INDEX_NAME, None)
        else:
            raise AssertionError(f"unexpected schema statement: {query}")
        return _Result([])


class _Client:
    def __init__(self) -> None:
        self.indexes: dict[str, dict[str, object]] = {}
        self.constraints: dict[str, dict[str, object]] = {}
        self.ddl: list[str] = []
        self.fail_constraint_creation = False

    def execute_read(self, work: object) -> object:
        return cast("object", work)(_Transaction(self))  # type: ignore[operator]

    @contextmanager
    def session(self) -> Iterator[_Session]:
        yield _Session(self)


def _index(
    name: str,
    properties: tuple[str, ...],
    *,
    owning_constraint: str | None = None,
    index_type: str = "RANGE",
) -> dict[str, object]:
    return {
        "name": name,
        "type": index_type,
        "entityType": "NODE",
        "labelsOrTypes": ["Identifier"],
        "properties": list(properties),
        "state": "ONLINE",
        "owningConstraint": owning_constraint,
    }


def _constraint(name: str, owned_index: str) -> dict[str, object]:
    return {
        "name": name,
        "type": "UNIQUENESS",
        "entityType": "NODE",
        "labelsOrTypes": ["Identifier"],
        "properties": list(IDENTIFIER_SCOPE_PROPERTIES),
        "ownedIndex": owned_index,
    }


def test_index_inventory_excludes_default_lookup_rows_with_null_schema_fields() -> None:
    assert "WHERE labelsOrTypes IS NOT NULL AND properties IS NOT NULL" in (
        SHOW_IDENTIFIER_SCOPE_INDEXES
    )
    assert "owningConstraint" in SHOW_IDENTIFIER_SCOPE_INDEXES


def test_already_correct_schema_is_a_ddl_noop() -> None:
    client = _Client()
    client.constraints[IDENTIFIER_SCOPE_CONSTRAINT_NAME] = _constraint(
        IDENTIFIER_SCOPE_CONSTRAINT_NAME,
        IDENTIFIER_SCOPE_CONSTRAINT_NAME,
    )
    client.indexes[IDENTIFIER_SCOPE_CONSTRAINT_NAME] = _index(
        IDENTIFIER_SCOPE_CONSTRAINT_NAME,
        IDENTIFIER_SCOPE_PROPERTIES,
        owning_constraint=IDENTIFIER_SCOPE_CONSTRAINT_NAME,
    )

    assert apply_identifier_scope_schema_transition(cast(Neo4jClient, client)) == 1
    assert client.ddl == []


def test_legacy_transition_keeps_bridge_until_constraint_is_ready() -> None:
    client = _Client()
    client.indexes[LEGACY_INDEX_NAME] = _index(LEGACY_INDEX_NAME, IDENTIFIER_SCOPE_PROPERTIES)

    assert apply_identifier_scope_schema_transition(cast(Neo4jClient, client)) == 1

    assert client.ddl == [
        CREATE_IDENTIFIER_SCOPE_BRIDGE_INDEX,
        DROP_LEGACY_IDENTIFIER_SCOPE_INDEX,
        CREATE_IDENTIFIER_IDENTITY_SCOPE_UNIQUE,
        DROP_IDENTIFIER_SCOPE_BRIDGE_INDEX,
    ]
    assert LEGACY_INDEX_NAME not in client.indexes
    assert BRIDGE_INDEX_NAME not in client.indexes
    assert IDENTIFIER_SCOPE_CONSTRAINT_NAME in client.constraints


def test_wrong_named_legacy_definition_fails_before_schema_mutation() -> None:
    client = _Client()
    client.indexes[LEGACY_INDEX_NAME] = _index(
        LEGACY_INDEX_NAME,
        IDENTIFIER_SCOPE_PROPERTIES,
        index_type="TEXT",
    )

    with pytest.raises(RuntimeError, match="legacy scoped Identifier index has an unexpected"):
        apply_identifier_scope_schema_transition(cast(Neo4jClient, client))

    assert client.ddl == []


def test_wrong_named_constraint_definition_fails_before_schema_mutation() -> None:
    client = _Client()
    client.constraints[IDENTIFIER_SCOPE_CONSTRAINT_NAME] = {
        **_constraint(IDENTIFIER_SCOPE_CONSTRAINT_NAME, IDENTIFIER_SCOPE_CONSTRAINT_NAME),
        "type": "EXISTS",
    }

    with pytest.raises(RuntimeError, match="uniqueness constraint has an unexpected"):
        apply_identifier_scope_schema_transition(cast(Neo4jClient, client))

    assert client.ddl == []


def test_constraint_failure_retains_bridge_after_legacy_removal() -> None:
    client = _Client()
    client.indexes[LEGACY_INDEX_NAME] = _index(LEGACY_INDEX_NAME, IDENTIFIER_SCOPE_PROPERTIES)
    client.fail_constraint_creation = True

    with pytest.raises(RuntimeError, match="duplicate tuple"):
        apply_identifier_scope_schema_transition(cast(Neo4jClient, client))

    assert client.ddl == [
        CREATE_IDENTIFIER_SCOPE_BRIDGE_INDEX,
        DROP_LEGACY_IDENTIFIER_SCOPE_INDEX,
        CREATE_IDENTIFIER_IDENTITY_SCOPE_UNIQUE,
    ]
    assert LEGACY_INDEX_NAME not in client.indexes
    assert BRIDGE_INDEX_NAME in client.indexes


@pytest.mark.parametrize("state", ("bridge_only", "constraint_bridge_cleanup"))
def test_supported_restart_states_converge(state: str) -> None:
    client = _Client()
    client.indexes[BRIDGE_INDEX_NAME] = _index(BRIDGE_INDEX_NAME, BRIDGE_PROPERTIES)
    if state == "constraint_bridge_cleanup":
        client.constraints[IDENTIFIER_SCOPE_CONSTRAINT_NAME] = _constraint(
            IDENTIFIER_SCOPE_CONSTRAINT_NAME,
            IDENTIFIER_SCOPE_CONSTRAINT_NAME,
        )
        client.indexes[IDENTIFIER_SCOPE_CONSTRAINT_NAME] = _index(
            IDENTIFIER_SCOPE_CONSTRAINT_NAME,
            IDENTIFIER_SCOPE_PROPERTIES,
            owning_constraint=IDENTIFIER_SCOPE_CONSTRAINT_NAME,
        )

    assert apply_identifier_scope_schema_transition(cast(Neo4jClient, client)) == 1
    assert BRIDGE_INDEX_NAME not in client.indexes
    assert IDENTIFIER_SCOPE_CONSTRAINT_NAME in client.constraints
