"""Celery tasks for the ingestion service.

A single task — :func:`run_ingestion_task` — wraps :func:`src.main.run_ingestion`
and enforces a *cluster-wide* cap on the number of ingestion runs in flight via
a Redis-backed semaphore. The cap is configured by ``MAX_CONCURRENT_INGESTIONS``
and is independent of ``CELERY_WORKER_CONCURRENCY`` (which sets per-worker
process count). Default is 1 — i.e. only one ingestion runs at a time across
the entire cluster, regardless of how many workers are deployed.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from typing import TypedDict, cast

import redis
from celery import Task
from celery.exceptions import Reject, Retry
from neo4j import ManagedTransaction
from pydantic.types import JsonValue

from src.birthday import BirthdayRunSummary, run_birthday_greetings
from src.celery_app import celery_app
from src.config import get_settings
from src.connectors.whatsadmin_api.credentials import WHATSADMIN_ENTITIES
from src.errors import SourceNotConfiguredError
from src.graph import queries
from src.graph.client import Neo4jClient
from src.graph.migrations import (
    reconcile_projection_relationship_lifecycle,
    reconcile_source_record_lifecycle,
)
from src.ingestion_config import get_ingestion_config
from src.lifecycle_reconciliation_queue import (
    LifecycleReconciliationTask,
    release_lifecycle_reconciliation_queue_gate,
)
from src.llm import get_profile_analysis_service
from src.main import (
    IngestionSummary,
    finalize_ingest_run,
    initialize_ingestion_graph,
    run_ingestion,
    setup_logging,
)
from src.matching.pair_score import score_person_pair
from src.pipeline_person_pairs import _ENGINE_VERSION, _POLICY_VERSION
from src.profile_analysis_repository import Neo4jProfileAnalysisRepository
from src.profile_analysis_worker import run_profile_analysis_person
from src.profile_analysis_worker_types import (
    LlmProfileAnalysisTextService,
    ProfileAnalysisSweepSummary,
)

logger = logging.getLogger(__name__)

_INGEST_SEMAPHORE_KEY = "profile_unifier:ingestion:active"
_SOURCE_LOCK_PREFIX = "profile_unifier:ingestion:source"
_INIT_LOCK_KEY = "profile_unifier:ingestion:init"
# Leases are renewed while ingestion is running. Keeping the base TTL modest
# bounds the unavailable period after a worker crashes.
_LOCK_LEASE_SECONDS = 60 * 60
_LEASE_RENEWAL_INTERVAL_SECONDS = 10 * 60
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


@contextmanager
def _acquire_source_lock(source_key: str) -> Iterator[str]:
    """Reserve an ingestion lock scope or raise if that scope is already running."""
    client = _redis_client()
    lock_id = uuid.uuid4().hex
    lock_key = f"{_SOURCE_LOCK_PREFIX}:{source_key}"
    lock_acquired = client.set(lock_key, lock_id, nx=True, ex=_LOCK_LEASE_SECONDS)
    if not lock_acquired:
        raise _SourceAlreadyRunningError(source_key=source_key)

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
    """Return the independent source-and-mode locks required for an ingestion run."""
    source_mode_key = f"{source_key}:{mode}"
    if source_key != "whatsapp_chat":
        return (source_mode_key,)
    entities = (entity_key,) if mode == "api" and entity_key is not None else WHATSADMIN_ENTITIES
    return tuple(f"{source_mode_key}:{entity}" for entity in entities)


@contextmanager
def _acquire_source_locks(
    source_keys: tuple[str, ...],
) -> Iterator[tuple[_SourceLockLease, ...]]:
    """Acquire all ingestion locks, releasing earlier locks if a later one is busy."""
    with ExitStack() as stack:
        leases: list[_SourceLockLease] = []
        for source_key in source_keys:
            lock_id = stack.enter_context(_acquire_source_lock(source_key))
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
    def __init__(self, source_key: str) -> None:
        super().__init__(f"Ingestion lock already held: {source_key}")
        self.source_key = source_key


class LifecycleReconciliationSummary(TypedDict):
    status: str
    source_records: int
    projections: int


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


def _empty_profile_analysis_summary(
    *,
    unexpected_failures: int = 0,
    has_more: bool = False,
) -> ProfileAnalysisSweepSummary:
    return {
        "claimed": 0,
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "obsolete": 0,
        "unexpected_failures": unexpected_failures,
        "released": 0,
        "has_more": has_more,
    }


def _run_profile_analysis_request_once(
    request_id: str,
) -> tuple[ProfileAnalysisSweepSummary, datetime | None]:
    """Claim and process exactly one durable on-demand analysis request."""
    settings = get_settings()
    profile_config = settings.profile_analysis
    llm_config = get_ingestion_config().llm
    client = Neo4jClient(settings)
    claim_token = uuid.uuid4().hex
    repository = Neo4jProfileAnalysisRepository(client)
    request_claimed = False
    try:
        now = datetime.now(UTC)
        person = repository.claim_request(
            request_id=request_id,
            claim_token=claim_token,
            now=now,
            claim_until=now + profile_config.claim_lease,
        )
        if person is None:
            # A queued request may be waiting behind the other analysis type's
            # Person-level lease. Terminal, missing, and inactive requests must
            # not consume all Celery retries on duplicate delivery.
            waiting = repository.request_is_waiting(request_id=request_id)
            return _empty_profile_analysis_summary(has_more=waiting), now if waiting else None
        request_claimed = True
        text_service = LlmProfileAnalysisTextService(get_profile_analysis_service())
        summary = run_profile_analysis_person(
            repository=repository,
            text_service=text_service,
            person=person,
            claim_token=claim_token,
            claim_lease=profile_config.claim_lease,
            max_attempts=profile_config.retry_limit,
            retry_base=timedelta(seconds=llm_config.retry_base_delay_seconds),
            retry_cap=timedelta(seconds=llm_config.retry_max_delay_seconds),
            clock=lambda: datetime.now(UTC),
        )
        request_status = (
            "succeeded"
            if summary["succeeded"] > 0
            else "obsolete"
            if summary["obsolete"] > 0
            else "failed"
        )
        next_retry_at = repository.requeue_request_if_retryable(request_id=request_id)
        if next_retry_at is not None:
            return summary, next_retry_at
        repository.complete_request(request_id=request_id, status=request_status)
        return summary, None
    except Exception:
        if request_claimed:
            try:
                repository.complete_request(request_id=request_id, status="failed")
            except Exception:
                logger.exception("Failed to finalize profile-analysis request %s", request_id)
        raise
    finally:
        client.close()


def run_lifecycle_reconciliation() -> LifecycleReconciliationSummary:
    """Repair lifecycle deltas while serializing concurrent repair attempts."""
    with _acquire_source_lock("lifecycle-reconciliation"), _acquire_init_lock():
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
) -> IngestionSummary:
    """Run a single ingestion under the cluster-wide concurrency cap."""
    # PR #62 introduced ``entity_key`` as the fourth positional task argument.
    # PR #63's API producer used that position for its Bitrix ingest-run ID.
    # Keep existing WhatsAdmin task messages valid while interpreting the
    # source-specific, otherwise-invalid Bitrix entity as the legacy run ID.
    if source_key == "bitrix_chat" and ingest_run_id is None and entity_key is not None:
        ingest_run_id = entity_key
        entity_key = None
    source_lock_keys = _source_lock_keys(source_key, mode, entity_key)
    try:
        settings = get_settings()
        setup_logging(settings.log_level)
        with _acquire_init_lock():
            initialize_ingestion_graph()
        with _acquire_source_locks(source_lock_keys) as source_lock_leases:
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
                _acquire_ingestion_slot(settings.max_concurrent_ingestions) as slot_id,
                _renew_ingestion_leases(source_lock_leases, slot_id),
            ):
                celery_task_id = self.request.id
                if celery_task_id is not None:
                    summary = run_ingestion(
                        source_key,
                        mode,
                        dump_path,
                        entity_key=entity_key,
                        initialize_graph=False,
                        existing_ingest_run_id=ingest_run_id,
                        task_id=str(celery_task_id),
                    )
                elif ingest_run_id is None:
                    if entity_key is None:
                        summary = run_ingestion(
                            source_key,
                            mode,
                            dump_path,
                            initialize_graph=False,
                        )
                    else:
                        summary = run_ingestion(
                            source_key,
                            mode,
                            dump_path,
                            entity_key=entity_key,
                            initialize_graph=False,
                        )
                else:
                    if entity_key is None:
                        summary = run_ingestion(
                            source_key,
                            mode,
                            dump_path,
                            initialize_graph=False,
                            existing_ingest_run_id=ingest_run_id,
                        )
                    else:
                        summary = run_ingestion(
                            source_key,
                            mode,
                            dump_path,
                            entity_key=entity_key,
                            initialize_graph=False,
                            existing_ingest_run_id=ingest_run_id,
                        )
                try:
                    run_lifecycle_reconciliation()
                except _SourceAlreadyRunningError:
                    logger.info("Lifecycle reconciliation is already running; skipping follow-up")
                except Exception:
                    logger.exception("Post-ingestion lifecycle reconciliation failed")
                return summary
    except _SourceAlreadyRunningError as exc:
        if ingest_run_id is not None:
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
    except Exception as exc:
        logger.exception("Ingestion task failed for %s", source_key)
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
    name="src.tasks.run_profile_analysis_request_task",
    bind=True,
    max_retries=120,
)
def run_profile_analysis_request_task(
    self: Task,
    request_id: str,
) -> ProfileAnalysisSweepSummary:
    """Run one user/page-requested Person analysis without scanning other Persons."""
    settings = get_settings()
    setup_logging(settings.log_level)
    if not settings.profile_analysis.enabled:
        return _empty_profile_analysis_summary()
    try:
        summary, retry_at = _run_profile_analysis_request_once(request_id)
        if retry_at is not None and self.request.retries < self.max_retries:
            countdown = max(1, int((retry_at - datetime.now(UTC)).total_seconds()))
            raise self.retry(countdown=countdown)
        return summary
    except Retry:
        raise
    except Exception:
        logger.error("Profile-analysis request failed; safe_code=request_failed")
        return _empty_profile_analysis_summary(unexpected_failures=1)


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
