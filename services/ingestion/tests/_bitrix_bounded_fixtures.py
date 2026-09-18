"""Reusable fixtures for the bounded Bitrix adapter (issue #432).

``ProxyTransport`` is an in-memory stand-in for the bounded Bitrix proxy: it
records every call and replays scripted capability / changed-deal /
changed-conversation pages on demand, so a test can assert exactly which source
calls a bounded unit made.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import cast

import httpx
from pydantic.types import JsonValue
from src.bitrix_ingestion_models import BitrixStreamKey, FenceContext
from src.bounded_ingestion_models import (
    AttemptContext,
    BoundedMode,
    CancellationSignal,
    RunScope,
    Usage,
)
from src.connectors.bitrix_openlines.bounded_checkpoint import initial_checkpoint
from src.connectors.bitrix_openlines.bounded_client import BitrixBoundedClient
from src.connectors.bitrix_openlines.bounded_contract import (
    CAPABILITY_ENDPOINT,
    CHANGED_CONVERSATIONS_ENDPOINT,
    CHANGED_DEALS_ENDPOINT,
    CONTRACT_VERSION,
    BitrixBoundedStream,
    required_capability,
)
from src.resumable import CheckpointDescriptor

SOURCE_SCOPE = "bitrix-scope-1"
SNAPSHOT_ID = "snapshot-1"
BOUNDED_BASE_URL = "https://bounded.test"
ALL_CAPABILITIES: tuple[str, ...] = (
    "changed_deals_v1",
    "changed_conversations_v1",
    "stage_history_artifact_v1",
)


def source_window(
    stream_key: BitrixBoundedStream,
    *,
    capabilities: tuple[str, ...] | None = None,
) -> dict[str, JsonValue]:
    """Return a frozen source window for one bounded stream."""
    granted = capabilities if capabilities is not None else (required_capability(stream_key),)
    return {
        "contract_version": CONTRACT_VERSION,
        "source_scope": SOURCE_SCOPE,
        "snapshot_id": SNAPSHOT_ID,
        "stream_key": stream_key,
        "capabilities": list(granted),
    }


def scope(
    stream_key: BitrixBoundedStream = "crm_deals",
    *,
    mode: BoundedMode = "delta",
    capabilities: tuple[str, ...] | None = None,
) -> RunScope:
    """Return a validated Bitrix bounded run scope for one stream."""
    return RunScope(
        environment="test",
        reset_generation=1,
        source_key="bitrix_chat",
        control_instance_id="bitrix-control",
        entity_key=None,
        stream_key=stream_key,
        mode=mode,
        configuration_fingerprint="bitrix-fingerprint",
        connector_version="bitrix-bounded-v1",
        checkpoint_schema_version=1,
        source_window=source_window(stream_key, capabilities=capabilities),
    )


def capability_checkpoint(
    stream_key: BitrixBoundedStream = "crm_deals",
    *,
    mode: BoundedMode = "delta",
    capabilities: tuple[str, ...] | None = None,
) -> CheckpointDescriptor:
    """Return the reserved capability checkpoint admission would produce."""
    return initial_checkpoint(
        scope(stream_key, mode=mode, capabilities=capabilities),
        None,
    )


def fence_context(stream_key: BitrixStreamKey = "crm_deals") -> FenceContext:
    """Return a Bitrix domain fence for one stream."""
    return FenceContext(
        logical_run_id="logical-bitrix",
        ingest_run_id="attempt-1",
        source_key="bitrix_chat",
        stream_key=stream_key,
        stream_generation=1,
        fencing_token=1,
        attempt_generation=1,
    )


def context(
    checkpoint: CheckpointDescriptor,
    run_scope: RunScope,
    *,
    cancellation: CancellationSignal | None = None,
    with_fence: bool = False,
) -> AttemptContext:
    """Return a bounded attempt holding the given checkpoint and scope."""
    return AttemptContext(
        logical_run_id="logical-bitrix",
        ingest_run_id="attempt-1",
        worker_task_id="task-1",
        attempt_generation=1,
        fencing_token=1,
        lease_token="lease-1",
        global_slot_index=0,
        global_slot_fencing_token=1,
        scope=run_scope,
        occurrence=None,
        checkpoint=checkpoint,
        usage=Usage(),
        reserved_usage=Usage(),
        bitrix_fence_context=fence_context() if with_fence else None,
        cancellation=cancellation,
    )


@dataclass(frozen=True)
class RevokedCancellation:
    """A cancellation signal that has already been requested."""

    def requested(self) -> bool:
        return True


@dataclass
class ProxyCall:
    """One recorded bounded proxy call."""

    endpoint: str
    body: dict[str, JsonValue]


@dataclass
class ProxyTransport:
    """An in-memory bounded Bitrix proxy with scripted pages per endpoint."""

    capabilities: JsonValue | None = None
    deals_pages: list[JsonValue] = field(default_factory=list)
    conversations_pages: list[JsonValue] = field(default_factory=list)
    calls: list[ProxyCall] = field(default_factory=list)

    def endpoints(self) -> list[str]:
        """Return the endpoints called so far, in order."""
        return [call.endpoint for call in self.calls]

    def _respond(self, endpoint: str) -> JsonValue:
        if endpoint == CAPABILITY_ENDPOINT:
            if self.capabilities is None:
                raise AssertionError("unexpected bounded capability call")
            return self.capabilities
        if endpoint == CHANGED_DEALS_ENDPOINT:
            if not self.deals_pages:
                raise AssertionError("unexpected changed-deal call beyond the scripted pages")
            return self.deals_pages.pop(0)
        if endpoint == CHANGED_CONVERSATIONS_ENDPOINT:
            if not self.conversations_pages:
                raise AssertionError(
                    "unexpected changed-conversation call beyond the scripted pages"
                )
            return self.conversations_pages.pop(0)
        raise AssertionError(f"unexpected bounded proxy endpoint {endpoint}")

    def handler(self, request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.lstrip("/")
        body = json.loads(request.content.decode("utf-8"))
        self.calls.append(ProxyCall(endpoint, cast(dict[str, JsonValue], dict(body))))
        return httpx.Response(200, json=cast(JsonValue, self._respond(endpoint)))

    def client(self) -> BitrixBoundedClient:
        """Return a bounded client wired to this transport."""
        return BitrixBoundedClient(
            base_url=BOUNDED_BASE_URL,
            timeout_seconds=5.0,
            max_response_bytes=1024 * 1024,
            http=httpx.Client(
                transport=httpx.MockTransport(self.handler),
                base_url=BOUNDED_BASE_URL,
            ),
        )


def capability_response(
    *,
    capabilities: tuple[str, ...] | None = None,
    source_scope: str = SOURCE_SCOPE,
    snapshot_id: str = SNAPSHOT_ID,
) -> dict[str, JsonValue]:
    """Return a conforming capability response."""
    return {
        "contract_version": CONTRACT_VERSION,
        "source_scope": source_scope,
        "snapshot_id": snapshot_id,
        "capabilities": list(capabilities if capabilities is not None else ALL_CAPABILITIES),
        "cursor_ttl_seconds": 3600,
    }


def deal_payload(
    deal_id: int,
    *,
    category_id: str = "1",
    stage_id: str | None = "NEW",
    title: str = "Bounded deal",
    modified_at: str | None = "2026-09-17T01:00:00+00:00",
    contacts: tuple[dict[str, JsonValue], ...] = (),
    contact_id: str | None = None,
) -> dict[str, JsonValue]:
    """Return an upstream-shaped pinned deal payload."""
    payload: dict[str, JsonValue] = {
        "ID": deal_id,
        "TITLE": title,
        "CATEGORY_ID": category_id,
        "STAGE_ID": stage_id,
        "CONTACTS": list(contacts),
    }
    if modified_at is not None:
        payload["DATE_MODIFY"] = modified_at
    if contact_id is not None:
        payload["CONTACT_ID"] = contact_id
    return payload


def deal_change(
    deal_id: int,
    *,
    change_version: int,
    kind: str = "upsert",
    category_id: str | None = "1",
    revision: str = "rev-1",
    payload: dict[str, JsonValue] | None = None,
    event_id: str | None = None,
) -> dict[str, JsonValue]:
    """Return one changed-deal feed entry."""
    return {
        "change_version": change_version,
        "deal_id": deal_id,
        "event_id": event_id or f"event-{deal_id}-{change_version}",
        "kind": kind,
        "revision": revision,
        "category_id": category_id,
        "payload": payload,
    }


def deals_page(
    changes: list[dict[str, JsonValue]],
    *,
    next_cursor: str | None = None,
    snapshot_id: str = SNAPSHOT_ID,
) -> dict[str, JsonValue]:
    """Return one changed-deal page."""
    return {
        "snapshot_id": snapshot_id,
        "changes": list(changes),
        "has_more": next_cursor is not None,
        "next_cursor": next_cursor,
    }


def conversation_change(
    chat_id: int,
    *,
    change_version: int,
    kind: str = "upsert",
    revision: str = "rev-1",
    message_cursor: str | None = "message-cursor-1",
    payload: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    """Return one changed-conversation feed entry."""
    return {
        "change_version": change_version,
        "chat_id": chat_id,
        "event_id": f"event-{chat_id}-{change_version}",
        "kind": kind,
        "revision": revision,
        "message_cursor": message_cursor,
        "payload": payload,
    }


def conversations_page(
    changes: list[dict[str, JsonValue]],
    *,
    next_cursor: str | None = None,
    snapshot_id: str = SNAPSHOT_ID,
) -> dict[str, JsonValue]:
    """Return one changed-conversation page."""
    return {
        "snapshot_id": snapshot_id,
        "changes": list(changes),
        "has_more": next_cursor is not None,
        "next_cursor": next_cursor,
    }
