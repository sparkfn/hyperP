"""Evidence snapshots and non-sensitive graph fingerprints for NRIC tests."""

from __future__ import annotations

import hashlib
import json

import pytest
from neo4j import Driver
from src.graph.queries import nric_rehash_consolidate as consolidate_queries
from src.graph.queries.identity_link_revisions import (
    LIST_IDENTITY_LINK_EVENTS,
    LIST_IDENTITY_LINK_SNAPSHOT,
)
from src.graph.queries.nric_rehash import NRIC_REHASH_READ_REVISION
from src.identity_links import ParticipantKind


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


def _non_derived_relationship_snapshot(driver: Driver, identifier_id: str) -> tuple[str, ...]:
    with driver.session() as session:
        row = session.run(
            "MATCH (identifier:Identifier {identifier_id: $identifier_id}) "
            "OPTIONAL MATCH (identifier)-[rel]-(other) "
            "WITH [item IN collect(CASE WHEN rel IS NULL OR type(rel) = 'HAS_IDENTIFIER' "
            "THEN null ELSE {type: type(rel), "
            "identifier_is_start: startNode(rel) = identifier, "
            "other_labels: labels(other), other_properties: properties(other), "
            "properties: properties(rel)} END) WHERE item IS NOT NULL] AS incident "
            "RETURN incident",
            identifier_id=identifier_id,
        ).single(strict=True)
    incident = row["incident"]
    if not isinstance(incident, list):
        raise AssertionError("evidence relationship inventory was malformed")
    serialized = [json.dumps(dict(item), sort_keys=True, default=str) for item in incident]
    return tuple(sorted(serialized))


def _person_properties_snapshot(driver: Driver, person_ids: tuple[str, ...]) -> tuple[str, ...]:
    with driver.session() as session:
        records = session.run(
            "UNWIND $person_ids AS person_id "
            "MATCH (person:Person {person_id: person_id}) "
            "RETURN properties(person) AS properties ORDER BY person_id",
            person_ids=list(person_ids),
        )
        serialized: list[str] = []
        for record in records:
            properties = record.get("properties")
            if not isinstance(properties, dict):
                raise AssertionError("Person properties were malformed")
            serialized.append(json.dumps(dict(properties), sort_keys=True, default=str))
    return tuple(serialized)


def _participant_nric_links(
    driver: Driver,
    participant_kind: ParticipantKind,
    participant_id: str,
) -> tuple[tuple[str, str, int | None, float], ...]:
    with driver.session() as session:
        records = session.run(
            "MATCH (participant)-[link:HAS_IDENTIFIER]->"
            "(identifier:Identifier {identifier_type: 'nric'}) "
            "WHERE ($participant_kind = 'person' AND participant:Person "
            "AND participant.person_id = $participant_id) "
            "OR ($participant_kind = 'profile' AND participant:Profile "
            "AND participant.profile_id = $participant_id) "
            "RETURN identifier.identifier_id AS identifier_id, link.link_id AS link_id, "
            "identifier.participant_count AS participant_count, "
            "link.exclusivity AS exclusivity ORDER BY identifier_id",
            participant_kind=participant_kind,
            participant_id=participant_id,
        )
        links: list[tuple[str, str, int | None, float]] = []
        for record in records:
            identifier_id: object = record.get("identifier_id")
            link_id: object = record.get("link_id")
            participant_count: object = record.get("participant_count")
            exclusivity: object = record.get("exclusivity")
            if not isinstance(identifier_id, str) or not isinstance(link_id, str):
                raise AssertionError("participant link properties were malformed")
            if participant_count is not None and (
                not isinstance(participant_count, int) or isinstance(participant_count, bool)
            ):
                raise AssertionError("participant count was malformed")
            if not isinstance(exclusivity, (int, float)) or isinstance(exclusivity, bool):
                raise AssertionError("participant link exclusivity was malformed")
            links.append((identifier_id, link_id, participant_count, float(exclusivity)))
    return tuple(links)


def _identifier_element_ids(driver: Driver) -> tuple[str, ...]:
    with driver.session() as session:
        rows = session.run(
            "MATCH (identifier:Identifier {identifier_type: 'nric'}) "
            "RETURN elementId(identifier) AS element_id ORDER BY element_id"
        )
        element_ids: list[str] = []
        for row in rows:
            element_id: object = row.get("element_id")
            if not isinstance(element_id, str):
                raise AssertionError("Identifier element id was malformed")
            element_ids.append(element_id)
    return tuple(element_ids)


def _active_attribution_snapshot(driver: Driver) -> tuple[tuple[str, str], ...]:
    with driver.session() as session:
        rows = session.run(
            "MATCH (record:SourceRecord)-[link:LINKED_TO]->(person:Person) "
            "WHERE coalesce(link.is_active, true) = true "
            "RETURN record.source_record_pk AS source_record_pk, "
            "person.person_id AS person_id ORDER BY source_record_pk, person_id"
        )
        pairs: list[tuple[str, str]] = []
        for row in rows:
            source_record_pk: object = row.get("source_record_pk")
            person_id: object = row.get("person_id")
            if not isinstance(source_record_pk, str) or not isinstance(person_id, str):
                raise AssertionError("active attribution row was malformed")
            pairs.append((source_record_pk, person_id))
    return tuple(pairs)


def _identity_revision_snapshot(
    driver: Driver,
) -> tuple[int, tuple[str, ...], tuple[str, ...], int]:
    with driver.session() as session:
        counter_record = session.run(
            NRIC_REHASH_READ_REVISION,
            stream_key="identity_link_revision_stream_v1",
        ).single(strict=True)
        raw_counter: object = counter_record.get("current_revision")
        if raw_counter is None:
            counter = 0
        elif isinstance(raw_counter, int) and not isinstance(raw_counter, bool):
            counter = raw_counter
        else:
            raise AssertionError("identity-link revision counter was malformed")
        events = session.run(
            LIST_IDENTITY_LINK_EVENTS,
            after_revision=0,
            through_revision=counter,
            limit=100,
        )
        event_rows = tuple(
            json.dumps(dict(row["revision"]), sort_keys=True, default=str) for row in events
        )
        snapshot = session.run(
            LIST_IDENTITY_LINK_SNAPSHOT,
            snapshot_revision=counter,
            after_link_key="",
            limit=100,
        )
        snapshot_rows = tuple(
            json.dumps(
                {"link_key": row["link_key"], "revision": row["revision"]},
                sort_keys=True,
                default=str,
            )
            for row in snapshot
        )
        tails = session.run(consolidate_queries.NRIC_REHASH_VERIFY_IDENTITY_LINK_HEAD_TAILS).single(
            strict=True
        )
        mismatches: object = tails.get("mismatch_count")
        if not isinstance(mismatches, int) or isinstance(mismatches, bool):
            raise AssertionError("identity-link head/tail count was malformed")
    return counter, event_rows, snapshot_rows, mismatches


def _normalize_legacy_clock(driver: Driver) -> None:
    """Normalize C3 recompute timestamps for equivalent disposable runs."""
    with driver.session() as session:
        session.run(
            "MATCH ()-[link:HAS_IDENTIFIER]->() SET link.computed_at = datetime($fixed)",
            fixed="2020-01-01T00:00:00Z",
        ).consume()
