"""Schema inventory and readiness checks for standalone CRM census control."""

from __future__ import annotations

from dataclasses import dataclass

from neo4j import ManagedTransaction, Record

from src.graph.client import Neo4jClient
from src.graph.ingestion_control_instance_schema import (
    ConstraintDefinition,
    assert_constraint,
    show_constraints,
)
from src.graph.queries.standalone_crm_census import (
    CENSUS_CONSTRAINT_SPECS,
    CENSUS_INDEX_SPECS,
)


@dataclass(frozen=True)
class IndexDefinition:
    """Normalized Neo4j index definition used for fresh/runtime schema parity."""

    name: str
    index_type: str
    entity_type: str
    labels_or_types: tuple[str, ...]
    properties: tuple[str, ...]


def show_indexes(client: Neo4jClient) -> dict[str, IndexDefinition]:
    """Return the runtime index inventory without accepting a similarly named index."""

    def read(tx: ManagedTransaction) -> dict[str, IndexDefinition]:
        result = tx.run(
            "SHOW INDEXES YIELD name, type, entityType, labelsOrTypes, properties "
            "RETURN name, type, entityType, labelsOrTypes, properties"
        )
        definitions: dict[str, IndexDefinition] = {}
        for record in result:
            definition = _index_definition(record)
            if definition.name in definitions:
                raise RuntimeError("standalone CRM census index inventory is ambiguous")
            definitions[definition.name] = definition
        return definitions

    return client.execute_read(read)


def assert_standalone_census_constraints(client: Neo4jClient) -> None:
    """Require every #273 identity constraint before accepting its ready marker."""
    constraints = show_constraints(client)
    for spec in CENSUS_CONSTRAINT_SPECS:
        definition: ConstraintDefinition | None = constraints.get(spec[0])
        if definition is None:
            raise RuntimeError(f"standalone CRM census constraint {spec[0]} is missing")
        assert_constraint(definition, spec)


def assert_standalone_census_indexes(client: Neo4jClient) -> None:
    """Require every #273 operational index before accepting its ready marker."""
    indexes = show_indexes(client)
    for name, label, properties in CENSUS_INDEX_SPECS:
        definition = indexes.get(name)
        if definition is None:
            raise RuntimeError(f"standalone CRM census index {name} is missing")
        if (
            definition.index_type != "RANGE"
            or definition.entity_type != "NODE"
            or definition.labels_or_types != (label,)
            or definition.properties != properties
        ):
            raise RuntimeError(f"standalone CRM census index {name} has an unexpected definition")


def assert_standalone_census_schema(client: Neo4jClient) -> None:
    """Require the complete #273 constraint and index inventory."""
    assert_standalone_census_constraints(client)
    assert_standalone_census_indexes(client)


def _index_definition(record: Record) -> IndexDefinition:
    name = record["name"]
    index_type = record["type"]
    entity_type = record["entityType"]
    labels = record["labelsOrTypes"]
    properties = record["properties"]
    if not all(isinstance(value, str) for value in (name, index_type, entity_type)):
        raise RuntimeError("standalone CRM census index inventory returned invalid text")
    if not isinstance(labels, list) or not all(isinstance(value, str) for value in labels):
        raise RuntimeError("standalone CRM census index inventory returned invalid labels")
    if not isinstance(properties, list) or not all(isinstance(value, str) for value in properties):
        raise RuntimeError("standalone CRM census index inventory returned invalid properties")
    return IndexDefinition(name, index_type, entity_type, tuple(labels), tuple(properties))
