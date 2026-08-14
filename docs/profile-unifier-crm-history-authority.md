# CRM History Authority Contract

## Status

Issue #146 establishes the typed storage and authority contract. As of **August
14, 2026**, issue #147 implements a separately authorized, default-off bounded
smoke path. It does not authorize a full traversal, recurring schedule,
analytical consumption, mapping, or historical backfill; those remain #148.

## Bounded capture and source-free replay

The #147 smoke path has two fail-closed phases:

1. `collect-smoke` verifies the accepted restricted owner/stage artifacts,
   authorization expiry, deployed provenance, and mandatory call/row/runtime/
   storage limits before the first Bitrix request. It traverses only the
   deterministic artifact-derived history-ID range and writes no graph state.
2. `dispatch-smoke` re-verifies a sealed `stage-ingestion` artifact and replays
   its immutable pages without making Bitrix calls. A retained
   `stage-ingestion-failed` artifact may only use `record-capture-failure`,
   which creates terminal accounting but no stage SourceRecord, variant,
   association, authority, or invalidation state.

Each replay page commits occurrences, immutable evidence, parent decisions,
authority transitions, invalidation intents, unit accounting, and checkpoint
CAS under one active `crm_stage_history` fence transaction. Worker redelivery
reconstructs the exact checkpoint from the committed unit ledger. A stale or
replaced fence cannot mutate domain state or advance the checkpoint.

The operator surface is intentionally manual and aggregate-only:
`collect-smoke`, `dispatch-smoke`, `record-capture-failure`, `status`,
`request-stop`, `resume`, `resolve-parents`, `reject-parent`,
`resolve-conflict`, `apply-correction`, and `reconcile`. There is no Beat entry
and no full-backfill command in #147.

## Current stage-catalog semantic boundary

The read-only #145 capability re-gate validates current deal stages through
`crm.status.list` before it freezes a deal or stage-history boundary. Bitrix
returns two compatible semantic representations: top-level `SEMANTICS` is a
compact lifecycle class (`S`, `F`, or null), while `EXTRA.SEMANTICS` is the
descriptive source value. The descriptive value is retained when present:

| `SEMANTICS` | `EXTRA.SEMANTICS` | retained value |
| --- | --- | --- |
| null | `process` | `process` |
| `S` | `success` | `success` |
| `F` | `failure` | `failure` |
| `F` | `apology` | `apology` |

Rows with incompatible, malformed, or unknown values fail closed. If the
descriptive value is absent, a compact `S` or `F` is normalized to `success` or
`failure`; both fields absent remain unknown. A catalog-preflight failure writes
only a restricted redacted failure manifest and must not begin deal or global
stage-history traversal.

## Typed source-record projection

`SourceRecord(record_type='crm_history')` may carry the following first-class
properties: `history_family`, `history_kind`, `history_source`, category/stage
identifiers, `event_at`, and projection lineage/version timestamps. Existing
generic activities are the `activity` family. Readers include only legacy-null
or exactly `activity` values; unknown/future families are excluded by default.

## Stage authority

A stage observation has immutable identity `(source_contract_uuid,
entity_type_id, history_id)` using `bitrix-crm-stagehistory-v1`, plus canonical
hash `bitrix-stage-history-v1`. It must never inherit historical `CHILD_OF`,
`OWNED_BY`, `LINKED_TO`, `DETAILS_HISTORY_ITEM`, or
`REPRESENTS_HISTORY_ITEM` relationships.

Authority consists of an immutable conflict group, immutable hash variants,
append-only decisions, and a CAS-only head. Authority appends require the
active logical run and generation in the same transaction and compare both
head version and fence token. Corrections are later restatement decisions;
they do not rewrite an earlier as-known decision.

## Access and projections

Stage access must resolve the current unambiguous logical deal parent and its
current owner, fail closed on ambiguity, and never rely on historical ownership
edges. Generic graph traversal excludes both stage records and authority nodes;
stored report execution is administrator-only because arbitrary Cypher cannot
be reliably tenant-row-scoped.

The legacy generic-activity projection migration is deliberately unregistered.
When explicitly enabled after #145, it is lease/CAS coordinated, resumable, and
reversible only for properties it marked and whose expected values still match.

## Analytical disable boundary

Stage evidence remains `history_family='stage'`, `pending_review`, and
`is_latest=false`. Existing person, entity, source-record, timeline, review,
graph explorer, profile-analysis, sales-prediction, activity, call,
conversation, and Open Lines readers admit only legacy-null or exactly
`history_family='activity'`. Unknown families fail closed. #147 persists
`crm_stage_timeline` invalidation intents but does not consume them. #148 owns
the analytical selector, mapping approval, outbox consumption/rebuild, full
backfill, recurring scheduling, and release.
