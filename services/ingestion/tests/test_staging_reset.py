"""Unit tests for the staging reset CLI tool (issue #441)."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest
from neo4j import ManagedTransaction

from src.config import Settings
from src.graph.queries.staging_reset import STAGING_RESET_CLEAR_GRAPH
from src.staging_reset import (
    _BATCH_SIZE,
    _CELERY_QUEUE_KEYS,
    _REDIS_SCAN_PATTERNS,
    _batch_count,
    _check_environment_guard,
    _clear_graph,
    _clear_redis,
    _host_looks_production,
    main,
)

_SAFE_HOSTNAME = "ingestion-worker-staging-01"
_SAFE_NEO4J_URI = "bolt://neo4j-staging:7687"
_SAFE_BROKER_URL = "redis://redis-staging:6379/0"
_ISO_TIMESTAMP = re.compile(r"\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^\]]*\]")


def _settings(
    *,
    deployment_environment: str = "staging",
    neo4j_uri: str = _SAFE_NEO4J_URI,
    celery_broker_url: str = _SAFE_BROKER_URL,
) -> Settings:
    return cast(
        Settings,
        SimpleNamespace(
            deployment_environment=deployment_environment,
            neo4j_uri=neo4j_uri,
            celery_broker_url=celery_broker_url,
        ),
    )


@contextmanager
def _guard_environment(
    *,
    hostname: str = _SAFE_HOSTNAME,
    env: Mapping[str, str] | None = None,
) -> Iterator[None]:
    """Pin the process hostname and environment variables seen by the guard."""
    overrides = dict(env or {})

    def _getenv(key: str, default: str = "") -> str:
        return overrides.get(key, default)

    with (
        patch("src.staging_reset.socket.gethostname", return_value=hostname),
        patch("src.staging_reset.os.getenv", side_effect=_getenv),
    ):
        yield


class _FakeResult:
    def __init__(self, deleted: object) -> None:
        self._deleted = deleted

    def single(self) -> object:
        if self._deleted is None:
            return None
        return {"deleted": self._deleted}


class _FakeTx:
    """Minimal managed-transaction double capturing the executed query."""

    def __init__(self, deleted: object) -> None:
        self._deleted = deleted
        self.query = ""
        self.params: dict[str, object] = {}

    def run(self, query: str, **params: object) -> _FakeResult:
        self.query = query
        self.params = params
        return _FakeResult(self._deleted)


class _FakeGraphClient:
    """Neo4jClient double returning canned per-batch deletion counts."""

    def __init__(self, batches: list[object]) -> None:
        self._batches = list(batches)
        self.calls = 0
        self.queries: list[str] = []
        self.params: list[dict[str, object]] = []
        self.closed = False

    def execute_write(self, work: Callable[[ManagedTransaction], int]) -> int:
        index = self.calls
        self.calls += 1
        deleted = self._batches[index] if index < len(self._batches) else 0
        tx = _FakeTx(deleted)
        result = work(cast(ManagedTransaction, tx))
        self.queries.append(tx.query)
        self.params.append(tx.params)
        return result

    def close(self) -> None:
        self.closed = True


class _FakeRedis:
    """Redis double recording scan/delete/close interactions."""

    def __init__(
        self,
        pages: Mapping[str, list[tuple[int, list[str]]]] | None = None,
        *,
        fail_scan: bool = False,
    ) -> None:
        self.pages = dict(pages or {})
        self.positions: dict[str, int] = {}
        self.scan_calls: list[tuple[str, int]] = []
        self.deleted: list[tuple[str, ...]] = []
        self.close_calls = 0
        self.fail_scan = fail_scan

    def scan(self, cursor: int, match: str, count: int) -> tuple[int, list[str]]:
        if self.fail_scan:
            raise RuntimeError("redis unavailable")
        self.scan_calls.append((match, cursor))
        position = self.positions.get(match, 0)
        self.positions[match] = position + 1
        sequence = self.pages.get(match, [(0, [])])
        if position >= len(sequence):
            return 0, []
        return sequence[position]

    def delete(self, *keys: str) -> int:
        self.deleted.append(tuple(keys))
        return len(keys)

    def close(self) -> None:
        self.close_calls += 1


# --- Environment guard: environment variables ---------------------------------


def test_guard_refuses_production_env() -> None:
    with _guard_environment():
        error = _check_environment_guard(_settings(deployment_environment="production"))
    assert error is not None
    assert "production" in error


def test_guard_refuses_prod_env() -> None:
    with _guard_environment(env={"ENVIRONMENT": "prod"}):
        error = _check_environment_guard(_settings())
    assert error is not None
    assert "prod" in error


def test_guard_refuses_deployment_env_override() -> None:
    with _guard_environment(env={"DEPLOYMENT_ENVIRONMENT": "production"}):
        error = _check_environment_guard(_settings())
    assert error is not None
    assert "production" in error


@pytest.mark.parametrize("value", ["Production", "PRODUCTION", "Prod"])
def test_guard_env_check_is_case_insensitive(value: str) -> None:
    with _guard_environment(env={"ENVIRONMENT": value}):
        error = _check_environment_guard(_settings())
    assert error is not None


def test_guard_allows_staging() -> None:
    with _guard_environment():
        assert _check_environment_guard(_settings()) is None


def test_guard_allows_development() -> None:
    with _guard_environment():
        assert _check_environment_guard(_settings(deployment_environment="development")) is None


# --- Environment guard: hostnames --------------------------------------------


def test_guard_refuses_production_system_hostname() -> None:
    with _guard_environment(hostname="app-production-01"):
        error = _check_environment_guard(_settings())
    assert error is not None
    assert "app-production-01" in error


def test_guard_refuses_hostname_env() -> None:
    with _guard_environment(env={"HOSTNAME": "prod-worker-3"}):
        error = _check_environment_guard(_settings())
    assert error is not None
    assert "prod-worker-3" in error


def test_guard_refuses_neo4j_production_host() -> None:
    with _guard_environment():
        error = _check_environment_guard(
            _settings(neo4j_uri="bolt://neo4j.prod.internal:7687")
        )
    assert error is not None
    assert "neo4j_uri" in error


def test_guard_refuses_redis_production_host() -> None:
    with _guard_environment():
        error = _check_environment_guard(
            _settings(celery_broker_url="redis://redis.production.internal:6379/0")
        )
    assert error is not None
    assert "celery_broker_url" in error


@pytest.mark.parametrize(
    "hostname",
    ["prod-app-01", "PROD-APP-01", "AppProduction01", "neo4j.production.internal"],
)
def test_host_looks_production_detects_indicators(hostname: str) -> None:
    assert _host_looks_production(hostname) is True


@pytest.mark.parametrize(
    "hostname",
    ["staging-01", "development", "neo4j-staging", "", "redis"],
)
def test_host_looks_production_allows_safe_hosts(hostname: str) -> None:
    assert _host_looks_production(hostname) is False


# --- CLI parsing --------------------------------------------------------------


def test_confirm_required() -> None:
    with (
        _guard_environment(),
        patch("src.staging_reset.get_settings", return_value=_settings()),
        patch("src.staging_reset.Neo4jClient") as client_cls,
        patch("src.staging_reset.redis.Redis.from_url") as from_url,
    ):
        assert main([]) == 1
    client_cls.assert_not_called()
    from_url.assert_not_called()


def test_main_production_exits() -> None:
    with (
        _guard_environment(),
        patch(
            "src.staging_reset.get_settings",
            return_value=_settings(deployment_environment="production"),
        ),
        patch("src.staging_reset.Neo4jClient") as client_cls,
        patch("src.staging_reset.redis.Redis.from_url") as from_url,
    ):
        assert main(["--confirm"]) == 1
    client_cls.assert_not_called()
    from_url.assert_not_called()


def test_main_guard_precedes_confirm_check(caplog: pytest.LogCaptureFixture) -> None:
    with (
        _guard_environment(hostname="prod-app-01"),
        patch("src.staging_reset.get_settings", return_value=_settings()),
        patch("src.staging_reset.Neo4jClient"),
        patch("src.staging_reset.redis.Redis.from_url"),
        caplog.at_level(logging.ERROR, logger="src.staging_reset"),
    ):
        assert main([]) == 1
    messages = [record.getMessage() for record in caplog.records]
    assert any("Refusing to reset" in message for message in messages)
    assert not any("--confirm" in message for message in messages)


# --- Graph clearing -----------------------------------------------------------


def test_graph_clear_query_excludes_preserved() -> None:
    assert STAGING_RESET_CLEAR_GRAPH == (
        "MATCH (n) "
        "WHERE NOT n:Entity AND NOT n:SourceSystem "
        "AND NOT n:OAuthClient AND NOT n:OAuthClientSecret "
        "WITH n LIMIT $batch_size "
        "DETACH DELETE n "
        "RETURN count(*) AS deleted"
    )
    for label in ("Entity", "SourceSystem", "OAuthClient", "OAuthClientSecret"):
        assert f"NOT n:{label}" in STAGING_RESET_CLEAR_GRAPH
    assert STAGING_RESET_CLEAR_GRAPH.count("$") == 1


def test_graph_clear_batched_loop() -> None:
    client = _FakeGraphClient([10_000, 5_000, 0])
    assert _clear_graph(client) == 15_000
    assert client.calls == 3
    assert client.queries == [STAGING_RESET_CLEAR_GRAPH] * 3


def test_graph_clear_uses_default_batch_size() -> None:
    client = _FakeGraphClient([0])
    _clear_graph(client)
    assert client.params == [{"batch_size": _BATCH_SIZE}]


def test_graph_clear_honors_custom_batch_size() -> None:
    client = _FakeGraphClient([7, 0])
    assert _clear_graph(client, batch_size=7) == 7
    assert client.params == [{"batch_size": 7}, {"batch_size": 7}]


def test_graph_clear_empty_is_idempotent() -> None:
    client = _FakeGraphClient([0])
    assert _clear_graph(client) == 0
    assert client.calls == 1


def test_graph_clear_rejects_invalid_batch_count() -> None:
    client = _FakeGraphClient(["not-a-count"])
    with pytest.raises(RuntimeError, match="invalid deleted count"):
        _clear_graph(client)


@pytest.mark.parametrize("deleted", [-1, True, "5", None])
def test_batch_count_rejects_invalid_values(deleted: object) -> None:
    with pytest.raises(RuntimeError, match="invalid deleted count"):
        _batch_count({"deleted": deleted})


def test_batch_count_reads_absent_record_as_zero() -> None:
    assert _batch_count(None) == 0


# --- Redis clearing -----------------------------------------------------------


def test_redis_scans_both_patterns() -> None:
    fake = _FakeRedis()
    with patch("src.staging_reset.redis.Redis.from_url", return_value=fake):
        _clear_redis(_SAFE_BROKER_URL)
    assert [match for match, _ in fake.scan_calls] == list(_REDIS_SCAN_PATTERNS)


def test_redis_deletes_celery_queues() -> None:
    fake = _FakeRedis()
    with patch("src.staging_reset.redis.Redis.from_url", return_value=fake):
        _clear_redis(_SAFE_BROKER_URL)
    assert _CELERY_QUEUE_KEYS in fake.deleted


def test_redis_paginates_scan_cursor_and_counts_keys() -> None:
    fake = _FakeRedis(
        pages={
            "profile_unifier:*": [(17, ["a", "b"]), (0, ["c"])],
            "ci1:*": [(0, ["ci-1", "ci-2"])],
        }
    )
    with patch("src.staging_reset.redis.Redis.from_url", return_value=fake):
        removed = _clear_redis(_SAFE_BROKER_URL)
    assert removed == 5 + len(_CELERY_QUEUE_KEYS)
    assert fake.deleted[:3] == [("a", "b"), ("c",), ("ci-1", "ci-2")]
    assert fake.scan_calls == [
        ("profile_unifier:*", 0),
        ("profile_unifier:*", 17),
        ("ci1:*", 0),
    ]


def test_redis_logs_per_pattern(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = _FakeRedis(pages={"profile_unifier:*": [(0, ["a"])]})
    with (
        patch("src.staging_reset.redis.Redis.from_url", return_value=fake),
        caplog.at_level(logging.INFO, logger="src.staging_reset"),
    ):
        _clear_redis(_SAFE_BROKER_URL)
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "profile_unifier:*" in message and "deleted 1 keys" in message for message in messages
    )
    assert any("Celery queues purged" in message for message in messages)


def test_redis_client_closed_even_when_scan_fails() -> None:
    fake = _FakeRedis(fail_scan=True)
    with patch("src.staging_reset.redis.Redis.from_url", return_value=fake):
        with pytest.raises(RuntimeError, match="redis unavailable"):
            _clear_redis(_SAFE_BROKER_URL)
    assert fake.close_calls == 1


# --- Resource cleanup ---------------------------------------------------------


def test_graph_client_closed_on_success() -> None:
    client = _FakeGraphClient([0])
    with (
        _guard_environment(),
        patch("src.staging_reset.get_settings", return_value=_settings()),
        patch("src.staging_reset.Neo4jClient", return_value=client),
        patch("src.staging_reset.redis.Redis.from_url", return_value=_FakeRedis()),
    ):
        assert main(["--confirm"]) == 0
    assert client.closed is True


def test_graph_client_closed_on_error() -> None:
    client = _FakeGraphClient(["bad"])
    with (
        _guard_environment(),
        patch("src.staging_reset.get_settings", return_value=_settings()),
        patch("src.staging_reset.Neo4jClient", return_value=client),
        patch("src.staging_reset.redis.Redis.from_url") as from_url,
    ):
        with pytest.raises(RuntimeError, match="invalid deleted count"):
            main(["--confirm"])
    assert client.closed is True
    from_url.assert_not_called()


# --- Logging ------------------------------------------------------------------


def test_main_success_logs_timestamps(caplog: pytest.LogCaptureFixture) -> None:
    settings = _settings()
    client = _FakeGraphClient([12, 0])
    fake_redis = _FakeRedis()
    with (
        _guard_environment(),
        patch("src.staging_reset.get_settings", return_value=settings),
        patch("src.staging_reset.Neo4jClient", return_value=client) as client_cls,
        patch("src.staging_reset.redis.Redis.from_url", return_value=fake_redis),
        caplog.at_level(logging.INFO, logger="src.staging_reset"),
    ):
        assert main(["--confirm"]) == 0
    client_cls.assert_called_once_with(settings)
    assert fake_redis.close_calls == 1
    messages = [record.getMessage() for record in caplog.records]
    start = [message for message in messages if "Starting staging reset" in message]
    complete = [message for message in messages if "Staging reset complete" in message]
    assert len(start) == 1 and _ISO_TIMESTAMP.search(start[0])
    assert "neo4j-staging" in start[0] and "redis-staging" in start[0]
    assert len(complete) == 1 and _ISO_TIMESTAMP.search(complete[0])
    assert "12 graph nodes deleted" in complete[0]
    assert f"{len(_CELERY_QUEUE_KEYS)} Redis keys deleted" in complete[0]
