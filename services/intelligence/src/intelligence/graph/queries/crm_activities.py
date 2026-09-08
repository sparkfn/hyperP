"""Closed, parameterized, read-only activity archive query contract."""

from __future__ import annotations

# Do not replace this explicit compatibility list with ``<> 'stage'``.  The
# legacy aliases are the v1/v2 forms proved by crm_history_projection_migration.
ACTIVITY_COMPATIBILITY = """
(
  record.history_family = 'activity'
  OR record.history_family IS NULL
  OR (record.history_family = 'crm_activity'
      AND record.history_source = 'bitrix_crm_activity'
      AND record.projection_source = 'bitrix_crm_activity_v1'
      AND toString(record.projection_version) IN ['1', '2'])
)
"""

_ACTIVITY_COMPATIBILITY = ACTIVITY_COMPATIBILITY.replace("record.", "activity.")

_ADMITTED_ACTIVITY_OR_CALL = f"""
(
  (record.record_type = 'crm_history' AND {ACTIVITY_COMPATIBILITY})
  OR (record.record_type = 'call' AND (EXISTS {{
    MATCH (record)-[:CHILD_OF]->(activity:SourceRecord {{source_instance_id: $source_instance_id, record_type: 'crm_history'}})
          -[:FROM_SOURCE]->(:SourceSystem {{source_key: $source_key}})
    WHERE {_ACTIVITY_COMPATIBILITY}
  }} OR EXISTS {{
    MATCH (record)-[:DETAILS_HISTORY_ITEM]->(activity:SourceRecord {{source_instance_id: $source_instance_id, record_type: 'crm_history'}})
          -[:FROM_SOURCE]->(:SourceSystem {{source_key: $source_key}})
    WHERE {_ACTIVITY_COMPATIBILITY}
  }}))
)
"""

_SAFE_PROJECTION = """
CALL (record) {
  OPTIONAL MATCH (record)-[:CHILD_OF]->(child_parent:SourceRecord)
  OPTIONAL MATCH (child_parent)-[:FROM_SOURCE]->(child_source:SourceSystem)
  RETURN collect(DISTINCT CASE WHEN child_parent IS NULL THEN null ELSE {
    source_record_pk: child_parent.source_record_pk,
    source_instance_id: child_parent.source_instance_id,
    source_record_id: child_parent.source_record_id,
    record_type: child_parent.record_type, source_system: child_source.source_key
  } END) AS child_parents
}
CALL (record) {
  OPTIONAL MATCH (record)-[:DETAILS_HISTORY_ITEM]->(details_parent:SourceRecord)
  OPTIONAL MATCH (details_parent)-[:FROM_SOURCE]->(details_source:SourceSystem)
  RETURN collect(DISTINCT CASE WHEN details_parent IS NULL THEN null ELSE {
    source_record_pk: details_parent.source_record_pk,
    source_instance_id: details_parent.source_instance_id,
    source_record_id: details_parent.source_record_id,
    record_type: details_parent.record_type, source_system: details_source.source_key
  } END) AS details_parents
}
CALL (record) {
  OPTIONAL MATCH (record)-[link:LINKED_TO]->(person:Person)
  WHERE coalesce(link.is_active, true) = true
  RETURN collect(DISTINCT CASE WHEN person IS NULL THEN null ELSE {
    person_id: person.person_id, status: person.status,
    revision: toString(person.revision), source_record_pk: link.source_record_pk
  } END) AS people
}
RETURN record.source_record_pk AS source_record_pk,
       record.source_record_id AS source_record_id,
       toString(record.source_record_version) AS source_record_version,
       record.source_version_key AS source_version_key,
       record.record_hash AS record_hash,
       record.source_instance_id AS source_instance_id,
       $source_key AS source_key, record.record_type AS record_type,
       record.lifecycle_status AS lifecycle_status,
       record.history_family AS history_family, record.history_kind AS history_kind,
       record.history_source AS history_source,
       toString(record.projection_version) AS projection_version,
       record.projection_source AS projection_source, toString(record.event_at) AS event_at,
       record.link_status AS link_status,
       toString(record.observed_at) AS observed_at,
       toString(record.ingested_at) AS ingested_at,
       toString(coalesce(record.standalone_crm_available_at, record.ingested_at)) AS available_at,
       {source_record_pk: null, source_instance_id: record.parent_source_instance_id,
        source_record_id: record.parent_source_record_id, record_type: record.parent_record_type,
        source_system: record.parent_source_system} AS stored_parent,
       [item IN child_parents WHERE item IS NOT NULL] AS child_parents,
       [item IN details_parents WHERE item IS NOT NULL] AS details_parents,
       [item IN people WHERE item IS NOT NULL] AS people,
       [] AS user_capabilities
ORDER BY source_record_pk
"""

READ_SELECTED_PAGE = f"""
MATCH (record:SourceRecord {{source_instance_id: $source_instance_id}})
      -[:FROM_SOURCE]->(:SourceSystem {{source_key: $source_key}})
WHERE record.source_record_pk > $after_source_record_pk
  AND {_ADMITTED_ACTIVITY_OR_CALL}
WITH record ORDER BY record.source_record_pk LIMIT $limit
{_SAFE_PROJECTION}
"""

READ_BY_IDENTITIES = f"""
MATCH (record:SourceRecord {{source_instance_id: $source_instance_id}})
      -[:FROM_SOURCE]->(:SourceSystem {{source_key: $source_key}})
WHERE record.source_record_pk IN $source_record_pks
  AND {_ADMITTED_ACTIVITY_OR_CALL}
WITH record ORDER BY record.source_record_pk
{_SAFE_PROJECTION}
"""

PREFLIGHT_STRUCTURAL_INVALID = f"""
MATCH (record:SourceRecord {{source_instance_id: $source_instance_id}})
      -[:FROM_SOURCE]->(:SourceSystem {{source_key: $source_key}})
WHERE (record.record_type = 'crm_history' AND {ACTIVITY_COMPATIBILITY})
   OR (record.record_type = 'call' AND (EXISTS {{
      MATCH (record)-[:CHILD_OF|DETAILS_HISTORY_ITEM]->(activity:SourceRecord {{source_instance_id: $source_instance_id, record_type: 'crm_history'}})
            -[:FROM_SOURCE]->(:SourceSystem {{source_key: $source_key}})
      WHERE {_ACTIVITY_COMPATIBILITY}
   }}))
WITH record
WHERE record.source_record_pk IS NULL OR trim(toString(record.source_record_pk)) = ''
   OR record.record_hash IS NULL OR trim(toString(record.record_hash)) = ''
   OR record.source_record_id IS NULL OR trim(toString(record.source_record_id)) = ''
   OR record.source_record_version IS NULL OR trim(toString(record.source_record_version)) = ''
   OR record.source_version_key IS NULL OR trim(toString(record.source_version_key)) = ''
RETURN count(record) AS invalid_count
LIMIT 1
"""

PREFLIGHT_REFERENCE_FANOUT = f"""
MATCH (record:SourceRecord {{source_instance_id: $source_instance_id}})
      -[:FROM_SOURCE]->(:SourceSystem {{source_key: $source_key}})
WHERE {_ADMITTED_ACTIVITY_OR_CALL}
WITH record,
     COUNT {{ MATCH (record)-[:CHILD_OF]->(:SourceRecord) }} AS child_parent_count,
     COUNT {{ MATCH (record)-[:DETAILS_HISTORY_ITEM]->(:SourceRecord) }} AS details_parent_count,
     COUNT {{
       MATCH (record)-[link:LINKED_TO]->(:Person)
       WHERE coalesce(link.is_active, true) = true
     }} AS active_person_count
WHERE child_parent_count > $max_references_per_record
   OR details_parent_count > $max_references_per_record
   OR active_person_count > $max_references_per_record
RETURN count(record) AS invalid_count
"""
