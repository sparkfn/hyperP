"""Internal-only health application; it has no data or writer endpoints."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from deal_intelligence.health import ComponentName, ReadinessProbe, get_readiness_probe

HEARTBEAT_INTERVAL_SECONDS = 30.0


def create_app(readiness_probe: ReadinessProbe | None = None) -> FastAPI:
    probe = readiness_probe or get_readiness_probe()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        stop_event = asyncio.Event()
        _record_api_heartbeat(probe)
        task = asyncio.create_task(_api_heartbeat_loop(probe, stop_event))
        try:
            yield
        finally:
            stop_event.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.get("/health/live", include_in_schema=False)
    def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready", include_in_schema=False)
    def ready() -> JSONResponse:
        component: ComponentName = "api"
        try:
            # This request itself proves API liveness, so it need not wait for the loop.
            probe.record_heartbeat(component)
            report = probe.readiness_report(component)
        except (OSError, SQLAlchemyError):
            return JSONResponse(_not_ready_body(component), status_code=503)
        return JSONResponse(report.as_dict(), status_code=200 if report.status == "ready" else 503)

    return app


async def _api_heartbeat_loop(probe: ReadinessProbe, stop_event: asyncio.Event) -> None:
    """Refresh API readiness without needing an inbound health request."""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
        except TimeoutError:
            _record_api_heartbeat(probe)


def _record_api_heartbeat(probe: ReadinessProbe) -> None:
    try:
        probe.record_heartbeat("api")
    except (OSError, SQLAlchemyError):
        return


def _not_ready_body(component: ComponentName) -> dict[str, str | bool | int]:
    return {
        "status": "not_ready",
        "component": component,
        "writers_enabled": False,
        "task_count": 0,
        "schedule_count": 0,
    }


def main(argv: list[str] | None = None) -> None:
    """Serve the internal API with Compose-compatible typed host and port options."""
    parser = argparse.ArgumentParser(prog="deal-intelligence-api")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=_port, default=8080)
    arguments = parser.parse_args(argv)
    uvicorn.run(create_app(), host=arguments.host, port=arguments.port)


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port
