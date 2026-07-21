from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from pytest import MonkeyPatch
from src.connectors.chat_helpers import ExtractionResult
from src.connectors.whatsadmin_api.connector import WhatsAdminChatApiConnector
from src.connectors.whatsadmin_api.credentials import WhatsAdminEntity
from src.connectors.whatsadmin_api.models import ChatPage, SessionRow
from src.connectors.whatsapp import connector as whatsapp_connector
from src.connectors.whatsapp.connector import _ChatBundle
from src.models import JsonValue


class StubClient:
    closed = False
    entity_key: WhatsAdminEntity = "eko"

    def iter_sessions(self) -> Iterator[SessionRow]:
        yield SessionRow.model_validate(
            {
                "id": "ses_1",
                "orgId": "org_1",
                "orgName": "EkoLife SG",
                "whatsappUserId": "6590000000@c.us",
                "expectedPhoneNumber": "6590000000",
                "updatedAt": "2026-07-17T05:00:00Z",
            }
        )

    def iter_chat_pages(self, session_id: str, changed_since: str | None) -> Iterator[ChatPage]:
        assert session_id == "ses_1"
        assert changed_since == "2026-07-16T00:00:00+00:00"
        yield ChatPage.model_validate(
            {
                "success": True,
                "data": [
                    {
                        "chatId": "6581111111@c.us",
                        "chatName": "Alice",
                        "sessionId": "ses_1",
                        "whatsappUserId": "6590000000@c.us",
                        "changedAt": "2026-07-17T05:30:00Z",
                        "participants": [
                            {
                                "jid": "6581111111@c.us",
                                "phone": "6581111111",
                                "name": "Alice",
                                "role": "chat",
                            }
                        ],
                        "messages": [
                            {
                                "fromId": "6581111111@c.us",
                                "toId": "6590000000@c.us",
                                "authorId": None,
                                "body": "Hello",
                                "timestamp": "2026-07-17T05:20:00Z",
                                "fromMe": False,
                            }
                        ],
                    }
                ],
                "meta": {
                    "timestamp": "2026-07-17T05:31:00Z",
                    "requestId": "req_1",
                    "snapshotAt": "2026-07-17T05:31:00Z",
                    "pagination": {"hasMore": False},
                },
            }
        )

    def close(self) -> None:
        self.closed = True


class StubWatermark:
    def __init__(self) -> None:
        self.committed: dict[tuple[WhatsAdminEntity, str], datetime] = {}

    def get(self, entity_key: WhatsAdminEntity, session_id: str) -> datetime | None:
        assert entity_key == "eko"
        assert session_id == "ses_1"
        return datetime(2026, 7, 16, tzinfo=UTC)

    def set(
        self,
        entity_key: WhatsAdminEntity,
        session_id: str,
        value: datetime,
    ) -> None:
        self.committed[(entity_key, session_id)] = value

    def close(self) -> None:
        pass


def test_api_connector_converts_bundles_for_existing_extraction_pipeline(
    monkeypatch: MonkeyPatch,
) -> None:
    captured: list[_ChatBundle] = []

    def fake_process(
        bundles: list[_ChatBundle],
        *,
        fail_on_extraction_error: bool = False,
    ) -> Iterator[dict[str, JsonValue]]:
        assert fail_on_extraction_error is True
        captured.extend(bundles)
        yield {"source_record_id": "record-1"}

    monkeypatch.setattr(
        "src.connectors.whatsadmin_api.connector.process_whatsapp_bundles",
        fake_process,
    )
    client = StubClient()
    watermark = StubWatermark()
    connector = WhatsAdminChatApiConnector((client,), watermark)

    records = list(connector.fetch_records())

    assert records == [{"source_record_id": "record-1"}]
    bundle = captured[0]
    assert bundle.tenant == "eko"
    assert bundle.msg_text == "[2026-07-17 05:20:00] Alice (6581111111): Hello"
    assert bundle.observed_at == "2026-07-17T05:20:00+00:00"
    connector.commit_watermark()
    assert watermark.committed[("eko", "ses_1")] == datetime(2026, 7, 17, 5, 31, tzinfo=UTC)


def test_api_connector_processes_each_page_without_buffering_all_pages(
    monkeypatch: MonkeyPatch,
) -> None:
    calls: list[int] = []

    class TwoPageClient(StubClient):
        def iter_chat_pages(
            self,
            session_id: str,
            changed_since: str | None,
        ) -> Iterator[ChatPage]:
            pages = list(super().iter_chat_pages(session_id, changed_since))
            yield pages[0]
            yield pages[0]

    def fake_process(
        bundles: list[_ChatBundle],
        *,
        fail_on_extraction_error: bool = False,
    ) -> Iterator[dict[str, JsonValue]]:
        assert fail_on_extraction_error is True
        calls.append(len(bundles))
        yield {"source_record_id": f"record-{len(calls)}"}

    monkeypatch.setattr(
        "src.connectors.whatsadmin_api.connector.process_whatsapp_bundles",
        fake_process,
    )
    connector = WhatsAdminChatApiConnector((TwoPageClient(),), StubWatermark())

    records = list(connector.fetch_records())

    assert calls == [1, 1]
    assert [record["source_record_id"] for record in records] == ["record-1", "record-2"]


@pytest.mark.parametrize(
    ("field", "mismatched_value", "error_match"),
    [
        ("session_id", "another_session", "chat session"),
        ("whatsapp_user_id", "another_user@c.us", "WhatsApp user"),
    ],
)
def test_connector_rejects_chat_identity_mismatch_without_advancing_watermark(
    monkeypatch: MonkeyPatch,
    field: str,
    mismatched_value: str,
    error_match: str,
) -> None:
    class MismatchedChatClient(StubClient):
        def iter_chat_pages(
            self,
            session_id: str,
            changed_since: str | None,
        ) -> Iterator[ChatPage]:
            page = next(super().iter_chat_pages(session_id, changed_since))
            chat = page.data[0].model_copy(update={field: mismatched_value})
            yield page.model_copy(update={"data": [chat]})

    monkeypatch.setattr(
        "src.connectors.whatsadmin_api.connector.process_whatsapp_bundles",
        lambda _bundles, *, fail_on_extraction_error: iter(()),
    )
    watermark = StubWatermark()
    connector = WhatsAdminChatApiConnector((MismatchedChatClient(),), watermark)

    with pytest.raises(RuntimeError, match=error_match):
        list(connector.fetch_records())
    connector.commit_watermark()

    assert watermark.committed == {}


@pytest.mark.parametrize(
    ("legacy_entity", "expected_ids"),
    [
        (
            None,
            [
                "whatsapp-chat-eko-ses_1-6581111111@c.us-person-1",
                "whatsapp-chat-speedzone-ses_1-6581111111@c.us-person-1",
            ],
        ),
        (
            "eko",
            [
                "whatsapp-chat-ses_1-6581111111@c.us-person-1",
                "whatsapp-chat-speedzone-ses_1-6581111111@c.us-person-1",
            ],
        ),
        (
            "speedzone",
            [
                "whatsapp-chat-eko-ses_1-6581111111@c.us-person-1",
                "whatsapp-chat-ses_1-6581111111@c.us-person-1",
            ],
        ),
    ],
)
def test_source_record_ids_preserve_only_the_configured_legacy_entity(
    monkeypatch: MonkeyPatch,
    legacy_entity: WhatsAdminEntity | None,
    expected_ids: list[str],
) -> None:
    monkeypatch.setattr(whatsapp_connector, "extraction_method_label", lambda: "llm:test")
    client = StubClient()
    connector = WhatsAdminChatApiConnector(
        (client,),
        StubWatermark(),
        legacy_entity=legacy_entity,
    )
    session = next(client.iter_sessions())
    page = next(client.iter_chat_pages("ses_1", "2026-07-16T00:00:00+00:00"))
    extraction = ExtractionResult(
        persons=[],
        possible_persons=[
            {
                "name": "Alice",
                "identifiers": [],
                "weak_identifiers": [],
                "confidence": 0.9,
            }
        ],
        transactions=[],
        chat_members=[],
        inquiries=[],
        strong_identifiers=[],
        weak_identifiers=[],
        summary="Customer conversation",
        customer_sentiment="neutral",
        confidence=0.9,
    )

    bundles = [
        connector._bundles(session, entity_key, page)[0] for entity_key in ("eko", "speedzone")
    ]
    source_record_ids = [
        whatsapp_connector._build_envelopes(bundle=bundle, extraction=extraction)[0][
            "source_record_id"
        ]
        for bundle in bundles
    ]

    assert source_record_ids == expected_ids


def test_default_connector_keeps_same_session_id_isolated_by_entity(
    monkeypatch: MonkeyPatch,
) -> None:
    snapshots: dict[WhatsAdminEntity, datetime] = {
        "eko": datetime(2026, 7, 17, 6, 0, tzinfo=UTC),
        "speedzone": datetime(2026, 7, 17, 7, 0, tzinfo=UTC),
    }
    changed_since_calls: list[tuple[WhatsAdminEntity, str, str | None]] = []

    class TenantClient:
        def __init__(self, entity_key: WhatsAdminEntity, org_name: str) -> None:
            self.entity_key = entity_key
            self._org_name = org_name

        def iter_sessions(self) -> Iterator[SessionRow]:
            yield SessionRow.model_validate(
                {
                    "id": "shared_session",
                    "orgId": f"org_{self.entity_key}",
                    "orgName": self._org_name,
                    "whatsappUserId": f"{self.entity_key}@c.us",
                    "expectedPhoneNumber": None,
                    "updatedAt": "2026-07-17T05:00:00Z",
                }
            )

        def iter_chat_pages(
            self,
            session_id: str,
            changed_since: str | None,
        ) -> Iterator[ChatPage]:
            changed_since_calls.append((self.entity_key, session_id, changed_since))
            yield ChatPage.model_validate(
                {
                    "success": True,
                    "data": [],
                    "meta": {
                        "timestamp": "2026-07-17T08:00:00Z",
                        "requestId": f"req_{self.entity_key}",
                        "snapshotAt": snapshots[self.entity_key].isoformat(),
                        "pagination": {"hasMore": False},
                    },
                }
            )

        def close(self) -> None:
            pass

    class TenantWatermark:
        def __init__(self) -> None:
            self.committed: dict[tuple[WhatsAdminEntity, str], datetime] = {}

        def get(self, entity_key: WhatsAdminEntity, session_id: str) -> datetime | None:
            assert session_id == "shared_session"
            hour = 1 if entity_key == "eko" else 2
            return datetime(2026, 7, 16, hour, tzinfo=UTC)

        def set(
            self,
            entity_key: WhatsAdminEntity,
            session_id: str,
            value: datetime,
        ) -> None:
            self.committed[(entity_key, session_id)] = value

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "src.connectors.whatsadmin_api.connector.process_whatsapp_bundles",
        lambda _bundles, *, fail_on_extraction_error: iter(()),
    )
    watermark = TenantWatermark()
    connector = WhatsAdminChatApiConnector(
        (
            TenantClient("eko", "EkoLife SG"),
            TenantClient("speedzone", "SpeedZone"),
        ),
        watermark,
    )

    assert list(connector.fetch_records()) == []
    connector.commit_watermark()

    assert changed_since_calls == [
        ("eko", "shared_session", "2026-07-16T01:00:00+00:00"),
        ("speedzone", "shared_session", "2026-07-16T02:00:00+00:00"),
    ]
    assert watermark.committed == {
        ("eko", "shared_session"): snapshots["eko"],
        ("speedzone", "shared_session"): snapshots["speedzone"],
    }


def test_connector_rejects_session_from_another_entity() -> None:
    class MismatchedClient(StubClient):
        def iter_sessions(self) -> Iterator[SessionRow]:
            session = next(super().iter_sessions())
            yield session.model_copy(update={"org_name": "SpeedZone"})

    connector = WhatsAdminChatApiConnector((MismatchedClient(),), StubWatermark())

    with pytest.raises(RuntimeError, match="organization.*entity"):
        list(connector.fetch_records())
