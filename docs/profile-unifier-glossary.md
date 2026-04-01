# Profile Unifier Glossary

## Purpose

Define the canonical vocabulary for the profile unifier platform so product,
engineering, data operations, and reviewers use the same terms consistently.

## Core Identity Terms

### Person

The canonical internal representation of a real-world individual. A `person`
has a stable internal `person_id` and may link to multiple source records,
identifiers, and attribute facts.

### Canonical Person

Same meaning as `person`. Used when emphasis is needed that this is the
platform-owned identity entity rather than a source-system record.

### Person ID

The internal stable identifier for a canonical person. This should be the main
identity key exposed to downstream systems.

### Source Record

A raw or normalized record originating from one upstream system such as POS,
Bitrix CRM, or a third-party app. A source record is source-scoped and is not
itself the canonical identity.

### Source Record ID

The identifier assigned by the upstream system to a source record. This is only
unique within the source namespace.

### Source System

An upstream system that produces or owns customer-related records, such as POS,
Bitrix CRM, onboarding app, or third-party operational app.

### Identifier

A typed identity signal used to help recognize a person. Examples: phone,
email, government ID hash, membership ID, CRM contact ID.

### Identifier Type

The classification of an identifier, such as `phone`, `email`, or
`government_id_hash`.

### Verified Identifier

An identifier that has been confirmed by a trusted process such as OTP,
verified email flow, or KYC/onboarding verification.

### Attribute Fact

A source-observed attribute value for a person with provenance. Examples:
full name, DOB, address. Multiple conflicting facts may coexist.

### Golden Profile

The computed preferred profile for a person, derived from survivorship rules
across identifiers and attribute facts.

### Survivorship

The logic used to choose preferred values for the golden profile based on
verification, recency, source trust, and manual overrides.

## Matching Terms

### Identity Resolution

The overall process of deciding whether multiple records refer to the same
real-world person.

### Deterministic Match

A decision based on hard rules and strong evidence, such as an exact verified
government-ID hash match.

### Probabilistic Match

A decision based on weighted evidence, similarity, or model judgment when no
hard deterministic rule applies.

### Candidate Generation

The process of narrowing the search space to plausible record pairs before
running heuristic or LLM-based adjudication.

### Blocking Key

A rule used during candidate generation to group likely matches, such as exact
phone, exact email, or DOB plus fuzzy name.

### Match Engine

The adjudication component that returns `merge`, `review`, or `no_match`. It
may be deterministic, heuristic, LLM-based, or manual in origin.

### Match Decision

The persisted output of a match engine run, including decision, confidence,
reasons, conflicts, and engine versioning.

### Hard Merge

A deterministic merge allowed by policy because the evidence is strong and
trusted.

### Hard No-Match

A deterministic block that forbids merge because policy or evidence says the
entities should not be linked.

### Review Band

The confidence range where the system should not auto-merge or auto-reject and
must route the case for human review.

### False Merge

An incorrect merge where two different people were linked as the same person.
This is the highest-cost error type in the system.

### Missed Match

A case where two records belong to the same real-world person but remain
separate. This is usually less harmful than a false merge.

## Review and Operations Terms

### Review Case

An operational unit of work created when a match decision requires human
review.

### Queue State

The lifecycle state of a review case, such as `open`, `assigned`, `deferred`,
`resolved`, or `cancelled`.

### Resolution

The final outcome of a resolved review case, such as `merge`, `reject`, or
`manual_no_match`.

### Reviewer Action

An explicit operational event taken on a review case, such as assign, merge,
defer, or escalate.

### Manual No-Match

A reviewer or admin decision that rejects a candidate pair and creates a
persistent lock to prevent repeated re-suggestion.

### Person Pair Lock

A durable suppression rule between two persons or two candidate entities that
prevents a known-bad suggestion from resurfacing automatically.

### Defer

A review action that pauses a case because additional information or time is
required before final resolution.

### Escalate

A review action that routes a case to a higher-authority or more specialized
owner. Escalation is not itself a final resolution.

### Unmerge

The process of reversing a prior merge and restoring separated lineage,
identifiers, and source links.

### Merge Event

An immutable audit event that records merge-related lifecycle changes such as
person creation, manual merge, review reject, manual no-match, or unmerge.

### Merge Lineage

The chain of `MERGED_INTO` relationships between Person nodes in Neo4j that
records the full merge history. Each merge creates a relationship; unmerge
removes it. The merge chain is traversable via Cypher queries without
additional joins.

## Data Governance Terms

### Source Trust

The confidence level assigned to a source for a specific field. Trust must be
evaluated per field, not just per system.

### Trust Tier

The ranked trust category assigned to a source-field combination, such as
`tier_1` through `tier_4`.

### Sensitive Identifier

A high-risk identifier such as NRIC or Singpass-linked data that requires
strong access controls, encryption, and careful retention.

### Raw Payload

The original source-system record stored for lineage, debugging, and audit.

### Normalized Value

A transformed value used for comparison or lookup, such as phone in E.164 or
email lowercased and trimmed.

### Hashed Value

A protected representation of sensitive identity data used for matching without
storing or exposing the raw value broadly.

## Integration Terms

### Ingest Run

A batch, backfill, or sync execution grouping used to track ingestion progress
and replay behavior.

### Idempotency

The property that retrying the same write request does not create duplicate or
inconsistent side effects.

### Downstream Consumer

A service, application, or reporting flow that reads the canonical person ID or
golden profile from the profile unifier platform.

### Policy Version

The version identifier for the active rules, trust settings, and operating
constraints used when a decision was made.

### Engine Version

The version of the deterministic ruleset, heuristic scorer, or LLM prompt/model
combination used to create a match decision.

## Graph and Storage Terms

### Neo4j

The graph database used to store the person graph, relationships, and all
platform entities. Chosen for native graph traversal to support contact
tracing and complex relationship use cases.

### Node

A Neo4j graph entity. Core node labels include `Person` (with golden profile
inline), `Identifier` (shared across persons), `SourceRecord`, `MatchDecision`,
`MergeEvent`, `ReviewCase`, `SourceSystem`, and `IngestRun`. Many relational
concepts are modeled as relationships rather than separate nodes.

### Relationship

A typed, directed connection between two nodes in Neo4j. Relationships are
first-class citizens with their own properties. Examples: `IDENTIFIED_BY`,
`LINKED_TO`, `MERGED_INTO`, `NO_MATCH_LOCK`, `HAS_FACT`, `FOR_DECISION`.

### Cypher

Neo4j's declarative graph query language used for all reads and writes.

### Contact Tracing

A multi-hop graph traversal use case that identifies people connected through
shared identifiers, interactions, or relationships. Enabled by Neo4j's native
graph storage and traversal engine.

### Explicit Relationship

A typed, directed connection between two Person nodes that represents a
semantic relationship (e.g. `REFERRED_BY`, `WORKS_WITH`, `FAMILY_OF`). Unlike
implicit connections through shared Identifier nodes, explicit relationships
are declared by source systems or created manually. They do not affect identity
resolution decisions. Planned for post-MVP.

### Interaction

An event node (post-MVP) that connects one or more persons to a time, place,
or activity. Examples: transactions, appointments, service calls. Used for
contact tracing and sales analytics.

### Supernode

An Identifier node with an unusually high number of `IDENTIFIED_BY`
relationships (e.g. a shared business phone number). Supernodes can cause
traversal bottlenecks and write contention. Managed through cardinality caps
and quality flags.

## Recommended Usage Rules

- Use `person` when referring to the canonical platform entity.
- Use `source record` when referring to an upstream row or object.
- Use `identifier` for identity evidence and `attribute fact` for profile data.
- Use `review case` for operational workflow, not `ticket` or `task`, unless a
  separate ticketing system is explicitly involved.
- Use `manual no-match` when a persistent suppression lock is created.
- Use `reject` when the case is resolved without creating a persistent lock.
