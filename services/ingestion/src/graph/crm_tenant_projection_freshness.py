"""Neo4j strict projection authority adapter used by standalone CRM census."""

from __future__ import annotations

from neo4j import ManagedTransaction

from src.crm_tenant_projection_models import CrmTenantProjectionConflictError
from src.graph.client import Neo4jClient
from src.graph.queries.crm_tenant_projection_freshness import (
    VALIDATE_MAPPING_ACTIVATION_PROJECTION,
    VALIDATE_SOURCE_SYNC_PROJECTION,
)
from src.graph.standalone_crm_lane_a_migration import assert_standalone_crm_lane_a_ready
from src.standalone_crm_census_requests import (
    MappingPrepareCensusRequest,
    MappingRollbackCensusRequest,
    SourceSyncCensusRequest,
    mapping_candidate_identity,
)


class Neo4jCrmTenantProjectionFreshnessAuthority:
    """Reject stale or incomplete exact projection authority; never chooses latest."""

    def __init__(self, client: Neo4jClient) -> None:
        self._client = client

    def validate_source_sync(self, request: SourceSyncCensusRequest) -> None:
        assert_standalone_crm_lane_a_ready(self._client)
        authority = request.authority
        if (
            authority.mapping_active_revision_id is None
            or authority.mapping_active_revision_number is None
            or authority.projection_active_release_id is None
            or authority.projection_active_release_number is None
        ):
            raise CrmTenantProjectionConflictError(
                "source-sync authority requires complete active-head identities"
            )
        record = self._client.execute_read(
            lambda tx: _one(
                tx,
                VALIDATE_SOURCE_SYNC_PROJECTION,
                source_key=request.source_key,
                source_instance_id=request.source_instance_id,
                control_instance_id=request.control_instance_id,
                projection_head_id=authority.projection_head_id,
                projection_head_digest=authority.projection_head_digest,
                projection_active_release_id=authority.projection_active_release_id,
                projection_active_release_number=authority.projection_active_release_number,
            )
        )
        if record is None:
            raise CrmTenantProjectionConflictError("source-sync projection authority is stale")

    def validate_mapping_activation(
        self, request: MappingPrepareCensusRequest | MappingRollbackCensusRequest
    ) -> None:
        authority = request.authority
        if authority.completed_release_id is None:
            raise CrmTenantProjectionConflictError(
                "legacy mapping activation authority is insufficient"
            )
        candidate_id, candidate_digest = mapping_candidate_identity(authority)
        assert_standalone_crm_lane_a_ready(self._client)
        record = self._client.execute_read(
            lambda tx: _one(
                tx,
                VALIDATE_MAPPING_ACTIVATION_PROJECTION,
                source_key=request.source_key,
                source_instance_id=request.source_instance_id,
                control_instance_id=request.control_instance_id,
                completed_release_id=authority.completed_release_id,
                completed_release_fingerprint=authority.completed_release_fingerprint,
                candidate_revision_id=candidate_id,
                candidate_manifest_digest=candidate_digest,
                expected_projection_head_id=authority.expected_projection_head_id,
                expected_projection_active_release_id=authority.expected_projection_active_release_id,
                expected_projection_active_release_number=authority.expected_projection_active_release_number,
                expected_projection_active_release_fingerprint=(
                    authority.expected_projection_active_release_fingerprint
                ),
            )
        )
        if record is None:
            raise CrmTenantProjectionConflictError(
                "mapping activation projection authority is stale"
            )


def _one(tx: ManagedTransaction, query: str, **parameters: object) -> object | None:
    records = list(tx.run(query, parameters))
    if len(records) > 1:
        raise CrmTenantProjectionConflictError("projection authority is not unique")
    return None if not records else records[0]
