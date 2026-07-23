from __future__ import annotations

import json
from datetime import UTC, datetime
from email.utils import formatdate

import httpx
import pytest
from src.connectors.bitrix_openlines.client import BitrixOpenLinesClient


def test_client_lists_active_open_channel_configurations() -> None:
    starts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://bitrix.test/rest/hook/imopenlines.config.list.get"
        body = json.loads(request.content)
        starts.append(body["start"])
        if body["start"] == 0:
            return httpx.Response(
                200,
                json={
                    "result": [
                        {"ID": "46", "ACTIVE": "Y", "LINE_NAME": "Speedzone: FB"},
                        {"ID": "47", "ACTIVE": "N", "LINE_NAME": "Old line"},
                    ],
                    "next": 50,
                },
            )
        return httpx.Response(
            200,
            json={
                "result": [
                    {"ID": "48", "ACTIVE": "Y", "LINE_NAME": "Eko: Instagram"},
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
        ("46", "Speedzone: FB"),
        ("48", "Eko: Instagram"),
    ]
    assert starts == [0, 50]


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
                        },
                        {
                            "id": 9,
                            "author_id": 502,
                            "text": "How can I help?",
                            "date": "2026-07-20T08:01:00+00:00",
                        },
                    ],
                    "users": [
                        {"id": 501, "name": "Customer", "type": "connector"},
                        {"id": 502, "name": "Assistant", "type": "bot"},
                    ],
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
        ("Customer", "Hello", False),
        ("Assistant", "How can I help?", True),
    ]


def test_client_reads_historical_openline_history_with_numeric_chat_id() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/imopenlines.session.history.get")
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "result": {
                    "message": {
                        "8": {
                            "id": "8",
                            "senderid": "501",
                            "text": "Historical hello",
                            "date": "2026-07-20T08:00:00+00:00",
                        },
                        "9": {
                            "id": "9",
                            "senderid": 502,
                            "text": "Historical reply",
                            "date": "2026-07-20T08:01:00+00:00",
                        },
                    },
                    "users": {
                        "501": {"id": "501", "name": "Customer", "type": "connector"},
                        "502": {"id": "502", "name": "Agent", "type": "user"},
                    },
                }
            },
        )

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    messages = client.get_history(79)

    assert [(item.id, item.author_name, item.text, item.is_agent) for item in messages] == [
        (8, "Customer", "Historical hello", False),
        (9, "Agent", "Historical reply", True),
    ]
    assert requests == [{"CHAT_ID": 79}]


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


def test_client_batches_owner_chat_lookups_for_each_crm_page() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        methods.append(method)
        if method == "crm.activity.list":
            return httpx.Response(
                200,
                json={
                    "result": [
                        {"OWNER_TYPE_ID": "2", "OWNER_ID": "501"},
                        {"OWNER_TYPE_ID": "3", "OWNER_ID": "502"},
                    ]
                },
            )
        assert method == "batch"
        body = json.loads(request.content)
        assert body["halt"] == 0
        commands = body["cmd"]
        assert len(commands) == 2
        assert any("CRM_ENTITY_TYPE=deal" in command for command in commands.values())
        assert any("CRM_ENTITY_TYPE=contact" in command for command in commands.values())
        return httpx.Response(
            200,
            json={
                "result": {
                    "result": {
                        "owner_0": [{"CHAT_ID": "79"}],
                        "owner_1": [{"CHAT_ID": "80"}],
                    },
                    "result_next": {},
                    "result_error": {},
                }
            },
        )

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert [item.chat_id for item in client.iter_crm_chat_refs()] == [79, 80]
    assert methods == ["crm.activity.list", "batch"]


def test_client_can_resume_crm_discovery_from_saved_start() -> None:
    starts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        starts.append(body["start"])
        return httpx.Response(
            200,
            json={
                "result": [{"PROVIDER_PARAMS": {"CHAT_ID": "79"}}],
                "next": 100,
            },
        )

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    page = next(client.iter_crm_discovery_pages(start=50))

    assert starts == [50]
    assert [reference.chat_id for reference in page.references] == [79]
    assert page.next_start == 100


def test_client_pages_crm_collections_and_preserves_latest_activity_timestamp() -> None:
    activity_starts: list[int] = []
    chat_starts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        if method == "crm.activity.list":
            start = body["start"]
            activity_starts.append(start)
            changed_at = "2026-07-20T08:00:00+00:00" if start == 0 else "2026-07-20T10:00:00+00:00"
            response: dict[str, object] = {
                "result": [
                    {
                        "OWNER_TYPE_ID": "2",
                        "OWNER_ID": "501",
                        "LAST_UPDATED": changed_at,
                    }
                ]
            }
            if start == 0:
                response["next"] = 50
            return httpx.Response(200, json=response)
        assert method == "imopenlines.crm.chat.get"
        start = body["start"]
        chat_starts.append(start)
        response = {"result": [{"CHAT_ID": "79" if start == 0 else "80"}]}
        if start == 0:
            response["next"] = 50
        return httpx.Response(200, json=response)

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    refs = client.iter_crm_chat_refs()

    assert [(item.chat_id, item.changed_at) for item in refs] == [
        (79, datetime(2026, 7, 20, 10, tzinfo=UTC)),
        (80, datetime(2026, 7, 20, 10, tzinfo=UTC)),
    ]
    assert activity_starts == [0, 50]
    assert chat_starts == [0, 50]


def test_client_preserves_all_crm_activity_provenance_across_pages() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        assert request.url.path.endswith("/crm.activity.list")
        activity_id = "900" if body["start"] == 0 else "901"
        response: dict[str, object] = {
            "result": [
                {
                    "ID": activity_id,
                    "OWNER_TYPE_ID": "2",
                    "OWNER_ID": "501",
                    "PROVIDER_PARAMS": {
                        "CHAT_ID": "79",
                        "USER_CODE": "facebook|46|external",
                        "IM": [{"id": "chat79", "token": "private-im-token"}],
                        "WEBHOOK_URL": "https://token@example.test/hook",
                    },
                }
            ]
        }
        if body["start"] == 0:
            response["next"] = 50
        return httpx.Response(200, json=response)

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    reference = client.iter_crm_chat_refs()[0]

    assert calls == 2
    assert getattr(reference, "activity_ids", ()) == ("900", "901")
    owner_references = getattr(reference, "crm_owner_references", ())
    assert [(item.owner_type, item.owner_id) for item in owner_references] == [("deal", 501)]
    assert getattr(reference, "provider_references", ()) == (
        {
            "CHAT_ID": "79",
            "USER_CODE": "facebook|46|external",
            "IM": [{"id": "chat79"}],
        },
    )


def test_recent_dialog_pagination_advances_by_requested_limit() -> None:
    offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        offsets.append(body["OFFSET"])
        if body["OFFSET"] == 0:
            return httpx.Response(200, json={"result": {"items": [{}], "hasMore": True}})
        return httpx.Response(200, json={"result": {"items": [], "hasMore": False}})

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.iter_recent_chat_refs(25) == []
    assert offsets == [0, 25]


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


def test_client_honors_retry_after_on_retryable_server_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, headers={"Retry-After": "6"})
        return httpx.Response(200, json={"result": []})

    monkeypatch.setattr("src.connectors.bitrix_openlines.client.time.sleep", sleeps.append)
    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=2,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.list_active_configs() == []
    assert calls == 2
    assert sleeps == [6.0]


def test_client_honors_http_date_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, headers={"Retry-After": formatdate(1006, usegmt=True)})
        return httpx.Response(200, json={"result": []})

    monkeypatch.setattr("src.connectors.bitrix_openlines.client.time.sleep", sleeps.append)
    monkeypatch.setattr("src.connectors.bitrix_openlines.client.time.time", lambda: 1000.0)
    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=2,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.list_active_configs() == []
    assert calls == 2
    assert sleeps == [6.0]


def test_client_caps_numeric_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, headers={"Retry-After": "999"})
        return httpx.Response(200, json={"result": []})

    monkeypatch.setattr("src.connectors.bitrix_openlines.client.time.sleep", sleeps.append)
    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=2,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.list_active_configs() == []
    assert calls == 2
    assert sleeps == [300.0]


def test_client_retries_transient_error_envelope_using_bitrix_timing_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "error": "QUERY_LIMIT_EXCEEDED",
                    "error_description": "Request operating time limit exceeded",
                    "time": {"operating_reset_at": 1005.0},
                },
            )
        return httpx.Response(200, json={"result": []})

    monkeypatch.setattr("src.connectors.bitrix_openlines.client.time.sleep", sleeps.append)
    monkeypatch.setattr("src.connectors.bitrix_openlines.client.time.time", lambda: 1000.0)
    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=2,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.list_active_configs() == []
    assert calls == 2
    assert sleeps == [5.0]


def test_client_stops_after_transient_error_envelope_retry_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"error": "QUERY_LIMIT_EXCEEDED"})

    monkeypatch.setattr("src.connectors.bitrix_openlines.client.time.sleep", sleeps.append)
    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=3,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(RuntimeError, match="QUERY_LIMIT_EXCEEDED"):
        client.list_active_configs()

    assert calls == 3
    assert sleeps == [1.0, 2.0]


def test_client_does_not_retry_permanent_error_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"error": "ACCESS_DENIED", "error_description": "Permission denied"},
        )

    monkeypatch.setattr("src.connectors.bitrix_openlines.client.time.sleep", sleeps.append)
    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=3,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(RuntimeError, match="ACCESS_DENIED"):
        client.list_active_configs()

    assert calls == 1
    assert sleeps == []


@pytest.mark.parametrize(
    "transport_error",
    [
        httpx.RemoteProtocolError("private response https://secret.example/body"),
        httpx.ProxyError("private proxy https://token@example.test"),
    ],
)
def test_client_retries_transport_errors_and_sanitizes_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
    transport_error: httpx.TransportError,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise transport_error

    monkeypatch.setattr("src.connectors.bitrix_openlines.client.time.sleep", sleeps.append)
    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=2,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(RuntimeError) as exc_info:
        client.list_active_configs()

    assert calls == 2
    assert sleeps == [1]
    assert "private" not in str(exc_info.value)
    assert "http" not in str(exc_info.value)


def test_client_captures_openline_origin_from_recent_dialogs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/im.recent.list")
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
                                "entity_link": {"id": "facebook|46|external"},
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

    references = client.iter_recent_chat_refs(50)
    assert [(ref.config_id, ref.connector_id) for ref in references] == [("46", "facebook")]
