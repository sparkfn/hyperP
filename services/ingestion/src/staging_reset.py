"""Staging graph and Redis reset tool (issue #441).

Clears all HyperP data nodes while preserving system configuration nodes, then
clears ingestion Redis state (watermarks, locks, semaphores, queue gates,
caches, orchestration markers). Refuses to run against production targets and
requires an explicit ``--confirm`` acknowledgement.

Invoke with ``python -m src.staging_reset --confirm``.

Precondition: all ingestion workers must be stopped before running this tool;
it does not stop workers or check for in-flight tasks.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
from datetime import UTC, datetime
from urllib.parse import urlsplit

import redis
from neo4j import ManagedTransaction, Record

from src.config import Settings, get_settings
from src.graph.client import Neo4jClient
from src.graph.queries.staging_reset import STAGING_RESET_CLEAR_GRAPH

logger = logging.getLogger(__name__)

_BLOCKED_ENV_VALUES: frozenset[str] = frozenset({"production", "prod"})
_PRODUCTION_HOST_MARKERS: tuple[str, ...] = ("prod", "production")
_REDIS_SCAN_PATTERNS: tuple[str, ...] = ("profile_unifier:*", "ci1:*")
_CELERY_QUEUE_KEYS: tuple[str, ...] = ("ingestion", "lifecycle", "miscellaneous")
_REDIS_SCAN_COUNT = 1000
_BATCH_SIZE = 10_000
_PROG = "python -m src.staging_reset"
_DESCRIPTION = (
    "Clear all HyperP graph data nodes and ingestion Redis state, preserving "
    "system configuration nodes. Stop all ingestion workers before running."
)


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 timestamp."""
    return datetime.now(UTC).isoformat()


def _host_looks_production(hostname: str) -> bool:
    """Case-insensitive substring check for production indicators in a hostname."""
    lower = hostname.lower()
    return any(marker in lower for marker in _PRODUCTION_HOST_MARKERS)


def _check_environment_guard(settings: Settings) -> str | None:
    """Return an error message if any signal suggests production, else None."""
    for env_value in (
        settings.deployment_environment,
        os.getenv("DEPLOYMENT_ENVIRONMENT", ""),
        os.getenv("ENVIRONMENT", ""),
    ):
        if env_value.lower() in _BLOCKED_ENV_VALUES:
            return f"Refusing to reset: environment value {env_value!r} indicates production."

    for hostname in (socket.gethostname(), os.getenv("HOSTNAME", "")):
        if _host_looks_production(hostname):
            return f"Refusing to reset: hostname {hostname!r} contains a production indicator."

    for label, uri in (
        ("neo4j_uri", settings.neo4j_uri),
        ("celery_broker_url", settings.celery_broker_url),
    ):
        parsed_host = urlsplit(uri).hostname or ""
        if _host_looks_production(parsed_host):
            return (
                f"Refusing to reset: {label} host {parsed_host!r} "
                "contains a production indicator."
            )

    return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=_PROG, description=_DESCRIPTION)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required acknowledgement that this target is not production.",
    )
    return parser


def _batch_count(record: Record | None) -> int:
    """Coerce the deleted-node count from a batch result, rejecting invalid values."""
    if record is None:
        return 0
    value = record.get("deleted")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError("staging reset batch returned an invalid deleted count")
    return value


def _delete_batch(tx: ManagedTransaction, batch_size: int) -> int:
    """Single-batch graph deletion; called per transaction."""
    return _batch_count(tx.run(STAGING_RESET_CLEAR_GRAPH, batch_size=batch_size).single())


def _clear_graph(client: Neo4jClient, batch_size: int = _BATCH_SIZE) -> int:
    """Delete every non-preserved node in batches until none remain."""
    total = 0
    while True:
        deleted = client.execute_write(lambda tx, _bs=batch_size: _delete_batch(tx, _bs))
        total += deleted
        logger.info("Graph batch: deleted %d nodes (total so far: %d)", deleted, total)
        if deleted == 0:
            break
    return total


def _clear_redis(broker_url: str) -> int:
    """Delete all ingestion Redis keys and purge the Celery queue keys."""
    client = redis.Redis.from_url(broker_url)
    try:
        count = 0
        for pattern in _REDIS_SCAN_PATTERNS:
            pattern_count = 0
            cursor = 0
            while True:
                raw_cursor, keys = client.scan(cursor, match=pattern, count=_REDIS_SCAN_COUNT)
                cursor = int(raw_cursor)
                if keys:
                    pattern_count += int(client.delete(*keys))
                if cursor == 0:
                    break
            count += pattern_count
            logger.info("Redis scan %s: deleted %d keys", pattern, pattern_count)
        queue_count = int(client.delete(*_CELERY_QUEUE_KEYS))
        count += queue_count
        logger.info("Redis Celery queues purged: deleted %d keys", queue_count)
        return count
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = get_settings()

    guard_error = _check_environment_guard(settings)
    if guard_error is not None:
        logger.error(guard_error)
        return 1
    if not args.confirm:
        logger.error("Pass --confirm to proceed.")
        return 1

    logger.info(
        "[%s] Starting staging reset (neo4j=%s, redis=%s)",
        _utc_now_iso(),
        urlsplit(settings.neo4j_uri).hostname,
        urlsplit(settings.celery_broker_url).hostname,
    )

    graph_client = Neo4jClient(settings)
    try:
        nodes_deleted = _clear_graph(graph_client)
    finally:
        graph_client.close()

    redis_keys_deleted = _clear_redis(settings.celery_broker_url)

    logger.info(
        "[%s] Staging reset complete: %d graph nodes deleted, %d Redis keys deleted.",
        _utc_now_iso(),
        nodes_deleted,
        redis_keys_deleted,
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
