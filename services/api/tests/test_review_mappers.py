from __future__ import annotations

from src.graph.mappers import _map_sales_summary, map_review_case_detail
from src.types import NonVehicleLine, SalesOrderSummary, SalesVehicleSummary

# ---------------------------------------------------------------------------
# _map_sales_summary
# ---------------------------------------------------------------------------


def test_map_sales_summary_none_when_no_order() -> None:
    assert _map_sales_summary(None, None) is None


def test_map_sales_summary_none_when_empty_dict() -> None:
    assert _map_sales_summary({}, []) is None


def test_map_sales_summary_order_only_no_vehicles() -> None:
    order = {
        "order_id": "ord-1",
        "order_no": "INV-001",
        "total_amount": 299.90,
        "currency": "SGD",
        "ordered_at": "2026-01-15T10:00:00",
    }
    result = _map_sales_summary(order, None, None)
    assert isinstance(result, SalesOrderSummary)
    assert result.order_id == "ord-1"
    assert result.order_no == "INV-001"
    assert result.total_amount == 299.90
    assert result.currency == "SGD"
    assert result.ordered_at == "2026-01-15T10:00:00"
    assert result.vehicles == []
    assert result.non_vehicle_lines == []


def test_map_sales_summary_with_vehicles() -> None:
    order = {
        "order_id": "ord-2", "order_no": None, "total_amount": None,
        "currency": None, "ordered_at": None,
    }
    vehicles = [
        {
            "vehicle_id": "v-1",
            "product": "Segway X",
            "product_sku": "sku-1",
            "normalized_lta_tag": "TAG001",
            "normalized_serial_number": "SN001",
            "conflict_flag": False,
        },
        {
            "vehicle_id": "v-2",
            "product": None,
            "product_sku": None,
            "normalized_lta_tag": None,
            "normalized_serial_number": "SN002",
            "conflict_flag": True,
        },
    ]
    result = _map_sales_summary(order, vehicles, None)
    assert result is not None
    assert len(result.vehicles) == 2
    v1 = result.vehicles[0]
    assert isinstance(v1, SalesVehicleSummary)
    assert v1.vehicle_id == "v-1"
    assert v1.product == "Segway X"
    assert v1.product_sku == "sku-1"
    assert v1.normalized_lta_tag == "TAG001"
    assert v1.conflict_flag is False
    v2 = result.vehicles[1]
    assert v2.vehicle_id == "v-2"
    assert v2.conflict_flag is True


def test_map_sales_summary_skips_null_vehicle_entries() -> None:
    order = {"order_id": "ord-3"}
    result = _map_sales_summary(order, [None, {}, {"vehicle_id": "v-good"}], None)
    assert result is not None
    assert len(result.vehicles) == 1
    assert result.vehicles[0].vehicle_id == "v-good"


def test_map_sales_summary_non_vehicle_lines_none() -> None:
    order = {"order_id": "ord-4"}
    result = _map_sales_summary(order, None, None)
    assert result is not None
    assert result.non_vehicle_lines == []


def test_map_sales_summary_non_vehicle_lines_empty_string() -> None:
    order = {"order_id": "ord-5"}
    result = _map_sales_summary(order, None, "")
    assert result is not None
    assert result.non_vehicle_lines == []


def test_map_sales_summary_non_vehicle_lines_json_string() -> None:
    order = {"order_id": "ord-6"}
    # The writer (``_build_non_vehicle_lines`` in pipeline_sales) emits these
    # keys per the design spec (``sku``/``product_name``/``line_total``).
    # The mapper translates to the API's NonVehicleLine field names
    # (``product_sku``/``product``/``total_amount``).
    raw = '[{"sku": "sku-1", "product_name": "Helmet", "merchant": "Acme"}]'
    result = _map_sales_summary(order, None, raw)
    assert result is not None
    assert len(result.non_vehicle_lines) == 1
    line = result.non_vehicle_lines[0]
    assert isinstance(line, NonVehicleLine)
    assert line.product_sku == "sku-1"
    assert line.product == "Helmet"
    assert line.merchant == "Acme"


def test_map_sales_summary_non_vehicle_lines_list_passthrough() -> None:
    order = {"order_id": "ord-7"}
    # The list path accepts writer-keyed dicts directly (no JSON round-trip).
    raw = [{"sku": "sku-1", "product_name": "Helmet"}]
    result = _map_sales_summary(order, None, raw)
    assert result is not None
    assert len(result.non_vehicle_lines) == 1
    assert result.non_vehicle_lines[0].product_sku == "sku-1"
    assert result.non_vehicle_lines[0].product == "Helmet"


def test_map_sales_summary_non_vehicle_lines_invalid_json() -> None:
    order = {"order_id": "ord-8"}
    result = _map_sales_summary(order, None, "not-json")
    assert result is not None
    assert result.non_vehicle_lines == []


def test_map_sales_summary_vehicles_with_conflict_flag() -> None:
    order = {"order_id": "ord-9"}
    vehicles = [
        {
            "vehicle_id": "v-1",
            "product": "Bike",
            "product_sku": "sku-x",
            "conflict_flag": True,
        },
    ]
    result = _map_sales_summary(order, vehicles, None)
    assert result is not None
    assert result.vehicles[0].vehicle_id == "v-1"
    assert result.vehicles[0].conflict_flag is True


# ---------------------------------------------------------------------------
# map_review_case_detail
# ---------------------------------------------------------------------------


def _base_record() -> dict[str, object]:
    return {
        "review_case": {
            "review_case_id": "rc-1",
            "queue_state": "open",
            "priority": 5,
            "actions": [],
            "created_at": "2026-06-16T00:00:00Z",
        },
        "left_kind": "person",
        "left_entity": {
            "person_id": "person-2",
            "status": "active",
            "preferred_full_name": "Bob",
            "preferred_phone": None,
            "preferred_email": None,
            "preferred_dob": None,
        },
        "left_address": None,
        "right_kind": "person",
        "right_entity": {
            "person_id": "person-1",
            "status": "active",
            "preferred_full_name": "Alice",
            "preferred_phone": None,
            "preferred_email": None,
            "preferred_dob": None,
        },
        "right_address": None,
        "sales_order": None,
        "sales_vehicles": [],
        "non_vehicle_lines": None,
    }


def test_map_review_case_detail_person_left_no_sales_summary() -> None:
    record = _base_record()
    detail = map_review_case_detail(record)  # type: ignore[arg-type]
    assert detail.comparison_left is not None
    assert detail.comparison_left.entity_kind == "person"
    assert detail.comparison_left.sales_summary is None


def test_map_review_case_detail_sales_source_record_carries_summary() -> None:
    record = _base_record()
    record["left_kind"] = "source_record"
    record["left_entity"] = {
        "source_record_pk": "sr-42",
        "source_record_id": "order-42",
        "normalized_payload": None,
        "observed_at": None,
    }
    record["left_address"] = None
    record["sales_order"] = {
        "order_id": "ord-42",
        "order_no": "INV-42",
        "total_amount": 199.0,
        "currency": "SGD",
        "ordered_at": "2026-06-10T08:00:00",
    }
    record["sales_vehicles"] = [
        {
            "vehicle_id": "v-42",
            "product": "EScooter Pro",
            "product_sku": "sku-42",
            "normalized_lta_tag": "LTA-42",
            "normalized_serial_number": "SN-42",
            "conflict_flag": False,
        }
    ]
    record["non_vehicle_lines"] = '[{"sku": "sku-acc", "product_name": "Helmet"}]'
    detail = map_review_case_detail(record)  # type: ignore[arg-type]
    left = detail.comparison_left
    assert left is not None
    assert left.entity_kind == "source_record"
    summary = left.sales_summary
    assert summary is not None
    assert summary.order_id == "ord-42"
    assert summary.currency == "SGD"
    assert len(summary.vehicles) == 1
    assert summary.vehicles[0].vehicle_id == "v-42"
    assert summary.vehicles[0].conflict_flag is False
    assert len(summary.non_vehicle_lines) == 1
    assert summary.non_vehicle_lines[0].product_sku == "sku-acc"


def test_map_review_case_detail_sales_order_none_gives_no_summary() -> None:
    record = _base_record()
    record["left_kind"] = "source_record"
    record["left_entity"] = {
        "source_record_pk": "sr-43",
        "source_record_id": "order-43",
        "normalized_payload": None,
        "observed_at": None,
    }
    record["left_address"] = None
    # sales_order is already None in _base_record
    detail = map_review_case_detail(record)  # type: ignore[arg-type]
    assert detail.comparison_left is not None
    assert detail.comparison_left.sales_summary is None