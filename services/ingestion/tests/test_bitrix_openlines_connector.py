from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from pytest import MonkeyPatch
from src.connectors.bitrix_openlines.connector import BitrixOpenLinesConnector
from src.connectors.bitrix_openlines.models import (
    ChatReference,
    CrmDiscoveryPage,
    CrmOwnerReference,
    DialogMetadata,
    OpenLineConfig,
    OpenLineMessage,
)
from src.connectors.bitrix_openlines.watermark import BackfillCheckpoint
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

    def iter_crm_chat_ref_pages(self) -> Iterator[list[ChatReference]]:
        yield self.iter_crm_chat_refs()

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


def test_full_api_extraction_does_not_load_or_commit_incremental_watermark(
    monkeypatch: MonkeyPatch,
) -> None:
    class FullWatermark(StubWatermark):
        def get(self, *, overlap_seconds: int) -> datetime | None:
            raise AssertionError("full extraction must not load a watermark")

        def set(self, value: datetime) -> None:
            raise AssertionError("full extraction must not commit a watermark")

    monkeypatch.setattr(
        "src.connectors.bitrix_openlines.connector.run_extraction_batch",
        lambda texts: [
            {
                "persons": [],
                "transactions": [],
                "summary": "Full extraction.",
                "confidence": 0.95,
            }
            for _ in texts
        ],
    )
    connector = BitrixOpenLinesConnector(
        StubClient(),
        FullWatermark(),
        BitrixOpenLinesConfig(entity_by_config_id={"46": "speedzone"}),
        mode="api",
        incremental=False,
    )

    list(connector.fetch_records())
    connector.commit_watermark()


def test_backfill_resumes_from_and_advances_persisted_crm_page() -> None:
    class ResumableClient(StubClient):
        def __init__(self) -> None:
            self.starts: list[int] = []

        def iter_recent_chat_refs(self, page_size: int) -> list[ChatReference]:
            return []

        def iter_crm_chat_ref_pages(self) -> Iterator[list[ChatReference]]:
            return
            yield

        def iter_crm_discovery_pages(self, *, start: int = 0) -> Iterator[CrmDiscoveryPage]:
            self.starts.append(start)
            yield CrmDiscoveryPage([], None)

    class BackfillWatermark(StubWatermark):
        def __init__(self) -> None:
            super().__init__()
            self.checkpoint: BackfillCheckpoint | None = BackfillCheckpoint(crm_start=50)
            self.cleared = False

        def get_backfill_checkpoint(self) -> BackfillCheckpoint | None:
            return self.checkpoint

        def set_backfill_checkpoint(self, checkpoint: BackfillCheckpoint) -> None:
            self.checkpoint = checkpoint

        def clear_backfill_checkpoint(self) -> None:
            self.cleared = True
            self.checkpoint = None

    client = ResumableClient()
    watermark = BackfillWatermark()
    connector = BitrixOpenLinesConnector(
        client,
        watermark,
        BitrixOpenLinesConfig(entity_by_config_id={"46": "speedzone"}),
        mode="backfill",
    )

    assert list(connector.fetch_records()) == []
    assert client.starts == [50]
    assert watermark.checkpoint == BackfillCheckpoint(crm_start=None)
    connector.commit_watermark()
    assert watermark.cleared is True


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


def test_incremental_api_emits_deal_history_call_and_chat_activity_references(
    monkeypatch: MonkeyPatch,
) -> None:
    from src.connectors.bitrix_openlines.models import CrmActivity, CrmContact, CrmDeal

    class CrmStubClient(StubClient):
        def iter_crm_deals(self) -> Iterator[CrmDeal]:
            yield self.get_deal(501)

        def get_deal(self, deal_id: int) -> CrmDeal:
            assert deal_id == 501
            return CrmDeal(
                id="501",
                title="Ada service",
                category_id="2",
                stage_id="C2:NEW",
                observed_at=datetime(2026, 7, 20, 9, tzinfo=UTC),
                primary_contact=CrmContact(
                    id="400",
                    full_name="Ada Lovelace",
                    phones=("+6591234567",),
                ),
                contacts=(
                    CrmContact(
                        id="400",
                        full_name="Ada Lovelace",
                        phones=("+6591234567",),
                    ),
                ),
                contact_count=1,
                has_ambiguous_contacts=False,
                raw_payload={"ID": "501"},
            )

        def iter_crm_activities(self) -> list[CrmActivity]:
            return [
                CrmActivity(
                    id="900",
                    owner_type="deal",
                    owner_id="501",
                    history_kind="openlines_session",
                    subject="Open Lines session",
                    observed_at=datetime(2026, 7, 20, 9, tzinfo=UTC),
                    start_at=None,
                    end_at=None,
                    duration_seconds=None,
                    direction=None,
                    outcome=None,
                    is_call=False,
                    raw_payload={"ID": "900", "PROVIDER_PARAMS": {"CHAT_ID": "77"}},
                ),
                CrmActivity(
                    id="901",
                    owner_type="deal",
                    owner_id="501",
                    history_kind="call",
                    subject="Follow-up call",
                    observed_at=datetime(2026, 7, 20, 10, tzinfo=UTC),
                    start_at=datetime(2026, 7, 20, 10, tzinfo=UTC),
                    end_at=datetime(2026, 7, 20, 10, 5, tzinfo=UTC),
                    duration_seconds=300,
                    direction="2",
                    outcome="Y",
                    is_call=True,
                    raw_payload={"ID": "901", "TYPE_ID": "2"},
                ),
            ]

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
    connector = BitrixOpenLinesConnector(
        CrmStubClient(),
        StubWatermark(),
        BitrixOpenLinesConfig(
            entity_by_config_id={"46": "speedzone"},
            entity_by_crm_category_id={"2": "speedzone"},
        ),
        mode="api",
        incremental=True,
    )

    records = list(connector.fetch_records())

    assert [record["record_type"] for record in records] == [
        "crm_deal",
        "crm_history",
        "crm_history",
        "call",
        "conversation",
    ]
    assert records[2]["raw_payload"]["crm_activity_id"] == "901"
    assert records[1]["raw_payload"]["bitrix_chat_id_numeric"] == 77
    assert records[3]["raw_payload"]["duration_seconds"] == 300
    assert "parent_ref" not in records[4]
    assert records[4]["raw_payload"]["crm_activity_ids"] == ["900"]


def test_non_incremental_api_does_not_request_crm_enrichment(
    monkeypatch: MonkeyPatch,
) -> None:
    class EnrichmentForbiddenClient(StubClient):
        def iter_crm_deals(self) -> list[object]:
            raise AssertionError("unexpected CRM deal discovery")

        def iter_crm_activities(self) -> list[object]:
            raise AssertionError("unexpected CRM activity discovery")

    monkeypatch.setattr(
        "src.connectors.bitrix_openlines.connector.run_extraction_batch",
        lambda texts: [
            {"persons": [], "transactions": [], "summary": "Full run.", "confidence": 0.95}
            for _ in texts
        ],
    )
    connector = BitrixOpenLinesConnector(
        EnrichmentForbiddenClient(),
        StubWatermark(),
        BitrixOpenLinesConfig(entity_by_config_id={"46": "speedzone"}),
        mode="api",
        incremental=False,
    )

    assert [record["record_type"] for record in connector.fetch_records()] == []


def test_stale_chat_still_emits_new_crm_history_without_running_chat_extraction(
    monkeypatch: MonkeyPatch,
) -> None:
    from src.connectors.bitrix_openlines.models import CrmActivity, CrmDeal

    class StaleCrmClient(StubClient):
        def iter_crm_deals(self) -> Iterator[CrmDeal]:
            yield self.get_deal(501)

        def get_deal(self, deal_id: int) -> CrmDeal:
            assert deal_id == 501
            return CrmDeal(
                id="501",
                title="Unidentified service deal",
                category_id="2",
                stage_id="NEW",
                observed_at=datetime(2026, 7, 20, 8, tzinfo=UTC),
                primary_contact=None,
                contacts=(),
                contact_count=0,
                has_ambiguous_contacts=False,
                raw_payload={"ID": "501"},
            )

        def iter_crm_activities(self) -> list[CrmActivity]:
            return [
                CrmActivity(
                    id="902",
                    owner_type="2",
                    owner_id="501",
                    history_kind="call",
                    subject="New call on an old chat",
                    observed_at=datetime(2026, 7, 20, 11, tzinfo=UTC),
                    start_at=datetime(2026, 7, 20, 11, tzinfo=UTC),
                    end_at=datetime(2026, 7, 20, 11, 1, tzinfo=UTC),
                    duration_seconds=60,
                    direction="2",
                    outcome="Y",
                    is_call=True,
                    raw_payload={"ID": "902", "TYPE_ID": "2"},
                )
            ]

    monkeypatch.setattr(
        "src.connectors.bitrix_openlines.connector.run_extraction_batch",
        lambda _texts: (_ for _ in ()).throw(
            AssertionError("stale conversations must not run chat extraction")
        ),
    )
    connector = BitrixOpenLinesConnector(
        StaleCrmClient(),
        StubWatermark(datetime(2026, 7, 20, 10, tzinfo=UTC)),
        BitrixOpenLinesConfig(
            entity_by_config_id={"46": "speedzone"},
            entity_by_crm_category_id={"2": "speedzone"},
            incremental_overlap_seconds=0,
        ),
        mode="api",
        incremental=True,
    )

    records = list(connector.fetch_records())

    assert [record["record_type"] for record in records] == [
        "crm_deal",
        "crm_history",
        "call",
    ]
    assert records[0]["identifiers"] == []
    assert records[1]["source_record_id"] == "bitrix-crm-history-902"
    assert records[2]["source_record_id"] == "bitrix-call-902"


def test_incremental_crm_emits_when_every_openlines_channel_is_excluded() -> None:
    from src.connectors.bitrix_openlines.models import CrmActivity, CrmDeal

    class IndependentCrmClient(StubClient):
        def list_active_configs(self) -> list[OpenLineConfig]:
            raise AssertionError("chat configuration must not load when none can be selected")

        def iter_crm_chat_ref_pages(self) -> Iterator[list[ChatReference]]:
            raise AssertionError("chat discovery must not run when none can be selected")

        def iter_crm_deals(self) -> Iterator[CrmDeal]:
            yield CrmDeal(
                id="701",
                title="Deal without a selected chat",
                category_id="2",
                stage_id="NEW",
                observed_at=datetime(2026, 7, 20, 8, tzinfo=UTC),
                primary_contact=None,
                contacts=(),
                contact_count=0,
                has_ambiguous_contacts=False,
                raw_payload={"ID": "701"},
            )

        def iter_crm_activities(self) -> list[CrmActivity]:
            return [
                CrmActivity(
                    id="990",
                    owner_type="2",
                    owner_id="701",
                    history_kind="call",
                    subject="Independent call",
                    observed_at=datetime(2026, 7, 20, 9, tzinfo=UTC),
                    start_at=None,
                    end_at=None,
                    duration_seconds=None,
                    direction=None,
                    outcome=None,
                    is_call=True,
                    raw_payload={"ID": "990", "TYPE_ID": "2"},
                )
            ]

    connector = BitrixOpenLinesConnector(
        IndependentCrmClient(),
        StubWatermark(),
        BitrixOpenLinesConfig(
            included_channel_types=[],
            entity_by_config_id={"46": "speedzone"},
            entity_by_crm_category_id={"2": "speedzone"},
        ),
        mode="api",
        incremental=True,
    )

    records = list(connector.fetch_records())

    assert [record["record_type"] for record in records] == [
        "crm_deal",
        "crm_history",
        "call",
    ]
    assert records[0]["source_record_id"] == "bitrix-crm-deal-701"
    assert records[0]["entity_key"] == "speedzone"
    assert records[1]["source_record_id"] == "bitrix-crm-history-990"
    assert records[1]["raw_payload"]["bitrix_chat_id_numeric"] is None
    assert records[2]["source_record_id"] == "bitrix-call-990"
    assert connector._counters.crm_deals_scanned == 1
    assert connector._counters.crm_activities_scanned == 1
    assert connector._counters.crm_activities_skipped_missing_deal == 0
    assert connector._counters.chats_skipped_by_config == 0


def test_incremental_crm_assigns_each_deal_and_activity_to_its_category_entity() -> None:
    from src.connectors.bitrix_openlines.models import CrmActivity, CrmDeal

    class MultiEntityCrmClient(StubClient):
        def list_active_configs(self) -> list[OpenLineConfig]:
            raise AssertionError("chat configuration must not load when none can be selected")

        def iter_crm_chat_ref_pages(self) -> Iterator[list[ChatReference]]:
            raise AssertionError("chat discovery must not run when none can be selected")

        def iter_crm_deals(self) -> Iterator[CrmDeal]:
            for deal_id, category_id in (("701", "1"), ("702", "2")):
                yield CrmDeal(
                    id=deal_id,
                    title="Mapped deal",
                    category_id=category_id,
                    stage_id="NEW",
                    observed_at=datetime(2026, 7, 20, 8, tzinfo=UTC),
                    primary_contact=None,
                    contacts=(),
                    contact_count=0,
                    has_ambiguous_contacts=False,
                    raw_payload={"ID": deal_id},
                )

        def iter_crm_activities(self) -> list[CrmActivity]:
            return [
                CrmActivity(
                    id="901",
                    owner_type="deal",
                    owner_id="701",
                    history_kind="openlines_session",
                    subject="Eko activity",
                    observed_at=datetime(2026, 7, 20, 9, tzinfo=UTC),
                    start_at=None,
                    end_at=None,
                    duration_seconds=None,
                    direction=None,
                    outcome=None,
                    is_call=False,
                    raw_payload={"ID": "901"},
                ),
                CrmActivity(
                    id="902",
                    owner_type="deal",
                    owner_id="702",
                    history_kind="call",
                    subject="SpeedZone activity",
                    observed_at=datetime(2026, 7, 20, 9, tzinfo=UTC),
                    start_at=None,
                    end_at=None,
                    duration_seconds=None,
                    direction=None,
                    outcome=None,
                    is_call=True,
                    raw_payload={"ID": "902", "TYPE_ID": "2"},
                ),
            ]

    connector = BitrixOpenLinesConnector(
        MultiEntityCrmClient(),
        StubWatermark(),
        BitrixOpenLinesConfig(
            included_channel_types=[],
            entity_by_crm_category_id={"1": "eko", "2": "speedzone"},
        ),
        mode="api",
        incremental=True,
    )

    records = list(connector.fetch_records())

    assert [(record["record_type"], record["entity_key"]) for record in records] == [
        ("crm_deal", "eko"),
        ("crm_deal", "speedzone"),
        ("crm_history", "eko"),
        ("crm_history", "speedzone"),
        ("call", "speedzone"),
    ]


def test_incremental_crm_rejects_an_unmapped_deal_category() -> None:
    from src.connectors.bitrix_openlines.models import CrmActivity, CrmDeal

    class UnmappedCrmClient(StubClient):
        def iter_crm_deals(self) -> Iterator[CrmDeal]:
            yield CrmDeal(
                id="701",
                title="Unmapped deal",
                category_id="99",
                stage_id="NEW",
                observed_at=datetime(2026, 7, 20, 8, tzinfo=UTC),
                primary_contact=None,
                contacts=(),
                contact_count=0,
                has_ambiguous_contacts=False,
                raw_payload={"ID": "701"},
            )

        def iter_crm_activities(self) -> list[CrmActivity]:
            return []

    connector = BitrixOpenLinesConnector(
        UnmappedCrmClient(),
        StubWatermark(),
        BitrixOpenLinesConfig(entity_by_crm_category_id={"2": "speedzone"}),
        mode="api",
        incremental=True,
    )

    with pytest.raises(ValueError, match="category '99'.*no entity mapping"):
        list(connector.fetch_records())


def test_incremental_crm_sanitizes_unrelated_value_errors() -> None:
    class InvalidCrmResponseClient(StubClient):
        def iter_crm_deals(self) -> list[object]:
            raise ValueError("private malformed CRM response")

        def iter_crm_activities(self) -> list[object]:
            return []

    connector = BitrixOpenLinesConnector(
        InvalidCrmResponseClient(),
        StubWatermark(),
        BitrixOpenLinesConfig(entity_by_crm_category_id={"2": "speedzone"}),
        mode="api",
        incremental=True,
    )

    with pytest.raises(RuntimeError, match="Bitrix CRM detail retrieval failed") as exc_info:
        list(connector.fetch_records())

    assert "private malformed CRM response" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_conversation_preserves_multiple_crm_activity_links(
    monkeypatch: MonkeyPatch,
) -> None:
    class MultipleActivityClient(StubClient):
        def iter_crm_chat_refs(self) -> list[ChatReference]:
            return [
                ChatReference(
                    77,
                    None,
                    "crm_activity",
                    activity_ids=("900", "901"),
                )
            ]

    monkeypatch.setattr(
        "src.connectors.bitrix_openlines.connector.run_extraction_batch",
        lambda _texts: [
            {
                "persons": [{"name": "Ada", "phone": "+6591234567"}],
                "transactions": [],
                "summary": "Customer conversation.",
                "confidence": 0.95,
            }
        ],
    )
    connector = BitrixOpenLinesConnector(
        MultipleActivityClient(),
        StubWatermark(),
        BitrixOpenLinesConfig(entity_by_config_id={"46": "speedzone"}),
        mode="api",
    )

    records = list(connector.fetch_records())

    assert len(records) == 1
    assert "parent_ref" not in records[0]
    assert records[0]["raw_payload"]["crm_activity_ids"] == ["900", "901"]
    from src.connectors.fundbox.builders import compute_hash

    hash_payload = {key: value for key, value in records[0].items() if key != "record_hash"}
    assert records[0]["record_hash"] == compute_hash(hash_payload)


def test_duplicate_crm_deal_discovery_scans_activities_once() -> None:
    from src.connectors.bitrix_openlines.models import CrmActivity, CrmDeal

    class DuplicateDealClient(StubClient):
        def __init__(self) -> None:
            self.activity_scans = 0

        def iter_crm_deals(self) -> Iterator[CrmDeal]:
            deal = CrmDeal(
                id="701",
                title="Duplicate discovery",
                category_id="2",
                stage_id="NEW",
                observed_at=datetime(2026, 7, 20, 8, tzinfo=UTC),
                primary_contact=None,
                contacts=(),
                contact_count=0,
                has_ambiguous_contacts=False,
                raw_payload={"ID": "701"},
            )
            yield deal
            yield deal

        def iter_crm_activities(self) -> list[CrmActivity]:
            self.activity_scans += 1
            return []

    client = DuplicateDealClient()
    connector = BitrixOpenLinesConnector(
        client,
        StubWatermark(),
        BitrixOpenLinesConfig(entity_by_crm_category_id={"2": "speedzone"}),
        mode="api",
    )

    records = list(connector.fetch_records())

    assert [record["source_record_id"] for record in records] == ["bitrix-crm-deal-701"]
    assert client.activity_scans == 1
