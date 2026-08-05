# Profile Unifier — CRM WON 30-Day Discovery

Status: issue #124 discovery protocol (updated 05 Aug 2026).

This protocol determines whether HyperP can support the following operational
CRM outcome. It does not authorize a dataset, a rule, a model, scoring, an API,
a worklist, automated action, or a production ingestion change.

```text
crm_won_30d

For an eligible open CRM deal at time T, did the deal first enter an approved
WON stage during T + 30 days?
```

CRM WON is an operational sales outcome. It is not payment, fulfillment, order
completion, invoice settlement, deal-to-order attribution, or realized revenue.

## Current decision

The current decision is **`collect_more_data`**. The dated counts, owners,
blockers, report hashes, and follow-ups belong in GitHub issue #124; this
document contains the stable method rather than a duplicate status log.

Reconsider `go` only after all of the following are evidenced:

1. Recent CRM deal ingestion is complete.
2. An authoritative stage-transition source is captured or reconstructed.
3. Matured `crm_won_30d` outcomes are measured by entity and month.
4. Amount and other CRM field availability is validated at historical cutoffs.

## Scope boundary

Issue #124 may produce aggregate coverage and capability evidence. It must not
produce deal-level snapshots, eligibility rows, first-WON labels, mature cohort
labels, feature rows, train/test splits, a rules baseline, a model, predictions,
or a production schema. Those are follow-on work, blocked until this gate passes.

## Bitemporal evidence contract

Every discovery run has two UTC cutoffs:

| Term | Meaning |
| --- | --- |
| `as_of_at` | Historical observation cutoff `T`. |
| `report_cutoff_at` | Latest evidence the report may inspect; it must be on or after `as_of_at`. |
| source event / observation time | When the source says the fact occurred or was observed. |
| ingestion time | When HyperP received the fact. |
| activation time | When HyperP accepted it for use. |
| availability time | The latest required source-event, ingestion, and activation time. |

A fact is historically available only when valid source-event, ingestion, and
activation timestamps are all at or before `as_of_at`. A record observed by
`as_of_at` but ingested or activated before `report_cutoff_at` is late-arriving
evidence, never historical state. Missing lifecycle timestamps are uncertainty;
current lifecycle status must not be used as a substitute.

## Source-capability assessment

For each required semantic, report three separate facts:

1. **Connector/schema capability** — whether the active source contract carries
   the required meaning.
2. **Observed coverage** — whether qualifying records exist by
   `report_cutoff_at`.
3. **Point-in-time support** — whether the evidence can reproduce state at `T`.

A zero count does not prove source unavailability. Conversely, the current
`crm_history` records are generic Bitrix CRM activities and must not be treated
as authoritative deal-stage transitions unless a source investigation proves
otherwise.

## Required policy input

Sales Operations must approve entity-specific stage policies outside executable
code. The optional discovery JSON can record open, WON, LOST, excluded, and
reopen/revert policy status for reproducibility, but a self-declared approval
flag is not evidence. The report separately records mapping supplied/valid,
canonical configuration hash, external approval reference, and verified versus
unverified approval status.

Do not infer a stage meaning from its name. A missing mapping is reported as
`mapping_not_supplied`; a supplied mapping without verified external approval is
reported as `mapping_approval_unverified`. Either condition, or missing
authoritative transition history, produces `label_unavailable`. The runner must
not infer a first-WON label from the current stage or from snapshot differences.

## Aggregate reports and privacy

The read-only runner may report restricted aggregate counts by entity, source,
category, stage, currency, field-status band, and capability status. It must
bound and validate these taxonomy values before rendering.

The version-1 connector catalog recognizes `bitrix_chat` as the CRM source and
`SGD`, `USD`, and `MYR` as supported currencies. Other values in the embedded
ISO 4217 alphabetic-code catalog are counted as
`present_valid_but_unsupported`; non-ISO and missing values remain separate.
Stage IDs are emitted only when present in the validated mapping supplied for
that entity. Category values are reported as shape/status bands rather than
copied from source payloads.

It must never emit Person, deal, source-record, employee, phone, email, address,
or other direct IDs; raw payloads; record-level amounts; messages; transcripts;
or LLM-authored prose. Outputs are restricted analytics artifacts. Small-cell
suppression is not part of this protocol, so reports must not be distributed
beyond the approved restricted environment until a disclosure policy exists.

Messages and calls are optional feature modalities. Their absence affects
coverage, never the ability to define the CRM WON label.

## Required evidence and decision package

Before a positive gate decision, issue #124 needs:

- approved stage mappings and reopen/revert treatment per entity;
- an eligibility and maturation definition;
- source/timestamp map and authoritative first-WON source;
- mature label distribution by entity/month;
- Person-to-deal linkage evidence at historical cutoffs;
- amount, currency, and Bitrix probability availability/leakage evidence;
- message and call quality coverage;
- late-arrival and backfill risk register;
- privacy classification and prohibited-feature list; and
- an explicit `go`, `collect_more_data`, `rules_only`, or `stop` decision.

The current recommendation is no trained model and no production rules. A
transparent rules baseline may be considered only after valid labels exist;
logistic regression follows only after point-in-time leakage review. More complex
models are not a default. Later dataset, shadow, and pilot thresholds must be
evidence-backed or explicitly recorded as `unavailable_pending_measurement`.

## Reproducible aggregate command

```bash
uv run --project services/api python -m src.sales_prediction_discovery \
  --as-of-at 2026-08-01T00:00:00Z \
  --report-cutoff-at 2026-08-05T00:00:00Z \
  --entities fundbox \
  --configuration-version issue-124-crm-won-v1 \
  --json-output /secure/output/issue-124-fundbox.json \
  --markdown-output /secure/output/issue-124-fundbox.md
```

The optional `--stage-mapping` input is discovery-only. Draft or unreferenced
mappings may be supplied to measure policy completeness, but they always remain
`mapping_approval_unverified` and cannot enable labels. Even an externally
referenced mapping does not enable labels by itself; authoritative transition
history and verification outside this local artifact are also required.
