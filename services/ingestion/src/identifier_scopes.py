"""Identifier namespace rules for graph identity and candidate matching."""

from __future__ import annotations

from src.source_instances import effective_source_instance_id

# Non-CRM identifiers deliberately share one global namespace.
GLOBAL_IDENTIFIER_SCOPE = "global"

# CRM primary keys are only meaningful within one configured CRM portal.
CRM_CANONICAL_IDENTIFIER_TYPES = frozenset(
    {"crm_contact_id", "crm_lead_id", "crm_company_id"}
)


def source_instance_for_identifier(
    identifier_type: str,
    source_instance_id: str | None,
) -> str | None:
    """Return the instance namespace for CRM canonical identifiers only."""
    if identifier_type in CRM_CANONICAL_IDENTIFIER_TYPES:
        return effective_source_instance_id(source_instance_id)
    return None


def identifier_scope(
    identifier_type: str,
    source_instance_id: str | None,
) -> str:
    """Return the canonical non-null graph identity scope for an identifier."""
    scoped_instance = source_instance_for_identifier(identifier_type, source_instance_id)
    return scoped_instance or GLOBAL_IDENTIFIER_SCOPE
