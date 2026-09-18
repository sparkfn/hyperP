"""Tests for WhatsAdminIncrementalConnector."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from src.connectors.whatsadmin_api.credentials import WhatsAdminEntity
from src.connectors.whatsadmin_api.incremental import WhatsAdminIncrementalConnector
from src.connectors.whatsadmin_api.models import ChatPage, SessionRow
from src.connectors.whatsapp.connector import _ChatBundle
from src.incremental_connector import IncrementalPage
from src.models import JsonValue


class StubClient:
    """Minimal WhatsAdminClient for test use."""

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

    def iter_chat_pages(
        self,
        session_id: str,
        changed_since: str | None,
        cursor: str | None = None,
    ) -> Iterator[ChatPage]:
        _ = cursor
        yield _make_chat_page(
            session_id=session_id,
            whatsapp_user_id="6590000000@c.us",
            snapshot_at="2026-07-17T05:31:00Z",
        )

    def close(self) -> None:
        self.closed = True


def _make_chat_page(
    *,
    session_id: str = "ses_1",
    whatsapp_user_id: str = "6590000000@c.us",
    snapshot_at: str = "2026-07-17T05:31:00Z",
    has_more: bool = False,
) -> ChatPage:
    return ChatPage.model_validate(
        {
            "success": True,
            "data": [
                {
                    "chatId": "6581111111@c.us",
                    "chatName": "Alice",
                    "sessionId": session_id,
                    "whatsappUserId": whatsapp_user_id,
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
                "snapshotAt": snapshot_at,
                "pagination": {"hasMore": has_more},
            },
        }
    )


def _fake_process(
    bundles: list[_ChatBundle],
    *,
    on_extraction_failure: object | None = None,
) -> Iterator[dict[str, JsonValue]]:
    _ = on_extraction_failure
    for bundle in bundles:
        yield {"source_record_id": f"record-{bundle.chat_id}"}


def test_incremental_connector_open_query_passes_updated_since() -> None:
    calls: list[str | None] = []

    class TrackingClient(StubClient):
        def iter_chat_pages(
            self,
            session_id: str,
            changed_since: str | None,
            cursor: str | None = None,
        ) -> Iterator[ChatPage]:
            calls.append(changed_since)
            yield from super().iter_chat_pages(session_id, changed_since, cursor)

    with patch(
        "src.connectors.whatsadmin_api.incremental.process_whatsapp_bundles",
        _fake_process,
    ):
        connector = WhatsAdminIncrementalConnector((TrackingClient(),))
        ts = datetime(2026, 7, 16, tzinfo=UTC)
        connector.open_query(ts)
        page = connector.fetch_next_page()

    assert calls == ["2026-07-16T00:00:00+00:00"]
    assert page.has_more is False


def test_incremental_connector_bootstrap_passes_none() -> None:
    calls: list[str | None] = []

    class TrackingClient(StubClient):
        def iter_chat_pages(
            self,
            session_id: str,
            changed_since: str | None,
            cursor: str | None = None,
        ) -> Iterator[ChatPage]:
            calls.append(changed_since)
            yield from super().iter_chat_pages(session_id, changed_since, cursor)

    with patch(
        "src.connectors.whatsadmin_api.incremental.process_whatsapp_bundles",
        _fake_process,
    ):
        connector = WhatsAdminIncrementalConnector((TrackingClient(),))
        connector.open_query(None)
        connector.fetch_next_page()

    assert calls == [None]


def test_incremental_connector_drains_all_sessions_and_pages() -> None:
    class TwoPageClient(StubClient):
        def __init__(self, entity: WhatsAdminEntity, org: str) -> None:
            self.entity_key = entity
            self._org = org
            self.closed = False

        def iter_sessions(self) -> Iterator[SessionRow]:
            yield SessionRow.model_validate(
                {
                    "id": "ses_A",
                    "orgId": "org_A",
                    "orgName": self._org,
                    "whatsappUserId": "650A@c.us",
                    "expectedPhoneNumber": None,
                    "updatedAt": "2026-07-17T05:00:00Z",
                }
            )

        def iter_chat_pages(
            self,
            session_id: str,
            changed_since: str | None,
            cursor: str | None = None,
        ) -> Iterator[ChatPage]:
            yield _make_chat_page(
                session_id=session_id,
                whatsapp_user_id="650A@c.us",
                snapshot_at="2026-07-17T06:00:00Z",
            )
            yield _make_chat_page(
                session_id=session_id,
                whatsapp_user_id="650A@c.us",
                snapshot_at="2026-07-17T07:00:00Z",
            )

    with patch(
        "src.connectors.whatsadmin_api.incremental.process_whatsapp_bundles",
        _fake_process,
    ):
        eko = TwoPageClient("eko", "EkoLife SG")
        sz = TwoPageClient("speedzone", "SpeedZone")
        connector = WhatsAdminIncrementalConnector((eko, sz))
        connector.open_query(None)

        pages: list[IncrementalPage] = []
        while True:
            page = connector.fetch_next_page()
            pages.append(page)
            if not page.has_more:
                break

    assert len(pages) == 4
    assert all(p.has_more for p in pages[:-1])
    assert pages[-1].has_more is False
    assert all(len(p.records) == 1 for p in pages)


def test_incremental_connector_max_updated_at_tracks_snapshot() -> None:
    class TwoPageClient(StubClient):
        def iter_chat_pages(
            self,
            session_id: str,
            changed_since: str | None,
            cursor: str | None = None,
        ) -> Iterator[ChatPage]:
            yield _make_chat_page(
                session_id=session_id,
                snapshot_at="2026-07-17T05:00:00Z",
            )
            yield _make_chat_page(
                session_id=session_id,
                snapshot_at="2026-07-17T08:00:00Z",
            )

    with patch(
        "src.connectors.whatsadmin_api.incremental.process_whatsapp_bundles",
        _fake_process,
    ):
        connector = WhatsAdminIncrementalConnector((TwoPageClient(),))
        connector.open_query(None)
        p1 = connector.fetch_next_page()
        p2 = connector.fetch_next_page()

    assert p1.max_updated_at == datetime(2026, 7, 17, 5, 0, tzinfo=UTC)
    assert p2.max_updated_at == datetime(2026, 7, 17, 8, 0, tzinfo=UTC)


def test_incremental_connector_validates_chat_identity() -> None:
    class MismatchClient(StubClient):
        def iter_chat_pages(
            self,
            session_id: str,
            changed_since: str | None,
            cursor: str | None = None,
        ) -> Iterator[ChatPage]:
            page = next(super().iter_chat_pages(session_id, changed_since, cursor))
            chat = page.data[0].model_copy(update={"session_id": "wrong_session"})
            yield page.model_copy(update={"data": [chat]})

    with patch(
        "src.connectors.whatsadmin_api.incremental.process_whatsapp_bundles",
        _fake_process,
    ):
        connector = WhatsAdminIncrementalConnector((MismatchClient(),))
        with pytest.raises(RuntimeError, match="chat session"):
            connector.open_query(None)


def test_incremental_connector_rejects_missing_snapshot() -> None:
    class NoSnapshotClient(StubClient):
        def iter_chat_pages(
            self,
            session_id: str,
            changed_since: str | None,
            cursor: str | None = None,
        ) -> Iterator[ChatPage]:
            page = next(super().iter_chat_pages(session_id, changed_since, cursor))
            meta = page.meta.model_copy(update={"snapshot_at": None})
            yield page.model_copy(update={"meta": meta})

    with patch(
        "src.connectors.whatsadmin_api.incremental.process_whatsapp_bundles",
        _fake_process,
    ):
        connector = WhatsAdminIncrementalConnector((NoSnapshotClient(),))
        with pytest.raises(RuntimeError, match="snapshotAt"):
            connector.open_query(None)


def test_incremental_connector_close_closes_all_clients() -> None:
    c1 = StubClient()
    c2 = StubClient()
    connector = WhatsAdminIncrementalConnector((c1, c2))
    connector.close()
    assert c1.closed
    assert c2.closed


def test_incremental_connector_source_key() -> None:
    connector = WhatsAdminIncrementalConnector(())
    assert connector.get_source_key() == "whatsapp_chat"


def test_incremental_connector_registry_has_whatsapp_chat() -> None:
    from src.tasks import INCREMENTAL_CONNECTORS

    assert "whatsapp_chat" in INCREMENTAL_CONNECTORS
    assert callable(INCREMENTAL_CONNECTORS["whatsapp_chat"])


def test_incremental_connector_empty_sessions_returns_empty_terminal_page() -> None:
    class EmptyClient(StubClient):
        def iter_sessions(self) -> Iterator[SessionRow]:
            return iter(())

    connector = WhatsAdminIncrementalConnector((EmptyClient(),))
    ts = datetime(2026, 7, 16, tzinfo=UTC)
    connector.open_query(ts)
    page = connector.fetch_next_page()

    assert page.records == ()
    assert page.has_more is False
    assert page.max_updated_at == ts


def test_incremental_connector_factory_respects_entity_key() -> None:
    from unittest.mock import MagicMock

    from pydantic import SecretStr
    from src.connectors.whatsadmin_api.credentials import (
        WhatsAdminCredential,
        WhatsAdminCredentialResolver,
    )

    cred_eko = WhatsAdminCredential(
        entity_key="eko",
        base_url="https://test.example.com",
        api_key=SecretStr("hk_eko_test"),
    )
    mock_resolver = MagicMock(spec=WhatsAdminCredentialResolver)
    mock_resolver.resolve_job.return_value = (cred_eko,)

    with (
        patch("src.tasks.get_settings") as mock_settings,
        patch(
            "src.connectors.whatsadmin_api.credentials.WhatsAdminCredentialResolver",
            return_value=mock_resolver,
        ),
        patch(
            "src.connectors.whatsadmin_api.client.WhatsAdminApiClient",
        ) as mock_client_cls,
    ):
        settings = mock_settings.return_value
        settings.whatsadmin_api_base_url = "https://test.example.com"
        settings.whatsadmin_eko_api_key = SecretStr("hk_eko_test")
        settings.whatsadmin_speedzone_api_key = SecretStr("hk_sz_test")
        settings.whatsadmin_api_page_size = 25
        settings.whatsadmin_api_timeout_seconds = 120.0
        settings.whatsadmin_api_max_attempts = 5
        settings.whatsadmin_api_retry_base_delay_seconds = 1.0
        settings.whatsadmin_legacy_entity = None
        mock_client_cls.return_value = MagicMock()

        from src.tasks import _create_whatsadmin_incremental

        result = _create_whatsadmin_incremental(entity_key="eko")

    mock_resolver.resolve_job.assert_called_once_with("eko")
    assert isinstance(result, WhatsAdminIncrementalConnector)
