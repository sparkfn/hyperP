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

# Do not include these statements in routine graph initialization. Existing
# membership nodes require an explicit data backfill before the legacy identity
# constraint can be replaced safely.
MIGRATE_BITRIX_DEAL_SCOPE_LINEAGE_CONSTRAINTS: tuple[str, ...] = (
    "DROP CONSTRAINT crm_deal_scope_membership_identity_unique IF EXISTS",
    """CREATE CONSTRAINT crm_deal_scope_lineage_identity_unique IF NOT EXISTS
FOR (membership:CrmDealScopeMembership)
REQUIRE (membership.source_key, membership.deal_id, membership.scope_sequence) IS UNIQUE""",
)

UPSERT_DEAL_SCOPE_MEMBERSHIPS = """
UNWIND $observations AS observation
MERGE (deal:CrmLogicalDeal {source_key: $source_key, deal_id: observation.deal_id})
ON CREATE SET deal.logical_deal_key = 'bitrix-crm-deal-' + observation.deal_id,
              deal.created_at = datetime(),
              deal.current_scope_sequence = 0
WITH deal, observation,
     CASE
       WHEN deal.current_scope_state IS NULL
         OR deal.current_scope_state <> observation.scope_state
         OR coalesce(deal.current_entity_key, '') <> coalesce(observation.entity_key, '')
         OR coalesce(deal.current_category_id, '') <> coalesce(observation.category_id, '')
       THEN true
       ELSE false
     END AS semantic_change
SET deal.current_scope_sequence = CASE
      WHEN semantic_change THEN coalesce(deal.current_scope_sequence, 0) + 1
      ELSE coalesce(deal.current_scope_sequence, 1)
    END,
    deal.current_scope_state = observation.scope_state,
    deal.current_entity_key = observation.entity_key,
    deal.current_category_id = observation.category_id,
    deal.current_source_record_pk = observation.source_record_pk,
    deal.current_observed_at = datetime(),
    deal.absence_streak = CASE
      WHEN observation.preserve_absence_streak
      THEN coalesce(deal.absence_streak, 0)
      ELSE 0
    END,
    deal.last_present_at = CASE
      WHEN observation.preserve_absence_streak
      THEN deal.last_present_at
      ELSE datetime()
    END,
    deal.updated_at = datetime()
MERGE (membership:CrmDealScopeMembership {
  source_key: $source_key,
  deal_id: observation.deal_id,
  scope_sequence: deal.current_scope_sequence
})
ON CREATE SET membership.membership_id = randomUUID(),
              membership.scope_state = observation.scope_state,
              membership.entity_key = observation.entity_key,
              membership.category_id = observation.category_id,
              membership.source_record_pk = observation.source_record_pk,
              membership.provenance = 'deal_census',
              membership.observed_at = deal.current_observed_at,
              membership.created_at = datetime()
MERGE (membership)-[:FOR_LOGICAL_DEAL]->(deal)
RETURN deal.deal_id AS deal_id,
       deal.current_scope_sequence AS scope_sequence,
       deal.current_scope_state AS scope_state,
       deal.current_entity_key AS entity_key,
       deal.current_category_id AS category_id,
       deal.current_source_record_pk AS source_record_pk
"""

LOCK_AND_RECORD_KNOWN_DEAL_ABSENCE = """
MATCH (deal:CrmLogicalDeal {source_key: $source_key, deal_id: $deal_id})
WHERE deal.current_scope_state IN ['in_scope', 'indeterminate', 'out_of_scope']
SET deal.absence_lock_version = coalesce(deal.absence_lock_version, 0) + 1
WITH deal
SET deal.absence_streak = coalesce(deal.absence_streak, 0) + 1,
    deal.last_absent_at = datetime(),
    deal.updated_at = datetime()
RETURN deal.absence_streak AS absence_streak,
       deal.current_scope_state AS scope_state,
       deal.current_category_id AS category_id,
       deal.current_source_record_pk AS source_record_pk
"""

GET_CURRENT_DEAL_SCOPE_BATCH = """
UNWIND $deal_ids AS requested_deal_id
OPTIONAL MATCH (deal:CrmLogicalDeal {source_key: $source_key, deal_id: requested_deal_id})
RETURN requested_deal_id AS deal_id,
       deal.current_scope_sequence AS scope_sequence,
       deal.current_scope_state AS scope_state,
       deal.current_entity_key AS entity_key,
       deal.current_category_id AS category_id,
       deal.current_source_record_pk AS source_record_pk
ORDER BY deal_id
"""

GET_CURRENT_DEAL_SCOPE = """
MATCH (deal:CrmLogicalDeal {source_key: $source_key, deal_id: $deal_id})
RETURN deal.deal_id AS deal_id,
       deal.current_scope_sequence AS scope_sequence,
       deal.current_scope_state AS scope_state,
       deal.current_entity_key AS entity_key,
       deal.current_category_id AS category_id,
       deal.current_source_record_pk AS source_record_pk
"""
