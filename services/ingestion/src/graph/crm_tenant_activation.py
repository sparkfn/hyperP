"""Neo4j implementation of the #307 mapping/projection activation CAS."""

from __future__ import annotations

from collections.abc import Mapping

from neo4j import ManagedTransaction, Record, Result

from src.crm_tenant_activation_contracts import (
    CrmTenantActivationCommand,
    CrmTenantActivationReceipt,
    CrmTenantActivationRelease,
    CrmTenantActivationResult,
)
from src.crm_tenant_activation_models import (
    CrmTenantActivationConflictError,
    CrmTenantActivationIntegrityError,
)
from src.crm_tenant_mapping_contracts import CrmTenantMappingExpectedHead
from src.crm_tenant_mapping_identity import mapping_head_id
from src.crm_tenant_projection_identity import projection_head_id
from src.crm_tenant_projection_records import (
    CrmTenantProjectionExpectedHead,
    CrmTenantProjectionScope,
)
from src.graph.client import Neo4jClient
from src.graph.queries.crm_tenant_activation import (
    ACTIVATE,
    LOCK_ACTIVATION_SCOPE,
    READ_RECEIPT,
    READ_RECEIPT_BY_ID,
)
from src.graph.standalone_crm_lane_a_migration import assert_standalone_crm_lane_a_ready


class Neo4jCrmTenantActivationRepository:
    """Perform the two-head transition and durable receipt write in one transaction."""

    def __init__(self, client: Neo4jClient) -> None:
        self._client = client

    def activate(self, command: CrmTenantActivationCommand) -> CrmTenantActivationResult:
        assert_standalone_crm_lane_a_ready(self._client)

        def work(tx: ManagedTransaction) -> CrmTenantActivationResult:
            _lock_scope(tx, command)
            replay = _read_exact_receipt(tx, command)
            if replay is not None:
                return CrmTenantActivationResult(replay, True)
            row = _run_command(tx, ACTIVATE, command).single()
            if row is None:
                raise CrmTenantActivationConflictError(
                    "activation candidate, release, or head is stale"
                )
            return CrmTenantActivationResult(_receipt_from_record(row), False)

        return self._client.execute_write(work)

    def read_receipt(
        self,
        scope: CrmTenantProjectionScope,
        release: CrmTenantActivationRelease,
        census_id: str,
        generation: int,
        task_id: str,
    ) -> CrmTenantActivationReceipt | None:
        """Read a published release's durable settlement receipt without mutation."""
        assert_standalone_crm_lane_a_ready(self._client)

        def work(tx: ManagedTransaction) -> CrmTenantActivationReceipt | None:
            row = tx.run(
                READ_RECEIPT_BY_ID,
                source_key=scope.source_key,
                source_instance_id=scope.source_instance_id,
                control_instance_id=scope.control_instance_id,
                release_id=release.release_id,
                release_fingerprint=release.release_fingerprint,
                census_id=census_id,
                generation=generation,
                task_id=task_id,
            ).single()
            return None if row is None else _receipt_from_record(row)

        return self._client.execute_read(work)


def _lock_scope(tx: ManagedTransaction, command: CrmTenantActivationCommand) -> None:
    row = tx.run(
        LOCK_ACTIVATION_SCOPE,
        source_key=command.mapping_scope.source_key,
        source_instance_id=command.mapping_scope.source_instance_id,
        control_instance_id=command.mapping_scope.control_instance_id,
    ).single()
    if row is None:
        raise CrmTenantActivationIntegrityError("activation scope lock is missing")


def _read_exact_receipt(
    tx: ManagedTransaction, command: CrmTenantActivationCommand
) -> CrmTenantActivationReceipt | None:
    row = _run_command(tx, READ_RECEIPT, command).single()
    return None if row is None else _receipt_from_record(row)


def _run_command(
    tx: ManagedTransaction,
    query: str,
    command: CrmTenantActivationCommand,
) -> Result:
    mapping = command.expected_mapping_head
    projection = command.expected_projection_head
    return tx.run(
        query,
        source_key=command.mapping_scope.source_key,
        source_instance_id=command.mapping_scope.source_instance_id,
        control_instance_id=command.mapping_scope.control_instance_id,
        candidate_revision_id=command.candidate.revision_id,
        candidate_manifest_digest=command.candidate.manifest_digest,
        release_id=command.release.release_id,
        release_fingerprint=command.release.release_fingerprint,
        census_id=command.census_id,
        generation=command.generation,
        task_id=command.task_id,
        mapping_head_id=mapping_head_id(command.mapping_scope),
        # The candidate always froze the deterministic scope head ID, including
        # generation-zero where the predecessor record itself is absent.
        expected_mapping_head_id=mapping_head_id(command.mapping_scope),
        mapping_head_present=mapping is not None,
        expected_mapping_revision_id=None if mapping is None else mapping.active_revision_id,
        expected_mapping_revision_number=None
        if mapping is None
        else mapping.active_revision_number,
        expected_mapping_manifest_digest=None
        if mapping is None
        else mapping.active_manifest_digest,
        projection_head_id=projection_head_id(command.projection_scope),
        expected_projection_head_id=None if projection is None else projection.head_id,
        projection_head_present=projection is not None,
        expected_projection_release_id=None if projection is None else projection.active_release_id,
        expected_projection_release_number=None
        if projection is None
        else projection.active_release_number,
        expected_projection_release_fingerprint=(
            None if projection is None else projection.active_release_fingerprint
        ),
    )


def _receipt_from_record(record: Record) -> CrmTenantActivationReceipt:
    raw = record.get("release")
    if not isinstance(raw, Mapping):
        raise CrmTenantActivationIntegrityError("activation receipt is malformed")
    values = {str(key): value for key, value in raw.items()}
    try:
        mapping = _mapping_predecessor(values)
        projection = _projection_predecessor(values)
        return CrmTenantActivationReceipt(
            _required_text(values, "release_id"),
            _required_text(values, "activation_census_id"),
            _required_int(values, "activation_generation"),
            _required_text(values, "activation_task_id"),
            _required_text(values, "activation_candidate_revision_id"),
            _required_text(values, "activation_activated_at"),
            mapping,
            projection,
        )
    except ValueError as exc:
        raise CrmTenantActivationIntegrityError("activation receipt is malformed") from exc


def _mapping_predecessor(values: Mapping[str, object]) -> CrmTenantMappingExpectedHead | None:
    present = _required_bool(values, "activation_mapping_head_present")
    fields = (
        "activation_prior_mapping_head_id",
        "activation_prior_mapping_revision_id",
        "activation_prior_mapping_revision_number",
        "activation_prior_mapping_manifest_digest",
    )
    if not present:
        _require_absent(values, fields)
        return None
    return CrmTenantMappingExpectedHead(
        _required_text(values, fields[0]),
        _required_text(values, fields[1]),
        _required_int(values, fields[2]),
        _required_text(values, fields[3]),
    )


def _projection_predecessor(values: Mapping[str, object]) -> CrmTenantProjectionExpectedHead | None:
    present = _required_bool(values, "activation_projection_head_present")
    fields = (
        "activation_prior_projection_head_id",
        "activation_prior_projection_release_id",
        "activation_prior_projection_release_number",
        "activation_prior_projection_release_fingerprint",
    )
    if not present:
        _require_absent(values, fields)
        return None
    return CrmTenantProjectionExpectedHead(
        _required_text(values, fields[0]),
        _required_text(values, fields[1]),
        _required_int(values, fields[2]),
        _required_text(values, fields[3]),
    )


def _require_absent(values: Mapping[str, object], fields: tuple[str, ...]) -> None:
    if any(values.get(field) is not None for field in fields):
        raise CrmTenantActivationIntegrityError("absent predecessor has persisted identity")


def _required_text(values: Mapping[str, object], field: str) -> str:
    value = values.get(field)
    if not isinstance(value, str):
        raise CrmTenantActivationIntegrityError(f"activation {field} is malformed")
    return value


def _required_int(values: Mapping[str, object], field: str) -> int:
    value = values.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CrmTenantActivationIntegrityError(f"activation {field} is malformed")
    return value


def _required_bool(values: Mapping[str, object], field: str) -> bool:
    value = values.get(field)
    if not isinstance(value, bool):
        raise CrmTenantActivationIntegrityError(f"activation {field} is malformed")
    return value
