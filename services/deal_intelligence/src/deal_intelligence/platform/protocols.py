"""Narrow protocols for future components to join a shared transaction."""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from deal_intelligence.platform.types import (
    CompareAndSet,
    CompareAndSetResult,
    JsonValue,
    Lease,
    ProcessHeartbeat,
    RunDescriptor,
    RunRecord,
    SchemaReadiness,
    SourceInstanceRecord,
    SourceInstanceRegistration,
    TerminalAccounting,
    UnitDescriptor,
)


class TransactionBoundary(Protocol):
    def transaction(self) -> AbstractContextManager[Session]:
        """Yield one session that commits on success and rolls back on failure."""


class PlatformStore(Protocol):
    def register_source_instance(
        self, session: Session, registration: SourceInstanceRegistration
    ) -> SourceInstanceRecord: ...
    def create_run(self, session: Session, descriptor: RunDescriptor) -> RunRecord: ...
    def add_unit(self, session: Session, descriptor: UnitDescriptor) -> None: ...
    def compare_and_set_checkpoint(
        self, session: Session, run_id: UUID, checkpoint_key: str, change: CompareAndSet
    ) -> CompareAndSetResult: ...
    def acquire_lease(
        self, session: Session, resource_key: str, owner_run_id: UUID, duration: timedelta
    ) -> Lease | None: ...
    def renew_lease(
        self,
        session: Session,
        resource_key: str,
        owner_run_id: UUID,
        fence_token: int,
        duration: timedelta,
    ) -> Lease | None: ...
    def record_readiness_heartbeat(
        self, session: Session, component: str, details: JsonValue | None = None
    ) -> ProcessHeartbeat: ...
    def read_fresh_heartbeat(
        self, session: Session, component: str, not_before: datetime
    ) -> ProcessHeartbeat | None: ...
    def record_schema_readiness(self, session: Session, readiness: SchemaReadiness) -> None: ...
    def record_terminal_accounting(
        self, session: Session, accounting: TerminalAccounting
    ) -> None: ...


class TransactionalParticipant(Protocol):
    def prepare(self, session: Session) -> None: ...
    def commit(self, session: Session) -> None: ...
    def rollback(self, session: Session) -> None: ...
