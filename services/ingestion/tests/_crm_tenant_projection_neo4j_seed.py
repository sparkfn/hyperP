"""Non-collected mapping and membership seed fixtures for Issue #305 Neo4j tests."""

from __future__ import annotations

from typing import Literal

from neo4j import Driver, Session
from src.connectors.bitrix_openlines.models import CrmCompanyBindingPayload
from src.crm_company_contracts import CrmCompanyMembershipSnapshotRecord
from src.crm_identity_associations import normalize_company_membership_snapshot
from src.crm_tenant_mapping_contracts import (
    CrmTenantMappingAuthorization,
    CrmTenantMappingCompanyEntry,
    CrmTenantMappingManifest,
    CrmTenantMappingTarget,
)
from src.crm_tenant_mapping_identity import mapping_head_id, mapping_revision_id
from src.crm_tenant_mapping_models import (
    CrmTenantMappingExpectedHeadBoundary,
    CrmTenantMappingPrepareCommand,
)
from src.crm_tenant_projection_records import CrmTenantProjectionScope
from src.crm_tenant_projection_records import _digest as _projection_digest
from src.graph.crm_tenant_mapping_write import _persistence_components, _revision_properties
from src.standalone_crm_census_requests import (
    SourceSyncAuthority,
    SourceSyncCensusRequest,
    StandaloneCrmBudget,
    canonical_request_payload,
)
from src.standalone_crm_child_contracts import (
    StandaloneCrmSourceAvailability,
    StandaloneCrmSourceChildScope,
)

_DIGEST = "sha256:" + "a" * 64
_AVAILABLE_AT = "2026-08-29T00:00:00Z"


def _scope() -> CrmTenantProjectionScope:
    return CrmTenantProjectionScope("bitrix_chat", "issue-305-portal", "issue-305-control")


def _request_json() -> str:
    scope = _scope()
    return canonical_request_payload(
        SourceSyncCensusRequest(
            scope.source_key,
            scope.source_instance_id,
            scope.control_instance_id,
            "issue-305-occurrence",
            ("contact", "lead", "company"),
            StandaloneCrmBudget(2, 10, 60, 2, 10, 1, "2026-08-30T00:00:00Z"),
            "policy-a",
            "association-a",
            "configuration-a",
            SourceSyncAuthority(
                "mapping-head", "mapping-digest", "projection-head", "projection-digest"
            ),
        )
    )


def _mapping_manifest(
    entries: tuple[CrmTenantMappingCompanyEntry, ...] | None = None,
) -> CrmTenantMappingManifest:
    return CrmTenantMappingManifest(
        _scope().mapping_scope,
        entries
        if entries is not None
        else (CrmTenantMappingCompanyEntry("303", (CrmTenantMappingTarget("issue-305-entity"),)),),
    )


def _mapping_revision_id() -> str:
    return mapping_revision_id(_scope().mapping_scope, 1)


def _snapshot_record(
    subject_kind: Literal["contact", "lead"],
    subject_id: str,
    source_record_id: str,
    with_company: bool,
) -> CrmCompanyMembershipSnapshotRecord:
    scope = _scope()
    bindings = (CrmCompanyBindingPayload("303", None, None, True),) if with_company else ()
    snapshot = normalize_company_membership_snapshot(
        subject_type=subject_kind, subject_id=subject_id, payloads=bindings
    )
    return CrmCompanyMembershipSnapshotRecord(
        StandaloneCrmSourceChildScope(
            scope.source_key, scope.source_instance_id, scope.control_instance_id
        ),
        snapshot,
        source_record_id,
        "1",
        1,
        _DIGEST,
        None,
        StandaloneCrmSourceAvailability(_AVAILABLE_AT),
        len(bindings),
    )


def _contact_snapshot_id() -> str:
    return _snapshot_record("contact", "101", "issue-305-contact-source", True).snapshot_id


def _observation_id(
    snapshot_id: str, company_id: str, sort: int | None, role_id: str | None, is_primary: bool
) -> str:
    return _projection_digest(
        "crm-company-membership-observation-v1",
        [snapshot_id, company_id, sort, role_id, is_primary],
    )


def _mapping_properties(
    manifest: CrmTenantMappingManifest | None = None,
) -> tuple[dict[str, object], list[dict[str, object]], list[dict[str, object]]]:
    scope = _scope()
    effective_manifest = _mapping_manifest() if manifest is None else manifest
    command = CrmTenantMappingPrepareCommand(
        scope.mapping_scope,
        "issue-305-mapping-prepare",
        effective_manifest,
        CrmTenantMappingExpectedHeadBoundary(
            scope.mapping_scope, mapping_head_id(scope.mapping_scope), None
        ),
        CrmTenantMappingAuthorization(
            "issue-305-reviewer",
            "issue-305-approval",
            _DIGEST,
            _AVAILABLE_AT,
            "2026-08-30T00:00:00Z",
        ),
        _AVAILABLE_AT,
    )
    revision_id = _mapping_revision_id()
    entries, targets = _persistence_components(revision_id, effective_manifest)
    return _revision_properties(command, effective_manifest, revision_id, 1, None), entries, targets


def _seed(driver: Driver, manifest: CrmTenantMappingManifest | None = None) -> None:
    scope = _scope()
    mapping_properties, entries, targets = _mapping_properties(manifest)
    with driver.session() as session:
        session.run(
            """
            CREATE (census:StandaloneCrmCensus {census_id: 'issue-305-census',
              source_key: $source_key, source_instance_id: $source_instance_id,
              control_instance_id: $control_instance_id, census_kind: 'source_sync',
              status: 'completed', fingerprint: $digest, request_json: $request_json,
              expected_units: 3, completed_units: 2, failed_units: 0, cancelled_units: 0,
              no_work_units: 1, processed_rows: 2, skipped_rows: 0,
              created_at: datetime($available_at)})
            CREATE (:StandaloneCrmCensusUnit {census_id: 'issue-305-census',
              stream_kind: 'contact', state: 'completed', generation: 1, frozen_upper_id: 101})
            CREATE (:StandaloneCrmCensusUnit {census_id: 'issue-305-census',
              stream_kind: 'lead', state: 'completed', generation: 1, frozen_upper_id: 102})
            CREATE (:StandaloneCrmCensusUnit {census_id: 'issue-305-census',
              stream_kind: 'company', state: 'no_work', generation: 1, frozen_upper_id: 0})
            CREATE (:StandaloneCrmCensusCheckpoint {census_id: 'issue-305-census',
              stream_kind: 'contact', generation: 1, frozen_upper_id: 101,
              last_committed_id: 101, processed_rows: 1, skipped_rows: 0})
            CREATE (:StandaloneCrmCensusCheckpoint {census_id: 'issue-305-census',
              stream_kind: 'lead', generation: 1, frozen_upper_id: 102,
              last_committed_id: 102, processed_rows: 1, skipped_rows: 0})
            CREATE (revision:CrmTenantMappingRevision $mapping_properties)
            CREATE (entity:Entity {entity_key: 'issue-305-entity'})
            WITH revision, entity UNWIND $entries AS item
            CREATE (entry:CrmTenantMappingEntry {revision_id: revision.revision_id,
              entry_id: item.entry_id, company_id: item.company_id})
            CREATE (revision)-[:HAS_MAPPING_ENTRY]->(entry)
            WITH entity UNWIND $targets AS item
            MATCH (entry:CrmTenantMappingEntry {entry_id: item.entry_id})
            CREATE (target:CrmTenantMappingTarget {entry_id: item.entry_id,
              target_id: item.target_id, entity_key: item.entity_key,
              relationship_kind: item.relationship_kind})
            CREATE (entry)-[:HAS_MAPPING_TARGET]->(target)-[:TARGETS_ENTITY]->(entity)
            """,
            source_key=scope.source_key,
            source_instance_id=scope.source_instance_id,
            control_instance_id=scope.control_instance_id,
            request_json=_request_json(),
            available_at=_AVAILABLE_AT,
            digest=_DIGEST,
            mapping_properties=mapping_properties,
            entries=entries,
            targets=targets,
        ).consume()
        _seed_snapshot(session, "contact", "101", "issue-305-contact", True)
        _seed_snapshot(session, "lead", "102", "issue-305-lead", False)


def _seed_snapshot(
    session: Session,
    subject_kind: Literal["contact", "lead"],
    subject_id: str,
    prefix: str,
    with_company: bool,
) -> None:
    scope = _scope()
    record = _snapshot_record(subject_kind, subject_id, f"{prefix}-source", with_company)
    session.run(
        """
        CREATE (head:CrmCompanyMembershipHead {source_instance_id: $source_instance_id,
          control_instance_id: $control_instance_id, subject_kind: $subject_kind,
          subject_id: $subject_id, selected_snapshot_id: $snapshot_id,
          available_at: datetime($available_at), source_record_version: 1, source_record_pk: '1'})
        CREATE (snapshot:CrmCompanyMembershipSnapshot {snapshot_id: $snapshot_id,
          fixture_snapshot_id: $fixture_snapshot_id, snapshot_digest: $digest,
          source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
          subject_kind: $subject_kind, subject_id: $subject_id, source_record_id: $source_record_id,
          source_record_pk: $source_record_pk, source_record_version: $source_record_version,
          source_record_hash: $source_record_hash, observed_at: null,
          available_at: datetime($available_at), binding_count: $binding_count,
          contract_version: $contract_version})
        CREATE (head)-[:SELECTS_MEMBERSHIP_SNAPSHOT]->(snapshot)
        """,
        source_instance_id=scope.source_instance_id,
        control_instance_id=scope.control_instance_id,
        subject_kind=subject_kind,
        subject_id=subject_id,
        snapshot_id=record.snapshot_id,
        fixture_snapshot_id=f"{prefix}-snapshot",
        digest=record.snapshot_digest,
        available_at=_AVAILABLE_AT,
        source_record_id=record.source_record_id,
        source_record_pk=record.source_record_pk,
        source_record_version=record.source_record_version,
        source_record_hash=record.source_record_hash,
        binding_count=record.binding_count,
        contract_version=record.contract_version,
    ).consume()
    if with_company:
        _add_membership_observation(
            session, record.snapshot_id, subject_kind, subject_id, "303", True
        )


def _add_membership_observation(
    session: Session,
    snapshot_id: str,
    subject_kind: str,
    subject_id: str,
    company_id: str,
    is_primary: bool,
) -> None:
    scope = _scope()
    session.run(
        """
        MATCH (snapshot:CrmCompanyMembershipSnapshot {snapshot_id: $snapshot_id})
        CREATE (reference:CrmCompanyReference {source_key: $source_key,
          source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
          company_id: $company_id})
        CREATE (observation:CrmCompanyMembershipObservation {snapshot_id: $snapshot_id,
          fixture_snapshot_id: $fixture_snapshot_id, company_id: $company_id,
          observation_id: $observation_id, subject_kind: $subject_kind,
          subject_id: $subject_id, is_primary: $is_primary})
        CREATE (snapshot)-[:HAS_MEMBERSHIP_OBSERVATION]->(observation)
        CREATE (observation)-[:REFERENCES_COMPANY]->(reference)
        """,
        snapshot_id=snapshot_id,
        fixture_snapshot_id="issue-305-contact-snapshot",
        source_key=scope.source_key,
        source_instance_id=scope.source_instance_id,
        control_instance_id=scope.control_instance_id,
        company_id=company_id,
        observation_id=_observation_id(snapshot_id, company_id, None, None, is_primary),
        subject_kind=subject_kind,
        subject_id=subject_id,
        is_primary=is_primary,
    ).consume()
