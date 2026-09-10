# CRM-deal identity repair graph inventory (#254)

## Scope

Issue #254 is a staging-only, **read-only Neo4j inventory** for historical
Bitrix CRM-deal identity evidence. It does not call Bitrix, dispatch ingestion,
write Neo4j, alter source records, approve an execution, or terminalize a run.
Any mutation belongs to the separate #255 execution issue.

The single operator entry point is:

```text
python -m src.crm_deal_identity_repair_control inventory \
  --repair-id <opaque-id> --source-contract-uuid <uuid>
```

## Collected evidence

The inventory reads every stored `crm_deal` version for `bitrix_chat`, including
pre-policy and `crm_deal_identity_v2` versions. For each version it captures:

- active and inactive direct `LINKED_TO` relationships and multiplicity;
- source-record-scoped identifier, fact, address, and legacy deal address
  projections;
- `crm_history`/call descendants and descendant owner evidence;
- persisted match decisions and review cases;
- no-match locks, pair-audit decisions, merge lineage, survivorship overrides,
  CRM-count, and golden-profile impact evidence; and
- graph/control evidence for stale run
  `e5deb1d6-7333-4660-be4f-c44fcf5af686`.

The graph query contains no mutation clauses. Unavailable worker or external
state is represented as stale-run `unknown`; graph evidence alone never claims
that an external task is absent.

## Classification and artifact

Classification uses stored graph payload/provenance only. Persisted legacy
policy is retained as pre-policy evidence; persisted v2 policy is review
evidence; malformed, conflicting, missing, or structurally ambiguous evidence
is investigate/cleanup evidence. The inventory does not infer ownership or
construct a replacement source envelope.

The restricted graph-discovery artifact contains only non-executable documents:

- canonical, contiguous `inventory-00001.jsonl` ... inventory parts, each bounded
  below the restricted store's per-file limit while one digest authenticates their
  exact logical concatenation;
- measurable impact counts and condition equations;
- a bounded deterministic list of representative inventory IDs;
- descriptive compensation/rollback guidance with expected-before multiplicity
  and no planned execution value;
- stale-run graph/control evidence; and
- a clean-boundary checklist for #255.

Every emitted row and plan sets `execution_allowed: false`; no artifact contains
runnable Cypher or an approval/execution state.

## Large-run allocation and admission checkpoint protocol

### Allocation stored-set verification (issue #409)

The `ALLOCATE_REPAIR_UNITS` query no longer materializes every stored
`CrmDealRepairUnit` node in transaction memory.  It instead verifies the stored
set with a bounded `COUNT` over the run's allocated `unit_id`s plus an
existence probe for each expected `unit_id`.  This preserves the same
fail-closed semantics (exactly `unit_count` stored units, every allocated
`unit_id` present) while avoiding the O(N) memory growth that caused the
original 178k-unit staging allocation to exhaust Neo4j's transaction-total
cap.

### Staging transaction/memory sizing

The first staging allocation of 178,322 units exceeded the default Neo4j
transaction-total memory cap of 716.8 MiB after several hours.  The staging
environment was provisioned with larger Neo4j memory settings to complete the
single allocation transaction.  Production defaults and the root
`docker-compose.yml` are intentionally unchanged; large allocations require
operational memory sizing in the target environment.

### Admission checkpoint protocol

To avoid an O(N²) predecessor scan on every unit admission, the run's
`CrmDealRepairAllocationCompletion` node carries two durable checkpoint
fields:

- `settled_sequence` — the next sequence whose prior units are fully settled.
  It starts unset (treated as `0`).  It advances monotonically to
  `sequence + 1` inside `STORE_ROLLBACK_RECEIPT`, but only after a bounded
  single-unit verification confirms the unit is in `applied`/`review_required`,
  has a claimed fence, a mutation result, a verified verification, an
  approved/consumable authorization, the matching rollback image, and the just-
  stored available receipt.  Re-issuing a receipt on an already-settled unit
  does not rewind the checkpoint.

- `admission_checkpoint_blocked` — set to `true` by
  `PERSIST_ROLLBACK_TERMINAL` when a previously settled unit is rolled back.
  While this flag is present, `CLAIM_ADMITTED_FENCE` rejects every fresh
  admission regardless of `settled_sequence`.

A fresh admission (`unit.state = 'allocated'`, no existing fence) now
requires only:

```text
completion.admission_checkpoint_blocked IS NULL
AND coalesce(completion.settled_sequence, 0) = $sequence
```

The replay branch (one exact existing fence) is unchanged.

### Checkpoint residual

The checkpoint protocol replaces the previous per-admission exact-chain walk
over every prior unit.  The residual difference is that a post-settle *manual*
deletion of an older unit's ledger nodes is no longer detected at admission.
No production or repair code path deletes those nodes, so the residual is
acceptable and documented here.
