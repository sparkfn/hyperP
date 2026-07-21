from __future__ import annotations

from datetime import UTC, datetime

from src.connectors.bitrix_openlines.discovery import discover_chats
from src.connectors.bitrix_openlines.models import ChatReference, CrmOwnerReference


class StubDiscoveryClient:
    def iter_crm_chat_refs(self) -> list[ChatReference]:
        return [ChatReference(10, None, "crm_activity")]

    def iter_recent_chat_refs(self, page_size: int) -> list[ChatReference]:
        assert page_size == 25
        return [
            ChatReference(10, datetime(2026, 7, 20, tzinfo=UTC), "recent_dialog"),
            ChatReference(11, datetime(2026, 7, 19, tzinfo=UTC), "recent_dialog"),
        ]


def test_hybrid_discovery_deduplicates_chat_ids_and_retains_provenance() -> None:
    discovered = discover_chats(StubDiscoveryClient(), recent_page_size=25)

    assert [(item.chat_id, item.discovery) for item in discovered] == [
        (10, "crm_activity,recent_dialog"),
        (11, "recent_dialog"),
    ]


class ConflictingTimestampClient:
    def iter_crm_chat_refs(self) -> list[ChatReference]:
        return [ChatReference(10, datetime(2026, 7, 20, 10, tzinfo=UTC), "crm_activity")]

    def iter_recent_chat_refs(self, page_size: int) -> list[ChatReference]:
        return [ChatReference(10, datetime(2026, 7, 20, 9, tzinfo=UTC), "recent_dialog")]


def test_hybrid_discovery_preserves_newest_changed_timestamp() -> None:
    discovered = discover_chats(ConflictingTimestampClient(), recent_page_size=25)

    assert discovered[0].changed_at == datetime(2026, 7, 20, 10, tzinfo=UTC)


def test_chat_reference_has_typed_crm_provenance_fields() -> None:
    assert {
        "activity_ids",
        "crm_owner_references",
        "provider_references",
    }.issubset(ChatReference.__dataclass_fields__)


class ProvenanceDiscoveryClient:
    def iter_crm_chat_refs(self) -> list[ChatReference]:
        return [
            ChatReference(
                10,
                datetime(2026, 7, 20, 8, tzinfo=UTC),
                "crm_activity",
                activity_ids=("900",),
                crm_owner_references=(CrmOwnerReference("deal", 501),),
                provider_references=({"CHAT_ID": "10"},),
            ),
            ChatReference(
                10,
                datetime(2026, 7, 20, 10, tzinfo=UTC),
                "crm_activity",
                activity_ids=("901",),
                crm_owner_references=(CrmOwnerReference("contact", 502),),
                provider_references=({"IM": [{"id": "chat10"}]},),
            ),
        ]

    def iter_recent_chat_refs(self, page_size: int) -> list[ChatReference]:
        return [ChatReference(10, None, "recent_dialog")]


def test_hybrid_discovery_unions_typed_crm_provenance_for_duplicate_chat() -> None:
    reference = discover_chats(ProvenanceDiscoveryClient(), recent_page_size=25)[0]

    assert reference.activity_ids == ("900", "901")
    assert reference.crm_owner_references == (
        CrmOwnerReference("deal", 501),
        CrmOwnerReference("contact", 502),
    )
    assert reference.provider_references == (
        {"CHAT_ID": "10"},
        {"IM": [{"id": "chat10"}]},
    )
