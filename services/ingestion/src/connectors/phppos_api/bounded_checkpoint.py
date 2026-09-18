"""PHPPOS bounded-window and cursor validation.

The bounded path deliberately keeps all continuation state in a single fixed
phase. A source page is replayed from its page-start cursor until its complete
aggregate can be committed; no volatile child-row accumulator is checkpointed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from src.connectors.phppos_api.models import BoundedWindow
from src.models import JsonValue
from src.resumable import CheckpointCompatibility, CheckpointDescriptor

Resource = Literal["customers", "sales"]
CONNECTOR_VERSION = "phppos-bounded-v1"
CHECKPOINT_SCHEMA_VERSION = 1


class PhpposCheckpointError(ValueError):
    """A local checkpoint or source-window contract cannot be trusted."""

    def __init__(
        self,
        safe_message: str,
        *,
        compatibility: CheckpointCompatibility = "incompatible",
    ) -> None:
        super().__init__(safe_message)
        self.compatibility = compatibility


@dataclass(frozen=True)
class PhpposSourceWindow:
    """Non-secret identity plus immutable source-side window guarantees."""

    source_key: str
    tenant_id: str
    resource: Resource
    configuration_fingerprint: str
    window: BoundedWindow

    @classmethod
    def from_mapping(
        cls,
        raw: dict[str, JsonValue],
        *,
        source_key: str,
        configuration_fingerprint: str,
    ) -> PhpposSourceWindow:
        raw_source_key = raw.get("source_key")
        tenant_id = raw.get("tenant_id")
        resource = raw.get("resource")
        raw_fingerprint = raw.get("configuration_fingerprint")
        raw_window = raw.get("window")
        expected_resource = resource_for_source(source_key)
        if raw_source_key != source_key or not isinstance(tenant_id, str) or not tenant_id:
            raise PhpposCheckpointError("PHPPOS source window has the wrong source or tenant")
        if resource != expected_resource or raw_fingerprint != configuration_fingerprint:
            raise PhpposCheckpointError("PHPPOS source window is incompatible")
        if not isinstance(raw_window, dict):
            raise PhpposCheckpointError(
                "PHPPOS source window is malformed",
                compatibility="corrupted",
            )
        try:
            window = BoundedWindow.model_validate(raw_window)
        except ValueError as exc:
            raise PhpposCheckpointError("PHPPOS source capabilities are not admitted") from exc
        if window.retention_until <= datetime.now(UTC):
            raise PhpposCheckpointError(
                "PHPPOS source window has expired",
                compatibility="expired",
            )
        return cls(source_key, tenant_id, expected_resource, configuration_fingerprint, window)

    @property
    def fingerprint(self) -> str:
        payload = {
            "source_key": self.source_key,
            "tenant_id": self.tenant_id,
            "resource": self.resource,
            "configuration_fingerprint": self.configuration_fingerprint,
            "window": self.window.model_dump(mode="json"),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class PhpposCursor:
    """A page-start continuation and bounded in-page replay identity."""

    page_cursor: str | None
    record_offset: int
    page_replay_id: str
    terminal: bool = False
    terminal_marker: str | None = None

    def __post_init__(self) -> None:
        if self.record_offset < 0 or not self.page_replay_id:
            raise PhpposCheckpointError("PHPPOS cursor is invalid")
        if self.terminal != (self.terminal_marker is not None):
            raise PhpposCheckpointError("PHPPOS terminal cursor is invalid")
        if self.terminal and self.page_cursor is not None:
            raise PhpposCheckpointError("terminal PHPPOS cursor cannot have a next page")

    @classmethod
    def initial(cls, window: PhpposSourceWindow) -> PhpposCursor:
        return cls(None, 0, _replay_id(window, None, 0, False))

    @classmethod
    def from_mapping(cls, raw: dict[str, JsonValue], window: PhpposSourceWindow) -> PhpposCursor:
        page_cursor = raw.get("page_cursor")
        record_offset = raw.get("record_offset")
        page_replay_id = raw.get("page_replay_id")
        terminal = raw.get("terminal")
        terminal_marker = raw.get("terminal_marker")
        if page_cursor is not None and not isinstance(page_cursor, str):
            raise PhpposCheckpointError("PHPPOS cursor page is invalid", compatibility="corrupted")
        if not isinstance(record_offset, int) or isinstance(record_offset, bool):
            raise PhpposCheckpointError(
                "PHPPOS cursor offset is invalid",
                compatibility="corrupted",
            )
        if not isinstance(page_replay_id, str) or not isinstance(terminal, bool):
            raise PhpposCheckpointError(
                "PHPPOS cursor identity is invalid",
                compatibility="corrupted",
            )
        if terminal_marker is not None and not isinstance(terminal_marker, str):
            raise PhpposCheckpointError(
                "PHPPOS terminal marker is invalid",
                compatibility="corrupted",
            )
        cursor = cls(page_cursor, record_offset, page_replay_id, terminal, terminal_marker)
        expected = _replay_id(window, cursor.page_cursor, cursor.record_offset, cursor.terminal)
        if cursor.page_replay_id != expected:
            raise PhpposCheckpointError(
                "PHPPOS cursor replay identity is invalid",
                compatibility="corrupted",
            )
        return cursor

    def as_mapping(self) -> dict[str, JsonValue]:
        return {
            "page_cursor": self.page_cursor,
            "record_offset": self.record_offset,
            "page_replay_id": self.page_replay_id,
            "terminal": self.terminal,
            "terminal_marker": self.terminal_marker,
        }


def resource_for_source(source_key: str) -> Resource:
    if source_key in {"eko_phppos", "speedzone_phppos"}:
        return "customers"
    if source_key in {"eko_phppos:sales", "speedzone_phppos:sales"}:
        return "sales"
    raise PhpposCheckpointError("PHPPOS bounded source is unsupported")


def phase_for_resource(resource: Resource) -> str:
    return f"phppos_api:{resource}"


def initial_checkpoint(
    source_key: str,
    source_window: PhpposSourceWindow,
) -> CheckpointDescriptor:
    cursor = PhpposCursor.initial(source_window)
    return CheckpointDescriptor(
        phase=phase_for_resource(source_window.resource),
        cursor=cursor.as_mapping(),
        source_window=source_window_mapping(source_window),
        last_committed_record_id=None,
        connector_version=CONNECTOR_VERSION,
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        replay_boundary=cursor.page_replay_id,
    )


def parse_checkpoint(
    checkpoint: CheckpointDescriptor,
    *,
    source_key: str,
    configuration_fingerprint: str,
) -> tuple[PhpposSourceWindow, PhpposCursor]:
    if (
        checkpoint.connector_version != CONNECTOR_VERSION
        or checkpoint.schema_version != CHECKPOINT_SCHEMA_VERSION
    ):
        raise PhpposCheckpointError("PHPPOS checkpoint version is incompatible")
    window = PhpposSourceWindow.from_mapping(
        checkpoint.source_window,
        source_key=source_key,
        configuration_fingerprint=configuration_fingerprint,
    )
    if checkpoint.phase != phase_for_resource(window.resource):
        raise PhpposCheckpointError("PHPPOS checkpoint phase is incompatible")
    cursor = PhpposCursor.from_mapping(checkpoint.cursor, window)
    if checkpoint.replay_boundary != cursor.page_replay_id:
        raise PhpposCheckpointError(
            "PHPPOS checkpoint replay boundary is incompatible",
            compatibility="corrupted",
        )
    return window, cursor


def next_checkpoint(
    checkpoint: CheckpointDescriptor,
    window: PhpposSourceWindow,
    *,
    next_page_cursor: str | None,
    next_record_offset: int = 0,
    last_committed_record_id: str | None,
    terminal: bool,
) -> CheckpointDescriptor:
    """Continue at the next page, or inside the current page at a bounded offset."""
    if terminal:
        if next_page_cursor is not None or next_record_offset != 0:
            raise PhpposCheckpointError("terminal PHPPOS checkpoint cannot continue in page")
        marker = "terminal:" + window.window.snapshot_id
        cursor = PhpposCursor(None, 0, _replay_id(window, None, 0, True), True, marker)
    else:
        if next_record_offset < 0:
            raise PhpposCheckpointError("PHPPOS cursor offset is invalid")
        cursor = PhpposCursor(
            next_page_cursor,
            next_record_offset,
            _replay_id(window, next_page_cursor, next_record_offset, False),
        )
    return CheckpointDescriptor(
        phase=checkpoint.phase,
        cursor=cursor.as_mapping(),
        source_window=checkpoint.source_window,
        last_committed_record_id=last_committed_record_id,
        connector_version=checkpoint.connector_version,
        schema_version=checkpoint.schema_version,
        replay_boundary=cursor.page_replay_id,
    )


def source_window_mapping(window: PhpposSourceWindow) -> dict[str, JsonValue]:
    return {
        "source_key": window.source_key,
        "tenant_id": window.tenant_id,
        "resource": window.resource,
        "configuration_fingerprint": window.configuration_fingerprint,
        "window": window.window.model_dump(mode="json"),
    }


def _replay_id(
    window: PhpposSourceWindow,
    page_cursor: str | None,
    record_offset: int,
    terminal: bool,
) -> str:
    payload = "|".join(
        (
            window.fingerprint,
            page_cursor or "first-page",
            str(record_offset),
            "terminal" if terminal else "open",
        )
    )
    return "phppos:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
