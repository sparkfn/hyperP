from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr
from src.connectors.whatsadmin_api.client import WhatsAdminApiClient
from src.connectors.whatsadmin_api.credentials import WhatsAdminCredential, WhatsAdminEntity


def _credential(entity_key: WhatsAdminEntity, api_key: str) -> WhatsAdminCredential:
    return WhatsAdminCredential(
        entity_key=entity_key,
        base_url="https://whatsadmin.test",
        api_key=SecretStr(api_key),
    )


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
        credential=_credential("eko", "hk_eko_secret"),
        page_size=25,
        http=http,
    )

    pages = list(client.iter_chat_pages("ses_1", "2026-07-16T00:00:00Z"))

    assert len(pages) == 1
    snapshot_at = pages[0].meta.snapshot_at
    assert snapshot_at is not None
    assert snapshot_at.isoformat() == "2026-07-17T06:00:00+00:00"
    assert requests[0].headers["x-api-key"] == "hk_eko_secret"
    assert requests[0].url.path == "/api/integrations/hyperp/chats/query"


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
        credential=_credential("eko", "hk_eko_secret"),
        page_size=50,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    sessions = list(client.iter_sessions())

    assert sessions == []
    assert bodies == [{"limit": 50}, {"limit": 50, "cursor": "opaque-next"}]


@pytest.mark.parametrize(
    ("entity_key", "selected_key", "other_key"),
    [
        ("eko", "hk_eko_only", "hk_speedzone_only"),
        ("speedzone", "hk_speedzone_only", "hk_eko_only"),
    ],
)
def test_client_uses_only_its_entity_key(
    entity_key: WhatsAdminEntity,
    selected_key: str,
    other_key: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_response([]))

    client = WhatsAdminApiClient(
        credential=_credential(entity_key, selected_key),
        page_size=50,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert list(client.iter_sessions()) == []
    assert client.entity_key == entity_key
    assert requests[0].headers["x-api-key"] == selected_key
    assert other_key not in requests[0].headers.values()
    assert requests[0].url.path == "/api/integrations/hyperp/sessions/query"


def test_http_error_does_not_expose_api_key(caplog: pytest.LogCaptureFixture) -> None:
    api_key = "hk_eko_must_not_leak"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    client = WhatsAdminApiClient(
        credential=_credential("eko", api_key),
        page_size=50,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        list(client.iter_sessions())

    assert api_key not in f"{exc_info.value}\n{caplog.text}"
