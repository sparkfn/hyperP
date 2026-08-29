"""Disabled scheduler entry point; no schedules are registered or executed."""

from __future__ import annotations

import logging
import signal
from dataclasses import dataclass
from threading import Event
from typing import Protocol

from sqlalchemy.exc import SQLAlchemyError

from deal_intelligence.health import ComponentName
from deal_intelligence.platform.database import Database, get_database
from deal_intelligence.platform.store import SqlAlchemyPlatformStore

logger = logging.getLogger(__name__)
HEARTBEAT_INTERVAL_SECONDS = 30.0


class ProcessHeartbeatWriter(Protocol):
    """Minimal test seam for recording a named heartbeat."""

    def write(self, component: ComponentName) -> None:
        """Persist one component heartbeat."""


class HeartbeatWriter:
    """Write one named disabled-process heartbeat."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def write(self, component: ComponentName) -> None:
        with self._database.transaction() as session:
            SqlAlchemyPlatformStore().record_readiness_heartbeat(
                session, component, {"writers_enabled": False, "task_count": 0, "schedule_count": 0}
            )


@dataclass(frozen=True, slots=True)
class IdleScheduler:
    enabled: bool = False
    schedule_count: int = 0


def run() -> IdleScheduler:
    return IdleScheduler()


def run_one_cycle(writer: ProcessHeartbeatWriter | None = None) -> IdleScheduler:
    """Write exactly one scheduler readiness heartbeat and execute no schedule."""
    active_writer = writer or HeartbeatWriter(get_database())
    active_writer.write("scheduler")
    return run()


def run_idle_loop(stop_event: Event, writer: ProcessHeartbeatWriter | None = None) -> None:
    """Stay disabled, heartbeat periodically, and return promptly after a signal."""
    active_writer = writer or HeartbeatWriter(get_database())
    while not stop_event.is_set():
        try:
            run_one_cycle(active_writer)
        except (OSError, SQLAlchemyError):
            logger.exception("Deal Intelligence scheduler heartbeat failed")
        stop_event.wait(HEARTBEAT_INTERVAL_SECONDS)


def main() -> None:
    stop_event = Event()
    _install_stop_handlers(stop_event)
    run_idle_loop(stop_event)


def _install_stop_handlers(stop_event: Event) -> None:
    def stop(_signal_number: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
