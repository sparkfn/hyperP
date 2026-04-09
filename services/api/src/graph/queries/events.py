"""Cypher constants for the polling events endpoint (downstream consumer feed)."""

from __future__ import annotations

LIST_EVENTS = """
MATCH (me:MergeEvent)
WHERE me.created_at >= datetime($since)
  AND ($event_type IS NULL OR me.event_type = $event_type)
OPTIONAL MATCH (me)-[:ABSORBED]->(absorbed:Person)
OPTIONAL MATCH (me)-[:SURVIVOR]->(survivor:Person)
WITH me, collect(DISTINCT absorbed.person_id) + collect(DISTINCT survivor.person_id) AS pids
WITH me, [x IN pids WHERE x IS NOT NULL] AS affected_person_ids
RETURN me.merge_event_id AS event_id,
       CASE me.event_type
         WHEN 'auto_merge' THEN 'person_merged'
         WHEN 'manual_merge' THEN 'person_merged'
         WHEN 'unmerge' THEN 'person_unmerged'
         WHEN 'person_created' THEN 'person_created'
         WHEN 'survivorship_override' THEN 'golden_profile_updated'
         WHEN 'review_reject' THEN 'review_case_resolved'
         WHEN 'manual_no_match' THEN 'review_case_resolved'
         ELSE me.event_type
       END AS event_type,
       affected_person_ids,
       me.metadata AS metadata,
       toString(me.created_at) AS created_at
ORDER BY me.created_at ASC
SKIP $skip LIMIT $limit
"""
