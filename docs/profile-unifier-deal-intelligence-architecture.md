# Profile Unifier Intelligence Runtime Architecture

## Status and scope

Issue #352 replaces the obsolete Deal Intelligence PostgreSQL, API, worker, scheduler, migration,
and ownership-transfer proposal. Intelligence is deliberately one Python package, one Compose
application container, and one persistent named volume, `intelligence-data` mounted at
`/var/lib/intelligence`.

## Boundary

Intelligence is an agent-operated workspace for future reviewed immutable artifacts only. Neo4j
remains the operational authority. The foundation does not read Neo4j, call Bitrix, expose HTTP,
add nginx/BFF/MCP routes, use a sidecar, add domain extraction/jobs, datasets, training,
evaluation, schedules, or live execution, or change HyperP API/frontend/ingestion behavior.

## Runtime contract

The `intelligence` CLI owns a WAL-mode SQLite database, schema version, runs, cancellation,
accepted-output registration, a durable exclusive mutation lock, heartbeat, monotonically
increasing fence, terminal manifests, and backups. A production registry is empty. A reviewed
handler runs only in a parent-supervised spawned child process/session: the parent enforces
cancellation, aggregate staging limits, and the configured runtime bound by terminating the whole
group. Raw child stdout/stderr is suppressed. No executable, shell input, plugin
discovery, raw environment, or arbitrary arguments are admitted or persisted.

Mutations are disabled by default. An active healthy lock cannot be recovered. A stale lock makes
health fail until an operator explicitly supplies its exact run identifier and recovery reason.
If process-group cleanup is unresolved, health fails immediately and stale recovery requires a
different trusted container execution epoch; same-container CLI execs cannot bypass that fence.
Terminal records remain durable; no cleanup, pruning, or restore is included.

## Artifact and backup contract

Outputs originate in private workspace staging, have bounded size, are checksummed, and publish to
an immutable no-replace path in the same volume. Terminal manifests are canonical JSON with schema,
timestamps, effective safe limits, reason, output inventory, and a checksummed run-log reference;
they never contain credentials or raw environment. A backup is an atomic no-replace bundle holding
an SQLite online snapshot plus copies and checksummed inventory of completed-run manifests and
accepted output evidence. The bundle format is versioned independently from the live SQLite
schema and verifies legacy schema-4 snapshots without `limits_json`. Operators must verify and
export a bundle off-volume by an approved external process.

## CRM deal-reference export boundary

#353 adds a bounded read-only Neo4j-to-Intelligence export of immutable CRM deal-version references.
Neo4j remains authoritative for CRM deals, counts, active/current projections, stage semantics, lifecycle
decisions, and identity resolution. Intelligence owns only accepted immutable evidence under
`snapshots/crm/deal-refs/`; it creates neither a current-deal table nor replacement authority.

The domain selects all in-scope `SourceRecord(record_type='crm_deal')` versions for one active
`bitrix_chat` source instance whose first-known availability is at or before the cutoff, plus legacy
versions whose availability is unknown. Unknown or future source facts remain explicit and are not
point-in-time eligible. Rows are ordered by source identity, numeric version, and source-record PK; the
export does not select only latest versions or Person-linked deals. The sealed descriptor records membership, terminal
key, identity global-revision ceiling, and immutable-fact, identity-revision, and capture-observation
fingerprints. Keyset pages, durable page manifests, SHA-256 checksums, and a checkpoint are written before
foundation publication. Identical replay is safe; conflicting duplicate evidence or observed drift fails
closed.

The allowlist is source instance/version identity, entity/category, stage/outcome references,
observed/ingested and source-created/updated/close-date values, plus capture-time lifecycle/link
observations. Bounded raw payload JSON is decoded only in memory for that projection and discarded;
titles, money, contact groups, matching candidates/evidence, scores, locks, and arbitrary payloads are not
persisted. A source close date remains a source field, not proof of a closure transition. Stage families,
stage mapping, lifecycle derivation, and outcome inference are excluded.

`SourceRecord.ingested_at` is first-known availability. `observed_at` and source timestamps retain their
source meanings and are never substituted for availability. Identity `effective_at` is preserved, while
identity `created_at` is first-known availability. Missing availability remains unknown and cannot be
represented as existing by `--as-of`. Lifecycle/eligibility are mutable capture-time observations, not
historical authority. Only validated immutable `resolved` identity evidence whose first-known and
effective times are both at or before the cutoff may contain a Person UUID and capture-time Person status;
unresolved, pending, blocked, rejected, retired, and unlinked records remain Person-unlinked.

The correction-cycle schema separates immutable source/identity fact fingerprints from mutable lifecycle,
link, and observed-Person-status fingerprints. Reconciliation reuses the persisted identity revision ceiling
and rejects only a counter that falls behind it; unrelated revisions created after that ceiling are outside
the frozen boundary. Resume validates and copies only a committed regular-file prefix into a new staging
tree, then appends after the persisted cursor; no existing page or manifest is overwritten. Completed
accepted replay is artifact-only, so later Neo4j state cannot invalidate accepted immutable output.

## Deployment contract

The always-started container is non-root, has no ports or dependencies, and runs idle with a CLI
healthcheck. Root and staging Compose contain the same sole service and sole volume subject only to
the existing staging build-context rebase. Stopping Intelligence has no effect on normal HyperP.

## Explicit exclusions

No domain extraction/jobs, datasets, training, evaluation, schedules, live execution, model
activation, plugin framework, generic remote code execution, database sidecar, destructive cleanup,
or operational ownership transfer belongs to this foundation.
