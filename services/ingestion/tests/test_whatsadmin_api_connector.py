from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest
from pytest import MonkeyPatch
from src.connectors.chat_helpers import ExtractionFailure, ExtractionResult
from src.connectors.whatsadmin_api.connector import WhatsAdminChatApiConnector
from src.connectors.whatsadmin_api.credentials import WhatsAdminEntity
from src.connectors.whatsadmin_api.models import ChatPage, SessionRow
from src.connectors.whatsadmin_api.retry_queue import serialize_retry_bundle
from src.connectors.whatsadmin_api.watermark import PageCheckpoint
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


class TimeoutClient(StubClient):
    def iter_chat_pages(self, session_id: str, changed_since: str | None) -> Iterator[ChatPage]:
        _ = session_id, changed_since
        request = httpx.Request("POST", "https://whatsadmin.test/chats/query")
        raise httpx.ReadTimeout("upstream stalled", request=request)
        yield

    def failure_context(self) -> dict[str, JsonValue]:
        return {
            "upstream_resource": "chats/query",
            "upstream_session_id": "ses_1",
            "upstream_cursor": "page-4",
            "upstream_attempt": 3,
            "upstream_latency_seconds": 30.0,
        }


def test_connector_reports_session_checkpoint_when_page_fetch_times_out() -> None:
    connector = WhatsAdminChatApiConnector((TimeoutClient(),), StubWatermark())

    with pytest.raises(httpx.ReadTimeout):
        list(connector.fetch_records())

    assert connector.failure_checkpoint() == {
        "entity_key": "eko",
        "session_id": "ses_1",
        "changed_since": "2026-07-16T00:00:00+00:00",
        "cursor": "first",
        "upstream_resource": "chats/query",
        "upstream_session_id": "ses_1",
        "upstream_cursor": "page-4",
        "upstream_attempt": 3,
        "upstream_latency_seconds": 30.0,
    }


def test_connector_resumes_from_persisted_page_cursor(
    monkeypatch: MonkeyPatch,
) -> None:
    checkpoint = PageCheckpoint(
        changed_since="2026-07-16T00:00:00+00:00",
        cursor="opaque-next",
        snapshot_at=datetime(2026, 7, 17, 5, 31, tzinfo=UTC),
        complete=False,
    )

    class CheckpointWatermark(StubWatermark):
        def __init__(self) -> None:
            super().__init__()
            self.saved: list[PageCheckpoint] = []
            self.deleted = False

        def get_checkpoint(
            self,
            entity_key: WhatsAdminEntity,
            session_id: str,
        ) -> PageCheckpoint | None:
            assert (entity_key, session_id) == ("eko", "ses_1")
            return checkpoint

        def set_checkpoint(
            self,
            entity_key: WhatsAdminEntity,
            session_id: str,
            value: PageCheckpoint,
        ) -> None:
            assert (entity_key, session_id) == ("eko", "ses_1")
            self.saved.append(value)

        def delete_checkpoint(
            self,
            entity_key: WhatsAdminEntity,
            session_id: str,
        ) -> None:
            assert (entity_key, session_id) == ("eko", "ses_1")
            self.deleted = True

    class ResumeClient(StubClient):
        def __init__(self) -> None:
            self.cursors: list[str | None] = []

        def iter_chat_pages(
            self,
            session_id: str,
            changed_since: str | None,
            cursor: str | None = None,
        ) -> Iterator[ChatPage]:
            self.cursors.append(cursor)
            yield from super().iter_chat_pages(session_id, changed_since)

    monkeypatch.setattr(
        "src.connectors.whatsadmin_api.connector.process_whatsapp_bundles",
        lambda _bundles, **_kwargs: iter(()),
    )
    client = ResumeClient()
    watermark = CheckpointWatermark()
    connector = WhatsAdminChatApiConnector((client,), watermark)

    assert list(connector.fetch_records()) == []
    assert client.cursors == ["opaque-next"]
    assert watermark.saved[-1].complete is True
    connector.commit_watermark()
    assert watermark.deleted is True


def test_rejected_record_does_not_advance_page_checkpoint(
    monkeypatch: MonkeyPatch,
) -> None:
    class CheckpointWatermark(StubWatermark):
        def __init__(self) -> None:
            super().__init__()
            self.saved: list[PageCheckpoint] = []

        def get_checkpoint(
            self,
            entity_key: WhatsAdminEntity,
            session_id: str,
        ) -> PageCheckpoint | None:
            return None

        def set_checkpoint(
            self,
            entity_key: WhatsAdminEntity,
            session_id: str,
            value: PageCheckpoint,
        ) -> None:
            self.saved.append(value)

        def delete_checkpoint(
            self,
            entity_key: WhatsAdminEntity,
            session_id: str,
        ) -> None:
            return None

    monkeypatch.setattr(
        "src.connectors.whatsadmin_api.connector.process_whatsapp_bundles",
        lambda _bundles, **_kwargs: iter(
            ({"source_record_id": "record-1"},)
        ),
    )
    watermark = CheckpointWatermark()
    connector = WhatsAdminChatApiConnector((StubClient(),), watermark)
    records = connector.fetch_records()

    assert next(records) == {"source_record_id": "record-1"}
    connector.record_processed(succeeded=False)
    assert list(records) == []
    assert watermark.saved == []


def test_api_connector_converts_bundles_for_existing_extraction_pipeline(
    monkeypatch: MonkeyPatch,
) -> None:
    captured: list[_ChatBundle] = []

    def fake_process(
        bundles: list[_ChatBundle],
        *,
        on_extraction_failure: object | None = None,
    ) -> Iterator[dict[str, JsonValue]]:
        assert on_extraction_failure is not None
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


def test_api_connector_records_bad_extraction_and_continues(
    monkeypatch: MonkeyPatch,
) -> None:
    def fake_process(
        bundles: list[_ChatBundle],
        *,
        on_extraction_failure: object | None = None,
    ) -> Iterator[dict[str, JsonValue]]:
        assert callable(on_extraction_failure)
        on_extraction_failure(bundles[0], ExtractionFailure("malformed_response", 3))
        yield {"source_record_id": "record-after-failure"}

    monkeypatch.setattr(
        "src.connectors.whatsadmin_api.connector.process_whatsapp_bundles",
        fake_process,
    )
    connector = WhatsAdminChatApiConnector((StubClient(),), StubWatermark())

    assert list(connector.fetch_records()) == [{"source_record_id": "record-after-failure"}]
    assert connector.connector_error_count() == 1
    assert connector.failure_checkpoint()["extraction_failures"] == [
        {
            "entity_key": "eko",
            "session_id": "ses_1",
            "chat_id": "6581111111@c.us",
            "observed_at": "2026-07-17T05:20:00+00:00",
            "failure_code": "malformed_response",
            "attempts": 3,
        }
    ]


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
        on_extraction_failure: object | None = None,
    ) -> Iterator[dict[str, JsonValue]]:
        assert on_extraction_failure is not None
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
        lambda _bundles, **_kwargs: iter(()),
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
        lambda _bundles, **_kwargs: iter(()),
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


def test_queued_extraction_clears_only_after_downstream_success(
    monkeypatch: MonkeyPatch,
) -> None:
    class RetryWatermark(StubWatermark):
        def __init__(self) -> None:
            super().__init__()
            self.retries: list[dict[str, JsonValue]] = []

        def get_extraction_retries(
            self, entity_key: WhatsAdminEntity, session_id: str
        ) -> list[dict[str, JsonValue]]:
            _ = (entity_key, session_id)
            return list(self.retries)

        def set_extraction_retries(
            self,
            entity_key: WhatsAdminEntity,
            session_id: str,
            retries: list[dict[str, JsonValue]],
        ) -> None:
            _ = (entity_key, session_id)
            self.retries = list(retries)

    bundle = _ChatBundle(
        chat_id="chat-1",
        chat_name="Customer",
        session_id="ses_1",
        whatsapp_user_id="6590000000@c.us",
        tenant="eko",
        msg_text="Customer: hello",
        observed_at="2026-07-17T05:20:00+00:00",
        participants=[],
        message_endpoints=[],
        session_phone=None,
        source_id_scope="eko-ses_1",
    )
    details: dict[str, JsonValue] = {
        "entity_key": "eko",
        "session_id": "ses_1",
        "chat_id": "chat-1",
        "observed_at": bundle.observed_at,
        "failure_code": "malformed_response",
        "attempts": 3,
    }

    def fake_process(
        bundles: list[_ChatBundle],
        **_kwargs: object,
    ) -> Iterator[dict[str, JsonValue]]:
        assert [item.chat_id for item in bundles] == ["chat-1"]
        yield {"source_record_id": "record-1"}

    monkeypatch.setattr(
        "src.connectors.whatsadmin_api.connector.process_whatsapp_bundles",
        fake_process,
    )
    watermark = RetryWatermark()
    connector = WhatsAdminChatApiConnector((StubClient(),), watermark)
    watermark.retries = [serialize_retry_bundle(bundle, details)]

    records = connector._retry_pending_extractions("eko", "ses_1")
    assert next(records) == {"source_record_id": "record-1"}
    connector.record_processed(succeeded=True)
    assert list(records) == []
    assert watermark.retries == []

    watermark.retries = [serialize_retry_bundle(bundle, details)]
    records = connector._retry_pending_extractions("eko", "ses_1")
    assert next(records) == {"source_record_id": "record-1"}
    connector.record_processed(succeeded=False)
    assert list(records) == []
    assert [item["chat_id"] for item in watermark.retries] == ["chat-1"]
