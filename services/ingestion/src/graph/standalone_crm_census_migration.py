from __future__ import annotations

from neo4j import ManagedTransaction

from src.graph.client import Neo4jClient
from src.graph.ingestion_control_instance_migration import assert_ingestion_control_ready
from src.graph.queries.standalone_crm_census import CREATE_STANDALONE_CRM_CENSUS_CONSTRAINTS

MIGRATION_KEY = "standalone_crm_census_control_v1"
REQUIRED_SCHEMA_NAMES = frozenset(
    {
        "standalone_crm_census_id_unique",
        "standalone_crm_census_occurrence_unique",
        "standalone_crm_attempt_identity_unique",
        "standalone_crm_unit_identity_unique",
        "standalone_crm_call_intent_unique",
        "standalone_crm_call_sequence_unique",
        "standalone_crm_publication_unique",
        "standalone_crm_checkpoint_unique",
        "standalone_crm_fence_unique",
        "standalone_crm_active_scope_unique",
        "standalone_crm_census_active_scope",
        "standalone_crm_attempt_lease",
        "standalone_crm_unit_status",
        "standalone_crm_publication_status",
    }
)
REQUIRED_CONSTRAINTS = {
    "standalone_crm_census_id_unique": ("StandaloneCrmCensus", ("census_id",)),
    "standalone_crm_census_occurrence_unique": (
        "StandaloneCrmCensus",
        (
            "source_key",
            "source_instance_id",
            "control_instance_id",
            "census_kind",
            "occurrence_key",
        ),
    ),
    "standalone_crm_attempt_identity_unique": (
        "StandaloneCrmCensusAttempt",
        ("census_id", "generation"),
    ),
    "standalone_crm_unit_identity_unique": (
        "StandaloneCrmCensusUnit",
        ("census_id", "stream_kind"),
    ),
    "standalone_crm_call_intent_unique": ("StandaloneCrmHttpCallReservation", ("intent_id",)),
    "standalone_crm_call_sequence_unique": (
        "StandaloneCrmHttpCallReservation",
        ("census_id", "call_sequence"),
    ),
    "standalone_crm_publication_unique": (
        "StandaloneCrmChildPublication",
        ("census_id", "generation", "stream_kind"),
    ),
    "standalone_crm_checkpoint_unique": (
        "StandaloneCrmCensusCheckpoint",
        ("census_id", "stream_kind"),
    ),
    "standalone_crm_fence_unique": (
        "StandaloneCrmCensusFence",
        ("census_id", "generation", "stream_kind"),
    ),
    "standalone_crm_active_scope_unique": ("StandaloneCrmCensusActiveScope", ("scope_key",)),
}
REQUIRED_INDEXES = {
    "standalone_crm_census_active_scope": (
        "StandaloneCrmCensus",
        ("source_key", "source_instance_id", "control_instance_id", "status"),
    ),
    "standalone_crm_attempt_lease": (
        "StandaloneCrmCensusAttempt",
        ("census_id", "status", "lease_until"),
    ),
    "standalone_crm_unit_status": ("StandaloneCrmCensusUnit", ("census_id", "generation", "state")),
    "standalone_crm_publication_status": (
        "StandaloneCrmChildPublication",
        ("census_id", "generation", "status"),
    ),
}


def ensure_standalone_crm_census_ready(client: Neo4jClient) -> None:
    assert_ingestion_control_ready(client)
    with client.session() as session:
        for statement in CREATE_STANDALONE_CRM_CENSUS_CONSTRAINTS:
            session.run(statement).consume()

    def _work(tx: ManagedTransaction) -> bool:
        record = tx.run(
            "MERGE (migration:DataMigration {migration_key: $migration_key}) "
            "ON CREATE SET migration.created_at = datetime() "
            "SET migration.completed_at = coalesce(migration.completed_at, datetime()) "
            "RETURN migration.completed_at IS NOT NULL AS ready",
            migration_key=MIGRATION_KEY,
        ).single()
        return record is not None and record["ready"] is True

    if not client.execute_write(_work):
        raise RuntimeError("standalone CRM census readiness marker could not be installed")


def assert_standalone_crm_census_ready(client: Neo4jClient) -> None:
    assert_ingestion_control_ready(client)

    with client.session() as session:
        constraints = {
            str(record["name"]): (
                str(record["type"]),
                str(record["entityType"]),
                tuple(str(value) for value in record["labelsOrTypes"]),
                tuple(str(value) for value in record["properties"]),
            )
            for record in session.run(
                "SHOW CONSTRAINTS YIELD name, type, entityType, labelsOrTypes, properties "
                "RETURN name, type, entityType, labelsOrTypes, properties"
            )
        }
        indexes = {
            str(record["name"]): (
                str(record["type"]),
                str(record["entityType"]),
                tuple(str(value) for value in record["labelsOrTypes"]),
                tuple(str(value) for value in record["properties"]),
            )
            for record in session.run(
                "SHOW INDEXES YIELD name, type, entityType, labelsOrTypes, properties "
                "RETURN name, type, entityType, labelsOrTypes, properties"
            )
        }
    schema_names = set(constraints) | set(indexes)
    if not REQUIRED_SCHEMA_NAMES.issubset(schema_names):
        missing = ", ".join(sorted(REQUIRED_SCHEMA_NAMES - schema_names))
        raise RuntimeError(f"standalone CRM census schema is incomplete: {missing}")
    for name, (label, properties) in REQUIRED_CONSTRAINTS.items():
        actual = constraints.get(name)
        if (
            actual is None
            or actual[0] != "UNIQUENESS"
            or actual[1] != "NODE"
            or actual[2:]
            != (
                (label,),
                properties,
            )
        ):
            raise RuntimeError(f"standalone CRM census constraint is malformed: {name}")
    for name, (label, properties) in REQUIRED_INDEXES.items():
        actual = indexes.get(name)
        if (
            actual is None
            or actual[0] != "RANGE"
            or actual[1] != "NODE"
            or actual[2:]
            != (
                (label,),
                properties,
            )
        ):
            raise RuntimeError(f"standalone CRM census index is malformed: {name}")

    def _read(tx: ManagedTransaction) -> bool:
        record = tx.run(
            "MATCH (migration:DataMigration {migration_key: $migration_key}) "
            "RETURN migration.completed_at IS NOT NULL AS ready",
            migration_key=MIGRATION_KEY,
        ).single()
        return record is not None and record["ready"] is True

    if not client.execute_read(_read):
        raise RuntimeError("standalone CRM census control migration is not complete")
