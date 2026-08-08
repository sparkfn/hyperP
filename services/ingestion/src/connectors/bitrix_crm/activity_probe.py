"""Bounded strict-keyset capability gate for Bitrix CRM activities."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from src.connectors.bitrix_openlines.models import CrmActivityCapabilityPage
from src.connectors.bitrix_stage_history.models import ProbeLimits


class ActivityCapabilityClient(Protocol):
    def list_crm_activity_capability_page(
        self,
        *,
        greater_than_id: int | None,
        less_than_or_equal_to_id: int,
        order_direction: str = "ASC",
    ) -> CrmActivityCapabilityPage: ...


@dataclass(frozen=True)
class ActivityCapabilityReport:
    traversal_outcome: str
    upper_activity_id: int
    calls: int
    rows: int
    source_total: int | None
    source_total_consistent: bool
    runtime_seconds: float


def freeze_activity_upper_id(client: ActivityCapabilityClient) -> int:
    page = client.list_crm_activity_capability_page(
        greater_than_id=None,
        less_than_or_equal_to_id=2**63 - 1,
        order_direction="DESC",
    )
    if not page.items:
        return 0
    ids = [int(item.id) for item in page.items]
    if ids != sorted(ids, reverse=True) or len(ids) != len(set(ids)):
        raise RuntimeError("Bitrix activity upper-bound probe was not strictly descending")
    return ids[0]


def verify_activity_keyset(
    client: ActivityCapabilityClient,
    *,
    upper_activity_id: int,
    limits: ProbeLimits,
) -> ActivityCapabilityReport:
    """Qualify one bounded activity traversal or fail closed as unsupported."""
    started = time.monotonic()
    cursor: int | None = None
    calls = rows = 0
    source_total: int | None = None
    total_consistent = True
    while upper_activity_id > 0:
        if calls >= limits.max_calls:
            raise RuntimeError("Bitrix activity capability call limit exceeded")
        if time.monotonic() - started > limits.max_runtime_seconds:
            raise RuntimeError("Bitrix activity capability runtime limit exceeded")
        page = client.list_crm_activity_capability_page(
            greater_than_id=cursor,
            less_than_or_equal_to_id=upper_activity_id,
        )
        calls += 1
        if source_total is None:
            source_total = page.total
        elif page.total != source_total:
            total_consistent = False
        ids = [int(item.id) for item in page.items]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise RuntimeError("Bitrix activity capability page was not strictly increasing")
        if cursor is not None and ids and ids[0] <= cursor:
            raise RuntimeError("Bitrix activity capability keyset did not advance")
        if any(value > upper_activity_id for value in ids):
            raise RuntimeError("Bitrix activity capability exceeded its frozen boundary")
        rows += len(ids)
        if rows > limits.max_rows:
            raise RuntimeError("Bitrix activity capability row limit exceeded")
        if len(ids) < 50:
            break
        if not ids:
            raise RuntimeError("Bitrix activity capability returned an invalid full page")
        cursor = ids[-1]
    runtime = time.monotonic() - started
    if not total_consistent:
        raise RuntimeError("Bitrix activity capability total changed during traversal")
    if source_total is not None and source_total != rows:
        raise RuntimeError("Bitrix activity capability total did not reconcile")
    return ActivityCapabilityReport(
        traversal_outcome="verified_activity_keyset",
        upper_activity_id=upper_activity_id,
        calls=calls,
        rows=rows,
        source_total=source_total,
        source_total_consistent=total_consistent,
        runtime_seconds=runtime,
    )
