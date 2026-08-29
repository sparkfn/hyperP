"""Focused structured readiness tests using database-free fakes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from deal_intelligence.api import create_app
from deal_intelligence.health import ComponentName, DatabaseReadiness, ReadinessProbe
from deal_intelligence.migrations.revisions import expected_heads
from deal_intelligence.platform.schema import schema_inventory
from deal_intelligence.platform.types import ReadinessReport
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute


@dataclass(slots=True)
class FakeReadiness(ReadinessProbe):
    ready: bool
    heartbeats: list[ComponentName] = field(default_factory=list)

    def record_heartbeat(self, component: ComponentName) -> None:
        self.heartbeats.append(component)

    def readiness_report(self, component: ComponentName) -> ReadinessReport:
        return ReadinessReport("ready" if self.ready else "not_ready", component)


@dataclass(frozen=True, slots=True)
class FakeDatabase:
    connected: bool
    revisions: frozenset[str] | None
    tables: frozenset[str] | None

    def can_connect(self) -> bool:
        return self.connected

    def database_revisions(self) -> frozenset[str] | None:
        return self.revisions

    def platform_table_names(self) -> frozenset[str] | None:
        return self.tables


def _endpoint(app: FastAPI, path: str) -> Callable[[], object]:
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path == path:
            return route.endpoint
    raise AssertionError(f"Missing route: {path}")


def test_live_is_available_without_readiness() -> None:
    assert _endpoint(create_app(FakeReadiness(False)), "/health/live")() == {"status": "live"}


def test_ready_returns_disabled_contract_and_api_heartbeat() -> None:
    fake = FakeReadiness(True)
    response = _endpoint(create_app(fake), "/health/ready")()
    assert isinstance(response, JSONResponse)
    assert response.status_code == 200
    assert response.body == (
        b'{"status":"ready","component":"api","writers_enabled":false,'
        b'"task_count":0,"schedule_count":0}'
    )
    assert fake.heartbeats == ["api"]


def test_not_ready_returns_disabled_contract() -> None:
    response = _endpoint(create_app(FakeReadiness(False)), "/health/ready")()
    assert isinstance(response, JSONResponse)
    assert response.status_code == 503
    assert b'"status":"not_ready"' in response.body
    assert b'"writers_enabled":false' in response.body


def test_database_readiness_rejects_unavailable_mismatched_or_stale_database() -> None:
    heads = expected_heads()
    assert not DatabaseReadiness(FakeDatabase(False, heads, schema_inventory())).is_ready()
    assert not DatabaseReadiness(FakeDatabase(True, None, schema_inventory())).is_ready()
    assert not DatabaseReadiness(FakeDatabase(True, heads, frozenset())).is_ready()


def test_api_lifespan_records_an_initial_heartbeat_without_traffic() -> None:
    fake = FakeReadiness(True)
    app = create_app(fake)

    async def exercise_lifespan() -> None:
        async with app.router.lifespan_context(app):
            assert fake.heartbeats == ["api"]

    asyncio.run(exercise_lifespan())
