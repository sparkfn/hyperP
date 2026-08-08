"""Known-owner absence confirmation remains bounded and restart-safe."""

from __future__ import annotations

from dataclasses import dataclass

from src.bitrix_deal_scope_reconciliation import _get_deal_with_absence_confirmation
from src.bitrix_ingestion_models import DealScopeState, ExecutionContext, FenceContext
from src.connectors.bitrix_openlines.models import CrmDeal
from src.graph.bitrix_deal_scope import CurrentDealScope
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
