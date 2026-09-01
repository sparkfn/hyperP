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
inputs; and staging beat resource limits. Any other topology, image, command,
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

## Lifecycle worker pause and resume

The lifecycle worker consumes reconciliation and deferred KNOWS materialization.
Run the supported control script on the staging host rather than stopping a
container or editing Redis directly:

```bash
STAGING_REPO_DIR=/path/to/hyperP scripts/lifecycle-worker-control.sh pause
STAGING_REPO_DIR=/path/to/hyperP scripts/lifecycle-worker-control.sh status
STAGING_REPO_DIR=/path/to/hyperP scripts/lifecycle-worker-control.sh resume
```

`STAGING_COMPOSE_FILE` may override the default
`.docker/staging/docker-compose.yml`. Relative Compose paths are resolved from
`STAGING_REPO_DIR`.

All staging Compose operations use the canonical `hyperp-ada-asia` project.
`STAGING_COMPOSE_PROJECT` may override it for an isolated test checkout, but
operators and deployment automation must not rely on Compose's directory-derived
default because that can create duplicate workers.

`pause` stops `lifecycle-worker` successfully before atomically creating the
non-secret `.lifecycle-worker-paused` marker. `resume` starts the worker
successfully before removing that marker. `status` reports both marker presence
and whether the consumer is running. Treat either of these combinations as an
inconsistent state requiring investigation:

- marker present and `consumer_running=true`;
- marker absent and `consumer_running=false` when lifecycle consumption is expected.

The staging deployment workflow builds an updated lifecycle image while paused,
but does not recreate or start the lifecycle worker. It also verifies that the
consumer remains stopped. Beat and completed ingestion tasks can still publish
lifecycle work; Redis retains the queue until the worker is resumed.

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
