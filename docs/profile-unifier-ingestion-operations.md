# Profile Unifier Ingestion Operations

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

All staging Compose operations use the canonical `stg-hyperp` project.
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
