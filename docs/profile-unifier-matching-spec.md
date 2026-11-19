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

**Source-record-type gate**: Layer 1 only fires when *both* sides of the
candidate pair have evidence sourced from `record_type = 'system'` source
records. If either side's supporting evidence comes from a
`record_type = 'conversation'` source record, the pair skips Layer 1 and is
handed directly to Layer 2 (heuristic scoring). Conversation-extracted
identifiers — even if they look like a verified government ID — are never
sufficient on their own for an auto-merge.

### Layer 2: Heuristic Scoring

Use for structured probabilistic adjudication when no hard rule applies.

#### Identity model v2 weighted scoring (policies `c4-w1` / `c4-v1`)

Under identity model v2, Layer 2 replaces the additive weights in the Example
model below with a link-weight-driven score (uncalibrated prototype constants;
L3 owns recalibration):

- per-assertion strength `s(a) = min(1, tier · q · c · (1.15 if verified))`
  with tier weights keyed by tier NAME (`tier_1: 1.00`, `tier_2: 0.85`,
  `tier_3: 0.60`, `tier_4: 0.35`; conversation records drop one tier, missing
  or incomplete metadata is `tier_4`), quality `q` (`valid: 1.0`, else `0.6`),
  and extraction confidence `c` (default `1.0`)
- link weight `w = 1 − (1 − w_conf)(1 − w_cand)` where `w_conf` is the
  noisy-OR of confirmed assertions and
  `w_cand = min(0.24, noisy-OR of candidate-only assertions)`; candidate-only
  evidence (`link_status = 'pending_review'`) is capped at `0.24` and can never
  reduce confirmed evidence. The cap is dormant in production: the evidence
  queries still exclude `pending_review` rows
- exclusivity `excl = 1 / max(1, Identifier.participant_count)`
- rank `rank_score = w · excl · 0.5^(age_days / half_life)` with age anchored
  to evidence time (the max `last_confirmed_at` among the participant's links
  of the same type, never wall-clock); half-lives phone 365 / email 730 /
  `device_id` 180 / `social:*` 365 (nric and CRM ids never decay). Stored
  values round to 6 dp
- identifier contribution `signal × link_weight × IDF` with
  `IDF(n) = 1 / (1 + ln(max(n, 1)))` on the live participant count; every
  matching link contributes (not just the top 3), under the existing
  `IDENTIFIER_EVIDENCE_CAP` of 0.85. The legacy fanout recount and −0.25
  cliff are not used in v2; `phone_high_fanout` becomes a veto.
  Uncorroborated conversation evidence is capped at `τ_attach − 0.01 = 0.69`
- bands: attach `≥ 0.70`, union `0.50–0.69`, park below `0.50`, review only
  for the exception taxonomy. Attach needs ≥ 2 signal families from
  `{phone, email, name ≥ 0.50, dob, address}` and no veto

Provisional `c3-provisional` links are treated as unweighted (`link_weight`
`null` → 1.0) so pre-C4 links never zero out matching.

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
- penalize identifiers with high fan-out (`shared_suspected`)

### Cardinality Caps

If an Identifier node has more active `IDENTIFIED_BY` relationships than a
configurable threshold, skip that identifier for candidate generation.
Thresholds must be configurable per identifier type. Skipped identifiers must
be logged. Default thresholds are defined in
[profile-unifier-policy-decisions.md](./profile-unifier-policy-decisions.md).

### No-Candidate Path

When candidate generation produces zero candidates for a source record, the
match engine is not invoked. A new person is created directly. No
`MatchDecision` node is created. A `MergeEvent` of type `person_created`
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

## Multi-Match Resolution (Link-to-All, No Person Merge)

The engine evaluates **every** candidate, not only the first. When an incoming
source record independently reaches the **merge** band against more than one
*distinct* active person, the engine must not silently attach to one and discard
the rest — but it must also **not** merge those persons. Two persons sharing the
identifier the record matched on may be genuinely different people (e.g. a shared
household phone), so collapsing them on the strength of one bridging record would
risk a false merge, which the precision-first policy forbids.

Resolution:

- choose a **primary** = highest-confidence merge, ties broken deterministically
  by `person_id` so the outcome does not depend on candidate iteration order
- link the incoming record and its extracted evidence (source record,
  identifiers, addresses, facts) to the primary **and to every other
  merge-matched person** — each person keeps its own `LINKED_TO` /
  `IDENTIFIED_BY` / `LIVES_AT` / `HAS_FACT` edges for the record
- recompute each affected person's golden profile
- the persons remain **separate** — no `MERGED_INTO`, no `merged` status, no
  person-to-person rewiring

The extra person ids are carried on `MatchResult.additional_linked_person_ids`.
This applies to both deterministic (confidence 1.0) and heuristic merge matches
**except CRM deals**. A Bitrix CRM deal must have at most one authoritative active
Person link: an existing canonical `crm_contact_id` owner wins before generic
matching; duplicate canonical owners and generic multi-MERGE results become review
cases. CRM-deal phone/email values are bounded, unverified match-only hints and are
never projected as Person identity evidence. Hard NO_MATCH rules still drop a
conflicting candidate before it can become a merge target. Whether the shared
evidence means the persons should ultimately be merged is left to human review /
later analysis, not decided automatically here.

**Margin gate (identity model v2 only, #492).** Before merge resolution, when
the top two attach-class results are both heuristic (no deterministic MERGE
among the collected results), the engine compares top-1 minus top-2 confidence:
a margin below `0.10` emits no MERGE at all — two or more anchored attach-class
candidates within the margin open one `anchored_ambiguity` review case (the case
key spans the sorted anchored person ids, so a second ingest returns the same
case), otherwise the record parks with no case. A margin of `≥ 0.10` keeps
link-to-all unchanged: the primary attaches and every other attach-clearing
candidate is still linked via `MatchResult.additional_linked_person_ids`.

## Person-Pair Auditing (Shared-Identifier Bridges) — Retired in v2 (#492)

The synchronous person-pair auditor (`pipeline_person_pairs.py`) was deleted in
#492: it created `engine_type = 'pair_audit'` cases (and merged pairs at
confidence ≥ 0.40) that the exception-only review policy no longer admits, and
its alias-free `merge_person_pair` left no safe production caller. Merge of a
person pair is now alias-only (a `LinkDecision` of kind `alias` naming a person
in the pair) and exists only as the building block for L2's NRIC-alias
remediation.

Legacy `pair_audit` cases already in the tree are **re-scored only** (the
recalc task, `pair_audit_recalc.py`, and the CRM-repair verifier stay); no new
pair-audit cases are created. Resolution of an existing person-pair case
(merge / reject / manual_no_match) still follows the normal reviewer workflow.

## Heuristic Feature Catalog

### Positive Evidence

- exact verified phone match
- exact verified email match
- approximate (near-miss) phone match (same region, NSN edit-distance 1)
- approximate (near-miss) email match (domain-typo or local-part near-miss)
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
- source record type (`system` vs `conversation`)
- extraction confidence (for `conversation` records)
- verification status
- identifier recency
- whether the identifier was manually confirmed
- whether the profile already has many linked source systems

**Conversation-record handling**: when a feature is supported by a
`conversation` source record, multiply its contribution by the record's
`extraction_confidence` (0.0–1.0) and downgrade its effective trust tier by
one level. A pair whose evidence is *exclusively* conversation-sourced
cannot exceed the auto-merge threshold; the scoring engine must cap such
pairs at the top of the review band. Two independent conversation records
that corroborate the same identifier are treated as a single, slightly
stronger conversation observation — never as deterministic confirmation.

**Per-record-type merge criteria**: some record types carry their own
auto-merge rule layered onto the generic engine. All promotions share one
hard-conflict blocker set — strong name mismatch (Jaro-Winkler < 0.50), DOB
conflict, or a high-fanout phone (> cap) — on top of the deterministic blockers
(conflicting NRIC, `NO_MATCH_LOCK`).

- **`bankruptcy`** (Layer 1 gate) — the exact-NRIC deterministic merge
  additionally requires a **partial name match** (JW ≥ 0.50) when *both* sides
  carry a name. A matching NRIC with a strongly conflicting name does **not**
  auto-merge (it falls through to Layer 2 → typically a new person + a
  person-pair review on the shared NRIC). NRIC-alone still merges when a name is
  absent on either side.
- **`relationship`** (Layer 2 promotion) — a pair matching on **phone + partial
  name** (JW ≥ 0.50) is promoted to auto-merge even below the 0.40 band, unless a
  blocker fires. This mirrors the conversation-promotion mechanism
  (`_promote_by_record_type`).
- **`sales`** — *(planned)* phone + partial-name fallback resolution for orders
  that cannot resolve a customer via the POS foreign key.

`identity` keeps the plain additive behaviour with the unconditional NRIC merge.

### Approximate Identifier Matching

Two normalization-correctness prerequisites make *exact*-match identifiers more
correct before any approximate scoring runs:

- **Region-hint phone normalization**: eko/speedzone connectors derive an ISO
  region hint from the source row's `phone_code`/`country` columns and pass it
  to `normalize_phone`, falling back to the SG default if the hinted region
  produces `invalid_format`. This resolves the region-ambiguity where an
  8-digit local number normalizes to a different, both-"valid" E.164 value
  depending on default region.
- **Gmail/Googlemail canonicalization**: `john.tan+promo@googlemail.com` and
  `johntan@gmail.com` normalize to the same `normalized_value` (`+tag`
  stripped, dots removed from the local part, domain folded to `gmail.com`) —
  a true equivalence, so these match **exactly** via existing graph traversal,
  not via approximate scoring.

On top of these, the heuristic scorer runs a second pass over incoming
phone/email identifiers that did **not** get an exact match: a same-region
phone whose national significant number is Damerau-Levenshtein distance 1 from
one of the candidate's phones (`phone_approx_match`), or an email that is a
single-axis domain-typo or local-part near-miss of one of the candidate's
emails (`email_approx_match`). Each contributes a small `+0.10` weight.

**Approximate signals are excluded from all promotion paths.** Conversation
promotion (`_can_promote_conversation`) and the `relationship` promotion branch
of `_promote_by_record_type` both key off `phone_exact_match`/`email_exact_match
is True` — `phone_approx_match`/`email_approx_match` are distinct fields that
neither function reads, so an approximate match can never by itself satisfy a
promotion criterion. Approximate evidence is also excluded from
`identifier_system_corroborated` and from fanout checks (fanout concerns the
*candidate's* value being widely shared, not a value that doesn't match it
exactly).

## Example Feature Vector

```json
{
  "phone_exact_match": true,
  "phone_approx_match": false,
  "phone_verified_both": false,
  "email_exact_match": false,
  "email_approx_match": false,
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

The persisted feature snapshot is built from **structured scoring signals** that
each scoring function sets directly — it is never derived by parsing the
human-readable decision `reasons`. Merge-gating logic (e.g. conversation
promotion) and the stored audit/ML record therefore depend on typed fields, not
on log wording, so reason text can change without altering decisions.

## Example Heuristic Scoring Model

Illustrative only. Tune on labeled data. The weights below are simple additive
examples for readability — they describe the legacy v1 scorer. Under identity
model v2 the production scorer uses the weighted model in Layer 2 above
(`signal × link_weight × IDF`, bands attach `0.70` / union `0.50`, margin
`0.10`, ≥ 2 signal families). The production implementation should use conditional
weighting (unverified identifiers worth less than verified), capping (multiple
identifier matches should not stack linearly to exceed auto-merge threshold),
or a proper probabilistic framework such as Fellegi-Sunter. Simple addition of
weak signals must not produce auto-merge confidence.

### Positive Weights

- verified government ID exact match: hard merge
- exact verified phone: `+0.35`
- exact verified email: `+0.35`
- approximate phone match: `+0.10`
- approximate email match: `+0.10`
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

Legacy v1 bands (also used in relationship records at `0.20` / `0.10`):

- `>= 0.40`: auto-merge
- `0.20 - 0.39`: review
- `< 0.20`: no-match

Identity model v2 bands (policy `c4-v1`):

- `>= 0.70` with ≥ 2 signal families and no veto: attach (MERGE)
- `0.50 - 0.69`: union (NO_MATCH labelled `union`, Profile materialised once)
- `< 0.50`: park (NO_MATCH, no case)
- review only for the exception taxonomy, via the margin gate or explicit
  `review_reason` (`anchored_ambiguity`, `nric_contradiction`,
  `crm_ambiguity`, `continuity_reassignment`)

### Name Similarity Bands (Jaro–Winkler)

Name weights are keyed off Jaro–Winkler similarity, whose distribution is *not*
linear — it credits string length and shared prefixes, so unrelated name pairs
floor around **0.35–0.49** rather than near zero, while same-person variants sit
at/above **0.50** (e.g. Bob/Robert ≈ 0.50, Li Wei/Wei Li ≈ 0.67, typos ≈ 0.85+).
Thresholds are therefore calibrated to that distribution, not to an intuitive
"percent similar":

- `> 0.80` → high similarity (`+0.20`)
- `0.50 – 0.80` → medium similarity (`+0.10`)
- `< 0.50` → strong name mismatch (`-0.25`)

The strong-mismatch cutoff sits just below the same-person floor so clearly
different names penalize without catching legitimate variants. A false mismatch
only routes a pair to review (never a false merge), so the cutoff errs high on
purpose. Retune on labeled benchmark data.

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
- single-digit phone typos and transposed-digit phone numbers (same region)
- gmail dot/plus-addressing variants (e.g. `john.tan+promo@googlemail.com` vs `johntan@gmail.com`)
- common email domain typos (e.g. `gmial.com`, `hotmial.com`, `yahooo.com`)

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

**Exception-only review (identity model v2, #492).** Under v2 the review band
above admits nothing on its own: heuristic scoring never emits REVIEW, and a
REVIEW without a taxonomy reason is suppressed (`suppressed_reason =
'not_exception'`). The only review reasons are the exception taxonomy —
`anchored_ambiguity` (margin gate), `nric_contradiction` (trusted-ID
contradiction on promotion), `crm_ambiguity` (duplicate canonical CRM owners),
`continuity_reassignment` (re-evaluation moves). Cases are keyed by
`case_key` (sha1 over a per-reason field set: sorted person ids for
`anchored_ambiguity`, profile id plus holder person id for
`nric_contradiction`, record identity plus ids otherwise), created with
`CREATE_REVIEW_CASE_BY_KEY` (MERGE on `case_key`; a repeat occurrence bumps
`occurrence_count` and `last_seen_at` even on a resolved case, never reopens
it, and adds no second `FOR_DECISION` edge), and guarded by a unique
constraint on `ReviewCase.case_key`.

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
