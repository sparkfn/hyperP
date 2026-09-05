"""Read-only Cypher for split person CRM metrics."""

from __future__ import annotations

_LIFECYCLE = (
    "(sr.lifecycle_status = 'active' OR (sr.lifecycle_status IS NULL AND sr.is_latest = true))"
)
_LINK_ACTIVE = "coalesce(link.is_active, true) = true"
_ACTIVITY_FAMILY = "(sr.history_family IS NULL OR sr.history_family = 'activity')"
_BITRIX = "EXISTS { MATCH (sr)-[:FROM_SOURCE]->(:SourceSystem {source_key: 'bitrix_chat'}) }"


def _daily_buckets(record_type: str, alias: str) -> str:
    return f"""
CALL (person, as_of_at) {{
  OPTIONAL MATCH (sr:SourceRecord {{record_type: '{record_type}'}})-[link:LINKED_TO]->(person)
  WHERE {_LINK_ACTIVE} AND {_LIFECYCLE} AND {_ACTIVITY_FAMILY} AND {_BITRIX}
  WITH collect(DISTINCT sr) AS records, as_of_at
  RETURN [i IN range(0, 29) |
    size([record IN records WHERE record.observed_at >= as_of_at - duration({{days: 30 - i}})
      AND record.observed_at < as_of_at - duration({{days: 29 - i}})])
  ] AS {alias}_daily_counts
}}
"""


GET_PERSON_CRM_DEAL_METRICS = f"""
MATCH (p:Person {{person_id: $person_id}})
OPTIONAL MATCH (p)-[:MERGED_INTO]->(canonical:Person)
WITH coalesce(canonical, p) AS person, datetime($as_of_at) AS as_of_at
CALL (person, as_of_at) {{
  OPTIONAL MATCH (sr:SourceRecord {{record_type: 'crm_deal'}})-[link:LINKED_TO]->(person)
  WHERE {_LINK_ACTIVE} AND {_LIFECYCLE} AND {_ACTIVITY_FAMILY} AND {_BITRIX}
  WITH sr, sr.observed_at AS at
  RETURN count(DISTINCT sr) AS deal_count, min(at) AS first_deal_at, max(at) AS last_deal_at,
         count(DISTINCT CASE WHEN at >= as_of_at - duration('P30D') AND at <= as_of_at THEN sr END) AS recent_30d_deal_count,
         count(DISTINCT CASE WHEN at >= as_of_at - duration('P60D') AND at < as_of_at - duration('P30D') THEN sr END) AS prior_30d_deal_count
}}
CALL (person, as_of_at) {{
  OPTIONAL MATCH (sr:SourceRecord {{record_type: 'crm_deal'}})-[link:LINKED_TO]->(person)
  WHERE {_LINK_ACTIVE} AND {_LIFECYCLE} AND {_ACTIVITY_FAMILY} AND {_BITRIX}
  WITH sr.crm_deal_stage_id AS stage_id, sr
  WITH stage_id, count(DISTINCT sr) AS count ORDER BY stage_id
  RETURN collect(CASE WHEN stage_id IS NULL THEN null ELSE {{stage_id: stage_id, count: count}} END) AS deal_stage_breakdown
}}
CALL (person, as_of_at) {{
  OPTIONAL MATCH (sr:SourceRecord {{record_type: 'conversation'}})-[link:LINKED_TO]->(person)
  WHERE {_LINK_ACTIVE} AND {_LIFECYCLE} AND {_ACTIVITY_FAMILY} AND {_BITRIX}
  WITH sr, sr.observed_at AS at
  RETURN count(DISTINCT sr) AS conversation_count, max(at) AS last_conversation_at,
         count(DISTINCT CASE WHEN at >= as_of_at - duration('P30D') AND at <= as_of_at THEN sr END) AS recent_30d_conversation_count,
         count(DISTINCT CASE WHEN at >= as_of_at - duration('P60D') AND at < as_of_at - duration('P30D') THEN sr END) AS prior_30d_conversation_count
}}
CALL (person) {{
  OPTIONAL MATCH (sr:SourceRecord)-[link:LINKED_TO]->(person)
  WHERE {_LINK_ACTIVE} AND {_LIFECYCLE} AND {_ACTIVITY_FAMILY} AND {_BITRIX}
    AND sr.record_type IN ['crm_deal', 'conversation']
  OPTIONAL MATCH (sr)-[:OWNED_BY]->(record_entity:Entity)
  OPTIONAL MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem {{source_key: 'bitrix_chat'}})
  OPTIONAL MATCH (ss)-[:OPERATED_BY]->(source_entity:Entity)
  WITH coalesce(record_entity, source_entity) AS entity, sr
  WITH entity.entity_key AS entity_key,
       entity.display_name AS entity_display_name, sr
  WITH entity_key, entity_display_name,
       count(DISTINCT CASE WHEN sr.record_type = 'crm_deal' THEN sr END) AS deal_count,
       count(DISTINCT CASE WHEN sr.record_type = 'conversation' THEN sr END) AS conversation_count
  ORDER BY entity_display_name, entity_key
  RETURN collect(CASE WHEN entity_key IS NULL THEN null ELSE {{entity_key: entity_key, entity_display_name: entity_display_name, deal_count: deal_count, conversation_count: conversation_count}} END) AS entity_breakdown
}}
{_daily_buckets("crm_deal", "deal")}
{_daily_buckets("conversation", "conversation")}
RETURN deal_count, deal_stage_breakdown, first_deal_at, last_deal_at, recent_30d_deal_count,
       prior_30d_deal_count, conversation_count, last_conversation_at, recent_30d_conversation_count,
       prior_30d_conversation_count, entity_breakdown, deal_daily_counts,
       conversation_daily_counts,
       CASE WHEN last_deal_at IS NULL OR (last_conversation_at IS NOT NULL AND last_conversation_at > last_deal_at) THEN last_conversation_at ELSE last_deal_at END AS last_graph_crm_touch_at,
       CASE WHEN last_deal_at IS NULL THEN null WHEN last_deal_at >= as_of_at THEN 0 ELSE toInteger(floor(duration.inSeconds(last_deal_at, as_of_at).seconds / 86400.0)) END AS days_since_last_deal
"""


GET_PERSON_BITRIX_DEAL_SCOPE = f"""
MATCH (p:Person {{person_id: $person_id}})
OPTIONAL MATCH (p)-[:MERGED_INTO]->(canonical:Person)
WITH coalesce(canonical, p) AS person
CALL (person) {{
  MATCH (sr:SourceRecord {{record_type: 'crm_deal'}})-[link:LINKED_TO]->(person)
  WHERE {_LINK_ACTIVE} AND {_LIFECYCLE} AND {_ACTIVITY_FAMILY} AND {_BITRIX}
    AND sr.source_instance_id = $source_instance
    AND sr.source_entity_type = 'deal'
    AND sr.source_entity_id IS NOT NULL
  RETURN DISTINCT sr.source_entity_id AS deal_id ORDER BY deal_id LIMIT $deal_limit_plus_one
}}
RETURN person.person_id AS canonical_person_id, collect(deal_id) AS deal_ids
"""
