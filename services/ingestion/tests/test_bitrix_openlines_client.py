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


def test_client_fetches_all_deal_contacts_and_call_activity_details() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        requests.append((method, body))
        if method == "crm.deal.get":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "ID": "501",
                        "TITLE": "Ada service",
                        "CONTACT_ID": "400",
                        "DATE_MODIFY": "2026-07-20T10:00:00+00:00",
                    }
                },
            )
        if method == "crm.deal.contact.items.get":
            return httpx.Response(
                200,
                json={"result": [{"CONTACT_ID": "400"}, {"CONTACT_ID": "401"}]},
            )
        if method == "crm.contact.get":
            contact_id = body["id"]
            return httpx.Response(
                200,
                json={
                    "result": {
                        "ID": str(contact_id),
                        "NAME": "Ada" if contact_id == "400" else "Grace",
                        "LAST_NAME": "Lovelace" if contact_id == "400" else "Hopper",
                        "PHONE": [{"VALUE": "+6591234567"}],
                        "EMAIL": [{"VALUE": "ada@example.com"}],
                    }
                },
            )
        assert method == "crm.activity.list"
        return httpx.Response(
            200,
            json={
                "result": [
                    {
                        "ID": "901",
                        "OWNER_TYPE_ID": "2",
                        "OWNER_ID": "501",
                        "TYPE_ID": "2",
                        "SUBJECT": "Follow-up call",
                        "START_TIME": "2026-07-20T10:00:00+00:00",
                        "END_TIME": "2026-07-20T10:05:00+00:00",
                        "DIRECTION": "2",
                        "RESULT_STATUS": "Y",
                    }
                ]
            },
        )

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    deal = client.get_deal(501)
    activities = client.list_deal_activities(501)

    assert deal.primary_contact is not None
    assert deal.primary_contact.id == "400"
    assert [contact.id for contact in deal.contacts] == ["400", "401"]
    assert deal.has_ambiguous_contacts is False
    assert activities[0].is_call is True
    assert activities[0].duration_seconds == 300
    assert activities[0].start_at == datetime(2026, 7, 20, 10, tzinfo=UTC)
    assert ("crm.deal.contact.items.get", {"id": 501}) in requests


def test_client_batches_deal_contact_and_lead_hydration() -> None:
    batches: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/batch")
        body = json.loads(request.content)
        commands = body["cmd"]
        assert isinstance(commands, dict)
        batches.append(commands)
        results: dict[str, object] = {}
        for command_key, command in commands.items():
            if command.startswith("crm.deal.get?"):
                deal_id = command.rsplit("=", 1)[-1]
                results[command_key] = {
                    "ID": deal_id,
                    "TITLE": f"Deal {deal_id}",
                    "CATEGORY_ID": "2",
                    "STAGE_ID": "C2:NEW",
                    "CONTACT_ID": "400" if deal_id == "501" else "0",
                    "LEAD_ID": "700" if deal_id == "503" else "0",
                }
            elif command.startswith("crm.deal.contact.items.get?"):
                deal_id = command.rsplit("=", 1)[-1]
                associations = {
                    "501": [{"CONTACT_ID": "400"}, {"CONTACT_ID": "401"}],
                    "502": [{"CONTACT_ID": "402"}, {"CONTACT_ID": "403"}],
                    "503": [],
                }
                results[command_key] = associations[deal_id]
            elif command.startswith("crm.contact.get?"):
                contact_id = command.rsplit("=", 1)[-1]
                results[command_key] = {"ID": contact_id, "NAME": f"Contact {contact_id}"}
            else:
                assert command.startswith("crm.lead.get?")
                lead_id = command.rsplit("=", 1)[-1]
                results[command_key] = {"ID": lead_id, "NAME": f"Lead {lead_id}"}
        return httpx.Response(
            200,
            json={"result": {"result": results, "result_error": {}}},
        )

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    deals = client.get_deals([501, 502, 503])

    assert [deal.id for deal in deals] == ["501", "502", "503"]
    assert [contact.id for contact in deals[0].contacts] == ["400", "401"]
    assert deals[0].primary_contact is not None
    assert deals[0].primary_contact.id == "400"
    assert deals[0].has_ambiguous_contacts is False
    assert [contact.id for contact in deals[1].contacts] == ["402", "403"]
    assert deals[1].primary_contact is None
    assert deals[1].has_ambiguous_contacts is True
    assert deals[2].primary_contact is not None
    assert deals[2].primary_contact.id == "700"
    assert [contact.kind for contact in deals[2].contacts] == ["lead"]
    assert len(batches) == 4
    assert [len(batch) for batch in batches] == [3, 3, 4, 1]


def test_client_splits_more_than_fifty_unique_contact_commands() -> None:
    batch_sizes: list[tuple[str, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        commands = body["cmd"]
        assert isinstance(commands, dict)
        first_command = next(iter(commands.values()))
        assert isinstance(first_command, str)
        batch_sizes.append((first_command.split("?", 1)[0], len(commands)))
        results: dict[str, object] = {}
        for command_key, command in commands.items():
            entity_id = command.rsplit("=", 1)[-1]
            if command.startswith("crm.deal.get?"):
                results[command_key] = {"ID": entity_id, "CATEGORY_ID": "2"}
            elif command.startswith("crm.deal.contact.items.get?"):
                results[command_key] = [
                    {"CONTACT_ID": str(contact_id)} for contact_id in range(1, 52)
                ]
            else:
                assert command.startswith("crm.contact.get?")
                results[command_key] = {"ID": entity_id, "NAME": f"Contact {entity_id}"}
        return httpx.Response(
            200,
            json={"result": {"result": results, "result_error": {}}},
        )

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    deal = client.get_deals([501])[0]

    assert len(deal.contacts) == 51
    assert deal.contact_count == 51
    assert deal.has_ambiguous_contacts is True
    assert batch_sizes == [
        ("crm.deal.get", 1),
        ("crm.deal.contact.items.get", 1),
        ("crm.contact.get", 50),
        ("crm.contact.get", 1),
    ]


def test_client_rejects_batch_command_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {
                    "result": {},
                    "result_error": {"deal_0": {"error": "ERROR_NOT_FOUND"}},
                }
            },
        )

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(RuntimeError, match="deal batch contained a command error"):
        client.get_deals([501])


def test_client_treats_zero_contact_and_lead_ids_as_unset() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        methods.append(method)
        if method == "crm.deal.get":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "ID": "501",
                        "TITLE": "No contact yet",
                        "CONTACT_ID": "0",
                        "CONTACT_IDS": ["0"],
                        "LEAD_ID": 0,
                    }
                },
            )
        assert method == "crm.deal.contact.items.get"
        return httpx.Response(200, json={"result": [{"CONTACT_ID": "0"}]})

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    deal = client.get_deal(501)

    assert deal.primary_contact is None
    assert deal.contacts == ()
    assert deal.contact_count == 0
    assert methods == ["crm.deal.get", "crm.deal.contact.items.get"]


def test_client_lists_crm_deals_independently_of_openlines_activities() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        requests.append((method, body))
        if method == "crm.deal.list":
            start = body["start"]
            if start == 0:
                return httpx.Response(
                    200,
                    json={"result": [{"ID": "501", "TITLE": "Deal 501"}], "next": 1},
                )
            assert start == 1
            return httpx.Response(
                200,
                json={
                    "result": [
                        {"ID": "501", "TITLE": "Duplicate Deal 501"},
                        {"ID": "502", "TITLE": "Deal 502"},
                    ]
                },
            )
        assert method == "crm.deal.contact.items.get"
        return httpx.Response(200, json={"result": []})

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    deals = list(client.iter_crm_deals(["2", "7"]))

    assert [deal.id for deal in deals] == ["501", "502"]
    assert [deal.title for deal in deals] == ["Deal 501", "Deal 502"]
    assert (
        "crm.deal.list",
        {
            "filter": {"@CATEGORY_ID": ["2", "7"]},
            "order": {"ID": "ASC"},
            "start": 0,
        },
    ) in requests
    assert (
        "crm.deal.list",
        {
            "filter": {"@CATEGORY_ID": ["2", "7"]},
            "order": {"ID": "ASC"},
            "start": 1,
        },
    ) in requests
    assert all(method != "crm.deal.get" for method, _body in requests)
    assert [body["id"] for method, body in requests if method == "crm.deal.contact.items.get"] == [
        501,
        502,
    ]


def test_client_does_not_request_crm_deals_for_an_empty_category_scope() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("empty category scope must not make an HTTP request")

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert list(client.iter_crm_deal_pages([])) == []


def test_client_reads_minimal_bounded_crm_deal_capability_page_without_enrichment() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        requests.append((method, body))
        assert method == "crm.deal.list"
        return httpx.Response(
            200,
            json={
                "result": [
                    {"ID": "501", "CATEGORY_ID": "2", "STAGE_ID": "C2:NEW"},
                    {"ID": 502, "CATEGORY_ID": 0},
                ],
                "next": 50,
                "total": "143000",
                "time": {"operating": 0.02, "operating_reset_at": 12345},
            },
        )

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    page = client.list_crm_deal_capability_page(
        category_ids=["2", "7", "2"],
        greater_than_id=500,
        less_than_or_equal_to_id=900,
    )

    assert requests == [
        (
            "crm.deal.list",
            {
                "filter": {"@CATEGORY_ID": ["2", "7"], ">ID": 500, "<=ID": 900},
                "select": ["ID", "CATEGORY_ID", "STAGE_ID"],
                "order": {"ID": "ASC"},
                "start": -1,
            },
        )
    ]
    assert [(item.deal_id, item.category_id, item.stage_id) for item in page.items] == [
        ("501", "2", "C2:NEW"),
        ("502", "0", None),
    ]
    assert page.next_start == 50
    assert page.total == 143000
    assert page.operating == 0.02
    assert page.operating_reset_at == 12345.0
    assert all(
        method not in {"crm.deal.contact.items.get", "crm.contact.get", "crm.lead.get"}
        for method, _body in requests
    )


def test_client_supports_descending_crm_deal_capability_boundary_probe() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={"result": [{"ID": "900", "CATEGORY_ID": "2"}]})

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    page = client.list_crm_deal_capability_page(category_ids=["2"], order_direction="DESC")

    assert page.items[0].deal_id == "900"
    assert requests == [
        {
            "filter": {"@CATEGORY_ID": ["2"]},
            "select": ["ID", "CATEGORY_ID", "STAGE_ID"],
            "order": {"ID": "DESC"},
            "start": -1,
        }
    ]


@pytest.mark.parametrize(
    "response",
    [
        {"result": {}},
        {"result": [{"ID": "", "CATEGORY_ID": "2"}]},
        {"result": [{"ID": "501", "CATEGORY_ID": "two"}]},
        {"result": [{"ID": "501", "CATEGORY_ID": "2", "STAGE_ID": "  "}]},
        {"result": [{"ID": "501", "CATEGORY_ID": "2"}], "next": -1},
        {"result": [{"ID": "501", "CATEGORY_ID": "2"}], "total": True},
        {"result": [{"ID": "501", "CATEGORY_ID": "2"}], "time": {"operating": "0.2"}},
    ],
)
def test_client_fails_closed_for_malformed_crm_deal_capability_values(
    response: dict[str, object],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(RuntimeError, match="Bitrix CRM deal capability"):
        client.list_crm_deal_capability_page(category_ids=["2"])


@pytest.mark.parametrize(
    ("greater_than_id", "less_than_or_equal_to_id"),
    [(True, None), (0, None), (None, 0), (900, 900), (901, 900)],
)
def test_client_rejects_invalid_crm_deal_capability_bounds(
    greater_than_id: int | None,
    less_than_or_equal_to_id: int | None,
) -> None:
    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(500))),
    )

    with pytest.raises(ValueError, match="capability"):
        client.list_crm_deal_capability_page(
            category_ids=["2"],
            greater_than_id=greater_than_id,
            less_than_or_equal_to_id=less_than_or_equal_to_id,
        )


def test_client_rejects_invalid_crm_deal_capability_order_direction() -> None:
    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(500))),
    )

    with pytest.raises(ValueError, match="order_direction"):
        client.list_crm_deal_capability_page(category_ids=["2"], order_direction="ascending")


def test_client_retries_the_same_filtered_crm_deal_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.path.endswith("/crm.deal.list")
        if attempts == 0:
            attempts += 1
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"result": []})

    monkeypatch.setattr("src.connectors.bitrix_openlines.client.time.sleep", lambda _delay: None)
    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=2,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    pages = list(client.iter_crm_deal_pages(["8", "2", "8"]))

    assert len(pages) == 1
    assert pages[0].deals == ()
    assert pages[0].returned_count == 0
    assert requests == [
        {
            "filter": {"@CATEGORY_ID": ["8", "2"]},
            "order": {"ID": "ASC"},
            "start": 0,
        },
        {
            "filter": {"@CATEGORY_ID": ["8", "2"]},
            "order": {"ID": "ASC"},
            "start": 0,
        },
    ]


def test_client_scans_all_deal_activities_without_per_deal_requests() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        requests.append((method, body))
        assert method == "crm.activity.list"
        if body["start"] == 0:
            return httpx.Response(
                200,
                json={
                    "result": [{"ID": "900", "OWNER_TYPE_ID": "2", "OWNER_ID": "501"}],
                    "next": 1,
                },
            )
        return httpx.Response(
            200,
            json={"result": [{"ID": "901", "OWNER_TYPE_ID": "2", "OWNER_ID": "502"}]},
        )

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    activities = list(client.iter_crm_activities())

    assert [activity.id for activity in activities] == ["900", "901"]
    assert [body["filter"] for _method, body in requests] == [
        {"OWNER_TYPE_ID": 2},
        {"OWNER_TYPE_ID": 2},
    ]


def test_client_reads_typed_stage_history_page_with_nested_items() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/crm.stagehistory.list")
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "result": {
                    "items": [
                        {
                            "ID": "900",
                            "OWNER_ID": "501",
                            "TYPE_ID": "1",
                            "CREATED_TIME": "2026-08-06T12:00:00+08:00",
                            "CATEGORY_ID": "2",
                            "STAGE_SEMANTIC_ID": "P",
                            "STAGE_ID": "C2:NEW",
                        }
                    ]
                },
                "next": 50,
                "total": 87,
                "time": {"operating": 0.02, "operating_reset_at": 12345},
            },
        )

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    page = client.list_stage_history_page(
        entity_type_id=2,
        filters={">ID": "899", "@OWNER_ID": ["501"]},
        start=-1,
    )

    assert requests == [
        {
            "entityTypeId": 2,
            "filter": {">ID": "899", "@OWNER_ID": ["501"]},
            "order": {"ID": "ASC"},
            "start": -1,
        }
    ]
    assert page.next_start == 50
    assert page.total == 87
    assert page.operating == 0.02
    assert page.items[0].history_id == "900"
    assert page.items[0].entity_type_id == "2"
    assert page.items[0].created_time == datetime(2026, 8, 6, 4, tzinfo=UTC)


@pytest.mark.parametrize("order_direction", ["", "ascending", "DESCENDING"])
def test_client_rejects_invalid_stage_history_order_direction(order_direction: str) -> None:
    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(500))),
    )

    with pytest.raises(ValueError, match="order_direction"):
        client.list_stage_history_page(entity_type_id=2, order_direction=order_direction)


def test_client_rejects_stage_history_timestamp_without_timezone() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {
                    "items": [
                        {
                            "ID": "900",
                            "OWNER_ID": "501",
                            "CREATED_TIME": "2026-08-06T12:00:00",
                        }
                    ]
                }
            },
        )

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(RuntimeError, match="must include a timezone"):
        client.list_stage_history_page(entity_type_id=2)


@pytest.mark.parametrize(
    "entity_type_id,start",
    [(True, -1), (2, True), (2, -2)],
)
def test_client_rejects_invalid_stage_history_numeric_arguments(
    entity_type_id: int, start: int
) -> None:
    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(500))),
    )

    with pytest.raises(ValueError):
        client.list_stage_history_page(entity_type_id=entity_type_id, start=start)


def test_fast_keyset_capability_zero_total_is_unavailable_metadata() -> None:
    responses = {
        "crm.deal.list": {
            "result": [{"ID": "501", "CATEGORY_ID": "2", "STAGE_ID": "C2:NEW"}],
            "total": 0,
        },
        "crm.activity.list": {
            "result": [
                {
                    "ID": "900",
                    "OWNER_TYPE_ID": "2",
                    "OWNER_ID": "501",
                    "TYPE_ID": "2",
                }
            ],
            "total": "0",
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        assert body["start"] == -1
        return httpx.Response(200, json=responses[method])

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    deal_page = client.list_crm_deal_capability_page(category_ids=["2"])
    activity_page = client.list_crm_activity_capability_page(
        greater_than_id=None,
        less_than_or_equal_to_id=900,
    )

    assert len(deal_page.items) == 1
    assert deal_page.total is None
    assert len(activity_page.items) == 1
    assert activity_page.total is None
