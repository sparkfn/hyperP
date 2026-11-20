"""Evidence snapshots and non-sensitive graph fingerprints for NRIC tests."""

from __future__ import annotations

import hashlib
import json

import pytest
from neo4j import Driver


def _identifier_properties(driver: Driver, identifier_id: str) -> dict[str, object]:
    with driver.session() as session:
        row = session.run(
            "MATCH (identifier:Identifier {identifier_id: $identifier_id}) "
            "RETURN properties(identifier) AS properties",
            identifier_id=identifier_id,
        ).single(strict=True)
    properties = row["properties"]
    if not isinstance(properties, dict):
        raise AssertionError("Identifier properties were malformed")
    return dict(properties)


def _relationship_snapshot(driver: Driver, identifier_id: str) -> tuple[str, ...]:
    with driver.session() as session:
        row = session.run(
            "MATCH (identifier:Identifier {identifier_id: $identifier_id}) "
            "OPTIONAL MATCH (identifier)-[rel]-(other) "
            "WITH identifier, [item IN collect(CASE WHEN rel IS NULL THEN null ELSE {"
            "type: type(rel), identifier_is_start: startNode(rel) = identifier, "
            "other_labels: labels(other), other_properties: properties(other), "
            "properties: properties(rel)} END) WHERE item IS NOT NULL] AS incident "
            "RETURN incident",
            identifier_id=identifier_id,
        ).single(strict=True)
    incident = row["incident"]
    if not isinstance(incident, list):
        raise AssertionError("incident relationship inventory was malformed")
    serialized = [json.dumps(dict(item), sort_keys=True, default=str) for item in incident]
    return tuple(sorted(serialized))


def _safe_equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        pytest.fail(f"{label} did not match")


def _graph_fingerprint(
    driver: Driver,
) -> tuple[tuple[tuple[str, int], ...], tuple[tuple[str, int], ...], str]:
    with driver.session() as session:
        node_counts = tuple(
            sorted(
                (str(row["label"]), int(row["count"]))
                for row in session.run(
                    "MATCH (node) UNWIND labels(node) AS label RETURN label, count(*) AS count"
                )
            )
        )
        relationship_counts = tuple(
            sorted(
                (str(row["type"]), int(row["count"]))
                for row in session.run(
                    "MATCH ()-[rel]->() RETURN type(rel) AS type, count(rel) AS count"
                )
            )
        )
        nodes = [
            {"labels": list(row["labels"]), "properties": dict(row["properties"])}
            for row in session.run(
                "MATCH (node) RETURN labels(node) AS labels, properties(node) AS properties"
            )
        ]
        relationships = [
            {
                "type": row["type"],
                "properties": dict(row["properties"]),
                "start_labels": list(row["start_labels"]),
                "start_properties": dict(row["start_properties"]),
                "end_labels": list(row["end_labels"]),
                "end_properties": dict(row["end_properties"]),
            }
            for row in session.run(
                "MATCH (start)-[rel]->(end) RETURN type(rel) AS type, "
                "properties(rel) AS properties, labels(start) AS start_labels, "
                "properties(start) AS start_properties, labels(end) AS end_labels, "
                "properties(end) AS end_properties"
            )
        ]
    nodes.sort(key=lambda item: json.dumps(item, sort_keys=True, default=str))
    relationships.sort(key=lambda item: json.dumps(item, sort_keys=True, default=str))
    payload = json.dumps(
        {"nodes": nodes, "relationships": relationships}, sort_keys=True, default=str
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return node_counts, relationship_counts, digest


def _normalize_migration_clock(driver: Driver) -> None:
    # Normalize wall-clock and lease-lock fields only for cross-run comparison.
    with driver.session() as session:
        session.run(
            "MATCH (migration:DataMigration {migration_key: $key}) "
            "SET migration.created_at = datetime($fixed), "
            "migration.updated_at = datetime($fixed), "
            "migration.offline_acknowledged_at = datetime($fixed), "
            "migration.lock_version = 0",
            key="nric_hmac_rehash_v1",
            fixed="2020-01-01T00:00:00Z",
        ).consume()
