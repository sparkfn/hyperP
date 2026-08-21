"""Schema definitions for indexes created by the API at startup."""

from __future__ import annotations

import re

PERSON_COMPLETENESS_INDEX_NAME = "idx_person_completeness"
PERSON_COMPLETENESS_INDEX_LABEL = "Person"
PERSON_COMPLETENESS_INDEX_PROPERTY = "profile_completeness_score"
PERSON_CRM_DEAL_COUNT_INDEX_NAME = "idx_person_crm_deal_count"
PERSON_CRM_DEAL_COUNT_INDEX_PROPERTY = "crm_deal_count"


def build_person_completeness_index_cypher(index_name: str) -> str:
    """Build the canonical completeness range-index definition for a safe name."""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", index_name) is None:
        raise ValueError("Person completeness index name must be a safe Cypher identifier")
    return (
        f"CREATE INDEX {index_name} IF NOT EXISTS "
        f"FOR (p:{PERSON_COMPLETENESS_INDEX_LABEL}) "
        f"ON (p.{PERSON_COMPLETENESS_INDEX_PROPERTY})"
    )


def build_person_crm_deal_count_index_cypher(index_name: str) -> str:
    """Build the canonical CRM deal-count range-index definition for a safe name."""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", index_name) is None:
        raise ValueError("Person CRM deal-count index name must be a safe Cypher identifier")
    return (
        f"CREATE INDEX {index_name} IF NOT EXISTS "
        f"FOR (p:{PERSON_COMPLETENESS_INDEX_LABEL}) "
        f"ON (p.{PERSON_CRM_DEAL_COUNT_INDEX_PROPERTY})"
    )


PERSON_COMPLETENESS_INDEX_CYPHER = build_person_completeness_index_cypher(
    PERSON_COMPLETENESS_INDEX_NAME
)

PERSON_CRM_DEAL_COUNT_INDEX_CYPHER = build_person_crm_deal_count_index_cypher(
    PERSON_CRM_DEAL_COUNT_INDEX_NAME
)

PERSON_INDEXES: tuple[str, ...] = (
    PERSON_COMPLETENESS_INDEX_CYPHER,
    PERSON_CRM_DEAL_COUNT_INDEX_CYPHER,
    "CREATE INDEX idx_person_high_value IF NOT EXISTS FOR (p:Person) ON (p.is_high_value)",
    "CREATE INDEX idx_person_high_risk IF NOT EXISTS FOR (p:Person) ON (p.is_high_risk)",
    "CREATE INDEX idx_person_updated_at IF NOT EXISTS FOR (p:Person) ON (p.updated_at)",
)
