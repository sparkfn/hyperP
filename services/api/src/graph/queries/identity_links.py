"""Cypher for API-side maintenance of the derived ``HAS_IDENTIFIER`` projection.

Manual merge/unmerge and review approval move the legacy projections
(``LINKED_TO``/``IDENTIFIED_BY``); these statements keep the C3 projection in
step with that evidence so HAS-first reads cannot go stale until the next
ingest. Semantics mirror the ingestion writer, which remains the authority.
"""

from __future__ import annotations

FETCH_PERSON_ASSERTION_EVIDENCE = """
MATCH (person:Person {person_id: $person_id})
MATCH (record:SourceRecord)-[linked:LINKED_TO]->(person)
WHERE coalesce(linked.is_active, true) = true
  AND coalesce(record.link_status, 'linked') <> 'pending_review'
  AND coalesce(record.lifecycle_status, 'active') <> 'superseded'
MATCH (record)-[assertion:ASSERTS]->(identifier:Identifier)
OPTIONAL MATCH (record)-[:FROM_SOURCE]->(source:SourceSystem)
RETURN DISTINCT record.source_record_pk AS source_record_pk,
       identifier.identifier_id AS identifier_id,
       identifier.identifier_type AS identifier_type,
       identifier.identifier_scope AS identifier_scope,
       identifier.normalized_value AS normalized_value,
       assertion.quality_flag AS quality_flag,
       assertion.is_verified AS is_verified,
       source.field_trust AS field_trust,
       toString(record.observed_at) AS observed_at
ORDER BY source_record_pk, identifier_type, identifier_scope, normalized_value
"""

MERGE_PERSON_HAS_IDENTIFIER_LINKS = """
MATCH (person:Person {person_id: $person_id})
UNWIND $links AS row
MATCH (identifier:Identifier {identifier_id: row.identifier_id})
MERGE (person)-[link:HAS_IDENTIFIER {link_id: row.link_id}]->(identifier)
WITH link, row,
     (link.computed_at IS NULL) AS created,
     (link.assertion_count = row.assertion_count
      AND link.source_count = row.source_count
      AND link.verified_count = row.verified_count
      AND link.max_tier = row.max_tier
      AND link.last_seen_at = row.last_seen_at
      AND link.last_confirmed_at = row.last_confirmed_at
      AND link.score = row.score
      AND link.exclusivity = row.exclusivity
      AND link.rank_score = row.rank_score
      AND link.golden_rank IS NULL
      AND link.basis = row.basis
      AND link.status = row.status
      AND link.policy_version = row.policy_version
      AND (CASE
             WHEN link.first_seen_at IS NULL THEN row.first_seen_at IS NULL
             WHEN row.first_seen_at IS NULL THEN true
             ELSE row.first_seen_at >= link.first_seen_at
           END)) AS unchanged
FOREACH (_ IN CASE WHEN created THEN [1] ELSE [] END |
    SET link.assertion_count = row.assertion_count,
        link.source_count = row.source_count,
        link.verified_count = row.verified_count,
        link.max_tier = row.max_tier,
        link.first_seen_at = row.first_seen_at,
        link.last_seen_at = row.last_seen_at,
        link.last_confirmed_at = row.last_confirmed_at,
        link.score = row.score,
        link.exclusivity = row.exclusivity,
        link.rank_score = row.rank_score,
        link.golden_rank = null,
        link.basis = row.basis,
        link.status = row.status,
        link.policy_version = row.policy_version,
        link.computed_at = datetime()
)
FOREACH (_ IN CASE WHEN NOT created AND NOT unchanged THEN [1] ELSE [] END |
    SET link.assertion_count = row.assertion_count,
        link.source_count = row.source_count,
        link.verified_count = row.verified_count,
        link.max_tier = row.max_tier,
        link.first_seen_at = CASE
            WHEN link.first_seen_at IS NULL THEN row.first_seen_at
            WHEN row.first_seen_at IS NULL THEN link.first_seen_at
            WHEN row.first_seen_at < link.first_seen_at THEN row.first_seen_at
            ELSE link.first_seen_at END,
        link.last_seen_at = row.last_seen_at,
        link.last_confirmed_at = row.last_confirmed_at,
        link.score = row.score,
        link.exclusivity = row.exclusivity,
        link.rank_score = row.rank_score,
        link.golden_rank = null,
        link.basis = row.basis,
        link.status = row.status,
        link.policy_version = row.policy_version,
        link.computed_at = datetime()
)
RETURN count(link) AS link_count
"""

DELETE_STALE_PERSON_HAS_IDENTIFIER_LINKS = """
MATCH (person:Person {person_id: $person_id})
MATCH (person)-[link:HAS_IDENTIFIER]->(identifier:Identifier)
WHERE NOT link.link_id IN $link_ids
WITH collect(DISTINCT link) AS stale_links, collect(DISTINCT identifier) AS stale_identifiers
FOREACH (stale_link IN stale_links | DELETE stale_link)
UNWIND stale_identifiers AS identifier
RETURN identifier.identifier_type AS identifier_type,
       identifier.identifier_scope AS identifier_scope,
       identifier.normalized_value AS normalized_value
"""

DELETE_PERSON_HAS_IDENTIFIER_LINKS = """
MATCH (person:Person {person_id: $person_id})
MATCH (person)-[link:HAS_IDENTIFIER]->(identifier:Identifier)
WITH collect(DISTINCT link) AS links, collect(DISTINCT identifier) AS identifiers
FOREACH (link IN links | DELETE link)
UNWIND identifiers AS identifier
RETURN identifier.identifier_type AS identifier_type,
       identifier.identifier_scope AS identifier_scope,
       identifier.normalized_value AS normalized_value
"""

RECOMPUTE_IDENTIFIER_PARTICIPANT_COUNTS = """
UNWIND $identifier_keys AS key
MATCH (identifier:Identifier {
    identifier_type: key.identifier_type,
    identifier_scope: key.identifier_scope,
    normalized_value: key.normalized_value
})
CALL {
    WITH identifier
    OPTIONAL MATCH (person:Person)-[:HAS_IDENTIFIER]->(identifier)
    WHERE person.status = 'active'
    RETURN count(DISTINCT person) AS person_count
}
CALL {
    WITH identifier
    OPTIONAL MATCH (profile:Profile)-[:HAS_IDENTIFIER]->(identifier)
    WHERE profile.status = 'open'
    RETURN count(DISTINCT profile) AS profile_count
}
CALL {
    WITH identifier
    OPTIONAL MATCH (record:SourceRecord)-[:ASSERTS]->(identifier)
    WHERE record.lifecycle_status = 'parked'
      AND record.link_status = 'unlinked'
      AND record.attribution_status = 'parked'
      AND record.reeval_pending = true
    RETURN count(DISTINCT record) AS record_count
}
WITH identifier, person_count + profile_count + record_count AS computed_count
WHERE identifier.participant_count IS NULL
   OR identifier.participant_count <> computed_count
SET identifier.participant_count = computed_count
RETURN count(identifier) AS updated_count
"""
