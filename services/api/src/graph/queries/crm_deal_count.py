"""Transactional CRM deal-count projection maintenance."""

from __future__ import annotations

RECOMPUTE_PERSON_CRM_DEAL_COUNTS = """
UNWIND $person_ids AS person_id
MATCH (person:Person {person_id: person_id})
WITH DISTINCT person
ORDER BY person.person_id
SET person.crm_deal_count_lock_version =
    coalesce(person.crm_deal_count_lock_version, 0) + 1
WITH person
CALL (person) {
  OPTIONAL MATCH (deal:SourceRecord {record_type: 'crm_deal'})-[link:LINKED_TO]->(person)
  WHERE coalesce(link.is_active, true) = true
    AND (deal.history_family IS NULL OR deal.history_family = 'activity')
    AND (deal.lifecycle_status = 'active'
      OR (deal.lifecycle_status IS NULL AND deal.is_latest = true))
    AND EXISTS {
      MATCH (deal)-[:FROM_SOURCE]->(:SourceSystem {source_key: 'bitrix_chat'})
    }
  RETURN count(DISTINCT deal) AS authoritative_count
}
SET person.crm_deal_count = authoritative_count,
    person.crm_deal_count_updated_at = datetime()
RETURN person.person_id AS person_id
ORDER BY person_id
"""
