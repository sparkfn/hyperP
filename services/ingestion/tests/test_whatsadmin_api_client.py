from __future__ import annotations

import json

import httpx
from src.connectors.whatsadmin_api.client import WhatsAdminApiClient


def _response(
    data: list[dict[str, object]],
    *,
    snapshot_at: str | None = None,
) -> dict[str, object]:
    meta: dict[str, object] = {
        "timestamp": "2026-07-17T06:00:00Z",
        "requestId": "req_1",
        "pagination": {"hasMore": False},
    }
    if snapshot_at is not None:
        meta["snapshotAt"] = snapshot_at
    return {"success": True, "data": data, "meta": meta}


def test_client_sends_api_key_and_changed_since() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        assert payload == {
            "sessionId": "ses_1",
            "changedSince": "2026-07-16T00:00:00Z",
            "limit": 25,
        }
        return httpx.Response(
            200,
            json=_response([], snapshot_at="2026-07-17T06:00:00Z"),
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = WhatsAdminApiClient(
        base_url="https://whatsadmin.test",
        api_key="hk_secret",
        page_size=25,
        http=http,
    )

    pages = list(client.iter_chat_pages("ses_1", "2026-07-16T00:00:00Z"))

    assert len(pages) == 1
    snapshot_at = pages[0].meta.snapshot_at
    assert snapshot_at is not None
    assert snapshot_at.isoformat() == "2026-07-17T06:00:00+00:00"
    assert requests[0].headers["x-api-key"] == "hk_secret"


def test_client_follows_server_cursor() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        has_more = len(bodies) == 1
        response = _response([])
        response_meta = response["meta"]
        assert isinstance(response_meta, dict)
        response_meta["pagination"] = {
            "hasMore": has_more,
            "nextCursor": "opaque-next" if has_more else None,
        }
        return httpx.Response(200, json=response)

    client = WhatsAdminApiClient(
        base_url="https://whatsadmin.test",
        api_key="hk_secret",
        page_size=50,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    sessions = list(client.iter_sessions())

    assert sessions == []
    assert bodies == [{"limit": 50}, {"limit": 50, "cursor": "opaque-next"}]
