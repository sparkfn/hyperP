# Profile Unifier Intelligence Operations

## Scope

Intelligence is one CLI-only container and one `intelligence-data` volume. It has no HTTP port,
sidecar, scheduler, database service, dependency on HyperP services, or production domain jobs.

## Safe defaults

The container starts idle. `INTELLIGENCE_MUTATIONS_ENABLED=false` is the default and production
registry is empty. Later reviewed code may register bounded reviewed handlers in a parent-supervised
child process; operators cannot
supply an executable, shell fragment, environment capture, or plugin path.

## Commands

Use `intelligence status`, `health`, `run NAME`, `inspect RUN_ID`, `cancel RUN_ID`,
`recover-stale RUN_ID --reason TEXT`, `backup NAME`, and `verify-backup NAME`. Status reports the
default-off mutation control, allowlisted names, health/recovery reason, and a safe active-run
summary. Inspect reports the safe terminal record and accepted-output inventory. Backup names are
single safe names, not paths or `.sqlite3` snapshots. A bundle is atomic/no-replace and contains a
SQLite online snapshot plus checksummed copies of completed-run manifests and accepted outputs;
verify it before exporting a copy off-volume. There is no restore or pruning command.

### CRM deal-reference snapshots

The reviewed domain surface is deliberately fixed:

```text
intelligence crm deal-refs extract --source-instance-id ID --as-of ISO8601 --max-records N [--page-size N]
intelligence crm deal-refs resume RUN_ID
intelligence crm deal-refs verify RUN_ID
intelligence crm deal-refs status RUN_ID
```

`extract` and `resume` are default-off Intelligence mutations using the same exclusive lock, child
supervision, output limits, staging, and no-replace publication rules as other reviewed runtime
commands. `verify` and `status` are read-only controls: they neither connect to Neo4j nor submit a job.
Credentials are read only inside the supervised child from `INTELLIGENCE_NEO4J_URI`,
`INTELLIGENCE_NEO4J_USER`, `INTELLIGENCE_NEO4J_PASSWORD`, and `INTELLIGENCE_NEO4J_DATABASE`; they
must not occur in arguments, logs, descriptors, or manifests.

Snapshots stage privately and publish immutably at `outputs/<run-id>/snapshots/crm/deal-refs/`; no
mutable current-deal pointer exists. A sealed descriptor records the source instance, UTC availability
cutoff, identity revision ceiling, keyset membership/content/observation fingerprints, schema/query
versions, page size, limits, and capture time. Extraction repeats persisted predicates before publication
and fails on missing, added, changed, or conflicting selected records. This is fail-closed observed-drift
detection, not an atomic Neo4j image.

Resume accepts no replacement boundary. Artifact-only replay is reserved for an independently verified
accepted output whose registered file inventory exactly matches its regular files, sizes, and checksums;
it copies rather than hard-links that evidence. Every unaccepted prior run, including a complete-looking
failed staging tree, reconciles the sealed selection before any copy/write and again after resumed writing
before publication. An interruption before sealing is not resumable. Artifact-only verification remains
valid after Neo4j advances; source reconciliation is intentionally limited to extraction and resume.

The CRM deal-reference schema is versioned and accepts only `crm_deal_identity_v2` evidence. It preserves
the source record hash, source-system/instance/policy provenance, source event/effective/close times,
availability and first-known times, `STAGE_ID`, and `STAGE_SEMANTIC_ID` when present. It does not infer a
stage outcome or lifecycle. Person UUID and observed Person status appear only for a resolved immutable
identity revision whose first-known availability and effective time are both at or before the cutoff;
future-effective, unavailable, and all non-resolved identity evidence remains Person-unlinked. Page requests use bounded max+1
keyset traversal with a fixed record ceiling. Page and checkpoint evidence carries cursors, exact boundary
identity, counts, bytes, checksums, and an exact expected file inventory; links, hard links, non-regular
entries, and unexpected evidence are rejected. Every component from the trusted workspace through an
accepted or partial domain root must be a real directory, not a symlink. Accepted controls compare their
registered regular-file inventory before parsing snapshot content. Metadata, pages, NDJSON lines, and
records have fixed size limits; page records are streamed rather than whole-file materialized. Checkpoint
record/page counts are bounded against the frozen record ceiling and page size before any sidecar traversal,
and accepted path/byte-count equality is checked before any file content is hashed.

The CRM activities archive is a separately reviewed, default-off nested command surface:
`intelligence crm activities extract|resume|verify|status`. It has a private checkpoint below
`staging/.crm-activities/`, uses bounded read-only Neo4j projections, and publishes only after a
sealed selection boundary, complete disposition reconciliation, and final drift verification. See
`profile-unifier-crm-activities-archive.md`; installing the capability does not perform a live
source extraction.

### CRM intelligence datasets

`intelligence dataset build|verify|list|inspect` is the fixed dataset surface. Build and verify are
default-off supervised jobs; list and inspect are bounded read-only State/artifact controls. They
consume accepted deal-reference and partial-activity snapshots only and never connect to source
systems. See `profile-unifier-intelligence-datasets.md` for point-in-time, missingness, inventory,
and replay rules.

### Manifest-gated CRM activity cleanup

Cleanup is provisioned only; no command is scheduled and no deployment enables deletion. The fixed
operator interface is `intelligence crm activities cleanup dry-run|execute|resume|verify|status`.
Dry run freezes `--cleanup-run-id` and `--batch-size` into an immutable, State-registered receipt.
Execute, resume, and verify accept that logical cleanup ID and the exact receipt run/digest but never
accept a replacement batch size. Status reads only bounded local checkpoint evidence and requires no
Neo4j credential, environment identity, or mutation switch.

Dry run and verification persist Intelligence evidence and therefore require
`INTELLIGENCE_MUTATIONS_ENABLED=true`; they use graph reads only. Execution and resume additionally
require `INTELLIGENCE_CRM_ACTIVITY_CLEANUP_ENABLED=true` and a matching
`INTELLIGENCE_ENVIRONMENT_ID`. Each receipt binds the accepted archive run/snapshot/manifest, exact
identity set, archive connection fingerprint, independently observed Neo4j database identity,
protected baseline, ownership/dependency evidence, limits, and policy. Attempts persist immutable
batch intent/result evidence under one logical checkpoint. Recovery never expands receipt identities;
an uncertain acknowledgement requires exact graph reconciliation. Terminal evidence is the disjoint
partition `authorized = deleted + already_absent + retained + conflict + failed`, with no unexplained
remainder. Verification requires successful terminal reconciliation, selected absence, and unchanged
protected evidence.

Cancellation is accepted while a run is queued or executing. Entering `publishing` is the
explicit non-cancellable commit point: a second connection receives a rejection rather than
silently racing terminal publication. A stale publishing run is recovered against its durable
inventory: every intact published output is registered and completed, while a missing, partial,
symlinked, or tampered publication is terminalized as failed with no accepted outputs.

## Persistence and recovery

SQLite uses WAL and the named volume contains `state/`, `staging/`, `runs/manifests/`,
`runs/rejected-manifests/`, `runs/logs/`, `outputs/`, and `backups/`. Terminal run evidence is
immutable canonical JSON; each
bounded secret-free NDJSON log has timestamps, severity, command/run identity, safe details, and a
checksum recorded in its terminal manifest. Removing/recreating only the Intelligence container
preserves the volume. Do not remove the volume. Restore, pruning, automatic cleanup, schedules,
and live execution are excluded. Future reviewed work remains default-off and bounded because the
parent starts each handler in a private process session, suppresses raw child stdout/stderr,
monitors aggregate staged bytes and entry count while it runs, and terminates the whole
process group on timeout or durable cancellation.

Manifest writers emit schema v2 while readers retain safe schema-v1 compatibility for legacy
empty or three-key limit objects, normalizing the missing entry limit to its documented default.
Effective limits are persisted at run admission and reused for stale recovery and backup
verification. Any handler-created or corrupt manifest is quarantined under
`runs/rejected-manifests/` without being read or followed.

If process-group cleanup cannot be proven, the run remains active and health becomes unhealthy;
ordinary heartbeat age is not sufficient to recover it. Recovery is permitted only after a
trusted execution-domain boundary: the runtime records the container's stable PID 1 start epoch,
which is shared by CLI execs in that container and changes when the container is recreated. A
same-container operator command therefore cannot bypass unresolved cleanup. Legacy migrated runs
with no persisted limits emit schema-v1 evidence with empty limits rather than fabricated defaults.
Backup bundles use an independent format version and continue to verify pre-v5 schema-4 bundles
whose snapshots lack persisted limits.
