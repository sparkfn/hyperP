"""Known-owner absence confirmation remains bounded and restart-safe."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from pytest import MonkeyPatch
from src import bitrix_deal_scope_reconciliation as reconciliation
from src.bitrix_backfill_models import KnownOwnerMembershipSet
from src.bitrix_deal_scope_reconciliation import (
    _get_deal_with_absence_confirmation,
    refresh_known_owner_set,
)
from src.bitrix_ingestion_models import DealScopeState, ExecutionContext, FenceContext
from src.connectors.bitrix_openlines.models import CrmDeal
from src.graph.bitrix_deal_scope import CurrentDealScope
from src.graph.client import Neo4jClient
from src.models import SourceRecordEnvelope
from src.resumable import CheckpointDescriptor


@dataclass
class _MissingClient:
    calls: int = 0

    def get_deal_or_none(self, deal_id: int) -> CrmDeal | None:
        assert deal_id == 7
        self.calls += 1
        return None

    def close(self) -> None:
        pass


@dataclass
class _Scope:
    calls: int = 0

    def record_healthy_not_found(
        self,
        deal_id: str,
        *,
        fence_context: FenceContext,
    ) -> tuple[int, CurrentDealScope]:
        assert deal_id == "7"
        assert fence_context.fencing_token == 1
        self.calls += 1
        state: DealScopeState = "indeterminate" if self.calls >= 2 else "in_scope"
        return self.calls, CurrentDealScope(
            deal_id="7",
            scope_sequence=self.calls,
            scope_state=state,
            entity_key="eko" if state == "in_scope" else None,
            category_id="2",
            source_record_pk="source-7",
        )


def _context() -> ExecutionContext:
    return ExecutionContext(
        worker_task_id="task-1",
        fence_context=FenceContext(
            logical_run_id="logical-1",
            ingest_run_id="ingest-1",
            source_key="bitrix_chat",
            stream_key="crm_deals",
            stream_generation=1,
            fencing_token=1,
            attempt_generation=1,
        ),
        checkpoint=CheckpointDescriptor(
            phase="known_owner_refresh_v1",
            cursor={"last_known_deal_id": None, "census_epoch": 1},
            source_window={
                "known_owner_membership_set_id": "owners-1",
                "known_owner_set_digest": "sha256:owners",
                "known_owner_count": 1,
            },
            last_committed_record_id=None,
            connector_version="bitrix-crm-known-owner-refresh-v1",
            schema_version=1,
            replay_boundary="exclusive_sorted_known_deal_id",
        ),
    )


def test_absence_requires_two_independent_healthy_targeted_reads() -> None:
    client = _MissingClient()
    scope = _Scope()

    deal, streak = _get_deal_with_absence_confirmation(
        client,
        scope,
        "7",
        7,
        _context(),
    )

    assert deal is None
    assert streak == 2
    assert client.calls == 2
    assert scope.calls == 2


@dataclass
class _BatchClient:
    chunks: list[tuple[int, ...]]
    closed: bool = False

    @property
    def request_count(self) -> int:
        return len(self.chunks) * 2

    def get_deals_or_none(self, deal_ids: list[int]) -> dict[int, CrmDeal | None]:
        self.chunks.append(tuple(deal_ids))
        return {deal_id: _deal(deal_id) for deal_id in deal_ids}

    def get_deal_or_none(self, deal_id: int) -> CrmDeal | None:
        raise AssertionError(f"healthy batched owner {deal_id} must not be fetched directly")

    def close(self) -> None:
        self.closed = True


class _Pipeline:
    ingested: list[str] = []

    def __init__(self, _graph: object, *, execution_context: ExecutionContext) -> None:
        assert execution_context.checkpoint.phase == "known_owner_refresh_v1"

    def ingest(self, envelope: SourceRecordEnvelope, *, ingest_run_id: str) -> None:
        assert ingest_run_id == "ingest-1"
        self.ingested.append(envelope.source_record_id)


def _deal(deal_id: int) -> CrmDeal:
    raw = {
        "ID": str(deal_id),
        "TITLE": f"Deal {deal_id}",
        "CATEGORY_ID": "2",
        "STAGE_ID": "C2:NEW",
    }
    return CrmDeal(
        id=str(deal_id),
        title=f"Deal {deal_id}",
        category_id="2",
        stage_id="C2:NEW",
        observed_at=datetime(2026, 8, 13, tzinfo=UTC),
        primary_contact=None,
        contacts=(),
        contact_count=0,
        has_ambiguous_contacts=False,
        raw_payload=raw,
    )


def test_known_owner_refresh_batches_frozen_membership_in_source_order(
    monkeypatch: MonkeyPatch,
) -> None:
    client = _BatchClient(chunks=[])
    _Pipeline.ingested.clear()
    monkeypatch.setattr(reconciliation, "IngestPipeline", _Pipeline)
    monkeypatch.setattr(reconciliation, "BitrixDealScopeRepository", lambda _graph: object())
    membership = KnownOwnerMembershipSet(
        generation_id="generation-1",
        membership_set_id="owners-1",
        digest="sha256:owners",
        deal_ids=tuple(str(value) for value in range(1, 121)),
    )

    summary = refresh_known_owner_set(
        client,
        cast(Neo4jClient, object()),
        membership=membership,
        context=_context(),
        included_category_ids=["2"],
        entity_by_category_id={"2": "eko"},
    )

    assert [len(chunk) for chunk in client.chunks] == [50, 50, 20]
    assert client.chunks[0][0] == 1
    assert client.chunks[-1][-1] == 120
    assert len(_Pipeline.ingested) == 120
    assert summary.refreshed == 120
    assert summary.moved_out_of_scope == 0
    assert summary.http_request_count == 6
    assert client.closed is True


def test_known_owner_refresh_resume_skips_checkpointed_members(
    monkeypatch: MonkeyPatch,
) -> None:
    client = _BatchClient(chunks=[])
    _Pipeline.ingested.clear()
    monkeypatch.setattr(reconciliation, "IngestPipeline", _Pipeline)
    monkeypatch.setattr(reconciliation, "BitrixDealScopeRepository", lambda _graph: object())
    membership = KnownOwnerMembershipSet(
        generation_id="generation-1",
        membership_set_id="owners-1",
        digest="sha256:owners",
        deal_ids=tuple(str(value) for value in range(1, 121)),
    )
    context = _context()
    context = ExecutionContext(
        worker_task_id=context.worker_task_id,
        fence_context=context.fence_context,
        checkpoint=CheckpointDescriptor(
            phase="known_owner_refresh_v1",
            cursor={"last_known_deal_id": "75", "census_epoch": 1},
            source_window=context.checkpoint.source_window,
            last_committed_record_id=context.checkpoint.last_committed_record_id,
            connector_version=context.checkpoint.connector_version,
            schema_version=context.checkpoint.schema_version,
            replay_boundary=context.checkpoint.replay_boundary,
        ),
    )

    summary = refresh_known_owner_set(
        client,
        cast(Neo4jClient, object()),
        membership=membership,
        context=context,
        included_category_ids=["2"],
        entity_by_category_id={"2": "eko"},
    )

    assert client.chunks == [tuple(range(76, 121))]
    assert _Pipeline.ingested == [f"bitrix-crm-deal-{value}" for value in range(76, 121)]
    assert summary.refreshed == 45
