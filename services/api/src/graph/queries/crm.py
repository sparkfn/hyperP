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
_RECENT = "sr_timestamp >= as_of_at - duration('P30D') AND sr_timestamp <= as_of_at"


def _daily_buckets(record_type: str, timestamp_expr: str, alias: str) -> str:
    """Build a CALL sub-query that returns 30 daily counts (oldest → newest),
    the recent_30d total, and the prior_30d total for a given record_type.

    `timestamp_expr` is a Cypher expression for the record's event timestamp
    (e.g. ``sr.observed_at`` for deals, ``coalesce(sr.event_at, sr.observed_at)``
    for activities). Bucket alignment is UTC midnight, with bucket 29 (the
    newest) ending at as_of_at and bucket 0 (the oldest) ending 30 days before
    as_of_at. The prior_30d window covers days 60..30 before as_of_at.

    `alias` is suffixed to the returned column names (``{alias}_daily_counts``,
    ``{alias}_recent_total``, ``{alias}_prior_total``) so that multiple sub-
    queries can coexist in the outer WITH clause without column-name clashes.
    """
    return f"""
CALL (person, as_of_at) {{
  OPTIONAL MATCH (sr:SourceRecord {{record_type: '{record_type}'}})-[link:LINKED_TO]->(person)
  WHERE {_LINK_ACTIVE}
    AND {_LIFECYCLE}
    AND {_ACTIVITY_FAMILY}
    AND {_BITRIX_SOURCE}
  WITH sr, {timestamp_expr} AS sr_timestamp
  WITH sr_timestamp,
       CASE
         WHEN sr_timestamp >= as_of_at - duration('P30D') THEN 1
         ELSE 0
       END AS in_recent,
       CASE
         WHEN sr_timestamp >= as_of_at - duration('P60D')
          AND sr_timestamp <  as_of_at - duration('P30D') THEN 1
         ELSE 0
       END AS in_prior
  WITH collect({{ts: sr_timestamp, recent: in_recent, prior: in_prior}}) AS events
  WITH [i IN range(0, 29) |
         as_of_at - duration({{days: 30 - i}})
       ] AS bucket_starts,
       events
  RETURN [bucket_start IN bucket_starts |
           size([e IN events
                 WHERE e.ts >= bucket_start
                   AND e.ts <  bucket_start + duration('P1D')
                   AND e.recent = 1])
         ] AS {alias}_daily_counts,
         size([e IN events WHERE e.recent = 1]) AS {alias}_recent_total,
         size([e IN events WHERE e.prior = 1]) AS {alias}_prior_total
}}
"""


GET_PERSON_CRM_METRICS = f"""
MATCH (p:Person {{person_id: $person_id}})
OPTIONAL MATCH (p)-[:MERGED_INTO]->(canonical:Person)
WITH coalesce(canonical, p) AS person, datetime($as_of_at) AS as_of_at

CALL (person, as_of_at) {{
  OPTIONAL MATCH (sr:SourceRecord {{record_type: 'crm_deal'}})-[link:LINKED_TO]->(person)
  WHERE {_LINK_ACTIVE}
    AND {_LIFECYCLE}
    AND {_ACTIVITY_FAMILY}
    AND {_BITRIX_SOURCE}
  WITH sr, as_of_at, sr.observed_at AS sr_timestamp
  RETURN count(DISTINCT sr) AS deal_count,
         min(sr_timestamp) AS first_deal_at,
         max(sr_timestamp) AS last_deal_at,
         count(DISTINCT CASE WHEN {_RECENT} THEN sr END) AS recent_30d_deal_count
}}

CALL (person) {{
  OPTIONAL MATCH (sr:SourceRecord {{record_type: 'crm_deal'}})-[link:LINKED_TO]->(person)
  WHERE {_LINK_ACTIVE}
    AND {_LIFECYCLE}
    AND {_ACTIVITY_FAMILY}
    AND {_BITRIX_SOURCE}
  WITH sr.crm_deal_stage_id AS stage_id, sr
  WITH stage_id, count(DISTINCT sr) AS cnt
  ORDER BY stage_id
  RETURN collect(
    CASE WHEN stage_id IS NOT NULL THEN {{stage_id: stage_id, count: cnt}} END
  ) AS deal_stage_breakdown
}}

CALL (person, as_of_at) {{
  OPTIONAL MATCH (sr:SourceRecord {{record_type: 'crm_history'}})-[link:LINKED_TO]->(person)
  WHERE {_LINK_ACTIVE}
    AND {_LIFECYCLE}
    AND {_ACTIVITY_FAMILY}
    AND {_BITRIX_SOURCE}
  WITH sr, as_of_at, coalesce(sr.event_at, sr.observed_at) AS sr_timestamp
  RETURN count(DISTINCT sr) AS activity_count,
         min(sr_timestamp) AS first_activity_at,
         max(sr_timestamp) AS last_activity_at,
         count(DISTINCT CASE WHEN {_RECENT} THEN sr END) AS recent_30d_activity_count
}}

CALL (person) {{
  OPTIONAL MATCH (sr:SourceRecord {{record_type: 'crm_history'}})-[link:LINKED_TO]->(person)
  WHERE {_LINK_ACTIVE}
    AND {_LIFECYCLE}
    AND {_ACTIVITY_FAMILY}
    AND {_BITRIX_SOURCE}
  WITH CASE WHEN sr IS NOT NULL THEN coalesce(sr.history_kind, 'unknown') END AS history_kind,
       CASE WHEN sr IS NOT NULL THEN coalesce(sr.event_at, sr.observed_at) END AS event_at,
       sr
  WITH history_kind, count(DISTINCT sr) AS cnt, max(event_at) AS last_event_at
  ORDER BY cnt DESC, history_kind
  RETURN collect(
    CASE WHEN history_kind IS NOT NULL
      THEN {{history_kind: history_kind, count: cnt, last_event_at: last_event_at}}
    END
  ) AS activity_kind_breakdown
}}

CALL (person, as_of_at) {{
  OPTIONAL MATCH (sr:SourceRecord {{record_type: 'call'}})-[link:LINKED_TO]->(person)
  WHERE {_LINK_ACTIVE}
    AND {_LIFECYCLE}
    AND {_ACTIVITY_FAMILY}
    AND {_BITRIX_SOURCE}
  WITH sr, as_of_at, sr.observed_at AS sr_timestamp
  RETURN count(DISTINCT sr) AS call_count,
         max(sr_timestamp) AS last_call_at,
         count(DISTINCT CASE WHEN {_RECENT} THEN sr END) AS recent_30d_call_count
}}

CALL (person, as_of_at) {{
  OPTIONAL MATCH (sr:SourceRecord {{record_type: 'conversation'}})-[link:LINKED_TO]->(person)
  WHERE {_LINK_ACTIVE}
    AND {_LIFECYCLE}
    AND {_ACTIVITY_FAMILY}
    AND {_BITRIX_SOURCE}
  WITH sr, as_of_at, sr.observed_at AS sr_timestamp
  RETURN count(DISTINCT sr) AS conversation_count,
         max(sr_timestamp) AS last_conversation_at,
         count(DISTINCT CASE WHEN {_RECENT} THEN sr END) AS recent_30d_conversation_count
}}

{_daily_buckets('crm_deal', 'sr.observed_at', 'deal')}

{_daily_buckets('crm_history', 'coalesce(sr.event_at, sr.observed_at)', 'activity')}

{_daily_buckets('call', 'sr.observed_at', 'call')}

{_daily_buckets('conversation', 'sr.observed_at', 'conversation')}

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
       sr
  WITH entity_key,
       entity_display_name,
       count(DISTINCT CASE WHEN sr.record_type = 'crm_deal' THEN sr END) AS deal_count,
       count(DISTINCT CASE WHEN sr.record_type = 'crm_history' THEN sr END) AS activity_count,
       count(DISTINCT CASE WHEN sr.record_type = 'conversation' THEN sr END) AS conversation_count
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

WITH deal_count,
     deal_stage_breakdown,
     first_deal_at,
     last_deal_at,
     recent_30d_deal_count,
     activity_count,
     call_count,
     conversation_count,
     activity_kind_breakdown,
     first_activity_at,
     last_activity_at,
     recent_30d_activity_count,
     recent_30d_call_count,
     recent_30d_conversation_count,
     entity_breakdown,
     deal_daily_counts,
     deal_recent_total,
     deal_prior_total,
     activity_daily_counts,
     activity_recent_total,
     activity_prior_total,
     call_daily_counts,
     call_recent_total,
     call_prior_total,
     conversation_daily_counts,
     conversation_recent_total,
     conversation_prior_total,
     as_of_at,
     [timestamp IN [last_deal_at, last_activity_at, last_call_at, last_conversation_at]
      WHERE timestamp IS NOT NULL] AS touch_timestamps
WITH deal_count,
     deal_stage_breakdown,
     first_deal_at,
     last_deal_at,
     recent_30d_deal_count,
     activity_count,
     call_count,
     conversation_count,
     activity_kind_breakdown,
     first_activity_at,
     last_activity_at,
     recent_30d_activity_count,
     recent_30d_call_count,
     recent_30d_conversation_count,
     entity_breakdown,
     deal_daily_counts,
     deal_recent_total,
     deal_prior_total,
     activity_daily_counts,
     activity_recent_total,
     activity_prior_total,
     call_daily_counts,
     call_recent_total,
     call_prior_total,
     conversation_daily_counts,
     conversation_recent_total,
     conversation_prior_total,
     as_of_at,
     reduce(latest = null, timestamp IN touch_timestamps |
       CASE WHEN latest IS NULL OR timestamp > latest THEN timestamp ELSE latest END
     ) AS last_crm_touch_at
RETURN deal_count,
       deal_stage_breakdown,
       first_deal_at,
       last_deal_at,
       recent_30d_deal_count,
       activity_count,
       call_count,
       conversation_count,
       activity_kind_breakdown,
       first_activity_at,
       last_activity_at,
       recent_30d_activity_count,
       recent_30d_call_count,
       recent_30d_conversation_count,
       entity_breakdown,
       deal_daily_counts,
       deal_recent_total,
       deal_prior_total,
       CASE WHEN deal_prior_total = 0 THEN null
            ELSE toInteger(round((toFloat(deal_recent_total) - deal_prior_total) * 100.0 / deal_prior_total))
       END AS deal_change_pct,
       activity_daily_counts,
       activity_recent_total,
       activity_prior_total,
       CASE WHEN activity_prior_total = 0 THEN null
            ELSE toInteger(round((toFloat(activity_recent_total) - activity_prior_total) * 100.0 / activity_prior_total))
       END AS activity_change_pct,
       call_daily_counts,
       call_recent_total,
       call_prior_total,
       CASE WHEN call_prior_total = 0 THEN null
            ELSE toInteger(round((toFloat(call_recent_total) - call_prior_total) * 100.0 / call_prior_total))
       END AS call_change_pct,
       conversation_daily_counts,
       conversation_recent_total,
       conversation_prior_total,
       CASE WHEN conversation_prior_total = 0 THEN null
            ELSE toInteger(round((toFloat(conversation_recent_total) - conversation_prior_total) * 100.0 / conversation_prior_total))
       END AS conversation_change_pct,
       last_crm_touch_at,
       CASE WHEN last_crm_touch_at IS NULL THEN null
            WHEN last_crm_touch_at >= as_of_at THEN 0
            ELSE toInteger(floor(duration.inSeconds(last_crm_touch_at, as_of_at).seconds / 86400.0))
       END AS days_since_last_crm_touch,
       CASE WHEN last_deal_at IS NULL THEN null
            WHEN last_deal_at >= as_of_at THEN 0
            ELSE toInteger(floor(duration.inSeconds(last_deal_at, as_of_at).seconds / 86400.0))
       END AS days_since_last_deal,
       CASE WHEN last_activity_at IS NULL THEN null
            WHEN last_activity_at >= as_of_at THEN 0
            ELSE toInteger(floor(duration.inSeconds(last_activity_at, as_of_at).seconds / 86400.0))
       END AS days_since_last_activity
"""

