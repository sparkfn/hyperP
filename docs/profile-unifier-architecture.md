# Profile Unifier Architecture

## Objective

Build a centralized identity resolution platform that ingests customer records
from multiple systems, links them to a canonical person graph, and exposes a
trusted golden profile with full auditability.

## Design Principles

- Do not use phone or email as the sole permanent master key.
- Preserve source facts and history instead of flattening immediately.
- Separate identity evidence from profile attributes.
- Make merges explainable, reversible, and reviewable.
- Optimize for very low false-merge rates.
- Treat NRIC and Singpass-linked identifiers as highly sensitive PII.

## Core Domain Model

### Person

Canonical internal entity identified by `person_id`.

### SourceRecord

Raw record from a system such as POS, Bitrix CRM, or a third-party app.

### Identifier

Typed identifier associated with a source record or person.

Examples:

- `nric_hash`
- `phone`
- `email`
- `pos_member_id`
- `bitrix_contact_id`
- `external_customer_id`

### AttributeFact

Observed attribute value with source and timestamp.

Examples:

- `full_name`
- `dob`
- `address`
- `gender`

### MatchDecision

Decision record returned by a matching engine:

- `merge`
- `review`
- `no_match`

### MergeEvent

Immutable audit record for merge, unmerge, override, or manual review action.

## Suggested Logical Schema

### person

- `person_id`
- `status`
- `created_at`
- `updated_at`

### source_record

- `source_system`
- `source_record_id`
- `person_id`
- `raw_payload`
- `record_hash`
- `ingested_at`

### identifier

- `identifier_id`
- `person_id`
- `source_system`
- `source_record_id`
- `identifier_type`
- `normalized_value`
- `hashed_value`
- `is_verified`
- `first_seen_at`
- `last_seen_at`

### attribute_fact

- `attribute_fact_id`
- `person_id`
- `attribute_name`
- `attribute_value`
- `source_system`
- `confidence`
- `observed_at`

### match_decision

- `match_decision_id`
- `candidate_a`
- `candidate_b`
- `engine_type`
- `engine_version`
- `decision`
- `confidence`
- `reasons`
- `blocking_conflicts`
- `created_at`

### merge_event

- `merge_event_id`
- `from_person_id`
- `to_person_id`
- `actor_type`
- `actor_id`
- `reason`
- `created_at`

## Normalization Layer

- Normalize phone to E.164.
- Lowercase and trim emails.
- Keep both raw and normalized names.
- Standardize DOB to ISO date.
- Break addresses into structured components where possible.
- Encrypt or hash highly sensitive identifiers.

## Pipeline

1. Ingest raw records from each upstream system.
2. Normalize identifiers and attributes.
3. Generate candidate pairs using blocking rules.
4. Extract match features for each pair.
5. Evaluate the pair through a decision engine.
6. Merge, queue for review, or reject.
7. Recompute the golden profile.
8. Persist the decision and audit trail.

## Candidate Generation

Candidate generation should reduce pair explosion before any expensive
matching step.

Recommended blocking keys:

- same normalized phone
- same normalized email
- same government identifier hash
- same DOB plus similar name
- same last name plus same postal code

## Match Engine Contract

Both heuristic and LLM paths should implement the same output contract.

```json
{
  "decision": "merge",
  "confidence": 0.93,
  "reasons": ["same verified phone", "same DOB", "high name similarity"],
  "blocking_conflicts": [],
  "engine_type": "heuristic",
  "engine_version": "v1.0.0"
}
```

## Deterministic Rules

Recommended examples:

- exact verified government ID match -> immediate merge
- conflicting government IDs -> immediate no-match
- same upstream migration map ID -> immediate merge if trusted

## Heuristic Path

The heuristic engine is the production baseline and should be used first.

### Feature Set

- exact government ID match
- conflicting government ID
- exact verified phone match
- exact verified email match
- DOB exact match
- name similarity score
- address similarity score
- source trust weight
- recency weight
- identifier uniqueness penalty

### Example Thresholds

- `>= 0.90`: auto-merge
- `0.60 - 0.89`: manual review
- `< 0.60`: no-match

These thresholds should be calibrated using labeled data.

## LLM Path

The LLM path should be used only after candidate generation and structured
feature extraction.

Recommended uses:

- adjudicate ambiguous matches
- explain edge cases for reviewers
- identify when conflicts are non-blocking but risky

Guardrails:

- never override hard conflict rules
- never auto-merge when sensitive identifiers conflict
- prefer `review` over speculative `merge`
- log model and prompt versions

## Golden Profile Rules

The golden profile should compute preferred values rather than discard
alternatives.

Suggested survivorship logic:

- verified beats unverified
- newer beats older
- trusted source beats low-trust source
- manually confirmed beats automated import

## Reviewer Workflow

Reviewers should be able to:

- compare both candidate profiles side by side
- see all linked identifiers and source records
- inspect reasons and conflicts from the engine
- merge, reject, or defer
- unmerge prior decisions

## APIs

Recommended initial APIs:

- `POST /ingest/{source}`
- `GET /persons/{person_id}`
- `GET /persons/search?identifier_type=phone&value=...`
- `GET /persons/{person_id}/source-records`
- `POST /matches/review/{match_decision_id}`
- `POST /persons/unmerge`

## Security and Compliance

- Encrypt sensitive PII at rest.
- Restrict access by role.
- Audit read and write access to sensitive identifiers.
- Minimize PII sent to any LLM path.
- Prefer tokenization or private deployment for LLM evaluation.

## Key Risks

- false merges
- reused family or business phones
- stale identifiers
- inconsistent source quality
- reviewer overload
- privacy leakage through LLM testing

## Recommendation

Ship deterministic plus heuristic matching first. Introduce the LLM in shadow
mode on ambiguous cases only after the baseline pipeline and reviewer workflow
are stable.
