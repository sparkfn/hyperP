"""Create immutable shared Deal Intelligence platform tables only."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "di_0002_shared_platform"
down_revision = "di_0001_baseline"
branch_labels = ("platform",)
depends_on = None

_SCHEMA = "deal_intelligence"


def upgrade() -> None:
    """Create the approved generic platform inventory using explicit Alembic DDL."""
    op.execute("CREATE SCHEMA IF NOT EXISTS deal_intelligence")
    op.create_table(
        "source_instances",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_system", sa.String(length=80), nullable=False),
        sa.Column("instance_key", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "char_length(source_system) BETWEEN 1 AND 80 AND "
            "source_system ~ '^[a-z][a-z0-9]*([_-][a-z0-9]+)*$'",
            name="source_instances_source_system_slug",
        ),
        sa.CheckConstraint(
            "char_length(instance_key) BETWEEN 1 AND 255 AND "
            "instance_key ~ '^[a-z][a-z0-9]*([_-][a-z0-9]+)*$'",
            name="source_instances_instance_key_slug",
        ),
        sa.CheckConstraint(
            "char_length(display_name) BETWEEN 1 AND 255 AND "
            "display_name !~ '(^[[:space:]]|[[:space:]]$)' AND "
            "position('://' in display_name) = 0 AND position('@' in display_name) = 0",
            name="source_instances_display_name_safe",
        ),
        sa.UniqueConstraint(
            "source_system", "instance_key", name="source_instances_source_instance_key_unique"
        ),
        schema=_SCHEMA,
    )
    op.create_table(
        "process_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("component_name", sa.String(length=80), nullable=False),
        sa.Column("run_kind", sa.String(length=100), nullable=False),
        sa.Column(
            "source_instance_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{_SCHEMA}.source_instances.id"),
            nullable=True,
        ),
        sa.Column("requested_by", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("terminal_disposition", sa.String(length=160), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "char_length(component_name) > 0", name="process_runs_component_name_nonempty"
        ),
        sa.CheckConstraint("char_length(run_kind) > 0", name="process_runs_run_kind_nonempty"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name="process_runs_status_valid",
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded', 'failed', 'cancelled')) = (finished_at IS NOT NULL)",
            name="process_runs_terminal_finished_at",
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded', 'failed', 'cancelled')) = (terminal_disposition IS NOT NULL)",
            name="process_runs_terminal_disposition_required",
        ),
        sa.CheckConstraint(
            "terminal_disposition IS NULL OR terminal_disposition ~ "
            "'^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$'",
            name="process_runs_terminal_disposition_namespaced",
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "process_runs_component_status_index",
        "process_runs",
        ["component_name", "status"],
        schema=_SCHEMA,
    )
    op.create_index(
        "process_runs_source_created_index",
        "process_runs",
        ["source_instance_id", "created_at"],
        schema=_SCHEMA,
    )
    op.create_table(
        "process_units",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{_SCHEMA}.process_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("unit_key", sa.String(length=255), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("char_length(unit_key) > 0", name="process_units_unit_key_nonempty"),
        sa.CheckConstraint("attempt >= 0", name="process_units_attempt_nonnegative"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'skipped')",
            name="process_units_status_valid",
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded', 'failed', 'skipped')) = (finished_at IS NOT NULL)",
            name="process_units_terminal_finished_at",
        ),
        sa.UniqueConstraint("run_id", "unit_key", name="process_units_run_unit_key_unique"),
        schema=_SCHEMA,
    )
    op.create_index(
        "process_units_run_status_index", "process_units", ["run_id", "status"], schema=_SCHEMA
    )
    op.create_table(
        "checkpoints",
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{_SCHEMA}.process_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("checkpoint_key", sa.String(length=255), nullable=False),
        sa.Column("version", sa.BIGINT(), nullable=False, server_default="0"),
        sa.Column(
            "payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("run_id", "checkpoint_key", name="checkpoints_primary_key"),
        sa.CheckConstraint("char_length(checkpoint_key) > 0", name="checkpoints_key_nonempty"),
        sa.CheckConstraint("version >= 0", name="checkpoints_version_nonnegative"),
        schema=_SCHEMA,
    )
    op.create_table(
        "leases",
        sa.Column("resource_key", sa.String(length=255), primary_key=True),
        sa.Column(
            "owner_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{_SCHEMA}.process_runs.id"),
            nullable=False,
        ),
        sa.Column("fence_token", sa.BIGINT(), nullable=False),
        sa.Column(
            "acquired_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("char_length(resource_key) > 0", name="leases_resource_key_nonempty"),
        sa.CheckConstraint("fence_token > 0", name="leases_fence_token_positive"),
        sa.CheckConstraint("expires_at > acquired_at", name="leases_expiry_after_acquisition"),
        schema=_SCHEMA,
    )
    op.create_index("leases_owner_run_index", "leases", ["owner_run_id"], schema=_SCHEMA)
    op.create_table(
        "terminal_accounting",
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{_SCHEMA}.process_runs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("terminal_disposition", sa.String(length=160), nullable=False),
        sa.Column("succeeded_count", sa.BIGINT(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.BIGINT(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.BIGINT(), nullable=False, server_default="0"),
        sa.Column("total_count", sa.BIGINT(), nullable=False, server_default="0"),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "terminal_disposition ~ '^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$'",
            name="terminal_accounting_disposition_namespaced",
        ),
        sa.CheckConstraint(
            "succeeded_count >= 0", name="terminal_accounting_succeeded_nonnegative"
        ),
        sa.CheckConstraint("failed_count >= 0", name="terminal_accounting_failed_nonnegative"),
        sa.CheckConstraint("skipped_count >= 0", name="terminal_accounting_skipped_nonnegative"),
        sa.CheckConstraint("total_count >= 0", name="terminal_accounting_total_nonnegative"),
        sa.CheckConstraint(
            "total_count = succeeded_count + failed_count + skipped_count",
            name="terminal_accounting_counts_balance",
        ),
        schema=_SCHEMA,
    )
    op.create_table(
        "schema_readiness",
        sa.Column("component", sa.String(length=80), primary_key=True),
        sa.Column("is_ready", sa.Boolean(), nullable=False),
        sa.Column("expected_revisions", postgresql.JSONB(), nullable=False),
        sa.Column("observed_revisions", postgresql.JSONB(), nullable=False),
        sa.Column(
            "checked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.CheckConstraint(
            "char_length(component) > 0", name="schema_readiness_component_nonempty"
        ),
        schema=_SCHEMA,
    )
    op.create_table(
        "process_heartbeats",
        sa.Column("component", sa.String(length=80), primary_key=True),
        sa.Column(
            "heartbeat_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.CheckConstraint(
            "char_length(component) > 0", name="process_heartbeats_component_nonempty"
        ),
        schema=_SCHEMA,
    )
    op.create_table(
        "migration_bookkeeping",
        sa.Column("component_name", sa.String(length=80), nullable=False),
        sa.Column("revision", sa.String(length=100), nullable=False),
        sa.Column(
            "applied_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.PrimaryKeyConstraint(
            "component_name", "revision", name="migration_bookkeeping_primary_key"
        ),
        sa.CheckConstraint(
            "char_length(component_name) > 0", name="migration_bookkeeping_component_nonempty"
        ),
        sa.CheckConstraint(
            "char_length(revision) > 0", name="migration_bookkeeping_revision_nonempty"
        ),
        schema=_SCHEMA,
    )


def downgrade() -> None:
    """Drop only explicitly created Deal Intelligence objects, preserving external state."""
    for table_name in (
        "migration_bookkeeping",
        "process_heartbeats",
        "schema_readiness",
        "terminal_accounting",
        "leases",
        "checkpoints",
        "process_units",
        "process_runs",
        "source_instances",
    ):
        op.drop_table(table_name, schema=_SCHEMA)
    op.execute("DROP SCHEMA IF EXISTS deal_intelligence")
