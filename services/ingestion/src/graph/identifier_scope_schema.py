"""Fail-closed replacement of the legacy scoped-Identifier range index."""

from __future__ import annotations

import time
from dataclasses import dataclass

from neo4j import ManagedTransaction, Record

from src.graph.client import Neo4jClient
from src.graph.queries.identifier_scope_schema import (
    CREATE_IDENTIFIER_IDENTITY_SCOPE_UNIQUE,
    CREATE_IDENTIFIER_SCOPE_BRIDGE_INDEX,
    DROP_IDENTIFIER_SCOPE_BRIDGE_INDEX,
    DROP_LEGACY_IDENTIFIER_SCOPE_INDEX,
    SHOW_IDENTIFIER_SCOPE_CONSTRAINTS,
    SHOW_IDENTIFIER_SCOPE_INDEXES,
)

IDENTIFIER_LABEL = "Identifier"
LEGACY_INDEX_NAME = "idx_identifier_type_scope_norm"
BRIDGE_INDEX_NAME = "idx_identifier_scope_norm_type_bridge"
IDENTIFIER_SCOPE_CONSTRAINT_NAME = "identifier_identity_scope_unique"
IDENTIFIER_SCOPE_PROPERTIES = ("identifier_type", "identifier_scope", "normalized_value")
BRIDGE_PROPERTIES = ("identifier_scope", "normalized_value", "identifier_type")
_ONLINE = "ONLINE"
_SCHEMA_WAIT_SECONDS = 120.0
_SCHEMA_POLL_SECONDS = 0.25


@dataclass(frozen=True)
class IdentifierScopeIndex:
    """One validated ``SHOW INDEXES`` row relevant to this transition."""

    name: str
    index_type: str
    entity_type: str
    labels_or_types: tuple[str, ...] | None
    properties: tuple[str, ...] | None
    state: str
    owning_constraint: str | None


@dataclass(frozen=True)
class IdentifierScopeConstraint:
    """One validated ``SHOW CONSTRAINTS`` row relevant to this transition."""

    name: str
    constraint_type: str
    entity_type: str
    labels_or_types: tuple[str, ...]
    properties: tuple[str, ...]
    owned_index: str | None


@dataclass(frozen=True)
class IdentifierScopeSchemaInventory:
    """The full schema inventory needed to choose a safe transition state."""

    indexes: dict[str, IdentifierScopeIndex]
    constraints: dict[str, IdentifierScopeConstraint]


def apply_identifier_scope_schema_transition(client: Neo4jClient) -> int:
    """Converge the scoped Identifier tuple onto its uniqueness constraint.

    The bridge stays in place after any replacement failure. A retry observes
    and resumes a supported state instead of reporting initialization success
    without tuple lookup coverage or uniqueness.
    """
    inventory = show_identifier_scope_schema(client)
    _assert_transition_inventory(inventory)

    constraint = inventory.constraints.get(IDENTIFIER_SCOPE_CONSTRAINT_NAME)
    if constraint is not None:
        _assert_constraint_ready(inventory, constraint)
        _drop_bridge_if_present(client, inventory)
        _assert_final_state(show_identifier_scope_schema(client))
        return 1

    inventory = _ensure_online_bridge(client, inventory)
    if LEGACY_INDEX_NAME in inventory.indexes:
        _execute_schema_statement(client, DROP_LEGACY_IDENTIFIER_SCOPE_INDEX)
        inventory = show_identifier_scope_schema(client)
        _assert_transition_inventory(inventory)
        if LEGACY_INDEX_NAME in inventory.indexes:
            raise RuntimeError("legacy scoped Identifier index was not removed")

    _execute_schema_statement(client, CREATE_IDENTIFIER_IDENTITY_SCOPE_UNIQUE)
    inventory = show_identifier_scope_schema(client)
    _assert_transition_inventory(inventory)
    constraint = inventory.constraints.get(IDENTIFIER_SCOPE_CONSTRAINT_NAME)
    if constraint is None:
        raise RuntimeError("scoped Identifier uniqueness constraint was not created")
    _assert_constraint_ready(inventory, constraint)

    _drop_bridge_if_present(client, inventory)
    _assert_final_state(show_identifier_scope_schema(client))
    return 1


def show_identifier_scope_schema(client: Neo4jClient) -> IdentifierScopeSchemaInventory:
    """Read and validate the exact schema rows used by the transition."""

    def _read_indexes(tx: ManagedTransaction) -> dict[str, IdentifierScopeIndex]:
        indexes: dict[str, IdentifierScopeIndex] = {}
        for record in tx.run(SHOW_IDENTIFIER_SCOPE_INDEXES):
            definition = _index_definition(record)
            if definition.name in indexes:
                raise RuntimeError("scoped Identifier index inventory is ambiguous")
            indexes[definition.name] = definition
        return indexes

    def _read_constraints(tx: ManagedTransaction) -> dict[str, IdentifierScopeConstraint]:
        constraints: dict[str, IdentifierScopeConstraint] = {}
        for record in tx.run(SHOW_IDENTIFIER_SCOPE_CONSTRAINTS):
            definition = _constraint_definition(record)
            if definition.name in constraints:
                raise RuntimeError("scoped Identifier constraint inventory is ambiguous")
            constraints[definition.name] = definition
        return constraints

    return IdentifierScopeSchemaInventory(
        indexes=client.execute_read(_read_indexes),
        constraints=client.execute_read(_read_constraints),
    )


def _ensure_online_bridge(
    client: Neo4jClient,
    inventory: IdentifierScopeSchemaInventory,
) -> IdentifierScopeSchemaInventory:
    bridge = inventory.indexes.get(BRIDGE_INDEX_NAME)
    if bridge is None:
        _execute_schema_statement(client, CREATE_IDENTIFIER_SCOPE_BRIDGE_INDEX)
    elif not _is_bridge_index(bridge):
        raise RuntimeError("scoped Identifier bridge index has an unexpected definition")

    _await_index_online(client, BRIDGE_INDEX_NAME)
    refreshed = show_identifier_scope_schema(client)
    _assert_transition_inventory(refreshed)
    bridge = refreshed.indexes.get(BRIDGE_INDEX_NAME)
    if bridge is None or not _is_bridge_index(bridge) or bridge.state != _ONLINE:
        raise RuntimeError("scoped Identifier bridge index is not ONLINE")
    return refreshed


def _drop_bridge_if_present(
    client: Neo4jClient,
    inventory: IdentifierScopeSchemaInventory,
) -> None:
    bridge = inventory.indexes.get(BRIDGE_INDEX_NAME)
    if bridge is None:
        return
    if not _is_bridge_index(bridge):
        raise RuntimeError("scoped Identifier bridge index has an unexpected definition")
    _execute_schema_statement(client, DROP_IDENTIFIER_SCOPE_BRIDGE_INDEX)


def _assert_final_state(inventory: IdentifierScopeSchemaInventory) -> None:
    _assert_transition_inventory(inventory)
    if LEGACY_INDEX_NAME in inventory.indexes:
        raise RuntimeError("legacy scoped Identifier index remains after transition")
    if BRIDGE_INDEX_NAME in inventory.indexes:
        raise RuntimeError("scoped Identifier bridge index remains after transition")
    constraint = inventory.constraints.get(IDENTIFIER_SCOPE_CONSTRAINT_NAME)
    if constraint is None:
        raise RuntimeError("scoped Identifier uniqueness constraint is absent after transition")
    _assert_constraint_ready(inventory, constraint)


def _assert_transition_inventory(inventory: IdentifierScopeSchemaInventory) -> None:
    constraint = inventory.constraints.get(IDENTIFIER_SCOPE_CONSTRAINT_NAME)
    if constraint is not None and not _is_identifier_scope_constraint(constraint):
        raise RuntimeError("scoped Identifier uniqueness constraint has an unexpected definition")
    _assert_reserved_index_names(inventory.indexes, constraint)

    for definition in inventory.constraints.values():
        if definition.name in {LEGACY_INDEX_NAME, BRIDGE_INDEX_NAME}:
            raise RuntimeError("reserved scoped Identifier index name is occupied by a constraint")
        is_canonical = _is_identifier_scope_identity(
            definition.labels_or_types,
            definition.properties,
        )
        is_bridge = _is_bridge_identity(definition.labels_or_types, definition.properties)
        if is_canonical and definition.name != IDENTIFIER_SCOPE_CONSTRAINT_NAME:
            raise RuntimeError("unrecognized constraint owns the scoped Identifier identity")
        if is_bridge:
            raise RuntimeError("unrecognized constraint owns the scoped Identifier bridge identity")

    expected_constraint = inventory.constraints.get(IDENTIFIER_SCOPE_CONSTRAINT_NAME)
    for index_definition in inventory.indexes.values():
        if not _is_identifier_scope_identity(
            index_definition.labels_or_types,
            index_definition.properties,
        ):
            continue
        if index_definition.name == LEGACY_INDEX_NAME:
            continue
        if (
            index_definition.owning_constraint == IDENTIFIER_SCOPE_CONSTRAINT_NAME
            and expected_constraint is not None
            and expected_constraint.owned_index == index_definition.name
        ):
            continue
        raise RuntimeError("unrecognized index owns the scoped Identifier identity")

    for index_definition in inventory.indexes.values():
        if _is_bridge_identity(index_definition.labels_or_types, index_definition.properties):
            if index_definition.name != BRIDGE_INDEX_NAME:
                raise RuntimeError("unrecognized index owns the scoped Identifier bridge identity")


def _assert_reserved_index_names(
    indexes: dict[str, IdentifierScopeIndex],
    expected_constraint: IdentifierScopeConstraint | None,
) -> None:
    legacy = indexes.get(LEGACY_INDEX_NAME)
    if legacy is not None and not _is_legacy_index(legacy):
        raise RuntimeError("legacy scoped Identifier index has an unexpected definition")

    bridge = indexes.get(BRIDGE_INDEX_NAME)
    if bridge is not None and not _is_bridge_index(bridge):
        raise RuntimeError("scoped Identifier bridge index has an unexpected definition")

    constraint_index = indexes.get(IDENTIFIER_SCOPE_CONSTRAINT_NAME)
    if constraint_index is None:
        return
    if (
        expected_constraint is None
        or expected_constraint.owned_index != constraint_index.name
        or not _is_constraint_backing_index(constraint_index)
    ):
        raise RuntimeError("reserved scoped Identifier constraint name is occupied by an index")


def _is_constraint_backing_index(definition: IdentifierScopeIndex) -> bool:
    return (
        definition.index_type == "RANGE"
        and definition.entity_type == "NODE"
        and _is_identifier_scope_identity(definition.labels_or_types, definition.properties)
        and definition.owning_constraint == IDENTIFIER_SCOPE_CONSTRAINT_NAME
    )


def _assert_constraint_ready(
    inventory: IdentifierScopeSchemaInventory,
    constraint: IdentifierScopeConstraint,
) -> None:
    if not _is_identifier_scope_constraint(constraint):
        raise RuntimeError("scoped Identifier uniqueness constraint has an unexpected definition")
    if constraint.owned_index is None:
        raise RuntimeError("scoped Identifier uniqueness constraint has no backing index")
    index = inventory.indexes.get(constraint.owned_index)
    if index is None:
        raise RuntimeError("scoped Identifier uniqueness backing index is absent")
    if (
        not _is_identifier_scope_identity(index.labels_or_types, index.properties)
        or index.owning_constraint != IDENTIFIER_SCOPE_CONSTRAINT_NAME
        or index.index_type != "RANGE"
        or index.entity_type != "NODE"
        or index.state != _ONLINE
    ):
        raise RuntimeError("scoped Identifier uniqueness backing index is not ready")


def _await_index_online(client: Neo4jClient, index_name: str) -> None:
    deadline = time.monotonic() + _SCHEMA_WAIT_SECONDS
    while True:
        inventory = show_identifier_scope_schema(client)
        index = inventory.indexes.get(index_name)
        if index is None:
            raise RuntimeError(f"scoped Identifier index {index_name} disappeared while awaiting")
        if index.state == _ONLINE:
            return
        if index.state == "FAILED":
            raise RuntimeError(f"scoped Identifier index {index_name} failed to populate")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"timed out waiting for scoped Identifier index {index_name}")
        time.sleep(_SCHEMA_POLL_SECONDS)


def _execute_schema_statement(client: Neo4jClient, statement: str) -> None:
    with client.session() as session:
        session.run(statement).consume()


def _is_legacy_index(definition: IdentifierScopeIndex) -> bool:
    return (
        definition.index_type == "RANGE"
        and definition.entity_type == "NODE"
        and definition.labels_or_types == (IDENTIFIER_LABEL,)
        and definition.properties == IDENTIFIER_SCOPE_PROPERTIES
        and definition.state == _ONLINE
        and definition.owning_constraint is None
    )


def _is_bridge_index(definition: IdentifierScopeIndex) -> bool:
    return (
        definition.index_type == "RANGE"
        and definition.entity_type == "NODE"
        and definition.labels_or_types == (IDENTIFIER_LABEL,)
        and definition.properties == BRIDGE_PROPERTIES
        and definition.owning_constraint is None
    )


def _is_identifier_scope_constraint(definition: IdentifierScopeConstraint) -> bool:
    return (
        definition.constraint_type == "UNIQUENESS"
        and definition.entity_type == "NODE"
        and _is_identifier_scope_identity(definition.labels_or_types, definition.properties)
    )


def _is_identifier_scope_identity(
    labels_or_types: tuple[str, ...] | None, properties: tuple[str, ...] | None
) -> bool:
    return labels_or_types == (IDENTIFIER_LABEL,) and properties == IDENTIFIER_SCOPE_PROPERTIES


def _is_bridge_identity(
    labels_or_types: tuple[str, ...] | None, properties: tuple[str, ...] | None
) -> bool:
    return labels_or_types == (IDENTIFIER_LABEL,) and properties == BRIDGE_PROPERTIES


def _index_definition(record: Record) -> IdentifierScopeIndex:
    return IdentifierScopeIndex(
        name=_required_text(record, "name"),
        index_type=_required_text(record, "type"),
        entity_type=_required_text(record, "entityType"),
        labels_or_types=_optional_text_list(record, "labelsOrTypes"),
        properties=_optional_text_list(record, "properties"),
        state=_required_text(record, "state"),
        owning_constraint=_optional_text(record, "owningConstraint"),
    )


def _constraint_definition(record: Record) -> IdentifierScopeConstraint:
    return IdentifierScopeConstraint(
        name=_required_text(record, "name"),
        constraint_type=_required_text(record, "type"),
        entity_type=_required_text(record, "entityType"),
        labels_or_types=_required_text_list(record, "labelsOrTypes"),
        properties=_required_text_list(record, "properties"),
        owned_index=_optional_text(record, "ownedIndex"),
    )


def _required_text(record: Record, key: str) -> str:
    value = record[key]
    if not isinstance(value, str):
        raise RuntimeError(f"scoped Identifier schema inventory returned invalid {key}")
    return value


def _optional_text(record: Record, key: str) -> str | None:
    value = record[key]
    if value is None or isinstance(value, str):
        return value
    raise RuntimeError(f"scoped Identifier schema inventory returned invalid {key}")


def _required_text_list(record: Record, key: str) -> tuple[str, ...]:
    value = record[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuntimeError(f"scoped Identifier schema inventory returned invalid {key}")
    return tuple(value)


def _optional_text_list(record: Record, key: str) -> tuple[str, ...] | None:
    value = record[key]
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuntimeError(f"scoped Identifier schema inventory returned invalid {key}")
    return tuple(value)
