"""Read-side Cypher constants for derived C3 Profiles."""

from __future__ import annotations

_PROFILE_Q_PREDICATE = """
  AND ($q IS NULL
       OR toLower(profile.profile_id) CONTAINS $q
       OR EXISTS {
         MATCH (member:SourceRecord)-[:IN_PROFILE]->(profile)
         WHERE toLower(member.source_record_pk) CONTAINS $q
            OR toLower(coalesce(member.source_record_id, '')) CONTAINS $q
       }
       OR EXISTS {
         MATCH (fact_member:SourceRecord)-[:IN_PROFILE]->(profile)
         MATCH (fact_member)-[:ASSERTS]->(fact:ProfileFact {attribute_name: 'full_name'})
         WHERE toLower(fact.attribute_value) CONTAINS $q
       }
       OR EXISTS {
         MATCH (profile)-[:HAS_IDENTIFIER]->(identifier:Identifier)
         WHERE identifier.identifier_type <> 'nric'
           AND toLower(identifier.normalized_value) CONTAINS $q
       })
"""

LIST_PROFILES = f"""
MATCH (profile:Profile)
WHERE ($status IS NULL OR profile.status = $status)
{_PROFILE_Q_PREDICATE}
OPTIONAL MATCH (member:SourceRecord)-[:IN_PROFILE]->(profile)
WITH profile, count(DISTINCT member) AS record_count
OPTIONAL MATCH (profile)-[:HAS_IDENTIFIER]->(identified:Identifier)
WITH profile, record_count, count(DISTINCT identified) AS identifier_count
RETURN profile {{
         .profile_id, .status, .evidence_version, .materialized_at, .promoted_person_id
       }} AS profile,
       record_count,
       identifier_count
ORDER BY profile.materialized_at DESC, profile.profile_id
SKIP $skip LIMIT $limit
"""

COUNT_PROFILES = f"""
MATCH (profile:Profile)
WHERE ($status IS NULL OR profile.status = $status)
{_PROFILE_Q_PREDICATE}
RETURN count(profile) AS total
"""

LIST_UNLINKED_PROFILES = """
MATCH (profile:Profile {status: 'open'})
OPTIONAL MATCH (member:SourceRecord)-[:IN_PROFILE]->(profile)
WITH profile,
     count(DISTINCT member) AS record_count,
     count(DISTINCT CASE WHEN member.record_type = 'crm_deal' THEN member END) AS value_score
RETURN profile {
         .profile_id, .status, .evidence_version, .materialized_at, .promoted_person_id
       } AS profile,
       record_count,
       value_score
ORDER BY value_score DESC, record_count DESC, profile.profile_id
SKIP $skip LIMIT $limit
"""

COUNT_UNLINKED_PROFILES = """
MATCH (profile:Profile {status: 'open'})
RETURN count(profile) AS total
"""

GET_PROFILE = """
MATCH (profile:Profile {profile_id: $profile_id})
OPTIONAL MATCH (member:SourceRecord)-[:IN_PROFILE]->(profile)
OPTIONAL MATCH (member)-[:FROM_SOURCE]->(source:SourceSystem)
OPTIONAL MATCH (member)-[:OWNED_BY]->(record_entity:Entity)
OPTIONAL MATCH (source)-[:OPERATED_BY]->(source_entity:Entity)
WITH profile,
     [record IN collect(DISTINCT CASE WHEN member IS NULL THEN null ELSE {
        source_record: member {
          .source_record_pk, .source_record_id, .source_record_version,
          .record_type, .extraction_confidence, .extraction_method,
          .link_status, .lifecycle_status, .observed_at, .ingested_at,
          .parent_source_system, .parent_source_record_id, .parent_record_type,
          .conversation_ref, .raw_payload, .normalized_payload
        },
        source_system: source.source_key,
        linked_person_id: profile.promoted_person_id,
        entity_key: coalesce(record_entity, source_entity).entity_key,
        entity_display_name: coalesce(record_entity, source_entity).display_name
     } END) WHERE record IS NOT NULL] AS source_records
OPTIONAL MATCH (profile)-[link:HAS_IDENTIFIER {status: 'active'}]->(identifier:Identifier)
WITH profile, source_records,
     [item IN collect(DISTINCT CASE WHEN identifier IS NULL THEN null ELSE {
        identifier_type: identifier.identifier_type,
        normalized_value: identifier.normalized_value,
        nric_masked: identifier.nric_masked,
        is_active: true,
        is_verified: coalesce(link.verified_count, 0) > 0,
        last_confirmed_at: link.last_confirmed_at,
        source_system_key: null,
        source_record_pks: [],
        source_record_ids: [],
        entities: [],
        source_records: [],
        score: link.score,
        golden_rank: link.golden_rank
     } END) WHERE item IS NOT NULL] AS identifiers
RETURN profile {
         .profile_id, .status, .evidence_version, .materialized_at, .promoted_person_id
       } AS profile,
       size(source_records) AS record_count,
       size(identifiers) AS identifier_count,
       source_records,
       identifiers
"""
