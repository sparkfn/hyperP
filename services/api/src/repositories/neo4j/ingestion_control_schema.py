"""Read-only #272 control-schema admission checks for API Bitrix writes."""

from __future__ import annotations

from dataclasses import dataclass

from neo4j import AsyncManagedTransaction, Record

from src.repositories.protocols.ingest import BitrixApiAdmissionError


@dataclass(frozen=True)
class _ConstraintSpec:
    name: str
    label: str
    properties: tuple[str, ...]


_REQUIRED_SPECS: tuple[_ConstraintSpec, ...] = (
    _ConstraintSpec(
        "ingest_run_worker_task_control_unique",
        "IngestRun",
        ("control_instance_id", "worker_task_id"),
    ),
    _ConstraintSpec(
        "ingest_run_source_control_idempotency_unique",
        "IngestRun",
        ("source_key", "control_instance_id", "idempotency_key"),
    ),
    _ConstraintSpec(
        "ingestion_logical_run_source_control_idempotency_unique",
        "IngestionLogicalRun",
        ("source_key", "control_instance_id", "idempotency_key"),
    ),
    _ConstraintSpec(
        "ingestion_checkpoint_control_logical_phase_unique",
        "IngestionCheckpoint",
        ("control_instance_id", "logical_run_id", "phase"),
    ),
    _ConstraintSpec(
        "incremental_checkpoint_control_identity_unique",
        "IngestionCheckpoint",
        ("control_instance_id", "checkpoint_key"),
    ),
    _ConstraintSpec(
        "bitrix_ingestion_stream_control_identity_unique",
        "BitrixIngestionStream",
        ("source_key", "control_instance_id", "stream_key"),
    ),
    _ConstraintSpec(
        "bitrix_known_owner_set_control_unique",
        "BitrixKnownOwnerRefreshSet",
        ("control_instance_id", "generation_id", "membership_set_id"),
    ),
    _ConstraintSpec(
        "bitrix_known_owner_member_control_unique",
        "BitrixKnownOwnerRefreshMember",
        ("control_instance_id", "generation_id", "membership_set_id", "deal_id"),
    ),
    _ConstraintSpec(
        "bitrix_backfill_coverage_control_identity_unique",
        "BitrixBackfillCoverage",
        (
            "control_instance_id",
            "generation_id",
            "stream_key",
            "source_identity",
            "source_boundary",
        ),
    ),
    _ConstraintSpec(
        "bitrix_dispatch_control_control_unique",
        "BitrixDispatchControl",
        ("source_key", "control_instance_id"),
    ),
    _ConstraintSpec(
        "bitrix_backfill_generation_control_unique",
        "BitrixBackfillGeneration",
        ("control_instance_id", "generation_id"),
    ),
    _ConstraintSpec(
        "bitrix_dispatch_outbox_control_successor_unique",
        "BitrixBackfillDispatchOutbox",
        ("control_instance_id", "successor_generation_id"),
    ),
    _ConstraintSpec(
        "bitrix_source_instance_identity_unique",
        "BitrixSourceInstance",
        ("source_key", "source_instance_id"),
    ),
)

_RETIRED_SPECS: tuple[_ConstraintSpec, ...] = (
    _ConstraintSpec("ingest_run_worker_task_id_unique", "IngestRun", ("worker_task_id",)),
    _ConstraintSpec(
        "ingest_run_source_idempotency_unique",
        "IngestRun",
        ("source_key", "idempotency_key"),
    ),
    _ConstraintSpec("ingestion_checkpoint_key_unique", "IngestionCheckpoint", ("checkpoint_key",)),
    _ConstraintSpec(
        "ingestion_checkpoint_identity_unique",
        "IngestionCheckpoint",
        ("logical_run_id", "phase"),
    ),
    _ConstraintSpec(
        "ingestion_logical_run_source_idempotency_unique",
        "IngestionLogicalRun",
        ("source_key", "idempotency_key"),
    ),
    _ConstraintSpec(
        "bitrix_ingestion_stream_identity_unique",
        "BitrixIngestionStream",
        ("source_key", "stream_key"),
    ),
    _ConstraintSpec(
        "bitrix_backfill_generation_id_unique",
        "BitrixBackfillGeneration",
        ("generation_id",),
    ),
    _ConstraintSpec(
        "bitrix_known_owner_set_id_unique",
        "BitrixKnownOwnerRefreshSet",
        ("generation_id", "membership_set_id"),
    ),
    _ConstraintSpec(
        "bitrix_known_owner_member_unique",
        "BitrixKnownOwnerRefreshMember",
        ("generation_id", "membership_set_id", "deal_id"),
    ),
    _ConstraintSpec(
        "bitrix_backfill_coverage_identity_unique",
        "BitrixBackfillCoverage",
        ("generation_id", "stream_key", "source_identity", "source_boundary"),
    ),
    _ConstraintSpec(
        "bitrix_dispatch_control_source_unique", "BitrixDispatchControl", ("source_key",)
    ),
    _ConstraintSpec(
        "bitrix_dispatch_outbox_successor_unique",
        "BitrixBackfillDispatchOutbox",
        ("successor_generation_id",),
    ),
)

_RETIRED_NAMES = frozenset(
    {
        "ingest_run_worker_task_id_unique",
        "ingest_run_source_idempotency_unique",
        "ingestion_checkpoint_key_unique",
        "ingestion_checkpoint_identity_unique",
        "ingestion_logical_run_source_idempotency_unique",
        "bitrix_ingestion_stream_identity_unique",
        "bitrix_backfill_generation_id_unique",
        "bitrix_known_owner_set_id_unique",
        "bitrix_known_owner_member_unique",
        "bitrix_backfill_coverage_identity_unique",
        "bitrix_dispatch_control_source_unique",
        "bitrix_dispatch_outbox_successor_unique",
    }
)

SHOW_BITRIX_CONTROL_CONSTRAINTS = (
    "SHOW CONSTRAINTS YIELD name, type, entityType, labelsOrTypes, properties "
    "RETURN name, type, entityType, labelsOrTypes, properties"
)


async def assert_bitrix_control_schema_ready(tx: AsyncManagedTransaction) -> None:
    """Fail closed when a completed migration no longer has its exact DDL."""
    result = await tx.run(SHOW_BITRIX_CONTROL_CONSTRAINTS)
    definitions: dict[str, Record] = {}
    async for record in result:
        if record is None:
            raise BitrixApiAdmissionError("Bitrix control schema is not ready")
        name: object = record["name"]
        if not isinstance(name, str) or name in definitions:
            raise BitrixApiAdmissionError("Bitrix control schema is not ready")
        definitions[name] = record
    for name in _RETIRED_NAMES:
        if name in definitions:
            raise BitrixApiAdmissionError("Bitrix control schema is not ready")
    expected_identities = {
        (spec.label, spec.properties) for spec in (*_RETIRED_SPECS, *_REQUIRED_SPECS)
    }
    known_names = {spec.name for spec in (*_RETIRED_SPECS, *_REQUIRED_SPECS)}
    for name, record in definitions.items():
        identity = _node_constraint_identity(record)
        if name not in known_names and identity in expected_identities:
            raise BitrixApiAdmissionError("Bitrix control schema is not ready")
    for spec in _REQUIRED_SPECS:
        required_record = definitions.get(spec.name)
        if required_record is None or not _matches_spec(required_record, spec):
            raise BitrixApiAdmissionError("Bitrix control schema is not ready")


def _matches_spec(record: Record, spec: _ConstraintSpec) -> bool:
    constraint_type: object = record["type"]
    entity_type: object = record["entityType"]
    labels: object = record["labelsOrTypes"]
    properties: object = record["properties"]
    return (
        constraint_type == "UNIQUENESS"
        and entity_type == "NODE"
        and isinstance(labels, list)
        and all(isinstance(label, str) for label in labels)
        and labels == [spec.label]
        and isinstance(properties, list)
        and all(isinstance(property_name, str) for property_name in properties)
        and properties == list(spec.properties)
    )


def _node_constraint_identity(record: Record) -> tuple[str, tuple[str, ...]] | None:
    entity_type: object = record["entityType"]
    labels: object = record["labelsOrTypes"]
    properties: object = record["properties"]
    if (
        entity_type != "NODE"
        or not isinstance(labels, list)
        or not all(isinstance(label, str) for label in labels)
        or len(labels) != 1
        or not isinstance(properties, list)
        or not all(isinstance(property_name, str) for property_name in properties)
    ):
        return None
    return labels[0], tuple(properties)
