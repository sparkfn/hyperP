"""PostgreSQL-only checks for the immutable Deal Intelligence platform migration."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from os import getenv
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from deal_intelligence.health import DatabaseReadiness
from deal_intelligence.migrations.cli import ALEMBIC_VERSION_TABLE, alembic_config
from deal_intelligence.platform.database import Database
from deal_intelligence.platform.schema import PLATFORM_SCHEMA, schema_inventory
from deal_intelligence.platform.store import SqlAlchemyPlatformStore
from deal_intelligence.platform.types import (
    CompareAndSet,
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

if _DATABASE_URL is None:
    pytestmark = pytest.mark.skip(reason="HYPERP_DEAL_INTELLIGENCE_TEST_DATABASE_URL is not set")


@dataclass(frozen=True, slots=True)
class IntegrationTarget:
    url: URL
    engine: Engine
    config: Config


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


def test_fresh_baseline_to_head_migration_preserves_unique_test_owned_sentinel(
    target: IntegrationTarget,
) -> None:
    """A fresh platform state upgrades from baseline to head without moving public state."""
    sentinel = f"di_test_sentinel_{uuid4().hex}"
    script_location = target.config.get_main_option("script_location")
    assert script_location is not None
    assert Path(script_location).is_absolute()
    with target.engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT to_regnamespace(:schema)"), {"schema": PLATFORM_SCHEMA}
            ).scalar()
            is None
        )
        assert (
            connection.execute(text(f"SELECT version_num FROM {ALEMBIC_VERSION_TABLE}")).scalar()
            == "di_0001_baseline"
        )
    with target.engine.begin() as connection:
        connection.execute(text(f'CREATE TABLE public."{sentinel}" (id integer PRIMARY KEY)'))
    try:
        command.upgrade(target.config, "heads")
        with target.engine.connect() as connection:
            tables = connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = :schema"
                ),
                {"schema": PLATFORM_SCHEMA},
            ).scalars()
            assert frozenset(str(value) for value in tables) == schema_inventory()
            assert (
                connection.execute(text(f"SELECT to_regclass('public.\"{sentinel}\"')")).scalar()
                is not None
            )
    finally:
        with target.engine.begin() as connection:
            connection.execute(text(f'DROP TABLE IF EXISTS public."{sentinel}"'))


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
        assert first.applied and first.checkpoint is not None and first.checkpoint.version == 0
        second = store.compare_and_set_checkpoint(
            session, run.id, "cursor", CompareAndSet(0, {"value": 2})
        )
        assert second.applied and second.checkpoint is not None and second.checkpoint.version == 1
        stale = store.compare_and_set_checkpoint(
            session, run.id, "cursor", CompareAndSet(0, {"value": 3})
        )
        assert not stale.applied and stale.checkpoint is not None and stale.checkpoint.version == 1

        first_lease = store.acquire_lease(session, resource_key, run.id, now + timedelta(seconds=1))
        assert first_lease is not None
        session.flush()
        sleep(1.1)
        reacquired = store.acquire_lease(
            session, resource_key, run.id, datetime.now(UTC) + timedelta(minutes=1)
        )
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
            store.renew_lease(
                session,
                resource_key,
                run.id,
                first_lease.fence_token,
                datetime.now(UTC) + timedelta(minutes=1),
            )
            is None
        )
        assert (
            store.renew_lease(
                session,
                resource_key,
                run.id,
                reacquired.fence_token,
                datetime.now(UTC) + timedelta(minutes=1),
            )
            is not None
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
        store.record_terminal_accounting(
            session, TerminalAccounting(run.id, "test.completed", 1, 0, 0, 1, now)
        )
        with pytest.raises(ValueError, match="balance"):
            store.record_terminal_accounting(
                session, TerminalAccounting(run.id, "test.completed", 1, 0, 0, 2, now)
            )
        session.commit()

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
    assert readiness_record[3]["table_count"] == len(schema_inventory())
