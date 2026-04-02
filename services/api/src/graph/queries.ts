// ---------------------------------------------------------------------------
// Cypher query constants for the profile unifier Neo4j graph.
//
// Rules followed:
//   - HAS_FACT goes Person -> SourceRecord
//   - IDENTIFIED_BY carries source_system_key and source_record_pk on the rel
//   - Golden profile fields live directly on the Person node
//   - preferred_address_id is resolved to a full Address at read time
//   - All reads use parameterised queries; writes belong in route files
//     and MUST use session.executeWrite with explicit transactions.
// ---------------------------------------------------------------------------

// --- Person Lookup ---

/**
 * Find person(s) by identifier value.
 * Traverses Identifier -> Person and OPTIONAL MATCHes the preferred Address.
 */
export const FIND_PERSON_BY_IDENTIFIER = `
MATCH (id:Identifier {identifier_type: $identifier_type, normalized_value: $value})
  <-[:IDENTIFIED_BY]-(p:Person)
WHERE p.status <> 'merged'
OPTIONAL MATCH (addr:Address {address_id: p.preferred_address_id})
RETURN p {
  .person_id, .status, .is_high_value, .is_high_risk,
  .preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob,
  .profile_completeness_score, .golden_profile_computed_at, .golden_profile_version,
  .created_at, .updated_at
} AS person,
addr {
  .address_id, .unit_number, .street_number, .street_name,
  .city, .postal_code, .country_code, .normalized_full
} AS preferred_address
ORDER BY p.updated_at DESC
`;

/**
 * Get a single person by person_id.
 * Resolves the merge chain (max 1 hop) and resolves preferred_address.
 */
export const GET_PERSON_BY_ID = `
MATCH (p:Person {person_id: $person_id})
OPTIONAL MATCH (p)-[:MERGED_INTO]->(canonical:Person)
WITH coalesce(canonical, p) AS person
OPTIONAL MATCH (addr:Address {address_id: person.preferred_address_id})
OPTIONAL MATCH (sr:SourceRecord)-[:LINKED_TO]->(person)
WITH person, addr, count(sr) AS source_record_count
RETURN person {
  .person_id, .status, .is_high_value, .is_high_risk,
  .preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob,
  .profile_completeness_score, .golden_profile_computed_at, .golden_profile_version,
  .created_at, .updated_at
} AS person,
addr {
  .address_id, .unit_number, .street_number, .street_name,
  .city, .postal_code, .country_code, .normalized_full
} AS preferred_address,
source_record_count
`;

/**
 * Get source records linked to a person, with their source system info.
 * SourceRecord -[:LINKED_TO]-> Person, SourceRecord -[:FROM_SOURCE]-> SourceSystem
 */
export const GET_PERSON_SOURCE_RECORDS = `
MATCH (sr:SourceRecord)-[:LINKED_TO]->(p:Person {person_id: $person_id})
MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem)
RETURN sr {
  .source_record_pk, .source_record_id, .source_record_version,
  .link_status, .observed_at, .ingested_at
} AS source_record,
ss.source_key AS source_system,
p.person_id AS linked_person_id
ORDER BY sr.observed_at DESC
SKIP $skip LIMIT $limit
`;

/**
 * Get persons connected through shared Identifier AND/OR Address nodes.
 * Supports connection_type filter: 'identifier', 'address', or 'all'.
 *
 * For max_hops=1 the query finds direct shared nodes.
 * The application layer handles higher hop counts by adjusting the path length.
 */
export const GET_PERSON_CONNECTIONS_IDENTIFIER = `
MATCH (p:Person {person_id: $person_id})-[:IDENTIFIED_BY]->(id:Identifier)
  <-[:IDENTIFIED_BY]-(other:Person)
WHERE other.person_id <> p.person_id
  AND other.status <> 'merged'
  AND ($identifier_type IS NULL OR id.identifier_type = $identifier_type)
WITH other, collect(DISTINCT {identifier_type: id.identifier_type, normalized_value: id.normalized_value}) AS shared_identifiers
RETURN other.person_id AS person_id,
       other.status AS status,
       other.preferred_full_name AS preferred_full_name,
       1 AS hops,
       shared_identifiers,
       [] AS shared_addresses
ORDER BY other.preferred_full_name
SKIP $skip LIMIT $limit
`;

export const GET_PERSON_CONNECTIONS_ADDRESS = `
MATCH (p:Person {person_id: $person_id})-[:LIVES_AT]->(addr:Address)
  <-[:LIVES_AT]-(other:Person)
WHERE other.person_id <> p.person_id
  AND other.status <> 'merged'
WITH other, collect(DISTINCT {address_id: addr.address_id, normalized_full: addr.normalized_full}) AS shared_addresses
RETURN other.person_id AS person_id,
       other.status AS status,
       other.preferred_full_name AS preferred_full_name,
       1 AS hops,
       [] AS shared_identifiers,
       shared_addresses
ORDER BY other.preferred_full_name
SKIP $skip LIMIT $limit
`;

export const GET_PERSON_CONNECTIONS_ALL = `
MATCH (p:Person {person_id: $person_id})
OPTIONAL MATCH (p)-[:IDENTIFIED_BY]->(id:Identifier)<-[:IDENTIFIED_BY]-(oi:Person)
  WHERE oi.person_id <> p.person_id AND oi.status <> 'merged'
    AND ($identifier_type IS NULL OR id.identifier_type = $identifier_type)
OPTIONAL MATCH (p)-[:LIVES_AT]->(addr:Address)<-[:LIVES_AT]-(oa:Person)
  WHERE oa.person_id <> p.person_id AND oa.status <> 'merged'
WITH p,
  collect(DISTINCT CASE WHEN oi IS NOT NULL THEN {person_id: oi.person_id, status: oi.status, preferred_full_name: oi.preferred_full_name, identifier_type: id.identifier_type, normalized_value: id.normalized_value} END) AS id_links,
  collect(DISTINCT CASE WHEN oa IS NOT NULL THEN {person_id: oa.person_id, status: oa.status, preferred_full_name: oa.preferred_full_name, address_id: addr.address_id, normalized_full: addr.normalized_full} END) AS addr_links
UNWIND (id_links + addr_links) AS link
WITH link WHERE link IS NOT NULL
WITH link.person_id AS person_id,
     link.status AS status,
     link.preferred_full_name AS preferred_full_name,
     collect(DISTINCT CASE WHEN link.identifier_type IS NOT NULL THEN {identifier_type: link.identifier_type, normalized_value: link.normalized_value} END) AS shared_identifiers_raw,
     collect(DISTINCT CASE WHEN link.address_id IS NOT NULL THEN {address_id: link.address_id, normalized_full: link.normalized_full} END) AS shared_addresses_raw
RETURN person_id, status, preferred_full_name, 1 AS hops,
       [x IN shared_identifiers_raw WHERE x IS NOT NULL] AS shared_identifiers,
       [x IN shared_addresses_raw WHERE x IS NOT NULL] AS shared_addresses
ORDER BY preferred_full_name
SKIP $skip LIMIT $limit
`;

/**
 * Full-text search on preferred_full_name using the person_name_search index.
 */
export const SEARCH_PERSONS = `
CALL db.index.fulltext.queryNodes('person_name_search', $query) YIELD node AS p, score
WHERE p.status <> 'merged'
  AND ($status IS NULL OR p.status = $status)
OPTIONAL MATCH (addr:Address {address_id: p.preferred_address_id})
RETURN p {
  .person_id, .status, .is_high_value, .is_high_risk,
  .preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob,
  .profile_completeness_score, .golden_profile_computed_at, .golden_profile_version,
  .created_at, .updated_at
} AS person,
addr {
  .address_id, .unit_number, .street_number, .street_name,
  .city, .postal_code, .country_code, .normalized_full
} AS preferred_address,
score
ORDER BY score DESC
SKIP $skip LIMIT $limit
`;

/**
 * Audit trail: MergeEvent nodes connected to person via ABSORBED or SURVIVOR.
 */
export const GET_PERSON_AUDIT = `
MATCH (me:MergeEvent)
WHERE (me)-[:ABSORBED]->(:Person {person_id: $person_id})
   OR (me)-[:SURVIVOR]->(:Person {person_id: $person_id})
OPTIONAL MATCH (me)-[:ABSORBED]->(absorbed:Person)
OPTIONAL MATCH (me)-[:SURVIVOR]->(survivor:Person)
OPTIONAL MATCH (me)-[:TRIGGERED_BY]->(md:MatchDecision)
RETURN me {
  .merge_event_id, .event_type, .actor_type, .actor_id,
  .reason, .metadata, .created_at
} AS merge_event,
absorbed.person_id AS absorbed_person_id,
survivor.person_id AS survivor_person_id,
md.match_decision_id AS triggered_by_decision_id
ORDER BY me.created_at DESC
SKIP $skip LIMIT $limit
`;

/**
 * Match decisions related to a person (via ABOUT_LEFT or ABOUT_RIGHT).
 */
export const GET_PERSON_MATCHES = `
MATCH (md:MatchDecision)
WHERE (md)-[:ABOUT_LEFT]->(:Person {person_id: $person_id})
   OR (md)-[:ABOUT_RIGHT]->(:Person {person_id: $person_id})
OPTIONAL MATCH (md)-[:ABOUT_LEFT]->(left)
OPTIONAL MATCH (md)-[:ABOUT_RIGHT]->(right)
RETURN md {
  .match_decision_id, .engine_type, .engine_version, .policy_version,
  .decision, .confidence, .reasons, .blocking_conflicts, .created_at
} AS match_decision,
left.person_id AS left_person_id,
right.person_id AS right_person_id
ORDER BY md.created_at DESC
SKIP $skip LIMIT $limit
`;

// --- Review Cases ---

/**
 * List review cases with optional filters, joined to their MatchDecision.
 */
export const LIST_REVIEW_CASES = `
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
WHERE ($queue_state IS NULL OR rc.queue_state = $queue_state)
  AND ($assigned_to IS NULL OR rc.assigned_to = $assigned_to)
  AND ($priority_lte IS NULL OR rc.priority <= $priority_lte)
RETURN rc {
  .review_case_id, .queue_state, .priority, .assigned_to,
  .follow_up_at, .sla_due_at, .resolution, .resolved_at,
  .actions, .created_at, .updated_at
} AS review_case,
md {
  .match_decision_id, .engine_type, .engine_version, .policy_version,
  .decision, .confidence, .reasons, .blocking_conflicts, .created_at
} AS match_decision
ORDER BY rc.priority, rc.sla_due_at, rc.created_at
SKIP $skip LIMIT $limit
`;

/**
 * Full detail for a single review case, including left/right entities.
 */
export const GET_REVIEW_CASE = `
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
OPTIONAL MATCH (md)-[:ABOUT_LEFT]->(left)
OPTIONAL MATCH (md)-[:ABOUT_RIGHT]->(right)
OPTIONAL MATCH (left_addr:Address) WHERE left:Person AND left_addr.address_id = left.preferred_address_id
OPTIONAL MATCH (right_addr:Address) WHERE right:Person AND right_addr.address_id = right.preferred_address_id
RETURN rc {
  .review_case_id, .queue_state, .priority, .assigned_to,
  .follow_up_at, .sla_due_at, .resolution, .resolved_at,
  .actions, .created_at, .updated_at
} AS review_case,
md {
  .match_decision_id, .engine_type, .engine_version, .policy_version,
  .decision, .confidence, .reasons, .blocking_conflicts, .created_at
} AS match_decision,
left { .person_id, .status, .preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob } AS left_entity,
left_addr { .address_id, .unit_number, .street_number, .street_name, .city, .postal_code, .country_code, .normalized_full } AS left_address,
right { .person_id, .status, .preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob } AS right_entity,
right_addr { .address_id, .unit_number, .street_number, .street_name, .city, .postal_code, .country_code, .normalized_full } AS right_address
`;
