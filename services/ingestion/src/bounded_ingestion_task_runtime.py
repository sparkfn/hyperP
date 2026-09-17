"""Task-facing validation and execution for one bounded ingestion unit."""

from __future__ import annotations

import hashlib
import json
import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import TypedDict

from pydantic.types import JsonValue

from src.bounded_ingestion_dispatch import dispatch_one
from src.bounded_ingestion_models import (
    BoundedMode,
    BoundedRecoveryLeasedError,
    BoundedRunResult,
    RunScope,
    utc_now,
)
from src.bounded_ingestion_window import occurrence_from_payload
from src.config import get_settings
from src.connectors.registry import BoundedConnectorRegistry, registry
from src.graph.bitrix_source_instances import BitrixSourceInstanceRepository
from src.graph.bounded_ingestion_control import BoundedIngestionControl
from src.graph.client import Neo4jClient
from src.ingestion_config import get_ingestion_config
from src.source_instances import effective_control_instance_id, effective_source_instance_id

_shutdown_requested = threading.Event()


class BoundedTaskSummary(TypedDict):
    ingest_run_id: str
    logical_run_id: str
    status: str
    succeeded: int
    errors: int
    skipped: int
    source_key: str
    mode: str
    dump_path: None
    entity_key: str | None
    pause_reason: str | None
    failure_category: str | None


def request_bounded_shutdown(**_kwargs: object) -> None:
    _shutdown_requested.set()


def clear_bounded_shutdown_for_test() -> None:
    _shutdown_requested.clear()


class WorkerShutdownSignal:
    def requested(self) -> bool:
        return _shutdown_requested.is_set()


@contextmanager
def bounded_shutdown_signal() -> Iterator[None]:
    """Convert SIGTERM into cooperative drain for the current bounded unit."""
    clear_bounded_shutdown_for_test()
    previous = signal.getsignal(signal.SIGTERM)

    def request_shutdown(_signum: int, _frame: object) -> None:
        _shutdown_requested.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def bounded_configuration_fingerprint(
    *,
    environment: str,
    reset_generation: int,
    source_key: str,
    control_instance_id: str,
    entity_key: str | None,
    stream_key: str | None,
    mode: BoundedMode,
    connector_version: str,
    configuration_version: str,
    checkpoint_schema_version: int,
) -> str:
    payload = {
        "environment": environment,
        "reset_generation": reset_generation,
        "source_key": source_key,
        "control_instance_id": control_instance_id,
        "entity_key": entity_key,
        "stream_key": stream_key,
        "mode": mode,
        "connector_version": connector_version,
        "configuration_version": configuration_version,
        "checkpoint_schema_version": checkpoint_schema_version,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def run_registered_bounded_unit(
    *,
    source_key: str,
    entity_key: str | None,
    control_instance_id: str | None,
    worker_task_id: str,
    environment: str,
    reset_generation: int,
    bounded_mode: BoundedMode,
    configuration_fingerprint: str,
    connector_version: str,
    configuration_version: str,
    checkpoint_schema_version: int,
    source_window: dict[str, JsonValue],
    occurrence_payload: dict[str, str],
    stream_key: str | None = None,
    descriptor_registry: BoundedConnectorRegistry = registry,
    now: datetime | None = None,
) -> BoundedTaskSummary:
    occurrence = occurrence_from_payload(occurrence_payload)
    scope = _scope(
        source_key=source_key,
        entity_key=entity_key,
        control_instance_id=control_instance_id,
        environment=environment,
        reset_generation=reset_generation,
        bounded_mode=bounded_mode,
        configuration_fingerprint=configuration_fingerprint,
        connector_version=connector_version,
        configuration_version=configuration_version,
        checkpoint_schema_version=checkpoint_schema_version,
        source_window=source_window,
        stream_key=stream_key,
        descriptor_registry=descriptor_registry,
    )
    clock = (lambda: now) if now is not None else utc_now
    budget = get_ingestion_config().bounded_ingestion
    client = Neo4jClient(get_settings())
    try:
        if source_key == "bitrix_chat":
            source_instance_id = effective_source_instance_id(
                get_ingestion_config().bitrix_openlines.source_instance_id
            )
            BitrixSourceInstanceRepository(client).admit(
                control_instance_id=scope.control_instance_id,
                source_instance_id=source_instance_id,
            )
        result = dispatch_one(
            registry=descriptor_registry,
            control=BoundedIngestionControl(
                client,
                transaction_timeout_seconds=budget.max_graph_transaction_seconds,
            ),
            scope=scope,
            occurrence=occurrence,
            worker_task_id=worker_task_id,
            budget=budget,
            clock=clock,
            shutdown=WorkerShutdownSignal(),
        )
    finally:
        client.close()
    return _summary(result, source_key, entity_key, bounded_mode)


def active_reset_generation(environment: str) -> int | None:
    budget = get_ingestion_config().bounded_ingestion
    client = Neo4jClient(get_settings())
    try:
        return BoundedIngestionControl(
            client,
            transaction_timeout_seconds=budget.max_graph_transaction_seconds,
        ).active_reset_generation(environment)
    finally:
        client.close()


def pause_bounded_run_disabled(
    *,
    logical_run_id: str,
    source_key: str,
    control_instance_id: str,
    reset_generation: int,
) -> bool:
    budget = get_ingestion_config().bounded_ingestion
    client = Neo4jClient(get_settings())
    try:
        return BoundedIngestionControl(
            client,
            transaction_timeout_seconds=budget.max_graph_transaction_seconds,
        ).pause_unclaimed(
            logical_run_id,
            source_key,
            control_instance_id,
            reset_generation,
            "disabled",
        )
    finally:
        client.close()


def recover_bounded_logical_run(
    *,
    logical_run_id: str,
    source_key: str,
    control_instance_id: str,
    reset_generation: int,
    worker_task_id: str,
    descriptor_registry: BoundedConnectorRegistry = registry,
    now: datetime | None = None,
) -> BoundedTaskSummary:
    client = Neo4jClient(get_settings())
    try:
        budget = get_ingestion_config().bounded_ingestion
        control = BoundedIngestionControl(
            client,
            transaction_timeout_seconds=budget.max_graph_transaction_seconds,
        )
        recovery = control.recovery_state(
            logical_run_id,
            source_key,
            control_instance_id,
            reset_generation,
            now or utc_now(),
        )
        if recovery is None:
            return _blocked_summary(source_key, None, "recovery_not_eligible")
        if recovery == "completed":
            return _completed_recovery_summary(source_key)
        if isinstance(recovery, datetime):
            raise BoundedRecoveryLeasedError(recovery)
        if (
            recovery.scope.mode != "one_time"
            and not get_ingestion_config().scheduled_ingestion.enabled
        ):
            control.pause_unclaimed(
                logical_run_id,
                source_key,
                control_instance_id,
                reset_generation,
                "disabled",
            )
            return _blocked_summary(
                source_key,
                recovery.scope.entity_key,
                "scheduled_ingestion_disabled",
            )
        descriptor = descriptor_registry.require(source_key, recovery.scope.mode)
        expected = bounded_configuration_fingerprint(
            environment=recovery.scope.environment,
            reset_generation=recovery.scope.reset_generation,
            source_key=recovery.scope.source_key,
            control_instance_id=recovery.scope.control_instance_id,
            entity_key=recovery.scope.entity_key,
            stream_key=recovery.scope.stream_key,
            mode=recovery.scope.mode,
            connector_version=descriptor.connector_version,
            configuration_version=descriptor.configuration_version,
            checkpoint_schema_version=descriptor.checkpoint_schema_version,
        )
        if expected != recovery.scope.configuration_fingerprint:
            return _blocked_summary(
                source_key,
                recovery.scope.entity_key,
                "configuration_mismatch",
            )
        if source_key == "bitrix_chat":
            source_instance_id = effective_source_instance_id(
                get_ingestion_config().bitrix_openlines.source_instance_id
            )
            BitrixSourceInstanceRepository(client).admit(
                control_instance_id=recovery.scope.control_instance_id,
                source_instance_id=source_instance_id,
            )
        result = dispatch_one(
            registry=descriptor_registry,
            control=control,
            scope=recovery.scope,
            occurrence=recovery.occurrence,
            worker_task_id=worker_task_id,
            budget=budget,
            clock=(lambda: now) if now is not None else utc_now,
            shutdown=WorkerShutdownSignal(),
        )
        return _summary(
            result,
            recovery.scope.source_key,
            recovery.scope.entity_key,
            recovery.scope.mode,
        )
    finally:
        client.close()


def _scope(
    *,
    source_key: str,
    entity_key: str | None,
    control_instance_id: str | None,
    environment: str,
    reset_generation: int,
    bounded_mode: BoundedMode,
    configuration_fingerprint: str,
    connector_version: str,
    configuration_version: str,
    checkpoint_schema_version: int,
    source_window: dict[str, JsonValue],
    stream_key: str | None,
    descriptor_registry: BoundedConnectorRegistry,
) -> RunScope:
    settings = get_settings()
    if environment != settings.deployment_environment:
        raise ValueError("bounded delivery environment does not match this worker")
    if reset_generation < 1 or checkpoint_schema_version < 1:
        raise ValueError("bounded delivery generations must be positive")
    control_id = effective_control_instance_id(control_instance_id)
    descriptor = descriptor_registry.require(source_key, bounded_mode)
    if descriptor.connector_version != connector_version:
        raise ValueError("bounded connector version mismatch")
    if descriptor.configuration_version != configuration_version:
        raise ValueError("bounded configuration version mismatch")
    if descriptor.checkpoint_schema_version != checkpoint_schema_version:
        raise ValueError("bounded checkpoint schema version mismatch")
    expected = bounded_configuration_fingerprint(
        environment=environment,
        reset_generation=reset_generation,
        source_key=source_key,
        control_instance_id=control_id,
        entity_key=entity_key,
        stream_key=stream_key,
        mode=bounded_mode,
        connector_version=connector_version,
        configuration_version=configuration_version,
        checkpoint_schema_version=checkpoint_schema_version,
    )
    if configuration_fingerprint != expected:
        raise ValueError("bounded configuration fingerprint mismatch")
    return RunScope(
        environment=environment,
        reset_generation=reset_generation,
        source_key=source_key,
        control_instance_id=control_id,
        entity_key=entity_key,
        stream_key=stream_key,
        mode=bounded_mode,
        configuration_fingerprint=configuration_fingerprint,
        connector_version=connector_version,
        checkpoint_schema_version=checkpoint_schema_version,
        source_window=dict(source_window),
    )


def _completed_recovery_summary(source_key: str) -> BoundedTaskSummary:
    return {
        "ingest_run_id": "",
        "logical_run_id": "",
        "status": "completed",
        "succeeded": 0,
        "errors": 0,
        "skipped": 1,
        "source_key": source_key,
        "mode": "delta",
        "dump_path": None,
        "entity_key": None,
        "pause_reason": None,
        "failure_category": None,
    }


def _blocked_summary(
    source_key: str,
    entity_key: str | None,
    failure_category: str,
) -> BoundedTaskSummary:
    return {
        "ingest_run_id": "",
        "logical_run_id": "",
        "status": "blocked",
        "succeeded": 0,
        "errors": 1,
        "skipped": 0,
        "source_key": source_key,
        "mode": "delta",
        "dump_path": None,
        "entity_key": entity_key,
        "pause_reason": None,
        "failure_category": failure_category,
    }


def _summary(
    result: BoundedRunResult,
    source_key: str,
    entity_key: str | None,
    mode: BoundedMode,
) -> BoundedTaskSummary:
    context = result.context
    completed = result.status == "completed"
    return {
        "ingest_run_id": context.ingest_run_id if context else "",
        "logical_run_id": context.logical_run_id if context else "",
        "status": result.status,
        "succeeded": result.unit_usage.records if completed else 0,
        "errors": 1 if result.status in {"failed", "blocked"} else 0,
        "skipped": 0,
        "source_key": source_key,
        "mode": mode,
        "dump_path": None,
        "entity_key": entity_key,
        "pause_reason": result.pause_reason,
        "failure_category": result.failure_category,
    }
