"""Neo4j persistence and strict readers for immutable CRM tenant mapping revisions."""

from __future__ import annotations

from neo4j import ManagedTransaction

from src.crm_tenant_mapping_contracts import (
    CrmTenantActiveMappingHead,
    CrmTenantMappingManifest,
    CrmTenantMappingRollbackProvenance,
    CrmTenantMappingScope,
)
from src.crm_tenant_mapping_identity import mapping_revision_id
from src.crm_tenant_mapping_models import (
    CrmTenantMappingConflictError,
    CrmTenantMappingExpectedHeadBoundary,
    CrmTenantMappingIntegrityError,
    CrmTenantMappingPrepareCommand,
    CrmTenantMappingRejectCommand,
    CrmTenantMappingRevisionSnapshot,
    CrmTenantMappingRollbackCommand,
)
from src.graph.client import Neo4jClient
from src.graph.crm_tenant_mapping_graph_values import (
    _record_values,
    _required_int,
    _required_str,
    _scope_parameters,
)
from src.graph.crm_tenant_mapping_read import (
    _find_by_request,
    _read_by_id,
    _read_snapshot,
)
from src.graph.crm_tenant_mapping_read_boundaries import (
    _assert_expected_head,
    _read_active_head,
    _validate_entities,
)
from src.graph.crm_tenant_mapping_write import (
    _persistence_components,
    _require_replay,
    _revision_properties,
    _target_keys,
)
from src.graph.queries.crm_tenant_mapping import (
    ALLOCATE_REVISION_NUMBER,
    CHECK_REVISION_ID,
    CREATE_ENTRIES,
    CREATE_REVISION,
    CREATE_TARGETS,
    LOCK_SCOPE,
    REJECT_REVISION,
)
from src.graph.standalone_crm_lane_a_migration import assert_standalone_crm_lane_a_ready
from src.standalone_crm_census_requests import (
    MappingPrepareAuthority,
    MappingRollbackAuthority,
    SourceSyncAuthority,
)


class Neo4jCrmTenantMappingRepository:
    """All mapping writes are one transaction; this class intentionally has no activation writer."""

    def __init__(self, client: Neo4jClient) -> None:
        self._client = client

    def find_by_preparation_request(
        self, scope: CrmTenantMappingScope, preparation_request_id: str
    ) -> CrmTenantMappingRevisionSnapshot | None:
        assert_standalone_crm_lane_a_ready(self._client)
        return self._client.execute_read(
            lambda tx: _find_by_request(tx, scope, preparation_request_id)
        )

    def get_revision(
        self, scope: CrmTenantMappingScope, revision_id: str, manifest_digest: str
    ) -> CrmTenantMappingRevisionSnapshot | None:
        assert_standalone_crm_lane_a_ready(self._client)
        return self._client.execute_read(
            lambda tx: _read_snapshot(tx, scope, revision_id, manifest_digest)
        )

    def get_active_head(self, scope: CrmTenantMappingScope) -> CrmTenantActiveMappingHead | None:
        assert_standalone_crm_lane_a_ready(self._client)
        return self._client.execute_read(lambda tx: _read_active_head(tx, scope))

    def get_active_revision(
        self, scope: CrmTenantMappingScope
    ) -> CrmTenantMappingRevisionSnapshot | None:
        assert_standalone_crm_lane_a_ready(self._client)

        def work(tx: ManagedTransaction) -> CrmTenantMappingRevisionSnapshot | None:
            head = _read_active_head(tx, scope)
            if head is None:
                return None
            snapshot = _read_snapshot(
                tx, scope, head.active_revision_id, head.active_manifest_digest
            )
            if snapshot is None:
                raise CrmTenantMappingIntegrityError(
                    "active mapping head references a missing revision"
                )
            if (
                snapshot.revision.state != "active"
                or snapshot.revision.revision_number != head.active_revision_number
            ):
                raise CrmTenantMappingIntegrityError("active mapping head revision is malformed")
            return snapshot

        return self._client.execute_read(work)

    def prepare(self, command: CrmTenantMappingPrepareCommand) -> CrmTenantMappingRevisionSnapshot:
        assert_standalone_crm_lane_a_ready(self._client)

        def work(tx: ManagedTransaction) -> CrmTenantMappingRevisionSnapshot:
            existing = _find_by_request(tx, command.scope, command.preparation_request_id)
            if existing is not None:
                _require_replay(existing, command.request_fingerprint)
                return existing
            _assert_expected_head(tx, command.scope, command.expected_head_boundary)
            _validate_entities(tx, _target_keys(command.manifest))
            existing = _lock_and_recheck(tx, command)
            if existing is not None:
                return existing
            return _persist_prepared(tx, command, None)

        return self._client.execute_write(work)

    def rollback(
        self, command: CrmTenantMappingRollbackCommand
    ) -> CrmTenantMappingRevisionSnapshot:
        assert_standalone_crm_lane_a_ready(self._client)

        def work(tx: ManagedTransaction) -> CrmTenantMappingRevisionSnapshot:
            existing = _find_by_request(tx, command.scope, command.preparation_request_id)
            if existing is not None:
                _require_replay(existing, command.request_fingerprint)
                return existing
            _assert_expected_head(tx, command.scope, command.expected_head_boundary)
            historical = _read_snapshot(
                tx,
                command.scope,
                command.rollback_of_revision_id,
                command.rollback_of_manifest_digest,
            )
            if historical is None or historical.revision.state not in {"active", "superseded"}:
                raise CrmTenantMappingConflictError("rollback target was never effective")
            expected = command.expected_head_boundary.expected_head
            if (
                expected is None
                or historical.revision.revision_number >= expected.active_revision_number
            ):
                raise CrmTenantMappingConflictError(
                    "rollback target must be lower than current active revision"
                )
            _validate_entities(tx, _target_keys(historical.manifest))
            existing = _lock_and_recheck(tx, command)
            if existing is not None:
                return existing
            historical = _read_snapshot(
                tx,
                command.scope,
                command.rollback_of_revision_id,
                command.rollback_of_manifest_digest,
            )
            if historical is None or historical.revision.state not in {"active", "superseded"}:
                raise CrmTenantMappingConflictError("rollback target was never effective")
            expected = command.expected_head_boundary.expected_head
            if (
                expected is None
                or historical.revision.revision_number >= expected.active_revision_number
            ):
                raise CrmTenantMappingConflictError(
                    "rollback target must be lower than current active revision"
                )
            provenance = CrmTenantMappingRollbackProvenance(
                historical.revision.revision_id,
                historical.revision.revision_number,
                historical.revision.manifest_digest,
            )
            return _persist_prepared(tx, command, provenance, historical.manifest)

        return self._client.execute_write(work)

    def reject(self, command: CrmTenantMappingRejectCommand) -> CrmTenantMappingRevisionSnapshot:
        assert_standalone_crm_lane_a_ready(self._client)

        def work(tx: ManagedTransaction) -> CrmTenantMappingRevisionSnapshot:
            current = _read_snapshot(
                tx, command.scope, command.revision_id, command.manifest_digest
            )
            if current is None:
                raise CrmTenantMappingConflictError("mapping revision is missing")
            if current.revision.state == "rejected":
                if current.rejection_request_fingerprint == command.request_fingerprint:
                    return current
                raise CrmTenantMappingConflictError("mapping rejection metadata conflicts")
            if current.revision.state != "prepared":
                raise CrmTenantMappingConflictError(
                    "only prepared mapping revisions may be rejected"
                )
            record = tx.run(
                REJECT_REVISION,
                **_scope_parameters(command.scope),
                revision_id=command.revision_id,
                manifest_digest=command.manifest_digest,
                rejection_actor=command.rejection.actor,
                rejection_reference=command.rejection.rejection_reference,
                rejection_reason=command.rejection.reason,
                rejected_at=command.operation_time,
                authorization_actor=command.authorization.actor,
                authorization_reference=command.authorization.authorization_reference,
                authorization_digest=command.authorization.authorization_digest,
                authorized_at=command.authorization.authorized_at,
                authorization_expires_at=command.authorization.expires_at,
                rejection_request_fingerprint=command.request_fingerprint,
            ).single()
            if record is None:
                replay = _read_snapshot(
                    tx, command.scope, command.revision_id, command.manifest_digest
                )
                if replay is not None and replay.revision.state == "rejected":
                    if replay.rejection_request_fingerprint == command.request_fingerprint:
                        return replay
                    raise CrmTenantMappingConflictError("mapping rejection metadata conflicts")
                raise CrmTenantMappingConflictError("mapping rejection conflicts")
            result = _read_snapshot(tx, command.scope, command.revision_id, command.manifest_digest)
            if (
                result is None
                or result.revision.state != "rejected"
                or result.rejection_request_fingerprint != command.request_fingerprint
            ):
                raise CrmTenantMappingIntegrityError("mapping rejection readback is malformed")
            return result

        return self._client.execute_write(work)

    def require_prepared_for_materialization(
        self,
        scope: CrmTenantMappingScope,
        revision_id: str,
        manifest_digest: str,
        expected_head_boundary: CrmTenantMappingExpectedHeadBoundary,
    ) -> CrmTenantMappingRevisionSnapshot:
        snapshot = self.get_revision(scope, revision_id, manifest_digest)
        if snapshot is None or snapshot.revision.state != "prepared":
            raise CrmTenantMappingConflictError(
                "materialization requires an exact prepared mapping revision"
            )
        if snapshot.expected_head_boundary != expected_head_boundary:
            raise CrmTenantMappingConflictError("prepared mapping expected-head boundary conflicts")
        return snapshot

    def validate_source_sync(
        self, scope: CrmTenantMappingScope, authority: SourceSyncAuthority
    ) -> None:
        head = self.get_active_head(scope)
        if head is None or (head.head_id, head.active_manifest_digest) != (
            authority.mapping_head_id,
            authority.mapping_head_digest,
        ):
            raise CrmTenantMappingConflictError("source-sync mapping authority is stale")
        active = self.get_active_revision(scope)
        if active is None:
            raise CrmTenantMappingIntegrityError("active mapping head has no active revision")

    def validate_mapping_prepare(
        self, scope: CrmTenantMappingScope, authority: MappingPrepareAuthority
    ) -> None:
        snapshot = self._prepared_by_id(
            scope, authority.prepared_revision_id, authority.prepared_revision_digest
        )
        if snapshot.expected_head_boundary.head_id != authority.expected_current_head_id:
            raise CrmTenantMappingConflictError("mapping prepare authority head ID conflicts")
        self._assert_current_boundary(scope, snapshot.expected_head_boundary)

    def validate_mapping_rollback(
        self, scope: CrmTenantMappingScope, authority: MappingRollbackAuthority
    ) -> None:
        snapshot = self._prepared_by_id(scope, authority.rollback_head_id, None)
        provenance = snapshot.revision.rollback_provenance
        if provenance is None or (
            provenance.rollback_of_revision_id,
            provenance.rollback_of_manifest_digest,
        ) != (authority.target_revision_id, authority.target_revision_digest):
            raise CrmTenantMappingConflictError("mapping rollback provenance conflicts")
        if snapshot.expected_head_boundary.head_id != authority.expected_current_head_id:
            raise CrmTenantMappingConflictError("mapping rollback authority head ID conflicts")
        expected = snapshot.expected_head_boundary.expected_head
        if expected is None:
            raise CrmTenantMappingIntegrityError(
                "rollback prepared revision lacks current-head boundary"
            )
        historical = self.get_revision(
            scope,
            provenance.rollback_of_revision_id,
            provenance.rollback_of_manifest_digest,
        )
        if historical is None or historical.revision.state not in {"active", "superseded"}:
            raise CrmTenantMappingConflictError(
                "mapping rollback target is not prior effective history"
            )
        if (
            historical.revision.revision_number != provenance.rollback_of_revision_number
            or historical.revision.revision_number >= expected.active_revision_number
        ):
            raise CrmTenantMappingConflictError("mapping rollback provenance revision is malformed")
        self._assert_current_boundary(scope, snapshot.expected_head_boundary)

    def _prepared_by_id(
        self, scope: CrmTenantMappingScope, revision_id: str, digest: str | None
    ) -> CrmTenantMappingRevisionSnapshot:
        assert_standalone_crm_lane_a_ready(self._client)

        def work(tx: ManagedTransaction) -> CrmTenantMappingRevisionSnapshot:
            result = _read_by_id(tx, scope, revision_id)
            if (
                result is None
                or result.revision.state != "prepared"
                or (digest is not None and result.revision.manifest_digest != digest)
            ):
                raise CrmTenantMappingConflictError(
                    "exact prepared mapping revision is unavailable"
                )
            return result

        return self._client.execute_read(work)

    def _assert_current_boundary(
        self, scope: CrmTenantMappingScope, boundary: CrmTenantMappingExpectedHeadBoundary
    ) -> None:
        assert_standalone_crm_lane_a_ready(self._client)
        self._client.execute_read(lambda tx: _assert_expected_head(tx, scope, boundary))


def _persist_prepared(
    tx: ManagedTransaction,
    command: CrmTenantMappingPrepareCommand | CrmTenantMappingRollbackCommand,
    provenance: CrmTenantMappingRollbackProvenance | None,
    manifest: CrmTenantMappingManifest | None = None,
) -> CrmTenantMappingRevisionSnapshot:
    effective_manifest = (
        command.manifest if isinstance(command, CrmTenantMappingPrepareCommand) else manifest
    )
    if effective_manifest is None:
        raise CrmTenantMappingIntegrityError("rollback manifest is missing")
    _assert_expected_head(tx, command.scope, command.expected_head_boundary)
    number_record = tx.run(ALLOCATE_REVISION_NUMBER, **_scope_parameters(command.scope)).single()
    number = _required_int(_record_values(number_record), "revision_number")
    revision_id = mapping_revision_id(command.scope, number)
    collision = tx.run(CHECK_REVISION_ID, revision_id=revision_id).single()
    if _required_int(_record_values(collision), "revision_count") != 0:
        raise CrmTenantMappingConflictError("deterministic mapping revision ID collides")
    revision_properties = _revision_properties(
        command, effective_manifest, revision_id, number, provenance
    )
    created = tx.run(CREATE_REVISION, revision_properties=revision_properties).single()
    if _required_str(_record_values(created), "revision_id") != revision_id:
        raise CrmTenantMappingIntegrityError("mapping revision persistence failed")
    entries, targets = _persistence_components(revision_id, effective_manifest)
    if entries:
        entry_record = tx.run(CREATE_ENTRIES, revision_id=revision_id, entries=entries).single()
        if _required_int(_record_values(entry_record), "entry_count") != len(entries):
            raise CrmTenantMappingIntegrityError("mapping entry persistence failed")
    if targets:
        target_record = tx.run(CREATE_TARGETS, targets=targets).single()
        if _required_int(_record_values(target_record), "target_count") != len(targets):
            raise CrmTenantMappingIntegrityError("mapping target persistence failed")
    snapshot = _read_snapshot(tx, command.scope, revision_id, effective_manifest.digest)
    if snapshot is None or snapshot.request_fingerprint != command.request_fingerprint:
        raise CrmTenantMappingIntegrityError("mapping prepared revision readback is malformed")
    return snapshot


def _lock_and_recheck(
    tx: ManagedTransaction,
    command: CrmTenantMappingPrepareCommand | CrmTenantMappingRollbackCommand,
) -> CrmTenantMappingRevisionSnapshot | None:
    """Serialize one scope, then classify replay and stale boundaries before allocation."""
    lock = tx.run(LOCK_SCOPE, **_scope_parameters(command.scope)).single()
    _required_int(_record_values(lock), "serialization_version")
    existing = _find_by_request(tx, command.scope, command.preparation_request_id)
    if existing is not None:
        _require_replay(existing, command.request_fingerprint)
        return existing
    _assert_expected_head(tx, command.scope, command.expected_head_boundary)
    return None
