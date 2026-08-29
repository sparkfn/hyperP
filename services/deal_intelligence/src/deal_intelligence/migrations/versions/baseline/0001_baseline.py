"""Create package-owned bookkeeping required before component migrations branch."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "di_0001_baseline"
down_revision = None
branch_labels = ("baseline",)
depends_on = None


def upgrade() -> None:
    """Create the package schema and its baseline migration bookkeeping table."""
    op.execute("CREATE SCHEMA IF NOT EXISTS deal_intelligence")
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
        schema="deal_intelligence",
    )


def downgrade() -> None:
    """Drop only the package-owned baseline bookkeeping and schema."""
    op.drop_table("migration_bookkeeping", schema="deal_intelligence")
    op.execute("DROP SCHEMA IF EXISTS deal_intelligence")
