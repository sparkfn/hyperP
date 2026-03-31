# Profile Unifier Policy Decisions

## Purpose

Document the proposed default operating policies that close the main
implementation gaps in the architecture and PRD. These defaults should be used
unless business, legal, or security review explicitly replaces them.

## Decision Status

All decisions in this document are `proposed defaults` for MVP and phase-1
delivery.

## Policy 1: Source Trust Ranking by Field

Trust should be evaluated per field, not per system globally. A source may be
high trust for one field and weak for another.

## Trust Levels

- `tier_1`: highly trusted
- `tier_2`: trusted with validation caveats
- `tier_3`: usable but weak
- `tier_4`: low trust, advisory only

## Recommended Source Categories

### KYC or Onboarding System

Examples:

- Singpass-backed onboarding flow
- identity verification workflow
- customer onboarding app with OTP and document checks

Typical trust:

- government ID: `tier_1`
- DOB: `tier_1`
- legal name: `tier_1`
- phone: `tier_1` if OTP verified, otherwise `tier_2`
- email: `tier_2` if verified, otherwise `tier_3`
- address: `tier_2`

### POS System

Examples:

- loyalty signup at cashier
- retail membership capture

Typical trust:

- membership ID: `tier_1` within POS namespace
- phone: `tier_2` if OTP verified, otherwise `tier_3`
- email: `tier_3`
- name: `tier_3`
- address: `tier_4`
- DOB: `tier_4` unless collected through verified enrollment

### CRM System Such as Bitrix

Examples:

- sales-entered contact records
- support-maintained contact records

Typical trust:

- CRM contact ID: `tier_1` within CRM namespace only
- phone: `tier_3`
- email: `tier_3`
- preferred name: `tier_3`
- legal name: `tier_4`
- DOB: `tier_4`
- address: `tier_4`

### Third-Party Operational App

Examples:

- booking platform
- service app
- custom form app

Typical trust:

- external customer ID: `tier_1` within that source namespace
- phone: `tier_3` unless verified
- email: `tier_3` unless verified
- name: `tier_3`
- DOB: `tier_4`
- address: `tier_4`

## Recommended Field-Level Trust Matrix

| Field | Tier 1 | Tier 2 | Tier 3 | Tier 4 |
| --- | --- | --- | --- | --- |
| Government ID | Singpass or KYC verified | Manual ops confirmation | Not applicable | Not applicable |
| Legal Name | KYC verified source | Manual verified onboarding | POS or CRM free text | Third-party uncontrolled input |
| Preferred Name | Manually confirmed by staff or customer | Recent customer-managed app | CRM or POS free text | Imported unknown |
| DOB | KYC verified source | Manual verified onboarding | Source-entered text | Unknown or unverified |
| Phone | OTP verified source | Recent customer-confirmed source | CRM or POS unverified | Shared or low-quality imports |
| Email | Verified email flow | Recent customer-confirmed source | CRM or POS unverified | Placeholder, role inbox, or stale |
| Address | Verified fulfillment or KYC source | Recent successful delivery address | CRM entered text | Incomplete or malformed |
| External ID | System-owned namespace key | Migrated ID with validation | Legacy import only | Duplicated or unstable source ID |

## Engineering Rule

- source trust must be configurable in code or admin config
- trust must be applied at field level during survivorship
- trust must also affect probabilistic scoring

## Policy 2: Exact Hard-Blocker and Hard-Merge Identifiers

Hard rules must be explicit, sparse, and auditable. They should be biased
toward safety.

## Hard Merge Rules

The following should qualify for deterministic auto-merge:

1. exact match on verified government identifier hash from trusted sources
2. trusted migration map showing old record A and new record B belong to the same person
3. exact source-owned namespace key when the source has already internally deduplicated the person and the import is an update to an existing linked record
4. explicit manual merge approved by authorized reviewer or admin

## Hard No-Match Rules

The following should block merge immediately:

1. conflicting verified government identifiers
2. manual no-match lock between the same source record and person, or between two persons
3. one profile marked as fraud, test data, or suppressed and policy forbids merge
4. policy rule stating a certain source namespace cannot auto-merge without review

## Strong Review Triggers

These are not hard blockers, but they must route to review:

1. same phone but different emails and weak name similarity
2. same email but different phones and DOB conflict
3. high similarity with shared-family or shared-business identifier patterns
4. LLM and heuristic disagreement across decision classes
5. any candidate involving a high-value or high-risk profile

## Fields That Must Never Be Sole Auto-Merge Keys

- phone
- email
- name
- address
- DOB

These may contribute to scoring, but they must not independently trigger
deterministic merge in the default policy.

## Policy 3: Retention Rules

Retention should distinguish between raw payloads, normalized evidence, and
decision history.

## Recommended Retention Schedule

### Raw Source Payloads

- retain full raw payload for `12 months`
- archive or purge after 12 months unless required for audit or legal hold
- for highly sensitive sources, retain only required fields if full raw payload is not operationally necessary

### Normalized Identifiers and Attribute Facts

- retain while the person is active plus `24 months`
- if a person is deleted under policy, apply deletion or anonymization to linked normalized data where legally required

### Match Decisions and Merge Events

- retain for `7 years`
- these records are critical for explaining how identity decisions were made

### Model Inputs and Feature Snapshots

- retain heuristic feature snapshots for `24 months`
- retain LLM prompt payloads in redacted form for `12 months`
- never retain unnecessary raw sensitive identifiers in model logs

### Review Audit Logs

- retain for `7 years`

## Retention Operating Rules

- retention periods must be configurable
- legal hold must override scheduled deletion
- deletion jobs must be auditable
- analytics should use anonymized or aggregated data where possible

## Policy 4: LLM Operating Boundary

For MVP and the first production phases, the LLM should remain `assist-only`
and `review-band only`.

## Default LLM Usage Policy

- allowed to evaluate ambiguous candidate pairs after candidate generation
- allowed to produce structured `merge`, `review`, or `no_match` recommendations
- allowed to generate reviewer summaries
- not allowed to search the full corpus for matches
- not allowed to override hard conflict rules
- not allowed to auto-merge in production during MVP

## Promotion Policy

Before any move beyond assist-only, all of the following must be true:

1. benchmark quality is stable across multiple evaluation periods
2. false-merge rate in shadow mode remains within target
3. privacy and legal review explicitly approve the expanded use
4. rollback and kill-switch controls are tested
5. reviewers and product owner sign off on measured value

## Recommended Long-Term Policy

Even after maturity, the LLM should only be considered for:

- review prioritization
- explanation generation
- adjudication of narrow ambiguous bands

It should still not own unrestricted autonomous merging.

## Policy 5: Manual Locks and Overrides

Manual actions must survive reprocessing.

### Manual Merge

- allowed for authorized users only
- must record actor, timestamp, reason, and affected persons

### Manual No-Match Lock

- allowed for authorized users only
- should suppress future repeated suggestions for the same pair
- must support expiration if business wants periodic reevaluation

### Unmerge

- must preserve original audit lineage
- must trigger recomputation of golden profiles
- must notify dependent systems if relevant

## Policy 6: Initial Default Threshold Policy

Use conservative thresholds until benchmark evidence justifies change.

- deterministic hard rules first
- heuristic auto-merge threshold: `0.90`
- heuristic review band: `0.60 - 0.89`
- heuristic no-match threshold: `< 0.60`
- LLM can recommend any state, but production system should treat LLM `merge`
  as `review` during MVP

## Policy 7: Source-Onboarding Criteria

Do not onboard a new source into auto-merge flows until the following are
defined:

1. source schema and identifier mapping
2. field-level trust ranking
3. known bad-data patterns
4. retention classification
5. benchmark samples including true matches and true non-matches

## Policy 8: Candidate Generation Cardinality Caps

If a blocking key matches more than a configurable threshold of existing
persons, that key must be skipped for that record. The system should fall back
to other blocking keys. Skipped keys must be logged for observability.

### Recommended Default Thresholds

These are starting values and must be tuned per deployment:

- phone: 50 persons
- email: 100 persons
- government ID hash: 5 persons
- DOB plus name: 200 persons

## Policy 9: Identifier Aging

Identifiers that have not been re-confirmed by any source within a configurable
window should be deactivated. A background job periodically marks stale
identifiers as `is_active = false`. Identifiers reactivate automatically if a
fresh source record re-confirms them.

Deactivated identifiers remain in the database for audit but stop participating
in candidate generation and scoring.

Aging windows must be configurable per identifier type. Government ID hashes
should not be subject to time-based aging.

## Policy 10: Batch Reprocessing

When thresholds are tuned or scoring logic changes, historical decisions may
need re-evaluation. Reprocessing must respect prior human decisions:

- skip any pair with an existing resolved review case or active lock
- new candidates that were never previously evaluated go through the normal
  pipeline
- previously auto-merged pairs that now fall below threshold are flagged for
  review, not auto-unmerged
- every reprocessing run must be tracked for observability and rollback

## Policy 11: Search API Protection

The search API must enforce:

- rate limiting per caller role, with stricter limits for `support_agent`
- minimum query length for free-text search
- exact-match-only mode for government ID lookups
- all search queries logged with caller identity for audit

## Final Recommendation

Adopt these defaults now so implementation can proceed without waiting on
further ambiguity. If business policy later changes, update this document and
version the affected ruleset and operating procedures.
