"""Additive readiness check for the disabled CRM repair ledger."""

from __future__ import annotations

from collections.abc import Mapping

from neo4j import ManagedTransaction, Record

from src.graph.client import Neo4jClient
from src.graph.ingestion_control_instance_migration import assert_ingestion_control_ready
from src.graph.queries.crm_deal_identity_repair_ledger import CREATE_CRM_DEAL_REPAIR_LEDGER_SCHEMA

MIGRATION_KEY = "crm_deal_identity_repair_ledger_v1"
REQUIRED_CONSTRAINTS = {
    "crm_deal_repair_boundary_manifest_unique": (
        "RepairExecutionBoundary",
        ("manifest_digest",),
    ),
    "crm_deal_repair_boundary_artifact_unique": (
        "RepairExecutionBoundary",
        ("artifact_id",),
    ),
    "crm_deal_repair_run_id_unique": ("CrmDealRepairRun", ("run_id",)),
    "crm_deal_repair_run_repair_id_unique": ("CrmDealRepairRun", ("repair_id",)),
    "crm_deal_repair_run_identity_unique": (
        "CrmDealRepairRun",
        ("qualification_identity",),
    ),
    "crm_deal_repair_quiescence_unique": (
        "CrmDealRepairQuiescence",
        ("run_id", "quiescence_id"),
    ),
    "crm_deal_repair_unit_unique": ("CrmDealRepairUnit", ("run_id", "unit_id")),
    "crm_deal_repair_checkpoint_unique": (
        "CrmDealRepairCheckpoint",
        ("run_id", "checkpoint_id"),
    ),
    "crm_deal_repair_fence_unique": ("CrmDealRepairFence", ("run_id", "fence_id")),
    "crm_deal_repair_mutation_unique": (
        "CrmDealRepairMutationResult",
        ("run_id", "mutation_id"),
    ),
    "crm_deal_repair_rollback_unique": (
        "CrmDealRepairRollbackImage",
        ("run_id", "rollback_image_id"),
    ),
    "crm_deal_repair_secondary_unique": (
        "CrmDealRepairSecondaryDisposition",
        ("run_id", "disposition_id"),
    ),
    "crm_deal_repair_verification_unique": (
        "CrmDealRepairVerification",
        ("run_id", "verification_id"),
    ),
    "crm_deal_repair_outbox_unique": ("CrmDealRepairOutbox", ("run_id", "event_id")),
    "crm_deal_repair_control_run_unique": ("CrmDealRepairControl", ("run_id",)),
    "crm_deal_repair_allocation_completion_unique": (
        "CrmDealRepairAllocationCompletion",
        ("run_id",),
    ),
    "crm_deal_repair_qualified_row_unique": (
        "CrmDealRepairQualifiedInventoryRow",
        ("run_id", "inventory_key"),
    ),
    "crm_deal_repair_authorization_proof_unique": (
        "CrmDealRepairAuthorizationProof",
        ("run_id", "operation", "revision"),
    ),
    "crm_deal_repair_publication_reservation_unique": (
        "BitrixRepairPublicationReservation",
        (
            "control_instance_id",
            "routing_identity_digest",
            "occurrence_generation_identity",
        ),
    ),
    "crm_deal_repair_publication_token_unique": (
        "BitrixRepairPublicationReservation",
        ("reservation_token",),
    ),
}
REQUIRED_INDEXES = {
    "crm_deal_repair_run_status": (
        "CrmDealRepairRun",
        ("status", "source_instance_id", "control_instance_id"),
    ),
    "crm_deal_repair_unit_state": (
        "CrmDealRepairUnit",
        ("run_id", "state", "generation"),
    ),
    "crm_deal_repair_quiescence_state": (
        "CrmDealRepairQuiescence",
        ("run_id", "state", "generation", "sequence"),
    ),
    "crm_deal_repair_fence_state": (
        "CrmDealRepairFence",
        ("run_id", "state", "generation"),
    ),
    "crm_deal_repair_checkpoint_sequence": (
        "CrmDealRepairCheckpoint",
        ("run_id", "unit_id", "generation", "sequence", "attempt"),
    ),
    "crm_deal_repair_mutation_sequence": (
        "CrmDealRepairMutationResult",
        ("run_id", "unit_id", "generation", "sequence", "attempt"),
    ),
    "crm_deal_repair_rollback_state": (
        "CrmDealRepairRollbackImage",
        ("run_id", "unit_id", "generation", "state"),
    ),
    "crm_deal_repair_secondary_outcome": (
        "CrmDealRepairSecondaryDisposition",
        ("run_id", "unit_id", "generation", "outcome"),
    ),
    "crm_deal_repair_verification_outcome": (
        "CrmDealRepairVerification",
        ("run_id", "unit_id", "generation", "outcome"),
    ),
    "crm_deal_repair_outbox_state": (
        "CrmDealRepairOutbox",
        ("run_id", "state", "sequence"),
    ),
    "crm_deal_repair_control_state": ("CrmDealRepairControl", ("state", "revision")),
    "crm_deal_repair_publication_reservation_state": (
        "BitrixRepairPublicationReservation",
        ("control_instance_id", "status"),
    ),
}


def ensure_crm_deal_repair_ledger_ready(client: Neo4jClient) -> None:
    """Install only #300 metadata schema after #272 is provably ready."""
    assert_ingestion_control_ready(client)
    with client.session() as session:
        for statement in CREATE_CRM_DEAL_REPAIR_LEDGER_SCHEMA:
            session.run(statement).consume()
    _assert_exact_schema(client)

    def _write(tx: ManagedTransaction) -> bool:
        record = tx.run(
            "MERGE (migration:DataMigration {migration_key: $migration_key}) "
            "ON CREATE SET migration.created_at = datetime() "
            "SET migration.completed_at = coalesce(migration.completed_at, datetime()) "
            "RETURN migration.completed_at IS NOT NULL AS ready",
            migration_key=MIGRATION_KEY,
        ).single()
        return record is not None and record["ready"] is True

    if not client.execute_write(_write):
        raise RuntimeError("CRM repair ledger readiness marker could not be installed")


def assert_crm_deal_repair_ledger_ready(client: Neo4jClient) -> None:
    assert_ingestion_control_ready(client)
    _assert_exact_schema(client)

    def _read(tx: ManagedTransaction) -> bool:
        record = tx.run(
            "MATCH (migration:DataMigration {migration_key: $migration_key}) "
            "RETURN migration.completed_at IS NOT NULL AS ready",
            migration_key=MIGRATION_KEY,
        ).single()
        return record is not None and record["ready"] is True

    if not client.execute_read(_read):
        raise RuntimeError("CRM repair ledger migration is not complete")


def _assert_exact_schema(client: Neo4jClient) -> None:
    with client.session() as session:
        columns = "name, type, entityType, labelsOrTypes, properties"
        constraints = {
            str(row["name"]): row
            for row in session.run(f"SHOW CONSTRAINTS YIELD {columns} RETURN {columns}")
        }
        indexes = {
            str(row["name"]): row
            for row in session.run(f"SHOW INDEXES YIELD {columns} RETURN {columns}")
        }
    _assert_definitions(constraints, REQUIRED_CONSTRAINTS, "UNIQUENESS", "constraint")
    _assert_definitions(indexes, REQUIRED_INDEXES, "RANGE", "index")


def _assert_definitions(
    rows: Mapping[str, Record],
    required: Mapping[str, tuple[str, tuple[str, ...]]],
    expected_type: str,
    label: str,
) -> None:
    for name, (node_label, properties) in required.items():
        row = rows.get(name)
        if row is None or row["type"] != expected_type or row["entityType"] != "NODE":
            raise RuntimeError(f"CRM repair ledger {label} is missing or malformed: {name}")
        if tuple(row["labelsOrTypes"]) != (node_label,) or tuple(row["properties"]) != properties:
            raise RuntimeError(f"CRM repair ledger {label} is missing or malformed: {name}")
