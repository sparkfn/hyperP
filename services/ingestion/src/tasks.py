"""Celery tasks for the ingestion service.

A single task — :func:`run_ingestion_task` — wraps :func:`src.main.run_ingestion`
and enforces a fixed cluster-wide cap on the number of ingestion runs in flight
via a Redis-backed semaphore.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Final, TypedDict, cast

import redis
from celery import Task
from celery.exceptions import Reject
from neo4j import ManagedTransaction
from pydantic.types import JsonValue

from src.birthday import BirthdayRunSummary, run_birthday_greetings
from src.bitrix_ingestion_models import BitrixStreamKey, ExecutionContext
from src.celery_app import LIFECYCLE_QUEUE, celery_app
from src.config import get_settings
from src.connectors.whatsadmin_api.credentials import WHATSADMIN_ENTITIES
from src.errors import SourceNotConfiguredError
from src.graph import queries
from src.graph.client import Neo4jClient
from src.graph.ingestion_control import BitrixStreamControl, LogicalRunControl
from src.graph.migrations import (
    reconcile_projection_relationship_lifecycle,
    reconcile_source_record_lifecycle,
)
from src.lifecycle_reconciliation_queue import (
    LifecycleReconciliationTask,
    release_lifecycle_reconciliation_queue_gate,
)
from src.main import (
    IngestionSummary,
    finalize_ingest_run,
    initialize_ingestion_graph,
    run_ingestion,
    setup_logging,
)
from src.matching.pair_score import score_person_pair
from src.pipeline_knows import KnowsMaterializationPhase, materialize_knows_batch
from src.pipeline_person_pairs import _ENGINE_VERSION, _POLICY_VERSION
from src.resumable import AttemptStatus, CheckpointDescriptor

logger = logging.getLogger(__name__)

_INGEST_SEMAPHORE_KEY = "profile_unifier:ingestion:active"
MAX_CONCURRENT_INGESTIONS: Final[int] = 2
_SOURCE_LOCK_PREFIX = "profile_unifier:ingestion:source"
_INIT_LOCK_KEY = "profile_unifier:ingestion:init"
_LEGACY_SOURCE_LOCK_MODES = ("api", "backfill", "batch", "dump")
# Leases are renewed while ingestion is running. Keeping the base TTL modest
# bounds the unavailable period after a worker crashes.
_LOCK_LEASE_SECONDS = 60 * 60
_LEASE_RENEWAL_INTERVAL_SECONDS = 10 * 60
_SCHEDULED_STEP_MARKER_SECONDS = 60 * 60 * 24 * 8
_SOURCE_LOCK_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""
_SOURCE_LOCK_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""
_INGEST_SEMAPHORE_RENEW_SCRIPT = """
if redis.call('zscore', KEYS[1], ARGV[1]) == false then
    return 0
end
redis.call('zadd', KEYS[1], ARGV[2], ARGV[1])
return redis.call('expire', KEYS[1], ARGV[3])
"""
_TERMINAL_INGEST_RUN_STATUSES = frozenset(
    {"already_running", "completed", "completed_with_errors", "failed"}
)

_BITRIX_SPLIT_CHECKPOINTS: Final[
    dict[BitrixStreamKey, tuple[str, dict[str, JsonValue], str, str]]
] = {
    "crm_deals": (
        "scoped_deal_census_v1",
        {"last_deal_id": None, "census_epoch": 1},
        "bitrix-crm-deals-keyset-v1",
        "exclusive_last_deal_id",
    ),
    "crm_activities": (
        "crm_activity_keyset_v1",
        {"last_activity_id": None},
        "bitrix-crm-activity-keyset-v1",
        "exclusive_last_activity_id",
    ),
    "openlines_conversations": (
        "openlines_conversation_replay_v1",
        {"crm_start": None},
        "bitrix-openlines-replay-v1",
        "at_least_once_page_start",
    ),
}


def _finalize_dispatched_run(ingest_run_id: str, status: str) -> None:
    """Finalize a run created by the API before task-level locking."""
    client = Neo4jClient(get_settings())
    try:
        finalize_ingest_run(client, ingest_run_id, status, 0, 0)
    finally:
        client.close()


def _finalize_rejected_dispatched_run(ingest_run_id: str | None) -> None:
    """Best-effort finalization for a task rejected before normal run cleanup."""
    if ingest_run_id is None:
        return
    try:
        status = _get_existing_ingest_run_status(ingest_run_id)
        if status in _TERMINAL_INGEST_RUN_STATUSES:
            return
        _finalize_dispatched_run(ingest_run_id, "failed")
    except Exception:
        logger.exception("Failed to finalize rejected IngestRun %s", ingest_run_id)


def _split_checkpoint(stream_key: BitrixStreamKey) -> CheckpointDescriptor:
    phase, cursor, connector_version, replay_boundary = _BITRIX_SPLIT_CHECKPOINTS[stream_key]
    return CheckpointDescriptor(
        phase=phase,
        cursor=dict(cursor),
        source_window={},
        last_committed_record_id=None,
        connector_version=connector_version,
        schema_version=1,
        replay_boundary=replay_boundary,
    )


def _split_configuration_fingerprint(
    *, source_key: str, mode: str, stream_key: BitrixStreamKey, incremental: bool
) -> str:
    payload = json.dumps(
        {
            "source_key": source_key,
            "mode": mode,
            "stream_key": stream_key,
            "incremental": incremental,
            "checkpoint_schema_version": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _skipped_split_summary(
    *,
    ingest_run_id: str,
    status: str,
    source_key: str,
    mode: str,
    dump_path: str | None,
) -> IngestionSummary:
    return {
        "ingest_run_id": ingest_run_id,
        "status": status,
        "succeeded": 0,
        "errors": 0,
        "skipped": 1,
        "source_key": source_key,
        "mode": mode,
        "dump_path": dump_path,
        "entity_key": None,
    }


def _run_split_bitrix_ingestion(
    *,
    source_key: str,
    mode: str,
    dump_path: str | None,
    incremental: bool,
    idempotency_key: str,
    stream_key: BitrixStreamKey,
    worker_task_id: str,
) -> IngestionSummary:
    """Create, claim, fence, execute, and terminate one canonical split attempt."""
    checkpoint = _split_checkpoint(stream_key)
    client = Neo4jClient(get_settings())
    logical = LogicalRunControl(client)
    try:
        attempt = logical.create_or_reuse(
            source_key=source_key,
            mode=mode,
            dump_path=dump_path,
            entity_key=None,
            idempotency_key=idempotency_key,
            worker_task_id=worker_task_id,
            configuration_fingerprint=_split_configuration_fingerprint(
                source_key=source_key,
                mode=mode,
                stream_key=stream_key,
                incremental=incremental,
            ),
            connector_version=checkpoint.connector_version,
            checkpoint_schema_version=checkpoint.schema_version,
            initial_checkpoint=checkpoint,
        )
        if attempt.worker_task_id != worker_task_id:
            status = (
                attempt.logical_status
                if attempt.logical_status in _TERMINAL_INGEST_RUN_STATUSES
                else "already_running"
            )
            return _skipped_split_summary(
                ingest_run_id=attempt.ingest_run_id,
                status=status,
                source_key=source_key,
                mode=mode,
                dump_path=dump_path,
            )
        if not logical.claim(
            logical_run_id=attempt.logical_run_id,
            ingest_run_id=attempt.ingest_run_id,
            generation=attempt.generation,
            worker_task_id=worker_task_id,
        ):
            return _skipped_split_summary(
                ingest_run_id=attempt.ingest_run_id,
                status=attempt.logical_status,
                source_key=source_key,
                mode=mode,
                dump_path=dump_path,
            )
        try:
            admission = BitrixStreamControl(client).admit_or_coalesce(
                stream_key=stream_key,
                logical_run_id=attempt.logical_run_id,
                ingest_run_id=attempt.ingest_run_id,
                attempt_generation=attempt.generation,
                worker_task_id=worker_task_id,
            )
        except Exception as exc:
            logical.fail(
                logical_run_id=attempt.logical_run_id,
                ingest_run_id=attempt.ingest_run_id,
                generation=attempt.generation,
                failure_category="stream_admission_failed",
                safe_failure_message=str(exc),
            )
            raise
        context = ExecutionContext(
            worker_task_id=worker_task_id,
            fence_context=admission.fence_context,
        )
        try:
            summary = run_ingestion(
                source_key,
                mode,
                dump_path,
                initialize_graph=False,
                incremental=incremental,
                bitrix_execution_stream=stream_key,
                execution_context=context,
            )
        except Exception as exc:
            logical.fail_fenced(
                context=admission.fence_context,
                failure_category=type(exc).__name__,
                safe_failure_message=str(exc),
            )
            raise
        completion_status = summary["status"]
        if completion_status not in {"completed", "completed_with_errors"}:
            raise RuntimeError(f"split Bitrix runner returned invalid status {completion_status}")
        logical.finalize_fenced(
            context=admission.fence_context,
            phase=checkpoint.phase,
            status=cast(AttemptStatus, completion_status),
            committed_count=summary["succeeded"],
            duplicate_count=summary["skipped"],
            excluded_count=0,
            retry_count=0,
            record_count=summary["succeeded"] + summary["errors"] + summary["skipped"],
            rejected_count=summary["errors"],
        )
        return summary
    finally:
        client.close()


def _get_existing_ingest_run_status(ingest_run_id: str) -> str | None:
    """Read the current status for a run dispatched by the API."""
    client = Neo4jClient(get_settings())

    def _read(tx: ManagedTransaction) -> str | None:
        record = tx.run(
            queries.GET_INGEST_RUN_STATUS,
            ingest_run_id=ingest_run_id,
        ).single()
        if record is None:
            return None
        status = record["status"]
        return status if isinstance(status, str) else None

    try:
        return client.execute_read(_read)
    finally:
        client.close()


type _SourceLockLease = tuple[str, str]


def _redis_client() -> redis.Redis:
    settings = get_settings()
    return redis.Redis.from_url(settings.celery_broker_url)


def _scheduled_step_completed(idempotency_key: str) -> bool:
    """Return whether this logical chain step completed cleanly."""
    with _redis_client() as client:
        return client.get(idempotency_key) is not None


def _mark_scheduled_step_completed(idempotency_key: str) -> None:
    """Retain a completion marker through the next weekly occurrence."""
    with _redis_client() as client:
        client.set(idempotency_key, "completed", ex=_SCHEDULED_STEP_MARKER_SECONDS)


def _try_acquire_slot(client: redis.Redis, member_key: str, max_slots: int) -> None:
    """Atomically reserve a semaphore slot via WATCH/MULTI or raise."""
    now = int(time.time())
    expiry = now + _LOCK_LEASE_SECONDS
    with client.pipeline() as pipe:
        while True:
            try:
                pipe.watch(_INGEST_SEMAPHORE_KEY)
                pipe.zremrangebyscore(_INGEST_SEMAPHORE_KEY, 0, now)
                zcard_result = pipe.zcard(_INGEST_SEMAPHORE_KEY)
                assert isinstance(zcard_result, int)
                if zcard_result >= max_slots:
                    pipe.unwatch()
                    raise _SlotUnavailableError(live=zcard_result, cap=max_slots)
                pipe.multi()
                pipe.zadd(_INGEST_SEMAPHORE_KEY, {member_key: expiry})
                pipe.expire(_INGEST_SEMAPHORE_KEY, _LOCK_LEASE_SECONDS + 60)
                pipe.execute()
                break
            except redis.WatchError:
                continue


@contextmanager
def _acquire_ingestion_slot(max_slots: int) -> Iterator[str]:
    """Reserve one ingestion slot in Redis or raise if the cluster is full."""
    client = _redis_client()
    slot_id = uuid.uuid4().hex
    member_key = f"{_INGEST_SEMAPHORE_KEY}:{slot_id}"
    _try_acquire_slot(client, member_key, max_slots)

    logger.info("Acquired ingestion slot %s (cap=%d)", slot_id, max_slots)
    try:
        yield slot_id
    finally:
        try:
            client.zrem(_INGEST_SEMAPHORE_KEY, member_key)
            logger.info("Released ingestion slot %s", slot_id)
        except Exception:
            logger.exception("Failed to release ingestion slot %s", slot_id)


def _lock_owner(client: redis.Redis, lock_key: str) -> str | None:
    """Read a Redis lock owner, normalizing Redis's bytes response."""
    value = client.get(lock_key)
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value if isinstance(value, str) else None


@contextmanager
def _acquire_source_lock(source_key: str, owner_id: str | None = None) -> Iterator[str]:
    """Reserve an ingestion lock scope or raise if that scope is already running."""
    client = _redis_client()
    lock_id = owner_id or uuid.uuid4().hex
    lock_key = f"{_SOURCE_LOCK_PREFIX}:{source_key}"
    while not client.set(lock_key, lock_id, nx=True, ex=_LOCK_LEASE_SECONDS):
        current_owner = _lock_owner(client, lock_key)
        if current_owner is None:
            # The lease expired between SET NX and GET. Retry acquisition
            # rather than misclassifying the delivery as a distinct duplicate.
            continue
        raise _SourceAlreadyRunningError(
            source_key=source_key,
            held_by_same_task=owner_id is not None and current_owner == owner_id,
        )

    logger.info("Acquired ingestion lock for %s", source_key)
    try:
        yield lock_id
    finally:
        try:
            client.eval(_SOURCE_LOCK_RELEASE_SCRIPT, 1, lock_key, lock_id)
            logger.info("Released ingestion lock for %s", source_key)
        except Exception:
            logger.exception("Failed to release ingestion lock for %s", source_key)


def _source_lock_keys(
    source_key: str,
    mode: str,
    entity_key: str | None,
) -> tuple[str, ...]:
    """Return source scopes, including legacy mode keys for rolling upgrades."""
    legacy_modes = tuple(sorted({*_LEGACY_SOURCE_LOCK_MODES, mode}))
    if source_key != "whatsapp_chat":
        return (source_key, *(f"{source_key}:{legacy_mode}" for legacy_mode in legacy_modes))
    entities = (entity_key,) if mode == "api" and entity_key is not None else WHATSADMIN_ENTITIES
    return tuple(
        lock_key
        for entity in entities
        for lock_key in (
            f"{source_key}:{entity}",
            *(f"{source_key}:{legacy_mode}:{entity}" for legacy_mode in legacy_modes),
        )
    )


@contextmanager
def _acquire_source_locks(
    source_keys: tuple[str, ...],
    owner_id: str | None = None,
) -> Iterator[tuple[_SourceLockLease, ...]]:
    """Acquire all ingestion locks, releasing earlier locks if a later one is busy."""
    with ExitStack() as stack:
        leases: list[_SourceLockLease] = []
        for source_key in source_keys:
            lock_context = (
                _acquire_source_lock(source_key, owner_id)
                if owner_id is not None
                else _acquire_source_lock(source_key)
            )
            lock_id = stack.enter_context(lock_context)
            leases.append((source_key, lock_id))
        yield tuple(leases)


def _renew_ingestion_slot(client: redis.Redis, slot_id: str) -> None:
    """Extend an existing semaphore reservation without recreating it."""
    member_key = f"{_INGEST_SEMAPHORE_KEY}:{slot_id}"
    expiry = int(time.time()) + _LOCK_LEASE_SECONDS
    # ZADD without CH reports newly-added members, not updated ones. Renew in
    # Lua so checking membership, updating its score, and refreshing the key's
    # TTL are atomic.
    renewed = cast(
        int,
        client.eval(
            _INGEST_SEMAPHORE_RENEW_SCRIPT,
            1,
            _INGEST_SEMAPHORE_KEY,
            member_key,
            str(expiry),
            str(_LOCK_LEASE_SECONDS + 60),
        ),
    )
    if renewed != 1:
        raise RuntimeError(f"Ingestion slot {slot_id} was lost before renewal")


def _renew_source_lock(client: redis.Redis, source_key: str, lock_id: str) -> None:
    """Extend an ingestion lock only when this task still owns it."""
    lock_key = f"{_SOURCE_LOCK_PREFIX}:{source_key}"
    renewed = cast(
        int,
        client.eval(
            _SOURCE_LOCK_RENEW_SCRIPT,
            1,
            lock_key,
            lock_id,
            str(_LOCK_LEASE_SECONDS),
        ),
    )
    if renewed != 1:
        raise RuntimeError(f"Ingestion lock for {source_key} was lost before renewal")


def _renew_init_lock(client: redis.Redis, lock_id: str) -> None:
    """Extend graph initialization exclusivity only while still owned."""
    renewed = cast(
        int,
        client.eval(
            _SOURCE_LOCK_RENEW_SCRIPT,
            1,
            _INIT_LOCK_KEY,
            lock_id,
            str(_LOCK_LEASE_SECONDS),
        ),
    )
    if renewed != 1:
        raise RuntimeError("Ingestion graph initialization lock was lost before renewal")


def _renew_source_locks(
    client: redis.Redis,
    source_lock_leases: tuple[_SourceLockLease, ...],
) -> None:
    for source_key, lock_id in source_lock_leases:
        _renew_source_lock(client, source_key, lock_id)


@contextmanager
def _renew_ingestion_leases(
    source_lock_leases: tuple[_SourceLockLease, ...],
    slot_id: str,
) -> Iterator[None]:
    """Keep long-running ingestion leases alive, stopping promptly on completion."""
    stop_event = threading.Event()
    source_label = ",".join(source_key for source_key, _lock_id in source_lock_leases)

    def renew() -> None:
        while not stop_event.wait(_LEASE_RENEWAL_INTERVAL_SECONDS):
            try:
                client = _redis_client()
                _renew_source_locks(client, source_lock_leases)
                _renew_ingestion_slot(client, slot_id)
            except Exception:
                logger.critical(
                    "Failed to renew ingestion leases for %s; terminating worker "
                    "to prevent concurrent ingestion",
                    source_label,
                    exc_info=True,
                )
                # A task cannot safely continue after losing its distributed
                # leases. With late acknowledgement, Celery redelivers it when
                # this worker process exits.
                os.kill(os.getpid(), signal.SIGTERM)
                return

    renewal_thread = threading.Thread(
        target=renew,
        name=f"ingestion-lease-renewal:{source_label}",
        daemon=True,
    )
    renewal_thread.start()
    try:
        yield
    finally:
        stop_event.set()
        renewal_thread.join(timeout=1)


@contextmanager
def _renew_init_lock_lease(lock_id: str) -> Iterator[None]:
    """Keep the initialization lock alive for the whole protected section."""
    stop_event = threading.Event()

    def renew() -> None:
        while not stop_event.wait(_LEASE_RENEWAL_INTERVAL_SECONDS):
            try:
                _renew_init_lock(_redis_client(), lock_id)
            except Exception:
                logger.critical(
                    "Failed to renew ingestion graph initialization lock; terminating worker",
                    exc_info=True,
                )
                os.kill(os.getpid(), signal.SIGTERM)
                return

    renewal_thread = threading.Thread(
        target=renew,
        name="ingestion-init-lock-renewal",
        daemon=True,
    )
    renewal_thread.start()
    try:
        yield
    finally:
        stop_event.set()
        renewal_thread.join(timeout=1)


@contextmanager
def _acquire_init_lock() -> Iterator[str]:
    client = _redis_client()
    lock_id = uuid.uuid4().hex
    while True:
        lock_acquired = client.set(_INIT_LOCK_KEY, lock_id, nx=True, ex=_LOCK_LEASE_SECONDS)
        if lock_acquired:
            break
        logger.info("Waiting for ingestion graph initialization lock")
        time.sleep(1.0)

    logger.info("Acquired ingestion graph initialization lock")
    try:
        yield lock_id
    finally:
        try:
            client.eval(_SOURCE_LOCK_RELEASE_SCRIPT, 1, _INIT_LOCK_KEY, lock_id)
            logger.info("Released ingestion graph initialization lock")
        except Exception:
            logger.exception("Failed to release ingestion graph initialization lock")


class _SlotUnavailableError(Exception):
    def __init__(self, live: int, cap: int) -> None:
        super().__init__(f"All ingestion slots in use ({live}/{cap})")
        self.live = live
        self.cap = cap


class _SourceAlreadyRunningError(Exception):
    def __init__(self, source_key: str, held_by_same_task: bool = False) -> None:
        super().__init__(f"Ingestion lock already held: {source_key}")
        self.source_key = source_key
        self.held_by_same_task = held_by_same_task


class LifecycleReconciliationSummary(TypedDict):
    status: str
    source_records: int
    projections: int


class KnowsMaterializationSummary(TypedDict):
    """JSON-safe result emitted by a bounded deferred KNOWS task."""

    phase: KnowsMaterializationPhase
    linked: int
    scanned: int
    complete: bool
    next_cursor: str | None


def _terminal_run_summary(
    ingest_run_id: str,
    status: str,
    source_key: str,
    mode: str,
    dump_path: str | None,
    entity_key: str | None,
) -> IngestionSummary:
    return {
        "ingest_run_id": ingest_run_id,
        "status": status,
        "succeeded": 0,
        "errors": 0,
        "skipped": 1,
        "source_key": source_key,
        "mode": mode,
        "dump_path": dump_path,
        "entity_key": entity_key,
    }


def run_lifecycle_reconciliation() -> LifecycleReconciliationSummary:
    """Repair lifecycle deltas while serializing concurrent repair attempts."""
    with _acquire_source_lock("lifecycle-reconciliation"), _acquire_init_lock() as init_lock_id:
        with _renew_init_lock_lease(init_lock_id):
            settings = get_settings()
            client = Neo4jClient(settings)
            try:
                client.verify_connectivity()
                source_records = reconcile_source_record_lifecycle(client)
                projections = reconcile_projection_relationship_lifecycle(client)
            finally:
                client.close()
    return {
        "status": "complete",
        "source_records": source_records,
        "projections": projections,
    }


def _knows_phase_for_source(source_key: str) -> KnowsMaterializationPhase | None:
    """Return the deferred projection phase affected by one source ingestion."""
    if source_key == "fundbox:contacts":
        return "contacts"
    if source_key in {"bitrix_chat", "whatsapp_chat"}:
        return "chat_relationships"
    return None


def _enqueue_knows_materialization(source_key: str) -> None:
    """Best-effort enqueue; source ingestion must never wait for a global sweep."""
    phase = _knows_phase_for_source(source_key)
    if phase is None:
        return
    try:
        materialize_knows_task.apply_async(args=(phase,), queue=LIFECYCLE_QUEUE)
    except Exception:
        logger.exception("Could not queue deferred KNOWS materialization phase=%s", phase)


@celery_app.task(
    name="src.tasks.materialize_knows_task",
    bind=True,
    acks_late=True,
    soft_time_limit=300,
    time_limit=330,
    max_retries=0,
)
def materialize_knows_task(
    self: Task,
    phase: KnowsMaterializationPhase,
    cursor: str = "",
) -> KnowsMaterializationSummary:
    """Process one bounded, locked KNOWS batch and continue from its cursor."""
    settings = get_settings()
    setup_logging(settings.log_level)
    started = time.monotonic()
    try:
        with (
            _acquire_source_lock(f"knows-materialization:{phase}"),
            _acquire_init_lock() as init_lock_id,
        ):
            with _renew_init_lock_lease(init_lock_id):
                initialize_ingestion_graph()
                client = Neo4jClient(settings)
                try:
                    result = materialize_knows_batch(client, phase, cursor=cursor)
                finally:
                    client.close()
    except _SourceAlreadyRunningError:
        logger.info("KNOWS materialization phase=%s is already running; skipping duplicate", phase)
        return {
            "phase": phase,
            "linked": 0,
            "scanned": 0,
            "complete": False,
            "next_cursor": cursor,
        }
    except Exception as exc:
        logger.exception("KNOWS materialization failed phase=%s cursor=%s", phase, cursor)
        raise Reject(str(exc), requeue=False) from exc

    next_cursor = result["next_cursor"]
    complete = next_cursor is None
    elapsed = time.monotonic() - started
    logger.info(
        "KNOWS materialization phase=%s cursor=%s scanned=%d linked=%d next_cursor=%s "
        "complete=%s elapsed_seconds=%.3f",
        phase,
        cursor,
        result["scanned"],
        result["linked"],
        next_cursor,
        complete,
        elapsed,
    )
    if next_cursor is not None:
        materialize_knows_task.apply_async(args=(phase, next_cursor), queue=LIFECYCLE_QUEUE)
    return {
        "phase": phase,
        "linked": result["linked"],
        "scanned": result["scanned"],
        "complete": complete,
        "next_cursor": next_cursor,
    }


@celery_app.task(
    name="src.tasks.run_ingestion_task",
    bind=True,
    # Preserve tasks when a worker exits. A duplicate delivered after Redis's
    # visibility timeout is safely treated as a no-op while the source-and-mode
    # lock is held by the original task.
    acks_late=True,
    autoretry_for=(_SlotUnavailableError,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=None,  # keep retrying — eventually a slot frees up
)
def run_ingestion_task(
    self: Task,
    source_key: str,
    mode: str = "batch",
    dump_path: str | None = None,
    entity_key: str | None = None,
    ingest_run_id: str | None = None,
    incremental: bool = True,
    wait_for_source: bool = False,
    require_clean_completion: bool = False,
    idempotency_key: str | None = None,
    bitrix_execution_stream: BitrixStreamKey | None = None,
) -> IngestionSummary:
    """Run a single ingestion under the cluster-wide concurrency cap."""
    # PR #62 introduced ``entity_key`` as the fourth positional task argument.
    # PR #63's API producer used that position for its Bitrix ingest-run ID.
    # Keep existing WhatsAdmin task messages valid while interpreting the
    # source-specific, otherwise-invalid Bitrix entity as the legacy run ID.
    if source_key == "bitrix_chat" and ingest_run_id is None and entity_key is not None:
        ingest_run_id = entity_key
        entity_key = None
    split_bitrix = bitrix_execution_stream is not None
    if split_bitrix:
        if source_key != "bitrix_chat":
            raise ValueError("split Bitrix streams require source_key='bitrix_chat'")
        if mode not in {"api", "backfill"}:
            raise ValueError("split Bitrix streams require API or backfill mode")
        if ingest_run_id is not None:
            raise ValueError("split Bitrix tasks cannot receive a legacy ingest_run_id")
        if idempotency_key is None or not idempotency_key.strip():
            raise ValueError("split Bitrix tasks require a stable idempotency_key")
        if entity_key is not None:
            raise ValueError("split Bitrix tasks do not accept entity_key")
    source_lock_keys = _source_lock_keys(source_key, mode, entity_key)
    try:
        if (
            not split_bitrix
            and idempotency_key is not None
            and _scheduled_step_completed(idempotency_key)
        ):
            return {
                "ingest_run_id": "",
                "status": "completed",
                "succeeded": 0,
                "errors": 0,
                "skipped": 1,
                "source_key": source_key,
                "mode": mode,
                "dump_path": dump_path,
                "entity_key": entity_key,
            }
        settings = get_settings()
        setup_logging(settings.log_level)
        with _acquire_init_lock() as init_lock_id, _renew_init_lock_lease(init_lock_id):
            initialize_ingestion_graph()
        celery_task_id = self.request.id
        source_lock_owner = str(celery_task_id) if celery_task_id is not None else None
        with _acquire_source_locks(source_lock_keys, source_lock_owner) as source_lock_leases:
            if ingest_run_id is not None:
                status = _get_existing_ingest_run_status(ingest_run_id)
                if status in _TERMINAL_INGEST_RUN_STATUSES:
                    logger.info(
                        "IngestRun %s is already terminal (%s); skipping",
                        ingest_run_id,
                        status,
                    )
                    assert status is not None
                    return _terminal_run_summary(
                        ingest_run_id,
                        status,
                        source_key,
                        mode,
                        dump_path,
                        entity_key,
                    )
            with (
                _acquire_ingestion_slot(MAX_CONCURRENT_INGESTIONS) as slot_id,
                _renew_ingestion_leases(source_lock_leases, slot_id),
            ):
                if split_bitrix:
                    if celery_task_id is None:
                        raise ValueError("split Bitrix tasks require a Celery worker task ID")
                    assert idempotency_key is not None
                    assert bitrix_execution_stream is not None
                    summary = _run_split_bitrix_ingestion(
                        source_key=source_key,
                        mode=mode,
                        dump_path=dump_path,
                        incremental=incremental,
                        idempotency_key=idempotency_key,
                        stream_key=bitrix_execution_stream,
                        worker_task_id=str(celery_task_id),
                    )
                elif celery_task_id is not None:
                    summary = run_ingestion(
                        source_key,
                        mode,
                        dump_path,
                        entity_key=entity_key,
                        initialize_graph=False,
                        existing_ingest_run_id=ingest_run_id,
                        task_id=str(celery_task_id),
                        incremental=incremental,
                    )
                elif ingest_run_id is None:
                    if entity_key is None:
                        summary = run_ingestion(
                            source_key,
                            mode,
                            dump_path,
                            initialize_graph=False,
                            incremental=incremental,
                        )
                    else:
                        summary = run_ingestion(
                            source_key,
                            mode,
                            dump_path,
                            entity_key=entity_key,
                            initialize_graph=False,
                            incremental=incremental,
                        )
                else:
                    if entity_key is None:
                        summary = run_ingestion(
                            source_key,
                            mode,
                            dump_path,
                            initialize_graph=False,
                            existing_ingest_run_id=ingest_run_id,
                            incremental=incremental,
                        )
                    else:
                        summary = run_ingestion(
                            source_key,
                            mode,
                            dump_path,
                            entity_key=entity_key,
                            initialize_graph=False,
                            existing_ingest_run_id=ingest_run_id,
                            incremental=incremental,
                        )
                if require_clean_completion and summary["status"] != "completed":
                    raise Reject(
                        f"Scheduled ingestion {source_key} returned {summary['status']}",
                        requeue=False,
                    )
                try:
                    reconcile_lifecycle_task.apply_async(queue=LIFECYCLE_QUEUE)
                except Exception:
                    logger.exception("Could not queue post-ingestion lifecycle reconciliation")
                if (
                    not split_bitrix
                    and idempotency_key is not None
                    and summary["status"] == "completed"
                ):
                    _mark_scheduled_step_completed(idempotency_key)
                if celery_task_id is not None and summary.get("status") in {
                    "completed",
                    "completed_with_errors",
                }:
                    _enqueue_knows_materialization(source_key)
                return summary
    except _SourceAlreadyRunningError as exc:
        if split_bitrix or exc.held_by_same_task or ingest_run_id is not None or wait_for_source:
            retry_number = min(self.request.retries, 8)
            countdown = min(2**retry_number, 300)
            logger.warning(
                "Ingestion lock %s is already held; retrying dispatched run %s",
                exc.source_key,
                ingest_run_id,
            )
            raise self.retry(exc=exc, countdown=countdown) from exc
        logger.warning(
            "Ingestion lock %s is already held; skipping duplicate",
            exc.source_key,
        )
        return {
            "ingest_run_id": ingest_run_id or "",
            "status": "already_running",
            "succeeded": 0,
            "errors": 0,
            "skipped": 1,
            "source_key": source_key,
            "mode": mode,
            "dump_path": dump_path,
            "entity_key": entity_key,
        }
    except _SlotUnavailableError as exc:
        logger.warning("Ingestion slot unavailable (%d/%d), retrying...", exc.live, exc.cap)
        raise
    except SourceNotConfiguredError as exc:
        # Pre-provisioning state: the source is scheduled but its env isn't set
        # yet. Log a clean warning (no traceback) and reject without retry so
        # beat firing on the cron doesn't flood the logs.
        logger.warning("Ingestion source %s not configured: %s", source_key, exc)
        _finalize_rejected_dispatched_run(ingest_run_id)
        raise Reject(str(exc), requeue=False) from exc
    except Reject:
        raise
    except Exception as exc:
        logger.exception("Ingestion task failed for %s", source_key)
        if split_bitrix:
            retry_number = min(self.request.retries, 8)
            countdown = min(2**retry_number, 300)
            raise self.retry(exc=exc, countdown=countdown) from exc
        # Don't retry on real errors — surface them to the caller.
        _finalize_rejected_dispatched_run(ingest_run_id)
        raise Reject(str(exc), requeue=False) from exc


@celery_app.task(
    name="src.tasks.reconcile_lifecycle_task",
    bind=True,
    base=LifecycleReconciliationTask,
    max_retries=0,
)
def reconcile_lifecycle_task(self: Task) -> LifecycleReconciliationSummary:
    """Periodically repair lifecycle state for late-arriving legacy records."""
    settings = get_settings()
    setup_logging(settings.log_level)
    try:
        try:
            return run_lifecycle_reconciliation()
        except _SourceAlreadyRunningError:
            logger.info("Lifecycle reconciliation is already running; skipping duplicate")
            return {
                "status": "already_running",
                "source_records": 0,
                "projections": 0,
            }
        except Exception as exc:
            logger.exception("Lifecycle reconciliation failed")
            raise Reject(str(exc), requeue=False) from exc
    finally:
        task_id = self.request.id
        if task_id is not None:
            release_lifecycle_reconciliation_queue_gate(str(task_id))


@celery_app.task(
    name="src.tasks.send_birthday_messages_task",
    bind=True,
    max_retries=0,
)
def send_birthday_messages_task(self: Task) -> BirthdayRunSummary:
    """Send a birthday WhatsApp message to every person whose DOB is today."""
    settings = get_settings()
    setup_logging(settings.log_level)
    try:
        return run_birthday_greetings()
    except Exception as exc:
        logger.exception("Birthday greeting task failed")
        raise Reject(str(exc), requeue=False) from exc


@celery_app.task(
    name="src.tasks.recalculate_pair_audit_match_task",
    bind=True,
    max_retries=0,
)
def recalculate_pair_audit_match_task(self: Task, review_case_id: str) -> str:
    """Re-score a person-pair review case after its persons changed (e.g. merge).

    The current left/right persons are read from the review case, scored with the
    Layer-2 heuristic engine used for pair audits, and the MatchDecision is
    updated with the new confidence, reasons and feature snapshot. The review
    case decision stays ``'review'``: pair audits are advisory and never
    auto-merge persons.
    """
    settings = get_settings()
    setup_logging(settings.log_level)
    client = Neo4jClient(settings)
    try:
        client.execute_write(lambda tx: _recalculate_pair_audit_match_in_tx(tx, review_case_id))
    except Exception as exc:
        logger.exception("Pair-audit recalculation failed for %s", review_case_id)
        raise Reject(str(exc), requeue=False) from exc
    finally:
        client.close()
    return review_case_id


def _recalculate_pair_audit_match_in_tx(tx: ManagedTransaction, review_case_id: str) -> str:
    """Transaction body that re-scores a single person-pair review case."""
    get_result = tx.run(
        queries.GET_PERSON_PAIR_REVIEW_CASE,
        review_case_id=review_case_id,
    )
    record = get_result.single()
    if record is None:
        logger.warning("No actionable person-pair review case found for %s", review_case_id)
        return review_case_id
    left_person_id = str(record["left_person_id"])
    right_person_id = str(record["right_person_id"])
    raw_snapshot = record.get("feature_snapshot")
    old_snapshot = _parse_feature_snapshot(raw_snapshot) if isinstance(raw_snapshot, str) else {}
    score = score_person_pair(tx, left_person_id, right_person_id)
    new_snapshot: dict[str, JsonValue] = {
        **old_snapshot,
        "heuristic_band": score.decision.value,
        **score.feature_snapshot,
    }
    update_result = tx.run(
        queries.UPDATE_PAIR_AUDIT_MATCH_DECISION,
        review_case_id=review_case_id,
        confidence=score.confidence,
        decision="review",
        reasons=score.reasons,
        feature_snapshot=json.dumps(new_snapshot),
        engine_version=_ENGINE_VERSION,
        policy_version=_POLICY_VERSION,
    )
    if update_result.single() is None:
        logger.warning("Match decision update skipped for %s", review_case_id)
    return review_case_id


def _parse_feature_snapshot(raw: str) -> dict[str, JsonValue]:
    """Parse a stored JSON feature snapshot, returning an empty dict on failure."""
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        logger.warning("Failed to decode feature snapshot: %r", raw)
    return {}
