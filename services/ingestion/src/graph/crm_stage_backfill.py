"""Neo4j repository for #148 mapping, rebuild, reconciliation, and release."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol, cast

from neo4j import ManagedTransaction, Record
from pydantic.types import JsonValue

from src.crm_stage_mapping import (
    CrmStageInventoryRow,
    CrmStageMappingPolicy,
    CrmStageTuple,
)
from src.crm_stage_reconciliation import (
    CrmStageInvalidationStatus,
    CrmStageRebuildResult,
    CrmStageReconciliationReport,
    CrmStageReleaseStatus,
    CrmStageRetryRetentionResult,
)
from src.graph.client import Neo4jClient
from src.graph.queries.crm_stage_backfill import (
    CRM_STAGE_CURRENT_EFFECTIVE_ROWS,
    CRM_STAGE_MAPPING_INVENTORY,
    ENABLE_CRM_STAGE_ANALYTICAL_RELEASE,
    GET_CRM_STAGE_ANALYTICAL_RELEASE,
    GET_CRM_STAGE_INVALIDATION_STATUS,
    GET_CRM_STAGE_RECONCILIATION,
    PUBLISH_CRM_STAGE_INVALIDATIONS,
    REHEARSE_CRM_STAGE_PROJECTION_ROLLBACK,
    RETAIN_REVIEWED_PENDING_PARENT_RETRIES,
    RETIRE_STALE_CRM_STAGE_TIMELINE_PROJECTIONS,
    UPSERT_CRM_STAGE_TIMELINE_PROJECTIONS,
)


class _Neo4jDateTime(Protocol):
    def to_native(self) -> datetime: ...


class CrmStageBackfillRepository:
    """Expose aggregate reads and tightly scoped analytical projection mutations."""

    def __init__(self, client: Neo4jClient, *, entity_type_id: int) -> None:
        if isinstance(entity_type_id, bool) or entity_type_id < 1:
            raise ValueError("CRM stage entity_type_id must be positive")
        self._client = client
        self._entity_type_id = str(entity_type_id)

    def inventory(self) -> tuple[CrmStageInventoryRow, ...]:
        def read(tx: ManagedTransaction) -> list[Record]:
            return list(tx.run(CRM_STAGE_MAPPING_INVENTORY, entity_type_id=self._entity_type_id))

        return tuple(_inventory_row(row) for row in self._client.execute_read(read))

    def reconcile(self) -> CrmStageReconciliationReport:
        def read(tx: ManagedTransaction) -> Record | None:
            return tx.run(GET_CRM_STAGE_RECONCILIATION).single()

        row = self._client.execute_read(read)
        if row is None:
            raise RuntimeError("CRM stage reconciliation returned no aggregate row")
        keys = (
            "occurrence_count",
            "distinct_occurrence_count",
            "nonterminal_occurrence_count",
            "variant_count",
            "variant_identity_count",
            "authority_head_count",
            "missing_head_decision_count",
            "invalid_selected_authority_count",
            "unresolved_retry_count",
            "quarantined_retry_count",
            "invalidation_count",
            "unpublished_invalidation_count",
        )
        return CrmStageReconciliationReport.create(**{key: _non_negative(row, key) for key in keys})

    def retain_pending_parent_retries(
        self,
        *,
        expected_count: int,
        accepted_by: str,
        reason: str,
        decision_id: str,
    ) -> CrmStageRetryRetentionResult:
        if isinstance(expected_count, bool) or expected_count < 1:
            raise ValueError("expected retry count must be positive")
        for value, field_name in (
            (accepted_by, "accepted actor"),
            (reason, "retention reason"),
            (decision_id, "retention decision ID"),
        ):
            if not value.strip():
                raise ValueError(f"{field_name} must be non-empty")

        def write(tx: ManagedTransaction) -> Record | None:
            return tx.run(
                RETAIN_REVIEWED_PENDING_PARENT_RETRIES,
                expected_count=expected_count,
                accepted_by=accepted_by,
                reason=reason,
                decision_id=decision_id,
            ).single()

        row = self._client.execute_write(write)
        if row is None:
            raise RuntimeError("pending parent retries changed or do not match the reviewed cohort")
        return CrmStageRetryRetentionResult(
            expected_count=expected_count,
            retained_count=_non_negative(row, "retained_count"),
            remaining_unresolved_count=_non_negative(row, "remaining_unresolved_count"),
            quarantined_retry_count=_non_negative(row, "quarantined_retry_count"),
        )

    def invalidation_status(self) -> CrmStageInvalidationStatus:
        def read(tx: ManagedTransaction) -> Record | None:
            return tx.run(GET_CRM_STAGE_INVALIDATION_STATUS).single()

        row = self._client.execute_read(read)
        if row is None:
            raise RuntimeError("CRM stage invalidation status returned no aggregate row")
        return CrmStageInvalidationStatus(
            total=_non_negative(row, "total"),
            pending=_non_negative(row, "pending"),
            claimed=_non_negative(row, "claimed"),
            published=_non_negative(row, "published"),
            failed=_non_negative(row, "failed"),
            superseded=_non_negative(row, "superseded"),
            active_projection_count=_non_negative(row, "active_projection_count"),
            projected_parent_count=_non_negative(row, "projected_parent_count"),
            active_mapping_versions=_string_tuple(row.get("active_mapping_versions")),
            active_policy_versions=_string_tuple(row.get("active_policy_versions")),
        )

    def rebuild(self, policy: CrmStageMappingPolicy) -> CrmStageRebuildResult:
        def write(tx: ManagedTransaction) -> CrmStageRebuildResult:
            source_rows = list(
                tx.run(
                    CRM_STAGE_CURRENT_EFFECTIVE_ROWS,
                    entity_type_id=self._entity_type_id,
                )
            )
            projections: list[dict[str, JsonValue]] = []
            active_ids: list[str] = []
            for source in source_rows:
                stage = CrmStageTuple(
                    entity_type_id=_string(source, "entity_type_id"),
                    category_id=_optional_string(source.get("category_id")),
                    stage_id=_optional_string(source.get("stage_id")),
                    source_semantic=_optional_string(source.get("source_semantic")),
                )
                mapping = policy.map_stage(stage)
                if mapping is None:
                    raise RuntimeError("effective CRM stage tuple is not mapped")
                event_identity = _string(source, "event_identity")
                active_ids.append(event_identity)
                if mapping.mapped_state in {"unresolved", "excluded"}:
                    continue
                projections.append(
                    {
                        "event_identity": event_identity,
                        "authority_decision_id": _string(source, "authority_decision_id"),
                        "authority_head_version": _non_negative(source, "authority_head_version"),
                        "authority_token": _non_negative(source, "authority_token"),
                        "available_at": _datetime(source, "available_at").isoformat(),
                        "parent_source_system": _string(source, "parent_source_system"),
                        "parent_source_record_id": _string(source, "parent_source_record_id"),
                        "entity_type_id": stage.entity_type_id,
                        "category_id": stage.category_id,
                        "stage_id": stage.stage_id,
                        "source_semantic": stage.source_semantic,
                        "mapped_state": mapping.mapped_state,
                        "mapping_reason": mapping.reason,
                        "event_at": _datetime(source, "event_at").isoformat(),
                    }
                )
            projection_record = tx.run(
                UPSERT_CRM_STAGE_TIMELINE_PROJECTIONS,
                rows=cast(list[Mapping[str, JsonValue]], projections),
                mapping_version=policy.mapping_version,
                policy_version=policy.policy_version,
                mapping_digest=policy.digest,
            ).single()
            retired_record = tx.run(
                RETIRE_STALE_CRM_STAGE_TIMELINE_PROJECTIONS,
                mapping_version=policy.mapping_version,
                active_event_identities=active_ids,
            ).single()
            published_record = tx.run(
                PUBLISH_CRM_STAGE_INVALIDATIONS,
                mapping_version=policy.mapping_version,
                policy_version=policy.policy_version,
            ).single()
            return CrmStageRebuildResult(
                mapping_version=policy.mapping_version,
                policy_version=policy.policy_version,
                projection_count=_non_negative_or_zero(projection_record, "projection_count"),
                retired_count=_non_negative_or_zero(retired_record, "retired_count"),
                published_invalidation_count=_non_negative_or_zero(
                    published_record, "published_count"
                ),
            )

        return self._client.execute_write(write)

    def rehearse_rollback(self, mapping_version: str, probe_id: str) -> tuple[int, int]:
        def write(tx: ManagedTransaction) -> Record | None:
            return tx.run(
                REHEARSE_CRM_STAGE_PROJECTION_ROLLBACK,
                mapping_version=mapping_version,
                probe_id=probe_id,
            ).single()

        row = self._client.execute_write(write)
        return (
            _non_negative_or_zero(row, "candidate_count"),
            _non_negative_or_zero(row, "leaked_probe_count"),
        )

    def release_status(self) -> CrmStageReleaseStatus:
        def read(tx: ManagedTransaction) -> Record | None:
            return tx.run(GET_CRM_STAGE_ANALYTICAL_RELEASE).single()

        row = self._client.execute_read(read)
        if row is None:
            raise RuntimeError("CRM stage release status returned no aggregate row")
        return _release_status(row)

    def enable_release(
        self,
        policy: CrmStageMappingPolicy,
        *,
        boundary_digest: str,
        reconciliation_digest: str,
        accepted_by: str,
    ) -> CrmStageReleaseStatus:
        def write(tx: ManagedTransaction) -> Record | None:
            return tx.run(
                ENABLE_CRM_STAGE_ANALYTICAL_RELEASE,
                mapping_version=policy.mapping_version,
                policy_version=policy.policy_version,
                mapping_digest=policy.digest,
                boundary_digest=boundary_digest,
                reconciliation_digest=reconciliation_digest,
                accepted_by=accepted_by,
            ).single()

        row = self._client.execute_write(write)
        if row is None:
            raise RuntimeError("CRM stage analytical release was not persisted")
        return _release_status(row)


def _inventory_row(row: Record) -> CrmStageInventoryRow:
    return CrmStageInventoryRow(
        stage=CrmStageTuple(
            entity_type_id=_string(row, "entity_type_id"),
            category_id=_optional_string(row.get("category_id")),
            stage_id=_optional_string(row.get("stage_id")),
            source_semantic=_optional_string(row.get("source_semantic")),
        ),
        observation_count=_non_negative(row, "observation_count"),
        event_identity_count=_non_negative(row, "event_identity_count"),
        first_event_at=_datetime(row, "first_event_at"),
        last_event_at=_datetime(row, "last_event_at"),
        effective_count=_non_negative(row, "effective_count"),
        withheld_count=_non_negative(row, "withheld_count"),
    )


def _release_status(row: Record) -> CrmStageReleaseStatus:
    accepted_at = row.get("accepted_at")
    return CrmStageReleaseStatus(
        enabled=bool(row.get("enabled", False)),
        mapping_version=_optional_string(row.get("mapping_version")),
        policy_version=_optional_string(row.get("policy_version")),
        mapping_digest=_optional_string(row.get("mapping_digest")),
        boundary_digest=_optional_string(row.get("boundary_digest")),
        reconciliation_digest=_optional_string(row.get("reconciliation_digest")),
        accepted_by=_optional_string(row.get("accepted_by")),
        accepted_at=_datetime_value(accepted_at).isoformat() if accepted_at is not None else None,
    )


def _string(row: Record, key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"CRM stage graph row omitted {key}")
    return value


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _non_negative(row: Record, key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"CRM stage graph row has invalid {key}")
    return cast(int, value)


def _non_negative_or_zero(row: Record | None, key: str) -> int:
    return 0 if row is None else _non_negative(row, key)


def _datetime(row: Record, key: str) -> datetime:
    return _datetime_value(row.get(key))


def _datetime_value(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if hasattr(value, "to_native"):
        native = cast(_Neo4jDateTime, value).to_native()
        if isinstance(native, datetime):
            return native
    raise RuntimeError("CRM stage graph row has an invalid datetime")


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(sorted({item for item in value if isinstance(item, str) and item}))
