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
// An over-cap hub key never drives expansion on its own; a Profile reachable
// through any under-cap key is still included.
WHERE key.fanout_cap IS NULL
   OR coalesce(identifier.participant_count, 0) <= key.fanout_cap
MATCH (profile:Profile)-[:HAS_IDENTIFIER]->(identifier)
WHERE profile.status = 'open'
WITH DISTINCT profile
RETURN profile.profile_id AS profile_id,
       profile.evidence_version AS evidence_version
ORDER BY profile_id
"""

#: Cap-aware cluster expansion. ``$identifier_keys`` rows carry ``fanout_cap``;
#: an over-cap key contributes no expansion by itself, while a record or Profile
#: also reached through an under-cap key stays in the cluster (and keeps every
#: Profile member). ``FETCH_CLUSTER_KEY_CLOSURE`` and ``FETCH_CLUSTER_EVIDENCE``
#: must keep identical expansion so the locked closure contains every key the
#: evidence read can return.
FETCH_CLUSTER_KEY_CLOSURE = """
UNWIND $identifier_keys AS key
MATCH (identifier:Identifier {
    identifier_type: key.identifier_type,
    identifier_scope: key.identifier_scope,
    normalized_value: key.normalized_value
})
WITH key, identifier,
     (key.fanout_cap IS NOT NULL
      AND coalesce(identifier.participant_count, 0) > key.fanout_cap) AS over_cap
OPTIONAL MATCH (parked:SourceRecord)-[:ASSERTS]->(identifier)
WHERE NOT over_cap
  AND parked.lifecycle_status = 'parked'
  AND parked.link_status = 'unlinked'
  AND parked.attribution_status = 'parked'
  AND parked.reeval_pending = true
OPTIONAL MATCH (profile:Profile)-[:HAS_IDENTIFIER]->(identifier)
WHERE NOT over_cap
  AND profile.status = 'open'
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
MATCH (record)-[:ASSERTS]->(asserted:Identifier)
RETURN DISTINCT asserted.identifier_type AS identifier_type,
       asserted.identifier_scope AS identifier_scope,
       asserted.normalized_value AS normalized_value
ORDER BY identifier_type, identifier_scope, normalized_value
"""

FETCH_CLUSTER_EVIDENCE = """
UNWIND $identifier_keys AS key
MATCH (identifier:Identifier {
    identifier_type: key.identifier_type,
    identifier_scope: key.identifier_scope,
    normalized_value: key.normalized_value
})
WITH key, identifier,
     (key.fanout_cap IS NOT NULL
      AND coalesce(identifier.participant_count, 0) > key.fanout_cap) AS over_cap
OPTIONAL MATCH (parked:SourceRecord)-[:ASSERTS]->(identifier)
WHERE NOT over_cap
  AND parked.lifecycle_status = 'parked'
  AND parked.link_status = 'unlinked'
  AND parked.attribution_status = 'parked'
  AND parked.reeval_pending = true
OPTIONAL MATCH (profile:Profile)-[:HAS_IDENTIFIER]->(identifier)
WHERE NOT over_cap
  AND profile.status = 'open'
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
      // A replaced version keeps its LINKED_TO edge but no longer evidences the
      // Person; retirement writes lifecycle_status='superseded'. Pre-lifecycle
      // rows (NULL) are treated as active.
      AND coalesce(record.lifecycle_status, 'active') <> 'superseded'
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
    // A growing Profile is counted by its members: counting the Profile as one
    // participant would hide sequential joins from the fanout cap.
    OPTIONAL MATCH (record:SourceRecord)-[:ASSERTS]->(identifier)
    WHERE EXISTS {
        MATCH (record)-[:IN_PROFILE]->(:Profile {status: 'open'})
    }
    RETURN count(DISTINCT record) AS profile_member_count
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
WITH identifier, person_count + profile_member_count + record_count AS computed_count
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
// MERGE on the unique nric_key is the only Person constructor: concurrent
// first anchors converge on one node instead of racing a CREATE against the
// unique constraint. The holder may then have been merged away, so resolve
// through MERGED_INTO lineage (path-compressed to one hop, chained
// defensively) to its active survivor before anchoring; a merged node never
// gains a second anchor.
OPTIONAL MATCH (existing:Person {nric_key: $nric_key})
WITH existing IS NOT NULL AS existed
MERGE (holder:Person {nric_key: $nric_key})
ON CREATE SET holder.person_id = randomUUID(),
              holder.status = 'active',
              holder.created_at = datetime()
WITH holder, existed
OPTIONAL MATCH (holder)-[:MERGED_INTO*1..]->(survivor:Person)
WHERE survivor.status = 'active'
WITH holder, existed, survivor
ORDER BY coalesce(survivor.person_id, '') LIMIT 1
WITH coalesce(survivor, holder) AS person, existed
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
MATCH (holder:Person {nric_key: $nric_key})
OPTIONAL MATCH (holder)-[:MERGED_INTO*1..]->(survivor:Person)
WHERE survivor.status = 'active'
WITH coalesce(survivor, holder) AS person
RETURN person.person_id AS person_id
ORDER BY person_id
LIMIT 1
"""

FETCH_CANONICAL_PERSON_ID = """
MATCH (person:Person {person_id: $person_id})
OPTIONAL MATCH (person)-[:MERGED_INTO*1..]->(survivor:Person)
WHERE survivor.status = 'active'
WITH coalesce(survivor, person) AS canonical
RETURN canonical.person_id AS person_id
ORDER BY person_id
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
WHERE record.source_record_pk IN $source_record_pks
MATCH (record)-[:FROM_SOURCE]->(source:SourceSystem)
MATCH (person:Person {person_id: $person_id})
// The disposition read already took every eligible member's identity lock.
// Exactly one open version of a source identity may be activated: another
// open version, or a higher version of the same identity, keeps this member
// parked (IN_PROFILE stays as lineage and nothing else is written).
WHERE NOT EXISTS {
    MATCH (other:SourceRecord)-[:FROM_SOURCE]->(source)
    WHERE other <> record
      AND coalesce(other.source_instance_id, 'legacy-default')
          = coalesce(record.source_instance_id, 'legacy-default')
      AND other.source_record_id = record.source_record_id
      AND (
        other.lifecycle_status IN ['active', 'pending_review']
        OR (other.lifecycle_status IS NULL AND other.is_latest = true)
        OR toInteger(other.source_record_version)
           > toInteger(record.source_record_version)
      )
}
OPTIONAL MATCH (record)-[existing:LINKED_TO]->(person)
WHERE coalesce(existing.is_active, true) = true
WITH record, source, person,
     (existing IS NOT NULL
      AND record.link_status = 'linked'
      AND record.attribution_status = 'person'
      AND coalesce(record.reeval_pending, false) = false
      AND record.lifecycle_status = 'active') AS unchanged
MERGE (record)-[active_link:LINKED_TO]->(person)
// Promotion is a delayed link: mirror the lifecycle a legacy-linked record is
// activated to, without rewriting version markers or timestamps on re-runs.
FOREACH (_ IN CASE WHEN unchanged THEN [] ELSE [1] END |
    SET record.link_status = 'linked',
        record.attribution_status = 'person',
        record.reeval_pending = false,
        record.lifecycle_status = 'active',
        record.is_latest = true,
        record.activated_at = coalesce(record.activated_at, datetime()),
        record.updated_at = datetime()
)
FOREACH (_ IN CASE WHEN coalesce(active_link.is_active, true) = true THEN [] ELSE [1] END |
    SET active_link.is_active = true
)
RETURN [member IN collect(CASE WHEN unchanged THEN null ELSE {
    source_record_pk: record.source_record_pk,
    normalized_payload: record.normalized_payload,
    observed_at: toString(record.observed_at),
    source_key: source.source_key
} END) WHERE member IS NOT NULL] AS reattributed_members
"""

FETCH_PROFILE_MEMBER_EVIDENCE = """
MATCH (profile:Profile {profile_id: $profile_id})
MATCH (member:SourceRecord)-[:IN_PROFILE]->(profile)
OPTIONAL MATCH (member)-[:ASSERTS]->(nric:Identifier {identifier_type: 'nric'})
WITH member, collect(DISTINCT CASE WHEN nric IS NULL THEN null ELSE {
    value: nric.normalized_value, checksum_valid: nric.checksum_valid
} END) AS nric_records
OPTIONAL MATCH (member)-[:ASSERTS]->(fact:ProfileFact)
WHERE fact.attribute_name IN ['full_name', 'dob']
WITH member, nric_records, collect(DISTINCT fact) AS facts
RETURN member.source_record_pk AS source_record_pk,
       nric_records,
       [item IN facts WHERE item.attribute_name = 'full_name' | item.attribute_value] AS full_names,
       [item IN facts WHERE item.attribute_name = 'dob' | item.attribute_value] AS dobs
ORDER BY source_record_pk
"""

FETCH_PROFILE_MEMBER_VERSION_DISPOSITION = """
MATCH (profile:Profile {profile_id: $profile_id})
UNWIND $source_record_pks AS source_record_pk
MATCH (member:SourceRecord {source_record_pk: source_record_pk})-[:IN_PROFILE]->(profile)
MATCH (member)-[:FROM_SOURCE]->(source:SourceSystem)
// Same lock shape as LOCK_AND_GET_SOURCE_STATE, ordered so competing promotions
// take the per-identity locks in one canonical order.
WITH member, source
ORDER BY source.source_key, coalesce(member.source_instance_id, 'legacy-default'),
         member.source_record_id
MERGE (lock:SourceRecordIdentityLock {
    source_system: source.source_key,
    source_instance_id: coalesce(member.source_instance_id, 'legacy-default'),
    source_record_id: member.source_record_id
})
SET lock.locked_at = datetime()
WITH member, source
CALL (member, source) {
    WITH member, source
    OPTIONAL MATCH (other:SourceRecord)-[:FROM_SOURCE]->(source)
    WHERE other <> member
      AND coalesce(other.source_instance_id, 'legacy-default')
          = coalesce(member.source_instance_id, 'legacy-default')
      AND other.source_record_id = member.source_record_id
      AND (other.lifecycle_status IN ['active', 'pending_review']
           OR (other.lifecycle_status IS NULL AND other.is_latest = true))
    WITH other
    ORDER BY toInteger(other.source_record_version) DESC
    RETURN head(collect(other)) AS open_version
}
CALL (member, source) {
    WITH member, source
    OPTIONAL MATCH (other:SourceRecord)-[:FROM_SOURCE]->(source)
    WHERE other <> member
      AND coalesce(other.source_instance_id, 'legacy-default')
          = coalesce(member.source_instance_id, 'legacy-default')
      AND other.source_record_id = member.source_record_id
      AND other.lifecycle_status = 'parked'
      AND toInteger(other.source_record_version)
          > toInteger(member.source_record_version)
    WITH other
    ORDER BY toInteger(other.source_record_version) DESC
    RETURN head(collect(other)) AS parked_higher
}
RETURN member.source_record_pk AS source_record_pk,
       CASE
           WHEN open_version IS NOT NULL THEN
               CASE WHEN toInteger(member.source_record_version)
                         < toInteger(open_version.source_record_version)
                    THEN 'supersede' ELSE 'reopen' END
           WHEN parked_higher IS NOT NULL THEN 'supersede'
           ELSE 'activate'
       END AS disposition,
       coalesce(open_version.source_record_pk, parked_higher.source_record_pk) AS winner_pk
ORDER BY source_record_pk
"""

SUPERSEDE_PROFILE_MEMBERS = """
UNWIND $rows AS row
MATCH (member:SourceRecord {source_record_pk: row.source_record_pk})
MATCH (winner:SourceRecord {source_record_pk: row.winner_pk})
SET member.lifecycle_status = 'superseded',
    member.is_latest = false,
    member.reeval_pending = false,
    member.superseded_at = datetime(),
    member.updated_at = datetime()
MERGE (member)-[:PREVIOUS_VERSION_OF]->(winner)
"""

REOPEN_PROFILE_MEMBERS = """
MATCH (profile:Profile {profile_id: $profile_id})
UNWIND $source_record_pks AS source_record_pk
MATCH (record:SourceRecord {source_record_pk: source_record_pk})
      -[member:IN_PROFILE]->(profile)
DELETE member
SET record.link_status = 'unlinked',
    record.attribution_status = 'parked',
    record.reeval_pending = true,
    record.attribution_eval_at = datetime()
"""

PERSON_HAS_IDENTIFIER_LINKS = """
MATCH (person:Person {person_id: $person_id})-[link:HAS_IDENTIFIER]->()
RETURN count(link) > 0 AS has_links
LIMIT 1
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
// The digest is uniquely keyed: another Person (merged or active) holding it
// must surface as a conflict instead of tripping the unique constraint.
OPTIONAL MATCH (holder:Person {nric_key: $nric_key})
WHERE holder.person_id <> person.person_id
WITH person, holder
WHERE holder IS NULL
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
