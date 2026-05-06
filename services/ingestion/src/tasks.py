"""Celery tasks for the ingestion service.

A single task — :func:`run_ingestion_task` — wraps :func:`src.main.run_ingestion`
and enforces a *cluster-wide* cap on the number of ingestion runs in flight via
a Redis-backed semaphore. The cap is configured by ``MAX_CONCURRENT_INGESTIONS``
and is independent of ``CELERY_WORKER_CONCURRENCY`` (which sets per-worker
process count). Default is 1 — i.e. only one ingestion runs at a time across
the entire cluster, regardless of how many workers are deployed.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import redis
from celery import Task
from celery.exceptions import Reject

from src.birthday import BirthdayRunSummary, run_birthday_greetings
from src.celery_app import celery_app
from src.config import get_settings
from src.main import IngestionSummary, initialize_ingestion_graph, run_ingestion, setup_logging

logger = logging.getLogger(__name__)

_INGEST_SEMAPHORE_KEY = "profile_unifier:ingestion:active"
_SOURCE_LOCK_PREFIX = "profile_unifier:ingestion:source"
_INIT_LOCK_KEY = "profile_unifier:ingestion:init"
_LOCK_LEASE_SECONDS = 60 * 60 * 6  # match Celery hard time limit
_SOURCE_LOCK_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


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
    """Reserve a source-specific lock or raise if that source is already running."""
    client = _redis_client()
    lock_id = uuid.uuid4().hex
    lock_key = f"{_SOURCE_LOCK_PREFIX}:{source_key}"
    lock_acquired = client.set(lock_key, lock_id, nx=True, ex=_LOCK_LEASE_SECONDS)
    if not lock_acquired:
        raise _SourceAlreadyRunningError(source_key=source_key)

    logger.info("Acquired ingestion source lock for %s", source_key)
    try:
        yield lock_id
    finally:
        try:
            client.eval(_SOURCE_LOCK_RELEASE_SCRIPT, 1, lock_key, lock_id)
            logger.info("Released ingestion source lock for %s", source_key)
        except Exception:
            logger.exception("Failed to release ingestion source lock for %s", source_key)


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
        super().__init__(f"Ingestion source already running: {source_key}")
        self.source_key = source_key


@celery_app.task(
    name="src.tasks.run_ingestion_task",
    bind=True,
    autoretry_for=(_SlotUnavailableError,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=None,  # keep retrying — eventually a slot frees up
)
def run_ingestion_task(self: Task, source_key: str, mode: str = "batch") -> IngestionSummary:
    """Run a single ingestion under the cluster-wide concurrency cap."""
    settings = get_settings()
    setup_logging(settings.log_level)

    try:
        with _acquire_init_lock():
            initialize_ingestion_graph()
        with (
            _acquire_source_lock(source_key),
            _acquire_ingestion_slot(settings.max_concurrent_ingestions),
        ):
            return run_ingestion(source_key, mode, initialize_graph=False)
    except _SourceAlreadyRunningError as exc:
        logger.warning(
            "Ingestion source %s is already running; skipping duplicate",
            exc.source_key,
        )
        raise Reject(str(exc), requeue=False) from exc
    except _SlotUnavailableError as exc:
        logger.warning("Ingestion slot unavailable (%d/%d), retrying...", exc.live, exc.cap)
        raise
    except Exception as exc:
        logger.exception("Ingestion task failed for %s", source_key)
        # Don't retry on real errors — surface them to the caller.
        raise Reject(str(exc), requeue=False) from exc


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
