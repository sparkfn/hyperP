from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

from pytest import MonkeyPatch
from src.connectors.bitrix_openlines.connector import BitrixOpenLinesConnector
from src.connectors.bitrix_openlines.models import (
    ChatReference,
    DialogMetadata,
    OpenLineConfig,
    OpenLineMessage,
)
from src.ingestion_config import BitrixOpenLinesConfig


class StubWatermark:
    def __init__(self) -> None:
        self.committed: datetime | None = None

    def get(self, *, overlap_seconds: int) -> datetime | None:
        return None

    def set(self, value: datetime) -> None:
        self.committed = value

    def close(self) -> None:
        return None


class StubDialogCache:
    def __init__(
        self,
        entries: dict[int, DialogMetadata] | None = None,
    ) -> None:
        self.entries: dict[int, DialogMetadata] = dict(entries or {})
        self.sets: list[tuple[int, DialogMetadata]] = []

    def get(self, chat_id: int) -> DialogMetadata | None:
        return self.entries.get(chat_id)

    def set(self, chat_id: int, dialog: DialogMetadata) -> None:
        self.sets.append((chat_id, dialog))
        self.entries[chat_id] = dialog

    def close(self) -> None:
        return None


class TrackingClient:
    def __init__(
        self,
        references: list[ChatReference],
        dialogs: dict[int, DialogMetadata] | None = None,
        config_id: str = "46",
    ) -> None:
        self.references = references
        self.dialogs = dialogs or {}
        self.dialog_calls: list[int] = []
        self.message_calls: list[int] = []
        self._config_id = config_id

    def list_active_configs(self) -> list[OpenLineConfig]:
        return [OpenLineConfig(self._config_id, "Speedzone: FB")]

    def iter_crm_chat_refs(self) -> list[ChatReference]:
        return self.references

    def iter_crm_chat_ref_pages(self) -> Iterator[list[ChatReference]]:
        yield self.references

    def iter_recent_chat_refs(self, page_size: int) -> list[ChatReference]:
        return []

    def get_dialog(self, chat_id: int) -> DialogMetadata:
        self.dialog_calls.append(chat_id)
        return self.dialogs.get(
            chat_id,
            DialogMetadata(chat_id, self._config_id, "facebook"),
        )

    def get_messages(self, chat_id: int) -> list[OpenLineMessage]:
        self.message_calls.append(chat_id)
        return [
            OpenLineMessage(
                1,
                501,
                "Ada",
                "My phone is +6591234567",
                datetime(2026, 7, 20, 8, tzinfo=UTC),
                False,
            )
        ]

    def get_history(self, chat_id: int) -> list[OpenLineMessage]:
        return self.get_messages(chat_id)

    def close(self) -> None:
        return None


def _extract_persons(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.connectors.bitrix_openlines.connector.run_extraction_batch",
        lambda texts: [
            {
                "persons": [{"name": "Ada", "phone": "+6591234567"}],
                "transactions": [],
                "summary": "Customer conversation.",
                "confidence": 0.95,
            }
            for _ in texts
        ],
    )


def test_backfill_with_no_selected_config_makes_no_dialog_lookups(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.connectors.bitrix_openlines.connector.run_extraction_batch",
        lambda texts: [_ for _ in texts] and [],
    )
    client = TrackingClient(
        [ChatReference(77, datetime(2026, 7, 20, 8, tzinfo=UTC), "crm_activity")],
    )
    connector = BitrixOpenLinesConnector(
        client,
        StubWatermark(),
        BitrixOpenLinesConfig(),
        mode="backfill",
    )

    records = list(connector.fetch_records())

    assert records == []
    assert client.dialog_calls == []
    assert client.message_calls == []
    assert connector._counters.dialogs_requested == 0
    assert connector._counters.chats_skipped_by_config == 1
    assert connector._counters.records_emitted == 0


def test_recent_dialog_origin_skips_dialog_lookup(
    monkeypatch: MonkeyPatch,
) -> None:
    _extract_persons(monkeypatch)
    client = TrackingClient(
        [
            ChatReference(
                77,
                datetime(2026, 7, 20, 8, tzinfo=UTC),
                "recent_dialog",
                config_id="46",
                connector_id="facebook",
            )
        ],
    )
    connector = BitrixOpenLinesConnector(
        client,
        StubWatermark(),
        BitrixOpenLinesConfig(entity_by_config_id={"46": "speedzone"}),
        mode="api",
    )

    records = list(connector.fetch_records())

    assert client.dialog_calls == []
    assert len(records) == 1
    assert records[0]["raw_payload"]["openline_config_id"] == "46"
    assert connector._counters.dialogs_requested == 0
    assert connector._counters.records_emitted == 1


def test_dialog_cache_skips_known_unselected_chat_without_lookup() -> None:
    cache = StubDialogCache(
        entries={77: DialogMetadata(77, "99", "facebook")},
    )
    client = TrackingClient(
        [ChatReference(77, datetime(2026, 7, 20, 8, tzinfo=UTC), "crm_activity")],
    )
    connector = BitrixOpenLinesConnector(
        client,
        StubWatermark(),
        BitrixOpenLinesConfig(
            included_channel_types=[],
            included_config_ids=["46"],
            entity_by_config_id={"46": "speedzone"},
        ),
        mode="api",
        dialog_cache=cache,
    )

    records = list(connector.fetch_records())

    assert records == []
    assert client.dialog_calls == []
    assert client.message_calls == []
    assert connector._counters.dialogs_requested == 0
    assert connector._counters.chats_skipped_by_config == 1


def test_dialog_cache_caches_unselected_chat_after_dialog_lookup() -> None:
    cache = StubDialogCache()
    client = TrackingClient(
        [ChatReference(77, datetime(2026, 7, 20, 8, tzinfo=UTC), "crm_activity")],
        dialogs={77: DialogMetadata(77, "99", "facebook")},
    )
    connector = BitrixOpenLinesConnector(
        client,
        StubWatermark(),
        BitrixOpenLinesConfig(
            included_channel_types=[],
            included_config_ids=["46"],
            entity_by_config_id={"46": "speedzone"},
        ),
        mode="api",
        dialog_cache=cache,
    )

    list(connector.fetch_records())

    assert client.dialog_calls == [77]
    assert cache.sets == [(77, DialogMetadata(77, "99", "facebook"))]

    client.dialog_calls.clear()
    connector2 = BitrixOpenLinesConnector(
        client,
        StubWatermark(),
        BitrixOpenLinesConfig(
            included_channel_types=[],
            included_config_ids=["46"],
            entity_by_config_id={"46": "speedzone"},
        ),
        mode="api",
        dialog_cache=cache,
    )

    list(connector2.fetch_records())

    assert client.dialog_calls == []
    assert connector2._counters.dialogs_requested == 0
    assert connector2._counters.chats_skipped_by_config == 1


def test_dialog_cache_reselects_chat_when_config_becomes_selected(
    monkeypatch: MonkeyPatch,
) -> None:
    _extract_persons(monkeypatch)
    cache = StubDialogCache(
        entries={77: DialogMetadata(77, "46", "facebook")},
    )
    client = TrackingClient(
        [ChatReference(77, datetime(2026, 7, 20, 8, tzinfo=UTC), "crm_activity")],
    )
    connector = BitrixOpenLinesConnector(
        client,
        StubWatermark(),
        BitrixOpenLinesConfig(
            included_channel_types=[],
            included_config_ids=["46"],
            entity_by_config_id={"46": "speedzone"},
        ),
        mode="api",
        dialog_cache=cache,
    )

    records = list(connector.fetch_records())

    assert client.dialog_calls == []
    assert len(records) == 1
    assert records[0]["raw_payload"]["openline_config_id"] == "46"
    assert connector._counters.dialogs_requested == 0
    assert connector._counters.records_emitted == 1


def test_counters_track_dialog_lookup_and_emission(
    monkeypatch: MonkeyPatch,
) -> None:
    _extract_persons(monkeypatch)
    client = TrackingClient(
        [ChatReference(77, datetime(2026, 7, 20, 8, tzinfo=UTC), "crm_activity")],
    )
    connector = BitrixOpenLinesConnector(
        client,
        StubWatermark(),
        BitrixOpenLinesConfig(entity_by_config_id={"46": "speedzone"}),
        mode="api",
    )

    list(connector.fetch_records())

    assert client.dialog_calls == [77]
    assert connector._counters.dialogs_requested == 1
    assert connector._counters.chats_skipped_by_config == 0
    assert connector._counters.records_emitted == 1
