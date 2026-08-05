# Profile Unifier — Sales Prediction Approach and PRD

Status: draft

Date: 2026-08-05

Initial use case: 30-day CRM WON propensity

## Purpose

HyperP has customer and relationship evidence across CRM deals, CRM activity,
messages, and calls. This document proposes an explainable
predictive capability that helps sales teams prioritize open opportunities.

The first model should answer one specific decision question:

> For an active Person with an open CRM opportunity, what is the probability
> that the deal will first enter an approved CRM WON stage within the next 30 days?

Call this the **CRM WON Propensity** prediction. CRM WON is an operational sales
outcome, not evidence of payment, fulfillment, order completion, or realized revenue.
Expected value,
time-to-close, repeat purchase, churn, product affinity, and next-best-action
should remain separate future models with their own labels and evaluation.

HyperP already provides the main source contracts:

- `crm_deal` for versioned deal state;
- `crm_history` for immutable CRM activity;
- `conversation` for extracted customer messages;
- `call` for detailed calls;
- `sales`, `Order`, `LineItem`, and `Product` for commerce activity; and
- canonical `Person` resolution across these records.

Prediction must remain independent from the existing LLM-authored sales profile.
The profile is descriptive prose and must not become a model feature because
prompt and provider changes would make training and inference irreproducible.

## Recommended roadmap

### Phase 1 — Conversion propensity

Prediction unit:

```text
(person_id, deal_key, as_of_at)
```

A Person may have several open deals, but each deal receives its own probability.
The Person page can present the highest-priority opportunity or an aggregate,
while the model continues to train and evaluate at deal level.

A positive outcome occurs only when the deal first enters an approved WON stage
in `(as_of_at, as_of_at + 30 days]`.

A row becomes negative only after the complete 30-day window passes without a
qualifying conversion. A LOST transition before the horizon ends does not by
itself prove a negative because the deal may reopen; the approved reopen/revert
policy determines whether such rows remain eligible, are censored, or are
excluded. Recent open deals must remain unlabeled until their outcome window
matures.

### Phase 2 — Expected value

Use a separate revenue model:

```text
expected_value = calibrated_conversion_probability × conditional_expected_revenue
```

Do not combine probability and value into one opaque MVP score.

### Phase 3 — Time to conversion

Estimate conversion within 7, 30, and 90 days and a time-to-conversion range.
A survival model can later represent open opportunities whose final outcomes
are not yet known.

### Phase 4 — Incremental action

Propensity predicts who is likely to buy; it does not prove that an additional
call, message, or offer will cause conversion. After controlled or randomized
outreach data exists, an uplift model can identify customers whose likelihood
changes because of a particular intervention.

### Later independent models

- repeat-purchase propensity;
- churn or lapse risk;
- product or category affinity;
- next-best-action or next-best-product;
- predicted deal value; and
- predicted payment delay.

## Data and labeling

### Point-in-time snapshots

Every training row must represent only what was known at `as_of_at`. Generate
historical snapshots daily or whenever a material event occurs. A feature may
use evidence only when:

```text
event_available_at <= as_of_at
```

Calculate the label from the future:

```text
as_of_at < qualifying_outcome_at <= as_of_at + 30 days
```

This prevents leakage from final deal status, post-sale activity, later payment
status, post-purchase conversations, future orders, and backfilled CRM fields.
Use the time when data became available to HyperP, not only the source event
time, when late-arriving evidence is possible.

Evaluation must use rolling time-ordered backtests rather than random row splits.
Snapshots for the same Person or deal must not cross incompatible train/test
partitions.

### Feature windows

Calculate features for the last 7, 30, 90, and 365 days, plus lifetime-to-date.
This captures immediate intent, engagement trend, and long-term value.

### Entity strategy

Evaluate a pooled model initially, with entity included as a controlled feature,
but report performance and calibration separately by entity. Use entity-specific
calibration or models when sales processes, base rates, products, or source
quality differ materially.

## Feature design

### CRM deal features

- current stage as of the cutoff;
- days in stage and since creation;
- stage-transition and regression counts;
- expected amount and product/category;
- quote and discount indicators;
- open deal count for the Person;
- prior won/lost counts and win rate;
- days since the last won deal;
- historical sales-cycle duration; and
- amount revision and availability status.

Historical source versions must reconstruct state at the prediction cutoff.
Current mutable deal fields are not sufficient for training.

### Deferred commerce features

- purchase recency and frequency;
- lifetime revenue;
- average, maximum, and trending order value;
- product-category breadth and repeat purchase;
- refund and cancellation rates;
- discount usage;
- time between purchases;
- previous invoice payment delay; and
- similarity between the current deal and historical purchases.

Orders, invoices, payments, fulfillment, and realized revenue are deferred to a
separately governed future outcome version. They are not label evidence for this MVP.

### Message features

Start with structured, explainable fields rather than raw text embeddings:

- days since the last valid customer message;
- inbound/outbound counts by feature window;
- customer-to-agent message ratio;
- agent and customer response times;
- customer-initiated conversation count;
- unanswered messages;
- product, price, quote, and purchase-intent inquiries;
- objections and requested follow-ups;
- appointments;
- conversation purpose and outcome;
- tone or sentiment trend when reliable;
- active thread count; and
- extraction confidence and coverage.

#### Valid message definition

A message or conversation is valid only when it:

1. links to one canonical active Person;
2. has an unambiguous timestamp and direction;
3. was available before the prediction cutoff;
4. is not duplicate, superseded, rejected, excluded, or internal-only;
5. contains a human customer or agent interaction;
6. is not solely a bot event, template, notification, or delivery receipt;
7. belongs to the relevant entity and, where possible, deal;
8. meets confidence requirements for derived fields; and
9. is permitted for the intended use and retention period.

Bot and template activity may create separate operational features but must not
count as customer engagement.

### Call features

- days since the last connected call;
- connected and attempted counts;
- connection rate and direction;
- total and median connected duration;
- customer speaking time when reliable;
- voicemail, missed-call, and repeated-attempt counts;
- follow-up commitments and appointments;
- product, quote, and outcome fields;
- time from message to call and call to reply; and
- transcript or extraction confidence.

#### Sufficient call definition

A call is usable only when it:

1. links to an active Person and CRM activity or deal context;
2. was available before the prediction cutoff;
3. is not a duplicate, test call, or internal-only call;
4. has a trustworthy connected/completed disposition;
5. meets a minimum duration or customer-speech requirement;
6. meets transcript-quality rules for transcript-derived features; and
7. complies with recording, consent, retention, and analytical-use policy.

Choose the duration threshold through exploratory analysis rather than assuming
one value works for every entity. Suggested quality bands are:

```text
not_connected
connected_short
connected_substantive
connected_transcript_usable
connected_transcript_unusable
```

Calls are not mandatory for a score. Missing calls affect data sufficiency;
otherwise digital-first customers and entities with incomplete call capture
would be systematically excluded.

### Cross-channel sequence features

After the baseline is stable, add explicitly defined sequences such as:

- message → call → quote within seven days;
- prompt agent response after a customer message;
- call followed by customer reply;
- product inquiry followed by quote activity;
- repeated engagement without stage movement;
- multiple channels active in one week; and
- engagement acceleration or decline.

### Data-quality features

- source and modality coverage;
- identity/deal linkage confidence;
- profile completeness;
- days since successful ingestion;
- missing-modality flags;
- extraction confidence;
- conflict count;
- percentage of activities linked to a deal; and
- Person merge or reassignment recency.

Data sufficiency must be exposed independently from conversion probability.

### Initially prohibited features

Do not train on direct identifier values, names, phone/email values, exact
address or postal code, exact date of birth, protected traits, health data,
voice biometrics, inferred emotion, sensitive relationship categories,
LLM-authored profile prose, or employee identity as a ranking feature.
Identifiers may link records before feature generation, but their literal
values must not become features.

## Model strategy

### Business-rule baseline

Benchmark simple rules using recent customer replies, substantive calls, quotes,
deal amount availability, and normal stage age. Machine learning must show
material improvement over this baseline.

### Logistic-regression baseline

Use regularized logistic regression as the first trained model. It is stable,
interpretable, easy to calibrate, and useful for identifying leakage or
unexpected feature direction.

### Gradient-boosted candidate

Evaluate CatBoost, LightGBM, or XGBoost for nonlinear interactions between
recency, frequency, stage, value, communication behavior, call quality, and
missingness. Select the simplest model with repeatable improvement.

### Probability calibration

The API returns a calibrated probability, not an arbitrary ranking score.
Evaluate sigmoid/Platt and isotonic calibration, with entity-specific
calibration where base rates differ.

More calls or messages may reflect customer intent, salesperson belief, a
difficult opportunity, or failed outreach. Explanations must describe
associations and must not claim these features caused conversion.

## Evaluation

### Offline metrics

Do not use accuracy as the primary metric. Report:

- precision and lift at the top 5%, 10%, and 20%;
- recall at actual sales-team capacity;
- precision-recall AUC;
- Brier score, log loss, and calibration error;
- calibration curves;
- false-positive and false-negative rates;
- results by entity, channel, category, and sufficiency band; and
- monthly temporal stability.

The primary offline decision metric is lift and precision at the number of
opportunities the team can actually work.

### Online business metrics

- conversion and incremental conversion versus control;
- won revenue per sales hour;
- time to meaningful contact and sales-cycle duration;
- contact attempts per conversion;
- unsubscribe and complaint rates;
- adoption, usefulness feedback, and override rate; and
- opportunity concentration by entity or approved segment.

The release succeeds through incremental business value in a controlled pilot,
not offline model performance alone.

# Product Requirements Document

## Problem statement

Sales representatives must manually determine which customers to contact first
despite having fragmented deal, message, and call evidence.
Activity volume is ambiguous and does not provide a consistent priority.

HyperP should provide an explainable, calibrated estimate for prioritizing open
opportunities without automating customer treatment.

## Product objective

Provide a daily prioritized list of open opportunities ranked by their
probability of first entering an approved CRM WON stage within 30 days.

The product must use point-in-time evidence, support incomplete modalities,
show reasons and cautions, distinguish insufficient data, retain immutable
history, and preserve human control.

## Goals

1. Generate a calibrated 30-day probability for eligible open deals.
2. Rank opportunities by probability and later expected value.
3. Provide evidence-backed reason and caution codes.
4. Show data sufficiency and freshness.
5. Capture disposition and override feedback.
6. Support shadow and controlled pilot rollout.
7. Maintain model, feature, input, and prediction lineage.
8. Invalidate predictions when accepted inputs change.

## Non-goals

The MVP will not automatically contact, close, reject, reassign, price, or
exclude a customer. It will not determine credit eligibility or terms, use
biometrics or protected traits, score employees, train on LLM-authored prose,
claim causal impact, or expose predictions publicly.

## Primary users

- **Sales representatives** need prioritized work, reasons, and fresh evidence.
- **Sales managers** need workload, outcome, adoption, and quality visibility.
- **Sales operations/data teams** define labels, mappings, monitoring, and
  model rollout.
- **Compliance/administration** audits features, lineage, restrictions, and
  entity enablement.

## User stories

- A rep can view and filter opportunities by probability, owner, entity,
  product, priority, sufficiency, and freshness.
- A rep can see supporting evidence and distinguish low probability from low
  data coverage.
- A rep can view prediction history on Person and deal detail.
- A rep can record a sales disposition and prediction feedback.
- A manager can compare outcomes by priority band and monitor coverage,
  concentration, staleness, adoption, and overrides.

## Eligibility

A deal is eligible when its Person is active and canonical, its stage maps to an
open state, it links to one approved Person, its entity and currency are
supported, its history is trustworthy and fresh, and governance configuration
is present.

Messages and calls are not mandatory. Return `insufficient_data` when linkage is
ambiguous, history cannot be reconstructed, timestamps are invalid, material
features are unavailable, the entity is unsupported, or merge/unmerge
recomputation is pending.

## Outcome contract

The primary label is `crm_won_30d`: first entry into an approved CRM WON stage
in the 30-day horizon. Configuration must define entity-specific open, WON,
LOST, excluded, reopened, reverted, stale, and administrative-stage treatment.

## Functional requirements

### FR-1 — Point-in-time features

Generate immutable training and scoring snapshots containing only evidence
available at or before `as_of_at`.

### FR-2 — Prediction generation

Generate one current `crm_won_30d` prediction per eligible deal and approved
model version.

### FR-3 — Immutable history

Every prediction retains its Person/deal keys, type, horizon, probability,
band, sufficiency, model/feature versions, input fingerprint/revision,
timestamps, reason/caution codes, and status.

### FR-4 — Current pointer

Only the latest valid prediction is current; prior predictions remain for audit
and score-change analysis.

### FR-5 — Invalidation

Mark a prediction stale after material accepted changes to deal stage/amount,
valid messages, substantive calls, source-record versions, Person merge/unmerge,
or customer assignment.

### FR-6 — Explanation

Return up to five versioned reason codes with evidence references. Explanations
must not claim causality.

### FR-7 — Availability states

Distinguish `current`, `low_probability`, `insufficient_data`, `stale`,
`pending`, `failed`, and `disabled`.

### FR-8 — Feedback

Allow contacted/not-contacted, interested/not-interested, wrong association,
already converted, do-not-contact, useful/not-useful, and a permitted note.
Feedback does not automatically become a training label.

### FR-9 — Administrative controls

Allow entity enablement, stage/outcome mappings, reopen/revert policy, active
model inspection, monitoring, disablement, and rollback.

## Output contract

```json
{
  "prediction_id": "uuid",
  "person_id": "uuid",
  "deal_key": "bitrix:12345",
  "prediction_type": "crm_won_30d",
  "as_of_at": "2026-08-04T00:00:00Z",
  "valid_until": "2026-08-05T00:00:00Z",
  "probability": 0.72,
  "score": 72,
  "priority_band": "high",
  "data_sufficiency": "medium",
  "model_version": "crm-won-30d-v1",
  "feature_version": "sales-features-v1",
  "input_revision": 19,
  "reason_codes": [
    "customer_replied_recently",
    "substantive_call_completed",
    "deal_amount_available",
    "normal_stage_age"
  ],
  "caution_codes": ["limited_message_history"]
}
```

Store calibrated probabilities. UI bands such as `very_high`, `high`, `medium`,
and `low` are capacity-based and may change without retraining.

## Proposed architecture

Do not train from ad hoc live Neo4j queries. Build versioned point-in-time
snapshots and export them to Parquet or an approved analytical warehouse.

```text
Neo4j accepted facts
    ↓
Point-in-time snapshot builder
    ↓
Versioned feature dataset
    ↓
Training and temporal backtest
    ↓
Approved model and calibration artifacts
    ↓
Batch scoring
    ↓
Immutable SalesPrediction nodes
```

Training and scoring must share feature definitions to avoid skew.

Suggested graph relationships:

```text
(:Person)-[:HAS_SALES_PREDICTION]->(:SalesPrediction)
(:SourceRecord {record_type: "crm_deal"})-[:HAS_SALES_PREDICTION]
  ->(:SalesPrediction)
(:Person)-[:CURRENT_SALES_PREDICTION {
      prediction_type: "crm_won_30d",
  deal_key: "..."
}]->(:SalesPrediction)
```

Suggested prediction fields include `prediction_id`, `person_id`, `deal_key`,
`prediction_type`, `horizon_days`, `as_of_at`, `probability`, `priority_band`,
`data_sufficiency`, `status`, `model_version`, `feature_version`,
`input_revision`, `input_fingerprint`, `reason_codes`, `caution_codes`,
`generated_at`, `valid_until`, and `obsolete_reason`.

The MVP uses nightly scoring, optional rescore after major commercial events,
and stale marking after new messages or calls. Prediction work must not
determine whether ingestion succeeds.

## API and MCP

Suggested canonical routes:

```text
GET /v1/sales/predictions
GET /v1/persons/{person_id}/sales-predictions
GET /v1/persons/{person_id}/sales-predictions/history
GET /v1/deals/{deal_key}/sales-prediction
POST /v1/sales/predictions/{prediction_id}/feedback
```

Authenticated UI routes use `/app/v2`; browser calls go through frontend2 BFF
handlers. Add schema-visible endpoints to the route catalog with stable unique
operation IDs and API-to-MCP parity tests. Use `ApiResponse[T]`, `envelope()`,
repository Protocols, and existing cursor pagination.

## UI requirements

The sales worklist displays Person, deal, owner, entity, amount, probability,
priority, sufficiency, top reason, last interaction, and prediction age.

The Person/deal card presents an estimate such as:

```text
72% estimated probability of entering CRM WON within 30 days
Data sufficiency: Medium
Calculated: 04 Aug 2026

Key signals
• Customer replied recently
• Substantive call completed
• Previous purchase in the same category

Caution
• Limited historical message coverage
```

Include evidence dates, score history, stale state, feedback controls, and an
advisory disclaimer. Never use deterministic wording such as “Will convert.”

## Non-functional requirements

- The same snapshot, feature version, model artifact, and configuration must
  reproduce the same prediction within documented tolerance.
- Nightly scoring must finish before the configured sales workday.
- Prediction failures must not block ingestion.
- Logs must not contain raw messages, calls, prompts, transcripts, or direct
  identifiers.
- Retain training dataset version, feature schema, train/validation windows,
  model and calibration hashes, approval, lineage, and rollback history.
- The active model must be disableable or rollbackable without code deployment.

Predictions are advisory and must not be the sole basis for customer exclusion,
credit terms, pricing, offer denial, regulated eligibility, or high-frequency
outreach. Credit or pricing use requires a separately designed and governed
product.

## Monitoring

### Data

Monitor eligible/scored volume, missing features, message/call linkage, invalid
timestamps, entity/source distribution, feature drift, staleness, and
ingestion-to-scoring delay.

### Model

Monitor score distributions, calibration by mature outcome month, precision and
lift, entity and sufficiency performance, reason codes, failures, and version
coverage.

### Business and safety

Monitor contact frequency, opt-outs, complaints, overrides, concentration,
conversion lift, revenue per sales hour, and disparity across approved
evaluation segments.

## Acceptance criteria

Shadow deployment requires:

1. point-in-time snapshots pass leakage review;
2. every feature has a definition, owner, type, availability timestamp, and
   privacy classification;
3. outcome mappings are approved per entity;
4. the model beats random and business-rule baselines in temporal tests;
5. high-priority bands show meaningful lift;
6. calibration meets the agreed tolerance;
7. entity-level performance has no unexplained severe degradation;
8. predictions are reproducible and fully versioned;
9. low probability and insufficient data remain distinct;
10. merge/unmerge invalidation is safe;
11. raw conversations and direct identifiers are absent from artifacts and
    explanations;
12. the UI clearly presents estimates;
13. disablement and rollback work; and
14. shadow scoring causes no reliability regression.

A controlled pilot additionally requires mature shadow outcomes, approved
explanation usability, operational monitoring and rollback, predefined pilot
and control groups, documented contact limits, and approved success/stopping
criteria.

## Delivery phases

### Phase 0 — Feasibility

Deliver entity outcome mappings, lifecycle analysis, message/call quality rules,
point-in-time audit, label-volume report, leakage review, and privacy inventory.

### Phase 1 — Dataset and rule baseline

Deliver the snapshot schema, feature dictionary, rule score, historical
backtest, and worklist prototype.

### Phase 2 — Model development

Deliver logistic and boosted-tree candidates, calibration, reason codes,
temporal/entity evaluation, and a model card.

### Phase 3 — Shadow production

Generate predictions without exposing them to sales decisions. Validate feature
parity, stability, reliability, mature outcomes, explanations, and privacy.

### Phase 4 — Controlled pilot

Compare model-assisted prioritization against business as usual. Measure
incremental conversion, revenue per effort, contact volume, complaints, and
adoption.

### Phase 5 — Expansion

Only after a successful pilot, add expected revenue, time-to-close, repeat
purchase, product affinity, and uplift-based next-best-action.

## Decisions required before implementation

1. Entity-specific CRM stage, reopen, revert, and exclusion mappings.
2. Prediction horizon; 30 days is recommended.
3. Prediction unit; deal-level is recommended with Person-level presentation.
4. Pooled versus entity-specific models and calibration.
5. Scoring cadence; nightly is recommended initially.
6. Sales worklist capacity for capacity-based evaluation.
7. Permitted structured message and call attributes.
8. Contact-frequency and outreach safeguards.
9. Currency and reversal/refund treatment.
10. Explicit prohibition from credit, eligibility, and pricing decisions.

The primary implementation principle is to create a trustworthy point-in-time
dataset and outcome definition before selecting a model. Data lineage, leakage
prevention, calibration, and controlled business evaluation matter more to the
MVP than a sophisticated neural architecture.

## External references

- [NIST AI Risk Management Framework](https://www.nist.gov/itl/ai-risk-management-framework)
- [Singapore PDPC Advisory Guidelines on Personal Data in AI Systems](https://www.pdpc.gov.sg/help-and-resources/2024/03/advisory-guidelines-on-use-of-personal-data-in-ai-recommendation-and-decision-systems)
- [Scikit-learn probability calibration](https://scikit-learn.org/stable/modules/calibration.html)
- [Scikit-learn TimeSeriesSplit](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html)
- [Google Machine Learning — Data Leakage](https://developers.google.com/machine-learning/crash-course/overfitting/data-leakage)
