# Profile Unifier Graph Schema

## Purpose

Define a graph-native Neo4j reference schema for the profile unifier platform.
The schema is designed around traversal patterns — identity resolution, contact
tracing, and relationship analysis — not as a port of a relational model.

## Design Philosophy

Model things as nodes only when they are independently queried or when they
sit at the intersection of multiple relationships. Use relationships to capture
associations and carry contextual properties. Prefer native Neo4j types (lists,
maps, datetime) over serialized JSON strings.

**Nodes are nouns you search for.** Person, Identifier, SourceRecord,
MatchDecision, ReviewCase, MergeEvent — these are all independently queried.

**Relationships are verbs.** `IDENTIFIED_BY`, `LINKED_TO`, `MERGED_INTO`,
`NO_MATCH_LOCK` — these carry the graph power and enable multi-hop traversal.

**Properties live where they belong.** Trust config is a property map on
relationships or on SourceSystem, not a standalone node. Golden profile fields
are properties on the Person node. Review actions are an ordered list of events
on a ReviewCase, not a separate node type.

## Scope

- source-system registration and field trust
- ingestion runs and raw source records
- canonical persons and golden profile
- identifiers and attribute facts
- match decisions and review cases
- merge/unmerge audit trail
- manual locks and survivorship overrides

## Neo4j Assumptions

Neo4j 5.x or later. ACID transactions, composite indexes, full-text search,
native datetime and list types.

## Graph Overview

```
(Identifier)<-[:IDENTIFIED_BY]-(Person)-[:LIVES_AT]->(Address)
                                  ^
(SourceRecord)-[:LINKED_TO]-------+
(SourceRecord)-[:FROM_SOURCE]->(SourceSystem)

(Person)-[:MERGED_INTO]->(Person)
(Person)-[:NO_MATCH_LOCK {props}]->(Person)
(Person)-[:HAS_FACT {props}]->(SourceRecord)

(MatchDecision)-[:ABOUT_LEFT]--->( Person | SourceRecord )
(MatchDecision)-[:ABOUT_RIGHT]-->( Person | SourceRecord )
(ReviewCase)-[:FOR_DECISION]->(MatchDecision)
(MergeEvent)-[:ABSORBED]->(Person)
(MergeEvent)-[:SURVIVOR]->(Person)
```

## Node Labels

### Person

The central node. Golden profile fields live directly on the Person node —
no separate GoldenProfile node needed since it is always 1:1 and always
fetched together.

```cypher
CREATE (p:Person {
  person_id: randomUUID(),
  status: 'active',               // active | merged | suppressed
  is_high_value: false,
  is_high_risk: false,
  suppression_reason: null,

  // golden profile (recomputed in place)
  preferred_full_name: null,
  preferred_phone: null,
  preferred_email: null,
  preferred_dob: null,
  preferred_address_id: null,       // reference to preferred Address node
  profile_completeness_score: 0.0,
  golden_profile_computed_at: null,
  golden_profile_version: null,

  created_at: datetime(),
  updated_at: datetime()
})
```

Why on Person: every person read fetches the golden profile. A separate node
adds a mandatory hop to every query for zero benefit. When the profile is
recomputed, update in place — the computation is synchronous within the merge
transaction anyway.

### Identifier

Shared identifiers are the connective tissue of the graph and the key enabler
for contact tracing. An Identifier node represents a _value_ (e.g. a specific
phone number). Persons connect to it, and any two Persons sharing the same
Identifier are implicitly linked — this is what makes multi-hop traversal
natural.

```cypher
CREATE (id:Identifier {
  identifier_id: randomUUID(),
  identifier_type: 'phone',       // phone | email | government_id_hash |
                                   // external_customer_id | membership_id |
                                   // crm_contact_id | loyalty_id | custom
  normalized_value: '+6591234567',
  hashed_value: null,              // for sensitive IDs
  created_at: datetime()
})
```

Constraint: at least one of `normalized_value` or `hashed_value` must be set.

Why a node and not a property: identifiers are shared across persons, and
querying "who else shares this phone" is a core traversal. Making identifiers
nodes means contact tracing is a single `MATCH` hop through shared Identifier
nodes rather than a cross-join on property values.

### Address

Normalized, structured addresses are shared nodes — the same pattern as
Identifier. Two persons at the same normalized address are implicitly linked,
enabling "who else lives here?" traversal for household detection and contact
tracing.

```cypher
CREATE (addr:Address {
  address_id: randomUUID(),
  unit_number: null,              // apartment, suite, unit
  street_number: '10',
  street_name: 'Example Street',
  building_name: null,
  city: 'Singapore',
  state_province: null,
  postal_code: '123456',
  country_code: 'SG',
  normalized_full: '10 example street, singapore 123456, sg',
  geo_lat: null,                  // optional geocoded latitude
  geo_lon: null,                  // optional geocoded longitude
  created_at: datetime()
})
```

Addresses are stored in structured, normalized components — not as free-text
strings. The normalization pipeline decomposes raw addresses during ingestion.

**Deduplication key**: `(country_code, postal_code, street_name, street_number,
unit_number)`. Use `MERGE` on this composite key to prevent duplicate Address
nodes. `normalized_full` is a lowercased, whitespace-normalized concatenation
for display and full-text search.

Why a node and not a `HAS_FACT` property: addresses are shared across persons.
"Who else lives at this address?" is a core traversal for household detection,
contact tracing, and sales territory analysis. A shared Address node makes
this a single hop — the same pattern that makes Identifier nodes powerful.

### SourceSystem

```cypher
CREATE (ss:SourceSystem {
  source_system_id: randomUUID(),
  source_key: 'bitrix',
  display_name: 'Bitrix CRM',
  system_type: 'crm',
  is_active: true,
  // field-level trust as a native map — no separate node needed
  field_trust: {
    phone: 'tier_3',
    email: 'tier_3',
    preferred_name: 'tier_3',
    legal_name: 'tier_4',
    dob: 'tier_4',
    address: 'tier_4'
  },
  created_at: datetime(),
  updated_at: datetime()
})
```

Why field trust is a map: it is always read together with the source system,
never queried independently, and changes infrequently. A separate node per
field-trust row is relational thinking.

### SourceRecord

Immutable raw input records.

```cypher
CREATE (sr:SourceRecord {
  source_record_pk: randomUUID(),
  source_record_id: '12345',
  source_record_version: null,
  link_status: 'pending_review',   // linked | pending_review | rejected | suppressed
  observed_at: datetime(),
  ingested_at: datetime(),
  record_hash: 'sha256:abc123',
  raw_payload: {},                 // native map, not a JSON string
  normalized_payload: {},
  metadata: {},
  retention_expires_at: null
})
```

### IngestRun

Lightweight operational node for observability and replay.

```cypher
CREATE (ir:IngestRun {
  ingest_run_id: randomUUID(),
  run_type: 'batch',
  status: 'started',
  started_at: datetime(),
  finished_at: null,
  record_count: 0,
  rejected_count: 0,
  metadata: {}
})
```

### MatchDecision

Immutable decision record from any engine path.

```cypher
CREATE (md:MatchDecision {
  match_decision_id: randomUUID(),
  engine_type: 'heuristic',       // deterministic | heuristic | llm | manual
  engine_version: 'v1.0.0',
  decision: 'review',             // merge | review | no_match
  confidence: 0.78,
  reasons: ['same phone', 'same DOB', 'highly similar name'],
  blocking_conflicts: [],
  feature_snapshot: {},
  prompt_snapshot: null,
  policy_version: 'policy-2026-03-31',
  created_at: datetime(),
  retention_expires_at: null
})
```

Note: `reasons` and `blocking_conflicts` are native Neo4j lists.
`feature_snapshot` is a native map.

### ReviewCase

```cypher
CREATE (rc:ReviewCase {
  review_case_id: randomUUID(),
  priority: 100,
  queue_state: 'open',            // open | assigned | deferred | resolved | cancelled
  assigned_to: null,
  follow_up_at: null,
  sla_due_at: datetime(),
  resolution: null,               // merge | reject | manual_no_match | cancelled_superseded
  resolved_at: null,
  // review actions stored as an ordered list of maps — no separate node
  actions: [],
  created_at: datetime(),
  updated_at: datetime()
})
```

Why actions as a list: review actions are only ever read in the context of
their case, never queried across cases. They are an append-only audit log on
the case. Each entry is a map:

```cypher
// Appending an action:
SET rc.actions = rc.actions + [{
  action_type: 'merge',
  actor_type: 'reviewer',
  actor_id: 'reviewer_123',
  notes: 'Phone and DOB align.',
  created_at: datetime()
}]
```

The `review_action_type` values include both API-submitted actions (`merge`,
`reject`, `defer`, `escalate`, `manual_no_match`) and system-recorded actions
(`assign`, `unassign`, `cancel`, `reopen`). The API layer exposes only the
API-submitted subset.

### MergeEvent

Immutable audit record for lifecycle changes.

```cypher
CREATE (me:MergeEvent {
  merge_event_id: randomUUID(),
  event_type: 'auto_merge',       // person_created | auto_merge | manual_merge |
                                   // review_reject | manual_no_match | unmerge |
                                   // person_split | survivorship_override
  actor_type: 'system',
  actor_id: 'match_engine',
  reason: 'Verified government ID match',
  metadata: {},
  created_at: datetime(),
  retention_expires_at: null
})
```

## Canonical Enums

### Quality Flag

A single canonical set of quality flags used on all `IDENTIFIED_BY`, `LIVES_AT`,
and `HAS_FACT` relationships. Implementations must use these values exactly.

| Value | Meaning |
| --- | --- |
| `valid` | Normalized successfully, passes format validation |
| `invalid_format` | Could not parse or normalize (e.g. malformed phone, unparseable address) |
| `placeholder_value` | Detected as a placeholder: NA, Unknown, -, test, etc. |
| `shared_suspected` | Identifier or address appears on many persons — likely shared |
| `stale` | Not re-confirmed within the aging window |
| `source_untrusted` | Source system is tier_4 or flagged as unreliable |
| `partial_parse` | Address or name partially parsed — usable but incomplete |

Normalizers should return one of these values. Application code should treat
this as a closed enum.

## Relationship Types

### Identity and Linkage

| Relationship | From | To | Properties | Purpose |
| --- | --- | --- | --- | --- |
| `IDENTIFIED_BY` | Person | Identifier | `is_verified`, `verification_method`, `is_active`, `quality_flag`, `first_seen_at`, `last_seen_at`, `last_confirmed_at`, `source_system_key`, `source_record_pk` | Links a person to an identity signal. Rich properties track verification, aging, and provenance. |
| `LIVES_AT` | Person | Address | `is_active`, `is_verified`, `source_system_key`, `source_record_pk`, `first_seen_at`, `last_seen_at`, `last_confirmed_at`, `quality_flag` | Links a person to a normalized address. Same aging and verification model as `IDENTIFIED_BY`. |
| `LINKED_TO` | SourceRecord | Person | `linked_at` | Associates an ingested record with its resolved person. |
| `FROM_SOURCE` | SourceRecord / IngestRun | SourceSystem | — | Provenance. |
| `PART_OF_RUN` | SourceRecord | IngestRun | — | Groups records by ingestion batch. |
| `HAS_FACT` | Person | — (inline) | see below | Attribute facts (for non-address, non-identifier attributes: name, DOB, etc.). |

### Attribute Facts

Non-address, non-identifier attributes (name, DOB, etc.) are stored as
`HAS_FACT` relationships from Person to SourceRecord, with the fact data as
relationship properties.

```cypher
CREATE (p)-[:HAS_FACT {
  attribute_name: 'full_name',
  attribute_value: 'Alice Tan',
  source_trust_tier: 'tier_2',
  confidence: 1.0,
  quality_flag: 'valid',
  is_current_hint: false,
  observed_at: datetime(),
  created_at: datetime()
}]->(sr)
```

Why a relationship: a fact is always "Person observed this value from this
source record." It is never queried independently — always in the context
of a person or a source record.

**Note**: Addresses are modeled as shared `Address` nodes (not `HAS_FACT`
relationships) because "who else lives here?" is a core traversal. Identifiers
are similarly shared `Identifier` nodes. `HAS_FACT` is reserved for attributes
that are not shared or traversed across persons (name, DOB, etc.).

### Merge and Lifecycle

| Relationship | From | To | Properties | Purpose |
| --- | --- | --- | --- | --- |
| `MERGED_INTO` | Person (absorbed) | Person (survivor) | `merge_event_id`, `actor`, `timestamp` | Merge lineage. Path compression enforced: max 1 hop. |
| `ABSORBED` | MergeEvent | Person | — | Points to the absorbed person. |
| `SURVIVOR` | MergeEvent | Person | — | Points to the surviving person. |
| `TRIGGERED_BY` | MergeEvent | MatchDecision | — | Links audit to the decision that caused it. |
| `AFFECTED_RECORD` | MergeEvent | SourceRecord | — | For unmerge replay. |

### Review and Matching

| Relationship | From | To | Properties | Purpose |
| --- | --- | --- | --- | --- |
| `FOR_DECISION` | ReviewCase | MatchDecision | — | Links a review case to the decision it covers. |
| `ABOUT_LEFT` | MatchDecision | Person or SourceRecord | `entity_type` | Left side of the compared pair. |
| `ABOUT_RIGHT` | MatchDecision | Person or SourceRecord | `entity_type` | Right side. |

### Locks

Locks are direct relationships between persons — no intermediary node.

```cypher
CREATE (a:Person)-[:NO_MATCH_LOCK {
  lock_id: randomUUID(),
  reason: 'Shared business phone, not the same individual.',
  lock_type: 'manual_no_match',   // manual_no_match | manual_merge_hint | person_suppression
  expires_at: null,
  actor_type: 'reviewer',
  actor_id: 'reviewer_123',
  created_at: datetime()
}]->(b:Person)
```

Constraint: always created with `a.person_id < b.person_id` to prevent
duplicates.

Why a relationship: a lock exists only between two entities. It is checked
during candidate evaluation by traversing from one person to the other. A
standalone node with two pointer relationships is relational thinking.

### Survivorship Overrides

Stored as a property map on the Person node keyed by field name.

```cypher
SET p.survivorship_overrides = {
  preferred_email: {
    source_record_pk: '<uuid>',
    reason: 'Customer confirmed preferred email',
    actor_type: 'reviewer',
    actor_id: 'reviewer_123',
    created_at: datetime()
  }
}
```

Why on Person: overrides are per-person per-field and only read during golden
profile recomputation. They don't need independent querying.

### Aliases

```cypher
CREATE (p)-[:ALSO_KNOWN_AS {
  alias_namespace: 'legacy_crm',
  alias_value: 'old-id-123',
  created_at: datetime()
}]->(p)
```

Self-relationship on Person. Only needed during migration.

## Planned Extensions (Post-MVP)

### Explicit Person-to-Person Relationships

Beyond shared Identifier nodes, the graph will support typed, directed
relationships between persons for relationship intelligence:

```cypher
CREATE (a:Person)-[:REFERRED_BY {
  source_system_key: 'loyalty_app',
  confidence: 1.0,
  declared_by: 'customer',
  created_at: datetime()
}]->(b:Person)
```

Planned types: `REFERRED_BY`, `WORKS_WITH`, `FAMILY_OF`, `SAME_HOUSEHOLD`,
`SAME_ACCOUNT`. These are never auto-created by the matching engine — they
require source-system declarations or manual actions. They do not affect
identity resolution but participate in graph traversal queries.

No schema migration required — these are new relationship types on existing
Person nodes.

### Interaction Nodes

For contact tracing and sales analytics, interactions will be modeled as
nodes connecting multiple persons to a time and place:

```cypher
CREATE (ix:Interaction {
  interaction_id: randomUUID(),
  interaction_type: 'transaction',  // transaction | appointment | service_call | event
  occurred_at: datetime(),
  location: null,
  metadata: {}
})
CREATE (p)-[:PARTICIPATED_IN {role: 'buyer'}]->(ix)
```

Interaction nodes enable queries like "who else was at the same event" or
"which persons transacted at the same location within 24 hours" — core
contact-tracing patterns.

### Review Actions as Nodes (Migration Path)

If review action querying across cases becomes a requirement (e.g. "all
actions by reviewer X"), the `actions` list property on ReviewCase should be
migrated to `ReviewAction` nodes with `ACTION_ON` relationships. The current
list-property approach is a known trade-off for MVP simplicity:

- Neo4j list updates rewrite the entire property (not append-in-place)
- No cross-case indexing on action timestamps or actor IDs
- Unbounded growth on long-lived deferred cases

For MVP, case-scoped reads are the only access pattern and the list is
sufficient.

## Constraints and Indexes

```cypher
// Uniqueness
CREATE CONSTRAINT person_id_unique IF NOT EXISTS
  FOR (p:Person) REQUIRE p.person_id IS UNIQUE;

CREATE CONSTRAINT identifier_id_unique IF NOT EXISTS
  FOR (id:Identifier) REQUIRE id.identifier_id IS UNIQUE;

CREATE CONSTRAINT source_system_key_unique IF NOT EXISTS
  FOR (ss:SourceSystem) REQUIRE ss.source_key IS UNIQUE;

CREATE CONSTRAINT source_record_pk_unique IF NOT EXISTS
  FOR (sr:SourceRecord) REQUIRE sr.source_record_pk IS UNIQUE;

CREATE CONSTRAINT match_decision_id_unique IF NOT EXISTS
  FOR (md:MatchDecision) REQUIRE md.match_decision_id IS UNIQUE;

CREATE CONSTRAINT review_case_id_unique IF NOT EXISTS
  FOR (rc:ReviewCase) REQUIRE rc.review_case_id IS UNIQUE;

CREATE CONSTRAINT merge_event_id_unique IF NOT EXISTS
  FOR (me:MergeEvent) REQUIRE me.merge_event_id IS UNIQUE;

CREATE CONSTRAINT address_id_unique IF NOT EXISTS
  FOR (addr:Address) REQUIRE addr.address_id IS UNIQUE;

// Identifier lookups — the hot path
CREATE INDEX idx_identifier_type_norm IF NOT EXISTS
  FOR (id:Identifier)
  ON (id.identifier_type, id.normalized_value);

CREATE INDEX idx_identifier_type_hash IF NOT EXISTS
  FOR (id:Identifier)
  ON (id.identifier_type, id.hashed_value);

// Source record lookup by source-scoped ID
CREATE INDEX idx_source_record_source IF NOT EXISTS
  FOR (sr:SourceRecord)
  ON (sr.source_record_id);

// Review queue
CREATE INDEX idx_review_case_queue IF NOT EXISTS
  FOR (rc:ReviewCase)
  ON (rc.queue_state, rc.priority);

// Address lookup by postal code + street
CREATE INDEX idx_address_postal IF NOT EXISTS
  FOR (addr:Address)
  ON (addr.country_code, addr.postal_code);

CREATE INDEX idx_address_composite IF NOT EXISTS
  FOR (addr:Address)
  ON (addr.country_code, addr.postal_code, addr.street_name, addr.street_number);

// Full-text search for person name and address
CREATE FULLTEXT INDEX person_name_search IF NOT EXISTS
  FOR (p:Person)
  ON EACH [p.preferred_full_name];

CREATE FULLTEXT INDEX address_full_search IF NOT EXISTS
  FOR (addr:Address)
  ON EACH [addr.normalized_full];
```

## Example Queries

### Find a Person by Phone

```cypher
MATCH (id:Identifier {identifier_type: 'phone', normalized_value: $phone})
  <-[:IDENTIFIED_BY]-(p:Person {status: 'active'})
RETURN p.person_id, p.preferred_full_name, p.preferred_phone, p.preferred_email
```

One hop. No joins.

### Show All Source Records for a Person

```cypher
MATCH (sr:SourceRecord)-[:LINKED_TO]->(p:Person {person_id: $pid})
MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem)
RETURN sr.source_record_id, ss.source_key, sr.link_status,
       sr.observed_at, sr.ingested_at
ORDER BY sr.observed_at DESC
```

### Contact Tracing: Who Shares an Identifier With This Person?

```cypher
MATCH (p:Person {person_id: $pid})-[:IDENTIFIED_BY]->(id:Identifier)
  <-[:IDENTIFIED_BY]-(other:Person {status: 'active'})
WHERE other.person_id <> p.person_id
RETURN DISTINCT other.person_id, other.preferred_full_name,
       id.identifier_type, id.normalized_value
```

This is the query that justifies the graph model. Two hops through a shared
Identifier node — no value comparison, no cross-join, pure traversal.

### Multi-Hop Contact Tracing (N Degrees of Separation)

```cypher
MATCH path = (start:Person {person_id: $pid})
  -[:IDENTIFIED_BY]->(:Identifier)<-[:IDENTIFIED_BY]-
  (:Person)-[:IDENTIFIED_BY]->(:Identifier)<-[:IDENTIFIED_BY]-
  (end:Person {status: 'active'})
WHERE end.person_id <> start.person_id
RETURN DISTINCT end.person_id, end.preferred_full_name,
       length(path) AS hops
ORDER BY hops
LIMIT 50
```

Or using variable-length paths:

```cypher
MATCH (start:Person {person_id: $pid})
MATCH path = (start)-[:IDENTIFIED_BY*2..6]-(end:Person {status: 'active'})
WHERE end.person_id <> start.person_id
RETURN DISTINCT end.person_id, length(path) / 2 AS degrees
ORDER BY degrees
LIMIT 50
```

### Who Else Lives at This Person's Address?

```cypher
MATCH (p:Person {person_id: $pid})-[:LIVES_AT]->(addr:Address)
  <-[:LIVES_AT]-(other:Person {status: 'active'})
WHERE other.person_id <> p.person_id
RETURN DISTINCT other.person_id, other.preferred_full_name,
       addr.normalized_full, addr.postal_code
```

Same pattern as shared Identifier traversal — pure graph hop, no value
comparison. Enables household detection and same-address contact tracing.

### Find Persons by Postal Code

```cypher
MATCH (addr:Address {country_code: 'SG', postal_code: $postal})
  <-[:LIVES_AT]-(p:Person {status: 'active'})
RETURN p.person_id, p.preferred_full_name, addr.normalized_full
```

### Check for No-Match Lock Between Two Persons

```cypher
MATCH (a:Person {person_id: $left})-[lock:NO_MATCH_LOCK]-(b:Person {person_id: $right})
WHERE lock.expires_at IS NULL OR lock.expires_at > datetime()
RETURN lock IS NOT NULL AS is_locked
```

### Fetch Pending Review Cases

```cypher
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
WHERE rc.queue_state IN ['open', 'assigned']
RETURN rc.review_case_id, rc.priority, rc.sla_due_at,
       md.decision, md.confidence, md.engine_type
ORDER BY rc.priority, rc.sla_due_at, rc.created_at
```

### Resolve Canonical Person (Follow Merge Chain)

```cypher
MATCH (p:Person {person_id: $pid})
OPTIONAL MATCH (p)-[:MERGED_INTO]->(canonical:Person)
RETURN coalesce(canonical.person_id, p.person_id) AS canonical_person_id
```

Always max 1 hop due to path compression.

## Write Flows

### Ingest a New Source Record

```cypher
// Within a single transaction:
// 1. Create or find the source system
MATCH (ss:SourceSystem {source_key: $source_key})

// 2. Create source record
CREATE (sr:SourceRecord { ... })-[:FROM_SOURCE]->(ss)

// 3. After matching resolves to a person:
MATCH (p:Person {person_id: $resolved_person_id})
CREATE (sr)-[:LINKED_TO]->(p)

// 4. Create or merge Identifier nodes, create IDENTIFIED_BY relationships
MERGE (id:Identifier {identifier_type: 'phone', normalized_value: $phone})
  ON CREATE SET id.identifier_id = randomUUID(), id.created_at = datetime()
CREATE (p)-[:IDENTIFIED_BY {
  is_verified: $verified,
  is_active: true,
  quality_flag: 'valid',
  source_system_key: $source_key,
  source_record_pk: sr.source_record_pk,
  first_seen_at: datetime(),
  last_seen_at: datetime(),
  last_confirmed_at: datetime()
}]->(id)

// 5. Create or merge Address node, create LIVES_AT relationship
MERGE (addr:Address {
  country_code: 'SG',
  postal_code: '123456',
  street_name: 'example street',
  street_number: '10',
  unit_number: null
})
  ON CREATE SET addr.address_id = randomUUID(),
    addr.normalized_full = '10 example street, singapore 123456, sg',
    addr.city = 'Singapore',
    addr.created_at = datetime()
CREATE (p)-[:LIVES_AT {
  is_active: true,
  is_verified: false,
  quality_flag: 'valid',
  source_system_key: $source_key,
  source_record_pk: sr.source_record_pk,
  first_seen_at: datetime(),
  last_seen_at: datetime(),
  last_confirmed_at: datetime()
}]->(addr)

// 6. Create attribute facts as relationships (name, DOB, etc.)
CREATE (p)-[:HAS_FACT {
  attribute_name: 'full_name',
  attribute_value: 'Alice Tan',
  source_trust_tier: 'tier_2',
  confidence: 1.0,
  quality_flag: 'valid',
  observed_at: datetime(),
  created_at: datetime()
}]->(sr)

// 7. Recompute golden profile (update Person properties in place)
SET p.preferred_full_name = ...,
    p.golden_profile_computed_at = datetime(),
    p.updated_at = datetime()
```

### Merge Two Persons

```cypher
// Within a single transaction:
MATCH (absorbed:Person {person_id: $from_id})
MATCH (survivor:Person {person_id: $to_id})

// 1. Create merge event
CREATE (me:MergeEvent {
  merge_event_id: randomUUID(),
  event_type: 'auto_merge',
  actor_type: $actor_type,
  actor_id: $actor_id,
  reason: $reason,
  created_at: datetime()
})
CREATE (me)-[:ABSORBED]->(absorbed)
CREATE (me)-[:SURVIVOR]->(survivor)

// 2. Rewire source records
MATCH (sr:SourceRecord)-[old:LINKED_TO]->(absorbed)
DELETE old
CREATE (sr)-[:LINKED_TO]->(survivor)
CREATE (me)-[:AFFECTED_RECORD]->(sr)

// 3. Rewire identifier relationships
MATCH (absorbed)-[old_id:IDENTIFIED_BY]->(id:Identifier)
DELETE old_id
CREATE (survivor)-[:IDENTIFIED_BY { ... copy props ... }]->(id)

// 3b. Rewire address relationships
MATCH (absorbed)-[old_addr:LIVES_AT]->(addr:Address)
DELETE old_addr
CREATE (survivor)-[:LIVES_AT { ... copy props ... }]->(addr)

// 4. Mark absorbed
SET absorbed.status = 'merged', absorbed.updated_at = datetime()
CREATE (absorbed)-[:MERGED_INTO {
  merge_event_id: me.merge_event_id,
  actor: $actor_id,
  timestamp: datetime()
}]->(survivor)

// 5. Path compression: anyone who merged into absorbed now points to survivor
MATCH (prev:Person)-[old_merge:MERGED_INTO]->(absorbed)
DELETE old_merge
CREATE (prev)-[:MERGED_INTO {
  merge_event_id: old_merge.merge_event_id,
  actor: old_merge.actor,
  timestamp: old_merge.timestamp
}]->(survivor)

// 6. Recompute golden profile on survivor
SET survivor.preferred_full_name = ..., survivor.updated_at = datetime()
```

### Create No-Match Lock

```cypher
MATCH (a:Person {person_id: $left_id})
MATCH (b:Person {person_id: $right_id})
WHERE a.person_id < b.person_id
CREATE (a)-[:NO_MATCH_LOCK {
  lock_id: randomUUID(),
  lock_type: 'manual_no_match',
  reason: $reason,
  actor_type: $actor_type,
  actor_id: $actor_id,
  expires_at: null,
  created_at: datetime()
}]->(b)
```

## Node Count Summary

| Label | Expected per deployment | Notes |
| --- | --- | --- |
| Person | millions | core entity |
| Identifier | millions | shared across persons — the graph backbone |
| Address | hundreds of thousands–millions | shared across persons at same location |
| SourceRecord | millions | immutable, grows with ingestion |
| SourceSystem | tens | rarely changes |
| IngestRun | thousands | operational, can be pruned |
| MatchDecision | millions | immutable audit |
| ReviewCase | thousands–millions | depends on review-band volume |
| MergeEvent | thousands–millions | immutable audit |

**Not nodes** (and why):
- ~~GoldenProfile~~ → properties on Person (always 1:1, always co-fetched)
- ~~SourceFieldTrust~~ → `field_trust` map on SourceSystem (never independently queried)
- ~~PersonAttributeFact~~ → `HAS_FACT` relationship properties (always person↔source_record)
- ~~ReviewAction~~ → `actions` list on ReviewCase (only read per-case)
- ~~SurvivorshipOverride~~ → `survivorship_overrides` map on Person (per-person per-field)
- ~~PersonPairLock~~ → `NO_MATCH_LOCK` relationship (always between two entities)
- ~~PersonAlias~~ → `ALSO_KNOWN_AS` self-relationship (migration-only)
- ~~GoldenProfileLineage~~ → tracked during recomputation, not persisted as a node
- ~~CandidatePair~~ → ephemeral, handled in application memory during matching
- ~~SourceRecordRejection~~ → properties on failed SourceRecord or logged externally

## Data Integrity Notes

- `MERGED_INTO` relationships must use path compression: when B merges into C,
  all relationships pointing to B are rewired to point to C
- `Person.status = 'merged'` must always imply a `MERGED_INTO` relationship
  exists
- `NO_MATCH_LOCK` always has `left.person_id < right.person_id`
- hard uniqueness on Identifier should be conservative to avoid blocking
  legitimate shared phone and email cases
- all locks and overrides must survive reprocessing
- raw payload and decision history should never be overwritten in place
- Identifier nodes use `MERGE` (upsert) on write to prevent duplicates

## Operational Concerns

### Write Throughput

Neo4j uses a single-leader write architecture. All writes go to one instance
in a cluster; replicas serve reads only.

- expected sustained ingestion rate should be baselined in Phase 0
- bulk backfill must use batched transactions (1,000–5,000 operations per
  transaction); unbounded transactions will exhaust heap
- the ingestion concurrency model (partition by blocking key) maps well to
  Neo4j because it serializes writes to related subgraphs, reducing lock
  contention
- merge transactions are write-heavy (rewire relationships, update Person
  properties, create MergeEvent); monitor transaction duration

### Memory Sizing

Graph traversal performance depends entirely on page cache and heap:

- **page cache**: should hold the entire graph store if possible; size to
  the sum of `neostore.*.db` files. If the graph exceeds available RAM,
  traversal performance degrades sharply for cold paths.
- **heap**: size for concurrent transaction load. Start with 4–8 GB heap
  for small deployments; increase based on transaction concurrency and
  query complexity.
- monitor page cache hit ratio — below 98% indicates undersized cache

### Transaction Sizing for Backfill

Do not run bulk imports as single transactions. Batch into groups of
1,000–5,000 node/relationship creates per transaction. Use `CALL { ... }
IN TRANSACTIONS OF N ROWS` (Neo4j 5.x) for large LOAD CSV or UNWIND
operations.

### Backup and Recovery

- use `neo4j-admin database dump` for offline backups or `neo4j-admin
  database backup` (Enterprise) for online backups
- schedule automated backups at minimum daily; more frequent for production
- test point-in-time recovery before go-live
- backups should be stored off-instance and retained per the platform
  retention policy

### Hot Identifier Nodes (Supernode Problem)

Identifier nodes with high fan-out (shared business phones, generic emails)
become traversal bottlenecks and write contention points:

- cardinality caps in the application layer limit how many persons can link
  to a single Identifier
- the `shared_suspected` quality flag signals hot nodes
- for extreme cases (100+ IDENTIFIED_BY relationships), consider excluding
  the Identifier from traversal queries or marking it as `is_active = false`

### Graph Size Projections

| Entity | Est. count (1M persons) | Notes |
| --- | --- | --- |
| Person nodes | 1M | |
| Identifier nodes | 2–3M | ~2–3 identifiers per person on average |
| SourceRecord nodes | 2–5M | ~2–5 source records per person |
| IDENTIFIED_BY rels | 3–6M | multiple persons may share identifiers |
| LIVES_AT rels | 1–3M | ~1–3 addresses per person |
| LINKED_TO rels | 2–5M | 1:1 with SourceRecord |
| HAS_FACT rels | 3–10M | ~3–10 non-address attribute facts per person |
| MergeEvent nodes | 100K–500K | depends on merge rate |
| Total relationships | 15–30M | plan page cache accordingly |

At 30M relationships, the graph store will be approximately 2–5 GB on disk.
Page cache should be sized to hold this entirely in memory.

## Scaling Notes

- Neo4j causal clustering for read replicas and high availability
- hot Identifier nodes (popular phone numbers) may cause write contention;
  use application-level batching or sharding by blocking key
- full-text search for name/address via Neo4j full-text indexes or a
  dedicated search engine (Elasticsearch)
- analytical reporting should be exported to a data warehouse; do not burden
  the graph with OLAP queries
- monitor query plans with `EXPLAIN` and `PROFILE` to catch full graph scans

## Migration Strategy

1. create constraints and indexes
2. create SourceSystem nodes with field trust maps
3. create Person nodes
4. create Identifier nodes, then `IDENTIFIED_BY` relationships
5. create Address nodes, then `LIVES_AT` relationships
6. create SourceRecord nodes with `LINKED_TO` and `FROM_SOURCE` relationships
7. create `HAS_FACT` relationships for non-address attribute observations
7. create MatchDecision, ReviewCase, and MergeEvent nodes with relationships
8. create `NO_MATCH_LOCK` relationships
9. recompute golden profile properties on all active Person nodes

## Recommendation

Use this schema as the graph reference model. Keep matching logic in the
application layer. The graph should enforce identity lineage, enable
relationship traversal, and provide audit durability — not embed heuristic
business logic in procedures or triggers.
