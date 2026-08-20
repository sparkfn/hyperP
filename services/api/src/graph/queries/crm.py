"""Read-only Cypher for per-person CRM engagement metrics."""

from __future__ import annotations

_LIFECYCLE = (
    "(sr.lifecycle_status = 'active' OR (sr.lifecycle_status IS NULL AND sr.is_latest = true))"
)

_LINK_ACTIVE = "coalesce(link.is_active, true) = true"

_ACTIVITY_FAMILY = "(sr.history_family IS NULL OR sr.history_family = 'activity')"

_BITRIX_SOURCE = (
    "(sr IS NULL OR EXISTS {"
    " MATCH (sr)-[:FROM_SOURCE]->(:SourceSystem {source_key: 'bitrix_chat'})"
    "})"
)

GET_PERSON_CRM_METRICS = f"""
MATCH (p:Person {{person_id: $person_id}})
OPTIONAL MATCH (p)-[:MERGED_INTO]->(canonical:Person)
WITH coalesce(canonical, p) AS person

CALL (person) {{
  OPTIONAL MATCH (sr:SourceRecord {{record_type: 'crm_deal'}})-[link:LINKED_TO]->(person)
  WHERE {_LINK_ACTIVE}
    AND {_LIFECYCLE}
    AND {_ACTIVITY_FAMILY}
    AND {_BITRIX_SOURCE}
  RETURN count(sr) AS deal_count,
         min(sr.observed_at) AS first_deal_at,
         max(sr.observed_at) AS last_deal_at
}}

CALL (person) {{
  OPTIONAL MATCH (sr:SourceRecord {{record_type: 'crm_deal'}})-[link:LINKED_TO]->(person)
  WHERE {_LINK_ACTIVE}
    AND {_LIFECYCLE}
    AND {_ACTIVITY_FAMILY}
    AND {_BITRIX_SOURCE}
  WITH CASE
         WHEN sr IS NOT NULL
          AND sr.raw_payload IS NOT NULL
          AND sr.raw_payload.stage_id IS NOT NULL
         THEN sr.raw_payload.stage_id
       END AS stage_id
  WITH stage_id, count(*) AS cnt
  ORDER BY stage_id
  RETURN collect(
    CASE WHEN stage_id IS NOT NULL THEN {{stage_id: stage_id, count: cnt}} END
  ) AS deal_stage_breakdown
}}

CALL (person) {{
  OPTIONAL MATCH (sr:SourceRecord {{record_type: 'crm_history'}})-[link:LINKED_TO]->(person)
  WHERE {_LINK_ACTIVE}
    AND {_LIFECYCLE}
    AND {_ACTIVITY_FAMILY}
    AND {_BITRIX_SOURCE}
  RETURN count(sr) AS activity_count,
         min(coalesce(sr.event_at, sr.observed_at)) AS first_activity_at,
         max(coalesce(sr.event_at, sr.observed_at)) AS last_activity_at
}}

CALL (person) {{
  OPTIONAL MATCH (sr:SourceRecord {{record_type: 'crm_history'}})-[link:LINKED_TO]->(person)
  WHERE {_LINK_ACTIVE}
    AND {_LIFECYCLE}
    AND {_ACTIVITY_FAMILY}
    AND {_BITRIX_SOURCE}
  WITH CASE WHEN sr IS NOT NULL THEN coalesce(sr.history_kind, 'unknown') END AS history_kind,
       CASE WHEN sr IS NOT NULL THEN coalesce(sr.event_at, sr.observed_at) END AS event_at
  WITH history_kind, count(*) AS cnt, max(event_at) AS last_event_at
  ORDER BY cnt DESC, history_kind
  RETURN collect(
    CASE WHEN history_kind IS NOT NULL
      THEN {{history_kind: history_kind, count: cnt, last_event_at: last_event_at}}
    END
  ) AS activity_kind_breakdown
}}

CALL (person) {{
  OPTIONAL MATCH (sr:SourceRecord {{record_type: 'call'}})-[link:LINKED_TO]->(person)
  WHERE {_LINK_ACTIVE}
    AND {_LIFECYCLE}
    AND {_ACTIVITY_FAMILY}
    AND {_BITRIX_SOURCE}
  RETURN count(sr) AS call_count
}}

CALL (person) {{
  OPTIONAL MATCH (sr:SourceRecord {{record_type: 'conversation'}})-[link:LINKED_TO]->(person)
  WHERE {_LINK_ACTIVE}
    AND {_LIFECYCLE}
    AND {_ACTIVITY_FAMILY}
    AND {_BITRIX_SOURCE}
  RETURN count(sr) AS conversation_count
}}

CALL (person) {{
  OPTIONAL MATCH (sr:SourceRecord)-[link:LINKED_TO]->(person)
  WHERE {_LINK_ACTIVE}
    AND {_LIFECYCLE}
    AND {_ACTIVITY_FAMILY}
    AND {_BITRIX_SOURCE}
    AND sr.record_type IN ['crm_deal', 'crm_history', 'conversation']
  OPTIONAL MATCH (sr)-[:OWNED_BY]->(record_entity:Entity)
  OPTIONAL MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem {{source_key: 'bitrix_chat'}})
  OPTIONAL MATCH (ss)-[:OPERATED_BY]->(source_entity:Entity)
  WITH sr, coalesce(record_entity, source_entity) AS entity
  WITH CASE WHEN entity IS NOT NULL THEN entity.entity_key END AS entity_key,
       CASE WHEN entity IS NOT NULL THEN entity.display_name END AS entity_display_name,
       CASE WHEN entity IS NOT NULL AND sr.record_type = 'crm_deal' THEN 1 ELSE 0 END AS is_deal,
       CASE WHEN entity IS NOT NULL AND sr.record_type = 'crm_history' THEN 1 ELSE 0 END AS is_activity,
       CASE WHEN entity IS NOT NULL AND sr.record_type = 'conversation' THEN 1 ELSE 0 END AS is_conversation
  WITH entity_key, entity_display_name,
       sum(is_deal) AS deal_count,
       sum(is_activity) AS activity_count,
       sum(is_conversation) AS conversation_count
  ORDER BY entity_display_name, entity_key
  RETURN collect(
    CASE WHEN entity_key IS NOT NULL THEN {{
      entity_key: entity_key,
      entity_display_name: entity_display_name,
      deal_count: deal_count,
      activity_count: activity_count,
      conversation_count: conversation_count
    }} END
  ) AS entity_breakdown
}}

RETURN deal_count,
       deal_stage_breakdown,
       first_deal_at,
       last_deal_at,
       activity_count,
       call_count,
       conversation_count,
       activity_kind_breakdown,
       first_activity_at,
       last_activity_at,
       entity_breakdown
"""
