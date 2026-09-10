# Profile Unifier Intelligence Models

## Scope and safe default

The `intelligence train`, `evaluate`, and `model` controls are offline, default-off operator
workflows. They consume only a State-accepted `crm-deal-state-v1` dataset produced by
`intelligence dataset build`. They do not connect to Neo4j or Bitrix, score production traffic,
activate a candidate, write predictions, alter extraction/cleanup, or schedule work.

```text
intelligence train run --dataset-id ID --accepted-run-id RUN --recipe categorical_frequency_v1 --seed N
intelligence evaluate run --model-id ID --model-run-id RUN --dataset-id ID --accepted-run-id RUN
intelligence model inspect MODEL_ID --accepted-run-id RUN
intelligence model verify --model-id ID --model-run-id RUN
```

Mutations use request-scoped reviewed `RegisteredCommand` instances; the production registry stays
empty. `INTELLIGENCE_MUTATIONS_ENABLED=false` remains the default.

## Dataset and partial-coverage contract

Admission requires the exact producing dataset run and ID, completed State registration, descriptor,
manifest, schema/config compatibility, registered checksums, row count, and the immutable activity
provenance `neo4j_existing_records` / `legacy_partial_snapshot` /
`bitrix_completeness_asserted=false`. Each consumed row must carry the same coverage class.

At parent-side command admission, the durable secret-free provenance binds the dataset and content
digests plus the exact #356 source pins: deal-reference run/boundary/inventory/manifest and activity
checkpoint/accepted-run/logical-snapshot/boundary/descriptor/inventory/manifest. Child work repeats
artifact-only admission before publication and rejects drift.

The initial recipe explicitly tolerates this partial coverage. It uses only point-in-time categorical
fields (`category_id`, `stage_id`, `stage_semantic_id`, and activity missingness reason). It does **not** use activity-count magnitudes, never treats null as zero, and never
interprets archived lower bounds as complete counts. Recipes with incompatible coverage or missingness
contracts fail before execution.

## Candidate and evaluation semantics

`categorical_frequency_v1` is a dependency-free deterministic baseline, not a production-quality
model. It uses canonical feature keys, fixed class ordering (`lost`, `open`, `won`), deterministic
frequency/tie selection, and a global fallback. A seeded SHA-256 person-group split keeps all rows for
one Person in a single partition; Person identity is split/audit metadata, never a feature. Training
requires a non-empty held-out partition and publishes mandatory held-out evaluation.

Evaluation records fixed-order confusion counts, accuracy, and per-class precision/recall. Every
ratio records its denominator and `zero_denominator` instead of inventing a value. Independent
evaluation reuses only the candidate held-out membership and rejects any training/evaluation overlap.
Comparison is permitted only where population, labels, and metric contracts match; incompatibility is
an explicit rejection, not an aggregate from unlike populations.

The independent evaluation selects its held-out rows by the same domain-separated source-identity
membership hashes persisted by training; raw Person IDs, deal IDs, source-record IDs, and source
instance values are not included in population or evaluation membership artifacts.

## Immutable artifacts and limits

Only canonical UTF-8 JSON is accepted in v1. A training publication contains:

```text
outputs/<run>/models/<model-id>/candidate.json
outputs/<run>/models/<model-id>/population.ndjson
outputs/<run>/models/<model-id>/missingness.json
outputs/<run>/evaluations/<evaluation-id>/evaluation.json
outputs/<run>/acceptance-descriptors/models/<model-id>.json
outputs/<run>/acceptance-descriptors/evaluations/<evaluation-id>.json
outputs/<comparison-run>/comparisons/<comparison-id>/comparison.json
outputs/<comparison-run>/acceptance-descriptors/comparisons/<comparison-id>.json
```

Candidates are always `active=false`. Artifact IDs derive from canonical logical content; timestamps
and attempt IDs are excluded. Files are staged then atomically published through the foundation's
no-overwrite inventory protocol. Unsafe links, extra files, pickle/joblib, archives, malformed JSON,
checksum drift, failed/cancelled/timed-out runs, and unregistered output cannot become candidates.

The reviewed child applies CPU and address-space caps on POSIX before materializing data. Foundation
elapsed-time, cancellation, process-group cleanup, output-byte/entry, and bounded log controls remain
authoritative; unavailable OS resource enforcement fails closed rather than falling back unbounded.
Effective limits and request provenance are persisted at run admission and retained for terminal
inspection, recovery, and backup evidence.

`model list` and `model inspect` read only fully State-registered candidate bundles. Their catalog
requires the exact completed training run, descriptor, candidate, population, missingness, and held-out
evaluation inventory; missing, extra, linked, noncanonical, or checksum-invalid evidence is rejected.
