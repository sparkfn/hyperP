"""Unit tests for the auto-discovered bounded Bitrix adapter (issue #432)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from _bitrix_bounded_fixtures import (
    ALL_CAPABILITIES,
    BOUNDED_BASE_URL,
    SNAPSHOT_ID,
    SOURCE_SCOPE,
    ProxyTransport,
    RevokedCancellation,
    capability_checkpoint,
    capability_response,
    context,
    conversations_page,
    deal_change,
    deal_payload,
    deals_page,
    source_window,
)
from pydantic.types import JsonValue
from src.bounded_ingestion_models import BoundedUnit, RunScope, Usage, utc_now
from src.connectors import chat_helpers
from src.connectors.bitrix_openlines import bounded_descriptor as descriptor_module
from src.connectors.bitrix_openlines.bounded_checkpoint import (
    BITRIX_BOUNDED_CONNECTOR_VERSION,
    BITRIX_BOUNDED_PHASE,
    advance_checkpoint,
)
from src.connectors.bitrix_openlines.bounded_client import BitrixBoundedClient
from src.connectors.bitrix_openlines.bounded_contract import (
    CAPABILITY_ENDPOINT,
    CHANGED_DEALS_ENDPOINT,
    BitrixBoundedCapabilityError,
    BitrixBoundedContractError,
)
from src.connectors.bitrix_openlines.bounded_descriptor import DESCRIPTOR
from src.connectors.bitrix_openlines.client import _assert_activity_request_retired
from src.connectors.bitrix_openlines.connector import _CrmEntityMappingError
from src.connectors.bitrix_openlines.discovery import discover_chats, stream_chats
from src.connectors.chat_helpers import run_extraction_batch_bounded
from src.ingestion_config import BitrixOpenLinesConfig, LlmConfig
from src.llm import base as base_module
from src.llm.base import ChatMessage, LlmCallAbortedError, LLMService
from src.resumable import CheckpointDescriptor, IngestionUnit

DEAL_CATEGORY = "1"
DEAL_ENTITY = "eko"


# --------------------------------------------------------------------------- #
# Descriptor registration / mode contract
# --------------------------------------------------------------------------- #


def test_descriptor_is_auto_discovered_for_bitrix_chat() -> None:
    from src.connectors.registry import registry

    assert "bitrix_chat" in registry.registered_sources()
    assert registry.get("bitrix_chat") is DESCRIPTOR


@pytest.mark.parametrize("mode", ["bootstrap", "delta"])
def test_descriptor_supports_bootstrap_and_delta(mode: str) -> None:
    from src.connectors.registry import registry

    selected = registry.require("bitrix_chat", mode)
    assert selected is DESCRIPTOR
    assert selected.source_key == "bitrix_chat"


def test_descriptor_rejects_one_time_mode() -> None:
    from src.connectors.registry import registry

    assert DESCRIPTOR.supports_one_time is False
    with pytest.raises(LookupError, match="does not support requested mode"):
        registry.require("bitrix_chat", "one_time")


def test_descriptor_declares_deadline_and_cancellation_support() -> None:
    assert DESCRIPTOR.supports_deadline is True
    assert DESCRIPTOR.supports_cancellation is True
    assert DESCRIPTOR.connector_version == BITRIX_BOUNDED_CONNECTOR_VERSION
    assert DESCRIPTOR.checkpoint_schema_version == 1
    assert DESCRIPTOR.max_source_requests_per_unit == 1
    assert DESCRIPTOR.max_extraction_calls_per_unit == 1
    assert DESCRIPTOR.checkpoint_schema_version >= 1


def test_initial_checkpoint_reserves_the_capability_substage() -> None:
    checkpoint = capability_checkpoint("crm_deals")

    assert checkpoint.phase == BITRIX_BOUNDED_PHASE
    assert checkpoint.cursor["substage"] == "capability"
    assert checkpoint.cursor["stream_key"] == "crm_deals"
    assert checkpoint.cursor["page_cursor"] is None
    assert checkpoint.connector_version == BITRIX_BOUNDED_CONNECTOR_VERSION
    assert checkpoint.schema_version == 1
    assert checkpoint.replay_boundary == "bitrix_bounded_unit"
    assert checkpoint.last_committed_record_id is None
    assert checkpoint.source_window == source_window("crm_deals")


def test_initial_checkpoint_rejects_one_time_mode() -> None:
    from _bitrix_bounded_fixtures import scope

    with pytest.raises(BitrixBoundedContractError, match="one-time mode"):
        descriptor_module.initial_checkpoint(scope("crm_deals", mode="one_time"), None)


@pytest.mark.parametrize("stream_key", ["crm_activities", "crm_deals_v2", ""])
def test_initial_checkpoint_rejects_unknown_streams(stream_key: str) -> None:
    with pytest.raises(BitrixBoundedContractError, match="stream is unsupported"):
        descriptor_module.initial_checkpoint(_scope(stream_key), None)


def _scope(stream_key: str) -> RunScope:
    """Build a scope carrying an arbitrary stream key for refusal assertions."""
    base = capability_checkpoint("crm_deals").source_window
    window: dict[str, JsonValue] = dict(base)
    window["stream_key"] = stream_key
    return RunScope(
        environment="test",
        reset_generation=1,
        source_key="bitrix_chat",
        control_instance_id="bitrix-control",
        entity_key=None,
        stream_key=stream_key,
        mode="delta",
        configuration_fingerprint="bitrix-fingerprint",
        connector_version=BITRIX_BOUNDED_CONNECTOR_VERSION,
        checkpoint_schema_version=1,
        source_window=window,
    )


# --------------------------------------------------------------------------- #
# Binding helpers
# --------------------------------------------------------------------------- #


def _bind(
    monkeypatch: pytest.MonkeyPatch,
    transport: ProxyTransport,
    *,
    config: BitrixOpenLinesConfig | None = None,
) -> ProxyTransport:
    """Point the descriptor at an in-memory proxy and a test category mapping."""
    selected = config or BitrixOpenLinesConfig(
        included_crm_category_ids=[DEAL_CATEGORY],
        entity_by_crm_category_id={DEAL_CATEGORY: DEAL_ENTITY},
    )
    monkeypatch.setattr(descriptor_module, "_bounded_base_url", lambda: BOUNDED_BASE_URL)
    monkeypatch.setattr(
        descriptor_module,
        "BitrixBoundedClient",
        lambda **_kwargs: transport.client(),
    )
    monkeypatch.setattr(
        descriptor_module,
        "get_ingestion_config",
        lambda: _StubConfig(selected),
    )
    return transport


@dataclass
class _StubConfig:
    bitrix_openlines: BitrixOpenLinesConfig


def _deals_checkpoint(*, page_cursor: str | None = None) -> CheckpointDescriptor:
    return advance_checkpoint(
        capability_checkpoint("crm_deals"),
        substage="deals",
        page_cursor=page_cursor,
    )


def _fetch(
    checkpoint: CheckpointDescriptor,
    *,
    run_scope: RunScope | None = None,
    cancellation: object | None = None,
) -> BoundedUnit:
    selected = run_scope or _deals_scope()
    attempt = context(checkpoint, selected, cancellation=cancellation)
    connector = DESCRIPTOR.create(attempt)
    try:
        return connector.fetch_one_unit(checkpoint, attempt)
    finally:
        connector.close()


def _deals_scope() -> RunScope:
    from _bitrix_bounded_fixtures import scope

    return scope("crm_deals")


# --------------------------------------------------------------------------- #
# Capability gate
# --------------------------------------------------------------------------- #


def test_capability_unit_makes_one_request_and_advances_to_deals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _bind(monkeypatch, ProxyTransport(capabilities=capability_response()))
    checkpoint = capability_checkpoint("crm_deals")

    unit = _fetch(checkpoint)

    assert transport.endpoints() == [CAPABILITY_ENDPOINT]
    assert unit.unit.records == ()
    assert unit.unit.checkpoint_after.cursor["substage"] == "deals"
    assert unit.unit.checkpoint_after.cursor["page_cursor"] is None
    assert unit.usage.source_requests == 1
    assert unit.usage.records == 0
    assert unit.terminal is False


def test_capability_unit_sends_the_frozen_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _bind(monkeypatch, ProxyTransport(capabilities=capability_response()))

    _fetch(capability_checkpoint("crm_deals"))

    assert transport.calls[0].body == {
        "source_scope": SOURCE_SCOPE,
        "snapshot_id": SNAPSHOT_ID,
    }


def test_missing_stream_capability_is_rejected_before_any_deal_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _bind(
        monkeypatch,
        ProxyTransport(
            capabilities=capability_response(capabilities=("changed_conversations_v1",)),
            deals_pages=[deals_page([])],
        ),
    )

    with pytest.raises(BitrixBoundedCapabilityError, match="capability missing"):
        _fetch(capability_checkpoint("crm_deals"))

    assert transport.endpoints() == [CAPABILITY_ENDPOINT]


def test_capability_scope_mismatch_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(
        monkeypatch,
        ProxyTransport(capabilities=capability_response(source_scope="another-scope")),
    )

    with pytest.raises(BitrixBoundedCapabilityError, match="source scope mismatch"):
        _fetch(capability_checkpoint("crm_deals"))


def test_capability_snapshot_mismatch_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(
        monkeypatch,
        ProxyTransport(capabilities=capability_response(snapshot_id="another-snapshot")),
    )

    with pytest.raises(BitrixBoundedCapabilityError, match="snapshot mismatch"):
        _fetch(capability_checkpoint("crm_deals"))


# --------------------------------------------------------------------------- #
# Deal stream
# --------------------------------------------------------------------------- #


def _contact(contact_id: int = 11) -> dict[str, JsonValue]:
    return {
        "ID": contact_id,
        "FULL_NAME": "Ada Lovelace",
        "PHONES": ["+6591234567"],
        "EMAILS": ["ada@example.test"],
    }


def test_deal_page_decodes_an_upsert_and_a_tombstone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _bind(
        monkeypatch,
        ProxyTransport(
            deals_pages=[
                deals_page(
                    [
                        deal_change(
                            7,
                            change_version=1,
                            payload=deal_payload(7, contacts=(_contact(),), contact_id="11"),
                        ),
                        deal_change(
                            8,
                            change_version=2,
                            kind="tombstone",
                            category_id=None,
                            payload=None,
                        ),
                    ]
                )
            ]
        ),
    )

    unit = _fetch(_deals_checkpoint())

    assert transport.endpoints() == [CHANGED_DEALS_ENDPOINT]
    assert len(unit.unit.records) == 2
    upsert, retire = unit.unit.records
    assert upsert["bounded_op"] == "upsert"
    envelope = upsert["envelope"]
    assert isinstance(envelope, dict)
    assert envelope["source_record_id"] == "bitrix-crm-deal-7"
    assert envelope["entity_key"] == DEAL_ENTITY
    assert envelope["record_type"] == "crm_deal"
    assert retire["bounded_op"] == "retire"
    assert retire["source_record_id"] == "bitrix-crm-deal-8"
    assert isinstance(retire["retired_at"], str)
    assert unit.usage.records == 2
    assert unit.usage.source_requests == 1


def test_deal_unit_is_terminal_when_the_cursor_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(monkeypatch, ProxyTransport(deals_pages=[deals_page([])]))

    unit = _fetch(_deals_checkpoint())

    assert unit.terminal is True
    assert unit.unit.checkpoint_after.cursor["substage"] == "terminal"
    assert unit.unit.checkpoint_after.cursor["page_cursor"] is None


def test_deal_unit_forwards_and_advances_the_page_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _bind(
        monkeypatch,
        ProxyTransport(
            deals_pages=[
                deals_page([], next_cursor="cursor-2"),
                deals_page([]),
            ]
        ),
    )

    first = _fetch(_deals_checkpoint(page_cursor="cursor-1"))
    assert first.terminal is False
    assert first.unit.checkpoint_after.cursor["page_cursor"] == "cursor-2"
    assert transport.calls[0].body["cursor"] == "cursor-1"

    second = _fetch(first.unit.checkpoint_after)
    assert transport.calls[1].body["cursor"] == "cursor-2"
    assert second.terminal is True


def test_deal_upsert_outside_the_category_filter_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(
        monkeypatch,
        ProxyTransport(
            deals_pages=[
                deals_page(
                    [
                        deal_change(
                            9,
                            change_version=1,
                            category_id="2",
                            payload=deal_payload(9, category_id="2"),
                        )
                    ]
                )
            ]
        ),
    )

    unit = _fetch(_deals_checkpoint())

    assert unit.unit.records == ()
    assert unit.usage.records == 0


def test_unmapped_included_category_is_refused_before_the_deal_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _bind(
        monkeypatch,
        ProxyTransport(deals_pages=[deals_page([])]),
        config=BitrixOpenLinesConfig(
            included_crm_category_ids=["1", "2"],
            entity_by_crm_category_id={"1": DEAL_ENTITY},
        ),
    )

    with pytest.raises(_CrmEntityMappingError):
        _fetch(_deals_checkpoint())

    assert transport.endpoints() == []


def test_deal_change_that_switches_category_during_hydration_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(
        monkeypatch,
        ProxyTransport(
            deals_pages=[
                deals_page(
                    [
                        deal_change(
                            7,
                            change_version=1,
                            category_id="1",
                            payload=deal_payload(7, category_id="2"),
                        )
                    ]
                )
            ]
        ),
    )

    with pytest.raises(BitrixBoundedContractError, match="changed category"):
        _fetch(_deals_checkpoint())


def test_deal_upsert_with_a_mismatched_payload_id_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(
        monkeypatch,
        ProxyTransport(
            deals_pages=[deals_page([deal_change(7, change_version=1, payload=deal_payload(8))])]
        ),
    )

    with pytest.raises(BitrixBoundedContractError, match="mismatched ID"):
        _fetch(_deals_checkpoint())


def test_deal_upsert_without_a_payload_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(
        monkeypatch,
        ProxyTransport(deals_pages=[deals_page([deal_change(7, change_version=1, payload=None)])]),
    )

    with pytest.raises(BitrixBoundedContractError, match="pinned payload"):
        _fetch(_deals_checkpoint())


def test_deal_upsert_without_a_category_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(
        monkeypatch,
        ProxyTransport(
            deals_pages=[deals_page([deal_change(7, change_version=1, category_id=None)])]
        ),
    )

    with pytest.raises(BitrixBoundedContractError, match="omitted its category"):
        _fetch(_deals_checkpoint())


# --------------------------------------------------------------------------- #
# Refusals for streams and substages without an approved transport
# --------------------------------------------------------------------------- #


def test_activity_stream_is_refused_before_source_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _bind(monkeypatch, ProxyTransport(capabilities=capability_response()))

    with pytest.raises(BitrixBoundedContractError, match="stream is unsupported"):
        _fetch(capability_checkpoint("crm_activities"))

    assert transport.endpoints() == []


def test_openlines_work_transport_is_refused_before_source_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _bind(
        monkeypatch,
        ProxyTransport(
            capabilities=capability_response(),
            conversations_pages=[conversations_page([])],
        ),
    )
    checkpoint = _fetch(capability_checkpoint("openlines_conversations")).unit.checkpoint_after

    with pytest.raises(BitrixBoundedCapabilityError, match="no approved work transport"):
        _fetch(checkpoint)

    assert transport.endpoints() == [CAPABILITY_ENDPOINT]


def test_stage_history_work_transport_is_refused_before_source_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _bind(monkeypatch, ProxyTransport(capabilities=capability_response()))
    checkpoint = _fetch(capability_checkpoint("crm_stage_history")).unit.checkpoint_after

    with pytest.raises(BitrixBoundedCapabilityError, match="no approved work transport"):
        _fetch(checkpoint)

    assert transport.endpoints() == [CAPABILITY_ENDPOINT]


def test_unsupported_deal_substage_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _bind(monkeypatch, ProxyTransport(deals_pages=[deals_page([])]))
    checkpoint = advance_checkpoint(
        capability_checkpoint("crm_deals"),
        substage="messages",
        page_cursor=None,
    )

    with pytest.raises(BitrixBoundedContractError, match="substage is unsupported"):
        _fetch(checkpoint)

    assert transport.endpoints() == []


def test_capability_checkpoint_cannot_be_reused_for_a_different_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _bind(monkeypatch, ProxyTransport(capabilities=capability_response()))
    checkpoint = capability_checkpoint("openlines_conversations")

    with pytest.raises(BitrixBoundedContractError, match="does not match the run scope"):
        DESCRIPTOR.create(context(checkpoint, _deals_scope()))

    assert transport.endpoints() == []


# --------------------------------------------------------------------------- #
# Deadline / cancellation
# --------------------------------------------------------------------------- #


def test_requested_cancellation_stops_before_source_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _bind(monkeypatch, ProxyTransport(capabilities=capability_response()))

    with pytest.raises(BitrixBoundedCapabilityError, match="cancelled"):
        _fetch(capability_checkpoint("crm_deals"), cancellation=RevokedCancellation())

    assert transport.endpoints() == []


def test_source_timeout_is_clamped_to_the_remaining_operation_budget() -> None:
    attempt = context(_deals_checkpoint(), _deals_scope())
    bounded = replace(attempt, operation_deadline_at=utc_now() + timedelta(seconds=8))

    assert descriptor_module._bounded_source_timeout(bounded) == pytest.approx(3.0, abs=0.5)


def test_source_timeout_defaults_without_an_operation_deadline() -> None:
    attempt = context(_deals_checkpoint(), _deals_scope())

    assert (
        descriptor_module._bounded_source_timeout(attempt)
        == descriptor_module._BOUNDED_TIMEOUT_SECONDS
    )


def test_connector_cancel_stops_the_next_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _bind(monkeypatch, ProxyTransport(capabilities=capability_response()))
    checkpoint = capability_checkpoint("crm_deals")
    attempt = context(checkpoint, _deals_scope())
    connector = DESCRIPTOR.create(attempt)
    connector.cancel()
    try:
        with pytest.raises(BitrixBoundedCapabilityError, match="cancelled"):
            connector.fetch_one_unit(checkpoint, attempt)
    finally:
        connector.close()

    assert transport.endpoints() == []


# --------------------------------------------------------------------------- #
# Writer
# --------------------------------------------------------------------------- #


class _FakeIngestResult:
    def __init__(self, *, skipped_duplicate: bool = False) -> None:
        self.skipped_duplicate = skipped_duplicate


class _RecordingPipeline:
    instances: list[_RecordingPipeline] = []

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.envelopes: list[object] = []
        _RecordingPipeline.instances.append(self)

    def ingest_in_transaction(
        self,
        _tx: object,
        envelope: object,
        *_args: object,
        **_kwargs: object,
    ) -> _FakeIngestResult:
        self.envelopes.append(envelope)
        return _FakeIngestResult()


def _bind_writer(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Replace the writer's graph dependencies with recorders."""
    _RecordingPipeline.instances = []
    retirements: list[dict[str, object]] = []
    monkeypatch.setattr(descriptor_module, "Neo4jClient", lambda *_a, **_k: object())
    monkeypatch.setattr(descriptor_module, "get_settings", lambda: object())
    monkeypatch.setattr(descriptor_module, "IngestPipeline", _RecordingPipeline)
    monkeypatch.setattr(
        descriptor_module,
        "retire_source_evidence_in_transaction",
        lambda _tx, source_system, source_record_id, retired_at, snapshot_at, **_kwargs: (
            retirements.append(
                {
                    "source_system": source_system,
                    "source_record_id": source_record_id,
                    "retired_at": retired_at,
                    "snapshot_at": snapshot_at,
                }
            )
            or 1
        ),
    )
    return retirements


def _apply(unit: BoundedUnit) -> tuple[object, ...]:
    attempt = context(
        unit.unit.checkpoint_before,
        _deals_scope(),
        with_fence=True,
    )
    result = DESCRIPTOR.writer.apply(object(), attempt, unit)  # type: ignore[arg-type]
    return result.dispositions


def test_writer_applies_an_upsert_through_the_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _bind(
        monkeypatch,
        ProxyTransport(
            deals_pages=[
                deals_page(
                    [
                        deal_change(
                            7,
                            change_version=1,
                            payload=deal_payload(7, contacts=(_contact(),), contact_id="11"),
                        )
                    ]
                )
            ]
        ),
    )
    retirements = _bind_writer(monkeypatch)
    unit = _fetch(_deals_checkpoint())

    dispositions = _apply(unit)

    assert dispositions == ("committed",)
    assert retirements == []
    pipeline = _RecordingPipeline.instances[-1]
    assert len(pipeline.envelopes) == 1
    envelope = pipeline.envelopes[0]
    assert getattr(envelope, "source_record_id", None) == "bitrix-crm-deal-7"
    assert transport.endpoints() == [CHANGED_DEALS_ENDPOINT]


def test_writer_reports_a_duplicate_upsert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(
        monkeypatch,
        ProxyTransport(
            deals_pages=[deals_page([deal_change(7, change_version=1, payload=deal_payload(7))])]
        ),
    )
    _bind_writer(monkeypatch)
    monkeypatch.setattr(
        descriptor_module.IngestPipeline,  # type: ignore[attr-defined]
        "ingest_in_transaction",
        lambda _self, _tx, _envelope, *_a, **_k: _FakeIngestResult(skipped_duplicate=True),
    )
    unit = _fetch(_deals_checkpoint())

    assert _apply(unit) == ("duplicate",)


def test_writer_retires_evidence_for_a_tombstone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(
        monkeypatch,
        ProxyTransport(
            deals_pages=[
                deals_page(
                    [
                        deal_change(
                            8,
                            change_version=1,
                            kind="tombstone",
                            category_id=None,
                            payload=None,
                        )
                    ]
                )
            ]
        ),
    )
    retirements = _bind_writer(monkeypatch)
    unit = _fetch(_deals_checkpoint())

    dispositions = _apply(unit)

    assert dispositions == ("committed",)
    assert len(retirements) == 1
    assert retirements[0]["source_system"] == "bitrix_chat"
    assert retirements[0]["source_record_id"] == "bitrix-crm-deal-8"
    assert retirements[0]["retired_at"]
    assert retirements[0]["snapshot_at"] == retirements[0]["retired_at"]


def test_writer_reports_an_excluded_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(
        monkeypatch,
        ProxyTransport(
            deals_pages=[
                deals_page(
                    [
                        deal_change(
                            8,
                            change_version=1,
                            kind="tombstone",
                            category_id=None,
                            payload=None,
                        )
                    ]
                )
            ]
        ),
    )
    _bind_writer(monkeypatch)
    monkeypatch.setattr(
        descriptor_module,
        "retire_source_evidence_in_transaction",
        lambda *_a, **_k: 0,
    )
    unit = _fetch(_deals_checkpoint())

    assert _apply(unit) == ("excluded",)


def test_writer_requires_a_fence_context(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind_writer(monkeypatch)
    checkpoint = _deals_checkpoint()
    unit = BoundedUnit(
        unit=IngestionUnit(
            checkpoint_before=checkpoint,
            checkpoint_after=advance_checkpoint(
                checkpoint, substage="deals", page_cursor="cursor-2"
            ),
            records=(),
        ),
        replay_id="replay-1",
        usage=Usage(),
        terminal=False,
    )

    with pytest.raises(BitrixBoundedContractError, match="requires a fence context"):
        DESCRIPTOR.writer.apply(
            object(),  # type: ignore[arg-type]
            context(checkpoint, _deals_scope()),
            unit,
        )


def test_writer_rejects_an_unsupported_operation_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_writer(monkeypatch)
    checkpoint = _deals_checkpoint()
    unit = BoundedUnit(
        unit=IngestionUnit(
            checkpoint_before=checkpoint,
            checkpoint_after=advance_checkpoint(
                checkpoint, substage="deals", page_cursor="cursor-2"
            ),
            records=({"bounded_op": "delete"},),
        ),
        replay_id="replay-1",
        usage=Usage(records=1),
        terminal=False,
    )

    with pytest.raises(BitrixBoundedContractError, match="operation kind is unsupported"):
        DESCRIPTOR.writer.apply(
            object(),  # type: ignore[arg-type]
            context(checkpoint, _deals_scope(), with_fence=True),
            unit,
        )


# --------------------------------------------------------------------------- #
# Retirement of legacy activity paths
# --------------------------------------------------------------------------- #


@dataclass
class _UnusedDiscoveryClient:
    def iter_crm_chat_refs(self) -> list[object]:
        raise AssertionError("retired discovery must not reach the client")

    def iter_crm_chat_ref_pages(self) -> list[object]:
        raise AssertionError("retired discovery must not reach the client")

    def iter_recent_chat_refs(self, page_size: int) -> list[object]:
        raise AssertionError("retired discovery must not reach the client")


def test_paged_activity_discovery_is_retired() -> None:
    with pytest.raises(RuntimeError, match="permanently retired"):
        next(
            iter(
                stream_chats(  # type: ignore[arg-type]
                    _UnusedDiscoveryClient(),
                    recent_page_size=10,
                )
            )
        )


def test_activity_chat_discovery_is_retired() -> None:
    with pytest.raises(RuntimeError, match="permanently retired"):
        discover_chats(_UnusedDiscoveryClient(), recent_page_size=10)  # type: ignore[arg-type]


def test_activity_list_requests_are_refused_before_io() -> None:
    with pytest.raises(RuntimeError, match="permanently retired"):
        _assert_activity_request_retired("crm.activity.list", {})


def test_activity_batch_commands_are_refused_before_io() -> None:
    params = {"cmd": {"a": "crm.activity.list?filter[PROVIDER_ID]=IMOPENLINES_SESSION"}}
    with pytest.raises(RuntimeError, match="permanently retired"):
        _assert_activity_request_retired("batch", params)


def test_non_activity_batch_commands_are_allowed() -> None:
    params = {"cmd": {"a": "crm.deal.list?start=-1", "b": "crm.contact.get"}}
    _assert_activity_request_retired("batch", params)


def test_deal_list_requests_are_allowed() -> None:
    _assert_activity_request_retired("crm.deal.list", {"start": -1})


# --------------------------------------------------------------------------- #
# Bounded extraction deadline / single attempt
# --------------------------------------------------------------------------- #


class _StubLlmSettings:
    """The bounded extraction settings ``chat_helpers`` reads."""

    chat_max_tokens = 1024


class _StubIngestionConfig:
    """A minimal ingestion config exposing only the extraction knobs."""

    def __init__(self) -> None:
        self.llm = _StubLlmSettings()


class _StubLlm(LLMService):
    """A minimal concrete LLM service for bounded-window assertions."""

    _endpoint_path = "/v1/chat"

    def _build_payload(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float,
        max_tokens: int | None,
    ) -> dict[str, object]:
        return {"model": model, "messages": [m.content for m in messages]}

    def _parse_text(self, body: object) -> str:
        return str(body)

    def _is_retryable(self, response: httpx.Response) -> bool:
        return response.status_code == 500


class _RecordingAsyncClient:
    """An httpx.AsyncClient stand-in that counts POSTs and never succeeds."""

    posts = 0

    def __init__(self, **_kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _RecordingAsyncClient:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        _RecordingAsyncClient.posts += 1
        request = httpx.Request("POST", "https://llm.test/v1/chat")
        return httpx.Response(500, request=request)


def _llm_config() -> LlmConfig:
    return LlmConfig(
        max_retries=3,
        retry_base_delay_seconds=0.0,
        retry_max_delay_seconds=0.0,
    )


def test_bounded_extraction_with_an_expired_deadline_makes_no_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingAsyncClient.posts = 0
    monkeypatch.setattr(base_module.httpx, "AsyncClient", _RecordingAsyncClient)
    service = _StubLlm(None, None, "stub", _llm_config())

    with pytest.raises(LlmCallAbortedError, match="passed its operation deadline"):
        asyncio.run(
            service.chat_json_bounded(
                [ChatMessage(role="user", content="hi")],
                deadline=datetime.now(UTC) - timedelta(seconds=1),
            )
        )

    assert _RecordingAsyncClient.posts == 0


def test_bounded_extraction_with_requested_cancellation_makes_no_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingAsyncClient.posts = 0
    monkeypatch.setattr(base_module.httpx, "AsyncClient", _RecordingAsyncClient)
    service = _StubLlm(None, None, "stub", _llm_config())

    with pytest.raises(LlmCallAbortedError, match="was cancelled"):
        asyncio.run(
            service.chat_json_bounded(
                [ChatMessage(role="user", content="hi")],
                deadline=datetime.now(UTC) + timedelta(seconds=30),
                cancellation=RevokedCancellation(),
            )
        )

    assert _RecordingAsyncClient.posts == 0


def test_bounded_extraction_makes_exactly_one_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingAsyncClient.posts = 0
    monkeypatch.setattr(base_module.httpx, "AsyncClient", _RecordingAsyncClient)
    service = _StubLlm(None, None, "stub", _llm_config())

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(
            service.chat_json_bounded(
                [ChatMessage(role="user", content="hi")],
                deadline=datetime.now(UTC) + timedelta(seconds=30),
            )
        )

    assert _RecordingAsyncClient.posts == 1


def test_legacy_json_call_keeps_its_retry_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingAsyncClient.posts = 0
    monkeypatch.setattr(base_module.httpx, "AsyncClient", _RecordingAsyncClient)
    service = _StubLlm(None, None, "stub", _llm_config())

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(service.chat_json([ChatMessage(role="user", content="hi")]))

    assert _RecordingAsyncClient.posts == 4


def test_bounded_extraction_skips_the_call_for_an_empty_batch() -> None:
    outcome = run_extraction_batch_bounded(
        [],
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )

    assert outcome.results == []
    assert outcome.extraction_calls == 0


def test_bounded_extraction_propagates_an_aborted_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        chat_helpers,
        "get_chat_extraction_service",
        lambda: _StubLlm(None, None, "stub", _llm_config()),
    )
    monkeypatch.setattr(chat_helpers, "get_ingestion_config", lambda: _StubIngestionConfig())

    with pytest.raises(LlmCallAbortedError):
        run_extraction_batch_bounded(
            ["hello"],
            deadline=datetime.now(UTC) - timedelta(seconds=1),
        )


def test_bounded_connector_version_matches_the_checkpoint_module() -> None:
    assert BITRIX_BOUNDED_CONNECTOR_VERSION == DESCRIPTOR.connector_version
    assert isinstance(BitrixBoundedClient, type)
    assert isinstance(ALL_CAPABILITIES, tuple)
