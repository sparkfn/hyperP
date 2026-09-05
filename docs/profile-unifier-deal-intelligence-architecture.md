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
handler runs only in a parent-supervised spawned child process: the parent enforces cancellation
and the configured runtime bound by terminating that child. No executable, shell input, plugin
discovery, raw environment, or arbitrary arguments are admitted or persisted.

Mutations are disabled by default. An active healthy lock cannot be recovered. A stale lock makes
health fail until an operator explicitly supplies its exact run identifier and recovery reason.
Terminal records remain durable; no cleanup, pruning, or restore is included.

## Artifact and backup contract

Outputs originate in private workspace staging, have bounded size, are checksummed, and publish to
an immutable no-replace path in the same volume. Terminal manifests are canonical JSON with schema,
timestamps, effective safe limits, reason, output inventory, and a checksummed run-log reference;
they never contain credentials or raw environment. A backup is an atomic no-replace bundle holding
an SQLite online snapshot plus copies and checksummed inventory of completed-run manifests and
accepted output evidence. Operators must verify and export a bundle off-volume by an approved
external process.

## Deployment contract

The always-started container is non-root, has no ports or dependencies, and runs idle with a CLI
healthcheck. Root and staging Compose contain the same sole service and sole volume subject only to
the existing staging build-context rebase. Stopping Intelligence has no effect on normal HyperP.

## Explicit exclusions

No domain extraction/jobs, datasets, training, evaluation, schedules, live execution, model
activation, plugin framework, generic remote code execution, database sidecar, destructive cleanup,
or operational ownership transfer belongs to this foundation.
