"""Cypher for the derived C3 identity-link projection and Profile lifecycle.

Every mutation here is derived from immutable ``ASSERTS`` evidence and keeps the
zero-diff invariant: a recompute over unchanged evidence performs no property or
relationship write, and ``first_seen_at``/``materialized_at`` are never rewritten
by a later recompute.
"""

from __future__ import annotations

LOCK_IDENTIFIERS = """
UNWIND $identifier_keys AS key
MATCH (identifier:Identifier {
    identifier_type: key.identifier_type,
    identifier_scope: key.identifier_scope,
    normalized_value: key.normalized_value
})
SET identifier.identifier_id = identifier.identifier_id
"""

FETCH_RECORD_IDENTIFIER_KEYS = """
MATCH (:SourceRecord {source_record_pk: $source_record_pk})
      -[:ASSERTS]->(identifier:Identifier)
RETURN DISTINCT identifier.identifier_type AS identifier_type,
       identifier.identifier_scope AS identifier_scope,
       identifier.normalized_value AS normalized_value
ORDER BY identifier_type, identifier_scope, normalized_value
"""

FETCH_OPEN_PROFILES_FOR_IDENTIFIERS = """
UNWIND $identifier_keys AS key
MATCH (identifier:Identifier {
    identifier_type: key.identifier_type,
    identifier_scope: key.identifier_scope,
    normalized_value: key.normalized_value
})
MATCH (profile:Profile)-[:HAS_IDENTIFIER]->(identifier)
WHERE profile.status = 'open'
WITH DISTINCT profile
RETURN profile.profile_id AS profile_id,
       profile.evidence_version AS evidence_version
ORDER BY profile_id
"""

FETCH_CLUSTER_EVIDENCE = """
UNWIND $identifier_keys AS key
MATCH (identifier:Identifier {
    identifier_type: key.identifier_type,
    identifier_scope: key.identifier_scope,
    normalized_value: key.normalized_value
})
OPTIONAL MATCH (parked:SourceRecord)-[:ASSERTS]->(identifier)
WHERE parked.lifecycle_status = 'parked'
  AND parked.link_status = 'unlinked'
  AND parked.attribution_status = 'parked'
  AND parked.reeval_pending = true
OPTIONAL MATCH (profile:Profile)-[:HAS_IDENTIFIER]->(identifier)
WHERE profile.status = 'open'
OPTIONAL MATCH (member:SourceRecord)-[:IN_PROFILE]->(profile)
OPTIONAL MATCH (incoming:SourceRecord {source_record_pk: $source_record_pk})
WITH collect(DISTINCT parked) + collect(DISTINCT member)
   + collect(DISTINCT incoming) AS records
UNWIND records AS record
WITH DISTINCT record
WHERE record IS NOT NULL
  AND NOT EXISTS {
      MATCH (record)-[linked:LINKED_TO]->(:Person)
      WHERE coalesce(linked.is_active, true) = true
  }
MATCH (record)-[assertion:ASSERTS]->(asserted:Identifier)
OPTIONAL MATCH (record)-[:FROM_SOURCE]->(source:SourceSystem)
RETURN DISTINCT record.source_record_pk AS source_record_pk,
       asserted.identifier_id AS identifier_id,
       asserted.identifier_type AS identifier_type,
       asserted.identifier_scope AS identifier_scope,
       asserted.normalized_value AS normalized_value,
       assertion.quality_flag AS quality_flag,
       assertion.is_verified AS is_verified,
       source.field_trust AS field_trust,
       toString(record.observed_at) AS observed_at
ORDER BY source_record_pk, identifier_type, identifier_scope, normalized_value
"""

FETCH_PARTICIPANT_EVIDENCE = """
CALL {
    WITH $participant_kind AS kind, $participant_id AS participant_id
    WITH kind, participant_id
    WHERE kind = 'person'
    MATCH (record:SourceRecord)-[linked:LINKED_TO]->(:Person {person_id: participant_id})
    WHERE coalesce(linked.is_active, true) = true
      AND coalesce(record.link_status, 'linked') <> 'pending_review'
    RETURN record
    UNION
    WITH $participant_kind AS kind, $participant_id AS participant_id
    WITH kind, participant_id
    WHERE kind = 'profile'
    MATCH (record:SourceRecord)-[:IN_PROFILE]->(:Profile {profile_id: participant_id})
    RETURN record
}
MATCH (record)-[assertion:ASSERTS]->(asserted:Identifier)
OPTIONAL MATCH (record)-[:FROM_SOURCE]->(source:SourceSystem)
RETURN DISTINCT record.source_record_pk AS source_record_pk,
       asserted.identifier_id AS identifier_id,
       asserted.identifier_type AS identifier_type,
       asserted.identifier_scope AS identifier_scope,
       asserted.normalized_value AS normalized_value,
       assertion.quality_flag AS quality_flag,
       assertion.is_verified AS is_verified,
       source.field_trust AS field_trust,
       toString(record.observed_at) AS observed_at
ORDER BY source_record_pk, identifier_type, identifier_scope, normalized_value
"""

MERGE_PARTICIPANT_HAS_IDENTIFIER_LINKS = """
MATCH (participant:Person|Profile)
WHERE ($participant_kind = 'person' AND participant.person_id = $participant_id)
   OR ($participant_kind = 'profile' AND participant.profile_id = $participant_id)
UNWIND $links AS row
MATCH (identifier:Identifier {identifier_id: row.identifier_id})
MERGE (participant)-[link:HAS_IDENTIFIER {link_id: row.link_id}]->(identifier)
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

DELETE_STALE_PARTICIPANT_HAS_IDENTIFIER_LINKS = """
MATCH (participant:Person|Profile)
WHERE ($participant_kind = 'person' AND participant.person_id = $participant_id)
   OR ($participant_kind = 'profile' AND participant.profile_id = $participant_id)
MATCH (participant)-[link:HAS_IDENTIFIER]->(identifier:Identifier)
WHERE NOT link.link_id IN $link_ids
WITH collect(DISTINCT link) AS stale_links, collect(DISTINCT identifier) AS stale_identifiers
FOREACH (stale_link IN stale_links | DELETE stale_link)
RETURN [identifier IN stale_identifiers | {
    identifier_type: identifier.identifier_type,
    identifier_scope: identifier.identifier_scope,
    normalized_value: identifier.normalized_value
}] AS stale_identifier_keys
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

SET_SOURCE_RECORD_PARKED = """
MATCH (record:SourceRecord {source_record_pk: $source_record_pk})
WITH record,
     (record.link_status = 'unlinked'
      AND record.attribution_status = 'parked'
      AND coalesce(record.reeval_pending, false) = true) AS unchanged
WHERE NOT unchanged
SET record.link_status = 'unlinked',
    record.attribution_status = 'parked',
    record.reeval_pending = true,
    record.attribution_eval_at = datetime()
RETURN record.source_record_pk AS source_record_pk
"""

CREATE_PROFILE = """
CREATE (profile:Profile {
    profile_id: $profile_id,
    status: 'open',
    evidence_version: 0,
    materialized_at: datetime()
})
RETURN profile.profile_id AS profile_id
"""

FETCH_PROFILE_STATE = """
MATCH (profile:Profile {profile_id: $profile_id})
RETURN profile.profile_id AS profile_id,
       profile.status AS status,
       profile.evidence_version AS evidence_version,
       profile.promoted_person_id AS promoted_person_id
"""

ATTACH_PROFILE_MEMBERS = """
MATCH (profile:Profile {profile_id: $profile_id})
WHERE profile.status = 'open' AND profile.evidence_version = $expected_version
UNWIND $source_record_pks AS source_record_pk
MATCH (record:SourceRecord {source_record_pk: source_record_pk})
OPTIONAL MATCH (record)-[existing:IN_PROFILE]->(profile)
WITH profile, record,
     (existing IS NOT NULL
      AND record.link_status = 'profile'
      AND record.attribution_status = 'profile'
      AND coalesce(record.reeval_pending, false) = false) AS unchanged
MERGE (record)-[:IN_PROFILE]->(profile)
FOREACH (_ IN CASE WHEN unchanged THEN [] ELSE [1] END |
    SET record.link_status = 'profile',
        record.attribution_status = 'profile',
        record.reeval_pending = false
)
WITH profile, collect({
    source_record_pk: record.source_record_pk,
    changed: NOT unchanged
}) AS members
WITH profile, members, size([member IN members WHERE member.changed]) AS changed_count
FOREACH (_ IN CASE WHEN changed_count > 0 THEN [1] ELSE [] END |
    SET profile.evidence_version = profile.evidence_version + 1,
        profile.updated_at = datetime()
)
RETURN changed_count AS changed_count,
       [member IN members | member.source_record_pk] AS member_source_record_pks
"""

RETIRE_PROFILE = """
MATCH (profile:Profile {profile_id: $profile_id})
WHERE profile.status = 'open' AND profile.evidence_version = $expected_version
SET profile.status = 'retired',
    profile.updated_at = datetime()
RETURN profile.profile_id AS profile_id
"""

MOVE_PROFILE_MEMBERS = """
MATCH (keeper:Profile {profile_id: $keeper_profile_id})
WHERE keeper.status = 'open' AND keeper.evidence_version = $keeper_expected_version
OPTIONAL MATCH (loser:Profile {profile_id: $loser_profile_id})
      <-[existing:IN_PROFILE]-(member:SourceRecord)
WITH keeper, collect(member) AS members, collect(existing) AS moved_edges
FOREACH (member IN members | MERGE (member)-[:IN_PROFILE]->(keeper))
FOREACH (edge IN moved_edges | DELETE edge)
WITH keeper, size(members) AS moved_count
FOREACH (_ IN CASE WHEN moved_count > 0 THEN [1] ELSE [] END |
    SET keeper.evidence_version = keeper.evidence_version + 1,
        keeper.updated_at = datetime()
)
RETURN moved_count AS moved_count
"""

MERGE_PERSON_BY_NRIC = """
OPTIONAL MATCH (existing:Person {nric_key: $nric_key})
WITH existing IS NOT NULL AS existed
MERGE (person:Person {nric_key: $nric_key})
ON CREATE SET person.person_id = randomUUID(),
              person.status = 'active',
              person.created_at = datetime()
WITH person, existed
MERGE (identifier:Identifier {
    identifier_type: 'nric',
    identifier_scope: $identifier_scope,
    normalized_value: $nric_key
})
ON CREATE SET identifier.identifier_id = randomUUID(),
              identifier.created_at = datetime()
MERGE (person)-[:ANCHORED_BY]->(identifier)
RETURN person.person_id AS person_id,
       identifier.identifier_id AS identifier_id,
       (NOT existed) AS was_created
"""

FETCH_PERSON_BY_NRIC = """
MATCH (person:Person {nric_key: $nric_key})
RETURN person.person_id AS person_id
ORDER BY person.person_id
LIMIT 1
"""

PROMOTE_PROFILE = """
MATCH (profile:Profile {profile_id: $profile_id})
WHERE profile.status = 'open' AND profile.evidence_version = $expected_version
MATCH (person:Person {person_id: $person_id})
SET profile.status = 'promoted',
    profile.promoted_person_id = $person_id,
    profile.updated_at = datetime()
MERGE (profile)-[:PROMOTED_TO]->(person)
RETURN profile.profile_id AS profile_id
"""

REATTRIBUTE_PROFILE_MEMBERS_TO_PERSON = """
MATCH (profile:Profile {profile_id: $profile_id})
MATCH (record:SourceRecord)-[:IN_PROFILE]->(profile)
MATCH (person:Person {person_id: $person_id})
OPTIONAL MATCH (record)-[existing:LINKED_TO]->(person)
WHERE coalesce(existing.is_active, true) = true
WITH record,
     (existing IS NOT NULL
      AND record.link_status = 'linked'
      AND record.attribution_status = 'person'
      AND coalesce(record.reeval_pending, false) = false) AS unchanged
MERGE (record)-[active_link:LINKED_TO]->(person)
FOREACH (_ IN CASE WHEN unchanged THEN [] ELSE [1] END |
    SET record.link_status = 'linked',
        record.attribution_status = 'person',
        record.reeval_pending = false
)
FOREACH (_ IN CASE WHEN coalesce(active_link.is_active, true) = true THEN [] ELSE [1] END |
    SET active_link.is_active = true
)
RETURN count(record) AS member_count
"""

SOURCE_RECORD_LINKED_TO_PERSON = """
MATCH (record:SourceRecord {source_record_pk: $source_record_pk})
      -[linked:LINKED_TO]->(:Person)
WHERE coalesce(linked.is_active, true) = true
RETURN record.source_record_pk AS source_record_pk
LIMIT 1
"""

ANCHOR_EXISTING_PERSON_BY_NRIC = """
MATCH (person:Person {person_id: $person_id})
WHERE person.nric_key IS NULL OR person.nric_key = $nric_key
SET person.nric_key = $nric_key
WITH person
MERGE (identifier:Identifier {
    identifier_type: 'nric',
    identifier_scope: $identifier_scope,
    normalized_value: $nric_key
})
ON CREATE SET identifier.identifier_id = randomUUID(),
              identifier.created_at = datetime()
MERGE (person)-[:ANCHORED_BY]->(identifier)
RETURN person.person_id AS person_id
"""
