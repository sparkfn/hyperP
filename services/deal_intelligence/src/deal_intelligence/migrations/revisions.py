"""Read-only revision graph helpers used by readiness checks."""

from __future__ import annotations

from alembic.script import ScriptDirectory

from deal_intelligence.migrations.cli import alembic_config


def expected_heads() -> frozenset[str]:
    """Return all package-local Alembic heads without applying migrations."""
    return frozenset(ScriptDirectory.from_config(alembic_config()).get_heads())
