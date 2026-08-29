# Profile Unifier - Deal Intelligence Platform Operations

## Purpose and operational boundary

This runbook applies to the disabled PostgreSQL 16 Deal Intelligence foundation
introduced by issue #315. It covers only the shared source-instance, readiness,
run/unit, checkpoint, lease/fence, terminal-accounting, and migration records. It
does not authorize live source synchronization, historical import, identity/deal/
stage/activity storage, artifacts, projections/outbox, ownership transfer,
credentials, browser ingress, nginx routing, or deployment changes.

The default registry is empty and every writer and schedule is disabled. Do not
turn on a writer, scheduler, source instance, or external integration under this
runbook. A future issue must define the bounded responsibility, configuration,
review gate, and rollback evidence first.

## Processes and health

The package entry points are deliberately separate:

```text
deal-intelligence-api
deal-intelligence-worker
deal-intelligence-scheduler
deal-intelligence-health
deal-intelligence-migrate
```

The API, worker, and scheduler are internal operational processes, not public
application services. Health/readiness is container-internal only: it has no host
port, nginx route, browser/BFF route, public endpoint, or MCP exposure.
`deal-intelligence-health --component api|worker|scheduler` returns secret-safe
structured disabled readiness only after validating the schema and a fresh heartbeat
for the selected component. In the default state, API/worker/scheduler probes must
confirm that no writer or schedule is active. Worker and scheduler commands are
long-lived, signal-aware disabled heartbeat loops; one-cycle seams are source-test
helpers, not short-lived production commands.

## Configuration and storage

Use runtime secret injection for PostgreSQL URLs, backup encryption keys, and any
future CRM credentials. Never put a secret in repository configuration, logs,
health output, source-instance identifiers, migration revisions, manifests, or
command history. Source-instance records contain only non-secret references and
control metadata.

Production PostgreSQL data is persistent and host-bound; do not substitute a
container-local anonymous volume for the durable data location. CI is different:
the Woodpecker PostgreSQL 16 service is disposable and contains only generated
test data.

## Schema releases

Run migrations explicitly; no process startup performs them:

```text
uv run --package profile-unifier-deal-intelligence deal-intelligence-migrate upgrade heads
```

Maintain additive Alembic revisions with independent future component lanes branching
from the platform by default. A real extra dependency belongs to the future revision
that needs it, never to blanket sibling serialization. Each release must have one
unambiguous head across maintained branches. If reviewed migration branches converge,
create and review an Alembic merge revision before release rather than choosing one
head operationally. PostgreSQL extensions also require an explicit reviewed additive
migration; startup must not create or upgrade an extension. Confirm schema inventory
and readiness after migration. Never use this procedure for live data movement; this
foundation has no live execution path.

## Backup, restore, and retention

Back up the persistent PostgreSQL database as an encrypted custom-format `pg_dump`:
create the custom-format dump, then encrypt it with a managed key outside the backup
set. PostgreSQL custom format is not encryption by itself. Store the encrypted dump, manifest, and
integrity data together, but keep keys out of the repository, dump, manifest, and
logs.

Every backup set requires a restore verification to an isolated target, including
successful decryption, `pg_restore` completion, and schema/readiness verification.
Retain **fourteen daily** sets and **eight weekly** sets. The operational objective
is an **RPO of 24 hours** and an **RTO of 4 hours**. A restore test validates only
the disabled control plane; it must not enable workers, schedules, or any live CRM
operation.

## CI contract

Synchronized PR and MAIN Woodpecker steps use a PostgreSQL 16 service and frozen
installs to run the Deal Intelligence package suite after lint, formatting, and
strict typing. PostgreSQL integration tests are package-owned and opt in only when
`HYPERP_DEAL_INTELLIGENCE_TEST_DATABASE_URL` is supplied. They reject obvious
non-test/default targets and create unique test-owned state; the CI value is
disposable and never a production credential. The suite covers fresh and
base-to-head migrations, schema inventory, component heartbeats, and structured
disabled readiness. MAIN additionally performs the frozen no-development install.
