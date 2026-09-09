# Profile Unifier Intelligence Datasets

## Scope

`intelligence dataset` builds reviewed, immutable CRM deal-state datasets only from the
accepted #353 CRM deal-reference snapshot and the accepted #354 legacy CRM-activity archive.
It neither connects to Neo4j or Bitrix nor claims that archived activities represent complete
Bitrix history. Dataset generation remains default-off and no live build is part of this change.

```text
intelligence dataset build --deal-refs-run-id RUN_ID --activities-checkpoint-id CHECKPOINT_ID \
  --activities-accepted-run-id RUN_ID --definition crm-deal-state-v1 \
  --feature-cutoff ISO8601 --label-cutoff ISO8601 --seed INTEGER
intelligence dataset verify DATASET_ID --accepted-run-id RUN_ID
intelligence dataset list [--limit N] [--after RUN_ID]
intelligence dataset inspect DATASET_ID --accepted-run-id RUN_ID
```

Build requires `feature_cutoff < label_cutoff <= deal boundary.as_of`. The only definition is
`crm-deal-state-v1`; its seed is included in immutable configuration and lineage even though this
version is deterministic.

## Admission, selection, and missingness

Admission is State-backed. Deal inputs must be completed `crm_deal_refs_extract` or
`crm_deal_refs_resume` outputs with an exact registered inventory and the #353 schema-2,
`crm-deal-refs-v2`, `crm_deal_identity_v2`, and `bitrix_chat` contracts. Activity inputs must be
the State-proven accepted publication for the pinned checkpoint/run, with an exact descriptor and
inventory using the reviewed #354 manifest, boundary, page, and selection contracts. Source
instance and source namespace must match.

Activities join only by a complete stored source parent for the same `bitrix_chat` instance and
deal **source record ID**. The dataset maintains a strict, unique source-record-ID →
source-entity-ID lineage index, so `bitrix-crm-deal-42` and entity ID `42` are related without
numeric normalization; an optional source-record PK must agree. A missing graph corroboration can
remain valid, but graph-only parent evidence, multiple deal candidates, and any present `CHILD_OF`
deal contradiction of stored source system, instance, ID, type, or PK are excluded with a stable
join reason code. Person associations and graph repair are never used to infer a join. Calls require
a valid accepted activity-parent chain. Rejected, quarantined, temporal, and join reason codes are
persisted in dispositions rather than silently disappearing.

Rows are one logical source deal. Feature state is selected at `T`; label state is selected
independently at `H`. Required availability, observation, event/effective, and applicable close
date evidence must all be valid and known by the relevant cutoff. A tied latest authoritative
version is censored; source-record PK is serialization-only. `P`, `S`, and `F` map to `open`,
`won`, and `lost`; missing or unknown state is censored.

Identity linkage uses only #353 immutable identity revisions that were both effective and known at
`T`. A newer non-resolved revision prevents fallback to an older resolution. #354 Person evidence
is never historical identity linkage and Person linkage is metadata, not a feature.

The row retains non-feature provenance separately from the feature allowlist: selected identity
revision/event, horizon source-version identity and temporal evidence, horizon staleness, and
activity join corroboration. These fields support audit and replay but are not model features.

Activity counts are lower bounds from the accepted legacy partial archive. If no eligible archived
activity evidence exists for a deal at `T`, activity counts and recency are null with an explicit
missingness reason, never zero. Every manifest preserves `neo4j_existing_records`,
`legacy_partial_snapshot`, and `bitrix_completeness_asserted=false` provenance.

## Immutable outputs and replay

Build writes only current-run staging and publishes atomically through the foundation runtime:

```text
outputs/<run>/datasets/<dataset-id>/{manifest,definition,config,schema}.json
outputs/<run>/datasets/<dataset-id>/{rows,dispositions}.ndjson
outputs/<run>/acceptance-descriptors/datasets/<dataset-id>.json
```

Canonical UTF-8 JSON/NDJSON, LF rows, deterministic ordering, UTC timestamps, and integer
durations determine logical content. Dataset ID derives from that digest; run ID, wall-clock time,
host paths, and attempt metadata do not. The exact configuration carries both `code_version` and a
reviewed runtime source fingerprint over normalized `datasets/*.py` bytes, definition, cutoffs,
seed, and exact input pins; descriptor and
manifest readers reject arbitrary configuration fields. The descriptor binds the request, dataset,
manifest, exact dataset inventory, pinned inputs, and producing run. Catalog and artifact reads
charge fixed entry, file, byte, and row budgets before hashing or parsing and validate State-registered
byte counts first. A matching request with different accepted content fails closed.
