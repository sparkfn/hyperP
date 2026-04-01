# Profile Unifier Architecture

## Objective

Build a centralized identity resolution and relationship intelligence platform
that ingests customer records from multiple systems, resolves them into
canonical persons, and exposes a trusted golden profile with full
explainability, auditability, and rollback. The graph-native storage (Neo4j)
enables complex relationship use cases such as contact tracing and multi-hop
network analysis. The initial use case is sales.

Shared terminology used across the document set is defined in
[profile-unifier-glossary.md](./profile-unifier-glossary.md).

## Goals

- unify fragmented customer records across POS, Bitrix CRM, and third-party apps
- support deterministic and probabilistic matching paths
- maintain low false-merge rates
- preserve source lineage for every identifier and attribute
- support human review and unmerge
- provide a stable profile API for downstream systems
- enable complex relationship queries such as contact tracing and referral networks

## Non-Goals

- replace all source CRMs in phase 1
- write back every profile update to every source system
- solve householding or family graph resolution in MVP (planned for later phases)
- perform unrestricted autonomous LLM-based merging

## Key Principles

- Do not use phone or email as the sole permanent master key.
- Preserve source facts and history instead of flattening immediately.
- Separate identity evidence from profile attributes.
- Make merges explainable, reversible, and reviewable.
- Optimize for very low false-merge rates.
- Treat NRIC and Singpass-linked identifiers as highly sensitive PII.

## System Context

Upstream systems publish or expose customer data through:

- batch exports
- polling APIs
- webhooks
- CDC streams where available

The platform ingests those records, normalizes them, resolves identity, stores
the person graph, and exposes:

- search APIs
- person profile APIs
- reviewer workflow APIs
- audit and analytics outputs

## High-Level Components

### Ingestion Connectors

Responsible for source-specific extraction and delivery into a common raw
record contract.

Expected modes:

- scheduled batch sync
- near-real-time webhook ingestion
- historical backfill jobs

### Normalization Service

Responsible for:

- field mapping by source
- identifier normalization
- attribute cleaning
- validation and rejection handling
- source-specific quality tagging

### Candidate Generation Service

Responsible for creating a manageable set of plausible record pairs or
record-to-person comparisons before any probabilistic evaluation.

### Match Engine

Supports multiple adjudication strategies behind a single interface:

- deterministic rules
- heuristic scoring
- LLM-assisted adjudication

### Person Graph Store (Neo4j)

Stores canonical persons, linked identifiers, source records, facts, and merge
history as a native property graph. Neo4j's Cypher query language enables
efficient multi-hop traversals for contact tracing and relationship analysis
beyond what identity resolution alone requires.

### Golden Profile Service

Computes field-level preferred values from the available facts and trust rules.

### Review Operations Service

Presents ambiguous cases to reviewers and records overrides, rejects, and
unmerge actions.

### API Layer

Provides read and workflow APIs to downstream systems and internal tools.

## End-to-End Data Flow

1. Extract source records from each upstream system.
2. Persist raw payload with idempotency keys.
3. Normalize identifiers and attributes.
4. Generate candidate pairs using blocking keys.
5. Evaluate deterministic hard rules.
6. Evaluate remaining cases through heuristic or LLM path.
7. Produce a `merge`, `review`, or `no_match` decision.
8. Update person links, facts, and audit trail.
9. Recompute golden profile.
10. Expose the result through APIs and reviewer tools.

## Source Data Contract

Every ingested source record should be translated into a common envelope:

```json
{
  "source_system": "bitrix",
  "source_record_id": "12345",
  "ingest_type": "batch",
  "observed_at": "2026-03-31T00:00:00Z",
  "record_hash": "sha256:...",
  "identifiers": [
    { "type": "phone", "value": "+6591234567", "is_verified": false },
    { "type": "email", "value": "alice@example.com", "is_verified": false }
  ],
  "attributes": {
    "full_name": "Alice Tan",
    "dob": "1989-10-01",
    "address": "10 Example Street"
  },
  "raw_payload": {}
}
```

## Domain Model

### Person

Canonical internal entity identified by `person_id`.

Suggested fields:

- `person_id`
- `status`
- `created_at`
- `updated_at`
- `manual_lock_state`

### Merge Lineage

Merge lineage is stored as native graph relationships in Neo4j. Each merge
creates a `MERGED_INTO` relationship between the absorbed Person node and the
surviving Person node, with properties for `merge_event_id`, `actor`, and
`timestamp`. The full merge chain is readable by traversing `MERGED_INTO`
relationships. The `MergeEvent` node remains the authoritative audit record.
The relationship chain can be rebuilt from `MergeEvent` nodes if corrupted.

Path compression is enforced on `MERGED_INTO` relationships: when B merges into
C, all relationships pointing to B are rewired to point to C. This guarantees
max depth of one hop for canonical person lookups.

Recommended statuses:

- `active`
- `merged`
- `suppressed`

### SourceRecord

Immutable representation of an upstream record.

Suggested fields:

- `source_system`
- `source_record_id`
- `source_record_version`
- `person_id`
- `ingest_run_id`
- `raw_payload`
- `record_hash`
- `ingested_at`

### PersonIdentifier

Typed identity evidence associated with a person and source record.

Examples:

- `nric_hash`
- `phone`
- `email`
- `pos_member_id`
- `bitrix_contact_id`
- `external_customer_id`

Suggested fields:

- `identifier_id`
- `person_id`
- `source_system`
- `source_record_id`
- `identifier_type`
- `raw_value`
- `normalized_value`
- `hashed_value`
- `is_verified`
- `verification_method`
- `is_active`
- `first_seen_at`
- `last_seen_at`

### PersonAttributeFact

Observed profile field with source and provenance.

Suggested fields:

- `attribute_fact_id`
- `person_id`
- `source_system`
- `source_record_id`
- `attribute_name`
- `attribute_value`
- `confidence`
- `observed_at`
- `is_current_hint`

### MatchDecision

Decision record returned by the engine.

Suggested fields:

- `match_decision_id`
- `left_entity_type`
- `left_entity_id`
- `right_entity_type`
- `right_entity_id`
- `engine_type`
- `engine_version`
- `decision`
- `confidence`
- `reasons`
- `blocking_conflicts`
- `feature_snapshot`
- `created_at`

### MergeEvent

Immutable audit record for lifecycle changes.

Suggested event types:

- `auto_merge`
- `manual_merge`
- `review_reject`
- `manual_no_match`
- `unmerge`
- `person_split`
- `survivorship_override`

## Logical Data Model

### person

- `person_id`
- `status`
- `primary_source`
- `created_at`
- `updated_at`

Merge history is represented via `MERGED_INTO` relationships between Person
nodes rather than self-referential properties.

### source_record

- `source_system`
- `source_record_id`
- `source_record_version`
- `person_id`
- `raw_payload`
- `record_hash`
- `ingested_at`

### person_identifier

- `person_identifier_id`
- `person_id`
- `source_system`
- `source_record_id`
- `identifier_type`
- `raw_value`
- `normalized_value`
- `hashed_value`
- `is_verified`
- `verification_method`
- `is_active`
- `first_seen_at`
- `last_seen_at`
- `last_confirmed_at`

### person_attribute_fact

- `person_attribute_fact_id`
- `person_id`
- `attribute_name`
- `attribute_value`
- `source_system`
- `source_record_id`
- `confidence`
- `observed_at`
- `is_current_hint`

Golden profile fields are stored directly on the Person node in Neo4j (always
1:1, always co-fetched):

- `preferred_full_name`
- `preferred_phone`
- `preferred_email`
- `preferred_address`
- `preferred_dob`
- `profile_completeness_score`
- `golden_profile_computed_at`

### match_decision

- `match_decision_id`
- `left_entity_type`
- `left_entity_id`
- `right_entity_type`
- `right_entity_id`
- `engine_type`
- `engine_version`
- `decision`
- `confidence`
- `reasons`
- `blocking_conflicts`
- `feature_snapshot`
- `created_at`

### merge_event

- `merge_event_id`
- `event_type`
- `from_person_id`
- `to_person_id`
- `actor_type`
- `actor_id`
- `reason`
- `metadata`
- `created_at`

### review_case

- `review_case_id`
- `match_decision_id`
- `priority`
- `queue_state`
- `assigned_to`
- `sla_due_at`
- `created_at`
- `updated_at`

## Identity and Record Lifecycle

### Source Record Lifecycle

1. Raw record arrives.
2. Record hash is checked for idempotency.
3. Record is normalized.
4. Candidate matches are generated.
5. A decision is produced.
6. The source record is linked to a person or held pending review.

### Person Lifecycle

1. `active`: current canonical entity.
2. `merged`: absorbed into another person.
3. `suppressed`: hidden from normal search if bad or test data.

### Unmerge Workflow

1. Reviewer or admin marks a bad merge.
2. Merge history and source links are replayed.
3. A new or restored person entity is created.
4. Source records that arrived after the original merge and are still linked to
   the surviving person stay in place but are flagged for review. Their match
   confidence may have changed without the unmerged person's signals.
5. Golden profiles are recomputed for affected persons.
6. Audit events and downstream notifications are emitted.

## Normalization Layer

### Identifier Normalization

- Normalize phone to E.164.
- Lowercase and trim emails.
- Preserve both raw and normalized forms.
- Normalize name punctuation and spacing while retaining original text.
- Standardize DOB to ISO date.
- Encrypt or hash highly sensitive identifiers.

### Attribute Normalization

- split full addresses into components where possible
- standardize country codes and postal codes
- normalize honorifics and common placeholders
- detect null-like values such as `NA`, `Unknown`, or `-`

### Data Quality Flags

Each normalized field should carry quality indicators:

- `valid`
- `invalid_format`
- `placeholder_value`
- `shared_identifier_suspected`
- `stale`
- `source_untrusted`

## Candidate Generation

Candidate generation must prevent full pairwise comparison. With Neo4j, the
primary strategy is graph traversal through shared Identifier nodes rather than
index-based blocking-key lookups.

### Graph-Native Candidate Generation

When a new source record is ingested, its identifiers are normalized and
matched to existing Identifier nodes. Candidate persons are found by
traversing from those Identifier nodes:

```cypher
// For each identifier on the incoming record:
MATCH (id:Identifier {identifier_type: $type, normalized_value: $value})
  <-[:IDENTIFIED_BY]-(candidate:Person {status: 'active'})
RETURN candidate.person_id
```

This replaces traditional blocking-key index scans. The Identifier node is
the blocking key — if two persons share an Identifier node, they are
candidates. No value comparison is needed; the graph edge is the evidence.

### Composite Blocking (Multiple Signals)

For weaker signals that require combination (e.g. DOB plus similar name),
the system falls back to index-based lookups since these are not modeled as
shared nodes:

- same DOB plus similar name (full-text index on Person.preferred_full_name)
- same last name plus same postal code

### Candidate Suppression Rules

- ignore placeholders and invalid identifiers (check `quality_flag` on the
  `IDENTIFIED_BY` relationship)
- down-rank identifiers known to be shared (check `shared_identifier_suspected`
  quality flag)
- suppress comparison if a `NO_MATCH_LOCK` relationship exists between the
  candidate and any person the source record is already linked to
- skip `IDENTIFIED_BY` relationships where `is_active = false`

### Cardinality Caps

If an Identifier node has more `IDENTIFIED_BY` relationships than a
configurable threshold, skip that identifier for candidate generation. This
prevents explosion from shared business phones or generic emails. The threshold
must be configurable per identifier type. Skipped identifiers should be logged
for observability as they flag data quality issues.

Check with:

```cypher
MATCH (id:Identifier {identifier_type: $type, normalized_value: $value})
WITH id, size([(id)<-[:IDENTIFIED_BY]-(:Person {status: 'active'}) | 1]) AS fan_out
WHERE fan_out <= $cap
```

### Performance Notes

- composite indexes on `Identifier(identifier_type, normalized_value)` are
  the hot path — these must be in page cache
- partition candidate generation by blocking key for write concurrency
- support backfill and incremental modes separately
- use `PROFILE` to verify candidate queries use index lookups, not label scans

## Ingestion Concurrency Model

Ingestion should be partitioned by the primary blocking key so that records that
could potentially match always land on the same worker. Records with unrelated
blocking keys can be processed concurrently. This prevents race conditions where
two concurrent ingestions both create a new person for what should be the same
individual.

When a record matches on multiple blocking keys pointing to different partitions,
a tie-breaking rule must determine which partition owns evaluation.

## Match Engine Contract

All decision engines should implement the same interface.

### Request Contract

```json
{
  "left_entity": {
    "entity_type": "source_record",
    "entity_id": "bitrix:12345"
  },
  "right_entity": {
    "entity_type": "person",
    "entity_id": "person_001"
  },
  "features": {
    "phone_exact_match": true,
    "email_exact_match": false,
    "dob_exact_match": true,
    "name_similarity": 0.82
  },
  "hard_constraints": {
    "conflicting_government_id": false,
    "manual_no_match_lock": false
  }
}
```

### Response Contract

```json
{
  "decision": "review",
  "confidence": 0.78,
  "reasons": ["same phone", "same DOB", "highly similar name"],
  "blocking_conflicts": [],
  "engine_type": "heuristic",
  "engine_version": "v1.0.0"
}
```

## New Person Creation

When candidate generation returns zero candidates for a source record, the
match engine is not invoked. A new person is created directly and the source
record is linked to it. No `match_decision` row is created. A `merge_event` of
type `person_created` provides the audit trail.

## Deterministic Rules

Examples of hard rules:

- exact verified government ID match -> immediate merge
- conflicting government IDs -> immediate no-match
- exact trusted migration-map ID -> immediate merge
- explicit manual no-match lock -> immediate no-match

Hard rules should execute before heuristic or LLM adjudication.

## Heuristic Path

The heuristic engine is the production baseline.

### Feature Families

- identity exact matches
- identity conflicts
- name similarity
- DOB similarity
- address similarity
- source trust
- recency
- uniqueness penalties
- historical merge signals

### Confidence Bands

- `>= 0.90`: auto-merge
- `0.60 - 0.89`: review
- `< 0.60`: no-match

These are starting thresholds and must be calibrated from labeled cases.

## LLM Path

The LLM path should be limited to structured adjudication after feature
extraction.

Recommended uses:

- review-band adjudication support
- reviewer explanation generation
- contradiction interpretation for ambiguous pairs

Guardrails:

- never override hard conflict rules
- never auto-merge on conflicting sensitive identifiers
- prefer `review` over speculative `merge`
- record model, prompt, and policy versions
- minimize raw PII exposure

## Golden Profile Computation

Golden profile recomputation is synchronous within the merge or review
transaction. This ensures downstream consumers never read a stale golden profile
after a merge. The recomputation logic should be encapsulated in a standalone
function so it can be extracted into an async worker later if scale demands it.

The golden profile should compute preferred values without discarding source
facts.

### Survivorship Rules

- verified beats unverified
- newer beats older
- trusted source beats low-trust source
- manually confirmed beats automated import
- non-placeholder beats placeholder

### Field Strategy

- name: prefer manually confirmed legal or preferred name, otherwise latest
  high-trust source
- phone: allow multiple active phones, but choose one preferred contact number
- email: allow multiple active emails, but choose one preferred primary email
- address: prefer most recent verified address
- DOB: prefer verified onboarding or KYC source

## Reviewer Workflow

Reviewers should be able to:

- compare candidate profiles side by side
- see all linked identifiers and source records
- inspect reasons, confidence, and conflicts
- merge, reject, defer, or escalate
- apply manual locks
- unmerge prior decisions

### Review Queue Prioritization

Priority should consider:

- downstream business impact
- confidence closeness to threshold
- presence of sensitive conflicts
- customer-service recency

## Identifier Aging

Identifiers that have not been re-confirmed by any source within a configurable
window should be marked `is_active = false` by a background job.
`last_confirmed_at` on `person_identifier` tracks the most recent confirmation.
Deactivated identifiers remain in the table for audit but stop participating in
candidate generation and scoring. If a fresh source record later re-confirms the
identifier, it is reactivated automatically. The aging window must be
configurable per identifier type.

## Pair Ordering Convention

Lock relationships (`NO_MATCH_LOCK`) between Person nodes must enforce a
canonical ordering where the left person ID is lexicographically less than the
right person ID. This eliminates duplicate locks and ensures checks only need
to query one direction.

## API Surface

Recommended initial APIs:

- `POST /ingest/{source}`
- `GET /persons/{person_id}`
- `GET /persons/search`
- `GET /persons/{person_id}/source-records`
- `GET /persons/{person_id}/connections` (shared-identifier graph traversal)
- `GET /persons/{person_id}/relationships` (explicit typed relationships, post-MVP)
- `GET /persons/{person_id}/audit`
- `POST /matches/review/{match_decision_id}`
- `POST /persons/manual-merge`
- `POST /persons/unmerge`
- `GET /events`

### Search API Requirements

- search by phone, email, source ID, or government-ID token
- partial match support where allowed
- exact-match mode for sensitive identifiers
- access-controlled filtering by role
- rate limiting per caller role
- minimum query length for free-text search
- all search queries logged with caller identity for audit

### Downstream Event Polling

Downstream consumers that need to react to identity changes should poll
`GET /events?since=<timestamp>` with cursor-based pagination. The event schema
should include event type, affected entity IDs, timestamp, and metadata. This
avoids the need for webhook or message queue infrastructure during early phases.
The event schema should be designed so it can be migrated to a push-based
delivery mechanism later.

## Security and Compliance

### Data Handling

- encrypt sensitive PII at rest
- use field-level protection for NRIC-like identifiers
- avoid broad exposure of raw sensitive IDs
- tokenize or hash identifiers used for matching where possible

### Access Control

- define separate roles for admins, reviewers, support agents, and service clients
- restrict unmerge and manual merge to elevated roles
- restrict access to raw payloads and sensitive identifiers

### Audit

- log read access to sensitive fields
- log all merge, no-match, review, override, and unmerge actions
- retain engine inputs and outputs for post-incident analysis

## Observability

### Application Metrics

Track:

- ingestion success and failure rates
- normalization rejection rates
- candidate generation volume
- auto-merge rate
- false-merge incidents
- review backlog and SLA
- source drift indicators
- golden profile recomputation failures

### Graph Metrics

Track:

- total node and relationship counts by label/type (growth over time)
- query latency percentiles (p50, p95, p99) for key patterns: person lookup,
  candidate generation traversal, contact-tracing queries
- Neo4j page cache hit ratio (target ≥ 98%)
- transaction throughput (transactions/sec) and average duration
- hot Identifier node detection: fan-out distribution, nodes exceeding
  cardinality cap thresholds
- write contention: lock wait times, deadlock frequency
- heap usage and garbage collection pressure

## Failure and Recovery

- ingestion must be idempotent
- merge decisions must be replayable
- unmerge must be supported without manual graph surgery
- failed downstream writes must not corrupt canonical state

## Explicit Relationship Model (Post-MVP)

Beyond implicit connections through shared Identifier nodes, the platform must
support explicit, typed relationships between persons. These are not identity
links — they are semantic connections discovered or declared by the business.

### Planned Relationship Types

- `REFERRED_BY` — one person referred another (sales, loyalty programs)
- `WORKS_WITH` — shared employer or business context
- `FAMILY_OF` — declared or inferred household/family connection
- `SAME_HOUSEHOLD` — shared address with corroborating signals
- `SAME_ACCOUNT` — linked under a shared business or service account

### Design

Explicit relationships are modeled as typed Neo4j relationships between Person
nodes with properties for source, confidence, and provenance:

```cypher
CREATE (a:Person)-[:REFERRED_BY {
  source_system_key: 'loyalty_app',
  confidence: 1.0,
  declared_by: 'customer',
  created_at: datetime()
}]->(b:Person)
```

### Rules

- explicit relationships must never be auto-created by the matching engine;
  they require either a source-system declaration or a manual action
- they do not affect identity resolution decisions (merge/no-match)
- they participate in graph traversal queries (contact tracing, network views)
- they must carry provenance (who declared it, when, from which source)

### MVP Scope

In MVP, the only person-to-person connection is implicit — through shared
Identifier nodes. Explicit relationship types are deferred to post-MVP but
the graph schema is designed to accommodate them without migration.

## Interaction and Touchpoint Model (Post-MVP)

For sales, contact tracing, and relationship analysis, the platform must
eventually capture interactions — events that connect a person to a time,
place, or another person. Examples: transactions, appointments, service calls,
co-attendance.

### Design Direction

Interactions will be modeled as nodes (not relationship properties) because
they connect multiple entities (person, location, time, other persons) and
need independent querying:

```
(Person)-[:PARTICIPATED_IN]->(Interaction)<-[:PARTICIPATED_IN]-(Person)
(Interaction)-[:AT_LOCATION]->(Location)
```

### MVP Scope

No interaction model in MVP. The current SourceRecord already captures "this
person did something in this system at this time" — enough for basic sales
timeline views. Dedicated interaction nodes are planned for the contact-tracing
phase.

### Design Constraint

The interaction model must not be bolted on later as a separate system. It
must connect to the same Person nodes in the same Neo4j graph so that
traversal queries can cross identity, relationship, and interaction data in
a single query.

## Resolved Technical Decisions

- graph database: Neo4j (supports contact tracing, multi-hop relationship queries, and complex graph use cases beyond simple identity resolution)
- source records map to persons via `LINKED_TO` relationships in Neo4j
- merge history uses path compression on `MERGED_INTO` relationships for max 1-hop canonical lookups
- golden profile recomputation is synchronous within the merge transaction (Neo4j ACID transactions)
- ingestion concurrency is partitioned by blocking key

## Open Technical Decisions

- whether to use event sourcing for merge replay
- whether review tooling is embedded in the app or built as an internal console
- deployment topology: single shared Neo4j graph for all business units vs.
  separate graph databases per tenant. Single graph enables cross-tenant
  relationship queries but complicates access control and data isolation.
  Separate graphs simplify compliance but prevent cross-tenant contact tracing.
  Decision depends on whether contact tracing and relationship queries need to
  span business boundaries.
- whether Interaction nodes (post-MVP) live in the same Neo4j instance or in a
  separate time-series store with graph references

## Default Policy Decisions

The current proposed defaults for unresolved product and governance questions
are documented in [profile-unifier-policy-decisions.md](./profile-unifier-policy-decisions.md).

These defaults currently recommend:

- field-level source trust ranking instead of source-wide trust
- deterministic hard merges only for verified government ID, trusted migration
  maps, and explicit manual merge
- hard no-match on conflicting verified government IDs and manual no-match locks
- 12-month raw payload retention, longer retention for decision history
- LLM assist-only operation during MVP and early production phases

## Reference Implementation

A concrete Neo4j graph schema for the architecture in this document is defined
in [profile-unifier-graph-schema.md](./profile-unifier-graph-schema.md).

The corresponding service contract for ingestion, search, person reads, review
workflow, and merge operations is defined in
[profile-unifier-api-spec.md](./profile-unifier-api-spec.md).

A machine-readable OpenAPI 3.1 version of the main service contract is defined
in [profile-unifier-openapi-3.1.yaml](./profile-unifier-openapi-3.1.yaml).

The operational state machine for review handling, merge rejection, locks, and
unmerge escalation is defined in
[profile-unifier-reviewer-workflow.md](./profile-unifier-reviewer-workflow.md).

Main runtime flows are illustrated in
[profile-unifier-sequence-diagrams.md](./profile-unifier-sequence-diagrams.md).

## Recommendation

Ship deterministic plus heuristic matching first. Introduce the LLM in shadow
mode on ambiguous cases only after the baseline pipeline, review workflow, and
audit controls are stable.
