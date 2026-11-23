# Profile Unifier Ingestion Operations

## Tracked staging Compose contract

`.docker/staging/docker-compose.yml` is the tracked, authoritative Compose
contract for the `hyperp-ada-asia` deployment. Operators must not substitute an
untracked host Compose file.

`services/api/tests/test_compose_contract.py` parses the root and staging files,
then permits only its finite exact exception registry: project metadata; root-only
deployment and CRM-repair settings; repository-relative build and mount rebases;
staging web memory, host-port removal, and Traefik attachment; the external
Traefik network; the ingestion worker's base target and Issue 147 evidence
inputs. Any other topology, image, command,
queue, environment, mount, network, or resource drift fails the contract test.

When an owning change modifies root `docker-compose.yml`, update the tracked
staging file in the same change. Review this exact registry and update it only
when an approved root/staging exception is added, removed, or changed.

### One-time ignored-to-tracked staging-host handoff

Before the **first** staging promotion that contains the tracked Compose file,
an operator must perform this fail-closed handoff in the persistent staging
checkout. The existing host file is ignored/untracked, while the incoming Git
commit tracks the same path; without this handoff, `git merge --ff-only` can
refuse to overwrite the untracked file. This is an operational prerequisite for
that first promotion, not a deployment workflow change.

Run the following from the staging repository before the fast-forward. It does
not invoke Docker Compose or start, stop, recreate, or otherwise alter services:

```bash
set -euo pipefail

COMPOSE_PATH=.docker/staging/docker-compose.yml
CANONICAL_SHA256=35883848CAED13BE206E9630CC0B939C541ADB00F7B8C25C26A5BB3BCE2489C3

if git ls-files --error-unmatch -- "$COMPOSE_PATH" >/dev/null 2>&1; then
  echo "Abort: $COMPOSE_PATH is already tracked; do not perform the one-time handoff." >&2
  exit 1
fi
if ! git check-ignore -q -- "$COMPOSE_PATH"; then
  echo "Abort: expected $COMPOSE_PATH to be ignored and untracked." >&2
  exit 1
fi
if [ ! -f "$COMPOSE_PATH" ]; then
  echo "Abort: expected ignored Compose file is missing or is not a regular file." >&2
  exit 1
fi

ACTUAL_SHA256="$(sha256sum "$COMPOSE_PATH" | awk '{print toupper($1)}')"
if [ "$ACTUAL_SHA256" != "$CANONICAL_SHA256" ]; then
  echo "Abort: preserving non-canonical $COMPOSE_PATH; hash is $ACTUAL_SHA256." >&2
  exit 1
fi

BACKUP_PATH="${COMPOSE_PATH}.pretracked-$(date -u +%Y%m%dT%H%M%SZ)"
if [ -e "$BACKUP_PATH" ]; then
  echo "Abort: backup path already exists: $BACKUP_PATH" >&2
  exit 1
fi
mv -- "$COMPOSE_PATH" "$BACKUP_PATH"
```

Only the verified canonical ignored file is moved aside; any missing, tracked,
unignored, or hash-mismatched path aborts without changing it. The normal fetch
and `git merge --ff-only FETCH_HEAD` may then check out the tracked path. After
the fast-forward, verify that Git owns the file and its bytes remain canonical:

```bash
set -euo pipefail

COMPOSE_PATH=.docker/staging/docker-compose.yml
CANONICAL_SHA256=35883848CAED13BE206E9630CC0B939C541ADB00F7B8C25C26A5BB3BCE2489C3

git ls-files --error-unmatch -- "$COMPOSE_PATH" >/dev/null
ACTUAL_SHA256="$(sha256sum "$COMPOSE_PATH" | awk '{print toupper($1)}')"
test "$ACTUAL_SHA256" = "$CANONICAL_SHA256"
```

## Woodpecker staging deployment

Pushes to `staging` are deployed by `.woodpecker/staging.yaml`. Pull requests and
pushes to `main` remain validation-only; they do not receive deployment secrets
or access the staging host. The staging pipeline follows the fail-closed pattern
used by `sparkfn/autocollect-backend`:

1. validate the deployment and lifecycle-guard shell syntax;
2. cross the container-to-host boundary with strict pipeline-managed SSH;
3. pass the exact full `CI_COMMIT_SHA` to
   `scripts/deploy/hyperp-staging.sh`;
4. lock, fast-forward, selectively rebuild, verify, and record the deployed SHA.

The CI checkout and staging checkout are both on `dev211` under `/home/docker`:

```text
/home/docker/ci.sparkfn.io
/home/docker/hyperp.ada.asia/.docker/staging
```

From the CI workspace, staging is `../hyperp.ada.asia/.docker/staging`; from
staging, CI is `../../../ci.sparkfn.io`. They are four filesystem edges apart.
The SSH connection is only the Woodpecker-container-to-host execution boundary;
no developer workstation, Support-repository proxy, or external deployment host
is part of the path.

Configure these push-only Woodpecker repository secrets without storing their
values in Git:

- `hyperp_staging_ssh_host`
- `hyperp_staging_ssh_port`
- `hyperp_staging_ssh_user`
- `hyperp_staging_ssh_key_b64`
- `hyperp_staging_ssh_known_hosts`
- `hyperp_staging_health_url`

The deployment script requires the persistent checkout to be clean and on
`staging`. It fetches the configured `origin`, requires `origin/staging` to equal
the pipeline SHA, requires that SHA to be contained in `origin/main`, and permits
only a fast-forward. This enforces the repository rule that `main` must never be
behind `staging`.

The script uses the canonical `hyperp-ada-asia` Compose project, preserves the
lifecycle pause marker, rebuilds only services whose code or build inputs changed,
recreates services for relevant Compose configuration changes without rebuilding
their images, retains invariant checks, waits for worker stability, verifies
internal and external health, and atomically records the successful SHA in the
ignored staging data directory at
`.docker/staging/data/deployed-revision`.

Rollback is forward-only: revert the faulty change on `main`, allow MAIN CI to
pass, then promote that new revert commit to `staging`. Do not force-push or move
`staging` behind `main`. The normal staging pipeline then performs the same
deployment and verification gates for the revert commit.

## Staging worker pause, resume, and deployment policy

The three bounded staging worker services have independent, durable pause intent:
`ingestion-worker`, `beat`, and `lifecycle-worker`. Run the supported control on
the staging host rather than stopping a container or editing Redis directly:

```bash
STAGING_REPO_DIR=/path/to/hyperP scripts/worker-control.sh pause ingestion-worker
STAGING_REPO_DIR=/path/to/hyperP scripts/worker-control.sh status
STAGING_REPO_DIR=/path/to/hyperP scripts/worker-control.sh resume lifecycle-worker
```

`STAGING_COMPOSE_FILE` may override the default
`.docker/staging/docker-compose.yml`. Relative Compose paths are resolved from
`STAGING_REPO_DIR`.

`scripts/lifecycle-worker-control.sh pause|status|resume` remains a compatibility
wrapper for lifecycle-only procedures. Only the three worker names above are accepted.

All staging Compose operations use the canonical `hyperp-ada-asia` project.
`STAGING_COMPOSE_PROJECT` may override it for an isolated test checkout, but
operators and deployment automation must not rely on Compose's directory-derived
default because that can create duplicate workers.

Pause intent is an empty regular marker below ignored
`.docker/staging/data/worker-pauses/{ingestion-worker,beat,lifecycle-worker}`.
`pause` atomically creates its marker before attempting to stop a service and
retains it when stopping or stopped-state verification fails. `resume` inspects
the effective policy before starting a worker, starts it successfully, then clears
its marker. `status SERVICE`
reports one service and `status` reports all three. Docker failures, symlinks,
tracked markers, malformed marker files, and unsafe legacy migration fail closed.
The old root `.lifecycle-worker-paused` intent migrates only when it is the sole
untracked change; unrelated dirty state is rejected.

Treat either of these combinations as an inconsistent state requiring investigation:

- marker present and `consumer_running=true`;
- marker absent and `consumer_running=false` when lifecycle consumption is expected.

The staging deployment workflow builds changed worker images even while paused.
It recreates unpaused eligible services with `up -d --no-deps --force-recreate`,
and paused services with `up --no-start --no-deps --force-recreate`; deployment
checks both supported flags before any service mutation and fails closed if either
is unavailable. It then verifies
every paused container remains stopped. Deployment never clears a marker or resumes
a worker. Beat and completed ingestion tasks can still publish lifecycle work;
Redis retains it until lifecycle consumption is explicitly resumed.
Immediately after reading pause intent, the held deployment lock invokes the
canonical control to stop and verify each marked worker before planning any build
or recreation; an inconsistent running worker fails the deployment with its marker
still present.

The deployment-only policy helper discovers the actual host bind backing
`/app/config`, requires all three workers to agree on it and `INGESTION_CONFIG_FILE`,
and probes the approved timezone/opening/cutoff/reserve before mutation. During an
authorized worker deployment or resume, it atomically adds only missing approved
keys to the actual mounted JSON, retains its source file mode, and strictly reads it
back. Explicit conflicting values fail closed. It never emits a config copy, logs
config values, overwrites `enabled`, manual/stronger pauses, or unrelated fields, or
writes a Git-tracked effective config. The approved policy is `Asia/Singapore`,
opening `09:00`, cutoff `23:00`, and a `900`-second drain reserve.

For a legacy bare-exclusions file with no recognized nested configuration section,
preparation first moves the complete legacy object under `exclusions` before adding
the scheduling section. This preserves the same effective exclusions seen by the
ingestion loader. Atomic replacement also preserves and verifies source UID, GID,
and restrictive mode before replacement; unavailable ownership preservation fails
without replacing the source file.

When scheduling is enabled, deployment admits an unpaused worker start/recreation
only in `[09:00, 22:45)` Asia/Singapore. It checks before image builds/config migration
and immediately before container mutation, so an image build crossing the boundary
fails closed without starting a worker. Paused workers may be recreated stopped outside
that interval. This is deployment admission only: #430/#431 own runtime admission,
absolute deadlines, checkpointing, lease release, and next-week resumption. There is
no blind cutoff kill.

Worker `resume` applies the same clock admission only when the mounted policy has
`scheduled_ingestion.enabled=true`; scheduling disabled permits an operator resume.
An admission, Compose, or verification failure leaves pause intent in place.

## Queue-gate recovery

KNOWS gates are owner-checked and intentionally do not use short TTLs: a paused
consumer may legitimately leave a message pending for longer than a lease. Use
the owner-safe command from an ingestion container instead of deleting Redis
keys blindly:

```bash
python -m src.lifecycle_queue_admin status
python -m src.lifecycle_queue_admin clear-knows --phase contacts --expected-owner <task-id>
python -m src.lifecycle_queue_admin clear-reconciliation --expected-owner <task-id>
```

Before clearing a gate:

1. Pause lifecycle consumption and confirm `consumer_running=false`.
2. Run `status` and record the exact owner ID.
   A `publishing` state records an ambiguous broker publication and includes its
   Redis-server timestamp. It is never replaced automatically, even if the
   lifecycle consumer remains paused for an extended period. A `malformed`
   state requires engineering investigation rather than an owner guess.
3. Confirm from the incident timeline that publication failed or the owning task
   was permanently discarded. A `PENDING` Celery result alone is not proof that
   no broker message exists.
4. Clear only that exact owner with `--expected-owner`.
5. Run `status` again, then resume lifecycle consumption.

Exit code `1` means the owner did not match, usually because ownership changed.
Exit code `2` means Redis could not be queried or updated. Do not retry a clear
with a different owner until the backend error is resolved and status is checked
again.

## Initialization-lock diagnostics

Look for `initialization_lock_acquired`, `initialization_graph_complete`, and
`initialization_lock_released` events. They report only safe task classes and
durations; source payloads and cursor values are intentionally excluded.

A source ingestion registered as an initialization waiter cannot be overtaken by
a later lifecycle initialization request. With no older source waiter, it is the
next eligible requester after the current graph-initialization section releases
the lock. The implementation emits
`initialization_lock_wait_slo_exceeded` after **5 seconds** of waiting.

The five-second threshold is an operational warning, not a hard timeout. Total
wait can still include the current serialized schema/migration operation, older
source waiters, Redis unavailability, or worker scheduling delay. When the
warning appears:

1. compare `wait_seconds` with the current holder's lock-hold duration;
2. verify lifecycle work is continuing outside the initialization lock;
3. confirm the source task eventually logs `initialization_lock_acquired`;
4. pause lifecycle consumption with the supported script if acquisition does not
   follow the current initialization section;
5. retain the safe lock events and task IDs for incident follow-up.

## Standalone CRM mapping/projection activation (#307)

The standalone CRM identity plane is **off by default**. Both
`standalone_crm_identity_enabled` and `standalone_crm_identity_schedule_enabled` must be
true before the manual `dispatch_standalone_crm_source_sync` task can enqueue a bounded
source-sync census. It is deliberately absent from Celery Beat; operators must invoke the
task explicitly. The dispatch captures complete active mapping and projection heads before
building the immutable request, and never calls a live source directly.

Mapping changes are operated as `prepare → project → activate` (or `rollback → project →
activate`). Prepare and rollback are accepted only with an unexpired exact configured grant:
there are no wildcards or implicit authorization. `project` preserves the existing projection
materializer semantics. `activate` is a Celery census admission, not a direct graph mutation;
the zero-Bitrix mapping child performs the atomic mapping/projection head CAS from the persisted
payload only. `status`, `reconcile`, and the bounded manual source-sync command are read/dispatch
controls. Reconcile is the acknowledgement-loss recovery path: if CAS committed but census
settlement did not, it reads the durable release receipt and settles the checkpoint/accounting
without rematerializing, moving heads, or counting the unit again.

Published mapping payloads and source payloads are mutually exclusive. A mapping worker rejects
source payloads before source client construction; a source worker rejects mapping payloads.

Concrete operator entry points are registered Celery task names:

```text
celery call src.crm_tenant_operator_tasks.prepare --args '[{"scope":...,"manifest":...,"authorization":...}]'
celery call src.crm_tenant_operator_tasks.project --args '[{"scope":...,"request_id":...,"expected_prior_head":...}]'
celery call src.crm_tenant_operator_tasks.activate --args '[{"census_kind":"mapping_prepare",...}]'
celery call src.crm_tenant_operator_tasks.rollback --args '[{"scope":...,"rollback_of_revision_id":...,"authorization":...}]'
celery call src.crm_tenant_operator_tasks.status --args '["<census-id>"]'
celery call src.crm_tenant_operator_tasks.reconcile --args '["<census-id>"]'
celery call src.crm_tenant_operator_tasks.source_sync --args '[{"census_kind":"source_sync",...}]'
```

The manual scheduler invokes `src.standalone_crm_schedule_tasks.dispatch_standalone_crm_source_sync`.
That task captures exact heads, enqueues `admit_and_run_standalone_crm_census`, and the latter admits
then runs the parent state machine; neither task performs a live source call itself.

Operator tasks receive JSON objects, never Python dataclasses. For example, `prepare` receives:

```json
{"scope":{"source_key":"bitrix_chat","source_instance_id":"portal-a","control_instance_id":"default"},"preparation_request_id":"prepare-20260901","manifest":{"entries":[{"company_id":"42","targets":[{"entity_key":"tenant-a"}]}]},"expected_head_boundary":{"head_id":"<deterministic-mapping-head-id>","expected_head":null},"authorization":{"actor":"operator","authorization_reference":"change-ticket","authorization_digest":"sha256:<64-hex>","authorized_at":"2026-09-01T00:00:00Z","expires_at":"2026-09-02T00:00:00Z"},"operation_time":"2026-09-01T00:00:00Z"}
```


## CRM-deal identity repair integration operator (#313)

This is a **staging-only, default-off** break-glass workflow. It exposes exactly six manually
selected commands: `apply`, `verify`, `rollback-status`, `rollback`, `accept`, and
`release-dispatch`. It has no Beat entry and commands never auto-chain.

Before every command, require `DEPLOYMENT_ENVIRONMENT=staging`,
`CRM_DEAL_IDENTITY_REPAIR_ENABLED=true`, and the control credential in
`CRM_DEAL_IDENTITY_REPAIR_CONTROL_TOKEN`. The signed overlay key and rollback authorization
digest are environment-only credentials; the latter is required only by `rollback-status` and
`rollback`. Never put either credential in command arguments, task payloads, reports, logs, or
receipts. Celery receives the same strict non-secret fields as the CLI and returns the service
receipt without printing CLI JSON.

Unit commands (`apply`, `verify`, `rollback-status`, and `rollback`) require the exact
`--repair-id`, `--run-id`, `--owner-id`, `--expected-revision`, `--approval-id`, and `--unit-id`.
Rollback commands additionally require `--authorization-reference` and
`--predecessor-transition-id`. Run commands (`accept`, `release-dispatch`) require the run/control
identity and approval but reject unit and rollback-only fields. The compatible
`--negative-control-limit` alias remains accepted for the inventory replay limit.

Normal order is `apply` then `verify`; `rollback-status` and `rollback` may occur after apply or
after verification, but never after terminal acceptance. `rollback-status` creates/replays an
immutable digest-only receipt for the exact available image. A crash or lost acknowledgement is
recovered by replaying the same command with the same authority; a conflicting owner, revision,
manifest, allocation, fence, boundary, image, receipt, negative-control, or equation result is a
stop condition, not a reason to retry with altered inputs.

`accept` is terminal and atomic: it requires the complete authenticated allocation unit/fence set,
all component verification/equation and negative-control checks, available rollback-status evidence
for every mutation image, and no remainder, failure, drift, pending secondary, or terminal rollback.
It releases only that run's fences and deliberately leaves dispatch quiesced. `release-dispatch` is
a separate explicit command after acceptance. It exact-CAS clears only the current repair-owned
`crm_deal_identity_repair_quiesce` block; it never clears a replaced or unrelated block.

Reports contain only `operation`, `state`, `request_digest`, and `receipt_digest`; SHA-256 digests
are evidence identifiers, not credentials. Stop immediately on any validation failure, preserve the
quiesce/fence state, investigate the immutable evidence, then use exact replay only after the
external discrepancy is resolved. This document does not authorize live execution.

# Bitrix CRM activity retirement

`bitrix_openlines.crm_activity_ingestion_status` is permanently `retired`.
It reports that Neo4j CRM activity and companion-call ingestion cannot be
enabled. Scheduled ingestion remains default-off. Successor and corrective
publication filters historical manifests to CRM-deal and Open Lines entries;
activity entries remain inspectable evidence only. Activity-ID-based Open Lines
discovery and `crm_activity_ids` provenance remain supported, but they do not
write or update historical activity evidence. Deploy #387 before activating
#388 when activity presentation must be preserved; queue quiescence and
inspection are separate live work.

## Person-list summary counter (#489)

### Initial cutover and backfill

The `PersonListSummaryCounter` singleton is not ready for use until an operator
explicitly backfills it. Before the first backfill, quiesce **all** API and
ingestion writers that can change summary contributions, wait for their
in-flight Neo4j transactions to drain, and keep them quiesced through backfill
and readiness publication. Confirm that every writer to be resumed maintains
the counter transactionally; older or out-of-band writers must remain stopped.

Run the fixed operator control from an ingestion environment after draining the
writers:

```bash
python -m src.person_list_summary_control backfill
```

This command is the explicit activation path. The startup bootstrap only creates
the singleton as unready; it does not calculate counts or make it ready.

### Nightly reconciliation

Celery Beat schedules `src.tasks.reconcile_person_list_summary_task` daily at
00:00 UTC and routes it to the consumed `lifecycle` queue. The task reconciles
the six stored counts against their authoritative aggregates and repairs drift
atomically. It also handles the first-of-month refresh. If the counter is
unready, the task logs that it has nothing to reconcile and **never** activates
it; run the explicit `person_list_summary_control backfill` before enabling the
maintained read path.

### Rollback

Rollback the maintained summary read path by swapping API reads back to the
retained full-scan aggregate queries and restarting the API. Before allowing any
writer version that does not maintain summary deltas to run, invalidate counter
readiness with the ingestion implementation's `invalidate_person_list_summary`
helper and verify that the singleton is unready. The current
`person_list_summary_control` CLI exposes only `backfill`, not a separate
invalidation command; do not use `backfill` as a substitute for invalidation.
After all writers again support the maintained counter and have been quiesced
and drained, run `python -m src.person_list_summary_control backfill` before
re-enabling counter reads.

For a memory rollback, update both root and tracked staging Compose files
synchronously: set `NEO4J_server_memory_heap_max__size` to `1g`,
`NEO4J_server_memory_pagecache_size` to `512m`, and Neo4j `mem_limit` to `3G`.

## NRIC plaintext-to-digest rehash backfill (#510)

### What it changes — and what it never does
The conversion pass keys each convertible plaintext `nric` Identifier, writing the HMAC-SHA256
digest to both `normalized_value` and `hashed_value` with `nric_masked`, `checksum_valid`,
`nric_key_version`, and Fernet `nric_encrypted`. If a global digest target exists, supported
evidence edges move with properties preserved; affected `HAS_IDENTIFIER` projections are
recomputed and the plaintext node is deleted.
The second pass anchors only eligible active Persons: it sets `Person.nric_key` and
`ANCHORED_BY` after the existing gate. The rehash never creates or merges a Person, rewrites
immutable `SourceRecord.normalized_payload`, or emits identity-link revisions; the final report
requires the revision counter unchanged.

Digest-shaped values are skipped, so this is not key rotation. Masked, empty, and
unknown-format values stay `unprocessable`; a recoverable invalid-checksum value is converted
with `checksum_valid = false`.

### Preconditions and approvals
Before the window, name the operator and change owner, record the approval, and rehearse on a
disposable copy. Configure non-empty `NRIC_HMAC_KEY` and `NRIC_HMAC_KEY_VERSION` through the
container environment (`v1` is the version default); never put key values in arguments, reports,
shell history, or logs. Confirm the exact database name for `--expect-database`. After draining
writers, take a protected, restorable backup before `apply`; it contains plaintext NRIC values and
must be protected like live data.

### Maintenance window and writer drain
Online operation is unsupported. Stop and drain identity writers before `apply`; keep them down
through `verify` and assertion audit:

- Pause `ingestion-worker`, `beat`, and `lifecycle-worker` with the existing
  [Staging worker pause, resume, and deployment policy](#staging-worker-pause-resume-and-deployment-policy).
  That control does not stop the API.
- Stop the `api` service. Do not run CRM repair tooling, or assertion backfill before
  `anchor_complete`.

The rehash page-locks only matched Identifiers, so an absent digest target is unprotected; v1 does
not take the C3 lock path. `--acknowledge-offline` records an acknowledgement, not proof of drain.
Verify all writers stopped independently.

### Running the CLI
The image runs as non-root `appuser` (uid 1000) in `/app/services/ingestion`. `plan` and `verify`
are read-only; optional `--batch-size N` defaults to 500. `apply` requires
`--acknowledge-offline`, `--expect-database <database-name>`, and `--handoff-dir <directory>`;
each conversion or anchor batch is one bounded write transaction.

Template only: adapt and rehearse it, never treat it as live authorization. Use the target-local
Docker daemon and an owner-only, resolved, non-symlink host directory writable by uid 1000. Export
the key through the environment, not as an argument:

```bash
docker compose -p "${STAGING_COMPOSE_PROJECT:-hyperp-ada-asia}" \
  -f .docker/staging/docker-compose.yml \
  run --rm --no-deps \
  -e NRIC_HMAC_KEY -e NRIC_HMAC_KEY_VERSION \
  -v /path/to/handoff:/path/to/handoff:rw \
  ingestion-worker \
  python -m src.nric_rehash apply --acknowledge-offline \
    --expect-database "<exact-database-name>" \
    --handoff-dir /path/to/handoff
```

Run it from the staging repository checkout with the canonical project and tracked Compose file, as
the worker-control script does; never rely on Compose's directory-derived default project (see the
worker pause section above). Compose is unchanged; use `plan` or `verify` instead of `apply` for
read-only reports.

Success prints one JSON report on stdout. `apply` writes one stderr JSON line per batch with only
`event`, `batches`, `converted_in_place`, `converted_consolidated`, and `removed_nodes`. Sanitized
failures print `{"code": ..., "status": "error"}` without exception text; an unexpected crash can
still print a Python traceback, so do not forward that output to shared logs. The driver may log
parameters at DEBUG; see “Never run with DEBUG logging.”

| Result | Exit | Meaning |
|---|---:|---|
| `plan.status: ok` or `verify.ok: true` | 0 | Read-only command passed its report checks. |
| `plan` blocked or `verify.ok: false` | 1 | Review blockers or failed invariants. |
| `apply.status: ok` | 0 | Final report passed blocking checks. |
| `apply.status: blocked` | 1 | Stop; do not resume writers. |
| `apply` code | 1 | `null_scope_present`, `unsupported_edge`, `residual_edges`, `missing_id`, `unexpected_scope`, `residual_plaintext`, `handoff_write_failed`. |
| Other CLI error or argparse | 2 | `offline_ack_required`, `nric_key_missing`, `invalid_configuration`, `database_mismatch`, `key_changed`, `lease_unavailable`, `lease_lost`, `drift_detected`, `paging_violation`, `handoff_dir_unsafe`, `neo4j_error`; config/Neo4j errors may occur in any command. |

### Procedure
1. Approve the window, name the operator/change owner, and rehearse on a disposable copy.
2. Drain and independently verify all listed writers.
3. Take a protected, restorable backup containing the pre-run plaintext.
4. Run `plan`; review counts, blockers, database, and decisions. `convertible > 0` is expected
   work and can coexist with `status: ok`; a blocked result (including any masked, empty, or
   unknown-format value) needs a recorded owner decision.
5. Run `apply` once with the exact database, offline acknowledgement, and safe handoff directory;
   keep writers stopped.
6. Read the full apply report and reconcile every counter/equation. Any blocked report stops work.
7. Run read-only `verify`; continue only with `ok: true` and readiness fields reviewed.
8. Have L2 consume protected handoff files; securely delete only after verify is accepted.
9. Only where assertions are written or were backfilled (`IDENTITY_MODEL=v2_shadow`; the default
   is `v1`), run assertion replay and the read-only audit at `anchor_complete`; require zero
   mismatches. Otherwise skip this step.
10. Configure the API with the same key and resume it first; then resume workers in approved order.

Any non-`ok`/blocked result needs a recorded owner decision; failed equations remain stop conditions.

### Reading the reports
`plan` reports `status`, `blockers`, `census`, `pages`, `convertible`, `already_digest`,
`checksum_invalid_convertible`, `digest_metadata_gap`, and `unprocessable` (`masked`, `empty`,
`unknown_format`) counts. Blockers are the census conditions `missing_id`, `unexpected_scope`,
and `null_scope`, plus any per-value `masked`, `empty`, or `unknown_format`, so `plan` is `blocked`
(exit 1) while such values exist. `convertible`, `checksum_invalid_convertible`, and
`digest_metadata_gap` are counts, not blockers; review them even when status is `ok`.
`verify.ok` is false if any gating count is non-zero:

| Gating field | Meaning |
|---|---|
| `non_digest_count` | NRIC Identifiers remain outside digest shape. |
| `missing_metadata_count` | Metadata is absent, key version differs, or `hashed_value` mismatches. |
| `missing_id_count` | An NRIC Identifier lacks `identifier_id`. |
| `unexpected_scope_count` | Scope is null or not `global`. |
| `duplicate_key_groups` / `duplicate_extra_owners` | Multiple Persons hold an NRIC key. |
| `duplicate_scoped_targets` | Multiple NRIC Identifiers share a scoped target. |

`identifier_count` and `anchored_persons` are context, not standalone gates.
`identifiers_without_assertion` and `identifiers_without_projection` are informational v2
readiness signals: non-zero means candidate discovery lacks `ASSERTS`/`HAS_IDENTIFIER` coverage,
not a failed rehash. This release added gating `missing_id_count` / `unexpected_scope_count` and
three readiness fields.

The final report contains `status`, `phase`, `changes`, `batches`, conversion/removal counters,
cumulative `anchored`, `already_anchored`, `anchorable_remaining`, `handoff` (`total`, `by_reason`,
`file`), baseline/final counts and evidence, plus five equations. `changes` is this-run conversions
plus anchors (zero on a zero-write rerun); `anchored` is cumulative. `already_anchored` includes
Persons carrying key and anchor edge at the final scan, including new anchors. `anchorable_remaining`
must be zero; unresolved handoff entries alone do not block `status: ok`.

Each equation must be true:

- `count_equation_ok`: final Identifier count = baseline − removed nodes.
- `conversion_equation_ok`: baseline non-digest = converted in-place + consolidated + final non-digest.
- `evidence_equation_ok`: `ASSERTS`, `IDENTIFIED_BY`, and moved counts are unchanged; `ANCHORED_BY` grows by at most Persons anchored.
- `attribution_unchanged`: active Source Record-to-Person links are unchanged.
- `revision_unchanged`: identity-link revision counter is unchanged.

If any equation is false, stop, keep writers drained, do not resume, and escalate to the #510
owner. Restore the backup if damage is suspected. `apply.status: blocked` (exit 1) means
convertibles or unprocessable values remain, `anchorable_remaining` is non-zero, `verify.ok` is
false, or an equation failed. Masked, empty, and unknown-format values intentionally keep the run
blocked until triaged out of band.

### Phases, resume and failure handling
The durable `DataMigration` state is keyed by `nric_hmac_rehash_v1`; it stores phase, cursors,
counters, key check, baseline, and lease. Phases are `convert`, `convert_complete`, `anchor`, and
`anchor_complete`. Each batch persists its cursor/counters in the same write transaction as its
graph changes, so rollback rolls back that batch and checkpoint together.

The lease is 300 seconds and extends per batch. After a crash, wait for expiry and rerun `apply`
with the same key, key version, and database. A live lease returns `lease_unavailable`; a lost
lease returns `lease_lost`. Never run two applies concurrently. A stored non-reversible key check
and the key version are compared on every run; a different key or version returns `key_changed`
before writes.

Completed reruns report zero graph changes but may create a new handoff file. Stop and escalate on
`drift_detected`, `paging_violation`, `residual_edges`, or `unsupported_edge`; do not retry with
altered inputs. `residual_plaintext` is also a stop. `handoff_dir_unsafe` is checked before
database access; `handoff_write_failed` may occur after graph changes commit. Fix the directory
and rerun: the completed migration makes no graph writes and regenerates the handoff file.

### Person anchoring and the L2 handoff file
The anchor pass examines active Persons and distinct NRIC Identifiers found through active
`IDENTIFIED_BY`, active `HAS_IDENTIFIER`, and attributed source-record `ASSERTS` evidence. It anchors
only when all conditions hold:

- Exactly one distinct NRIC Identifier is present; it is digest-shaped and checksum-valid.
- The Person has no conflicting `nric_key`; no other Person holds the key or has active evidence
  for that Identifier.
- Evidence is not truncated. The pass reads at most 201 attributed records; over 200 sets
  `evidence_truncated` and prevents anchoring.
- At least one attributed Source Record passes the NRIC anchor gate. Attribution is an active
  `LINKED_TO` edge or an active `IDENTIFIED_BY` edge naming that record.
- The source promotes Persons, meets its NRIC trust-tier floor, is not conversation-type, and
  does not contradict holder name/DOB. Member and durable-holder contradictions also prevent
  anchoring.

Other cases are L2 handoffs, never guesses; exact unresolved reasons:

| Reason | Meaning |
|---|---|
| `multi_nric` | More than one distinct NRIC Identifier. |
| `unrecoverable_identifier` | No usable NRIC digest Identifier. |
| `person_key_conflict` | Person already carries a different NRIC key. |
| `checksum_invalid` | Digest Identifier is not checksum-valid. |
| `shared_owner` | Another Person holds the key or has active Identifier evidence. |
| `evidence_truncated` | More than 200 attributed records were found. |
| `missing_source_context` | No attributed record supplies usable gate context. |
| `nric_contradiction` | Member or durable-holder evidence contradicts the anchor. |
| `source_not_promoting` | Source policy does not promote a Person. |
| `below_tier_floor` | Source NRIC trust is below the promotion floor. |
| `conversation_record` | Source record is a conversation. |

Write a handoff file only when unresolved Persons exist, including on zero-write reruns; files can
accumulate. Names use `nric-rehash-handoff-<UTC timestamp>-<8 hex>.jsonl`; files are exclusive,
mode `0600`, sorted by `person_id`, and contain only `person_id`, `reason`, `identifier_ids`, and
up to 50 `source_record_pks`. They contain no NRIC, digest, ciphertext, name, or DOB, but linkage
IDs remain sensitive. Keep files host-local; never copy them to tickets, chat, or CI artifacts.
Terminal output shows only counts by reason and the basename.

`--handoff-dir` must exist and have no group/other permission bits. Every path component is opened
without following symlinks; any symlink is refused. Pass a fully resolved path and make the bind
mount writable by uid 1000; directory ownership is not checked. `handoff_dir_unsafe` is returned
before database access. Retain a file only until L2 consumes it and `verify` is accepted, then
securely delete it.

### Assertion replay and the API after the rehash
The rehash never rewrites immutable `SourceRecord.normalized_payload`. Replay and audit apply only
where assertions are written or were backfilled (`IDENTITY_MODEL=v2_shadow`; the default is `v1`);
otherwise skip them, and the keyed protections below still apply if either runs later. Replay
legacy payloads only after `anchor_complete`: the assertion writer maps plaintext NRIC to the same
digest and preserves historical assertion IDs. Replaying first can hit the unique `assertion_id`
constraint. Backfill refuses to run unless `IDENTITY_MODEL=v2_shadow` and needs the same
`NRIC_HMAC_KEY`. Without the key, the first record holding a convertible legacy NRIC stops the
whole backfill: it prints the fixed line `NRIC assertion replay refused`, exits 2, and writes
nothing for that record, while earlier records stay written. Fix the key and rerun.

```bash
python -m src.identity_assertions backfill   # needs IDENTITY_MODEL=v2_shadow and NRIC_HMAC_KEY
python -m src.identity_assertions audit      # read-only, key-free
```

Run both through the one-off container template above with the command replaced and
`IDENTITY_MODEL` set in the container environment.

Then run the read-only, key-free audit. Go only on `Found 0 assertion count mismatches` (exit 0).
A mismatch is no-go: keep writers drained and escalate; never delete immutable `ASSERTS` by hand.
One accepted fallback can add a parallel `ASSERTS` edge: if a payload lacks an `identifiers` value,
replay falls back to graph-derived IDs, now digests. If that record was asserted before rehash,
replay can add a second edge with a different assertion ID. Audit reports the count mismatch; no
automatic deduplication occurs.

Configure the API with the same key before reviewers resume merges. Without it, a pending record
with a convertible legacy NRIC returns generic `merge_not_applicable` (HTTP 422), with no key
detail. Digest-shaped, masked, and unrecognizable NRIC values need no key. Activation sets global
scope on created NRIC Identifiers; other identifier types remain scope-less in that statement.

An API-created digest Identifier may lack NRIC metadata if no digest node existed. `verify`
reports `missing_metadata_count`; an isolated already-digest node is not repaired, so escalate. A
plaintext collision can complete survivor metadata during consolidation, but rehash is not a
general metadata-repair operation.

### Never run with DEBUG logging
The Neo4j driver logs queries and parameters at DEBUG; parameters may include digests, ciphertext,
lease owners, and internal element IDs. The CLI does not configure logging. Avoid DEBUG wrappers,
`sitecustomize`, `dictConfig`, or `basicConfig`, and never ship DEBUG output to a log service. Keep
`LOG_LEVEL` at INFO or higher; ingestion `setup_logging` consumes it, but rehash does not, so it is
not a safeguard. Without external configuration, the root logger defaults to WARNING; other
ingestion NRIC writes have the same exposure.

### Rollback and key rotation
There is no automated rollback. Conversion rewrites Identifier values, consolidation deletes the
plaintext node, and anchoring adds `Person.nric_key` and `ANCHORED_BY`. Restore the protected
pre-run backup to roll back; it restores migration state and the revision counter consistently. A
crash or stop mid-run is not by itself a reason to restore: rerun after lease expiry.

No identity-link revision events were emitted, so downstream cursors/snapshots need no reset. A
consumer that cached a consolidated plaintext Identifier ID will not find it; the digest survivor
keeps its own ID. Restoring backup reintroduces plaintext nodes, so replay ordering applies again.

Key rotation is separate and controlled, not part of this rehash. Follow the Graph Schema
procedure: decrypt with the old key, verify and re-hash, re-encrypt with the new key, reconcile
collisions and Person anchors, then switch `NRIC_HMAC_KEY` in a maintenance window. The API reveal
path uses one key, so mixed-key reads require a maintenance window. Losing the key makes keyed
records unmatchable and unrevealable; protect it in the approved secret store.

### Known residuals
- Pre-rehash assertion fallback can create a parallel `ASSERTS` edge; audit detects the mismatch.
- Pending-review activation still creates non-NRIC Identifiers without scope; outside #510.
- Digest metadata gaps are reported, not generally repaired, and keep `apply` blocked (`verify`
  fails).
- Masked, empty, and unknown-format values remain unconverted and keep `apply` blocked until
  triaged out of band.
- The handoff no-symlink rule can reject unresolved host paths.
- v2 readiness signals are reported, not enforced by the rehash.
- The tool cannot prove writers are stopped; acknowledgement is not operational evidence.
- Neo4j behavior not run manually is exercised by the disposable-Neo4j PR shard; operators must
  still rehearse on a disposable copy.

This document does not authorize live execution.
