# Profile Unifier CRM Activities Archive

## Scope

The Intelligence CRM activities archive is a bounded, default-off, CLI-only capability that preserves the selected existing Neo4j activity population. It is not a Bitrix synchronization, census, backfill, completeness claim, cleanup command, or live runbook. Live quiescence and extraction authorization belong to the follow-up operational workflow.

```text
intelligence crm activities extract --snapshot-id CHECKPOINT_ID
intelligence crm activities resume --snapshot-id CHECKPOINT_ID
intelligence crm activities verify --checkpoint-id CHECKPOINT_ID [--accepted-run-id RUN_ID]
intelligence crm activities status --snapshot-id CHECKPOINT_ID
```

`extract`, `resume`, and `verify` are bounded mutating Intelligence jobs and remain blocked while `INTELLIGENCE_MUTATIONS_ENABLED=false`. `status` performs only bounded read-only checkpoint, State, descriptor, and candidate-history admission. The global `PRODUCTION_REGISTRY` remains empty; the adapter builds one reviewed request-scoped registry for the requested command.

## Selection and privacy boundary

The source is one configured `source_instance_id` and `bitrix_chat` source key by default. Activity selection is closed: canonical `history_family=activity`, untyped legacy null family, and only the evidenced legacy Bitrix alias (`crm_activity`, `bitrix_crm_activity`, projection source `bitrix_crm_activity_v1`, versions `1` and `2`). Unknown/future families and versions, and all stage history, are excluded.

Companion calls must have exactly one same-instance activity `CHILD_OF` parent and one matching `DETAILS_HISTORY_ITEM` activity parent. Ambiguous, missing, cross-instance, conflicting, or non-accepted parents quarantine the call and exclude it from cleanup identity output.

The repository has parameterized Neo4j read sessions only. It projects immutable source/version/hash provenance; lifecycle and activity projection fields; stored parent source-system/instance/record references; direct graph Person UUID/status/revision association references; and distinct event, observed, ingested, and availability timestamps. Availability uses a dedicated availability field when present and otherwise preserves `ingested_at` as the first-known availability value. It does **not** parse or project raw payload, deal payloads, conversation references, message/comment/transcript content, identifiers, candidates, scores, locks, matching evidence, or topology. Missing parent/Person/user-capability evidence remains observable in safe unresolved-reference artifacts; malformed Person associations are counted without inventing a Person or aborting the selected source record.

## Boundary, replay, and acceptance

Before artifact paging, extraction performs bounded keyset enumeration and writes immutable record evidence beneath `staging/.crm-activities/SNAPSHOT_ID/`. It seals source identity, record digest, and approved-reference fingerprint into a boundary. Identical source deliveries are grouped before the keyset limit, counted in separate immutable checkpoint evidence, and remain page-size independent; conflicting same-identity records fail closed. Every output page is reread by sealed identities and compared to that boundary. A separate complete re-enumeration occurs before acceptance. Additions, removals, content changes, reference changes, or duplicate-count drift fail closed.

Before either boundary capture or final boundary verification, a separate bounded read-only preflight checks the same closed activity/call candidate scope for missing or blank source/version/hash identity fields. A nonzero count is structural boundary corruption: acceptance fails before snapshot publication, the records do not enter the immutable selected identity set, and they never create cleanup authorization.

The same preflight boundary rejects, without truncation or graph mutation, any admitted record whose `CHILD_OF`, `DETAILS_HISTORY_ITEM`, or active Person `LINKED_TO` reference cardinality exceeds the configured per-record ceiling. Parent edges remain observable even if their parent lacks `FROM_SOURCE`; the separately projected `source_system` value then records missing source-system evidence explicitly rather than hiding the edge.

Every sealed identity has exactly one `accepted`, `rejected`, or `quarantined` disposition. The final manifest proves the disjoint partition and records a zero unexplained remainder. Unknown lifecycle values are rejected; missing optional parent/identity/user references do not invent a relationship.

Accepted output is published only through the Intelligence runtime at `outputs/RUN_ID/snapshots/crm/activities/SNAPSHOT_ID/`. Logical snapshot/page/manifest digests use canonical JSON and exclude run IDs, attempts, and timing. A replay regenerates equivalent logical artifacts or fails on conflicting immutable evidence. Each accepted manifest declares `source_population=neo4j_existing_records`, `completeness=legacy_partial_snapshot`, and `bitrix_completeness_asserted=false`.

`cleanup-identities.json` is sorted and unique by `source_record_pk`, contains only accepted records, and is linked by count and digest from the accepted domain manifest. The supervised archive handler writes only an immutable per-attempt publication pointer; caller replay/status admission accepts it only when its registered descriptor and exact State output inventory prove a completed run. Failed or pending attempts remain history and never erase a completed result. It is later cleanup authorization evidence; runtime completion is not cleanup authorization.

## Limits and recovery

The hidden checkpoint directory has independent byte and entry limits because it is outside the foundation current-run staging scan. It rejects traversal, symlinks, hard links, non-regular files, conflicting immutable writes, corrupt JSON, unsupported state, and boundary/request mismatches. Cursor/page progress advances only after durable page evidence.

Use `resume` only after the foundation active-run/stale-recovery safeguards have established a safe execution domain. `verify` resolves the checkpoint's State-proven publication descriptor, derives the accepted run and logical snapshot, validates artifact/schema/inventory/partition/provenance/cleanup linkage, and writes separate verification evidence into its own current run output. A verification candidate is accepted only when a completed `crm_activities_verify` run has registered the exact descriptor-bound verification artifact; it never changes the accepted snapshot or requires retained Neo4j records.


## Manifest-gated cleanup capability

This is a disabled-by-default, CLI-only capability. It performs no live extraction or deletion when
installed. Dry run freezes a logical `--cleanup-run-id` and bounded `--batch-size` into one canonical
State-registered receipt. Execute, resume, and verify require that exact cleanup ID plus receipt run
and digest; they cannot override the frozen batch size. Status reads only bounded local checkpoint
evidence.

```text
intelligence crm activities cleanup dry-run AUTH TARGET --cleanup-run-id ID --batch-size N
intelligence crm activities cleanup execute AUTH TARGET RECEIPT --cleanup-run-id ID
intelligence crm activities cleanup resume AUTH TARGET RECEIPT --cleanup-run-id ID
intelligence crm activities cleanup verify AUTH TARGET RECEIPT --cleanup-run-id ID
intelligence crm activities cleanup status --cleanup-run-id ID
```

`INTELLIGENCE_MUTATIONS_ENABLED=true` is required for dry-run and verify because they publish
Intelligence evidence, although their graph access is read-only. Execute and resume additionally
require default-false `INTELLIGENCE_CRM_ACTIVITY_CLEANUP_ENABLED=true` and matching
`INTELLIGENCE_ENVIRONMENT_ID`. Exact PK inspection exposes drift rather than treating it as absence.
Only closed, enumerated ownership may be deleted; no broad graph scan, interpolation, neighboring-node
delete, or `DETACH DELETE` is available. Persistent checkpoint evidence records attempts, batch intents,
results, recovery reconciliation, and a balanced terminal partition. Verification proves selected absence
and protected-baseline preservation without changing the accepted archive.

Cleanup dry-run, execute, resume, and verify require a completed State-registered operational
quiescence artifact produced by the separate live archive acceptance workflow. The artifact binds
the accepted run, checkpoint, logical snapshot, manifest, cleanup identity, source key and
instance, environment, database identity, and frozen boundary. Graph write locks are
defense-in-depth only: they do not prove that a writer cannot create an absent identity. Protected
preservation evidence covers only the finite, exact endpoints and relationships recorded in the
receipt; it is not a graph-wide class-preservation proof.
