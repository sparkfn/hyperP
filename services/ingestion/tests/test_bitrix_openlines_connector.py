from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pytest import MonkeyPatch
from src.connectors.bitrix_openlines.connector import BitrixOpenLinesConnector
from src.connectors.bitrix_openlines.models import (
    ChatReference,
    CrmOwnerReference,
    DialogMetadata,
    OpenLineConfig,
    OpenLineMessage,
)
from src.exclusion_config import ExclusionFile
from src.ingestion_config import BitrixOpenLinesConfig


class StubClient:
    def list_active_configs(self) -> list[OpenLineConfig]:
        return [OpenLineConfig("46", "Speedzone: FB"), OpenLineConfig("123", "Device")]

    def iter_crm_chat_refs(self) -> list[ChatReference]:
        return [
            ChatReference(
                77,
                None,
                "crm_activity",
                activity_ids=("900",),
                crm_owner_references=(CrmOwnerReference("deal", 501),),
                provider_references=({"CHAT_ID": "77"},),
            )
        ]

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

    def get_history(self, chat_id: int) -> list[OpenLineMessage]:
        return self.get_messages(chat_id)

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

    assert connector.get_source_key() == "bitrix_chat"
    assert records[0]["source_record_id"] == "bitrix-openlines-chat-77-person-1"
    assert records[0]["entity_key"] == "speedzone"
    assert records[0]["conversation_ref"]["platform"] == "bitrix_openlines"
    assert records[0]["conversation_ref"]["tenant"] == "speedzone"
    assert records[0]["raw_payload"]["openline_config_id"] == "46"
    assert records[0]["raw_payload"]["channel_type"] == "facebook_direct"
    assert records[0]["raw_payload"]["deal_id"] is None
    assert records[0]["conversation_ref"]["deal_id"] is None
    assert records[0]["raw_payload"]["discovery_methods"] == [
        "crm_activity",
        "recent_dialog",
    ]
    assert records[0]["raw_payload"]["bitrix_chat_id"] == "chat77"
    assert records[0]["raw_payload"]["bitrix_chat_id_numeric"] == 77
    assert records[0]["raw_payload"]["crm_activity_ids"] == ["900"]
    assert records[0]["raw_payload"]["crm_owner_references"] == [
        {"owner_type": "deal", "owner_id": 501}
    ]
    assert records[0]["raw_payload"]["crm_provider_references"] == [{"CHAT_ID": "77"}]
    assert records[0]["raw_payload"]["first_message_at"] == "2026-07-20T08:00:00+00:00"
    assert records[0]["raw_payload"]["last_message_at"] == "2026-07-20T08:00:00+00:00"
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


class NewerMessageClient(StubClient):
    def iter_recent_chat_refs(self, page_size: int) -> list[ChatReference]:
        return [ChatReference(77, datetime(2026, 7, 20, 8, tzinfo=UTC), "recent_dialog")]

    def get_messages(self, chat_id: int) -> list[OpenLineMessage]:
        return [
            OpenLineMessage(
                1,
                501,
                "Ada",
                "A newer message",
                datetime(2026, 7, 20, 9, tzinfo=UTC),
                False,
            )
        ]


class StaleDiscoveryNewerHistoryClient(StubClient):
    def iter_crm_chat_refs(self) -> list[ChatReference]:
        return [ChatReference(77, datetime(2026, 7, 20, 9, tzinfo=UTC), "crm_activity")]

    def iter_recent_chat_refs(self, page_size: int) -> list[ChatReference]:
        return []

    def get_messages(self, chat_id: int) -> list[OpenLineMessage]:
        raise AssertionError(f"CRM-only chat used dialog messages: {chat_id}")

    def get_history(self, chat_id: int) -> list[OpenLineMessage]:
        return [
            OpenLineMessage(
                1,
                501,
                "Ada",
                "A message newer than stale CRM activity metadata",
                datetime(2026, 7, 20, 10, 5, tzinfo=UTC),
                False,
            )
        ]


def test_connector_advances_watermark_to_newest_discovery_or_message_timestamp(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.connectors.bitrix_openlines.connector.run_extraction_batch",
        lambda texts: [
            {
                "persons": [],
                "transactions": [],
                "summary": "Conversation with a newer message.",
                "confidence": 0.95,
            }
            for _ in texts
        ],
    )
    watermark = StubWatermark()
    connector = BitrixOpenLinesConnector(
        NewerMessageClient(),
        watermark,
        BitrixOpenLinesConfig(entity_by_config_id={"46": "speedzone"}),
        mode="api",
    )

    list(connector.fetch_records())
    connector.commit_watermark()

    assert watermark.committed == datetime(2026, 7, 20, 9, tzinfo=UTC)


def test_connector_does_not_skip_new_history_behind_stale_discovery_timestamp(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.connectors.bitrix_openlines.connector.run_extraction_batch",
        lambda texts: [
            {
                "persons": [{"name": "Ada", "phone": "+6591234567"}],
                "transactions": [],
                "summary": "New history behind stale discovery metadata.",
                "confidence": 0.95,
            }
            for _ in texts
        ],
    )
    watermark = StubWatermark(datetime(2026, 7, 20, 10, tzinfo=UTC))
    connector = BitrixOpenLinesConnector(
        StaleDiscoveryNewerHistoryClient(),
        watermark,
        BitrixOpenLinesConfig(
            entity_by_config_id={"46": "speedzone"},
            incremental_overlap_seconds=300,
        ),
        mode="api",
    )

    records = list(connector.fetch_records())
    connector.commit_watermark()

    assert len(records) == 1
    assert watermark.committed == datetime(2026, 7, 20, 10, 5, tzinfo=UTC)


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


class HistoricalOnlyClient(StubClient):
    def __init__(self) -> None:
        self.history_chat_ids: list[int] = []

    def iter_recent_chat_refs(self, page_size: int) -> list[ChatReference]:
        return []

    def get_messages(self, chat_id: int) -> list[OpenLineMessage]:
        raise AssertionError(f"CRM-only chat used dialog messages: {chat_id}")

    def get_history(self, chat_id: int) -> list[OpenLineMessage]:
        self.history_chat_ids.append(chat_id)
        return super().get_messages(chat_id)


def test_connector_uses_openline_history_for_crm_only_chats(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.connectors.bitrix_openlines.connector.run_extraction_batch",
        lambda texts: [
            {
                "persons": [{"name": "Ada", "phone": "+6591234567"}],
                "transactions": [],
                "summary": "Historical chat.",
                "confidence": 0.95,
            }
            for _ in texts
        ],
    )
    client = HistoricalOnlyClient()
    connector = BitrixOpenLinesConnector(
        client,
        StubWatermark(),
        BitrixOpenLinesConfig(entity_by_config_id={"46": "speedzone"}),
        mode="backfill",
    )

    records = list(connector.fetch_records())

    assert len(records) == 1
    assert client.history_chat_ids == [77]


class TwoChatClient(StubClient):
    def list_active_configs(self) -> list[OpenLineConfig]:
        return [OpenLineConfig("46", "Speedzone: FB"), OpenLineConfig("47", "Eko: FB")]

    def iter_crm_chat_refs(self) -> list[ChatReference]:
        return []

    def iter_recent_chat_refs(self, page_size: int) -> list[ChatReference]:
        return [
            ChatReference(77, datetime(2026, 7, 20, 8, tzinfo=UTC), "recent_dialog"),
            ChatReference(78, datetime(2026, 7, 20, 9, tzinfo=UTC), "recent_dialog"),
        ]

    def get_dialog(self, chat_id: int) -> DialogMetadata:
        return DialogMetadata(chat_id, "46" if chat_id == 77 else "47", "facebook")


def test_connector_reuses_character_and_count_bounded_llm_batching(
    monkeypatch: MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def extract(texts: list[str]) -> list[dict[str, object]]:
        calls.append(texts)
        return [
            {
                "persons": [{"name": f"Customer {index}", "phone": f"+659123456{index}"}],
                "transactions": [],
                "summary": "Customer conversation.",
                "confidence": 0.95,
            }
            for index, _text in enumerate(texts, start=1)
        ]

    monkeypatch.setattr(
        "src.connectors.bitrix_openlines.connector.run_extraction_batch",
        extract,
    )
    monkeypatch.setattr(
        "src.connectors.bitrix_openlines.connector.chat_batch_max_chars",
        lambda: 10_000,
    )
    monkeypatch.setattr(
        "src.connectors.bitrix_openlines.connector.chat_batch_size",
        lambda: 6,
    )
    connector = BitrixOpenLinesConnector(
        TwoChatClient(),
        StubWatermark(),
        BitrixOpenLinesConfig(entity_by_config_id={"46": "speedzone", "47": "eko"}),
        mode="api",
    )

    records = list(connector.fetch_records())

    assert [len(call) for call in calls] == [2]
    assert [record["raw_payload"]["tenant"] for record in records] == ["speedzone", "eko"]


class ManyChatClient(StubClient):
    def __init__(self, total: int) -> None:
        self.total = total
        self.fetched_chat_ids: list[int] = []

    def iter_crm_chat_refs(self) -> list[ChatReference]:
        return []

    def iter_recent_chat_refs(self, page_size: int) -> list[ChatReference]:
        return [
            ChatReference(
                chat_id,
                datetime(2026, 7, 20, 8, chat_id, tzinfo=UTC),
                "recent_dialog",
            )
            for chat_id in range(1, self.total + 1)
        ]

    def get_messages(self, chat_id: int) -> list[OpenLineMessage]:
        self.fetched_chat_ids.append(chat_id)
        return [
            OpenLineMessage(
                chat_id,
                500 + chat_id,
                f"Customer {chat_id}",
                f"Conversation {chat_id}",
                datetime(2026, 7, 20, 8, chat_id, tzinfo=UTC),
                False,
            )
        ]


def test_connector_extracts_bounded_batches_before_fetching_every_chat(
    monkeypatch: MonkeyPatch,
) -> None:
    client = ManyChatClient(total=12)
    fetched_counts_at_extraction: list[int] = []
    extraction_batch_sizes: list[int] = []
    max_chars = 200

    def extract(texts: list[str]) -> list[dict[str, object]]:
        fetched_counts_at_extraction.append(len(client.fetched_chat_ids))
        extraction_batch_sizes.append(len(texts))
        assert len(texts) <= 2
        assert sum(len(text) for text in texts) <= max_chars
        return [
            {
                "persons": [{"name": f"Customer {index}"}],
                "transactions": [],
                "summary": "Conversation.",
                "confidence": 0.95,
            }
            for index, _text in enumerate(texts, start=1)
        ]

    monkeypatch.setattr(
        "src.connectors.bitrix_openlines.connector.run_extraction_batch",
        extract,
    )
    monkeypatch.setattr(
        "src.connectors.bitrix_openlines.connector.chat_batch_max_chars",
        lambda: max_chars,
    )
    monkeypatch.setattr(
        "src.connectors.bitrix_openlines.connector.chat_batch_size",
        lambda: 2,
    )
    connector = BitrixOpenLinesConnector(
        client,
        StubWatermark(),
        BitrixOpenLinesConfig(entity_by_config_id={"46": "speedzone"}),
        mode="api",
    )

    records = list(connector.fetch_records())

    assert fetched_counts_at_extraction[0] == 2
    assert extraction_batch_sizes == [2, 2, 2, 2, 2, 2]
    assert [record["raw_payload"]["bitrix_chat_id_numeric"] for record in records] == list(
        range(1, 13)
    )


def test_connector_applies_company_internal_and_file_exclusions(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.connectors.bitrix_openlines.connector.run_extraction_batch",
        lambda texts: [
            {
                "persons": [
                    {"name": "Company", "phone": "+6560000000"},
                    {"name": "Internal Person", "email": "internal@example.test"},
                    {"name": "File Excluded", "phone": "+6580000000"},
                    {"name": "Customer", "phone": "+6591234567"},
                ],
                "transactions": [],
                "summary": "Customer conversation.",
                "confidence": 0.95,
            }
            for _ in texts
        ],
    )
    connector = BitrixOpenLinesConnector(
        StubClient(),
        StubWatermark(),
        BitrixOpenLinesConfig(entity_by_config_id={"46": "speedzone"}),
        mode="api",
        company_mobile_numbers=["+6560000000"],
        company_email_addresses=["internal@example.test"],
        internal_person_names=["Internal Person"],
        file_exclusions=ExclusionFile(names=["File Excluded"]),
    )

    records = list(connector.fetch_records())

    assert len(records) == 1
    assert records[0]["attributes"] == {"full_name": "Customer"}


class InaccessibleDialogClient(StubClient):
    def get_dialog(self, chat_id: int) -> DialogMetadata:
        raise RuntimeError("private upstream response")


def test_connector_attributes_inaccessible_dialog_to_chat_and_all_discoveries() -> None:
    watermark = StubWatermark()
    connector = BitrixOpenLinesConnector(
        InaccessibleDialogClient(),
        watermark,
        BitrixOpenLinesConfig(entity_by_config_id={"46": "speedzone"}),
        mode="api",
    )

    with pytest.raises(RuntimeError) as exc_info:
        list(connector.fetch_records())

    error = str(exc_info.value)
    assert "chat 77" in error
    assert "crm_activity,recent_dialog" in error
    assert "dialog retrieval failed" in error
    assert "private upstream response" not in error
    assert exc_info.value.__cause__ is None
    connector.commit_watermark()
    assert watermark.committed is None


class InaccessibleHistoryClient(StubClient):
    def iter_recent_chat_refs(self, page_size: int) -> list[ChatReference]:
        return []

    def get_history(self, chat_id: int) -> list[OpenLineMessage]:
        raise RuntimeError("private message body")


def test_connector_attributes_inaccessible_history_without_exposing_upstream_text() -> None:
    watermark = StubWatermark()
    connector = BitrixOpenLinesConnector(
        InaccessibleHistoryClient(),
        watermark,
        BitrixOpenLinesConfig(entity_by_config_id={"46": "speedzone"}),
        mode="backfill",
    )

    with pytest.raises(RuntimeError) as exc_info:
        list(connector.fetch_records())

    error = str(exc_info.value)
    assert "chat 77" in error
    assert "crm_activity" in error
    assert "history retrieval failed" in error
    assert "private message body" not in error
    assert exc_info.value.__cause__ is None
    connector.commit_watermark()
    assert watermark.committed is None


class InaccessibleMessagesClient(StubClient):
    def get_messages(self, chat_id: int) -> list[OpenLineMessage]:
        raise RuntimeError("private recent message body")


def test_connector_attributes_inaccessible_recent_messages_without_upstream_text() -> None:
    watermark = StubWatermark()
    connector = BitrixOpenLinesConnector(
        InaccessibleMessagesClient(),
        watermark,
        BitrixOpenLinesConfig(entity_by_config_id={"46": "speedzone"}),
        mode="api",
    )

    with pytest.raises(RuntimeError) as exc_info:
        list(connector.fetch_records())

    error = str(exc_info.value)
    assert "chat 77" in error
    assert "crm_activity,recent_dialog" in error
    assert "message retrieval failed" in error
    assert "private recent message body" not in error
    assert exc_info.value.__cause__ is None
    connector.commit_watermark()
    assert watermark.committed is None
