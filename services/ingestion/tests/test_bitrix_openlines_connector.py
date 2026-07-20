from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pytest import MonkeyPatch
from src.connectors.bitrix_openlines.connector import BitrixOpenLinesConnector
from src.connectors.bitrix_openlines.models import (
    ChatReference,
    DialogMetadata,
    OpenLineConfig,
    OpenLineMessage,
)
from src.ingestion_config import BitrixOpenLinesConfig


class StubClient:
    def list_active_configs(self) -> list[OpenLineConfig]:
        return [OpenLineConfig("46", "Speedzone: FB"), OpenLineConfig("123", "Device")]

    def iter_crm_chat_refs(self) -> list[ChatReference]:
        return [ChatReference(77, None, "crm_activity")]

    def iter_recent_chat_refs(self, page_size: int) -> list[ChatReference]:
        return [ChatReference(77, datetime(2026, 7, 20, 8, tzinfo=UTC), "recent_dialog")]

    def get_dialog(self, chat_id: int) -> DialogMetadata:
        return DialogMetadata(chat_id, "46", "facebook")

    def get_messages(self, chat_id: int) -> list[OpenLineMessage]:
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

    def close(self) -> None:
        return None


class UndatedOldChatClient(StubClient):
    def iter_recent_chat_refs(self, page_size: int) -> list[ChatReference]:
        return []

    def get_messages(self, chat_id: int) -> list[OpenLineMessage]:
        return [
            OpenLineMessage(
                1,
                501,
                "Ada",
                "Historical message",
                datetime(2026, 7, 20, 7, 50, tzinfo=UTC),
                False,
            )
        ]


class StubWatermark:
    def __init__(self, committed: datetime | None = None) -> None:
        self.committed = committed

    def get(self, *, overlap_seconds: int) -> datetime | None:
        if self.committed is None:
            return None
        return self.committed - timedelta(seconds=overlap_seconds)

    def set(self, value: datetime) -> None:
        self.committed = value

    def close(self) -> None:
        return None


def test_connector_emits_mapped_selected_conversation_with_open_lines_provenance(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.connectors.bitrix_openlines.connector.run_extraction_batch",
        lambda texts: [
            {
                "persons": [{"name": "Ada", "phone": "+6591234567"}],
                "transactions": [],
                "summary": "Ada supplied a phone number.",
                "confidence": 0.95,
            }
            for _ in texts
        ],
    )
    monkeypatch.setattr(
        "src.connectors.bitrix.connector.extraction_method_label", lambda: "llm:test"
    )
    watermark = StubWatermark()
    connector = BitrixOpenLinesConnector(
        StubClient(),
        watermark,
        BitrixOpenLinesConfig(entity_by_config_id={"46": "speedzone"}),
        mode="api",
    )

    records = list(connector.fetch_records())

    assert records[0]["source_record_id"] == "bitrix-openlines-chat-77-person-1"
    assert records[0]["raw_payload"]["openline_config_id"] == "46"
    assert records[0]["raw_payload"]["channel_type"] == "facebook_direct"
    assert records[0]["raw_payload"]["deal_id"] is None
    assert records[0]["conversation_ref"]["deal_id"] is None
    assert records[0]["raw_payload"]["discovery_methods"] == [
        "crm_activity",
        "recent_dialog",
    ]
    assert watermark.committed is None
    connector.commit_watermark()
    assert watermark.committed == datetime(2026, 7, 20, 8, tzinfo=UTC)


def test_connector_does_not_move_incremental_watermark_backward(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.connectors.bitrix_openlines.connector.run_extraction_batch",
        lambda texts: [
            {
                "persons": [],
                "transactions": [],
                "summary": "Older conversation inside the overlap window.",
                "confidence": 0.95,
            }
            for _ in texts
        ],
    )
    committed = datetime(2026, 7, 20, 8, 5, tzinfo=UTC)
    watermark = StubWatermark(committed)
    connector = BitrixOpenLinesConnector(
        StubClient(),
        watermark,
        BitrixOpenLinesConfig(
            entity_by_config_id={"46": "speedzone"},
            incremental_overlap_seconds=600,
        ),
        mode="api",
    )

    list(connector.fetch_records())
    connector.commit_watermark()

    assert watermark.committed == committed


def test_backfill_does_not_advance_incremental_watermark(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.connectors.bitrix_openlines.connector.run_extraction_batch",
        lambda texts: [
            {
                "persons": [],
                "transactions": [],
                "summary": "Backfilled conversation.",
                "confidence": 0.95,
            }
            for _ in texts
        ],
    )
    watermark = StubWatermark()
    connector = BitrixOpenLinesConnector(
        StubClient(),
        watermark,
        BitrixOpenLinesConfig(entity_by_config_id={"46": "speedzone"}),
        mode="backfill",
    )

    list(connector.fetch_records())
    connector.commit_watermark()

    assert watermark.committed is None


def test_connector_skips_undated_reference_with_messages_before_watermark(
    monkeypatch: MonkeyPatch,
) -> None:
    def fail_extraction(texts: list[str]) -> list[None]:
        raise AssertionError(f"Old chat reached extraction: {texts}")

    monkeypatch.setattr(
        "src.connectors.bitrix_openlines.connector.run_extraction_batch",
        fail_extraction,
    )
    committed = datetime(2026, 7, 20, 8, 5, tzinfo=UTC)
    connector = BitrixOpenLinesConnector(
        UndatedOldChatClient(),
        StubWatermark(committed),
        BitrixOpenLinesConfig(
            entity_by_config_id={"46": "speedzone"},
            incremental_overlap_seconds=600,
        ),
        mode="api",
    )

    assert list(connector.fetch_records()) == []
