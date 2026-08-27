"""Production-safe #272 migration for instance-scoped ingestion controls."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from neo4j import ManagedTransaction, Record

from src.graph.client import Neo4jClient
from src.graph.ingestion_control_instance_schema import (
    ConstraintDefinition as _ConstraintDefinition,
)
from src.graph.ingestion_control_instance_schema import (
    assert_constraint as _assert_constraint,
)
from src.graph.ingestion_control_instance_schema import (
    assert_no_unexpected_named_constraint as _assert_no_unexpected_named_constraint_impl,
)
from src.graph.ingestion_control_instance_schema import (
    create_constraint as _create_constraint,
)
from src.graph.ingestion_control_instance_schema import (
    show_constraints as _show_constraints,
)
from src.graph.ingestion_control_instance_validation import (
    validate_rows_and_collisions as _validate_rows_and_collisions,
)
from src.graph.queries.ingestion_control_instance_migration import (
    ACQUIRE_MIGRATION_LEASE,
    ADVANCE_PHASE,
    AFFECTED_LABELS,
    BLOCK_LEGACY_DISPATCH,
    FINISH_BACKFILL_LABEL,
    LEGACY_CONSTRAINT_SPECS,
    MARK_COMPLETE,
    MIGRATION_KEY,
    NEW_CONSTRAINT_SPECS,
    PHASES,
    PREPARE_BACKFILL_LABEL,
    RELEASE_MIGRATION_LEASE,
    RENEW_MIGRATION_LEASE,
    backfill_label_query,
)

_BATCH_SIZE = 500
_LEASE_SECONDS = 120
_REGISTRY_CONSTRAINT_SPEC = (
    "bitrix_source_instance_identity_unique",
    "BitrixSourceInstance",
    ("source_key", "source_instance_id"),
)


@dataclass(frozen=True)
class _MigrationState:
    phase: str
    cursor: str
    active_label: str
    progress_count: int


def migrate_ingestion_control_instances(
    client: Neo4jClient,
    *,
    ensure_legacy_registration: Callable[[], None] | None = None,
) -> None:
    """Safely migrate every existing control row into the legacy namespace.

    The migration is deliberately fail-closed.  It retains its marker and its
    dispatch block after every exception; only its lease is released.  DDL runs
    outside data transactions, so each pass inventories the actual schema and
    treats already-completed, partial-DDL states as explicit postconditions.
    """
    owner_id = uuid.uuid4().hex
    fresh = _is_fresh_database(client)
    state = _acquire(client, owner_id)
    if state is None:
        if _is_complete(client):
            # Never recover a registry row before proving the completed marker
            # still has its exact replacement schema. Recovery is a write.
            _validate_constraint_postconditions(client)
            if ensure_legacy_registration is not None:
                ensure_legacy_registration()
            _validate_steady_postconditions(client)
            return
        raise RuntimeError("control-instance migration lease is held by another worker")
    try:
        if fresh:
            _complete_fresh_database(client, owner_id, state, ensure_legacy_registration)
            return
        if state.phase == "block_dispatch":
            state = _execute_phase(client, owner_id, state)
        if ensure_legacy_registration is not None:
            ensure_legacy_registration()
        while state.phase != "complete":
            _renew(client, owner_id)
            state = _execute_phase(client, owner_id, state)
        _validate_postconditions(
            client,
            legacy_only=True,
            require_reserved_legacy_registration=True,
        )
        _mark_complete(client, owner_id)
    finally:
        _release(client, owner_id)


def assert_ingestion_control_ready(client: Neo4jClient) -> None:
    """Reject execution unless exact instance schema and completion marker exist."""

    def _read(tx: ManagedTransaction) -> bool:
        record = tx.run(
            "MATCH (migration:DataMigration {migration_key: $migration_key}) "
            "RETURN migration.completed_at IS NOT NULL AS ready",
            migration_key=MIGRATION_KEY,
        ).single()
        return record is not None and record["ready"] is True

    if not client.execute_read(_read):
        raise RuntimeError("Bitrix control-instance migration is not complete")
    _validate_steady_postconditions(client)


def _is_fresh_database(client: Neo4jClient) -> bool:
    """Classify an unmarked graph with no retired controls as a fresh install."""
    constraints = _show_constraints(client)
    if any(spec[0] in constraints for spec in LEGACY_CONSTRAINT_SPECS):
        return False
    label_predicate = " OR ".join(f"node:{label}" for label in AFFECTED_LABELS)

    def _read(tx: ManagedTransaction) -> bool:
        record = tx.run(
            "OPTIONAL MATCH (migration:DataMigration {migration_key: $migration_key}) "
            "WITH count(migration) AS markers "
            "OPTIONAL MATCH (node) "
            f"WHERE {label_predicate} "
            "RETURN markers = 0 AND count(node) = 0 AS fresh",
            migration_key=MIGRATION_KEY,
        ).single()
        return record is not None and record["fresh"] is True

    return client.execute_read(_read)


def _complete_fresh_database(
    client: Neo4jClient,
    owner_id: str,
    state: _MigrationState,
    ensure_legacy_registration: Callable[[], None] | None,
) -> None:
    """Install only replacement identities for a zero-row, unmarked graph."""
    if state.phase != "block_dispatch":
        raise RuntimeError("fresh control-instance migration has unexpected persisted phase")
    _replace_verified_constraints(client, drop_legacy=False, create_new=True)
    if ensure_legacy_registration is not None:
        ensure_legacy_registration()
    _validate_postconditions(
        client,
        legacy_only=True,
        require_reserved_legacy_registration=True,
    )
    _advance(client, owner_id, "block_dispatch", "complete")
    _mark_complete(client, owner_id)


def _is_complete(client: Neo4jClient) -> bool:
    def _read(tx: ManagedTransaction) -> bool:
        record = tx.run(
            "MATCH (migration:DataMigration {migration_key: $migration_key}) "
            "RETURN migration.completed_at IS NOT NULL AS completed",
            migration_key=MIGRATION_KEY,
        ).single()
        return record is not None and record["completed"] is True

    return client.execute_read(_read)


def _acquire(client: Neo4jClient, owner_id: str) -> _MigrationState | None:
    def _work(tx: ManagedTransaction) -> _MigrationState | None:
        record = tx.run(
            ACQUIRE_MIGRATION_LEASE,
            migration_key=MIGRATION_KEY,
            owner_id=owner_id,
            lease_seconds=_LEASE_SECONDS,
        ).single()
        if record is None or record["acquired"] is not True:
            return None
        return _state(record)

    return client.execute_write(_work)


def _renew(client: Neo4jClient, owner_id: str) -> None:
    def _work(tx: ManagedTransaction) -> bool:
        record = tx.run(
            RENEW_MIGRATION_LEASE,
            migration_key=MIGRATION_KEY,
            owner_id=owner_id,
            lease_seconds=_LEASE_SECONDS,
        ).single()
        return record is not None and record["renewed"] is True

    if not client.execute_write(_work):
        raise RuntimeError("control-instance migration lost its lease")


def _release(client: Neo4jClient, owner_id: str) -> None:
    def _work(tx: ManagedTransaction) -> None:
        tx.run(RELEASE_MIGRATION_LEASE, migration_key=MIGRATION_KEY, owner_id=owner_id).consume()

    client.execute_write(_work)


def _execute_phase(
    client: Neo4jClient,
    owner_id: str,
    state: _MigrationState,
) -> _MigrationState:
    phase = state.phase
    if phase == "block_dispatch":
        _block_dispatch(client, owner_id)
        return _read_owned_state(client, owner_id)
    if phase == "inventory":
        _inventory_schema(client)
        return _advance(client, owner_id, phase, "backfill_ingest_runs")
    if phase == "backfill_ingest_runs":
        _backfill_labels(client, owner_id, phase, ("IngestRun",))
        return _advance(client, owner_id, phase, "backfill_logical_runs")
    if phase == "backfill_logical_runs":
        _backfill_labels(client, owner_id, phase, ("IngestionLogicalRun",))
        return _advance(client, owner_id, phase, "backfill_checkpoints")
    if phase == "backfill_checkpoints":
        _backfill_labels(client, owner_id, phase, ("IngestionCheckpoint",))
        return _advance(client, owner_id, phase, "backfill_bitrix_streams_and_fences")
    if phase == "backfill_bitrix_streams_and_fences":
        _backfill_labels(client, owner_id, phase, ("BitrixIngestionStream",))
        return _advance(client, owner_id, phase, "backfill_dispatch_generations_publications")
    if phase == "backfill_dispatch_generations_publications":
        _backfill_labels(
            client,
            owner_id,
            phase,
            tuple(
                label
                for label in AFFECTED_LABELS
                if label
                not in {
                    "IngestRun",
                    "IngestionLogicalRun",
                    "IngestionCheckpoint",
                    "BitrixIngestionStream",
                }
            ),
        )
        return _advance(client, owner_id, phase, "validate_rows_and_future_identities")
    if phase == "validate_rows_and_future_identities":
        _validate_rows_and_collisions(client, legacy_only=True)
        return _advance(client, owner_id, phase, "drop_verified_legacy_constraints")
    if phase == "drop_verified_legacy_constraints":
        _replace_verified_constraints(client, drop_legacy=True, create_new=False)
        return _advance(client, owner_id, phase, "create_instance_constraints")
    if phase == "create_instance_constraints":
        _replace_verified_constraints(client, drop_legacy=False, create_new=True)
        return _advance(client, owner_id, phase, "postvalidate")
    if phase == "postvalidate":
        _validate_postconditions(
            client,
            legacy_only=True,
            require_reserved_legacy_registration=True,
        )
        return _advance(client, owner_id, phase, "complete")
    if phase not in PHASES:
        raise RuntimeError("control-instance migration has an unknown phase")
    raise RuntimeError("control-instance migration cannot advance from complete")


def _block_dispatch(client: Neo4jClient, owner_id: str) -> None:
    def _work(tx: ManagedTransaction) -> None:
        record = tx.run(
            BLOCK_LEGACY_DISPATCH, migration_key=MIGRATION_KEY, owner_id=owner_id
        ).single()
        if record is None or record["advanced"] is not True:
            raise RuntimeError("control-instance migration could not block Bitrix dispatch")

    client.execute_write(_work)


def _backfill_labels(
    client: Neo4jClient, owner_id: str, phase: str, labels: tuple[str, ...]
) -> None:
    state = _read_owned_state(client, owner_id)
    if state.active_label:
        if state.active_label not in labels:
            raise RuntimeError("control-instance migration marker has an invalid active label")
        _backfill_label(client, owner_id, phase, state.active_label, state.cursor)
    for label in labels:
        state = _read_owned_state(client, owner_id)
        if state.active_label == label:
            continue
        _backfill_label(client, owner_id, phase, label, "")


def _backfill_label(
    client: Neo4jClient, owner_id: str, phase: str, label: str, initial_cursor: str
) -> None:
    cursor = _prepare_backfill_label(client, owner_id, phase, label, initial_cursor)
    while True:
        _renew(client, owner_id)

        def _work(tx: ManagedTransaction, _cursor: str = cursor) -> tuple[int, str]:
            record = tx.run(
                backfill_label_query(label),
                migration_key=MIGRATION_KEY,
                owner_id=owner_id,
                phase=phase,
                cursor=_cursor,
                batch_size=_BATCH_SIZE,
            ).single()
            if record is None:
                raise RuntimeError("control-instance migration lost its phase lease")
            return _int(record, "updated"), _text(record, "next_cursor")

        updated, next_cursor = client.execute_write(_work)
        if updated == 0:
            _finish_backfill_label(client, owner_id, phase, label)
            return
        if not next_cursor or next_cursor <= cursor:
            raise RuntimeError("control-instance migration cursor did not advance")
        cursor = next_cursor


def _prepare_backfill_label(
    client: Neo4jClient, owner_id: str, phase: str, label: str, cursor: str
) -> str:
    def _work(tx: ManagedTransaction) -> str:
        record = tx.run(
            PREPARE_BACKFILL_LABEL,
            migration_key=MIGRATION_KEY,
            owner_id=owner_id,
            phase=phase,
            label=label,
        ).single()
        if record is None:
            raise RuntimeError("control-instance migration could not claim its backfill label")
        persisted = _text(record, "cursor")
        if cursor and persisted and cursor != persisted:
            raise RuntimeError("control-instance migration cursor ownership changed")
        return persisted or cursor

    return client.execute_write(_work)


def _finish_backfill_label(client: Neo4jClient, owner_id: str, phase: str, label: str) -> None:
    def _work(tx: ManagedTransaction) -> None:
        record = tx.run(
            FINISH_BACKFILL_LABEL,
            migration_key=MIGRATION_KEY,
            owner_id=owner_id,
            phase=phase,
            label=label,
        ).single()
        if record is None or record["finished"] is not True:
            raise RuntimeError("control-instance migration could not finish its backfill label")

    client.execute_write(_work)


def _advance(
    client: Neo4jClient, owner_id: str, expected_phase: str, next_phase: str
) -> _MigrationState:
    def _work(tx: ManagedTransaction) -> _MigrationState:
        record = tx.run(
            ADVANCE_PHASE,
            migration_key=MIGRATION_KEY,
            owner_id=owner_id,
            expected_phase=expected_phase,
            next_phase=next_phase,
        ).single()
        if record is None or record["advanced"] is not True:
            raise RuntimeError("control-instance migration phase transition was rejected")
        return _read_state_record(tx, owner_id)

    return client.execute_write(_work)


def _read_owned_state(client: Neo4jClient, owner_id: str) -> _MigrationState:
    def _work(tx: ManagedTransaction) -> _MigrationState:
        return _read_state_record(tx, owner_id)

    return client.execute_read(_work)


def _read_state_record(tx: ManagedTransaction, owner_id: str) -> _MigrationState:
    record = tx.run(
        "MATCH (migration:DataMigration {migration_key: $migration_key, lease_owner: $owner_id}) "
        "WHERE migration.lease_until >= datetime() "
        "RETURN migration.phase AS phase, coalesce(migration.cursor, '') AS cursor, "
        "coalesce(migration.active_label, '') AS active_label, "
        "coalesce(migration.progress_count, 0) AS progress_count",
        migration_key=MIGRATION_KEY,
        owner_id=owner_id,
    ).single()
    if record is None:
        raise RuntimeError("control-instance migration lease state is missing")
    return _state(record)


def _assert_no_unexpected_named_constraint(
    constraints: dict[str, _ConstraintDefinition],
) -> None:
    _assert_no_unexpected_named_constraint_impl(
        constraints, LEGACY_CONSTRAINT_SPECS + NEW_CONSTRAINT_SPECS + (_REGISTRY_CONSTRAINT_SPEC,)
    )


def _inventory_schema(client: Neo4jClient) -> None:
    constraints = _show_constraints(client)
    _assert_no_unexpected_named_constraint(constraints)
    for spec in LEGACY_CONSTRAINT_SPECS:
        found = constraints.get(spec[0])
        if found is not None:
            _assert_constraint(found, spec)
    for spec in NEW_CONSTRAINT_SPECS + (_REGISTRY_CONSTRAINT_SPEC,):
        found = constraints.get(spec[0])
        if found is not None:
            _assert_constraint(found, spec)


def _replace_verified_constraints(
    client: Neo4jClient, *, drop_legacy: bool, create_new: bool
) -> None:
    constraints = _show_constraints(client)
    _assert_no_unexpected_named_constraint(constraints)
    if drop_legacy:
        with client.session() as session:
            for spec in LEGACY_CONSTRAINT_SPECS:
                found = constraints.get(spec[0])
                if found is not None:
                    _assert_constraint(found, spec)
                    session.run(f"DROP CONSTRAINT {spec[0]} IF EXISTS").consume()
    if create_new:
        constraints = _show_constraints(client)
        with client.session() as session:
            for spec in NEW_CONSTRAINT_SPECS:
                found = constraints.get(spec[0])
                if found is None:
                    session.run(_create_constraint(spec)).consume()
                else:
                    _assert_constraint(found, spec)


def _validate_postconditions(
    client: Neo4jClient,
    *,
    legacy_only: bool,
    require_reserved_legacy_registration: bool = False,
) -> None:
    _validate_rows_and_collisions(client, legacy_only=legacy_only)
    _validate_constraint_postconditions(client)
    if require_reserved_legacy_registration:
        _validate_reserved_legacy_registration(client)


def _validate_steady_postconditions(client: Neo4jClient) -> None:
    _validate_rows_and_collisions(client, legacy_only=False)
    _validate_constraint_postconditions(client)
    _validate_reserved_legacy_registration(client)


def _validate_constraint_postconditions(client: Neo4jClient) -> None:
    constraints = _show_constraints(client)
    _assert_no_unexpected_named_constraint(constraints)
    for spec in NEW_CONSTRAINT_SPECS + (_REGISTRY_CONSTRAINT_SPEC,):
        found = constraints.get(spec[0])
        if found is None:
            raise RuntimeError(f"control-instance constraint {spec[0]} is missing")
        _assert_constraint(found, spec)
    for name, _label, _properties in LEGACY_CONSTRAINT_SPECS:
        if name in constraints:
            raise RuntimeError(f"retired control-instance constraint {name} still exists")


def _validate_reserved_legacy_registration(client: Neo4jClient) -> None:
    def _read(tx: ManagedTransaction) -> bool:
        record = tx.run(
            "OPTIONAL MATCH (instance:BitrixSourceInstance {source_key: 'bitrix_chat', "
            "source_instance_id: 'legacy-default', status: 'active'}) "
            "WITH collect(DISTINCT instance) AS instances "
            "RETURN size(instances) = 1 AND size([(instances[0])-[:INSTANCE_OF]->(:SourceSystem "
            "{source_key: 'bitrix_chat', is_active: true}) | 1]) = 1 "
            "AND size([(instances[0])-[:INSTANCE_OF]->(:SourceSystem) | 1]) = 1 AS ready"
        ).single()
        return record is not None and record["ready"] is True

    if not client.execute_read(_read):
        raise RuntimeError("reserved legacy Bitrix source registration is not ready")


def _mark_complete(client: Neo4jClient, owner_id: str) -> None:
    def _work(tx: ManagedTransaction) -> None:
        record = tx.run(MARK_COMPLETE, migration_key=MIGRATION_KEY, owner_id=owner_id).single()
        if record is None:
            raise RuntimeError("control-instance migration completion was rejected")

    client.execute_write(_work)


def _state(record: Record) -> _MigrationState:
    phase = _text(record, "phase")
    if phase not in PHASES:
        raise RuntimeError("control-instance migration marker has an invalid phase")
    return _MigrationState(
        phase,
        _text(record, "cursor"),
        _text(record, "active_label"),
        _int(record, "progress_count"),
    )


def _text(record: Record, key: str) -> str:
    value = record[key]
    if not isinstance(value, str):
        raise RuntimeError(f"control-instance migration returned invalid {key}")
    return value


def _int(record: Record, key: str) -> int:
    value: object = record[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"control-instance migration returned invalid {key}")
    return value
