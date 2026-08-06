"""Cypher for durable current Bitrix deal scope and append-only lineage."""

from __future__ import annotations

CREATE_BITRIX_DEAL_SCOPE_CONSTRAINTS: tuple[str, ...] = (
    """CREATE CONSTRAINT crm_logical_deal_identity_unique IF NOT EXISTS
FOR (deal:CrmLogicalDeal)
REQUIRE (deal.source_key, deal.deal_id) IS UNIQUE""",
    """CREATE CONSTRAINT crm_deal_scope_membership_identity_unique IF NOT EXISTS
FOR (membership:CrmDealScopeMembership)
REQUIRE (membership.source_record_pk, membership.scope_state) IS UNIQUE""",
)

UPSERT_IN_SCOPE_DEAL_MEMBERSHIP = """
MATCH (record:SourceRecord {source_record_pk: $source_record_pk, record_type: 'crm_deal'})
      -[:FROM_SOURCE]->(:SourceSystem {source_key: 'bitrix_chat'})
MATCH (record)-[:OWNED_BY]->(entity:Entity {entity_key: $entity_key})
MERGE (deal:CrmLogicalDeal {source_key: 'bitrix_chat', deal_id: $deal_id})
ON CREATE SET deal.logical_deal_key = 'bitrix-crm-deal-' + $deal_id,
              deal.created_at = datetime(),
              deal.current_scope_sequence = 0
SET deal.current_scope_sequence = CASE
      WHEN deal.current_source_record_pk = record.source_record_pk
      THEN deal.current_scope_sequence
      ELSE deal.current_scope_sequence + 1
    END,
    deal.current_scope_state = 'in_scope',
    deal.current_entity_key = entity.entity_key,
    deal.current_category_id = $category_id,
    deal.current_observed_at = record.observed_at,
    deal.current_source_record_pk = record.source_record_pk,
    deal.updated_at = datetime()
MERGE (membership:CrmDealScopeMembership {
  source_record_pk: record.source_record_pk,
  scope_state: 'in_scope'
})
ON CREATE SET membership.membership_id = randomUUID(),
              membership.source_key = 'bitrix_chat',
              membership.deal_id = $deal_id,
              membership.entity_key = entity.entity_key,
              membership.category_id = $category_id,
              membership.observed_at = record.observed_at,
              membership.provenance = 'deal_census',
              membership.created_at = datetime()
MERGE (membership)-[:FOR_LOGICAL_DEAL]->(deal)
MERGE (membership)-[:OBSERVED_FROM]->(record)
RETURN deal.current_scope_sequence AS scope_sequence,
       deal.current_scope_state AS scope_state,
       deal.current_entity_key AS entity_key
"""

GET_CURRENT_DEAL_SCOPE = """
MATCH (deal:CrmLogicalDeal {source_key: 'bitrix_chat', deal_id: $deal_id})
RETURN deal.current_scope_sequence AS scope_sequence,
       deal.current_scope_state AS scope_state,
       deal.current_entity_key AS entity_key,
       deal.current_category_id AS category_id,
       deal.current_source_record_pk AS source_record_pk
"""
