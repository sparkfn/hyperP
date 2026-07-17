from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

from pytest import MonkeyPatch
from src.connectors.whatsadmin_api.connector import WhatsAdminChatApiConnector
from src.connectors.whatsadmin_api.models import ChatPage, SessionRow
from src.connectors.whatsapp.connector import _ChatBundle
from src.models import JsonValue


class StubClient:
    closed = False

    def iter_sessions(self) -> Iterator[SessionRow]:
        yield SessionRow.model_validate(
            {
                "id": "ses_1",
                "orgId": "org_1",
                "orgName": "Fundbox",
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
    committed: dict[str, datetime] = {}

    def get(self, session_id: str) -> datetime | None:
        assert session_id == "ses_1"
        return datetime(2026, 7, 16, tzinfo=UTC)

    def set(self, session_id: str, value: datetime) -> None:
        self.committed[session_id] = value

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
    connector = WhatsAdminChatApiConnector(client, watermark)

    records = list(connector.fetch_records())

    assert records == [{"source_record_id": "record-1"}]
    bundle = captured[0]
    assert bundle.tenant == "fundbox"
    assert bundle.msg_text == "[2026-07-17 05:20:00] Alice (6581111111): Hello"
    assert bundle.observed_at == "2026-07-17T05:20:00+00:00"
    connector.commit_watermark()
    assert watermark.committed["ses_1"] == datetime(2026, 7, 17, 5, 31, tzinfo=UTC)


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
    connector = WhatsAdminChatApiConnector(TwoPageClient(), StubWatermark())

    records = list(connector.fetch_records())

    assert calls == [1, 1]
    assert [record["source_record_id"] for record in records] == ["record-1", "record-2"]
