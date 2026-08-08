# CRM History Authority Contract

## Status

Issue #146 establishes the typed storage and authority contract. As of **August
7, 2026**, Bitrix stage-history traversal remains `unsupported`; this document
does not authorize source access, stage ingestion, checkpoints, or a backfill.

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

A future stage observation has immutable identity `(source_contract_uuid,
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
