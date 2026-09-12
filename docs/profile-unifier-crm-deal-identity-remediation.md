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

### Read-only status boundary sizing (issue #422)

`python -m src.crm_deal_identity_repair_control status` is the canonical
staging-only, read-only admission check. It is intentionally separate from
`apply`, canary execution, dispatch release, and every #314 repair mutation.
The status path must remain able to check the qualified 178,328-row boundary
(178,322 eligible units and six negative controls) within the existing 2 GiB
`ingestion-worker` memory limit; no Compose memory increase is part of this
contract.

Status discovers every active CRM-deal source record with 100-row keyset pages,
validates the global invalid-PK guard, and reads only the matching projection
branches for each page. It consumes a result before issuing the next query,
classifies and serializes each row immediately, and compares observed PKs to
the immutable qualified PK sequence incrementally. It never restricts graph
discovery to that stored sequence, so additions, deletions, duplicates, and
non-advancing cursors remain boundary drift.

Canonical inventory JSONL and unordered control/stale evidence are sorted in a
private process-temporary SQLite scratch directory with an 8 MiB SQLite page
cache. The scratch holds only derived canonical bytes and byte sort keys; it is
not an artifact, is never logged, is not shared or persistent, and is removed
on success, error, and managed-transaction retry. Source-record digest slices
are also bounded to 100 PKs and stream the exact legacy `{\"rows\":[...]}`
canonical bytes. The retained qualified scalar PK tuple is the intentional
O(N) compatibility boundary; status retains no O(total payload/evidence)
representation.

The CI regression benchmark uses a lazy 178,328-row transaction fixture and
eager-retention traps. It enforces a tight Python-allocation guard and, where
`resource` is available, an absolute process maximum-RSS ceiling below 2 GiB.
Runtime remains visible through the ordinary CI step duration; the test does
not emit a separate metric.
The benchmark also verifies notification suppression is scoped to `status`,
restores the prior `neo4j.notifications` level, keeps ERROR notifications
visible, and leaves stdout as one complete JSON document.

After merge and staging deployment, #314 still requires its own canonical
status rerun to record valid JSON and confirm 178,322 allocated / 0 mutations.
That operational gate does not authorize apply or canary execution.

### Allocation stored-set verification (issue #409)

The `ALLOCATE_REPAIR_UNITS` query no longer materializes every stored
`CrmDealRepairUnit` node in transaction memory.  It instead verifies the stored
set with a bounded `COUNT` over all units for the run plus an existence probe
for each expected `unit_id`. This preserves fail-closed semantics: exactly
`unit_count` stored units and every allocated `unit_id` present, so an
unexpected unit cannot be ignored. This avoids the O(N) memory growth that caused the
original 178k-unit staging allocation to exhaust Neo4j's transaction-total
cap.

### Staging transaction/memory sizing

The first staging allocation of 178,322 units exceeded the default Neo4j
transaction-total memory cap of 716.8 MiB after several hours. Repository and
issue evidence confirms that a later staging allocation completed after larger
settings were applied, but does not record the exact successful values.
Production defaults and the root `docker-compose.yml` remain unchanged. Before
the post-deploy 178k run, operators must measure and record the target Neo4j
transaction-memory settings and observed headroom; this bounded stored-set
reduction is not evidence of a specific safe memory value.

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
  does not rewind the checkpoint.  A receipt stored out of order is a no-op;
  the checkpoint stalls at the gap until that unit's idempotent receipt
  re-store runs (unreachable in the strictly-sequential runtime; recoverable
  via replay).

- `admission_checkpoint_blocked` — set to `true` by
  `PERSIST_ROLLBACK_TERMINAL` on every terminal rollback.
  While this flag is present, `CLAIM_ADMITTED_FENCE` rejects every fresh
  admission regardless of `settled_sequence`.

A fresh admission (`unit.state = 'allocated'`, no existing fence) now
requires only:

```text
completion.admission_checkpoint_blocked IS NULL
AND coalesce(completion.settled_sequence, 0) = $sequence
```

`CLAIM_ADMITTED_FENCE` and `LOCK_AND_READ_ROLLBACK_BUNDLE` first take the same
serialization write lock on the unique per-run `CrmDealRepairControl` node
(`integration_admission_updated_at`). This serializes every admission against
every rollback before either query locks a unit or evaluates the checkpoint.
If rollback reaches terminal persistence, it sets `admission_checkpoint_blocked`
before releasing the common lock; if it aborts, its writes roll back and the
waiting admission can proceed.

The replay branch (one exact existing fence) is unchanged.

### Checkpoint residual

The checkpoint protocol replaces the previous per-admission exact-chain walk
over every prior unit.  The residual difference is that a post-settle *manual*
deletion of an older unit's ledger nodes is no longer detected at admission.
No production or repair code path deletes those nodes, so the residual is
acceptable and documented here.
