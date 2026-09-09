"""Bounded Neo4j implementation for manifest-gated activity cleanup."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Literal, Protocol, cast

from neo4j import READ_ACCESS, Driver, GraphDatabase, ManagedTransaction

from intelligence.graph.queries.crm_activity_cleanup import (
    DATABASE_IDENTITY,
    DELETE_NODES_BY_ELEMENT_IDS,
    DELETE_RELATIONSHIPS_BY_OWNERS,
    INSPECT_IDENTITIES,
    VERIFY_PROTECTED,
)
from intelligence.repositories.protocols.crm_activity_cleanup import (
    MAX_BATCH_IDENTITIES,
    MAX_INCIDENT_RELATIONSHIPS,
    BatchOutcome,
    CleanupPlan,
    EndpointIdentity,
    ExactRecordInspection,
    ExpectedDeletionFact,
    IncidentRelationship,
    LiveTargetIdentity,
    ObservedRecord,
    ParentIdentity,
    ProtectedEvidence,
    RecordOutcome,
)


class _Row(Protocol):
    def data(self) -> dict[str, object]: ...

    def get(self, key: str) -> object: ...


class _Result(Protocol):
    def __iter__(self) -> Iterator[_Row]: ...

    def single(self) -> _Row | None: ...


class _Run(Protocol):
    def __call__(self, query: str, parameters: Mapping[str, object] | None = None) -> _Result: ...


class Neo4jCrmActivityCleanupRepository:
    """A closed deletion capability with transaction-local exact revalidation."""

    def __init__(self, uri: str, user: str, password: str, database: str | None = None) -> None:
        if not uri or not user or not password:
            raise ValueError("Neo4j cleanup configuration is incomplete")
        self._driver: Driver = GraphDatabase.driver(uri, auth=(user, password))
        self._database = database or None

    def close(self) -> None:
        self._driver.close()

    def database_identity(self) -> str:
        with self._driver.session(
            database=self._database, default_access_mode=READ_ACCESS
        ) as session:
            return _database_identity(cast(_Run, session.run))

    def inspect(
        self,
        source_record_pks: tuple[str, ...],
        relationship_limit: int = MAX_INCIDENT_RELATIONSHIPS,
    ) -> tuple[ExactRecordInspection, ...]:
        _validate_pk_batch(source_record_pks)
        _validate_relationship_limit(relationship_limit)
        with self._driver.session(
            database=self._database, default_access_mode=READ_ACCESS
        ) as session:
            return _inspect(
                cast(_Run, session.run),
                source_record_pks,
                relationship_limit,
            )

    def plan(
        self,
        identities: tuple[LiveTargetIdentity, ...],
        inspections: tuple[ExactRecordInspection, ...],
    ) -> CleanupPlan:
        _validate_identities(identities)
        if tuple(item.source_record_pk for item in inspections) != tuple(
            item.source_record_pk for item in identities
        ):
            raise ValueError("cleanup inspections do not exactly cover requested identities")
        selected = {item.source_record_pk: item for item in identities}
        initial: dict[str, RecordOutcome] = {}
        valid: dict[str, ExactRecordInspection] = {}
        for identity, inspection in zip(identities, inspections, strict=True):
            reason = _identity_conflict(identity, inspection)
            if reason is None:
                parent_reason = _companion_parent_conflict(identity, inspection)
                if parent_reason is None:
                    valid[identity.source_record_pk] = inspection
                else:
                    initial[identity.source_record_pk] = RecordOutcome(
                        identity.source_record_pk, "conflict", parent_reason
                    )
            elif reason == "already_absent":
                initial[identity.source_record_pk] = RecordOutcome(
                    identity.source_record_pk, "already_absent", "absent_before_mutation"
                )
            else:
                initial[identity.source_record_pk] = RecordOutcome(
                    identity.source_record_pk, "conflict", reason
                )

        owned_by: dict[str, set[str]] = {key: set() for key in valid}
        allowed_relationships: set[str] = set()
        for key, inspection in valid.items():
            identity = selected[key]
            for relationship in inspection.incident_relationships:
                if _is_owned_from_source(identity, relationship):
                    owned_by[key].add(relationship.relationship_element_id)
                    allowed_relationships.add(relationship.relationship_element_id)
                elif _is_owned_call_parent(identity, relationship, selected):
                    owned_by[key].add(relationship.relationship_element_id)
                    allowed_relationships.add(relationship.relationship_element_id)

        protected: list[ProtectedEvidence] = []
        for key, inspection in valid.items():
            unowned = tuple(
                item
                for item in inspection.incident_relationships
                if item.relationship_element_id not in allowed_relationships
            )
            if unowned or inspection.relationship_inventory_truncated:
                reason = (
                    "incident_relationship_inventory_truncated"
                    if inspection.relationship_inventory_truncated
                    else "unproven_incident_relationship"
                )
                initial[key] = RecordOutcome(key, "retained", reason)
                protected.extend(ProtectedEvidence(key, item) for item in unowned)

        expected: list[ExpectedDeletionFact] = []
        for identity in identities:
            current_inspection: ExactRecordInspection | None = valid.get(identity.source_record_pk)
            if current_inspection is None or identity.source_record_pk in initial:
                continue
            if current_inspection.record is None:
                raise RuntimeError("valid graph inspection unexpectedly lacks a record")
            expected.append(
                ExpectedDeletionFact(
                    identity,
                    current_inspection.record,
                    current_inspection.incident_relationships,
                    tuple(sorted(owned_by[identity.source_record_pk])),
                )
            )
        outcomes = tuple(
            initial.get(
                identity.source_record_pk,
                RecordOutcome(identity.source_record_pk, "retained", "ready_for_batch_mutation"),
            )
            for identity in identities
        )
        return CleanupPlan(
            tuple(expected),
            tuple(sorted(set(protected), key=ProtectedEvidence.key)),
            outcomes,
        )

    def delete_batch(
        self,
        database_identity: str,
        expected: tuple[ExpectedDeletionFact, ...],
    ) -> BatchOutcome:
        if not database_identity:
            raise ValueError("expected database identity is required")
        _validate_expected(expected)
        if not expected:
            return BatchOutcome(database_identity, (), False)
        with self._driver.session(database=self._database) as session:
            return session.execute_write(self._delete_transaction, database_identity, expected)

    def verify_protected(
        self, protected: tuple[ProtectedEvidence, ...]
    ) -> tuple[ProtectedEvidence, ...]:
        if not protected:
            return ()
        if tuple(sorted(set(protected), key=ProtectedEvidence.key)) != protected:
            raise ValueError("protected evidence is not finite and canonical")
        parameters = {
            "protected_relationships": [_protected_parameter(item) for item in protected],
        }
        with self._driver.session(
            database=self._database, default_access_mode=READ_ACCESS
        ) as session:
            rows = tuple(cast(_Result, session.run(VERIFY_PROTECTED, parameters)))
        actual = tuple(_protected_from_row(row.data()) for row in rows)
        return tuple(item for item in protected if item not in actual)

    def _delete_transaction(
        self,
        transaction: ManagedTransaction,
        expected_database_identity: str,
        expected: tuple[ExpectedDeletionFact, ...],
    ) -> BatchOutcome:
        observed_database_identity = _database_identity(cast(_Run, transaction.run))
        if observed_database_identity != expected_database_identity:
            raise RuntimeError("Neo4j database identity changed before cleanup mutation")
        identities = tuple(item.target.source_record_pk for item in expected)
        observations = _inspect(cast(_Run, transaction.run), identities, MAX_INCIDENT_RELATIONSHIPS)
        conflicts: dict[str, RecordOutcome] = {}
        present: list[ExpectedDeletionFact] = []
        for fact, observation in zip(expected, observations, strict=True):
            if observation.matching_node_count == 0:
                conflicts[fact.target.source_record_pk] = RecordOutcome(
                    fact.target.source_record_pk, "already_absent", "absent_at_mutation"
                )
            elif not _fact_matches_observation(fact, observation):
                conflicts[fact.target.source_record_pk] = RecordOutcome(
                    fact.target.source_record_pk, "conflict", "revalidation_mismatch"
                )
            else:
                present.append(fact)
        if conflicts:
            outcomes = tuple(
                conflicts.get(
                    item.target.source_record_pk,
                    RecordOutcome(
                        item.target.source_record_pk,
                        "retained",
                        "batch_rolled_back_due_to_conflict",
                    ),
                )
                for item in expected
            )
            return BatchOutcome(observed_database_identity, outcomes, False)

        calls = tuple(item for item in present if item.target.record_type == "call")
        activities = tuple(item for item in present if item.target.record_type == "crm_history")
        _delete_group(cast(_Run, transaction.run), calls)
        _delete_group(cast(_Run, transaction.run), activities)
        return BatchOutcome(
            observed_database_identity,
            tuple(
                RecordOutcome(item.target.source_record_pk, "deleted", "deleted_after_revalidation")
                for item in expected
            ),
            bool(expected),
        )


def _delete_group(run: _Run, facts: tuple[ExpectedDeletionFact, ...]) -> None:
    if not facts:
        return
    owners = tuple(
        {
            "record_element_id": fact.observed.element_id,
            "relationship_element_ids": list(fact.owned_relationship_element_ids),
        }
        for fact in facts
        if fact.owned_relationship_element_ids
    )
    expected_relationship_count = sum(len(fact.owned_relationship_element_ids) for fact in facts)
    if owners:
        relationship_row = run(
            DELETE_RELATIONSHIPS_BY_OWNERS,
            {"relationship_owners": list(owners)},
        ).single()
        if (
            relationship_row is None
            or relationship_row.get("deleted_count") != expected_relationship_count
        ):
            raise RuntimeError("enumerated relationship deletion was incomplete")
    node_ids = tuple(item.observed.element_id for item in facts)
    node_row = run(
        DELETE_NODES_BY_ELEMENT_IDS,
        {"record_element_ids": list(node_ids)},
    ).single()
    if node_row is None or node_row.get("deleted_count") != len(node_ids):
        raise RuntimeError("enumerated node deletion was incomplete")


def _database_identity(run: _Run) -> str:
    row = run(DATABASE_IDENTITY).single()
    value = None if row is None else row.get("database_identity")
    if not isinstance(value, str) or not value:
        raise RuntimeError("Neo4j did not provide a live database identity")
    return value


def _inspect(
    run: _Run,
    source_record_pks: tuple[str, ...],
    relationship_limit: int,
) -> tuple[ExactRecordInspection, ...]:
    result = run(
        INSPECT_IDENTITIES,
        {
            "source_record_pks": list(source_record_pks),
            "relationship_limit_plus_one": relationship_limit + 1,
        },
    )
    rows = tuple(_inspection_from_row(row.data(), relationship_limit) for row in result)
    if tuple(item.source_record_pk for item in rows) != source_record_pks:
        raise RuntimeError("exact identity inspection did not return one canonical row per request")
    return rows


def _inspection_from_row(
    value: Mapping[str, object], relationship_limit: int
) -> ExactRecordInspection:
    pk = _required_text(value.get("source_record_pk"), "source_record_pk")
    matching_node_count = _nonnegative_int(value.get("matching_node_count"), "matching_node_count")
    raw_relationships = value.get("incident_relationships")
    if not isinstance(raw_relationships, list):
        raise RuntimeError("incident relationship inventory is invalid")
    relationships = tuple(
        sorted((_relationship(item) for item in raw_relationships), key=IncidentRelationship.key)
    )
    relationship_count = _nonnegative_int(
        value.get("incident_relationship_count"), "incident_relationship_count"
    )
    if relationship_count > relationship_limit and len(relationships) != relationship_limit + 1:
        raise RuntimeError("incident relationship inventory is not bounded as requested")
    element_id = _optional_text(value.get("record_element_id"), "record_element_id")
    record: ObservedRecord | None = None
    if matching_node_count > 0:
        if element_id is None:
            raise RuntimeError("matched node has no element identity")
        record = ObservedRecord(
            element_id,
            _labels(value.get("labels")),
            _optional_text(value.get("record_type"), "record_type"),
            _optional_text(value.get("source_instance_id"), "source_instance_id"),
            _optional_text(value.get("source_record_version"), "source_record_version"),
            _optional_text(value.get("record_hash"), "record_hash"),
            _optional_text(value.get("lifecycle_status"), "lifecycle_status"),
            _optional_text(value.get("history_family"), "history_family"),
            _parent(value),
        )
    return ExactRecordInspection(
        pk,
        matching_node_count,
        record,
        relationships,
        relationship_count,
        relationship_count > len(relationships),
    )


def _relationship(value: object) -> IncidentRelationship:
    if not isinstance(value, Mapping):
        raise RuntimeError("incident relationship is invalid")
    direction = _required_text(value.get("direction"), "relationship direction")
    if direction not in {"inbound", "outbound", "self"}:
        raise RuntimeError("incident relationship direction is invalid")
    return IncidentRelationship(
        _required_text(value.get("relationship_element_id"), "relationship element id"),
        cast(Literal["inbound", "outbound", "self"], direction),
        _required_text(value.get("relationship_type"), "relationship type"),
        EndpointIdentity(
            _required_text(value.get("other_element_id"), "other endpoint element id"),
            _labels(value.get("other_labels")),
            _optional_text(value.get("other_source_record_pk"), "other_source_record_pk"),
            _optional_text(value.get("other_person_id"), "other_person_id"),
            _optional_text(value.get("other_identifier_type"), "other_identifier_type"),
            _optional_text(value.get("other_identifier_value"), "other_identifier_value"),
            _optional_text(value.get("other_source_key"), "other_source_key"),
            _optional_text(value.get("other_review_case_id"), "other_review_case_id"),
            _optional_text(value.get("other_match_decision_id"), "other_match_decision_id"),
        ),
    )


def _parent(value: Mapping[str, object]) -> ParentIdentity:
    return ParentIdentity(
        _optional_text(value.get("parent_source_record_pk"), "parent_source_record_pk"),
        _optional_text(value.get("parent_source_instance_id"), "parent_source_instance_id"),
        _optional_text(value.get("parent_source_record_id"), "parent_source_record_id"),
        _optional_text(value.get("parent_record_type"), "parent_record_type"),
        _optional_text(value.get("parent_source_system"), "parent_source_system"),
    )


def _identity_conflict(
    identity: LiveTargetIdentity, inspection: ExactRecordInspection
) -> str | None:
    if inspection.matching_node_count == 0:
        return "already_absent"
    if inspection.matching_node_count != 1 or inspection.record is None:
        return "duplicate_or_malformed_identity"
    record = inspection.record
    if record.labels != ("SourceRecord",):
        return "wrong_labels"
    if record.record_type != identity.record_type:
        return "wrong_record_type"
    if record.source_instance_id != identity.source_instance_id:
        return "wrong_source_instance"
    if record.source_record_version != identity.source_record_version:
        return "wrong_source_record_version"
    if record.record_hash != identity.record_hash:
        return "wrong_record_hash"
    if record.lifecycle_status != identity.lifecycle_status:
        return "wrong_lifecycle_status"
    if record.history_family != identity.history_family:
        return "wrong_history_family"
    if record.stored_parent != identity.stored_parent:
        return "stored_parent_drift"
    return None


def _is_owned_from_source(identity: LiveTargetIdentity, relationship: IncidentRelationship) -> bool:
    endpoint = relationship.other_endpoint
    return (
        relationship.direction == "outbound"
        and relationship.relationship_type == "FROM_SOURCE"
        and endpoint.labels == ("SourceSystem",)
        and endpoint.source_key == identity.source_key
    )


def _is_owned_call_parent(
    identity: LiveTargetIdentity,
    relationship: IncidentRelationship,
    selected: Mapping[str, LiveTargetIdentity],
) -> bool:
    parent = selected.get(relationship.other_endpoint.source_record_pk or "")
    expected = (
        identity.child_parent_source_record_pks
        if relationship.relationship_type == "CHILD_OF"
        else identity.details_parent_source_record_pks
    )
    return (
        identity.record_type == "call"
        and relationship.direction == "outbound"
        and relationship.relationship_type in {"CHILD_OF", "DETAILS_HISTORY_ITEM"}
        and relationship.other_endpoint.labels == ("SourceRecord",)
        and parent is not None
        and parent.record_type == "crm_history"
        and parent.source_record_pk in expected
    )


def _companion_parent_conflict(
    identity: LiveTargetIdentity, inspection: ExactRecordInspection
) -> str | None:
    if identity.record_type != "call":
        return None
    child = tuple(
        sorted(
            item.other_endpoint.source_record_pk or ""
            for item in inspection.incident_relationships
            if item.direction == "outbound"
            and item.relationship_type == "CHILD_OF"
            and item.other_endpoint.labels == ("SourceRecord",)
        )
    )
    details = tuple(
        sorted(
            item.other_endpoint.source_record_pk or ""
            for item in inspection.incident_relationships
            if item.direction == "outbound"
            and item.relationship_type == "DETAILS_HISTORY_ITEM"
            and item.other_endpoint.labels == ("SourceRecord",)
        )
    )
    if (
        child != identity.child_parent_source_record_pks
        or details != identity.details_parent_source_record_pks
    ):
        return "companion_parent_relationship_drift"
    return None


def _fact_matches_observation(
    fact: ExpectedDeletionFact, observation: ExactRecordInspection
) -> bool:
    return (
        _identity_conflict(fact.target, observation) is None
        and observation.record == fact.observed
        and observation.incident_relationships == fact.incident_relationships
        and not observation.relationship_inventory_truncated
    )


def _validate_pk_batch(source_record_pks: tuple[str, ...]) -> None:
    if (
        not source_record_pks
        or len(source_record_pks) > MAX_BATCH_IDENTITIES
        or tuple(sorted(set(source_record_pks))) != source_record_pks
    ):
        raise ValueError("source record identities must be finite, unique, and canonically ordered")
    for source_record_pk in source_record_pks:
        if not source_record_pk or len(source_record_pk) > 1_024 or "\x00" in source_record_pk:
            raise ValueError("source record identity is invalid")


def _validate_identities(identities: tuple[LiveTargetIdentity, ...]) -> None:
    _validate_pk_batch(tuple(item.source_record_pk for item in identities))


def _validate_expected(expected: tuple[ExpectedDeletionFact, ...]) -> None:
    _validate_identities(tuple(item.target for item in expected))


def _validate_relationship_limit(relationship_limit: int) -> None:
    if not 1 <= relationship_limit <= MAX_INCIDENT_RELATIONSHIPS:
        raise ValueError("relationship inventory limit is outside approved bounds")


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1_024 or "\x00" in value:
        raise RuntimeError(f"{field} is invalid")
    return value


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field)


def _nonnegative_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError(f"{field} is invalid")
    return value


def _labels(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuntimeError("node labels are invalid")
    result = tuple(sorted(value))
    if len(set(result)) != len(result):
        raise RuntimeError("node labels are duplicated")
    return result


def _protected_parameter(value: ProtectedEvidence) -> dict[str, object]:
    relationship = value.relationship
    endpoint = relationship.other_endpoint
    return {
        "selected_source_record_pk": value.selected_source_record_pk,
        "relationship_element_id": relationship.relationship_element_id,
        "relationship_type": relationship.relationship_type,
        "direction": relationship.direction,
        "other_element_id": endpoint.element_id,
        "other_labels": list(endpoint.labels),
        "other_source_record_pk": endpoint.source_record_pk,
        "other_person_id": endpoint.person_id,
        "other_identifier_type": endpoint.identifier_type,
        "other_identifier_value": endpoint.identifier_value,
        "other_source_key": endpoint.source_key,
        "other_review_case_id": endpoint.review_case_id,
        "other_match_decision_id": endpoint.match_decision_id,
    }


def _protected_from_row(value: Mapping[str, object]) -> ProtectedEvidence:
    pk = _required_text(value.get("selected_source_record_pk"), "selected_source_record_pk")
    if value.get("relationship_type") is None or value.get("other_element_id") is None:
        raise RuntimeError("protected relationship no longer exists")
    return ProtectedEvidence(
        pk,
        IncidentRelationship(
            _required_text(value.get("relationship_element_id"), "relationship_element_id"),
            cast(
                Literal["inbound", "outbound", "self"],
                _required_text(value.get("direction"), "direction"),
            ),
            _required_text(value.get("relationship_type"), "relationship_type"),
            EndpointIdentity(
                _required_text(value.get("other_element_id"), "other_element_id"),
                _labels(value.get("other_labels")),
                _optional_text(value.get("other_source_record_pk"), "other_source_record_pk"),
                _optional_text(value.get("other_person_id"), "other_person_id"),
                _optional_text(value.get("other_identifier_type"), "other_identifier_type"),
                _optional_text(value.get("other_identifier_value"), "other_identifier_value"),
                _optional_text(value.get("other_source_key"), "other_source_key"),
                _optional_text(value.get("other_review_case_id"), "other_review_case_id"),
                _optional_text(value.get("other_match_decision_id"), "other_match_decision_id"),
            ),
        ),
    )
