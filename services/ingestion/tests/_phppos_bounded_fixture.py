"""Reusable synthetic fixtures for bounded PHPPOS adapter tests.

Every builder produces a valid frozen window or a valid bounded page envelope;
each test perturbs exactly the field it asserts on.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx

from src.bounded_ingestion_models import (
    AttemptContext,
    BoundedMode,
    OccurrenceContext,
    RunScope,
    Usage,
)
from src.connectors.phppos_api.bounded_checkpoint import (
    PhpposSourceWindow,
    Resource,
    initial_checkpoint,
    resource_for_source,
    source_window_mapping,
)
from src.connectors.phppos_api.client import ApiCredentials, PhpposApiClient
from src.models import JsonValue
from src.resumable import CheckpointDescriptor

FIXTURE_DIRECTORY = Path(__file__).parent / "fixtures" / "phppos_api"
CONFIGURATION_FINGERPRINT = "phppos-bounded-config-fingerprint-1"
CONTRACT_VERSION = "phppos-bounded-v1"
# Computed from the current time so the synthetic window never expires: a
# hardcoded retention date would turn these fixtures into a time bomb.
RETENTION_DAYS = 90
RETENTION_UNTIL = (datetime.now(UTC) + timedelta(days=RETENTION_DAYS)).isoformat()
OBSERVED_AT = datetime(2026, 9, 17, 2, 0, tzinfo=UTC)
TENANTS = {"eko_phppos": "eko-tenant", "speedzone_phppos": "speedzone-tenant"}


def tenant_for(source_key: str) -> str:
    return TENANTS[source_key.split(":")[0]]


def rows(name: str) -> list[dict[str, JsonValue]]:
    """Load one synthetic source-row fixture file."""
    payload = json.loads((FIXTURE_DIRECTORY / name).read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    return [cast(dict[str, JsonValue], row) for row in payload]


def capabilities(**overrides: JsonValue) -> dict[str, JsonValue]:
    values: dict[str, JsonValue] = {
        "effective_changes": True,
        "tombstones": True,
        "complete_sale_aggregates": True,
        "independent_tenant_principal": True,
        "replay_retention_days": 90,
    }
    values.update(overrides)
    return values


def window_payload(
    *,
    snapshot_id: str = "snap-2026-09-17",
    upper_change_version: str = "4127",
    retention_until: str = RETENTION_UNTIL,
    capability_overrides: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    return {
        "contract_version": CONTRACT_VERSION,
        "snapshot_id": snapshot_id,
        "upper_change_version": upper_change_version,
        "retention_until": retention_until,
        "capabilities": capabilities(**(capability_overrides or {})),
    }


def window(
    source_key: str = "eko_phppos",
    *,
    tenant_id: str | None = None,
    snapshot_id: str = "snap-2026-09-17",
) -> PhpposSourceWindow:
    """Return the parsed frozen source window a dispatcher would hand a run."""
    return PhpposSourceWindow.from_mapping(
        {
            "source_key": source_key,
            "tenant_id": tenant_id or tenant_for(source_key),
            "resource": resource_for_source(source_key),
            "configuration_fingerprint": CONFIGURATION_FINGERPRINT,
            "window": window_payload(snapshot_id=snapshot_id),
        },
        source_key=source_key,
        configuration_fingerprint=CONFIGURATION_FINGERPRINT,
    )


def source_window(
    source_key: str = "eko_phppos",
    *,
    tenant_id: str | None = None,
    snapshot_id: str = "snap-2026-09-17",
) -> dict[str, JsonValue]:
    return source_window_mapping(window(source_key, tenant_id=tenant_id, snapshot_id=snapshot_id))


def scope(
    source_key: str = "eko_phppos",
    *,
    mode: BoundedMode = "bootstrap",
    reset_generation: int = 1,
    source_window_override: dict[str, JsonValue] | None = None,
) -> RunScope:
    return RunScope(
        environment="test",
        reset_generation=reset_generation,
        source_key=source_key,
        control_instance_id="test-control",
        entity_key=None,
        stream_key=source_key,
        mode=mode,
        configuration_fingerprint=CONFIGURATION_FINGERPRINT,
        connector_version=CONTRACT_VERSION,
        checkpoint_schema_version=1,
        source_window=source_window_override or source_window(source_key),
    )


def initial_phppos_checkpoint(
    source_key: str = "eko_phppos",
    *,
    checkpoint_scope: RunScope | None = None,
) -> CheckpointDescriptor:
    selected = checkpoint_scope or scope(source_key)
    return initial_checkpoint(
        source_key,
        PhpposSourceWindow.from_mapping(
            selected.source_window,
            source_key=source_key,
            configuration_fingerprint=CONFIGURATION_FINGERPRINT,
        ),
    )


def attempt_context(
    source_key: str = "eko_phppos",
    *,
    checkpoint: CheckpointDescriptor | None = None,
    occurrence: OccurrenceContext | None = None,
    cancelled: bool = False,
    run_scope: RunScope | None = None,
) -> AttemptContext:
    selected = run_scope or scope(source_key)
    return AttemptContext(
        logical_run_id="logical-phppos",
        ingest_run_id="ingest-phppos-1",
        worker_task_id="task-phppos-1",
        attempt_generation=1,
        fencing_token=1,
        lease_token="lease-phppos-1",
        global_slot_index=0,
        global_slot_fencing_token=1,
        scope=selected,
        occurrence=occurrence,
        checkpoint=checkpoint or initial_phppos_checkpoint(source_key, checkpoint_scope=selected),
        usage=Usage(),
        reserved_usage=Usage(),
        operation_deadline_at=OBSERVED_AT + timedelta(minutes=5),
        cancellation=Cancellation(cancelled),
    )


class Cancellation:
    def __init__(self, is_cancelled: bool = False) -> None:
        self.is_cancelled = is_cancelled

    def requested(self) -> bool:
        return self.is_cancelled


def changes(
    resource: Resource,
    *,
    limit: int | None = None,
    version_prefix: str = "4120",
) -> list[dict[str, JsonValue]]:
    """Build upsert changes from the synthetic row fixtures."""
    source = rows("customer_rows.json") if resource == "customers" else rows("sale_rows.json")
    identity_field = "person_id" if resource == "customers" else "sale_id"
    selected = source[:limit] if limit is not None else source
    return [
        {
            "kind": "upsert",
            "source_id": str(row[identity_field]),
            "effective_change_version": f"{version_prefix}{index}",
            "record": row,
        }
        for index, row in enumerate(selected)
    ]


def tombstone(source_id: str, *, version: str = "4130") -> dict[str, JsonValue]:
    return {
        "kind": "tombstone",
        "source_id": source_id,
        "effective_change_version": version,
        "removal_reason": "deleted_customer",
    }


def page_payload(
    resource: Resource = "customers",
    *,
    tenant_id: str | None = None,
    data: list[dict[str, JsonValue]] | None = None,
    next_cursor: str | None = None,
    window_body: dict[str, JsonValue] | None = None,
    source_key: str = "eko_phppos",
) -> dict[str, JsonValue]:
    return {
        "contract_version": CONTRACT_VERSION,
        "tenant_id": tenant_id or tenant_for(source_key),
        "resource": resource,
        "window": window_body or window_payload(),
        "data": changes(resource) if data is None else data,
        "pagination": {
            "next_cursor": next_cursor,
            "has_more": next_cursor is not None,
        },
    }


def credentials(
    *,
    tenant_id: str = "eko-tenant",
    principal_tenant_id: str | None = None,
) -> ApiCredentials:
    return ApiCredentials(
        base_url="https://pos.example",
        client_id="client",
        client_secret="client-secret-value",
        tenant_id=tenant_id,
        page_size=500,
        scopes=("pos.customers.read",),
        principal_tenant_id=principal_tenant_id or tenant_id,
    )


def client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    tenant_id: str = "eko-tenant",
    principal_tenant_id: str | None = None,
    max_attempts: int = 3,
) -> PhpposApiClient:
    return PhpposApiClient(
        credentials(tenant_id=tenant_id, principal_tenant_id=principal_tenant_id),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _seconds: None,
        clock=lambda: 1_000.0,
        wall_clock=lambda: OBSERVED_AT,
        max_attempts=max_attempts,
    )


def scripted_handler(
    payloads: list[dict[str, JsonValue]],
    requests: list[httpx.Request],
    *,
    token: str = "access-token-value",
    status: int | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Serve one OAuth token, then the scripted page payloads in order."""
    served = {"pages": 0}

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(
                200,
                json={"access_token": token, "expires_in": 3600},
                request=request,
            )
        payload = payloads[min(served["pages"], len(payloads) - 1)]
        served["pages"] += 1
        if status is not None:
            return httpx.Response(status, json=payload, request=request)
        return httpx.Response(200, json=payload, request=request)

    return respond
