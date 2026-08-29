"""PostgreSQL-only checks for the immutable Deal Intelligence platform migration."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from os import getenv
from pathlib import Path
from queue import Queue
from threading import Barrier, Event, Thread
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from deal_intelligence.health import DatabaseReadiness
from deal_intelligence.migrations.cli import ALEMBIC_VERSION_TABLE, alembic_config
from deal_intelligence.platform.database import Database
from deal_intelligence.platform.schema import (
    PLATFORM_SCHEMA,
    fresh_schema_inventory,
)
from deal_intelligence.platform.store import SqlAlchemyPlatformStore
from deal_intelligence.platform.types import (
    CompareAndSet,
    CompareAndSetResult,
    Lease,
    RunDescriptor,
    TerminalAccounting,
    UnitDescriptor,
)
from deal_intelligence.settings import Settings
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

_DATABASE_URL = getenv("HYPERP_DEAL_INTELLIGENCE_TEST_DATABASE_URL")
_CONNECTION_READY_TIMEOUT_SECONDS = 15.0
_CONNECTION_RETRY_SECONDS = 0.25
_THREAD_TIMEOUT_SECONDS = 10.0
_CONTENTION_OBSERVATION_SECONDS = 0.25

if _DATABASE_URL is None:
    pytestmark = pytest.mark.skip(reason="HYPERP_DEAL_INTELLIGENCE_TEST_DATABASE_URL is not set")


@dataclass(frozen=True, slots=True)
class IntegrationTarget:
    url: URL
    engine: Engine
    config: Config


@dataclass(frozen=True, slots=True)
class _ThreadSuccess[T]:
    value: T


@dataclass(frozen=True, slots=True)
class _ThreadFailure:
    error: BaseException


type _ThreadOutcome[T] = _ThreadSuccess[T] | _ThreadFailure


def _test_url() -> URL:
    if _DATABASE_URL is None:
        pytest.skip("HYPERP_DEAL_INTELLIGENCE_TEST_DATABASE_URL is not set")
    url = make_url(_DATABASE_URL)
    database = (url.database or "").lower()
    if (
        database in {"", "deal_intelligence", "postgres", "template0", "template1"}
        or "test" not in database
    ):
        raise RuntimeError(
            "Integration database URL must explicitly name a non-default test database"
        )
    return url


def _config(url: URL) -> Config:
    return alembic_config(url)


@pytest.fixture
def target() -> IntegrationTarget:
    """Provide a reset, explicit test database without touching any other target."""
    url = _test_url()
    engine = create_engine(url)
    integration_target = IntegrationTarget(url, engine, _config(url))
    try:
        _wait_for_connection(engine)
        _reset_to_baseline(integration_target)
        yield integration_target
    finally:
        preserve_primary_error = sys.exc_info()[0] is not None
        try:
            _clean_target(integration_target)
        except (CommandError, SQLAlchemyError):
            if not preserve_primary_error:
                raise
        finally:
            engine.dispose()


def _reset_to_baseline(target: IntegrationTarget) -> None:
    """Reset only Deal Intelligence state in the validated disposable test database."""
    with target.engine.begin() as connection:
        connection.execute(text(f"DROP SCHEMA IF EXISTS {PLATFORM_SCHEMA} CASCADE"))
    _reset_alembic_bookkeeping(target)
    command.stamp(target.config, "base")
    command.upgrade(target.config, "di_0001_baseline")


def _clean_target(target: IntegrationTarget) -> None:
    """Remove only the owned schema and uniquely named Alembic bookkeeping table."""
    with target.engine.begin() as connection:
        connection.execute(text(f"DROP SCHEMA IF EXISTS {PLATFORM_SCHEMA} CASCADE"))
    _reset_alembic_bookkeeping(target)


def _reset_alembic_bookkeeping(target: IntegrationTarget) -> None:
    """Keep version cleanup scoped to this package's isolated public bookkeeping table."""
    with target.engine.begin() as connection:
        connection.execute(text(f"DROP TABLE IF EXISTS public.{ALEMBIC_VERSION_TABLE}"))


def _wait_for_connection(engine: Engine) -> None:
    """Wait briefly for CI's disposable PostgreSQL service without logging its URL."""
    deadline = monotonic() + _CONNECTION_READY_TIMEOUT_SECONDS
    while True:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return
        except OperationalError as error:
            if monotonic() >= deadline:
                raise RuntimeError(
                    "Timed out waiting for the disposable PostgreSQL test database"
                ) from error
            sleep(_CONNECTION_RETRY_SECONDS)


def test_empty_database_upgrades_to_the_exact_fresh_schema_inventory(
    target: IntegrationTarget,
) -> None:
    """All heads create precisely the package-owned fresh-schema inventory from empty."""
    script_location = target.config.get_main_option("script_location")
    assert script_location is not None
    assert Path(script_location).is_absolute()
    command.downgrade(target.config, "base")
    with target.engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT to_regnamespace(:schema)"), {"schema": PLATFORM_SCHEMA}
            ).scalar()
            is None
        )
    command.upgrade(target.config, "heads")
    with target.engine.connect() as connection:
        tables = connection.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = :schema"),
            {"schema": PLATFORM_SCHEMA},
        ).scalars()
        assert frozenset(str(value) for value in tables) == fresh_schema_inventory()


def test_platform_downgrade_preserves_baseline_bookkeeping_sentinel(
    target: IntegrationTarget,
) -> None:
    """Platform downgrade retains baseline-owned bookkeeping and its data."""
    sentinel_component = f"test_component_{uuid4().hex}"
    sentinel_revision = f"test_revision_{uuid4().hex}"
    with target.engine.connect() as connection:
        tables = connection.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = :schema"),
            {"schema": PLATFORM_SCHEMA},
        ).scalars()
        assert frozenset(str(value) for value in tables) == {"migration_bookkeeping"}
    with target.engine.begin() as connection:
        connection.execute(
            text(
                f"INSERT INTO {PLATFORM_SCHEMA}.migration_bookkeeping "
                "(component_name, revision) VALUES (:component, :revision)"
            ),
            {"component": sentinel_component, "revision": sentinel_revision},
        )
    command.upgrade(target.config, "heads")
    command.downgrade(target.config, "di_0001_baseline")
    with target.engine.connect() as connection:
        tables = connection.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = :schema"),
            {"schema": PLATFORM_SCHEMA},
        ).scalars()
        assert frozenset(str(value) for value in tables) == {"migration_bookkeeping"}
        assert (
            connection.execute(
                text(
                    f"SELECT 1 FROM {PLATFORM_SCHEMA}.migration_bookkeeping "
                    "WHERE component_name = :component AND revision = :revision"
                ),
                {"component": sentinel_component, "revision": sentinel_revision},
            ).scalar_one()
            == 1
        )


def test_platform_store_cas_fences_heartbeats_and_terminal_invariants(
    target: IntegrationTarget,
) -> None:
    """Exercise generic persistence contracts in an isolated migrated platform schema."""
    command.upgrade(target.config, "heads")
    store = SqlAlchemyPlatformStore()
    now = datetime.now(UTC)
    resource_key = f"test.resource.{uuid4().hex}"
    with Session(target.engine) as session:
        run = store.create_run(session, RunDescriptor("test.component", "test.run", None, None))
        non_initial = store.compare_and_set_checkpoint(
            session, run.id, "cursor", CompareAndSet(1, {"value": 0})
        )
        assert not non_initial.applied and non_initial.checkpoint is None
        first = store.compare_and_set_checkpoint(
            session, run.id, "cursor", CompareAndSet(0, {"value": 1})
        )
        assert first.applied and first.checkpoint is not None and first.checkpoint.version == 1
        second = store.compare_and_set_checkpoint(
            session, run.id, "cursor", CompareAndSet(1, {"value": 2})
        )
        assert second.applied and second.checkpoint is not None and second.checkpoint.version == 2
        stale = store.compare_and_set_checkpoint(
            session, run.id, "cursor", CompareAndSet(1, {"value": 3})
        )
        assert not stale.applied and stale.checkpoint is not None and stale.checkpoint.version == 2
        create_only = store.compare_and_set_checkpoint(
            session, run.id, "cursor", CompareAndSet(0, {"value": 4})
        )
        assert not create_only.applied and create_only.checkpoint is not None
        assert create_only.checkpoint.version == 2

        with pytest.raises(ValueError, match="strictly positive"):
            store.acquire_lease(session, resource_key, run.id, timedelta())
        first_lease = store.acquire_lease(session, resource_key, run.id, timedelta(seconds=1))
        assert first_lease is not None
        assert session.execute(
            text(
                f"SELECT expires_at - acquired_at FROM {PLATFORM_SCHEMA}.leases "
                "WHERE resource_key = :key"
            ),
            {"key": resource_key},
        ).scalar_one() == timedelta(seconds=1)
        session.execute(
            text(
                f"UPDATE {PLATFORM_SCHEMA}.leases "
                "SET expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE resource_key = :key"
            ),
            {"key": resource_key},
        )
        reacquired = store.acquire_lease(session, resource_key, run.id, timedelta(minutes=1))
        assert reacquired is not None
        assert reacquired.fence_token == first_lease.fence_token + 1
        assert (
            session.execute(
                text(f"SELECT count(*) FROM {PLATFORM_SCHEMA}.leases WHERE resource_key = :key"),
                {"key": resource_key},
            ).scalar_one()
            == 1
        )
        assert (
            session.execute(
                text(f"SELECT expires_at FROM {PLATFORM_SCHEMA}.leases WHERE resource_key = :key"),
                {"key": resource_key},
            ).scalar_one()
            == reacquired.expires_at
        )
        assert (
            store.renew_lease(
                session,
                resource_key,
                run.id,
                first_lease.fence_token,
                timedelta(minutes=1),
            )
            is None
        )
        assert (
            store.renew_lease(
                session,
                resource_key,
                run.id,
                reacquired.fence_token,
                timedelta(minutes=1),
            )
            is not None
        )
        with pytest.raises(ValueError, match="strictly positive"):
            store.renew_lease(
                session,
                resource_key,
                run.id,
                reacquired.fence_token,
                timedelta(),
            )

        heartbeat = store.record_readiness_heartbeat(session, "worker", {"writers_enabled": False})
        assert heartbeat.component == "worker"
        assert store.read_fresh_heartbeat(session, "worker", now - timedelta(seconds=1)) is not None
        store.add_unit(session, UnitDescriptor(run.id, "complete"))
        session.execute(
            text(
                f"UPDATE {PLATFORM_SCHEMA}.process_units "
                "SET status = 'succeeded', finished_at = now() WHERE run_id = :run_id"
            ),
            {"run_id": run.id},
        )
        session.execute(
            text(
                f"UPDATE {PLATFORM_SCHEMA}.process_runs SET status = 'succeeded', "
                "terminal_disposition = 'test.completed', finished_at = now() WHERE id = :run_id"
            ),
            {"run_id": run.id},
        )
        with pytest.raises(ValueError, match="terminal run"):
            store.add_unit(session, UnitDescriptor(run.id, "after-terminal"))
        store.record_terminal_accounting(
            session, TerminalAccounting(run.id, "test.completed", 1, 0, 0, 1, now)
        )
        with pytest.raises(ValueError, match="balance"):
            store.record_terminal_accounting(
                session, TerminalAccounting(run.id, "test.completed", 1, 0, 0, 2, now)
            )
        session.commit()

    with target.engine.begin() as connection:
        connection.execute(
            text(
                f"CREATE TABLE {PLATFORM_SCHEMA}.identity_component_state (id integer PRIMARY KEY)"
            )
        )

    database = Database(
        Settings(database_url=SecretStr(target.url.render_as_string(hide_password=False)))
    )
    report = DatabaseReadiness(database).readiness_report("worker")
    assert report.as_dict() == {
        "status": "ready",
        "component": "worker",
        "writers_enabled": False,
        "task_count": 0,
        "schedule_count": 0,
    }
    with target.engine.connect() as connection:
        readiness_record = connection.execute(
            text(
                f"SELECT is_ready, expected_revisions, observed_revisions, details "
                f"FROM {PLATFORM_SCHEMA}.schema_readiness WHERE component = :component"
            ),
            {"component": "worker"},
        ).one()
    assert readiness_record[0] is True
    assert readiness_record[1] == ["di_0002_shared_platform"]
    assert readiness_record[2] == ["di_0002_shared_platform"]
    assert readiness_record[3]["table_count"] == len(fresh_schema_inventory()) + 1


def test_multi_session_checkpoint_and_lease_contention(target: IntegrationTarget) -> None:
    """Only one concurrent creator or expired-lease claimant can win each PostgreSQL race."""
    command.upgrade(target.config, "heads")
    store = SqlAlchemyPlatformStore()
    with Session(target.engine) as session:
        first_run = store.create_run(
            session, RunDescriptor("test.component", "test.run", None, None)
        )
        second_run = store.create_run(
            session, RunDescriptor("test.component", "test.run", None, None)
        )
        session.commit()

    initial = _concurrent_checkpoint_changes(target.engine, first_run.id, 0)
    assert sorted(result.applied for result in initial) == [False, True]
    assert {result.checkpoint.version for result in initial if result.checkpoint is not None} == {1}

    advances = _concurrent_checkpoint_changes(target.engine, first_run.id, 1)
    assert sorted(result.applied for result in advances) == [False, True]
    assert {result.checkpoint.version for result in advances if result.checkpoint is not None} == {
        2
    }

    resource_key = f"test.resource.{uuid4().hex}"
    with Session(target.engine) as session:
        lease = store.acquire_lease(session, resource_key, first_run.id, timedelta(minutes=1))
        assert lease is not None
        session.commit()
    with target.engine.begin() as connection:
        connection.execute(
            text(
                f"UPDATE {PLATFORM_SCHEMA}.leases "
                "SET expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE resource_key = :key"
            ),
            {"key": resource_key},
        )

    claims = _concurrent_lease_claims(target.engine, resource_key, first_run.id, second_run.id)
    assert sum(claim is not None for claim in claims) == 1
    winner = next(claim for claim in claims if claim is not None)
    assert winner.fence_token == 2
    with target.engine.connect() as connection:
        assert (
            connection.execute(
                text(f"SELECT expires_at FROM {PLATFORM_SCHEMA}.leases WHERE resource_key = :key"),
                {"key": resource_key},
            ).scalar_one()
            == winner.expires_at
        )


def test_terminal_accounting_blocks_add_unit_until_terminal_commit(
    target: IntegrationTarget,
) -> None:
    """The shared run lock rejects a unit added after terminal accounting commits."""
    command.upgrade(target.config, "heads")
    store = SqlAlchemyPlatformStore()
    with Session(target.engine) as session:
        run = store.create_run(session, RunDescriptor("test.component", "test.run", None, None))
        store.add_unit(session, UnitDescriptor(run.id, "complete"))
        session.execute(
            text(
                f"UPDATE {PLATFORM_SCHEMA}.process_units "
                "SET status = 'succeeded', finished_at = CURRENT_TIMESTAMP WHERE run_id = :run_id"
            ),
            {"run_id": run.id},
        )
        session.commit()

    started = Event()
    completed = Event()
    results: Queue[_ThreadOutcome[None]] = Queue()
    with Session(target.engine) as terminal_session:
        terminal_session.execute(
            text(
                f"UPDATE {PLATFORM_SCHEMA}.process_runs SET status = 'succeeded', "
                "terminal_disposition = 'test.completed', finished_at = CURRENT_TIMESTAMP "
                "WHERE id = :run_id"
            ),
            {"run_id": run.id},
        )
        store.record_terminal_accounting(
            terminal_session,
            TerminalAccounting(run.id, "test.completed", 1, 0, 0, 1, datetime.now(UTC)),
        )

        def add_after_terminalization() -> None:
            try:
                started.set()
                with Session(target.engine) as competing_session:
                    store.add_unit(competing_session, UnitDescriptor(run.id, "after-terminal"))
                    competing_session.commit()
                results.put(_ThreadSuccess(None))
            except BaseException as error:
                results.put(_ThreadFailure(error))
            finally:
                completed.set()

        competing = Thread(target=add_after_terminalization, daemon=True)
        competing.start()
        assert started.wait(timeout=_THREAD_TIMEOUT_SECONDS)
        assert not completed.wait(timeout=_CONTENTION_OBSERVATION_SECONDS)
        terminal_session.commit()

    with pytest.raises(ValueError, match="terminal run"):
        _collect_thread_results((competing,), results)


def _concurrent_checkpoint_changes(
    engine: Engine, run_id: UUID, expected_version: int
) -> list[CompareAndSetResult]:
    barrier = Barrier(2, timeout=_THREAD_TIMEOUT_SECONDS)
    results: Queue[_ThreadOutcome[CompareAndSetResult]] = Queue()

    def change(payload_value: int) -> None:
        try:
            store = SqlAlchemyPlatformStore()
            with Session(engine) as session:
                barrier.wait()
                result = store.compare_and_set_checkpoint(
                    session,
                    run_id,
                    "cursor",
                    CompareAndSet(expected_version, {"value": payload_value}),
                )
                session.commit()
            results.put(_ThreadSuccess(result))
        except BaseException as error:
            results.put(_ThreadFailure(error))

    first = Thread(target=change, args=(1,), daemon=True)
    second = Thread(target=change, args=(2,), daemon=True)
    first.start()
    second.start()
    return _collect_thread_results((first, second), results)


def _concurrent_lease_claims(
    engine: Engine, resource_key: str, first_run_id: UUID, second_run_id: UUID
) -> list[Lease | None]:
    barrier = Barrier(2, timeout=_THREAD_TIMEOUT_SECONDS)
    results: Queue[_ThreadOutcome[Lease | None]] = Queue()

    def claim(owner_run_id: UUID) -> None:
        try:
            store = SqlAlchemyPlatformStore()
            with Session(engine) as session:
                barrier.wait()
                result = store.acquire_lease(
                    session, resource_key, owner_run_id, timedelta(minutes=1)
                )
                session.commit()
            results.put(_ThreadSuccess(result))
        except BaseException as error:
            results.put(_ThreadFailure(error))

    first = Thread(target=claim, args=(first_run_id,), daemon=True)
    second = Thread(target=claim, args=(second_run_id,), daemon=True)
    first.start()
    second.start()
    return _collect_thread_results((first, second), results)


def _collect_thread_results[T](
    threads: tuple[Thread, ...], results: Queue[_ThreadOutcome[T]]
) -> list[T]:
    for thread in threads:
        thread.join(timeout=_THREAD_TIMEOUT_SECONDS)
        assert not thread.is_alive(), "Concurrent PostgreSQL helper did not finish"
    outcomes = [results.get(timeout=_THREAD_TIMEOUT_SECONDS) for _ in threads]
    values: list[T] = []
    for outcome in outcomes:
        if isinstance(outcome, _ThreadFailure):
            raise outcome.error
        values.append(outcome.value)
    return values
