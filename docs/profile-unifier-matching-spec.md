# Profile Unifier Matching Spec

## Purpose

Define the decision framework for identity resolution so heuristic and LLM
paths can be evaluated under the same contracts, constraints, and quality
metrics.

## Decision Philosophy

- false merges are worse than missed merges
- hard conflicts must stay outside model discretion
- auto-merge requires very high precision
- uncertain cases should go to review, not guesswork

## Matching Layers

### Layer 1: Deterministic Rules

Use for strong and trusted evidence.

Examples:

- exact verified government ID match
- trusted upstream migration-map match
- explicit manual merge override

### Layer 2: Heuristic Scoring

Use for structured probabilistic adjudication when no hard rule applies.

### Layer 3: LLM Adjudication

Use only on candidate pairs already narrowed by upstream logic. Best suited for
ambiguous cases, explanation generation, and contradiction interpretation.

### Layer 4: Human Review

Final fallback for high-risk, low-confidence, or policy-sensitive decisions.

## Candidate Generation Strategy

Never compare every record with every other record.

### Graph-Native Candidate Generation

With Neo4j, the primary candidate generation strategy is traversal through
shared Identifier nodes. When a source record is ingested, its identifiers
are matched to existing Identifier nodes. Any Person connected to the same
Identifier is a candidate:

```cypher
MATCH (id:Identifier {identifier_type: $type, normalized_value: $value})
  <-[:IDENTIFIED_BY]-(candidate:Person {status: 'active'})
RETURN candidate.person_id
```

This replaces index-based blocking-key lookups for strong identifiers.

### Strong Identifier Traversal (Primary)

- exact normalized phone → traverse shared Identifier node
- exact normalized email → traverse shared Identifier node
- exact government-ID hash → traverse shared Identifier node
- source-side known cross-reference IDs → traverse shared Identifier node

### Address Traversal

- exact normalized address → traverse shared Address node

```cypher
MATCH (addr:Address {country_code: $cc, postal_code: $postal,
  street_name: $street, street_number: $num, unit_number: $unit})
  <-[:LIVES_AT]-(candidate:Person {status: 'active'})
RETURN candidate.person_id
```

### Composite Blocking (Secondary)

For weaker signals that require combining multiple fields, fall back to
index-based queries:

- exact DOB plus fuzzy name

### Candidate Filtering

- skip `IDENTIFIED_BY` relationships where `is_active = false`
- drop placeholders and invalid values (check `quality_flag`)
- suppress cases blocked by `NO_MATCH_LOCK` relationships
- penalize identifiers with high fan-out (`shared_identifier_suspected`)

### Cardinality Caps

If an Identifier node has more active `IDENTIFIED_BY` relationships than a
configurable threshold, skip that identifier for candidate generation.
Thresholds must be configurable per identifier type. Skipped identifiers must
be logged. Default thresholds are defined in
[profile-unifier-policy-decisions.md](./profile-unifier-policy-decisions.md).

### No-Candidate Path

When candidate generation produces zero candidates for a source record, the
match engine is not invoked. A new person is created directly. No
`match_decision` row is created. A `merge_event` of type `person_created`
provides the audit trail.

## Decision States

- `merge`
- `review`
- `no_match`

Optional operational states:

- `defer`
- `escalate`
- `manual_lock`

## Hard Rules

### Hard Merge Examples

- same verified government identifier from trusted sources
- same trusted migration-map identifier
- explicit admin merge override

### Hard No-Match Examples

- conflicting government identifiers
- explicit manual no-match lock
- policy rule that forbids merge for a certain source combination

Hard rules must execute before heuristic or LLM logic.

The current proposed defaults for hard merges and hard blockers are documented
in [profile-unifier-policy-decisions.md](./profile-unifier-policy-decisions.md).

## Heuristic Feature Catalog

### Positive Evidence

- exact verified phone match
- exact verified email match
- DOB exact match
- high full-name similarity
- high address similarity
- same Address node (shared `LIVES_AT` relationship)
- repeated co-occurrence across source updates
- same trusted external ID family

### Negative Evidence

- conflicting government identifiers
- many-to-one shared phone pattern
- many-to-one shared email pattern
- conflicting DOB
- strong name mismatch
- stale low-trust source as sole evidence

### Contextual Factors

- source trust level
- verification status
- identifier recency
- whether the identifier was manually confirmed
- whether the profile already has many linked source systems

## Example Feature Vector

```json
{
  "phone_exact_match": true,
  "phone_verified_both": false,
  "email_exact_match": false,
  "dob_exact_match": true,
  "dob_conflict": false,
  "name_similarity": 0.82,
  "address_similarity": 0.35,
  "source_trust_left": 0.7,
  "source_trust_right": 0.9,
  "phone_identifier_cardinality": 2,
  "manual_no_match_lock": false,
  "government_id_conflict": false
}
```

## Example Heuristic Scoring Model

Illustrative only. Tune on labeled data. The weights below are simple additive
examples for readability. The production implementation should use conditional
weighting (unverified identifiers worth less than verified), capping (multiple
identifier matches should not stack linearly to exceed auto-merge threshold),
or a proper probabilistic framework such as Fellegi-Sunter. Simple addition of
weak signals must not produce auto-merge confidence.

### Positive Weights

- verified government ID exact match: hard merge
- exact verified phone: `+0.35`
- exact verified email: `+0.35`
- DOB exact match: `+0.25`
- high name similarity: `+0.20`
- high address similarity: `+0.10`
- trusted source bonus: `+0.05`

### Negative Weights

- phone seen on many distinct persons: `-0.25`
- generic or placeholder email: `-0.15`
- DOB conflict: `-0.30`
- strong name mismatch: `-0.25`
- stale low-trust source only: `-0.15`
- government ID conflict: hard no-match

### Decision Thresholds

- `>= 0.90`: auto-merge
- `0.60 - 0.89`: review
- `< 0.60`: no-match

## LLM Adjudication Contract

### Allowed Inputs

- normalized identifiers
- selected raw display values where useful
- feature snapshot
- source trust and verification metadata
- hard-rule outputs

### Disallowed or Restricted Inputs

- unnecessary raw sensitive identifiers
- unrestricted full raw payloads
- free-form prompts with no output schema

### Required Output

```json
{
  "decision": "review",
  "confidence": 0.74,
  "reasons": [
    "same phone",
    "same DOB",
    "name appears to be an abbreviation variant"
  ],
  "blocking_conflicts": [],
  "engine_type": "llm",
  "engine_version": "model-x_prompt-v3"
}
```

### LLM Guardrails

- cannot override hard conflict rules
- should default to `review` when evidence is mixed
- must return structured JSON only
- must log prompt and model versions
- during MVP, any LLM `merge` recommendation should still route to human review

## Benchmarking and Evaluation

### Dataset Requirements

Include:

- true matches
- true non-matches
- ambiguous pairs
- shared family phones
- shared business contact details
- name abbreviation cases
- stale or outdated emails
- conflicting source records

### Metrics

- precision
- recall
- false merge rate
- review rate
- reviewer acceptance rate
- quality by source pair
- quality by confidence band

### Acceptance Targets

- auto-merge precision above target threshold
- false merges near zero
- review volume within team capacity

### Graph Performance Targets

These must be baselined during Phase 1 and tracked continuously:

- candidate generation (traversal through shared Identifier nodes): p95 < 50ms
  for a single source record at projected graph size
- person-by-identifier lookup: p95 < 10ms
- merge transaction (rewire relationships + recompute golden profile):
  p95 < 200ms
- contact-tracing query (1-hop shared identifiers): p95 < 100ms
- multi-hop traversal (2–3 hops): p95 < 500ms at projected graph density
- ingestion throughput: sustained rate sufficient for the largest batch sync
  without exceeding Neo4j write leader capacity

## Review Queue Policy

Cases should be routed to review when:

- score falls within review band
- sensitive conflicts exist but do not trigger a hard block
- the LLM and heuristic outputs disagree materially
- the profile is linked to a high-value customer or downstream workflow

## Manual Override Policy

- manual merge should create a persistent audit event
- manual no-match should create a lock where appropriate
- overrides should be replay-safe during reprocessing

## Replay and Versioning

- every decision must store engine type and version
- heuristic rules must be versioned
- prompts and model versions must be versioned
- benchmark runs should be reproducible

## Failure Modes to Design For

- identifier reuse across family members
- recycled phone numbers
- shared business emails
- typo-heavy names
- incomplete DOB
- conflicting source trust assumptions

## Recommendation

Use deterministic plus heuristic matching as the production baseline. Add the
LLM only on narrowed candidate sets and only after benchmark instrumentation,
review operations, and privacy controls are already in place.
