# Profile Unifier CRM Activities Archive

## Scope

The Intelligence CRM activities archive is a bounded, default-off, CLI-only capability that preserves the selected existing Neo4j activity population. It is not a Bitrix synchronization, census, backfill, completeness claim, cleanup command, or live runbook. Live quiescence and extraction authorization belong to the follow-up operational workflow.

```text
intelligence crm activities extract --snapshot-id CHECKPOINT_ID
intelligence crm activities resume --snapshot-id CHECKPOINT_ID
intelligence crm activities verify --checkpoint-id CHECKPOINT_ID [--accepted-run-id RUN_ID]
intelligence crm activities status --snapshot-id CHECKPOINT_ID
```

`extract`, `resume`, and `verify` are bounded mutating Intelligence jobs and remain blocked while `INTELLIGENCE_MUTATIONS_ENABLED=false`. `status` only reads checkpoint evidence. The global `PRODUCTION_REGISTRY` remains empty; the adapter builds one reviewed request-scoped registry for the requested command.

## Selection and privacy boundary

The source is one configured `source_instance_id` and `bitrix_chat` source key by default. Activity selection is closed: canonical `history_family=activity`, untyped legacy null family, and only the evidenced legacy Bitrix alias (`crm_activity`, `bitrix_crm_activity`, projection source `bitrix_crm_activity_v1`, version `1`). Unknown/future families and versions, and all stage history, are excluded.

Companion calls must have exactly one same-instance activity `CHILD_OF` parent and one matching `DETAILS_HISTORY_ITEM` activity parent. Ambiguous, missing, cross-instance, conflicting, or non-accepted parents quarantine the call and exclude it from cleanup identity output.

The repository has parameterized Neo4j read sessions only. It projects immutable source/version/hash provenance; lifecycle and activity projection fields; stored parent source-system/instance/record references; direct graph Person UUID/status/revision association references; and distinct event, observed, ingested, and availability timestamps. Availability uses a dedicated availability field when present and otherwise preserves `ingested_at` as the first-known availability value. It does **not** parse or project raw payload, deal payloads, conversation references, message/comment/transcript content, identifiers, candidates, scores, locks, matching evidence, or topology. Missing parent/Person/user-capability evidence remains observable in safe unresolved-reference artifacts; it is never retried or inferred.

## Boundary, replay, and acceptance

Before artifact paging, extraction performs bounded keyset enumeration and writes immutable record evidence beneath `staging/.crm-activities/SNAPSHOT_ID/`. It seals source identity, record digest, and approved-reference fingerprint into a boundary. Every output page is reread by sealed identities and compared to that boundary. A separate complete re-enumeration occurs before acceptance. Additions, removals, content changes, or reference changes fail closed.

Before either boundary capture or final boundary verification, a separate bounded read-only preflight checks the same closed activity/call candidate scope for missing or blank source/version/hash identity fields. A nonzero count is structural boundary corruption: acceptance fails before snapshot publication, the records do not enter the immutable selected identity set, and they never create cleanup authorization.

Every sealed identity has exactly one `accepted`, `rejected`, or `quarantined` disposition. The final manifest proves the disjoint partition and records a zero unexplained remainder. Unknown lifecycle values are rejected; missing optional parent/identity/user references do not invent a relationship.

Accepted output is published only through the Intelligence runtime at `outputs/RUN_ID/snapshots/crm/activities/SNAPSHOT_ID/`. Logical snapshot/page/manifest digests use canonical JSON and exclude run IDs, attempts, and timing. A replay regenerates equivalent logical artifacts or fails on conflicting immutable evidence. Each accepted manifest declares `source_population=neo4j_existing_records`, `completeness=legacy_partial_snapshot`, and `bitrix_completeness_asserted=false`.

`cleanup-identities.json` is sorted and unique by `source_record_pk`, contains only accepted records, and is linked by count and digest from the accepted domain manifest. It is later cleanup authorization evidence; runtime completion is not cleanup authorization.

## Limits and recovery

The hidden checkpoint directory has independent byte and entry limits because it is outside the foundation current-run staging scan. It rejects traversal, symlinks, hard links, non-regular files, conflicting immutable writes, corrupt JSON, unsupported state, and boundary/request mismatches. Cursor/page progress advances only after durable page evidence.

Use `resume` only after the foundation active-run/stale-recovery safeguards have established a safe execution domain. `verify` reads the checkpoint acceptance linkage, derives the accepted run and logical snapshot, validates artifact/schema/inventory/partition/provenance/cleanup linkage, and writes separate verification evidence into its own current run output; it never changes the accepted snapshot or requires retained Neo4j records.
