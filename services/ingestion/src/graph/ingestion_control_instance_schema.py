"""Exact Neo4j constraint inventory helpers for #272 control migration."""

from __future__ import annotations

from dataclasses import dataclass

from neo4j import ManagedTransaction, Record

from src.graph.client import Neo4jClient


@dataclass(frozen=True)
class ConstraintDefinition:
    name: str
    constraint_type: str
    entity_type: str
    labels_or_types: tuple[str, ...]
    properties: tuple[str, ...]


def show_constraints(client: Neo4jClient) -> dict[str, ConstraintDefinition]:
    def _read(tx: ManagedTransaction) -> dict[str, ConstraintDefinition]:
        result = tx.run(
            "SHOW CONSTRAINTS YIELD name, type, entityType, labelsOrTypes, properties "
            "RETURN name, type, entityType, labelsOrTypes, properties"
        )
        definitions: dict[str, ConstraintDefinition] = {}
        for record in result:
            definition = constraint_definition(record)
            if definition.name in definitions:
                raise RuntimeError("control-instance constraint inventory is ambiguous")
            definitions[definition.name] = definition
        return definitions

    return client.execute_read(_read)


def assert_no_unexpected_named_constraint(
    constraints: dict[str, ConstraintDefinition],
    specs: tuple[tuple[str, str, tuple[str, ...]], ...],
) -> None:
    known = {spec[0] for spec in specs}
    identities = {(label, properties) for _name, label, properties in specs}
    for name, definition in constraints.items():
        if not definition.labels_or_types:
            raise RuntimeError("constraint inventory returned empty labels or types")
        identity = (definition.labels_or_types[0], definition.properties)
        if name in known and definition.constraint_type != "UNIQUENESS":
            raise RuntimeError(f"control-instance constraint {name} has an unexpected type")
        if name not in known and definition.entity_type == "NODE" and identity in identities:
            raise RuntimeError(
                f"unrecognized constraint {name} enforces a retired or replacement identity"
            )


def assert_constraint(
    definition: ConstraintDefinition, spec: tuple[str, str, tuple[str, ...]]
) -> None:
    name, label, properties = spec
    if (
        definition.name != name
        or definition.constraint_type != "UNIQUENESS"
        or definition.entity_type != "NODE"
        or definition.labels_or_types != (label,)
        or definition.properties != properties
    ):
        raise RuntimeError(f"control-instance constraint {name} has an unexpected definition")


def create_constraint(spec: tuple[str, str, tuple[str, ...]]) -> str:
    name, label, properties = spec
    rendered = ", ".join(f"node.{property}" for property in properties)
    return (
        f"CREATE CONSTRAINT {name} IF NOT EXISTS FOR (node:{label}) REQUIRE ({rendered}) IS UNIQUE"
    )


def constraint_definition(record: Record) -> ConstraintDefinition:
    def text(key: str) -> str:
        value = record[key]
        if not isinstance(value, str):
            raise RuntimeError(f"constraint inventory returned invalid {key}")
        return value

    raw_labels = record["labelsOrTypes"]
    raw_properties = record["properties"]
    if not isinstance(raw_labels, list) or not all(isinstance(value, str) for value in raw_labels):
        raise RuntimeError("constraint inventory returned invalid labels")
    if not isinstance(raw_properties, list) or not all(
        isinstance(value, str) for value in raw_properties
    ):
        raise RuntimeError("constraint inventory returned invalid properties")
    return ConstraintDefinition(
        text("name"), text("type"), text("entityType"), tuple(raw_labels), tuple(raw_properties)
    )
