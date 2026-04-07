"""Cypher query constants for the ingestion pipeline.

Every write query is an explicit, parameterized Cypher statement intended
to run inside a single ``session.execute_write`` transaction function.
"""

# ---------------------------------------------------------------------------
# Idempotency check
# ---------------------------------------------------------------------------

CHECK_SOURCE_RECORD_EXISTS = """
MATCH (sr:SourceRecord {
    source_record_id: $source_record_id
})
WHERE sr.record_hash = $record_hash
MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem {source_key: $source_system})
RETURN sr.source_record_pk AS source_record_pk
LIMIT 1
"""

# ---------------------------------------------------------------------------
# Upsert shared nodes (MERGE to prevent duplicates)
# ---------------------------------------------------------------------------

UPSERT_IDENTIFIER = """
MERGE (id:Identifier {
    identifier_type: $identifier_type,
    normalized_value: $normalized_value
})
ON CREATE SET
    id.identifier_id = randomUUID(),
    id.created_at = datetime()
RETURN id.identifier_id AS identifier_id
"""

UPSERT_ADDRESS = """
MERGE (addr:Address {
    country_code: $country_code,
    postal_code:  $postal_code,
    street_name:  $street_name,
    street_number: $street_number,
    unit_number:  $unit_number
})
ON CREATE SET
    addr.address_id      = randomUUID(),
    addr.building_name   = $building_name,
    addr.city            = $city,
    addr.state_province  = $state_province,
    addr.normalized_full = $normalized_full,
    addr.created_at      = datetime()
RETURN addr.address_id AS address_id
"""

# ---------------------------------------------------------------------------
# Person creation
# ---------------------------------------------------------------------------

CREATE_PERSON = """
CREATE (p:Person {
    person_id:  randomUUID(),
    status:     'active',
    is_high_value: false,
    is_high_risk:  false,
    profile_completeness_score: 0.0,
    created_at: datetime(),
    updated_at: datetime()
})
RETURN p.person_id AS person_id
"""

# ---------------------------------------------------------------------------
# Source Record creation + FROM_SOURCE relationship
# ---------------------------------------------------------------------------

CREATE_SOURCE_RECORD = """
MATCH (ss:SourceSystem {source_key: $source_system})
CREATE (sr:SourceRecord {
    source_record_pk:      randomUUID(),
    source_record_id:      $source_record_id,
    source_record_version: $source_record_version,
    record_type:           $record_type,
    extraction_confidence: $extraction_confidence,
    extraction_method:     $extraction_method,
    conversation_ref:      $conversation_ref,
    link_status:           $link_status,
    observed_at:           datetime($observed_at),
    ingested_at:           datetime(),
    record_hash:           $record_hash,
    raw_payload:           $raw_payload,
    normalized_payload:    $normalized_payload,
    retention_expires_at:  null
})-[:FROM_SOURCE]->(ss)
RETURN sr.source_record_pk AS source_record_pk
"""

# ---------------------------------------------------------------------------
# Link source record to resolved person
# ---------------------------------------------------------------------------

LINK_SOURCE_RECORD_TO_PERSON = """
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
MATCH (p:Person {person_id: $person_id})
CREATE (sr)-[:LINKED_TO {linked_at: datetime()}]->(p)
"""

# ---------------------------------------------------------------------------
# Identifier relationship (Person -> Identifier)
# ---------------------------------------------------------------------------

LINK_PERSON_TO_IDENTIFIER = """
MATCH (p:Person {person_id: $person_id})
MATCH (id:Identifier {identifier_type: $identifier_type, normalized_value: $normalized_value})
MERGE (p)-[rel:IDENTIFIED_BY]->(id)
ON CREATE SET
    rel.is_verified = $is_verified,
    rel.verification_method = $verification_method,
    rel.is_active = true,
    rel.quality_flag = $quality_flag,
    rel.first_seen_at = datetime(),
    rel.last_seen_at = datetime(),
    rel.last_confirmed_at = datetime(),
    rel.source_system_key = $source_system_key,
    rel.source_record_pk = $source_record_pk
ON MATCH SET
    rel.last_seen_at = datetime(),
    rel.last_confirmed_at = datetime(),
    rel.is_active = true,
    rel.source_record_pk = $source_record_pk
"""

# ---------------------------------------------------------------------------
# Address relationship (Person -> Address)
# ---------------------------------------------------------------------------

LINK_PERSON_TO_ADDRESS = """
MATCH (p:Person {person_id: $person_id})
MATCH (addr:Address {
    country_code:  $country_code,
    postal_code:   $postal_code,
    street_name:   $street_name,
    street_number: $street_number,
    unit_number:   $unit_number
})
MERGE (p)-[rel:LIVES_AT]->(addr)
ON CREATE SET
    rel.is_active = true,
    rel.is_verified = $is_verified,
    rel.quality_flag = $quality_flag,
    rel.source_system_key = $source_system_key,
    rel.source_record_pk = $source_record_pk,
    rel.first_seen_at = datetime(),
    rel.last_seen_at = datetime(),
    rel.last_confirmed_at = datetime()
ON MATCH SET
    rel.last_seen_at = datetime(),
    rel.last_confirmed_at = datetime(),
    rel.is_active = true,
    rel.source_record_pk = $source_record_pk
"""

# ---------------------------------------------------------------------------
# Attribute fact (Person -[:HAS_FACT]-> SourceRecord) — NOT a self-loop
# ---------------------------------------------------------------------------

CREATE_ATTRIBUTE_FACT = """
MATCH (p:Person {person_id: $person_id})
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
CREATE (p)-[:HAS_FACT {
    attribute_name:    $attribute_name,
    attribute_value:   $attribute_value,
    source_trust_tier: $source_trust_tier,
    confidence:        $confidence,
    quality_flag:      $quality_flag,
    is_current_hint:   false,
    observed_at:       datetime($observed_at),
    created_at:        datetime()
}]->(sr)
"""

# ---------------------------------------------------------------------------
# Merge event for person creation
# ---------------------------------------------------------------------------

CREATE_MERGE_EVENT_PERSON_CREATED = """
MATCH (p:Person {person_id: $person_id})
CREATE (me:MergeEvent {
    merge_event_id:      randomUUID(),
    event_type:          'person_created',
    actor_type:          'system',
    actor_id:            'ingestion_pipeline',
    reason:              'New person created — no matching candidates found',
    metadata:            '{}',
    created_at:          datetime(),
    retention_expires_at: null
})-[:SURVIVOR]->(p)
RETURN me.merge_event_id AS merge_event_id
"""

# ---------------------------------------------------------------------------
# Merge event for auto-merge (deterministic / heuristic)
# ---------------------------------------------------------------------------

CREATE_MERGE_EVENT_AUTO_MERGE = """
MATCH (from_p:Person {person_id: $from_person_id})
MATCH (to_p:Person {person_id: $to_person_id})
CREATE (me:MergeEvent {
    merge_event_id: randomUUID(),
    event_type: 'auto_merge',
    actor_type: 'system',
    actor_id: 'match_engine',
    reason: $reason,
    metadata: '{}',
    created_at: datetime(),
    retention_expires_at: null
})
CREATE (me)-[:ABSORBED]->(from_p)
CREATE (me)-[:SURVIVOR]->(to_p)
RETURN me.merge_event_id AS merge_event_id
"""

# ---------------------------------------------------------------------------
# Full merge: rewire relationships from absorbed person to survivor
# ---------------------------------------------------------------------------

REWIRE_LINKED_TO = """
MATCH (sr:SourceRecord)-[old:LINKED_TO]->(absorbed:Person {person_id: $absorbed_id})
DELETE old
WITH sr
MATCH (survivor:Person {person_id: $survivor_id})
CREATE (sr)-[:LINKED_TO {linked_at: datetime()}]->(survivor)
RETURN count(sr) AS rewired_count
"""

REWIRE_IDENTIFIED_BY = """
MATCH (absorbed:Person {person_id: $absorbed_id})-[old:IDENTIFIED_BY]->(id:Identifier)
WITH absorbed, old, id, properties(old) AS props
DELETE old
WITH id, props
MATCH (survivor:Person {person_id: $survivor_id})
MERGE (survivor)-[rel:IDENTIFIED_BY]->(id)
ON CREATE SET
    rel.is_verified = props.is_verified,
    rel.verification_method = props.verification_method,
    rel.is_active = props.is_active,
    rel.quality_flag = props.quality_flag,
    rel.first_seen_at = props.first_seen_at,
    rel.last_seen_at = props.last_seen_at,
    rel.last_confirmed_at = props.last_confirmed_at,
    rel.source_system_key = props.source_system_key,
    rel.source_record_pk = props.source_record_pk
ON MATCH SET
    rel.last_seen_at = datetime(),
    rel.last_confirmed_at = datetime()
RETURN count(id) AS rewired_count
"""

REWIRE_LIVES_AT = """
MATCH (absorbed:Person {person_id: $absorbed_id})-[old:LIVES_AT]->(addr:Address)
WITH absorbed, old, addr, properties(old) AS props
DELETE old
WITH addr, props
MATCH (survivor:Person {person_id: $survivor_id})
MERGE (survivor)-[rel:LIVES_AT]->(addr)
ON CREATE SET
    rel.is_active = props.is_active,
    rel.is_verified = props.is_verified,
    rel.quality_flag = props.quality_flag,
    rel.source_system_key = props.source_system_key,
    rel.source_record_pk = props.source_record_pk,
    rel.first_seen_at = props.first_seen_at,
    rel.last_seen_at = props.last_seen_at,
    rel.last_confirmed_at = props.last_confirmed_at
ON MATCH SET
    rel.last_seen_at = datetime(),
    rel.last_confirmed_at = datetime()
RETURN count(addr) AS rewired_count
"""

REWIRE_HAS_FACT = """
MATCH (absorbed:Person {person_id: $absorbed_id})-[old:HAS_FACT]->(sr:SourceRecord)
WITH absorbed, old, sr, properties(old) AS props
DELETE old
WITH sr, props
MATCH (survivor:Person {person_id: $survivor_id})
CREATE (survivor)-[:HAS_FACT {
    attribute_name: props.attribute_name,
    attribute_value: props.attribute_value,
    source_trust_tier: props.source_trust_tier,
    confidence: props.confidence,
    quality_flag: props.quality_flag,
    is_current_hint: props.is_current_hint,
    observed_at: props.observed_at,
    created_at: props.created_at
}]->(sr)
RETURN count(sr) AS rewired_count
"""

MARK_PERSON_MERGED = """
MATCH (absorbed:Person {person_id: $absorbed_id})
SET absorbed.status = 'merged',
    absorbed.updated_at = datetime()
"""

CREATE_MERGED_INTO = """
MATCH (absorbed:Person {person_id: $absorbed_id})
MATCH (survivor:Person {person_id: $survivor_id})
CREATE (absorbed)-[:MERGED_INTO {
    merge_event_id: $merge_event_id,
    actor: 'match_engine',
    timestamp: datetime()
}]->(survivor)
"""

PATH_COMPRESS_MERGED_INTO = """
MATCH (prev:Person)-[old:MERGED_INTO]->(absorbed:Person {person_id: $absorbed_id})
WITH prev, old, properties(old) AS props
DELETE old
WITH prev, props
MATCH (survivor:Person {person_id: $survivor_id})
CREATE (prev)-[:MERGED_INTO {
    merge_event_id: props.merge_event_id,
    actor: props.actor,
    timestamp: props.timestamp
}]->(survivor)
RETURN count(prev) AS compressed_count
"""

GET_AFFECTED_SOURCE_RECORDS = """
MATCH (sr:SourceRecord)-[:LINKED_TO]->(p:Person {person_id: $person_id})
RETURN sr.source_record_pk AS source_record_pk
"""

LINK_MERGE_EVENT_TRIGGERED_BY = """
MATCH (me:MergeEvent {merge_event_id: $merge_event_id})
MATCH (md:MatchDecision {match_decision_id: $match_decision_id})
CREATE (me)-[:TRIGGERED_BY]->(md)
"""

LINK_MERGE_EVENT_AFFECTED_RECORD = """
MATCH (me:MergeEvent {merge_event_id: $merge_event_id})
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
CREATE (me)-[:AFFECTED_RECORD]->(sr)
"""

# ---------------------------------------------------------------------------
# Candidate generation — traverse shared Identifier nodes
# ---------------------------------------------------------------------------

FIND_CANDIDATES_BY_IDENTIFIER = """
MATCH (id:Identifier {
    identifier_type: $identifier_type,
    normalized_value: $normalized_value
})
<-[rel:IDENTIFIED_BY]-(candidate:Person {status: 'active'})
WHERE rel.is_active = true
  AND rel.quality_flag IN ['valid', 'partial_parse']
RETURN DISTINCT candidate.person_id AS person_id
"""

# ---------------------------------------------------------------------------
# Candidate generation — traverse shared Address nodes
# ---------------------------------------------------------------------------

FIND_CANDIDATES_BY_ADDRESS = """
MATCH (addr:Address {
    country_code:  $country_code,
    postal_code:   $postal_code,
    street_name:   $street_name,
    street_number: $street_number,
    unit_number:   $unit_number
})
<-[rel:LIVES_AT]-(candidate:Person {status: 'active'})
WHERE rel.is_active = true
  AND rel.quality_flag IN ['valid', 'partial_parse']
RETURN DISTINCT candidate.person_id AS person_id
"""

# ---------------------------------------------------------------------------
# Check identifier fanout (cardinality cap)
# ---------------------------------------------------------------------------

CHECK_IDENTIFIER_FANOUT = """
MATCH (id:Identifier {identifier_type: $identifier_type, normalized_value: $normalized_value})
      <-[:IDENTIFIED_BY]-(p:Person {status: 'active'})
RETURN count(p) AS fanout
"""

# ---------------------------------------------------------------------------
# Golden profile: fetch attribute facts for a person
# ---------------------------------------------------------------------------

FETCH_PERSON_FACTS = """
MATCH (p:Person {person_id: $person_id})-[f:HAS_FACT]->(sr:SourceRecord)
RETURN f.attribute_name AS attribute_name,
       f.attribute_value AS attribute_value,
       f.source_trust_tier AS source_trust_tier,
       f.observed_at AS observed_at,
       f.quality_flag AS quality_flag
"""

FETCH_PERSON_IDENTIFIERS = """
MATCH (p:Person {person_id: $person_id})-[rel:IDENTIFIED_BY]->(id:Identifier)
WHERE rel.is_active = true
RETURN id.identifier_type AS identifier_type,
       id.normalized_value AS normalized_value,
       rel.is_verified AS is_verified,
       rel.last_confirmed_at AS last_confirmed_at
"""

FETCH_PERSON_ADDRESSES = """
MATCH (p:Person {person_id: $person_id})-[rel:LIVES_AT]->(addr:Address)
WHERE rel.is_active = true
RETURN addr.address_id AS address_id,
       addr.normalized_full AS normalized_full,
       rel.is_verified AS is_verified,
       rel.last_confirmed_at AS last_confirmed_at
"""

UPDATE_GOLDEN_PROFILE = """
MATCH (p:Person {person_id: $person_id})
SET p.preferred_full_name = $preferred_full_name,
    p.preferred_phone = $preferred_phone,
    p.preferred_email = $preferred_email,
    p.preferred_dob = $preferred_dob,
    p.preferred_address_id = $preferred_address_id,
    p.profile_completeness_score = $profile_completeness_score,
    p.golden_profile_computed_at = datetime(),
    p.golden_profile_version = $golden_profile_version,
    p.updated_at = datetime()
RETURN p.person_id AS person_id
"""

# ---------------------------------------------------------------------------
# Check for NO_MATCH_LOCK between two persons
# ---------------------------------------------------------------------------

CHECK_NO_MATCH_LOCK = """
MATCH (a:Person {person_id: $left_person_id})
      -[lock:NO_MATCH_LOCK]-
      (b:Person {person_id: $right_person_id})
WHERE lock.expires_at IS NULL OR lock.expires_at > datetime()
RETURN count(lock) > 0 AS is_locked
"""

# ---------------------------------------------------------------------------
# IngestRun lifecycle
# ---------------------------------------------------------------------------

CREATE_INGEST_RUN = """
MATCH (ss:SourceSystem {source_key: $source_key})
CREATE (ir:IngestRun {
    ingest_run_id: randomUUID(),
    run_type: $run_type,
    status: 'started',
    started_at: datetime(),
    finished_at: null,
    record_count: 0,
    rejected_count: 0,
    metadata: '{}'
})-[:FROM_SOURCE]->(ss)
RETURN ir.ingest_run_id AS ingest_run_id
"""

UPDATE_INGEST_RUN = """
MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id})
SET ir.status = $status,
    ir.finished_at = datetime(),
    ir.record_count = $record_count,
    ir.rejected_count = $rejected_count
"""

LINK_SOURCE_RECORD_TO_RUN = """
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id})
CREATE (sr)-[:PART_OF_RUN]->(ir)
"""

# ---------------------------------------------------------------------------
# MatchDecision persistence
# ---------------------------------------------------------------------------

CREATE_MATCH_DECISION = """
CREATE (md:MatchDecision {
    match_decision_id: randomUUID(),
    engine_type: $engine_type,
    engine_version: $engine_version,
    decision: $decision,
    confidence: $confidence,
    reasons: $reasons,
    blocking_conflicts: $blocking_conflicts,
    feature_snapshot: $feature_snapshot,
    policy_version: $policy_version,
    created_at: datetime(),
    retention_expires_at: null
})
RETURN md.match_decision_id AS match_decision_id
"""

LINK_MATCH_DECISION_LEFT_PERSON = """
MATCH (md:MatchDecision {match_decision_id: $match_decision_id})
MATCH (p:Person {person_id: $person_id})
CREATE (md)-[:ABOUT_LEFT {entity_type: 'person'}]->(p)
"""

LINK_MATCH_DECISION_RIGHT_PERSON = """
MATCH (md:MatchDecision {match_decision_id: $match_decision_id})
MATCH (p:Person {person_id: $person_id})
CREATE (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(p)
"""

LINK_MATCH_DECISION_LEFT_SOURCE_RECORD = """
MATCH (md:MatchDecision {match_decision_id: $match_decision_id})
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
CREATE (md)-[:ABOUT_LEFT {entity_type: 'source_record'}]->(sr)
"""

LINK_MATCH_DECISION_RIGHT_SOURCE_RECORD = """
MATCH (md:MatchDecision {match_decision_id: $match_decision_id})
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
CREATE (md)-[:ABOUT_RIGHT {entity_type: 'source_record'}]->(sr)
"""

# ---------------------------------------------------------------------------
# ReviewCase creation
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# KNOWS — declared social Person↔Person relationship (e.g. emergency contacts)
# ---------------------------------------------------------------------------

#: Idempotent create of a KNOWS edge between two persons. Deduplicated by
#: (source_system_key, source_record_pk) so re-ingestion of the same source
#: row never produces duplicate edges.
LINK_PERSON_KNOWS = """
MATCH (declarer:Person {person_id: $declarer_person_id})
MATCH (contact:Person  {person_id: $contact_person_id})
MERGE (declarer)-[rel:KNOWS {
    source_system_key: $source_system_key,
    source_record_pk:  $source_record_pk
}]->(contact)
ON CREATE SET
    rel.knows_id              = randomUUID(),
    rel.relationship_label    = $relationship_label,
    rel.relationship_category = $relationship_category,
    rel.declared_by_person_id = $declarer_person_id,
    rel.status                = $status,
    rel.approved_at           = $approved_at,
    rel.first_seen_at         = datetime(),
    rel.last_seen_at          = datetime(),
    rel.last_confirmed_at     = datetime(),
    rel.created_at            = datetime(),
    rel.updated_at            = datetime()
ON MATCH SET
    rel.relationship_label = $relationship_label,
    rel.status             = $status,
    rel.approved_at        = $approved_at,
    rel.last_seen_at       = datetime(),
    rel.last_confirmed_at  = datetime(),
    rel.updated_at         = datetime()
RETURN rel.knows_id AS knows_id
"""

#: Resolve a (declarer source_record_pk, contact source_record_pk) pair to
#: their currently linked Person ids. Used by the post-resolution KNOWS
#: materializer to walk pending links once both sides have a Person.
RESOLVE_KNOWS_ENDPOINTS = """
MATCH (declarer_sr:SourceRecord {source_record_pk: $declarer_source_record_pk})
      -[:LINKED_TO]->(declarer:Person {status: 'active'})
MATCH (contact_sr:SourceRecord  {source_record_pk: $contact_source_record_pk})
      -[:LINKED_TO]->(contact:Person  {status: 'active'})
RETURN declarer.person_id AS declarer_person_id,
       contact.person_id  AS contact_person_id
"""

#: Rewire KNOWS edges in both directions when ``absorbed`` is merged into
#: ``survivor``. Mirrors the rewire pattern for IDENTIFIED_BY / LIVES_AT.
REWIRE_KNOWS_OUT = """
MATCH (absorbed:Person {person_id: $absorbed_id})-[old:KNOWS]->(other:Person)
WHERE other.person_id <> $survivor_id
WITH old, other, properties(old) AS props
DELETE old
WITH other, props
MATCH (survivor:Person {person_id: $survivor_id})
MERGE (survivor)-[rel:KNOWS {
    source_system_key: props.source_system_key,
    source_record_pk:  props.source_record_pk
}]->(other)
ON CREATE SET rel += props, rel.declared_by_person_id = $survivor_id
RETURN count(other) AS rewired_count
"""

REWIRE_KNOWS_IN = """
MATCH (other:Person)-[old:KNOWS]->(absorbed:Person {person_id: $absorbed_id})
WHERE other.person_id <> $survivor_id
WITH old, other, properties(old) AS props
DELETE old
WITH other, props
MATCH (survivor:Person {person_id: $survivor_id})
MERGE (other)-[rel:KNOWS {
    source_system_key: props.source_system_key,
    source_record_pk:  props.source_record_pk
}]->(survivor)
ON CREATE SET rel += props
RETURN count(other) AS rewired_count
"""

CREATE_REVIEW_CASE = """
MATCH (md:MatchDecision {match_decision_id: $match_decision_id})
CREATE (rc:ReviewCase {
    review_case_id: randomUUID(),
    priority: $priority,
    queue_state: 'open',
    assigned_to: null,
    follow_up_at: null,
    sla_due_at: datetime($sla_due_at),
    resolution: null,
    resolved_at: null,
    actions: '[]',
    created_at: datetime(),
    updated_at: datetime()
})-[:FOR_DECISION]->(md)
RETURN rc.review_case_id AS review_case_id
"""
