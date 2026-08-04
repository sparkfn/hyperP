# Profile Unifier — Sales Prediction Feasibility Discovery

Status: issue #124 discovery protocol (2026-08-04).  This document is the
decision record and execution protocol for the first gate of the sales
prediction program. It authorizes discovery only. It does **not** authorize
training, scoring, API work, a sales worklist, automated actions, or a model.

Reading order: read this after the [Sales Prediction Approach and
PRD](profile-unifier-sales-prediction-prd.md), the [Entity and Sales
design](profile-unifier-entity-and-sales.md), and the [Graph
Schema](profile-unifier-graph-schema.md).

## Decision question

For each approved entity and eligible deal, can HyperP reproducibly answer:

```text
What was known about this eligible deal at time T,
and did the approved commercial outcome occur during T+30 days?
```

The discovery owner must record one decision: **`go`**,
**`collect_more_data`**, **`rules_only`**, or **`stop`**. A missing data point
is not silently treated as negative evidence.

## Scope and non-goals

The prediction unit remains `(person_id, deal_key, as_of_at)` and evaluation
remains deal-level. Person-level presentation is out of scope. Discovery may
inspect aggregate, lineage-preserving records from Bitrix, Fundbox, Eko, and
SpeedZone.

Out of scope: production training, a prediction schema or endpoint, model
selection beyond an evidence-backed recommendation, customer contact, deal
closure, reassignment, pricing, discounts, payment terms, or credit and
eligibility use.

## Required configuration before running reports

Each run must identify its immutable configuration version and include the
following per entity. Do not infer mappings from label names in code.

| Configuration item | Required decision | Evidence owner |
| --- | --- | --- |
| Open stages | Exact CRM stage IDs that are eligible at `T` | Sales operations |
| Won stages | Exact CRM stage IDs that count only when no higher-precedence evidence exists | Sales operations |
| Lost/cancelled stages | Exact stage IDs and reopen treatment | Sales operations |
| Order success | Accepted order statuses and the event timestamp | Commerce owner |
| Invoice/payment success | Paid/settled states and the event timestamp; record `not available` if absent | Finance/commerce owner |
| Reversals | Refund, chargeback, cancellation, and reversal windows | Finance/commerce owner |
| Deal attribution | How an order/invoice is linked to a deal; unresolved ambiguity remains unresolved | Sales operations |
| Currency | Approved currencies and cross-currency treatment | Finance owner |
| Interaction quality | Message and call thresholds below | Data owner |

## Outcome and eligibility contract

`conversion_30d` is positive only when an approved commercial event occurs in
the interval `(T, T + 30 days]` and survives the configured reversal policy.
Recommended precedence is completed/fulfilled order, then paid invoice, then
approved CRM won stage. A lower-precedence source never overrides a verified
higher-precedence reversal.

A deal is eligible at `T` only when it has exactly one approved active Person,
an approved entity and currency, a configured open stage, a valid timestamp,
and reconstructable stage/version history. Exclude or separately count stale,
unassigned, renewal, recurring, and multi-contact deals until their policy is
approved. Do not label a deal negative until `T + 30 days` is in the past.

| Condition | Classification |
| --- | --- |
| Valid open deal and mature outcome window | eligible, labelable |
| Valid open deal but outcome window has not matured | eligible, unlabelled |
| Multiple Persons or unresolved contact attribution | insufficient data |
| Missing approved source mapping | unsupported entity/state |
| Invalid/eventually known timestamp | leakage risk; exclude until resolved |
| Refund/cancellation/chargeback under approved reversal policy | reversed; do not retain an unqualified positive label |

## Timestamp and historical reconstruction map

| Evidence | Source event time | HyperP observation time | HyperP availability time | Current limitation to verify |
| --- | --- | --- | --- | --- |
| CRM deal | Bitrix `DATE_CREATE` / `DATE_MODIFY` | `SourceRecord.observed_at` | `SourceRecord.ingested_at` and activation | Current API ingestion records versions; confirm all historical stage transitions are retrievable. |
| CRM activity/call | activity `START_TIME` / `LAST_UPDATED` | `SourceRecord.observed_at` | `SourceRecord.ingested_at` | Confirm a call's connection/outcome fields are reliable and no transcript availability is assumed. |
| Conversation | latest message timestamp | `SourceRecord.observed_at` | `SourceRecord.ingested_at` | A conversation is an assembled record; use only messages provably available by `T`. |
| Fundbox order | source order creation/release time | sales source record and `Order.ordered_at` | source record ingestion/activation | Current ingestion filters to realised statuses; refunds and cancellations need independent evidence. |
| Eko/SpeedZone sale | `sale_time` / invoice date | sales source record and `Order.ordered_at` | source record ingestion/activation | Verify production sales tables and returned/cancelled-sale semantics. |
| Invoice/payment | source settlement timestamp | not established | not established | Explicitly record `not available` unless a source is ingested. |

For every feature and label event, availability is the later of the source
event time and the time HyperP could have observed the record. Source event
time alone must never make late-arriving data eligible for a historical
snapshot.

## Message and call quality policy

Messages and calls are optional modalities. They change data sufficiency, not
base eligibility.

| Modality | Include only when | Exclude when |
| --- | --- | --- |
| Message | timestamp is valid; record is active; customer/agent role can be identified; configured extraction confidence is met | bot, template, notification, internal-only, duplicate, superseded, rejected, excluded, or older than configured evidence age |
| Call | timestamp is valid; record is active; connection/outcome policy is met; configured duration or customer-speech threshold is met | attempted/unconnected, duplicate, superseded, rejected, metadata that cannot support the configured quality rule, or older than configured evidence age |

Do not use LLM-authored summaries, profile prose, inferred emotion, or literal
conversation text as features. The discovery report may report only aggregate
coverage and quality counts.

## Privacy and restricted-use inventory

Allowed candidate categories require feature-level approval: stage/state,
amount bucket, age of approved commercial and interaction events, event counts,
structured source freshness, linkage quality, and data-sufficiency flags.

Prohibited features include direct identifiers; protected traits; health,
bankruptcy, NRIC, address, and other sensitive identifiers; sensitive
relationship categories; voice biometrics; inferred emotion; raw transcripts;
LLM-authored profile prose; and feedback text. The result is advisory sales
prioritization only, never credit, eligibility, price, discount, offer, or
payment-term determination.

## Reproducible aggregate discovery run

Use the read-only runner from `services/api` with an explicit historic cutoff,
entity list, and configuration version. It writes aggregate JSON and Markdown;
keep outputs outside the repository unless they are appropriately approved and
redacted.

```bash
uv run --project services/api python -m src.sales_prediction_discovery \
  --as-of-at 2026-08-01T00:00:00Z \
  --entities fundbox \
  --configuration-version issue-124-v1 \
  --json-output /secure/output/issue-124-fundbox.json \
  --markdown-output /secure/output/issue-124-fundbox.md
```

The runner transiently reads private source-record JSON to classify stage,
contact, call-duration, and order fields because those payloads are stored as
JSON strings in Neo4j. It never writes or displays that JSON. Output contains
only aggregates and never includes Person IDs, deal IDs, source-record IDs,
raw payloads, messages, calls, or feature values. It reports source, deal,
interaction, order, linkage, and late-arrival coverage. The output is necessary
but insufficient: combine it with approved mappings and a sampled
point-in-time lineage audit.

Person linkage in aggregate deal and sales rows is explicitly labelled
`current_graph_projection`. Merge/unmerge rewiring means it must not be treated
as proof that the same Person linkage existed at the historical cutoff. That
question requires the sampled lineage audit and may block the entity.

## Required report tables and evidence

| Deliverable | Minimum evidence | Decision if unavailable |
| --- | --- | --- |
| Outcome mapping | exact states, precedence, reversals, timestamps, owner approval | `collect_more_data` or `stop` for affected entity |
| Eligible population | counts by entity/month and every exclusion reason | do not train on undefined population |
| Label/maturity distribution | mature positive, negative, reversed, and unlabelled counts | do not call unripe rows negative |
| Linkage report | Person/deal and deal/order/invoice coverage plus ambiguity | retain unresolved links as insufficient data |
| Interaction report | raw and quality-filtered message/call coverage | keep modalities optional |
| Leakage register | late-arrival, backfill, versioning, reassignment, merge/unmerge risks | block features/labels that cannot be time-bounded |
| Privacy inventory | feature category, owner, use, prohibition decision | prohibit unapproved features |

## Recommended initial evaluation

Start only with an entity that has an approved outcome mapping, a reliable
exactly-one-Person deal population, and mature outcomes. Evaluate a transparent
rules-only prioritizer first. Recommend logistic regression only after rolling
temporal snapshots prove that point-in-time feature availability and outcome
attribution are reproducible. A more complex model is not a default outcome.

Numeric dataset, shadow, and pilot thresholds must be proposed from measured
entity-specific coverage, positive rates, late-arrival rates, and reversal
rates; this protocol intentionally sets no fabricated universal threshold.

## Final decision record

Fill this section after executing the evidence protocol:

| Field | Record |
| --- | --- |
| Configuration version | |
| Cutoff range and entities | |
| Approved outcome precedence | |
| Mature eligible count / positive count / base rate | |
| Person-to-deal / deal-to-outcome linkage coverage | |
| Valid message / usable call coverage | |
| Material leakage risks and owners | |
| Prohibited feature confirmation | |
| Recommended initial population | |
| Rules vs logistic vs complex recommendation | |
| Numeric next-gate thresholds | |
| Decision: `go` / `collect_more_data` / `rules_only` / `stop` | |
