"""Typed SQLAlchemy implementation of the generic disabled-platform store."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from deal_intelligence.platform.schema import (
    checkpoints,
    leases,
    process_heartbeats,
    process_runs,
    process_units,
    schema_readiness,
    source_instances,
    terminal_accounting,
)
from deal_intelligence.platform.types import (
    Checkpoint,
    CompareAndSet,
    CompareAndSetResult,
    JsonValue,
    Lease,
    ProcessHeartbeat,
    RunDescriptor,
    RunRecord,
    RunStatus,
    SchemaReadiness,
    SourceInstanceRecord,
    SourceInstanceRegistration,
    TerminalAccounting,
    UnitDescriptor,
)


class SqlAlchemyPlatformStore:
    """Generic PostgreSQL persistence with no writer or domain behavior."""

    def register_source_instance(
        self, session: Session, registration: SourceInstanceRegistration
    ) -> SourceInstanceRecord:
        if registration.is_enabled:
            raise ValueError("Source registrations must remain disabled")
        identifier = uuid4()
        now = _utc_now()
        statement = (
            insert(source_instances)
            .values(
                id=identifier,
                source_system=registration.source_system,
                instance_key=registration.instance_key,
                display_name=registration.display_name,
                is_enabled=False,
                registered_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                constraint="source_instances_source_instance_key_unique",
                set_={"display_name": registration.display_name, "updated_at": now},
            )
            .returning(
                source_instances.c.id,
                source_instances.c.registered_at,
                source_instances.c.updated_at,
            )
        )
        row = session.execute(statement).one()
        return SourceInstanceRecord(
            id=row[0], registration=registration, registered_at=row[1], updated_at=row[2]
        )

    def create_run(self, session: Session, descriptor: RunDescriptor) -> RunRecord:
        identifier = uuid4()
        now = _utc_now()
        session.execute(
            process_runs.insert().values(
                id=identifier,
                component_name=descriptor.component_name,
                run_kind=descriptor.run_kind,
                source_instance_id=descriptor.source_instance_id,
                requested_by=descriptor.requested_by,
                status=RunStatus.PENDING.value,
                created_at=now,
            )
        )
        return RunRecord(identifier, descriptor, RunStatus.PENDING, now, None, None, None)

    def add_unit(self, session: Session, descriptor: UnitDescriptor) -> None:
        statement = (
            insert(process_units)
            .values(
                id=uuid4(),
                run_id=descriptor.run_id,
                unit_key=descriptor.unit_key,
                attempt=descriptor.attempt,
                status="pending",
            )
            .on_conflict_do_nothing(constraint="process_units_run_unit_key_unique")
        )
        session.execute(statement)

    def compare_and_set_checkpoint(
        self, session: Session, run_id: UUID, checkpoint_key: str, change: CompareAndSet
    ) -> CompareAndSetResult:
        """Create only at expected zero, or advance the exact existing checkpoint version."""
        if change.expected_version < 0:
            raise ValueError("Checkpoint expected_version must be nonnegative")
        if change.expected_version != 0:
            existing = session.execute(
                select(checkpoints.c.version).where(
                    and_(
                        checkpoints.c.run_id == run_id,
                        checkpoints.c.checkpoint_key == checkpoint_key,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                return CompareAndSetResult(False, None)
        now = _utc_now()
        statement = (
            insert(checkpoints)
            .values(
                run_id=run_id,
                checkpoint_key=checkpoint_key,
                version=0,
                payload=change.payload,
                updated_at=now,
            )
            .on_conflict_do_update(
                constraint="checkpoints_primary_key",
                set_={
                    "version": checkpoints.c.version + 1,
                    "payload": change.payload,
                    "updated_at": now,
                },
                where=checkpoints.c.version == change.expected_version,
            )
            .returning(checkpoints.c.version, checkpoints.c.payload, checkpoints.c.updated_at)
        )
        row = session.execute(statement).one_or_none()
        if row is not None:
            return CompareAndSetResult(
                True, Checkpoint(run_id, checkpoint_key, row[0], row[1], row[2])
            )
        current = session.execute(
            select(checkpoints.c.version, checkpoints.c.payload, checkpoints.c.updated_at).where(
                and_(checkpoints.c.run_id == run_id, checkpoints.c.checkpoint_key == checkpoint_key)
            )
        ).one_or_none()
        if current is None:
            return CompareAndSetResult(False, None)
        return CompareAndSetResult(
            False, Checkpoint(run_id, checkpoint_key, current[0], current[1], current[2])
        )

    def acquire_lease(
        self, session: Session, resource_key: str, owner_run_id: UUID, expires_at: datetime
    ) -> Lease | None:
        """Acquire an expired lease while retaining its row and monotonic fence token."""
        now = _utc_now()
        if expires_at <= now:
            raise ValueError("Lease expiry must be in the future")
        statement = (
            insert(leases)
            .values(
                resource_key=resource_key,
                owner_run_id=owner_run_id,
                fence_token=1,
                acquired_at=now,
                expires_at=expires_at,
            )
            .on_conflict_do_update(
                index_elements=(leases.c.resource_key,),
                set_={
                    "owner_run_id": owner_run_id,
                    "fence_token": leases.c.fence_token + 1,
                    "acquired_at": now,
                    "expires_at": expires_at,
                },
                where=leases.c.expires_at <= now,
            )
            .returning(leases.c.fence_token)
        )
        row = session.execute(statement).one_or_none()
        if row is None:
            return None
        return Lease(resource_key, owner_run_id, row[0], expires_at)

    def renew_lease(
        self,
        session: Session,
        resource_key: str,
        owner_run_id: UUID,
        fence_token: int,
        expires_at: datetime,
    ) -> Lease | None:
        now = _utc_now()
        if expires_at <= now:
            raise ValueError("Lease expiry must be in the future")
        statement = (
            leases.update()
            .where(
                and_(
                    leases.c.resource_key == resource_key,
                    leases.c.owner_run_id == owner_run_id,
                    leases.c.fence_token == fence_token,
                    leases.c.expires_at > now,
                )
            )
            .values(expires_at=expires_at)
            .returning(leases.c.fence_token)
        )
        row = session.execute(statement).one_or_none()
        if row is None:
            return None
        return Lease(resource_key, owner_run_id, row[0], expires_at)

    def record_readiness_heartbeat(
        self, session: Session, component: str, details: JsonValue | None = None
    ) -> ProcessHeartbeat:
        now = _utc_now()
        value: JsonValue = {} if details is None else details
        statement = (
            insert(process_heartbeats)
            .values(component=component, heartbeat_at=now, details=value)
            .on_conflict_do_update(
                index_elements=(process_heartbeats.c.component,),
                set_={"heartbeat_at": now, "details": value},
            )
        )
        session.execute(statement)
        return ProcessHeartbeat(component, now, value)

    def read_fresh_heartbeat(
        self, session: Session, component: str, not_before: datetime
    ) -> ProcessHeartbeat | None:
        row = session.execute(
            select(process_heartbeats.c.heartbeat_at, process_heartbeats.c.details).where(
                and_(
                    process_heartbeats.c.component == component,
                    process_heartbeats.c.heartbeat_at >= not_before,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        return ProcessHeartbeat(component, row[0], row[1])

    def record_schema_readiness(self, session: Session, readiness: SchemaReadiness) -> None:
        statement = (
            insert(schema_readiness)
            .values(
                component=readiness.component,
                is_ready=readiness.is_ready,
                expected_revisions=list(readiness.expected_revisions),
                observed_revisions=list(readiness.observed_revisions),
                checked_at=readiness.checked_at,
                details=readiness.details,
            )
            .on_conflict_do_update(
                index_elements=(schema_readiness.c.component,),
                set_={
                    "is_ready": readiness.is_ready,
                    "expected_revisions": list(readiness.expected_revisions),
                    "observed_revisions": list(readiness.observed_revisions),
                    "checked_at": readiness.checked_at,
                    "details": readiness.details,
                },
            )
        )
        session.execute(statement)

    def record_terminal_accounting(self, session: Session, accounting: TerminalAccounting) -> None:
        if (
            accounting.total_count
            != accounting.succeeded_count + accounting.failed_count + accounting.skipped_count
        ):
            raise ValueError("Terminal accounting totals must balance")
        run = session.execute(
            select(process_runs.c.status, process_runs.c.terminal_disposition).where(
                process_runs.c.id == accounting.run_id
            )
        ).one_or_none()
        if run is None or run[0] not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("Terminal accounting requires a terminal run")
        if run[1] != accounting.terminal_disposition:
            raise ValueError("Terminal accounting disposition must match the run")
        counts = session.execute(
            select(
                func.count().filter(process_units.c.status == "succeeded"),
                func.count().filter(process_units.c.status == "failed"),
                func.count().filter(process_units.c.status == "skipped"),
            ).where(process_units.c.run_id == accounting.run_id)
        ).one()
        if (counts[0], counts[1], counts[2]) != (
            accounting.succeeded_count,
            accounting.failed_count,
            accounting.skipped_count,
        ):
            raise ValueError("Terminal accounting must match terminal process units")
        session.execute(
            terminal_accounting.insert().values(
                run_id=accounting.run_id,
                terminal_disposition=accounting.terminal_disposition,
                succeeded_count=accounting.succeeded_count,
                failed_count=accounting.failed_count,
                skipped_count=accounting.skipped_count,
                total_count=accounting.total_count,
                recorded_at=accounting.recorded_at,
            )
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)
