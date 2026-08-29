"""Baseline revision; intentionally creates no application tables."""

from __future__ import annotations

revision = "di_0001_baseline"
down_revision = None
branch_labels = ("baseline",)
depends_on = None


def upgrade() -> None:
    """Establish the revision graph without changing database state."""


def downgrade() -> None:
    """Revert the no-op baseline revision."""
