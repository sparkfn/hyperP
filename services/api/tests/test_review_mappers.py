from __future__ import annotations

from src.graph.mappers import _map_sales_summary, map_review_case_detail
from src.types import SalesOrderSummary, SalesUnitSummary


# ---------------------------------------------------------------------------
# _map_sales_summary
# ---------------------------------------------------------------------------

def test_map_sales_summary_none_when_no_order() -> None:
    assert _map_sales_summary(None, None) is None


def test_map_sales_summary_none_when_empty_dict() -> None:
    assert _map_sales_summary({}, []) is None


def test_map_sales_summary_order_only_no_units() -> None:
    order = {
        "order_id": "ord-1",
        "order_no": "INV-001",
        "total_amount": 299.90,
        "currency": "SGD",
        "ordered_at": "2026-01-15T10:00:00",
    }
    result = _map_sales_summary(order, [])
    assert isinstance(result, SalesOrderSummary)
    assert result.order_id == "ord-1"
    assert result.order_no == "INV-001"
    assert result.total_amount == 299.90
    assert result.currency == "SGD"
    assert result.ordered_at == "2026-01-15T10:00:00"
    assert result.units == []


def test_map_sales_summary_with_units() -> None:
    order = {"order_id": "ord-2", "order_no": None, "total_amount": None, "currency": None, "ordered_at": None}
    units = [
        {
            "machine_unit_id": "mu-1",
            "machine_product": "Segway X",
            "normalized_lta_tag": "TAG001",
            "normalized_serial_number": "SN001",
            "conflict_flag": False,
        },
        {
            "machine_unit_id": "mu-2",
            "machine_product": None,
            "normalized_lta_tag": None,
            "normalized_serial_number": "SN002",
            "conflict_flag": True,
        },
    ]
    result = _map_sales_summary(order, units)
    assert result is not None
    assert len(result.units) == 2
    u1 = result.units[0]
    assert isinstance(u1, SalesUnitSummary)
    assert u1.machine_unit_id == "mu-1"
    assert u1.machine_product == "Segway X"
    assert u1.normalized_lta_tag == "TAG001"
    assert u1.conflict_flag is False
    u2 = result.units[1]
    assert u2.machine_unit_id == "mu-2"
    assert u2.conflict_flag is True


def test_map_sales_summary_skips_null_unit_entries() -> None:
    order = {"order_id": "ord-3"}
    result = _map_sales_summary(order, [None, {}, {"machine_unit_id": "mu-good"}])
    assert result is not None
    assert len(result.units) == 1
    assert result.units[0].machine_unit_id == "mu-good"


# ---------------------------------------------------------------------------
# map_review_case_detail — sales left entity surfaces sales_summary
# ---------------------------------------------------------------------------

def _base_record() -> dict[str, object]:
    return {
        "review_case": {
            "review_case_id": "rc-test",
            "queue_state": "open",
            "priority": 5,
            "assigned_to": None,
            "follow_up_at": None,
            "sla_due_at": None,
            "resolution": None,
            "resolved_at": None,
            "actions": "[]",
            "created_at": "2026-06-16T00:00:00Z",
            "updated_at": "2026-06-16T00:00:00Z",
        },
        "match_decision": {
            "match_decision_id": "md-test",
            "engine_type": "heuristic",
            "engine_version": None,
            "policy_version": None,
            "decision": "review",
            "confidence": 0.65,
            "reasons": ["same_machine_unit_owner_claim"],
            "blocking_conflicts": None,
            "created_at": "2026-06-16T00:00:00Z",
        },
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
        "sales_units": [],
    }


def test_map_review_case_detail_person_left_no_sales_summary() -> None:
    record = _base_record()
    record["left_kind"] = "person"
    record["left_entity"] = {
        "person_id": "person-2",
        "status": "active",
        "preferred_full_name": "Bob",
        "preferred_phone": None,
        "preferred_email": None,
        "preferred_dob": None,
    }
    record["left_address"] = None
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
        "order_no": "INV-042",
        "total_amount": 500.0,
        "currency": "SGD",
        "ordered_at": "2026-06-10T08:00:00",
    }
    record["sales_units"] = [
        {
            "machine_unit_id": "mu-42",
            "machine_product": "EScooter Pro",
            "normalized_lta_tag": "T42",
            "normalized_serial_number": "SN42",
            "conflict_flag": False,
        }
    ]
    detail = map_review_case_detail(record)  # type: ignore[arg-type]
    left = detail.comparison_left
    assert left is not None
    assert left.entity_kind == "source_record"
    assert left.source_record_pk == "sr-42"
    summary = left.sales_summary
    assert summary is not None
    assert summary.order_id == "ord-42"
    assert summary.currency == "SGD"
    assert len(summary.units) == 1
    assert summary.units[0].machine_unit_id == "mu-42"
    assert summary.units[0].conflict_flag is False


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
