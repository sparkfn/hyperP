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
