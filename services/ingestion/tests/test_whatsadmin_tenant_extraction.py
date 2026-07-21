from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
from pydantic import SecretStr
from pytest import MonkeyPatch
from src.connectors.whatsadmin_api.client import WhatsAdminApiClient
from src.connectors.whatsadmin_api.connector import WhatsAdminChatApiConnector
from src.connectors.whatsadmin_api.credentials import WhatsAdminCredential, WhatsAdminEntity


class TrackingWatermark:
    def __init__(self) -> None:
        self.committed: dict[tuple[WhatsAdminEntity, str], datetime] = {}

    def get(self, entity_key: WhatsAdminEntity, session_id: str) -> datetime | None:
        assert entity_key == "eko"
        hour = 1 if session_id == "eko_session_1" else 2
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


def _session(session_id: str) -> dict[str, object]:
    return {
        "id": session_id,
        "orgId": "org_eko",
        "orgName": "EkoLife SG",
        "whatsappUserId": f"{session_id}@c.us",
        "expectedPhoneNumber": None,
        "updatedAt": "2026-07-17T05:00:00Z",
    }


def _response(
    data: list[dict[str, object]],
    *,
    snapshot_at: str | None = None,
    next_cursor: str | None = None,
) -> dict[str, object]:
    pagination: dict[str, object] = {"hasMore": next_cursor is not None}
    if next_cursor is not None:
        pagination["nextCursor"] = next_cursor
    meta: dict[str, object] = {
        "timestamp": "2026-07-17T08:00:00Z",
        "requestId": "req_1",
        "pagination": pagination,
    }
    if snapshot_at is not None:
        meta["snapshotAt"] = snapshot_at
    return {"success": True, "data": data, "meta": meta}


def test_multiple_sessions_reuse_entity_key_with_isolated_pagination(
    monkeypatch: MonkeyPatch,
) -> None:
    api_key = "hk_eko_shared_handle"
    requests: list[httpx.Request] = []
    chat_bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/sessions/query"):
            return httpx.Response(
                200,
                json=_response([_session("eko_session_1"), _session("eko_session_2")]),
            )
        body: dict[str, object] = json.loads(request.content)
        chat_bodies.append(body)
        session_id = body["sessionId"]
        assert isinstance(session_id, str)
        cursor = None if "cursor" in body else f"opaque::{session_id}::next"
        snapshot = "2026-07-17T06:00:00Z" if session_id.endswith("1") else "2026-07-17T07:00:00Z"
        return httpx.Response(
            200,
            json=_response([], snapshot_at=snapshot, next_cursor=cursor),
        )

    monkeypatch.setattr(
        "src.connectors.whatsadmin_api.connector.process_whatsapp_bundles",
        lambda _bundles, *, fail_on_extraction_error: iter(()),
    )
    client = WhatsAdminApiClient(
        credential=WhatsAdminCredential(
            entity_key="eko",
            base_url="https://whatsadmin.test",
            api_key=SecretStr(api_key),
        ),
        page_size=25,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    watermark = TrackingWatermark()
    connector = WhatsAdminChatApiConnector((client,), watermark)

    assert list(connector.fetch_records()) == []
    connector.commit_watermark()

    assert all(request.headers["x-api-key"] == api_key for request in requests)
    assert all("speedzone" not in request.headers["x-api-key"] for request in requests)
    assert chat_bodies == [
        {
            "sessionId": "eko_session_1",
            "limit": 25,
            "changedSince": "2026-07-16T01:00:00+00:00",
        },
        {
            "sessionId": "eko_session_1",
            "limit": 25,
            "changedSince": "2026-07-16T01:00:00+00:00",
            "cursor": "opaque::eko_session_1::next",
        },
        {
            "sessionId": "eko_session_2",
            "limit": 25,
            "changedSince": "2026-07-16T02:00:00+00:00",
        },
        {
            "sessionId": "eko_session_2",
            "limit": 25,
            "changedSince": "2026-07-16T02:00:00+00:00",
            "cursor": "opaque::eko_session_2::next",
        },
    ]
    assert watermark.committed == {
        ("eko", "eko_session_1"): datetime(2026, 7, 17, 6, tzinfo=UTC),
        ("eko", "eko_session_2"): datetime(2026, 7, 17, 7, tzinfo=UTC),
    }
