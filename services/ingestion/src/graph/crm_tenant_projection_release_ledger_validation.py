"""Bounded decision, association, and support ledger validation pages."""

from __future__ import annotations

from collections.abc import Mapping

from neo4j import ManagedTransaction

from src.crm_tenant_projection_identity import (
    projection_association_id,
    projection_support_digest,
    projection_support_id,
)
from src.crm_tenant_projection_models import (
    CrmTenantProjectionIntegrityError,
    CrmTenantProjectionReleaseSummary,
)
from src.crm_tenant_projection_records import _digest
from src.graph.client import Neo4jClient
from src.graph.crm_tenant_projection_integrity_values import _decision_matches
from src.graph.crm_tenant_projection_values import (
    _mapping_int,
    _mapping_optional_string,
    _mapping_string,
    _required_mapping,
    _subject_kind_value,
)
from src.graph.queries.crm_tenant_projection_release_pages import (
    READ_ASSOCIATION_PAGE,
    READ_DECISION_PAGE,
    READ_SUPPORT_PAGE,
)

_VALIDATION_PAGE_LIMIT = 200


def _validate_decisions_bounded(
    client: Neo4jClient, release: CrmTenantProjectionReleaseSummary
) -> int:
    cursor: str | None = None
    count = 0
    while True:

        def read_page(
            tx: ManagedTransaction, cursor: str | None = cursor
        ) -> list[Mapping[str, object]]:
            return _string_page(tx, READ_DECISION_PAGE, release.release_id, cursor)

        page = client.execute_read(read_page)
        for row in page:
            _validate_decision_row(row, release)
            cursor = _mapping_string(_required_mapping(row, "decision"), "input_id")
            count += 1
        if len(page) < _VALIDATION_PAGE_LIMIT:
            return count


def _validate_associations_bounded(
    client: Neo4jClient, release: CrmTenantProjectionReleaseSummary
) -> int:
    cursor: str | None = None
    count = 0
    while True:

        def read_page(
            tx: ManagedTransaction, cursor: str | None = cursor
        ) -> list[Mapping[str, object]]:
            return _string_page(tx, READ_ASSOCIATION_PAGE, release.release_id, cursor)

        page = client.execute_read(read_page)
        for row in page:
            _validate_association_row(row, release)
            cursor = _mapping_string(_required_mapping(row, "association"), "association_id")
            count += 1
        if len(page) < _VALIDATION_PAGE_LIMIT:
            return count


def _validate_supports_bounded(
    client: Neo4jClient,
    release: CrmTenantProjectionReleaseSummary,
    revision_number: int,
) -> int:
    cursor: str | None = None
    count = 0
    while True:

        def read_page(
            tx: ManagedTransaction, cursor: str | None = cursor
        ) -> list[Mapping[str, object]]:
            return _string_page(tx, READ_SUPPORT_PAGE, release.release_id, cursor)

        page = client.execute_read(read_page)
        for row in page:
            _validate_support_row(row, release, revision_number)
            cursor = _mapping_string(_required_mapping(row, "support"), "support_id")
            count += 1
        if len(page) < _VALIDATION_PAGE_LIMIT:
            return count


def _string_page(
    tx: ManagedTransaction,
    query: str,
    release_id: str,
    cursor: str | None,
) -> list[Mapping[str, object]]:
    return list(
        tx.run(query, release_id=release_id, cursor=cursor, page_limit=_VALIDATION_PAGE_LIMIT)
    )


def _validate_decision_row(
    row: Mapping[str, object], release: CrmTenantProjectionReleaseSummary
) -> None:
    decision = _required_mapping(row, "decision")
    input_node = _required_mapping(row, "input")
    snapshot = _required_mapping(row, "snapshot")
    if _mapping_int(row, "input_owner_count") != 1 or _mapping_int(row, "snapshot_count") != 1:
        raise CrmTenantProjectionIntegrityError("projection decision ownership is malformed")
    input_id = _mapping_string(decision, "input_id")
    decision_kind = _mapping_string(decision, "decision")
    reason = _mapping_optional_string(decision, "zero_target_reason")
    if (
        _mapping_string(decision, "release_id") != release.release_id
        or input_id != _mapping_string(input_node, "input_id")
        or _mapping_string(decision, "decision_digest")
        != _digest(
            "crm-tenant-projection-decision-v1",
            [release.release_id, input_id, decision_kind, reason],
        )
        or not _decision_matches(
            decision_kind,
            reason,
            _mapping_int(row, "association_count"),
            _mapping_int(snapshot, "binding_count"),
        )
    ):
        raise CrmTenantProjectionIntegrityError("projection decision is malformed")


def _validate_association_row(
    row: Mapping[str, object], release: CrmTenantProjectionReleaseSummary
) -> None:
    association = _required_mapping(row, "association")
    input_node = _required_mapping(row, "input")
    entity = _required_mapping(row, "entity")
    if _mapping_int(row, "input_owner_count") != 1 or _mapping_int(row, "entity_count") != 1:
        raise CrmTenantProjectionIntegrityError("projection association ownership is malformed")
    association_id = _mapping_string(association, "association_id")
    input_id = _mapping_string(association, "input_id")
    kind = _subject_kind_value(_mapping_string(association, "subject_kind"))
    subject_id = _mapping_string(association, "subject_id")
    entity_key = _mapping_string(association, "entity_key")
    relationship_kind = _mapping_string(association, "relationship_kind")
    if (
        _mapping_string(association, "release_id") != release.release_id
        or input_id != _mapping_string(input_node, "input_id")
        or kind != _mapping_string(input_node, "subject_kind")
        or subject_id != _mapping_string(input_node, "subject_id")
        or relationship_kind != "tenant_member"
        or entity_key != _mapping_string(entity, "entity_key")
        or association_id
        != projection_association_id(
            release.release_id, input_id, kind, subject_id, entity_key, relationship_kind
        )
        or _mapping_int(row, "support_count") < 1
    ):
        raise CrmTenantProjectionIntegrityError("projection association is malformed")


def _validate_support_row(
    row: Mapping[str, object],
    release: CrmTenantProjectionReleaseSummary,
    revision_number: int,
) -> None:
    support = _required_mapping(row, "support")
    association = _required_mapping(row, "association")
    input_node = _required_mapping(row, "input")
    snapshot = _required_mapping(row, "snapshot")
    observation = _required_mapping(row, "observation")
    target = _required_mapping(row, "target")
    entry = _required_mapping(row, "entry")
    revision = _required_mapping(row, "revision")
    entity = _required_mapping(row, "entity")
    multiplicities = (
        "association_owner_count",
        "input_owner_count",
        "snapshot_count",
        "observation_count",
        "target_count",
        "entry_count",
        "revision_count",
        "entity_count",
    )
    if any(_mapping_int(row, key) != 1 for key in multiplicities):
        raise CrmTenantProjectionIntegrityError(
            "projection support proof multiplicity is malformed"
        )
    support_id = _mapping_string(support, "support_id")
    association_id = _mapping_string(support, "association_id")
    observation_id = _mapping_string(support, "membership_observation_id")
    target_id = _mapping_string(support, "mapping_target_id")
    if support_id != projection_support_id(
        association_id, observation_id, target_id
    ) or _mapping_string(support, "support_digest") != projection_support_digest(
        release.release_id, association_id, observation_id, target_id
    ):
        raise CrmTenantProjectionIntegrityError(
            "projection support deterministic identity is malformed"
        )
    if (
        _mapping_string(support, "release_id") != release.release_id
        or association_id != _mapping_string(association, "association_id")
        or _mapping_string(observation, "observation_id") != observation_id
        or _mapping_string(observation, "snapshot_id") != _mapping_string(snapshot, "snapshot_id")
        or _mapping_string(observation, "subject_kind")
        != _mapping_string(input_node, "subject_kind")
        or _mapping_string(observation, "subject_id") != _mapping_string(input_node, "subject_id")
        or _mapping_string(target, "target_id") != target_id
        or _mapping_string(target, "entry_id") != _mapping_string(entry, "entry_id")
        or _mapping_string(target, "entity_key") != _mapping_string(entity, "entity_key")
        or _mapping_string(target, "entity_key") != _mapping_string(association, "entity_key")
        or _mapping_string(target, "relationship_kind")
        != _mapping_string(association, "relationship_kind")
        or _mapping_string(entry, "revision_id") != release.mapping_revision_id
        or _mapping_string(entry, "company_id") != _mapping_string(observation, "company_id")
        or _mapping_string(revision, "revision_id") != release.mapping_revision_id
        or _mapping_int(revision, "revision_number") != revision_number
        or _mapping_string(revision, "manifest_digest") != release.mapping_manifest_digest
        or _mapping_string(revision, "state") != "prepared"
    ):
        raise CrmTenantProjectionIntegrityError("projection support is malformed")
