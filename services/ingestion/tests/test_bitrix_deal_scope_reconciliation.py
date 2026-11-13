"""Known-owner absence confirmation remains bounded and restart-safe."""

from __future__ import annotations

import json
from collections.abc import Callable, Collection
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone
from typing import TypeVar, cast
from unittest.mock import Mock

import pytest
from neo4j import ManagedTransaction
from pytest import MonkeyPatch
from src import bitrix_deal_scope_reconciliation as reconciliation
from src.bitrix_backfill_models import GenerationRunContext, KnownOwnerMembershipSet
from src.bitrix_backfill_runtime import record_terminal_unit
from src.bitrix_deal_scope_reconciliation import (
    _get_deal_with_absence_confirmation,
    refresh_known_owner_set,
)
from src.bitrix_ingestion_models import DealScopeState, ExecutionContext, FenceContext
from src.connectors.bitrix_openlines.connector import build_crm_deal_envelope
from src.connectors.bitrix_openlines.models import CrmContact, CrmDeal
from src.graph.bitrix_deal_scope import CurrentDealScope
from src.graph.client import Neo4jClient
from src.graph.queries.ingest_skips import RECORD_INGEST_REFRESH_UNIT, WRITE_INGEST_SKIPS
from src.models import IngestResult, RecordType, SourceRecordEnvelope
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


def _generation_context() -> ExecutionContext:
    base = _context()
    return ExecutionContext(
        worker_task_id=base.worker_task_id,
        fence_context=base.fence_context,
        checkpoint=base.checkpoint,
        generation_context=GenerationRunContext(
            generation_id="generation-1",
            boundary_digest="sha256:boundary",
            configuration_digest="sha256:config",
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


T = TypeVar("T")


class _RecordingResult:
    """Result double that reports one successful fenced statement."""

    def __init__(self, record: dict[str, object]) -> None:
        self._record = record

    def single(self) -> dict[str, object]:
        return self._record


class _RecordingTx:
    def __init__(self) -> None:
        self.runs: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **kwargs: object) -> _RecordingResult:
        self.runs.append((query, kwargs))
        return _RecordingResult({"stop_requested": False})


class _WriteThroughGraph:
    def __init__(self) -> None:
        self.tx = _RecordingTx()

    def execute_write(self, work: Callable[[ManagedTransaction], T]) -> T:
        return work(cast(ManagedTransaction, self.tx))


@dataclass
class _OutOfScopeClient:
    deal: CrmDeal
    closed: bool = False
    calls: int = 0

    @property
    def request_count(self) -> int:
        return self.calls

    def get_deals_or_none(self, deal_ids: Collection[int]) -> dict[int, CrmDeal | None]:
        assert tuple(deal_ids) == (7,)
        self.calls += 1
        return {7: self.deal}

    def get_deal_or_none(self, deal_id: int) -> CrmDeal | None:
        raise AssertionError(f"healthy batched owner {deal_id} must not be fetched directly")

    def close(self) -> None:
        self.closed = True


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


@pytest.mark.parametrize(
    ("observed_at", "expected_observed_at"),
    [
        (
            datetime(2026, 8, 20, 2, 30, tzinfo=timezone(timedelta(hours=8))),
            "2026-08-20T02:30:00+08:00",
        ),
        (None, None),
    ],
)
def test_known_owner_refresh_serializes_out_of_scope_observed_at(
    monkeypatch: MonkeyPatch,
    observed_at: datetime | None,
    expected_observed_at: str | None,
) -> None:
    deal = replace(
        _deal(7),
        category_id="99",
        stage_id="C99:NEW",
        observed_at=observed_at,
        raw_payload={"ID": "7", "CATEGORY_ID": "99", "STAGE_ID": "C99:NEW"},
    )
    client = _OutOfScopeClient(deal)
    graph = _WriteThroughGraph()
    scope_recorder = Mock(return_value={})
    terminal_recorder = Mock()

    _Pipeline.ingested.clear()
    monkeypatch.setattr(reconciliation, "IngestPipeline", _Pipeline)
    monkeypatch.setattr(reconciliation, "record_scope_batch_in_transaction", scope_recorder)
    monkeypatch.setattr(reconciliation, "record_terminal_unit", terminal_recorder)

    context = _context()
    summary = refresh_known_owner_set(
        client,
        cast(Neo4jClient, graph),
        membership=KnownOwnerMembershipSet(
            generation_id="generation-1",
            membership_set_id="owners-1",
            digest="sha256:owners",
            deal_ids=("7",),
        ),
        context=context,
        included_category_ids=["2"],
        entity_by_category_id={"2": "eko"},
        source_instance_id=None,
    )

    [(skip_query, skip_params)] = graph.tx.runs
    assert skip_query == WRITE_INGEST_SKIPS
    assert skip_params["control_instance_id"] == context.fence_context.control_instance_id
    [skip] = cast(list[dict[str, object]], skip_params["rows"])
    assert (skip["stage"], skip["reason"], skip["source_record_id"]) == (
        "pipeline",
        "excluded_out_of_scope",
        "bitrix-crm-deal-7",
    )
    assert skip["ingest_run_id"] == context.fence_context.ingest_run_id
    assert skip["attempt_key"] == ""

    scope_recorder.assert_called_once()
    scope_call = scope_recorder.call_args
    observation = scope_call.args[1][0]
    assert (observation.deal_id, observation.scope_state, observation.category_id) == (
        "7",
        "out_of_scope",
        "99",
    )
    assert scope_call.kwargs == {"fence_context": context.fence_context}

    terminal_recorder.assert_called_once()
    terminal_call = terminal_recorder.call_args
    envelope = terminal_call.kwargs["envelope"]
    result = terminal_call.kwargs["result"]
    assert envelope.observed_at == expected_observed_at
    assert envelope.source_record_id == "bitrix-crm-deal-7"
    assert envelope.record_type == RecordType.CRM_DEAL
    assert result.excluded is True
    assert result.skip_reason == "excluded_out_of_scope"
    assert terminal_call.kwargs["context"] == context
    assert terminal_call.kwargs["disposition"] == "excluded_out_of_scope"
    assert terminal_call.kwargs["scope_state"] == "out_of_scope"

    assert summary.refreshed == 0
    assert summary.moved_out_of_scope == 1
    assert summary.missing_candidates == 0
    assert summary.unresolved == 0
    assert summary.http_request_count == 1
    assert _Pipeline.ingested == []
    assert client.closed is True


def test_known_owner_refresh_batches_frozen_membership_in_source_order(
    monkeypatch: MonkeyPatch,
) -> None:
    client = _BatchClient(chunks=[])
    _Pipeline.ingested.clear()
    observed_scopes: list[str | None] = []
    exact_builder = reconciliation._deal_envelope

    def scoped_builder(
        deal: CrmDeal,
        entity_key: str,
        *,
        source_instance_id: str | None,
    ) -> dict[str, object]:
        observed_scopes.append(source_instance_id)
        return cast(
            "dict[str, object]",
            exact_builder(
                deal,
                entity_key,
                source_instance_id=source_instance_id,
            ),
        )

    monkeypatch.setattr(reconciliation, "IngestPipeline", _Pipeline)
    monkeypatch.setattr(reconciliation, "_deal_envelope", scoped_builder)
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
        source_instance_id="bitrix-primary",
    )

    assert [len(chunk) for chunk in client.chunks] == [50, 50, 20]
    assert observed_scopes == ["bitrix-primary"] * 120
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
        source_instance_id=None,
    )

    assert client.chunks == [tuple(range(76, 121))]
    assert _Pipeline.ingested == [f"bitrix-crm-deal-{value}" for value in range(76, 121)]
    assert summary.refreshed == 45


def test_known_owner_refresh_rejects_oversized_membership_before_source_calls(
    monkeypatch: MonkeyPatch,
) -> None:
    client = _BatchClient(chunks=[])
    monkeypatch.setattr(reconciliation, "IngestPipeline", _Pipeline)
    monkeypatch.setattr(reconciliation, "BitrixDealScopeRepository", lambda _graph: object())
    membership = KnownOwnerMembershipSet(
        generation_id="generation-1",
        membership_set_id="owners-1",
        digest="sha256:owners",
        deal_ids=("1", "2"),
    )
    base = _context()
    context = ExecutionContext(
        worker_task_id=base.worker_task_id,
        fence_context=base.fence_context,
        checkpoint=base.checkpoint,
        max_rows=1,
    )

    with pytest.raises(RuntimeError, match="row ceiling"):
        refresh_known_owner_set(
            client,
            cast(Neo4jClient, object()),
            membership=membership,
            context=context,
            included_category_ids=["2"],
            entity_by_category_id={"2": "eko"},
            source_instance_id=None,
        )

    assert client.chunks == []


def test_known_owner_refresh_rechecks_runtime_after_batch_before_writes(
    monkeypatch: MonkeyPatch,
) -> None:
    client = _BatchClient(chunks=[])
    _Pipeline.ingested.clear()
    monotonic_values = iter((9.0, 11.0))
    monkeypatch.setattr(reconciliation, "IngestPipeline", _Pipeline)
    monkeypatch.setattr(reconciliation, "BitrixDealScopeRepository", lambda _graph: object())
    monkeypatch.setattr(reconciliation.time, "monotonic", lambda: next(monotonic_values))
    membership = KnownOwnerMembershipSet(
        generation_id="generation-1",
        membership_set_id="owners-1",
        digest="sha256:owners",
        deal_ids=("1",),
    )
    base = _context()
    context = ExecutionContext(
        worker_task_id=base.worker_task_id,
        fence_context=base.fence_context,
        checkpoint=base.checkpoint,
        deadline_monotonic=10.0,
    )

    with pytest.raises(RuntimeError, match="runtime ceiling"):
        refresh_known_owner_set(
            client,
            cast(Neo4jClient, object()),
            membership=membership,
            context=context,
            included_category_ids=["2"],
            entity_by_category_id={"2": "eko"},
            source_instance_id=None,
        )

    assert client.chunks == [(1,)]
    assert _Pipeline.ingested == []


class _MixedScopeClient:
    """Client that returns one in-scope and one moved-out-of-scope known owner."""

    def __init__(self, deals: dict[int, CrmDeal]) -> None:
        self._deals = deals
        self.closed = False

    @property
    def request_count(self) -> int:
        return 1

    def get_deals_or_none(self, deal_ids: Collection[int]) -> dict[int, CrmDeal | None]:
        return {deal_id: self._deals.get(deal_id) for deal_id in deal_ids}

    def get_deal_or_none(self, deal_id: int) -> CrmDeal | None:
        return self._deals.get(deal_id)

    def close(self) -> None:
        self.closed = True


class _AccountingPipeline:
    """Pipeline double that accounts its terminal unit inside the unit transaction."""

    ingested: list[str] = []
    accounting: list[dict[str, object]] = []

    def __init__(self, _graph: object, *, execution_context: ExecutionContext) -> None:
        self._context = execution_context

    def ingest(self, envelope: SourceRecordEnvelope, *, ingest_run_id: str) -> None:
        tx = _RecordingTx()
        record_terminal_unit(
            cast(ManagedTransaction, tx),
            context=self._context,
            envelope=envelope,
            result=IngestResult(source_record_id=envelope.source_record_id),
        )
        _AccountingPipeline.ingested.append(envelope.source_record_id)
        _AccountingPipeline.accounting.extend(
            params for query, params in tx.runs if query == RECORD_INGEST_REFRESH_UNIT
        )


def test_known_owner_refresh_accounts_in_scope_and_moved_out_of_scope_units(
    monkeypatch: MonkeyPatch,
) -> None:
    moved = replace(
        _deal(9),
        category_id="99",
        stage_id="C99:NEW",
        raw_payload={"ID": "9", "CATEGORY_ID": "99", "STAGE_ID": "C99:NEW"},
    )
    client = _MixedScopeClient({7: _deal(7), 9: moved})
    graph = _WriteThroughGraph()
    _AccountingPipeline.ingested.clear()
    _AccountingPipeline.accounting.clear()
    monkeypatch.setattr(reconciliation, "IngestPipeline", _AccountingPipeline)
    monkeypatch.setattr(reconciliation, "record_scope_batch_in_transaction", Mock(return_value={}))
    monkeypatch.setattr(reconciliation, "BitrixDealScopeRepository", lambda _graph: object())

    summary = refresh_known_owner_set(
        client,
        cast(Neo4jClient, graph),
        membership=KnownOwnerMembershipSet(
            generation_id="generation-1",
            membership_set_id="owners-1",
            digest="sha256:owners",
            deal_ids=("7", "9"),
        ),
        context=_generation_context(),
        included_category_ids=["2"],
        entity_by_category_id={"2": "eko"},
        source_instance_id=None,
    )

    assert summary.refreshed == 1
    assert summary.moved_out_of_scope == 1
    assert _AccountingPipeline.ingested == ["bitrix-crm-deal-7"]

    out_of_scope_accounting = [
        params for query, params in graph.tx.runs if query == RECORD_INGEST_REFRESH_UNIT
    ]
    assert len(out_of_scope_accounting) == 1
    accounted = {
        cast(str, params["source_identity"]): params
        for params in [*_AccountingPipeline.accounting, *out_of_scope_accounting]
    }
    assert set(accounted) == {"bitrix-crm-deal-7", "bitrix-crm-deal-9"}
    assert accounted["bitrix-crm-deal-7"]["persisted_count"] == 1
    assert accounted["bitrix-crm-deal-9"]["pipeline_skip_count"] == 1
    assert accounted["bitrix-crm-deal-9"]["attempt_key"] == "known_owner_refresh_v1"

    skip_statements = [params for query, params in graph.tx.runs if query == WRITE_INGEST_SKIPS]
    assert len(skip_statements) == 1
    [skip] = cast(list[dict[str, object]], skip_statements[0]["rows"])
    assert skip["attempt_key"] == "known_owner_refresh_v1"
    assert skip["source_record_id"] == "bitrix-crm-deal-9"


def test_legacy_crm_deal_identity_v2_payload_and_hash_are_fixed() -> None:
    contact = CrmContact(
        id="42",
        full_name="Ada Lovelace",
        phones=("+65 5555 0101",),
        emails=("ada@example.com",),
    )
    deal = CrmDeal(
        id="7",
        title="Renewal",
        category_id="2",
        stage_id="C2:NEW",
        observed_at=datetime(2026, 8, 27, 12, 0, tzinfo=UTC),
        primary_contact=contact,
        contacts=(contact,),
        contact_count=1,
        has_ambiguous_contacts=False,
        raw_payload={
            "ID": "7",
            "TITLE": "Renewal",
            "CATEGORY_ID": "2",
            "STAGE_ID": "C2:NEW",
        },
    )

    envelope = build_crm_deal_envelope(deal, "entity-1", source_instance_id=None)

    assert envelope["identity_link_key"] == "bitrix:legacy:deal:7"
    assert (
        envelope["record_hash"]
        == "25a4dcc9f60107ae349fa8944813d01273931be8a285aa5ed19d1e4977c1b6eb"
    )
    expected_raw_payload = {
        "crm_deal_id": "7",
        "title": "Renewal",
        "category_id": "2",
        "stage_id": "C2:NEW",
        "primary_contact_id": "42",
        "primary_contact_kind": "contact",
        "contact_count": 1,
        "crm_contact_groups": [
            [
                {"type": "crm_contact_id", "value": "42", "is_verified": True},
                {"type": "phone", "value": "+65 5555 0101", "is_verified": False},
                {"type": "email", "value": "ada@example.com", "is_verified": False},
            ]
        ],
        "crm_contact_raw_groups": [
            [
                {"type": "crm_contact_id", "value": "42", "is_verified": True},
                {"type": "phone", "value": "+65 5555 0101", "is_verified": False},
                {"type": "email", "value": "ada@example.com", "is_verified": False},
            ]
        ],
        "crm_contact_ids": ["42"],
        "crm_contact_resolution_required": False,
        "crm_deal_identity_policy_version": "crm_deal_identity_v2",
        "crm_contact_identity_metadata": [
            {
                "identity_policy_version": "crm_deal_identity_v2",
                "crm_contact_id": "42",
                "crm_contact_kind": "contact",
                "phone_count": 1,
                "email_count": 1,
                "max_phone_count": 5,
                "max_email_count": 5,
                "channel_hints_suppressed": False,
                "channel_hint_suppression_reasons": [],
            }
        ],
        "deal": {
            "ID": "7",
            "TITLE": "Renewal",
            "CATEGORY_ID": "2",
            "STAGE_ID": "C2:NEW",
        },
    }
    assert envelope["raw_payload"] == expected_raw_payload
    assert "control_instance_id" not in json.dumps(envelope, sort_keys=True)
