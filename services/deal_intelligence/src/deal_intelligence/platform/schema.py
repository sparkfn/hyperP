"""SQLAlchemy metadata for shared Deal Intelligence platform tables only."""

from __future__ import annotations

from sqlalchemy import (
    BIGINT,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from deal_intelligence.platform.types import (
    DISPLAY_NAME_MAX_LENGTH,
    INSTANCE_KEY_MAX_LENGTH,
    SOURCE_SYSTEM_MAX_LENGTH,
)

PLATFORM_SCHEMA = "deal_intelligence"
metadata = MetaData(schema=PLATFORM_SCHEMA)

source_instances = Table(
    "source_instances",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("source_system", String(SOURCE_SYSTEM_MAX_LENGTH), nullable=False),
    Column("instance_key", String(INSTANCE_KEY_MAX_LENGTH), nullable=False),
    Column("display_name", String(DISPLAY_NAME_MAX_LENGTH), nullable=False),
    Column("is_enabled", Boolean, nullable=False, server_default="false"),
    Column("registered_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "char_length(source_system) BETWEEN 1 AND 80 AND "
        "source_system ~ '^[a-z][a-z0-9]*([_-][a-z0-9]+)*$'",
        name="source_instances_source_system_slug",
    ),
    CheckConstraint(
        "char_length(instance_key) BETWEEN 1 AND 255 AND "
        "instance_key ~ '^[a-z][a-z0-9]*([_-][a-z0-9]+)*$'",
        name="source_instances_instance_key_slug",
    ),
    CheckConstraint(
        "char_length(display_name) BETWEEN 1 AND 255 AND "
        "display_name !~ '(^[[:space:]]|[[:space:]]$)' AND "
        "position('://' in display_name) = 0 AND position('@' in display_name) = 0",
        name="source_instances_display_name_safe",
    ),
    UniqueConstraint(
        "source_system", "instance_key", name="source_instances_source_instance_key_unique"
    ),
)

process_runs = Table(
    "process_runs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("component_name", String(80), nullable=False),
    Column("run_kind", String(100), nullable=False),
    Column(
        "source_instance_id", UUID(as_uuid=True), ForeignKey(source_instances.c.id), nullable=True
    ),
    Column("requested_by", String(255), nullable=True),
    Column("status", String(16), nullable=False, server_default="pending"),
    Column("terminal_disposition", String(160), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("char_length(component_name) > 0", name="process_runs_component_name_nonempty"),
    CheckConstraint("char_length(run_kind) > 0", name="process_runs_run_kind_nonempty"),
    CheckConstraint(
        "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
        name="process_runs_status_valid",
    ),
    CheckConstraint(
        "(status IN ('succeeded', 'failed', 'cancelled')) = (finished_at IS NOT NULL)",
        name="process_runs_terminal_finished_at",
    ),
    CheckConstraint(
        "(status IN ('succeeded', 'failed', 'cancelled')) = (terminal_disposition IS NOT NULL)",
        name="process_runs_terminal_disposition_required",
    ),
    CheckConstraint(
        "terminal_disposition IS NULL OR terminal_disposition ~ "
        "'^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$'",
        name="process_runs_terminal_disposition_namespaced",
    ),
)
Index("process_runs_component_status_index", process_runs.c.component_name, process_runs.c.status)
Index(
    "process_runs_source_created_index",
    process_runs.c.source_instance_id,
    process_runs.c.created_at,
)

process_units = Table(
    "process_units",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "run_id",
        UUID(as_uuid=True),
        ForeignKey(process_runs.c.id, ondelete="CASCADE"),
        nullable=False,
    ),
    Column("unit_key", String(255), nullable=False),
    Column("attempt", Integer, nullable=False, server_default="0"),
    Column("status", String(16), nullable=False, server_default="pending"),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("char_length(unit_key) > 0", name="process_units_unit_key_nonempty"),
    CheckConstraint("attempt >= 0", name="process_units_attempt_nonnegative"),
    CheckConstraint(
        "status IN ('pending', 'running', 'succeeded', 'failed', 'skipped')",
        name="process_units_status_valid",
    ),
    CheckConstraint(
        "(status IN ('succeeded', 'failed', 'skipped')) = (finished_at IS NOT NULL)",
        name="process_units_terminal_finished_at",
    ),
    UniqueConstraint("run_id", "unit_key", name="process_units_run_unit_key_unique"),
)
Index("process_units_run_status_index", process_units.c.run_id, process_units.c.status)

checkpoints = Table(
    "checkpoints",
    metadata,
    Column(
        "run_id",
        UUID(as_uuid=True),
        ForeignKey(process_runs.c.id, ondelete="CASCADE"),
        nullable=False,
    ),
    Column("checkpoint_key", String(255), nullable=False),
    Column("version", BIGINT, nullable=False, server_default="0"),
    Column("payload", JSONB, nullable=False, server_default="'{}'::jsonb"),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("run_id", "checkpoint_key", name="checkpoints_primary_key"),
    CheckConstraint("char_length(checkpoint_key) > 0", name="checkpoints_key_nonempty"),
    CheckConstraint("version >= 0", name="checkpoints_version_nonnegative"),
)

leases = Table(
    "leases",
    metadata,
    Column("resource_key", String(255), primary_key=True),
    Column("owner_run_id", UUID(as_uuid=True), ForeignKey(process_runs.c.id), nullable=False),
    Column("fence_token", BIGINT, nullable=False),
    Column("acquired_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("char_length(resource_key) > 0", name="leases_resource_key_nonempty"),
    CheckConstraint("fence_token > 0", name="leases_fence_token_positive"),
    CheckConstraint("expires_at > acquired_at", name="leases_expiry_after_acquisition"),
)
Index("leases_owner_run_index", leases.c.owner_run_id)

terminal_accounting = Table(
    "terminal_accounting",
    metadata,
    Column(
        "run_id",
        UUID(as_uuid=True),
        ForeignKey(process_runs.c.id, ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("terminal_disposition", String(160), nullable=False),
    Column("succeeded_count", BIGINT, nullable=False, server_default="0"),
    Column("failed_count", BIGINT, nullable=False, server_default="0"),
    Column("skipped_count", BIGINT, nullable=False, server_default="0"),
    Column("total_count", BIGINT, nullable=False, server_default="0"),
    Column("recorded_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "terminal_disposition ~ '^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$'",
        name="terminal_accounting_disposition_namespaced",
    ),
    CheckConstraint("succeeded_count >= 0", name="terminal_accounting_succeeded_nonnegative"),
    CheckConstraint("failed_count >= 0", name="terminal_accounting_failed_nonnegative"),
    CheckConstraint("skipped_count >= 0", name="terminal_accounting_skipped_nonnegative"),
    CheckConstraint("total_count >= 0", name="terminal_accounting_total_nonnegative"),
    CheckConstraint(
        "total_count = succeeded_count + failed_count + skipped_count",
        name="terminal_accounting_counts_balance",
    ),
)

schema_readiness = Table(
    "schema_readiness",
    metadata,
    Column("component", String(80), primary_key=True),
    Column("is_ready", Boolean, nullable=False),
    Column("expected_revisions", JSONB, nullable=False),
    Column("observed_revisions", JSONB, nullable=False),
    Column("checked_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("details", JSONB, nullable=False, server_default="'{}'::jsonb"),
    CheckConstraint("char_length(component) > 0", name="schema_readiness_component_nonempty"),
)

process_heartbeats = Table(
    "process_heartbeats",
    metadata,
    Column("component", String(80), primary_key=True),
    Column("heartbeat_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("details", JSONB, nullable=False, server_default="'{}'::jsonb"),
    CheckConstraint("char_length(component) > 0", name="process_heartbeats_component_nonempty"),
)

migration_bookkeeping = Table(
    "migration_bookkeeping",
    metadata,
    Column("component_name", String(80), nullable=False),
    Column("revision", String(100), nullable=False),
    Column("applied_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("details", JSONB, nullable=False, server_default="'{}'::jsonb"),
    PrimaryKeyConstraint("component_name", "revision", name="migration_bookkeeping_primary_key"),
    CheckConstraint(
        "char_length(component_name) > 0", name="migration_bookkeeping_component_nonempty"
    ),
    CheckConstraint("char_length(revision) > 0", name="migration_bookkeeping_revision_nonempty"),
)

PLATFORM_TABLE_NAMES: frozenset[str] = frozenset(table.name for table in metadata.sorted_tables)
EXCLUDED_DOMAIN_TABLE_NAMES: frozenset[str] = frozenset(
    {
        "identity",
        "deals",
        "stages",
        "activities",
        "historical_imports",
        "artifacts",
        "projections",
        "outbox",
        "ownership",
        "ownership_epochs",
    }
)


def schema_inventory() -> frozenset[str]:
    return PLATFORM_TABLE_NAMES - EXCLUDED_DOMAIN_TABLE_NAMES
