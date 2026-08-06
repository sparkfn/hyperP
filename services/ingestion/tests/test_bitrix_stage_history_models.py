from __future__ import annotations

import math

import pytest
from src.connectors.bitrix_stage_history.models import parse_stage_history_page
from src.models import JsonValue


def _payload() -> dict[str, JsonValue]:
    return {
        "result": {
            "items": [
                {
                    "ID": "900",
                    "OWNER_ID": "501",
                    "CREATED_TIME": "2026-08-06T12:00:00+08:00",
                }
            ]
        }
    }


@pytest.mark.parametrize("field_name", ["ID", "OWNER_ID", "CREATED_TIME"])
@pytest.mark.parametrize("value", ["", "   "])
def test_stage_history_rejects_blank_required_text(field_name: str, value: str) -> None:
    payload = _payload()
    result = payload["result"]
    assert isinstance(result, dict)
    items = result["items"]
    assert isinstance(items, list)
    item = items[0]
    assert isinstance(item, dict)
    item[field_name] = value

    with pytest.raises(RuntimeError, match="blank"):
        parse_stage_history_page(payload, entity_type_id="2", current_start=-1)


@pytest.mark.parametrize("field_name", ["TYPE_ID", "CATEGORY_ID", "STAGE_ID"])
@pytest.mark.parametrize("value", [True, 1.5, [], {}])
def test_stage_history_rejects_malformed_optional_text(field_name: str, value: JsonValue) -> None:
    payload = _payload()
    result = payload["result"]
    assert isinstance(result, dict)
    items = result["items"]
    assert isinstance(items, list)
    item = items[0]
    assert isinstance(item, dict)
    item[field_name] = value

    with pytest.raises(RuntimeError, match=field_name):
        parse_stage_history_page(payload, entity_type_id="2", current_start=-1)


@pytest.mark.parametrize("field_name", ["next", "total"])
@pytest.mark.parametrize("value", [True, -1, 1.5, "invalid", [], {}])
def test_stage_history_rejects_malformed_pagination_metadata(
    field_name: str, value: JsonValue
) -> None:
    payload = _payload()
    payload[field_name] = value

    with pytest.raises(RuntimeError, match=field_name):
        parse_stage_history_page(payload, entity_type_id="2", current_start=-1)


@pytest.mark.parametrize("value", [True, 1, "invalid", []])
def test_stage_history_rejects_malformed_time_container(value: JsonValue) -> None:
    payload = _payload()
    payload["time"] = value

    with pytest.raises(RuntimeError, match="time"):
        parse_stage_history_page(payload, entity_type_id="2", current_start=-1)


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("operating", True),
        ("operating", "1"),
        ("operating", math.nan),
        ("operating_reset_at", math.inf),
        ("operating_reset_at", []),
    ],
)
def test_stage_history_rejects_malformed_timing_values(field_name: str, value: JsonValue) -> None:
    payload = _payload()
    payload["time"] = {field_name: value}

    with pytest.raises(RuntimeError, match=field_name):
        parse_stage_history_page(payload, entity_type_id="2", current_start=-1)


def test_stage_history_allows_absent_or_null_optional_metadata() -> None:
    payload = _payload()
    payload["next"] = None
    payload["total"] = None
    payload["time"] = {"operating": None, "operating_reset_at": None}

    page = parse_stage_history_page(payload, entity_type_id="2", current_start=-1)

    assert page.next_start is None
    assert page.total is None
    assert page.operating is None
    assert page.operating_reset_at is None


def test_stage_history_preserves_blank_optional_source_text() -> None:
    payload = _payload()
    result = payload["result"]
    assert isinstance(result, dict)
    items = result["items"]
    assert isinstance(items, list)
    item = items[0]
    assert isinstance(item, dict)
    item["STAGE_SEMANTIC_ID"] = ""

    page = parse_stage_history_page(payload, entity_type_id="2", current_start=-1)

    assert page.items[0].stage_semantic_id == ""
