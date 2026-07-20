from __future__ import annotations

import httpx
import pytest
from src.connectors.bitrix_openlines.client import BitrixOpenLinesClient


def test_client_lists_active_open_channel_configurations() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://bitrix.test/rest/hook/imopenlines.config.list.get"
        return httpx.Response(
            200,
            json={
                "result": [
                    {"ID": "46", "ACTIVE": "Y", "LINE_NAME": "Speedzone: FB"},
                    {"ID": "47", "ACTIVE": "N", "LINE_NAME": "Old line"},
                ]
            },
        )

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert [(item.id, item.line_name) for item in client.list_active_configs()] == [
        ("46", "Speedzone: FB")
    ]


def test_client_reads_dialog_origin_and_messages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "im.dialog.get":
            return httpx.Response(
                200,
                json={"result": {"id": "chat77", "entity_link": {"id": "facebook|46|x"}}},
            )
        assert method == "im.dialog.messages.get"
        return httpx.Response(
            200,
            json={
                "result": {
                    "messages": [
                        {
                            "id": 8,
                            "author_id": 501,
                            "text": "Hello",
                            "date": "2026-07-20T08:00:00+00:00",
                        }
                    ],
                    "users": [{"id": 501, "name": "Customer", "type": "extranet"}],
                }
            },
        )

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.get_dialog(77).connector_id == "facebook"
    messages = client.get_messages(77)
    assert [(item.author_name, item.text, item.is_agent) for item in messages] == [
        ("Customer", "Hello", False)
    ]


def test_client_discovers_crm_and_recent_chat_references() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "crm.activity.list":
            return httpx.Response(
                200,
                json={"result": [{"PROVIDER_PARAMS": {"CHAT_ID": "77"}}]},
            )
        assert method == "im.recent.list"
        return httpx.Response(
            200,
            json={
                "result": {
                    "items": [
                        {
                            "type": "chat",
                            "chat_id": 78,
                            "date_update": "2026-07-20T08:00:00+00:00",
                            "chat": {
                                "entity_type": "LINES",
                                "entity_link": {"id": "facebook|46|x"},
                            },
                        }
                    ],
                    "hasMore": False,
                }
            },
        )

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert [item.chat_id for item in client.iter_crm_chat_refs()] == [77]
    assert [item.chat_id for item in client.iter_recent_chat_refs(50)] == [78]


def test_client_resolves_current_portal_openline_session_activities_to_chat_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "crm.activity.list":
            return httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "OWNER_TYPE_ID": "2",
                            "OWNER_ID": "501",
                            "PROVIDER_PARAMS": {"USER_CODE": "facebook|46|external"},
                        }
                    ]
                },
            )
        assert method == "imopenlines.crm.chat.get"
        return httpx.Response(
            200,
            json={"result": [{"CHAT_ID": "79", "CONNECTOR_ID": "facebook"}]},
        )

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert [item.chat_id for item in client.iter_crm_chat_refs()] == [79]


@pytest.mark.parametrize(
    ("retry_after", "expected_delay"),
    [("12", 12.0), ("later", 1.0), (None, 1.0)],
)
def test_client_honors_valid_retry_after_and_falls_back_for_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
    retry_after: str | None,
    expected_delay: float,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            headers = {"Retry-After": retry_after} if retry_after is not None else {}
            return httpx.Response(429, headers=headers)
        return httpx.Response(
            200,
            json={"result": [{"ID": "46", "ACTIVE": "Y", "LINE_NAME": "Line"}]},
        )

    monkeypatch.setattr("src.connectors.bitrix_openlines.client.time.sleep", sleeps.append)
    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=2,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.list_active_configs()[0].id == "46"
    assert sleeps == [expected_delay]
