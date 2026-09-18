"""Durable bounded-checkpoint tests for the bounded Bitrix adapter."""

from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest
from pydantic.types import JsonValue
from src.bounded_ingestion_models import BoundedMode, RunScope
from src.connectors.bitrix_openlines.bounded_checkpoint import (
    advance_checkpoint,
    initial_checkpoint,
    validate_checkpoint,
    validate_scope_window,
    validate_source_window,
)
from src.connectors.bitrix_openlines.bounded_contract import (
    CONTRACT_VERSION,
    BitrixBoundedContractError,
)
from src.resumable import CheckpointDescriptor

_SOURCE_SCOPE = "scope-a"
_SNAPSHOT_ID = "snap-a"
_STREAM_CAPABILITIES: dict[str, str] = {
    "crm_deals": "changed_deals_v1",
    "openlines_conversations": "changed_conversations_v1",
    "crm_stage_history": "stage_history_artifact_v1",
}
_STREAMS = ("crm_deals", "openlines_conversations", "crm_stage_history")
_SUBSTAGES = (
    "capability",
    "deals",
    "conversation_discovery",
    "messages",
    "extraction",
    "apply",
    "known_owner",
    "terminal",
)


def source_window(
    *,
    stream_key: str = "crm_deals",
    contract_version: JsonValue = CONTRACT_VERSION,
    source_scope: JsonValue = _SOURCE_SCOPE,
    snapshot_id: JsonValue = _SNAPSHOT_ID,
    capabilities: JsonValue = None,
) -> dict[str, JsonValue]:
    """Build one frozen source window; ``None`` capabilities means the stream's own."""
    capability = _STREAM_CAPABILITIES.get(stream_key)
    default_capabilities: JsonValue = []
    if capability is not None:
        default_capabilities = [capability]
    selected_capabilities: JsonValue = capabilities
    if selected_capabilities is None:
        selected_capabilities = default_capabilities
    return {
        "contract_version": contract_version,
        "source_scope": source_scope,
        "snapshot_id": snapshot_id,
        "stream_key": stream_key,
        "capabilities": selected_capabilities,
    }


def run_scope(
    *,
    source_key: str = "bitrix_chat",
    mode: BoundedMode = "bootstrap",
    stream_key: str | None = "crm_deals",
    window: dict[str, JsonValue] | None = None,
) -> RunScope:
    """Build one valid bounded Bitrix scope; callers mutate one field to reject."""
    selected_window = window
    if selected_window is None:
        selected_window = source_window(stream_key=stream_key or "crm_deals")
    return RunScope(
        environment="test",
        reset_generation=1,
        source_key=source_key,
        control_instance_id="bitrix-control",
        entity_key="bitrix-entity",
        stream_key=stream_key,
        mode=mode,
        configuration_fingerprint="bitrix-fingerprint",
        connector_version="bitrix-bounded-v1",
        checkpoint_schema_version=1,
        source_window=selected_window,
    )


def checkpoint(
    *,
    phase: str = "bitrix_bounded_v1",
    cursor: dict[str, JsonValue] | None = None,
    substage: JsonValue = "capability",
    stream_key: JsonValue = "crm_deals",
    page_cursor: JsonValue = None,
    window: dict[str, JsonValue] | None = None,
    last_committed_record_id: str | None = None,
    connector_version: str = "bitrix-bounded-v1",
    schema_version: int = 1,
    replay_boundary: str = "bitrix_bounded_unit",
) -> CheckpointDescriptor:
    """Build one bounded Bitrix checkpoint; ``cursor`` overrides the assembled cursor."""
    selected_cursor = cursor
    if selected_cursor is None:
        selected_cursor = {
            "substage": substage,
            "stream_key": stream_key,
            "page_cursor": page_cursor,
        }
    selected_window = window
    if selected_window is None:
        selected_window = source_window()
    return CheckpointDescriptor(
        phase=phase,
        cursor=selected_cursor,
        source_window=selected_window,
        last_committed_record_id=last_committed_record_id,
        connector_version=connector_version,
        schema_version=schema_version,
        replay_boundary=replay_boundary,
    )


@pytest.mark.parametrize("stream_key", _STREAMS)
def test_validate_scope_window_accepts_each_bounded_stream(stream_key: str) -> None:
    assert validate_scope_window(run_scope(stream_key=stream_key)) == stream_key


def test_validate_scope_window_rejects_a_foreign_source_key() -> None:
    with pytest.raises(BitrixBoundedContractError, match="requires source_key='bitrix_chat'"):
        validate_scope_window(run_scope(source_key="fundbox"))


def test_validate_scope_window_rejects_one_time_mode() -> None:
    with pytest.raises(BitrixBoundedContractError, match="does not support one-time mode"):
        validate_scope_window(run_scope(mode="one_time"))


def test_validate_scope_window_rejects_an_unsupported_mode() -> None:
    with pytest.raises(BitrixBoundedContractError, match="does not support one-time mode"):
        validate_scope_window(run_scope(mode=cast(BoundedMode, "batch")))


@pytest.mark.parametrize("window_stream", _STREAMS)
def test_validate_scope_window_rejects_an_unknown_stream_before_the_window(
    window_stream: str,
) -> None:
    scope = run_scope(stream_key="unknown_stream", window=source_window(stream_key=window_stream))
    with pytest.raises(BitrixBoundedContractError, match="stream is unsupported"):
        validate_scope_window(scope)


@pytest.mark.parametrize("stream_key", [None, "fixture-stream", "CRM_DEALS"])
def test_validate_scope_window_rejects_a_missing_or_unknown_stream(
    stream_key: str | None,
) -> None:
    with pytest.raises(BitrixBoundedContractError, match="stream is unsupported"):
        validate_scope_window(run_scope(stream_key=stream_key))


def test_validate_scope_window_propagates_a_capability_gap() -> None:
    scope = run_scope(window=source_window(capabilities=["other_capability_v1"]))
    with pytest.raises(BitrixBoundedContractError, match="lacks capability for stream crm_deals"):
        validate_scope_window(scope)


@pytest.mark.parametrize("stream_key", _STREAMS)
def test_validate_source_window_accepts_each_bounded_stream(stream_key: str) -> None:
    window = source_window(stream_key=stream_key)
    assert validate_source_window(window, stream_key) is None  # type: ignore[arg-type]


def test_validate_source_window_rejects_an_unexpected_field() -> None:
    window = source_window()
    window["extra"] = "unexpected"
    with pytest.raises(BitrixBoundedContractError, match="unexpected or missing fields"):
        validate_source_window(window, "crm_deals")


@pytest.mark.parametrize(
    "field",
    ["contract_version", "source_scope", "snapshot_id", "stream_key", "capabilities"],
)
def test_validate_source_window_rejects_a_missing_field(field: str) -> None:
    window = source_window()
    del window[field]
    with pytest.raises(BitrixBoundedContractError, match="unexpected or missing fields"):
        validate_source_window(window, "crm_deals")


@pytest.mark.parametrize(
    "contract_version",
    ["bitrix_bounded_v2", "", None, 1, ["bitrix_bounded_v1"]],
)
def test_validate_source_window_rejects_a_wrong_contract_version(
    contract_version: JsonValue,
) -> None:
    window = source_window(contract_version=contract_version)
    with pytest.raises(BitrixBoundedContractError, match="invalid contract version"):
        validate_source_window(window, "crm_deals")


def test_validate_source_window_rejects_a_stream_mismatch() -> None:
    window = source_window(stream_key="crm_stage_history")
    with pytest.raises(BitrixBoundedContractError, match="stream does not match run scope"):
        validate_source_window(window, "crm_deals")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_scope", "", "invalid source scope"),
        ("source_scope", "   ", "invalid source scope"),
        ("source_scope", 5, "invalid source scope"),
        ("snapshot_id", "", "invalid snapshot ID"),
        ("snapshot_id", "   ", "invalid snapshot ID"),
        ("snapshot_id", None, "invalid snapshot ID"),
    ],
)
def test_validate_source_window_rejects_blank_scope_or_snapshot(
    field: str,
    value: JsonValue,
    message: str,
) -> None:
    window = source_window()
    window[field] = value
    with pytest.raises(BitrixBoundedContractError, match=message):
        validate_source_window(window, "crm_deals")


@pytest.mark.parametrize("capability", [5, "", "   ", None])
def test_validate_source_window_rejects_a_non_string_capability(capability: JsonValue) -> None:
    window = source_window(capabilities=[capability])
    with pytest.raises(BitrixBoundedContractError, match="invalid capabilities"):
        validate_source_window(window, "crm_deals")


@pytest.mark.parametrize("capabilities", ["changed_deals_v1", {"changed_deals_v1": 1}, None])
def test_validate_source_window_rejects_non_list_capabilities(capabilities: JsonValue) -> None:
    window = source_window()
    window["capabilities"] = capabilities
    with pytest.raises(BitrixBoundedContractError, match="invalid capabilities"):
        validate_source_window(window, "crm_deals")


def test_validate_source_window_rejects_duplicate_capabilities() -> None:
    window = source_window(capabilities=["changed_deals_v1", "changed_deals_v1"])
    with pytest.raises(BitrixBoundedContractError, match="duplicate capabilities"):
        validate_source_window(window, "crm_deals")


def test_validate_source_window_rejects_a_missing_required_capability() -> None:
    window = source_window(capabilities=["changed_conversations_v1"])
    with pytest.raises(BitrixBoundedContractError, match="lacks capability for stream crm_deals"):
        validate_source_window(window, "crm_deals")


def test_initial_checkpoint_reserves_the_capability_unit() -> None:
    scope = run_scope()
    reserved = initial_checkpoint(scope, None)
    assert reserved.phase == "bitrix_bounded_v1"
    assert reserved.cursor == {
        "substage": "capability",
        "stream_key": "crm_deals",
        "page_cursor": None,
    }
    assert reserved.source_window == scope.source_window
    assert reserved.last_committed_record_id is None
    assert reserved.connector_version == "bitrix-bounded-v1"
    assert reserved.schema_version == 1
    assert reserved.replay_boundary == "bitrix_bounded_unit"


@pytest.mark.parametrize("stream_key", _STREAMS)
def test_initial_checkpoint_binds_the_stream_key(stream_key: str) -> None:
    scope = run_scope(stream_key=stream_key)
    reserved = initial_checkpoint(scope, None)
    assert reserved.cursor["stream_key"] == stream_key
    assert validate_checkpoint(reserved) == stream_key


def test_initial_checkpoint_does_not_alias_the_caller_window_mapping() -> None:
    scope = run_scope()
    reserved = initial_checkpoint(scope, None)
    reserved.source_window["snapshot_id"] = "mutated"
    assert scope.source_window["snapshot_id"] == _SNAPSHOT_ID


def test_initial_checkpoint_window_copy_is_shallow() -> None:
    """Finding: the frozen window shares its nested capability list with the caller's scope."""
    scope = run_scope()
    reserved = initial_checkpoint(scope, None)
    assert reserved.source_window["capabilities"] is scope.source_window["capabilities"]


def test_initial_checkpoint_rejects_a_foreign_scope() -> None:
    with pytest.raises(BitrixBoundedContractError, match="requires source_key='bitrix_chat'"):
        initial_checkpoint(run_scope(source_key="fundbox"), None)


@pytest.mark.parametrize("stream_key", _STREAMS)
def test_validate_checkpoint_returns_the_cursor_stream_key(stream_key: str) -> None:
    reserved = initial_checkpoint(run_scope(stream_key=stream_key), None)
    assert validate_checkpoint(reserved) == stream_key


@pytest.mark.parametrize("substage", _SUBSTAGES)
def test_validate_checkpoint_accepts_every_declared_substage(substage: str) -> None:
    assert validate_checkpoint(checkpoint(substage=substage)) == "crm_deals"


def test_validate_checkpoint_rejects_a_wrong_phase() -> None:
    with pytest.raises(BitrixBoundedContractError, match="invalid phase"):
        validate_checkpoint(checkpoint(phase="records"))


def test_validate_checkpoint_rejects_a_wrong_connector_version() -> None:
    with pytest.raises(BitrixBoundedContractError, match="invalid connector version"):
        validate_checkpoint(checkpoint(connector_version="fixture-v1"))


def test_validate_checkpoint_rejects_a_wrong_schema_version() -> None:
    with pytest.raises(BitrixBoundedContractError, match="invalid schema version"):
        validate_checkpoint(checkpoint(schema_version=2))


@pytest.mark.parametrize("schema_version", [0, -1, True])
def test_checkpoint_descriptor_rejects_a_non_positive_schema_version(
    schema_version: int,
) -> None:
    with pytest.raises(ValueError, match="schema version must be positive"):
        checkpoint(schema_version=schema_version)


def test_validate_checkpoint_rejects_a_wrong_replay_boundary() -> None:
    with pytest.raises(BitrixBoundedContractError, match="invalid replay boundary"):
        validate_checkpoint(checkpoint(replay_boundary="page"))


@pytest.mark.parametrize("substage", ["deals ", "unknown", "", "DEALS", "capability "])
def test_validate_checkpoint_rejects_an_unknown_substage(substage: JsonValue) -> None:
    with pytest.raises(BitrixBoundedContractError, match="invalid substage"):
        validate_checkpoint(checkpoint(substage=substage))


def test_validate_checkpoint_rejects_a_null_substage() -> None:
    with pytest.raises(BitrixBoundedContractError, match="invalid substage"):
        validate_checkpoint(checkpoint(substage=None))


def test_validate_checkpoint_rejects_a_cursor_without_a_substage() -> None:
    descriptor = checkpoint(cursor={"stream_key": "crm_deals"})
    with pytest.raises(BitrixBoundedContractError, match="invalid substage"):
        validate_checkpoint(descriptor)


@pytest.mark.parametrize("stream_key", ["not_a_stream", "unknown-stream", "crm_deal"])
def test_validate_checkpoint_rejects_an_unknown_stream_key(stream_key: JsonValue) -> None:
    with pytest.raises(BitrixBoundedContractError, match="stream is unsupported"):
        validate_checkpoint(checkpoint(stream_key=stream_key))


def test_validate_checkpoint_rejects_a_null_stream_key() -> None:
    with pytest.raises(BitrixBoundedContractError, match="stream is unsupported"):
        validate_checkpoint(checkpoint(stream_key=None))


def test_validate_checkpoint_rejects_a_cursor_without_a_stream_key() -> None:
    descriptor = checkpoint(cursor={"substage": "capability"})
    with pytest.raises(BitrixBoundedContractError, match="stream is unsupported"):
        validate_checkpoint(descriptor)


@pytest.mark.parametrize("stream_key", [[], {}, ["crm_deals"]])
def test_validate_checkpoint_rejects_a_non_hashable_stream_key(stream_key: JsonValue) -> None:
    """A corrupted cursor must not leak a raw TypeError out of the stream-key guard."""
    descriptor = checkpoint(cursor={"substage": "capability", "stream_key": stream_key})
    with pytest.raises(BitrixBoundedContractError, match="stream is unsupported"):
        validate_checkpoint(descriptor)


def test_validate_checkpoint_propagates_a_source_window_failure() -> None:
    incompatible = checkpoint(window=source_window(capabilities=["other_capability_v1"]))
    with pytest.raises(BitrixBoundedContractError, match="lacks capability for stream crm_deals"):
        validate_checkpoint(incompatible)


def test_validate_checkpoint_accepts_a_cursor_without_a_page_cursor() -> None:
    descriptor = checkpoint(cursor={"substage": "deals", "stream_key": "crm_deals"})
    assert validate_checkpoint(descriptor) == "crm_deals"


def test_advance_checkpoint_preserves_phase_window_and_versions() -> None:
    original = checkpoint()
    advanced = advance_checkpoint(original, substage="deals", page_cursor="cursor-1")
    assert advanced.phase == original.phase
    assert advanced.source_window == original.source_window
    assert advanced.connector_version == original.connector_version
    assert advanced.schema_version == original.schema_version
    assert advanced.replay_boundary == original.replay_boundary
    assert advanced.cursor["substage"] == "deals"
    assert advanced.cursor["page_cursor"] == "cursor-1"
    assert advanced.last_committed_record_id is None


@pytest.mark.parametrize("substage", _SUBSTAGES)
def test_advance_checkpoint_advances_to_each_declared_substage(substage: str) -> None:
    advanced = advance_checkpoint(checkpoint(), substage=substage, page_cursor=None)
    assert advanced.cursor["substage"] == substage
    assert validate_checkpoint(advanced) == "crm_deals"


def test_advance_checkpoint_records_the_committed_record_id() -> None:
    advanced = advance_checkpoint(
        checkpoint(),
        substage="messages",
        page_cursor="cursor-9",
        last_committed_record_id="record-9",
    )
    assert advanced.last_committed_record_id == "record-9"


def test_advance_checkpoint_resets_an_omitted_committed_record_id() -> None:
    original = checkpoint(last_committed_record_id="record-7")
    advanced = advance_checkpoint(original, substage="deals", page_cursor="cursor-1")
    assert advanced.last_committed_record_id is None


def test_advance_checkpoint_rejects_an_unsupported_substage() -> None:
    with pytest.raises(ValueError, match="substage is unsupported") as error:
        advance_checkpoint(checkpoint(), substage="unknown", page_cursor=None)
    assert type(error.value) is ValueError


def test_advance_checkpoint_rejects_an_incompatible_checkpoint() -> None:
    with pytest.raises(BitrixBoundedContractError, match="invalid phase"):
        advance_checkpoint(checkpoint(phase="records"), substage="deals", page_cursor=None)


def test_advance_checkpoint_preserves_the_original_stream_identity() -> None:
    original = initial_checkpoint(run_scope(stream_key="openlines_conversations"), None)
    advanced = advance_checkpoint(original, substage="messages", page_cursor="cursor-1")
    assert advanced.cursor["stream_key"] == "openlines_conversations"
    assert advanced.source_window["stream_key"] == "openlines_conversations"
    assert validate_checkpoint(advanced) == "openlines_conversations"


def test_a_checkpoint_cannot_be_advanced_into_another_streams_window() -> None:
    original = initial_checkpoint(run_scope(stream_key="crm_deals"), None)
    swapped = replace(original, source_window=source_window(stream_key="crm_stage_history"))
    with pytest.raises(BitrixBoundedContractError, match="stream does not match run scope"):
        advance_checkpoint(swapped, substage="deals", page_cursor=None)


def test_advance_checkpoint_output_revalidates() -> None:
    advanced = advance_checkpoint(
        initial_checkpoint(run_scope(), None),
        substage="deals",
        page_cursor="cursor-1",
    )
    assert validate_checkpoint(advanced) == "crm_deals"
    further = advance_checkpoint(advanced, substage="terminal", page_cursor=None)
    assert validate_checkpoint(further) == "crm_deals"
