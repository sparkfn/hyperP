"""Manual-only, default-off dispatch for bounded standalone CRM source sync."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import cast

from celery import shared_task
from neo4j import ManagedTransaction

from src.config import get_settings
from src.crm_tenant_mapping_contracts import CrmTenantMappingScope
from src.crm_tenant_orchestration import (
    ScheduledSourceSyncRequestInput,
    build_scheduled_source_sync_request,
)
from src.graph.client import Neo4jClient
from src.graph.crm_tenant_mapping import Neo4jCrmTenantMappingRepository
from src.graph.queries.crm_tenant_projection_freshness import READ_EXACT_ACTIVE_PROJECTION_HEAD
from src.ingestion_config import (
    BitrixOpenLinesConfig,
    get_ingestion_config,
    standalone_crm_source_sync_configuration_digest,
)
from src.source_instances import effective_control_instance_id
from src.standalone_crm_census_requests import (
    SourceSyncAuthority,
    SourceSyncCensusRequest,
    StandaloneCrmBudget,
)
from src.standalone_crm_census_tasks import admit_and_run_standalone_crm_census
from src.standalone_crm_census_types import StandaloneCrmStreamKind

ScheduledSourceSyncAuthorityProvider = Callable[[str, str], SourceSyncAuthority]
_CANONICAL_KINDS: tuple[StandaloneCrmStreamKind, ...] = ("contact", "lead", "company")
_POLICY_VERSION = "standalone-crm-source-sync-v1"
_OCCURRENCE_PREFIX = "standalone-crm-source-sync-v1"


def _scheduled_source_sync_authority_provider(
    source_instance_id: str,
    control_instance_id: str,
) -> SourceSyncAuthority:
    """Capture complete active mapping and projection heads; never select a latest row."""
    client = Neo4jClient(get_settings())
    try:
        scope = CrmTenantMappingScope("bitrix_chat", source_instance_id, control_instance_id)
        mapping = Neo4jCrmTenantMappingRepository(client).get_active_head(scope)
        if mapping is None:
            raise RuntimeError("enabled standalone CRM source sync requires an active mapping head")
        projection = client.execute_read(
            lambda tx: _projection_head(tx, source_instance_id, control_instance_id)
        )
        if projection is None:
            raise RuntimeError(
                "enabled standalone CRM source sync requires an active projection head"
            )
        return SourceSyncAuthority(
            mapping.head_id,
            mapping.active_manifest_digest,
            projection[0],
            projection[3],
            mapping.active_revision_id,
            mapping.active_revision_number,
            projection[1],
            projection[2],
        )
    finally:
        client.close()


def _projection_head(
    tx: ManagedTransaction, source_instance_id: str, control_instance_id: str
) -> tuple[str, str, int, str] | None:
    rows = list(
        tx.run(
            READ_EXACT_ACTIVE_PROJECTION_HEAD,
            source_key="bitrix_chat",
            source_instance_id=source_instance_id,
            control_instance_id=control_instance_id,
        )
    )
    if len(rows) > 1:
        raise RuntimeError("active projection head is not unique")
    if not rows:
        return None
    row = rows[0]
    values = (
        row.get("head_id"),
        row.get("release_id"),
        row.get("release_number"),
        row.get("fingerprint"),
    )
    if (
        not isinstance(values[0], str)
        or not isinstance(values[1], str)
        or isinstance(values[2], bool)
        or not isinstance(values[2], int)
        or not isinstance(values[3], str)
    ):
        raise RuntimeError("active projection head is malformed")
    return values[0], values[1], values[2], values[3]


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


@shared_task(  # type: ignore[untyped-decorator]  # Celery's decorator is untyped.
    name="src.standalone_crm_schedule_tasks.dispatch_standalone_crm_source_sync"
)
def dispatch_standalone_crm_source_sync() -> str | None:
    """Publish one bounded configured source-sync census only after both local gates pass."""
    config = get_ingestion_config().bitrix_openlines
    identity_enabled = config.standalone_crm_identity_enabled
    schedule_enabled = config.standalone_crm_identity_schedule_enabled
    if not identity_enabled or not schedule_enabled:
        return None
    input_value = _configured_request_input(config, _utc_now())
    request = build_scheduled_source_sync_request(input_value)
    return cast(
        str,
        admit_and_run_standalone_crm_census.delay(_external_request_payload(request)).id,
    )


def _configured_request_input(
    config: BitrixOpenLinesConfig,
    now: datetime,
) -> ScheduledSourceSyncRequestInput:
    if config.source_instance_id is None:
        raise RuntimeError("enabled standalone CRM source sync requires a source instance")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("scheduled source-sync clock must be timezone-aware")
    source_instance_id = config.source_instance_id
    control_instance_id = effective_control_instance_id(None)
    authority = _scheduled_source_sync_authority_provider(source_instance_id, control_instance_id)
    deadline = now + timedelta(
        seconds=math.ceil(config.standalone_crm_identity_max_wall_clock_seconds_per_occurrence)
    )
    return ScheduledSourceSyncRequestInput(
        source_instance_id=source_instance_id,
        control_instance_id=control_instance_id,
        occurrence_key=f"{_OCCURRENCE_PREFIX}:{now.astimezone(UTC).date().isoformat()}",
        selected_kinds=tuple(
            kind for kind in _CANONICAL_KINDS if kind in config.standalone_crm_identity_kinds
        ),
        budget=StandaloneCrmBudget(
            max_calls_per_attempt=config.standalone_crm_identity_max_calls_per_attempt,
            max_rows_per_attempt=config.standalone_crm_identity_max_rows_per_attempt,
            max_runtime_seconds_per_attempt=math.ceil(
                config.standalone_crm_identity_max_runtime_seconds_per_attempt
            ),
            max_calls_per_occurrence=config.standalone_crm_identity_max_calls_per_occurrence,
            max_rows_per_occurrence=config.standalone_crm_identity_max_rows_per_occurrence,
            max_attempts_per_occurrence=config.standalone_crm_identity_max_attempts_per_occurrence,
            occurrence_deadline=deadline.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        ),
        policy_version=_POLICY_VERSION,
        association_contract_version=config.crm_identity_association_contract_version,
        configuration_digest=standalone_crm_source_sync_configuration_digest(config),
        authority=authority,
    )


def _external_request_payload(request: SourceSyncCensusRequest) -> dict[str, object]:
    payload = asdict(request)
    payload["selected_kinds"] = list(request.selected_kinds)
    return payload
