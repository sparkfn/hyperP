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

- canonical `inventory.jsonl`;
- measurable impact counts and condition equations;
- a bounded deterministic list of representative inventory IDs;
- descriptive compensation/rollback guidance with expected-before multiplicity
  and no planned execution value;
- stale-run graph/control evidence; and
- a clean-boundary checklist for #255.

Every emitted row and plan sets `execution_allowed: false`; no artifact contains
runnable Cypher or an approval/execution state.
