"""Focused tests for the read-only Bitrix deal-stage catalog boundary."""

from __future__ import annotations

import json
from typing import cast

import httpx
import pytest
from src.connectors.bitrix_openlines.client import BitrixOpenLinesClient
from src.connectors.bitrix_openlines.crm_status_catalog import deal_stage_status_entity_id


def _client(handler: httpx.MockTransport) -> BitrixOpenLinesClient:
    return BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=handler),
    )


def test_client_reads_a_typed_current_stage_catalog_page() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path.rsplit("/", 1)[-1], body))
        return httpx.Response(
            200,
            json={
                "result": [
                    {
                        "ENTITY_ID": "DEAL_STAGE_2",
                        "STATUS_ID": "C2:NEW",
                        "CATEGORY_ID": "2",
                        "EXTRA": {"SEMANTICS": "process"},
                    },
                    {
                        "ENTITY_ID": "DEAL_STAGE_2",
                        "STATUS_ID": "C2:WON",
                        "CATEGORY_ID": 2,
                        "SEMANTICS": "success",
                        "EXTRA": {"SEMANTICS": "success"},
                    },
                ],
                "next": 50,
                "total": "51",
                "time": {"operating": 0.02, "operating_reset_at": 12345},
            },
        )

    page = _client(httpx.MockTransport(handler)).list_crm_deal_stage_catalog_page(category_id=2)

    assert requests == [
        (
            "crm.status.list",
            {
                "filter": {"ENTITY_ID": "DEAL_STAGE_2"},
                "order": {"SORT": "ASC"},
                "start": 0,
            },
        )
    ]
    assert [(item.category_id, item.stage_id, item.semantic_id) for item in page.items] == [
        ("2", "C2:NEW", "process"),
        ("2", "C2:WON", "success"),
    ]
    assert page.next_start == 50
    assert page.total == 51
    assert page.operating == 0.02
    assert page.operating_reset_at == 12345.0


def test_default_category_uses_the_default_deal_stage_directory() -> None:
    request_bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"result": [{"ENTITY_ID": "DEAL_STAGE", "STATUS_ID": "NEW"}]},
        )

    page = _client(httpx.MockTransport(handler)).list_crm_deal_stage_catalog_page(
        category_id=0,
        start=50,
    )

    assert deal_stage_status_entity_id(0) == "DEAL_STAGE"
    assert request_bodies == [
        {
            "filter": {"ENTITY_ID": "DEAL_STAGE"},
            "order": {"SORT": "ASC"},
            "start": 50,
        }
    ]
    assert page.items[0].category_id == "0"
    assert page.items[0].stage_id == "NEW"


@pytest.mark.parametrize(
    "response",
    [
        {"result": {}},
        {"result": [{"ENTITY_ID": "DEAL_STAGE_2", "STATUS_ID": " "}]},
        {"result": [{"ENTITY_ID": "DEAL_STAGE_2", "STATUS_ID": " C2:NEW"}]},
        {"result": [{"ENTITY_ID": "DEAL_STAGE_7", "STATUS_ID": "C2:NEW"}]},
        {
            "result": [
                {
                    "ENTITY_ID": "DEAL_STAGE_2",
                    "STATUS_ID": "C2:NEW",
                    "CATEGORY_ID": "7",
                }
            ]
        },
        {
            "result": [
                {
                    "ENTITY_ID": "DEAL_STAGE_2",
                    "STATUS_ID": "C2:NEW",
                    "SEMANTICS": "process",
                    "EXTRA": {"SEMANTICS": "success"},
                }
            ]
        },
        {"result": [], "next": 0},
        {"result": [], "time": {"operating": "0.2"}},
    ],
)
def test_client_fails_closed_for_malformed_stage_catalog_responses(
    response: dict[str, object],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    with pytest.raises(RuntimeError, match="Bitrix CRM stage catalog"):
        _client(httpx.MockTransport(handler)).list_crm_deal_stage_catalog_page(category_id=2)


@pytest.mark.parametrize("category_id", [-1, True, "2"])
def test_client_rejects_invalid_stage_catalog_category_ids(category_id: object) -> None:
    client = _client(httpx.MockTransport(lambda _request: httpx.Response(500)))

    with pytest.raises(ValueError, match="category_id"):
        client.list_crm_deal_stage_catalog_page(category_id=cast(int, category_id))
