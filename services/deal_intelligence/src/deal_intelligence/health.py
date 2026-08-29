"""Structured, secret-safe readiness checks for the disabled platform."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from sqlalchemy.exc import SQLAlchemyError

from deal_intelligence.migrations.revisions import expected_heads
from deal_intelligence.platform.database import Database, get_database
from deal_intelligence.platform.schema import schema_inventory
from deal_intelligence.platform.store import SqlAlchemyPlatformStore
from deal_intelligence.platform.types import JsonValue, ReadinessReport, SchemaReadiness

ComponentName = Literal["api", "worker", "scheduler"]
HEARTBEAT_MAX_AGE = timedelta(minutes=2)


class ReadinessProbe(Protocol):
    def readiness_report(self, component: ComponentName) -> ReadinessReport:
        """Inspect schema and the component's named fresh heartbeat."""

    def record_heartbeat(self, component: ComponentName) -> None:
        """Record a disabled process heartbeat without running work."""


@dataclass(frozen=True, slots=True)
class DatabaseReadiness:
    database: Database
    heartbeat_max_age: timedelta = HEARTBEAT_MAX_AGE

    def is_ready(self, component: ComponentName = "api") -> bool:
        return self.readiness_report(component).status == "ready"

    def record_heartbeat(self, component: ComponentName) -> None:
        store = SqlAlchemyPlatformStore()
        with self.database.transaction() as session:
            store.record_readiness_heartbeat(session, component, _disabled_details())

    def readiness_report(self, component: ComponentName) -> ReadinessReport:
        if not self.database.can_connect():
            return ReadinessReport(status="not_ready", component=component)
        observed = self.database.database_revisions()
        tables = self.database.platform_table_names()
        expected = expected_heads()
        if observed is None or tables is None:
            return ReadinessReport(status="not_ready", component=component)
        schema_ok = observed == expected and schema_inventory().issubset(tables)
        heartbeat_ok = False
        if schema_ok:
            try:
                store = SqlAlchemyPlatformStore()
                with self.database.transaction() as session:
                    heartbeat_ok = (
                        store.read_fresh_heartbeat(
                            session, component, _utc_now() - self.heartbeat_max_age
                        )
                        is not None
                    )
                    store.record_schema_readiness(
                        session,
                        SchemaReadiness(
                            component=component,
                            is_ready=heartbeat_ok,
                            expected_revisions=tuple(sorted(expected)),
                            observed_revisions=tuple(sorted(observed)),
                            checked_at=_utc_now(),
                            details=_schema_details(tables),
                        ),
                    )
            except SQLAlchemyError:
                heartbeat_ok = False
        status = "ready" if schema_ok and heartbeat_ok else "not_ready"
        return ReadinessReport(status=status, component=component)


def get_readiness_probe() -> ReadinessProbe:
    return DatabaseReadiness(get_database())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="deal-intelligence-health")
    parser.add_argument("--component", required=True, choices=("api", "worker", "scheduler"))
    arguments = parser.parse_args(argv)
    component: ComponentName = arguments.component
    report = get_readiness_probe().readiness_report(component)
    print(json.dumps(report.as_dict(), sort_keys=True))
    if report.status != "ready":
        raise SystemExit(1)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _disabled_details() -> JsonValue:
    return {"writers_enabled": False, "task_count": 0, "schedule_count": 0}


def _schema_details(tables: frozenset[str] | None) -> JsonValue:
    return {"table_count": 0 if tables is None else len(tables)}
