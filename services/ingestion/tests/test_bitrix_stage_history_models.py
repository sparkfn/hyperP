from __future__ import annotations

import math

import pytest
from src.connectors.bitrix_stage_history.models import (
    DecodedStageHistoryRow,
    MalformedStageHistoryRow,
    StageHistoryRowErrorCode,
    decode_stage_history_item,
    parse_stage_history_page,
    parse_stage_history_raw_page,
)
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


def test_raw_stage_history_page_preserves_rows_without_decoding_them() -> None:
    malformed: JsonValue = ["not", "an", "object"]
    payload = _payload()
    result = payload["result"]
    assert isinstance(result, dict)
    items = result["items"]
    assert isinstance(items, list)
    items.append(malformed)
    payload["next"] = 50
    payload["total"] = 2
    payload["time"] = {"operating": 0.25}

    page = parse_stage_history_raw_page(payload, current_start=-1)

    assert page.items == (items[0], malformed)
    assert page.next_start == 50
    assert page.total == 2
    assert page.operating == 0.25


def test_tolerant_stage_history_decoder_returns_a_typed_valid_row() -> None:
    payload = _payload()
    result = payload["result"]
    assert isinstance(result, dict)
    items = result["items"]
    assert isinstance(items, list)
    raw = items[0]

    decoded = decode_stage_history_item(raw, entity_type_id="2")

    assert isinstance(decoded, DecodedStageHistoryRow)
    assert decoded.raw is raw
    assert decoded.item.history_id == "900"
    assert decoded.item.owner_id == "501"


@pytest.mark.parametrize(
    ("raw,error_code"),
    [
        ("not-an-object", "invalid_row_shape"),
        ({"OWNER_ID": "501", "CREATED_TIME": "2026-08-06T12:00:00+08:00"}, "missing_history_id"),
        (
            {"ID": " ", "OWNER_ID": "501", "CREATED_TIME": "2026-08-06T12:00:00+08:00"},
            "blank_history_id",
        ),
        (
            {"ID": "900", "OWNER_ID": [], "CREATED_TIME": "2026-08-06T12:00:00+08:00"},
            "invalid_owner_id",
        ),
        (
            {"ID": "900", "OWNER_ID": "501", "CREATED_TIME": "not-a-time"},
            "invalid_created_time",
        ),
        (
            {"ID": "900", "OWNER_ID": "501", "CREATED_TIME": "2026-08-06T12:00:00"},
            "created_time_without_timezone",
        ),
        (
            {
                "ID": "900",
                "OWNER_ID": "501",
                "CREATED_TIME": "2026-08-06T12:00:00+08:00",
                "STAGE_ID": {},
            },
            "invalid_stage_id",
        ),
    ],
)
def test_tolerant_stage_history_decoder_preserves_malformed_row_and_safe_code(
    raw: JsonValue,
    error_code: StageHistoryRowErrorCode,
) -> None:
    decoded = decode_stage_history_item(raw, entity_type_id="2")

    assert isinstance(decoded, MalformedStageHistoryRow)
    assert decoded.raw is raw
    assert decoded.error_code == error_code


def test_strict_stage_history_page_still_rejects_one_malformed_row() -> None:
    payload = _payload()
    result = payload["result"]
    assert isinstance(result, dict)
    items = result["items"]
    assert isinstance(items, list)
    items.append({"ID": "901", "OWNER_ID": "502"})

    with pytest.raises(RuntimeError, match="omitted CREATED_TIME"):
        parse_stage_history_page(payload, entity_type_id="2", current_start=-1)


@pytest.mark.parametrize("value", ("0", "01", "+1", " 1", "١"))
def test_positive_history_id_rejects_noncanonical_numeric_text(value: str) -> None:
    from src.connectors.bitrix_stage_history.models import parse_positive_history_id

    with pytest.raises(ValueError, match="canonical positive ASCII"):
        parse_positive_history_id(value)


def test_positive_history_id_accepts_canonical_ascii_text() -> None:
    from src.connectors.bitrix_stage_history.models import parse_positive_history_id

    assert parse_positive_history_id("123") == 123
