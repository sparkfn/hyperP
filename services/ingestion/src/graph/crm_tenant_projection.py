"""Neo4j repository facade for bounded immutable CRM tenant projection releases."""

from __future__ import annotations

from neo4j import ManagedTransaction

from src.crm_tenant_projection_identity import projection_release_id
from src.crm_tenant_projection_models import (
    CrmTenantProjectionConflictError,
    CrmTenantProjectionFailureCode,
    CrmTenantProjectionIntegrityError,
    CrmTenantProjectionMaterializationCommand,
    CrmTenantProjectionReleaseSummary,
)
from src.crm_tenant_projection_records import CrmTenantProjectionScope
from src.crm_tenant_projection_repository import CrmTenantProjectionRepository
from src.graph.client import Neo4jClient
from src.graph.crm_tenant_projection_boundaries import (
    _find_by_request,
    _release_properties,
    _validate_mapping_boundary,
    _validate_release_boundary,
)
from src.graph.crm_tenant_projection_census import _validate_source_census
from src.graph.crm_tenant_projection_release_validation_bounded import (
    _validate_release_topology_bounded,
)
from src.graph.crm_tenant_projection_values import (
    _read_release,
    _require_building,
    _require_page_limit,
    _required_int,
    _summary_from_record,
)
from src.graph.crm_tenant_projection_write import (
    _cancel_release,
    _capture_page,
    _complete_release,
    _fail_release,
    _project_page,
)
from src.graph.queries.crm_tenant_projection import (
    CHECK_RELEASE_ID,
    CREATE_RELEASE,
    LOCK_SCOPE,
)
from src.graph.queries.crm_tenant_projection_integrity import READ_COMPLETED
from src.graph.standalone_crm_lane_a_migration import assert_standalone_crm_lane_a_ready


class Neo4jCrmTenantProjectionRepository(CrmTenantProjectionRepository):
    """Persist releases only; active heads remain read-only authority boundaries."""

    def __init__(self, client: Neo4jClient) -> None:
        self._client = client

    def allocate_or_replay(
        self, command: CrmTenantProjectionMaterializationCommand
    ) -> CrmTenantProjectionReleaseSummary:
        assert_standalone_crm_lane_a_ready(self._client)

        def work(tx: ManagedTransaction) -> CrmTenantProjectionReleaseSummary:
            existing = _find_by_request(tx, command)
            if existing is not None:
                return _replay_existing(tx, existing)
            counter = tx.run(
                LOCK_SCOPE,
                source_key=command.scope.source_key,
                source_instance_id=command.scope.source_instance_id,
                control_instance_id=command.scope.control_instance_id,
            ).single()
            if counter is None:
                raise CrmTenantProjectionIntegrityError(
                    "projection scope counter could not be locked"
                )
            existing = _find_by_request(tx, command)
            if existing is not None:
                return _replay_existing(tx, existing)
            boundary = _validate_source_census(tx, command)
            mapping_proof = _validate_mapping_boundary(tx, command)
            release_number = _required_int(counter, "next_release_number")
            release_id = projection_release_id(command.scope, release_number)
            collision = tx.run(CHECK_RELEASE_ID, release_id=release_id).single()
            if collision is None or _required_int(collision, "release_count") != 0:
                raise CrmTenantProjectionConflictError(
                    "deterministic projection release ID collides"
                )
            expected = command.expected_mapping_head_boundary.expected_head
            created = tx.run(
                CREATE_RELEASE,
                source_key=command.scope.source_key,
                source_instance_id=command.scope.source_instance_id,
                control_instance_id=command.scope.control_instance_id,
                source_census_id=command.source_census_id,
                source_census_fingerprint=command.source_census_fingerprint,
                mapping_revision_id=command.mapping_revision_id,
                mapping_manifest_digest=command.mapping_manifest_digest,
                expected_mapping_head_id=command.expected_mapping_head_id,
                expected_mapping_head_digest=command.expected_mapping_head_digest,
                expected_mapping_head_present=expected is not None,
                expected_mapping_active_revision_id=(
                    None if expected is None else expected.active_revision_id
                ),
                expected_mapping_active_revision_number=(
                    None if expected is None else expected.active_revision_number
                ),
                release_number=release_number,
                projection_head_id=command.projection_head_id,
                contact_unit_state=boundary.contact.state,
                contact_unit_generation=boundary.contact.generation,
                contact_checkpoint_generation=boundary.contact.checkpoint_generation,
                contact_frozen_upper_id=boundary.contact.frozen_upper_id,
                lead_unit_state=boundary.lead.state,
                lead_unit_generation=boundary.lead.generation,
                lead_checkpoint_generation=boundary.lead.checkpoint_generation,
                lead_frozen_upper_id=boundary.lead.frozen_upper_id,
                expected_prior_head_present=command.expected_prior_head is not None,
                expected_prior_head_id=(
                    None
                    if command.expected_prior_head is None
                    else command.expected_prior_head.head_id
                ),
                expected_prior_release_id=(
                    None
                    if command.expected_prior_head is None
                    else command.expected_prior_head.active_release_id
                ),
                expected_prior_release_number=(
                    None
                    if command.expected_prior_head is None
                    else command.expected_prior_head.active_release_number
                ),
                expected_prior_release_fingerprint=(
                    None
                    if command.expected_prior_head is None
                    else command.expected_prior_head.active_release_fingerprint
                ),
                properties=_release_properties(
                    command,
                    boundary,
                    mapping_proof,
                    release_id,
                    release_number,
                ),
            ).single()
            if created is None:
                raise CrmTenantProjectionConflictError(
                    "projection allocation boundary became stale"
                )
            return _summary_from_record(created)

        result = self._client.execute_write(work)
        if result.state == "completed":
            _validate_release_topology_bounded(self._client, result)
        return result

    def capture_page(
        self,
        release_id: str,
        release_fingerprint: str,
        page_limit: int,
    ) -> CrmTenantProjectionReleaseSummary:
        assert_standalone_crm_lane_a_ready(self._client)
        _require_page_limit(page_limit)
        return self._client.execute_write(
            lambda tx: _capture_page(tx, release_id, release_fingerprint, page_limit)
        )

    def project_page(
        self,
        release_id: str,
        release_fingerprint: str,
        page_limit: int,
    ) -> CrmTenantProjectionReleaseSummary:
        assert_standalone_crm_lane_a_ready(self._client)
        _require_page_limit(page_limit)
        return self._client.execute_write(
            lambda tx: _project_page(tx, release_id, release_fingerprint, page_limit)
        )

    def complete(
        self, release_id: str, release_fingerprint: str
    ) -> CrmTenantProjectionReleaseSummary:
        assert_standalone_crm_lane_a_ready(self._client)
        current = self._client.execute_read(lambda tx: _read_release(tx, release_id))
        if current.release_fingerprint != release_fingerprint:
            raise CrmTenantProjectionConflictError("projection release fingerprint conflicts")
        if current.state == "completed":
            _validate_release_topology_bounded(self._client, current)
            return current
        _require_building(current, release_fingerprint, "complete")
        _validate_release_topology_bounded(self._client, current)
        return self._client.execute_write(
            lambda tx: _complete_release(tx, release_id, release_fingerprint)
        )

    def cancel(
        self, release_id: str, release_fingerprint: str
    ) -> CrmTenantProjectionReleaseSummary:
        assert_standalone_crm_lane_a_ready(self._client)
        return self._client.execute_write(
            lambda tx: _cancel_release(tx, release_id, release_fingerprint)
        )

    def fail(
        self,
        release_id: str,
        release_fingerprint: str,
        failure_code: CrmTenantProjectionFailureCode,
    ) -> CrmTenantProjectionReleaseSummary:
        assert_standalone_crm_lane_a_ready(self._client)
        return self._client.execute_write(
            lambda tx: _fail_release(tx, release_id, release_fingerprint, failure_code)
        )

    def get_completed(
        self,
        scope: CrmTenantProjectionScope,
        release_id: str,
        release_fingerprint: str,
    ) -> CrmTenantProjectionReleaseSummary | None:
        assert_standalone_crm_lane_a_ready(self._client)

        def work(tx: ManagedTransaction) -> CrmTenantProjectionReleaseSummary | None:
            record = tx.run(
                READ_COMPLETED,
                source_key=scope.source_key,
                source_instance_id=scope.source_instance_id,
                control_instance_id=scope.control_instance_id,
                release_id=release_id,
                release_fingerprint=release_fingerprint,
            ).single()
            if record is None:
                return None
            result = _validate_release_boundary(tx, release_id, release_fingerprint)
            if result.scope != scope or result.state != "completed":
                raise CrmTenantProjectionIntegrityError("completed projection release is malformed")
            return result

        result = self._client.execute_read(work)
        if result is not None:
            _validate_release_topology_bounded(self._client, result)
        return result


def _replay_existing(
    tx: ManagedTransaction,
    existing: CrmTenantProjectionReleaseSummary,
) -> CrmTenantProjectionReleaseSummary:
    if existing.state in {"failed", "cancelled"}:
        return existing
    return _validate_release_boundary(tx, existing.release_id, existing.release_fingerprint)
