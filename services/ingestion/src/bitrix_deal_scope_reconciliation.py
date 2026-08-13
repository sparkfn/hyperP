"""Targeted refresh of the immutable known-owner membership set."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Collection
from dataclasses import dataclass
from typing import Protocol

from neo4j import ManagedTransaction

from src.bitrix_backfill_models import KnownOwnerMembershipSet
from src.bitrix_backfill_runtime import record_terminal_unit
from src.bitrix_ingestion_models import ExecutionContext, FenceContext
from src.connectors.bitrix_openlines.connector import _deal_envelope
from src.connectors.bitrix_openlines.models import CrmDeal
from src.graph.bitrix_deal_scope import (
    BitrixDealScopeRepository,
    CurrentDealScope,
    DealScopeObservation,
    record_scope_batch_in_transaction,
)
from src.graph.client import Neo4jClient
from src.models import IngestResult, SourceRecordEnvelope
from src.pipeline import IngestPipeline


class KnownOwnerClient(Protocol):
    @property
    def request_count(self) -> int: ...

    def get_deals_or_none(self, deal_ids: Collection[int]) -> dict[int, CrmDeal | None]: ...

    def get_deal_or_none(self, deal_id: int) -> CrmDeal | None: ...

    def close(self) -> None: ...


class KnownOwnerScope(Protocol):
    def record_healthy_not_found(
        self,
        deal_id: str,
        *,
        fence_context: FenceContext,
    ) -> tuple[int, CurrentDealScope]: ...


@dataclass(frozen=True)
class KnownOwnerRefreshSummary:
    refreshed: int
    moved_out_of_scope: int
    missing_candidates: int
    unresolved: int
    http_request_count: int


def refresh_known_owner_set(
    source: KnownOwnerClient,
    graph: Neo4jClient,
    *,
    membership: KnownOwnerMembershipSet,
    context: ExecutionContext,
    included_category_ids: Collection[str],
    entity_by_category_id: dict[str, str],
) -> KnownOwnerRefreshSummary:
    """Refresh only frozen known owners; never enumerate or materialize global outsiders."""
    included = frozenset(included_category_ids)
    pipeline = IngestPipeline(graph, execution_context=context)
    scope = BitrixDealScopeRepository(graph)
    cursor = context.checkpoint.cursor.get("last_known_deal_id")
    last_known = int(cursor) if isinstance(cursor, str) and cursor.isdigit() else None
    if context.max_rows is not None and len(membership.deal_ids) > context.max_rows:
        raise RuntimeError("known owner refresh row ceiling was exceeded")
    pending_ids = [
        int(deal_id)
        for deal_id in membership.deal_ids
        if last_known is None or int(deal_id) > last_known
    ]
    refreshed = moved = missing = unresolved = 0
    try:
        for offset in range(0, len(pending_ids), 50):
            _assert_refresh_runtime(context)
            numeric_ids = pending_ids[offset : offset + 50]
            deals = source.get_deals_or_none(numeric_ids)
            if set(deals) != set(numeric_ids):
                raise RuntimeError("known owner batch did not account for every requested deal")
            for numeric_id in numeric_ids:
                _assert_refresh_runtime(context)
                deal_id = str(numeric_id)
                deal = deals[numeric_id]
                absence = 0
                if deal is None:
                    deal, absence = _confirm_missing_deal(
                        source,
                        scope,
                        deal_id,
                        numeric_id,
                        context,
                    )
                if deal is None:
                    missing += 1
                    unresolved += 1
                    raise RuntimeError(
                        f"known owner {deal_id} is absent after {absence} healthy observations "
                        "and requires reviewed quarantine"
                    )
                category_id = deal.category_id
                if category_id is None:
                    raise RuntimeError("known owner refresh returned a deal without category")
                if category_id in included:
                    entity_key = entity_by_category_id.get(category_id)
                    if entity_key is None:
                        raise RuntimeError("known owner refresh category has no entity mapping")
                    envelope = SourceRecordEnvelope.model_validate(
                        {"source_system": "bitrix_chat", **_deal_envelope(deal, entity_key)}
                    )
                    pipeline.ingest(envelope, ingest_run_id=context.fence_context.ingest_run_id)
                    refreshed += 1
                    continue
                _record_out_of_scope(graph, deal, context)
                moved += 1
    finally:
        source.close()
    return KnownOwnerRefreshSummary(
        refreshed,
        moved,
        missing,
        unresolved,
        source.request_count,
    )


def _assert_refresh_runtime(context: ExecutionContext) -> None:
    deadline = context.deadline_monotonic
    if deadline is not None and time.monotonic() >= deadline:
        raise RuntimeError("known owner refresh runtime ceiling reached before the next write")


def _confirm_missing_deal(
    source: KnownOwnerClient,
    scope: KnownOwnerScope,
    deal_id: str,
    numeric_id: int,
    context: ExecutionContext,
) -> tuple[CrmDeal | None, int]:
    """Confirm a healthy batch miss once more before making it indeterminate."""
    _assert_refresh_runtime(context)
    streak, current = scope.record_healthy_not_found(
        deal_id,
        fence_context=context.fence_context,
    )
    if _absence_is_confirmed(streak, current):
        return None, streak
    deal = source.get_deal_or_none(numeric_id)
    if deal is not None:
        return deal, streak
    _assert_refresh_runtime(context)
    streak, current = scope.record_healthy_not_found(
        deal_id,
        fence_context=context.fence_context,
    )
    if not _absence_is_confirmed(streak, current):
        raise RuntimeError("second healthy deal absence did not become indeterminate")
    return None, streak


def _get_deal_with_absence_confirmation(
    source: KnownOwnerClient,
    scope: KnownOwnerScope,
    deal_id: str,
    numeric_id: int,
    context: ExecutionContext,
) -> tuple[CrmDeal | None, int]:
    """Require two healthy misses before returning an unresolved absence."""
    deal = source.get_deal_or_none(numeric_id)
    if deal is not None:
        return deal, 0
    _assert_refresh_runtime(context)
    streak, current = scope.record_healthy_not_found(
        deal_id,
        fence_context=context.fence_context,
    )
    if _absence_is_confirmed(streak, current):
        return None, streak
    deal = source.get_deal_or_none(numeric_id)
    if deal is not None:
        return deal, streak
    _assert_refresh_runtime(context)
    streak, current = scope.record_healthy_not_found(
        deal_id,
        fence_context=context.fence_context,
    )
    if not _absence_is_confirmed(streak, current):
        raise RuntimeError("second healthy deal absence did not become indeterminate")
    return None, streak


def _absence_is_confirmed(streak: int, current: CurrentDealScope) -> bool:
    return streak >= 2 or current.scope_state == "indeterminate"


def _record_out_of_scope(
    graph: Neo4jClient,
    deal: CrmDeal,
    context: ExecutionContext,
) -> None:
    if deal.category_id is None:
        raise RuntimeError("out-of-scope deal omitted category")
    payload = dict(deal.raw_payload)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    record_hash = "sha256:" + hashlib.sha256(encoded).hexdigest()
    envelope = SourceRecordEnvelope.model_validate(
        {
            "source_system": "bitrix_chat",
            "source_record_id": f"bitrix-crm-deal-{deal.id}",
            "record_type": "crm_deal",
            "ingest_type": "api_incremental",
            "observed_at": deal.observed_at,
            "record_hash": record_hash,
            "raw_payload": payload,
        }
    )

    def _work(tx: ManagedTransaction) -> None:
        record_scope_batch_in_transaction(
            tx,
            [
                DealScopeObservation(
                    deal_id=deal.id,
                    scope_state="out_of_scope",
                    category_id=deal.category_id,
                )
            ],
            fence_context=context.fence_context,
        )
        result = IngestResult(source_record_id=envelope.source_record_id, dropped=True)
        record_terminal_unit(
            tx,
            context=context,
            envelope=envelope,
            result=result,
            disposition="excluded_out_of_scope",
            scope_state="out_of_scope",
        )

    graph.execute_write(_work)
