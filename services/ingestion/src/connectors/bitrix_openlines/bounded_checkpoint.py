"""Typed durable checkpoint state for the bounded Bitrix adapter."""

from __future__ import annotations

from typing import cast

from pydantic.types import JsonValue

from src.bounded_ingestion_models import OccurrenceContext, RunScope
from src.connectors.bitrix_openlines.bounded_contract import (
    CONTRACT_VERSION,
    BitrixBoundedContractError,
    BitrixBoundedStream,
    required_capability,
)
from src.resumable import CheckpointDescriptor

BITRIX_BOUNDED_PHASE = "bitrix_bounded_v1"
BITRIX_BOUNDED_CONNECTOR_VERSION = "bitrix-bounded-v1"
BITRIX_BOUNDED_SCHEMA_VERSION = 1
_SUBSTAGES: frozenset[str] = frozenset(
    {
        "capability",
        "deals",
        "conversation_discovery",
        "messages",
        "extraction",
        "apply",
        "known_owner",
        "terminal",
    }
)


def initial_checkpoint(
    scope: RunScope,
    _occurrence: OccurrenceContext | None,
) -> CheckpointDescriptor:
    """Create the reserved capability unit checkpoint for a frozen source window."""
    stream_key = validate_scope_window(scope)
    return CheckpointDescriptor(
        phase=BITRIX_BOUNDED_PHASE,
        cursor={"substage": "capability", "stream_key": stream_key, "page_cursor": None},
        source_window=dict(scope.source_window),
        last_committed_record_id=None,
        connector_version=BITRIX_BOUNDED_CONNECTOR_VERSION,
        schema_version=BITRIX_BOUNDED_SCHEMA_VERSION,
        replay_boundary="bitrix_bounded_unit",
    )


def validate_checkpoint(checkpoint: CheckpointDescriptor) -> BitrixBoundedStream:
    """Reject incompatible, mutable, or cross-stream continuation state."""
    if checkpoint.phase != BITRIX_BOUNDED_PHASE:
        raise BitrixBoundedContractError("Bitrix bounded checkpoint has an invalid phase")
    if checkpoint.connector_version != BITRIX_BOUNDED_CONNECTOR_VERSION:
        raise BitrixBoundedContractError(
            "Bitrix bounded checkpoint has an invalid connector version"
        )
    if checkpoint.schema_version != BITRIX_BOUNDED_SCHEMA_VERSION:
        raise BitrixBoundedContractError("Bitrix bounded checkpoint has an invalid schema version")
    if checkpoint.replay_boundary != "bitrix_bounded_unit":
        raise BitrixBoundedContractError("Bitrix bounded checkpoint has an invalid replay boundary")
    substage = checkpoint.cursor.get("substage")
    stream_value = checkpoint.cursor.get("stream_key")
    if not isinstance(substage, str) or substage not in _SUBSTAGES:
        raise BitrixBoundedContractError("Bitrix bounded checkpoint has an invalid substage")
    stream_key = _stream_key(stream_value)
    validate_source_window(checkpoint.source_window, stream_key)
    return stream_key


def advance_checkpoint(
    checkpoint: CheckpointDescriptor,
    *,
    substage: str,
    page_cursor: str | None,
    last_committed_record_id: str | None = None,
) -> CheckpointDescriptor:
    """Advance one committed bounded unit without changing its frozen source window."""
    stream_key = validate_checkpoint(checkpoint)
    if substage not in _SUBSTAGES:
        raise ValueError("Bitrix bounded substage is unsupported")
    return CheckpointDescriptor(
        phase=checkpoint.phase,
        cursor={"substage": substage, "stream_key": stream_key, "page_cursor": page_cursor},
        source_window=dict(checkpoint.source_window),
        last_committed_record_id=last_committed_record_id,
        connector_version=checkpoint.connector_version,
        schema_version=checkpoint.schema_version,
        replay_boundary=checkpoint.replay_boundary,
    )


def validate_scope_window(scope: RunScope) -> BitrixBoundedStream:
    """Require Bitrix scope identity and a source window frozen before admission."""
    if scope.source_key != "bitrix_chat":
        raise BitrixBoundedContractError("Bitrix bounded adapter requires source_key='bitrix_chat'")
    if scope.mode not in {"bootstrap", "delta"}:
        raise BitrixBoundedContractError("Bitrix bounded adapter does not support one-time mode")
    stream_key = _stream_key(scope.stream_key)
    validate_source_window(scope.source_window, stream_key)
    return stream_key


def validate_source_window(
    window: dict[str, JsonValue],
    stream_key: BitrixBoundedStream,
) -> None:
    """Validate the immutable capability/snapshot boundary supplied by admission."""
    required = {"contract_version", "source_scope", "snapshot_id", "stream_key", "capabilities"}
    if set(window) != required:
        raise BitrixBoundedContractError(
            "Bitrix bounded source window has unexpected or missing fields"
        )
    if window["contract_version"] != CONTRACT_VERSION:
        raise BitrixBoundedContractError(
            "Bitrix bounded source window has an invalid contract version"
        )
    if window["stream_key"] != stream_key:
        raise BitrixBoundedContractError(
            "Bitrix bounded source window stream does not match run scope"
        )
    if not isinstance(window["source_scope"], str) or not window["source_scope"].strip():
        raise BitrixBoundedContractError("Bitrix bounded source window has an invalid source scope")
    if not isinstance(window["snapshot_id"], str) or not window["snapshot_id"].strip():
        raise BitrixBoundedContractError("Bitrix bounded source window has an invalid snapshot ID")
    capabilities = window["capabilities"]
    if not isinstance(capabilities, list) or any(
        not isinstance(item, str) or not item.strip() for item in capabilities
    ):
        raise BitrixBoundedContractError("Bitrix bounded source window has invalid capabilities")
    if len(set(capabilities)) != len(capabilities):
        raise BitrixBoundedContractError("Bitrix bounded source window has duplicate capabilities")
    if required_capability(stream_key) not in capabilities:
        raise BitrixBoundedContractError(
            f"Bitrix bounded source window lacks capability for stream {stream_key}"
        )


def _stream_key(value: object) -> BitrixBoundedStream:
    if isinstance(value, str) and value in {
        "crm_deals",
        "openlines_conversations",
        "crm_stage_history",
    }:
        return cast(BitrixBoundedStream, value)
    raise BitrixBoundedContractError("Bitrix bounded stream is unsupported")
