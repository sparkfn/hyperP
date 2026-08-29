"""Rerunnable readiness gate for standalone CRM Lane A shared contract schema."""

from __future__ import annotations

from neo4j import ManagedTransaction, Session

from src.graph.client import Neo4jClient
from src.graph.queries.standalone_crm_lane_a_contracts import (
    CREATE_STANDALONE_CRM_LANE_A_CONSTRAINTS,
)
from src.graph.standalone_crm_census_migration import assert_standalone_crm_census_ready

MIGRATION_KEY = "standalone_crm_lane_a_contracts_v1"
SchemaDefinition = tuple[str, str, tuple[str, ...], tuple[str, ...]]
LEGACY_MEMBERSHIP_SNAPSHOT_SCOPE_DIGEST_CONSTRAINT = (
    "crm_company_membership_snapshot_scope_digest_unique"
)
_LEGACY_MEMBERSHIP_SNAPSHOT_SCOPE_DIGEST_DEFINITION: SchemaDefinition = (
    "UNIQUENESS",
    "NODE",
    ("CrmCompanyMembershipSnapshot",),
    ("source_instance_id", "subject_kind", "subject_id", "snapshot_digest"),
)


def ensure_standalone_crm_lane_a_ready(client: Neo4jClient) -> None:
    """Install additive Lane A DDL and complete its marker after exact validation."""
    assert_standalone_crm_census_ready(client)
    with client.session() as session:
        _drop_legacy_membership_snapshot_scope_digest_constraint(session)
        for statement in CREATE_STANDALONE_CRM_LANE_A_CONSTRAINTS:
            session.run(statement).consume()
    assert_standalone_crm_lane_a_schema(client)

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
        raise RuntimeError("standalone CRM Lane A readiness marker could not be installed")


def assert_standalone_crm_lane_a_ready(client: Neo4jClient) -> None:
    """Fail closed unless #273 readiness, exact schema, and this marker are complete."""
    assert_standalone_crm_census_ready(client)
    assert_standalone_crm_lane_a_schema(client)

    def _read(tx: ManagedTransaction) -> bool:
        record = tx.run(
            "MATCH (migration:DataMigration {migration_key: $migration_key}) "
            "RETURN migration.completed_at IS NOT NULL AS ready",
            migration_key=MIGRATION_KEY,
        ).single()
        return record is not None and record["ready"] is True

    if not client.execute_read(_read):
        raise RuntimeError("standalone CRM Lane A contract migration is not complete")


def assert_standalone_crm_lane_a_schema(client: Neo4jClient) -> None:
    """Validate expected DDL names and exact constraint/index shape without rewrites."""
    expected = _expected_schema()
    with client.session() as session:
        constraints = _schema_rows(session, "SHOW CONSTRAINTS")
        indexes = _schema_rows(session, "SHOW INDEXES")
    if LEGACY_MEMBERSHIP_SNAPSHOT_SCOPE_DIGEST_CONSTRAINT in constraints:
        raise RuntimeError(
            "standalone CRM Lane A schema retains the legacy membership snapshot constraint"
        )
    for name, definition in expected.items():
        actual = constraints if definition[0] == "UNIQUENESS" else indexes
        if actual.get(name) != definition:
            raise RuntimeError(f"standalone CRM Lane A schema is malformed: {name}")


def _drop_legacy_membership_snapshot_scope_digest_constraint(session: Session) -> None:
    constraints = _schema_rows(session, "SHOW CONSTRAINTS")
    actual = constraints.get(LEGACY_MEMBERSHIP_SNAPSHOT_SCOPE_DIGEST_CONSTRAINT)
    if actual is None:
        return
    if actual != _LEGACY_MEMBERSHIP_SNAPSHOT_SCOPE_DIGEST_DEFINITION:
        raise RuntimeError(
            "standalone CRM Lane A legacy membership snapshot constraint is malformed"
        )
    session.run(
        "DROP CONSTRAINT " + LEGACY_MEMBERSHIP_SNAPSHOT_SCOPE_DIGEST_CONSTRAINT + " IF EXISTS"
    ).consume()


def _schema_rows(
    session: Session,
    command: str,
) -> dict[str, SchemaDefinition]:
    rows = session.run(
        command
        + " YIELD name, type, entityType, labelsOrTypes, properties "
        + "WHERE labelsOrTypes IS NOT NULL AND properties IS NOT NULL "
        + "RETURN name, type, entityType, labelsOrTypes, properties"
    )
    return {
        str(row["name"]): (
            str(row["type"]),
            str(row["entityType"]),
            tuple(str(value) for value in row["labelsOrTypes"]),
            tuple(str(value) for value in row["properties"]),
        )
        for row in rows
    }


def _expected_schema() -> dict[str, SchemaDefinition]:
    expected: dict[str, SchemaDefinition] = {}
    for statement in CREATE_STANDALONE_CRM_LANE_A_CONSTRAINTS:
        name, label = _schema_name_and_label(statement)
        if "CONSTRAINT" in statement:
            raw_properties = statement.split("REQUIRE ", 1)[1].removesuffix(" IS UNIQUE")
            properties = tuple(
                value.strip().removeprefix("n.") for value in raw_properties.strip("()").split(",")
            )
            expected[name] = ("UNIQUENESS", "NODE", (label,), properties)
        else:
            raw_properties = statement.split(" ON (", 1)[1].removesuffix(")")
            properties = tuple(
                value.strip().removeprefix("n.") for value in raw_properties.split(",")
            )
            expected[name] = ("RANGE", "NODE", (label,), properties)
    return expected


def _schema_name_and_label(statement: str) -> tuple[str, str]:
    name = statement.split()[2]
    label = statement.split("FOR (n:", 1)[1].split(")", 1)[0]
    return name, label
