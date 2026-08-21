from __future__ import annotations

import json
from collections.abc import Iterator
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest
from _txmock import _RecordingTx
from neo4j import ManagedTransaction
from src.connectors.eko.sales import EkoSalesConnector
from src.exclusions import ExclusionContext
from src.graph import queries as _queries
from src.graph.queries.loyalty_points_migration import TARGET_LOYALTY_ORDER_SOURCES
from src.matching.vehicle_heuristic import VEHICLE_MATCH_AUTO, VEHICLE_MATCH_REVIEW
from src.models import JsonValue
from src.pipeline_sales import (
    _build_non_vehicle_lines,
    _drain_one_pending_sale,
    _merge_order,
    _propose_one_pending_sale,
    _write_vehicle_observations,
    propose_vehicle_matches_for_pending_sales,
)

if TYPE_CHECKING:
    from src.pipeline_sales import _OrderPayload

# Query-string constants used to classify recorded ``tx.run`` calls.
_UPSERT_VEHICLE_QUERY = _queries.UPSERT_VEHICLE
_LINK_ORDER_INVOLVES_VEHICLE_QUERY = _queries.LINK_ORDER_INVOLVES_VEHICLE
_LINK_PERSON_BOUGHT_VEHICLE_QUERY = _queries.LINK_PERSON_BOUGHT_VEHICLE

# ---------------------------------------------------------------------------
# Test scaffolding: _Result/_Tx/_Session/_Client fake the Neo4j transaction
# surface so the propose tests can drive _propose_one_pending_sale and the
# propose_vehicle_matches_for_pending_sales orchestration without a live DB.
# ---------------------------------------------------------------------------


class _Result:
    def __init__(
        self,
        row: dict[str, object] | None = None,
        rows: list[dict[str, object]] | None = None,
    ) -> None:
        self._row = row
        self._rows = rows if rows is not None else ([] if row is None else [row])

    def single(self) -> dict[str, object] | None:
        return self._row

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._rows)


class _Tx(_RecordingTx):
    def __init__(
        self,
        *,
        candidates: list[dict[str, object]] | None = None,
        pending_rows: list[dict[str, object]] | None = None,
    ) -> None:
        super().__init__()
        self._candidates: list[dict[str, object]] = candidates or []
        self._pending_rows: list[dict[str, object]] = (
            pending_rows
            if pending_rows is not None
            else [
                {
                    "source_record_pk": "sr-pending",
                    "source_system_key": "sys",
                    "raw_payload": {
                        "order": {"source_order_id": "o-1"},
                        "customer_nric": None,
                        "customer_emails": ["a@b.com"],
                        "customer_phones": [],
                    },
                }
            ]
        )

    def run(self, query: str, **kwargs: object) -> _Result:
        self._record(query, kwargs)
        # FIND_VEHICLE_CANDIDATES_FOR_SALES: unique fragment.
        if "INVOLVES_VEHICLE {source_record_pk: $sales_source_record_pk}" in query:
            return _Result(rows=self._candidates)
        if query == _queries.UPSERT_VEHICLE:
            return _Result(row={"vehicle_id": "vehicle-1", "conflict": False})
        if query == _queries.STAGE_SALES_REVIEW:
            return _Result(row={"source_record_pk": kwargs["source_record_pk"]})
        if query in {
            _queries.ACTIVATE_FIRST_SOURCE_RECORD_VERSION,
            _queries.ACTIVATE_SOURCE_RECORD_VERSION,
        }:
            return _Result(row={"source_record_pk": kwargs.get("new_source_record_pk", "sr-1")})
        # FIND_PENDING_CUSTOMER_SALES: has $limit parameter and the
        # ``pending_customer`` link_status filter.
        if "LIMIT $limit" in query and "pending_customer" in query:
            cursor = str(kwargs.get("cursor", ""))
            limit = int(kwargs.get("limit", len(self._pending_rows)))
            rows = sorted(
                (row for row in self._pending_rows if str(row["source_record_pk"]) > cursor),
                key=lambda row: str(row["source_record_pk"]),
            )
            return _Result(rows=rows[:limit])
        return _Result()


class _Session:
    def __init__(self, tx: _Tx) -> None:
        self.tx = tx

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def execute_write(self, callback: object) -> object:
        return callback(cast(ManagedTransaction, self.tx))  # type: ignore[arg-type]


class _Client:
    """Fake Neo4jClient that hands out sessions in order."""

    def __init__(self, *txs: _Tx) -> None:
        self._sessions = [_Session(tx) for tx in txs]

    def session(self) -> _Session:
        return self._sessions.pop(0)


# ---------------------------------------------------------------------------
# _propose_one_pending_sale (Task 6: vehicle heuristic)
# ---------------------------------------------------------------------------

_LINK_PURCHASED_QUERY = _queries.LINK_PERSON_PURCHASED_ORDER
_LINK_BOUGHT_QUERY = _queries.LINK_PERSON_BOUGHT_VEHICLE
_MARK_LINKED_QUERY = _queries.MARK_SALES_RECORD_LINKED
_MARK_REVIEW_QUERY = _queries.MARK_SALES_RECORD_PENDING_REVIEW
_FIND_CANDIDATES_QUERY = _queries.FIND_VEHICLE_CANDIDATES_FOR_SALES


def _propose(
    tx: _Tx,
    *,
    source_record_pk: str = "sr-1",
    source_system_key: str = "eko_phppos:sales",
    source_order_id: str = "o-1",
    customer_nric: str | None = None,
    customer_emails: list[str] | None = None,
    customer_phones: list[str] | None = None,
) -> bool:
    return _propose_one_pending_sale(
        cast(ManagedTransaction, tx),
        source_record_pk=source_record_pk,
        source_system_key=source_system_key,
        source_order_id=source_order_id,
        customer_nric=customer_nric,
        customer_emails=customer_emails or [],
        customer_phones=customer_phones or [],
        source_record_id="sale-1",
        raw_payload={
            "order": {"source_order_id": source_order_id},
            "line_items": [_vehicle_line()],
            "customer_link": {
                "identity_source_record_id": "identity-1",
                "source_system_key": "eko_phppos",
            },
        },
    )


def _candidate(
    **overrides: object,
) -> dict[str, object]:
    base: dict[str, object] = {
        "person_id": "person-1",
        "vehicle_id": "vehicle-1",
        "rel_type": "OWNS_VEHICLE",
        "is_active": True,
        "conflict_flag": False,
        "last_confirmed_at": "2026-06-01T00:00:00+00:00",
        "contact_channels": ["email"],
        "nric_blocked": False,
    }
    base.update(overrides)
    return base


def test_propose_passes_customer_contacts_to_candidate_query() -> None:
    """The sale's customer_nric/emails/phones are forwarded to the candidate query."""
    tx = _Tx(candidates=[])
    with (
        patch("src.pipeline_sales.persist_match_decision"),
        patch("src.pipeline_sales.create_review_case_if_needed"),
    ):
        _propose(
            tx,
            customer_nric="S1234567A",
            customer_emails=["a@b.com"],
            customer_phones=["+6599999999"],
        )
    candidate_calls = [k for q, k in tx.calls if q == _FIND_CANDIDATES_QUERY]
    assert len(candidate_calls) == 1
    assert candidate_calls[0]["customer_nric"] == "S1234567A"
    assert candidate_calls[0]["customer_emails"] == ["a@b.com"]
    assert candidate_calls[0]["customer_phones"] == ["+6599999999"]
    assert candidate_calls[0]["sales_source_record_pk"] == "sr-1"


def test_propose_no_candidates_returns_false() -> None:
    """Case 5: no candidates -> False, no decision, no review case, no link."""
    tx = _Tx(candidates=[])
    with (
        patch("src.pipeline_sales.persist_match_decision") as mock_persist,
        patch("src.pipeline_sales.create_review_case_if_needed") as mock_create,
    ):
        result = _propose(tx)
    assert result is False
    mock_persist.assert_not_called()
    mock_create.assert_not_called()
    assert not any(q == _MARK_LINKED_QUERY for q, _ in tx.calls)
    assert not any(q == _MARK_REVIEW_QUERY for q, _ in tx.calls)


def test_propose_single_candidate_auto_links() -> None:
    """Case 6: single candidate, no NRIC block -> auto-link at 0.90, MERGE."""
    tx = _Tx(candidates=[_candidate()])
    with (
        patch("src.pipeline_sales.persist_match_decision", return_value="md-1") as mock_persist,
        patch("src.pipeline_sales.create_review_case_if_needed") as mock_create,
    ):
        result = _propose(tx)
    assert result is True
    mock_persist.assert_called_once()
    match_result_arg = mock_persist.call_args[0][1]
    assert match_result_arg.decision.value == "merge"
    assert match_result_arg.confidence == VEHICLE_MATCH_AUTO
    assert match_result_arg.matched_person_id == "person-1"
    # No review case for an auto-link.
    mock_create.assert_not_called()
    # PURCHASED + BOUGHT_VEHICLE + MARK_LINKED all run with the candidate person.
    purchased = [k for q, k in tx.calls if q == _LINK_PURCHASED_QUERY]
    bought = [k for q, k in tx.calls if q == _LINK_BOUGHT_QUERY]
    linked = [k for q, k in tx.calls if q == _MARK_LINKED_QUERY]
    assert len(purchased) == 1
    assert purchased[0]["person_id"] == "person-1"
    assert purchased[0]["source_order_id"] == "o-1"
    assert purchased[0]["source_system_key"] == "eko_phppos:sales"
    assert len(bought) == 1
    assert bought[0]["vehicle_id"] == "vehicle-1"
    assert bought[0]["is_active"] is True
    assert bought[0]["confidence"] == 1.0
    assert len(linked) == 1
    assert linked[0]["source_record_pk"] == "sr-1"
    # Sale is NOT moved to pending_review.
    assert not any(q == _MARK_REVIEW_QUERY for q, _ in tx.calls)


def test_propose_nric_blocked_records_no_match() -> None:
    """Case 7: best candidate nric_blocked=True -> NO_MATCH, sale NOT linked."""
    tx = _Tx(candidates=[_candidate(nric_blocked=True)])
    with (
        patch("src.pipeline_sales.persist_match_decision", return_value="md-x") as mock_persist,
        patch("src.pipeline_sales.create_review_case_if_needed") as mock_create,
    ):
        result = _propose(tx, customer_nric="S9999999Z")
    assert result is True
    mock_persist.assert_called_once()
    match_result_arg = mock_persist.call_args[0][1]
    assert match_result_arg.decision.value == "no_match"
    assert match_result_arg.confidence == 0.0
    assert match_result_arg.reasons == ["nric_anti_match"]
    # No review case, no link edges, no MARK_LINKED.
    mock_create.assert_not_called()
    assert not any(q == _LINK_PURCHASED_QUERY for q, _ in tx.calls)
    assert not any(q == _LINK_BOUGHT_QUERY for q, _ in tx.calls)
    assert not any(q == _MARK_LINKED_QUERY for q, _ in tx.calls)


def test_propose_blocked_best_drops_and_autolinks_next_unblocked() -> None:
    """Finding #4: blocked best is dropped; next unblocked candidate auto-links.

    The original ``test_propose_nric_blocked_records_no_match`` covers the
    single-candidate case. When the candidate pool contains an unblocked
    Person ranked below the blocked one, the pipeline must (1) NOT link to
    the blocked Person, (2) NOT record NO_MATCH, and (3) auto-link the
    unblocked Person at the normal ``VEHICLE_MATCH_AUTO`` confidence.
    """
    blocked = _candidate(
        person_id="person-blocked", nric_blocked=True, last_confirmed_at="2026-06-10"
    )
    unblocked = _candidate(
        person_id="person-unblocked", nric_blocked=False, last_confirmed_at="2026-06-01"
    )
    tx = _Tx(candidates=[blocked, unblocked])
    with (
        patch("src.pipeline_sales.persist_match_decision", return_value="md-x") as mock_persist,
        patch("src.pipeline_sales.create_review_case_if_needed") as mock_create,
    ):
        result = _propose(tx, customer_nric="S9999999Z")
    assert result is True
    mock_persist.assert_called_once()
    match_result_arg = mock_persist.call_args[0][1]
    # Auto-link to the unblocked person — the blocked one was dropped.
    assert match_result_arg.matched_person_id == "person-unblocked"
    assert match_result_arg.decision.value == "merge"
    assert mock_create.call_count == 0
    # No NO_MATCH decision; the link edges are written for the unblocked person.
    no_match_calls = [
        c for c in mock_persist.call_args_list if c[0][1].decision.value == "no_match"
    ]
    assert no_match_calls == []
    link_purchased_calls = [kwargs for q, kwargs in tx.calls if q == _LINK_PURCHASED_QUERY]
    assert link_purchased_calls
    assert link_purchased_calls[0]["person_id"] == "person-unblocked"


def test_propose_customer_nric_none_auto_links() -> None:
    """Case 8: customer_nric=None -> query returns nric_blocked=False -> auto-link."""
    tx = _Tx(candidates=[_candidate(nric_blocked=False)])
    with (
        patch("src.pipeline_sales.persist_match_decision", return_value="md-1"),
        patch("src.pipeline_sales.create_review_case_if_needed"),
    ):
        result = _propose(tx, customer_nric=None)
    assert result is True
    assert any(q == _MARK_LINKED_QUERY for q, _ in tx.calls)


def test_propose_multiple_distinct_persons_creates_review_case() -> None:
    """Case 9: >=2 distinct person_ids -> REVIEW at review band, pending_review."""
    candidates = [
        _candidate(person_id="person-a", vehicle_id="vehicle-1", last_confirmed_at="2026-06-10"),
        _candidate(
            person_id="person-b",
            vehicle_id="vehicle-2",
            rel_type="BOUGHT_VEHICLE",
            is_active=False,
            last_confirmed_at="2026-06-01",
        ),
    ]
    tx = _Tx(candidates=candidates)
    with (
        patch("src.pipeline_sales.persist_match_decision", return_value="md-r") as mock_persist,
        patch(
            "src.pipeline_sales.create_review_case_if_needed", return_value="rc-r"
        ) as mock_create,
    ):
        result = _propose(tx)
    assert result is True
    mock_persist.assert_called_once()
    match_result_arg = mock_persist.call_args[0][1]
    assert match_result_arg.decision.value == "review"
    assert match_result_arg.confidence == VEHICLE_MATCH_REVIEW
    assert match_result_arg.matched_person_id == "person-a"
    assert match_result_arg.additional_linked_person_ids == ["person-b"]
    mock_create.assert_called_once()
    # Sale moves to pending_review, NOT linked.
    review_calls = [k for q, k in tx.calls if q == _MARK_REVIEW_QUERY]
    assert len(review_calls) == 1
    assert review_calls[0]["source_record_pk"] == "sr-1"
    assert not any(q == _MARK_LINKED_QUERY for q, _ in tx.calls)
    assert not any(q == _LINK_PURCHASED_QUERY for q, _ in tx.calls)
    assert not any(q == _LINK_BOUGHT_QUERY for q, _ in tx.calls)


# ---------------------------------------------------------------------------
# propose_vehicle_matches_for_pending_sales (orchestration)
# ---------------------------------------------------------------------------


def test_propose_orchestration_missing_source_id_does_not_mutate() -> None:
    """A pending row without its lock identity is skipped without mutation."""
    tx = _Tx(candidates=[_candidate()])
    # Two cursor-page reads (data then empty) plus one proposal transaction.
    client = _Client(tx, tx, tx)
    with (
        patch("src.pipeline_sales.persist_match_decision", return_value="md-1"),
        patch("src.pipeline_sales.create_review_case_if_needed"),
    ):
        count = propose_vehicle_matches_for_pending_sales(client)
    assert count == 0


def test_propose_orchestration_no_pending_returns_zero() -> None:
    """Case 10b: no pending sales -> 0, no candidate query runs."""
    tx = _Tx(candidates=[], pending_rows=[])
    client = _Client(tx)
    count = propose_vehicle_matches_for_pending_sales(client)
    assert count == 0
    assert not any(q == _FIND_CANDIDATES_QUERY for q, _ in tx.calls)


def test_propose_orchestration_skips_sale_missing_order() -> None:
    """A pending sale with no raw_payload.order is skipped (count 0)."""
    tx = _Tx(
        candidates=[_candidate()],
        pending_rows=[
            {
                "source_record_pk": "sr-bad",
                "source_system_key": "sys",
                "raw_payload": {"line_items": []},
            }
        ],
    )
    client = _Client(tx, tx)
    with (
        patch("src.pipeline_sales.persist_match_decision"),
        patch("src.pipeline_sales.create_review_case_if_needed"),
    ):
        count = propose_vehicle_matches_for_pending_sales(client)
    assert count == 0


# ---------------------------------------------------------------------------
# Task 5: Order enrichment (non_vehicle_lines) + vehicle-write tests.
# ---------------------------------------------------------------------------


class _VehicleResult:
    """Result of a ``tx.run(...)`` whose ``.single()`` returns a Vehicle row."""

    def __init__(self, row: dict[str, object] | None = None) -> None:
        self._row = row

    def single(self) -> dict[str, object] | None:
        return self._row


class _VehicleTx(_RecordingTx):
    """Recording transaction for ``_write_vehicle_observations``.

    Returns a canned ``{vehicle_id, conflict}`` row from any ``run(...)`` call
    — only ``UPSERT_VEHICLE`` chains ``.single()`` in the pipeline, so the row
    is only consumed there. All ``run`` calls are recorded for assertions.
    """

    def __init__(self, vehicle_id: str = "v-1", conflict: bool = False) -> None:
        super().__init__()
        self._row: dict[str, object] = {"vehicle_id": vehicle_id, "conflict": conflict}

    def run(self, query: str, **kwargs: object) -> _VehicleResult:
        self._record(query, kwargs)
        return _VehicleResult(self._row)


class _OrderTx(_RecordingTx):
    """Recording tx for ``_merge_order`` tests: returns a canned order_id row."""

    def run(self, query: str, **kwargs: object) -> _VehicleResult:
        self._record(query, kwargs)
        return _VehicleResult({"order_id": "o-1"})


def _vehicle_line(
    *,
    source_line_item_id: str = "li-bike",
    sku: str = "EBIKE-001",
    name: str = "E-Bike Pro",
    category: str = "Electric Bicycles",
    serial_number: str | None = "SN-100",
    lta_tag: str | None = "LTA-ABC",
    manufacturer: str = "Eko",
    model: str = "X1",
) -> dict[str, object]:
    metadata: dict[str, object] = {}
    if serial_number is not None:
        metadata["serial_number"] = serial_number
    if lta_tag is not None:
        metadata["lta_tag"] = lta_tag
    return {
        "source_line_item_id": source_line_item_id,
        "product": {
            "source_product_id": f"p-{sku}",
            "sku": sku,
            "name": name,
            "display_name": name,
            "category": category,
            "manufacturer": manufacturer,
            "model": model,
        },
        "metadata": metadata,
        "quantity": 1,
        "unit_price": 2000.0,
        "line_total": 2000.0,
    }


def _non_vehicle_line(
    *,
    source_line_item_id: str = "li-helmet",
    sku: str = "HELM-001",
    name: str = "Helmet",
    category: str = "Bicycle Helmets",
) -> dict[str, object]:
    return {
        "source_line_item_id": source_line_item_id,
        "product": {
            "source_product_id": f"p-{sku}",
            "sku": sku,
            "name": name,
            "display_name": name,
            "category": category,
            "manufacturer": "BrandX",
        },
        "metadata": {"merchant": "eko-store"},
        "quantity": 2,
        "unit_price": 50.0,
        "line_total": 100.0,
    }


def test_non_vehicle_lines_excludes_vehicle_lines_with_identifier() -> None:
    """A vehicle line (category + serial) is excluded from non_vehicle_lines."""
    lines: list[object] = [
        _vehicle_line(),
        _non_vehicle_line(),
    ]
    result = _build_non_vehicle_lines("eko_phppos", cast(list[JsonValue], lines))
    assert len(result) == 1
    assert result[0]["source_line_item_id"] == "li-helmet"
    assert result[0]["sku"] == "HELM-001"
    assert result[0]["product_name"] == "Helmet"
    assert result[0]["category"] == "Bicycle Helmets"
    assert result[0]["merchant"] == "eko-store"


def test_non_vehicle_lines_includes_vehicle_category_line_without_serial_or_lta() -> None:
    """A vehicle-category line lacking serial+lta remains a non-vehicle line."""
    line = _vehicle_line(serial_number=None, lta_tag=None)
    result = _build_non_vehicle_lines("eko_phppos", cast(list[JsonValue], [line]))
    assert len(result) == 1
    assert result[0]["source_line_item_id"] == "li-bike"
    assert result[0]["serial_number"] is None
    assert result[0]["lta_tag"] is None


def test_non_vehicle_lines_for_order_with_only_non_vehicle_lines() -> None:
    """Order with only non-vehicle lines → non_vehicle_lines has all of them."""
    lines: list[object] = [_non_vehicle_line(), _non_vehicle_line(source_line_item_id="li-2")]
    result = _build_non_vehicle_lines("eko_phppos", cast(list[JsonValue], lines))
    assert len(result) == 2


def test_merge_order_receives_non_vehicle_lines_param() -> None:
    """``_merge_order`` passes ``non_vehicle_lines`` through to ``MERGE_ORDER``."""
    tx = _OrderTx()
    order: dict[str, object] = {
        "source_order_id": "o-1",
        "order_no": "ORD-1",
        "currency": "SGD",
        "metadata": {},
        "loyalty": {},
    }
    non_vehicle = [{"source_line_item_id": "li-helmet", "sku": "HELM-001"}]
    _merge_order(
        cast(ManagedTransaction, tx),
        source_system_key="eko_phppos",
        order=cast("_OrderPayload", order),
        non_vehicle_lines=cast(list[dict[str, JsonValue]], non_vehicle),
    )
    merge_calls = [(query, kw) for query, kw in tx.calls if query == _queries.MERGE_ORDER]
    assert len(merge_calls) == 1
    # Neo4j stores non_vehicle_lines as a JSON-encoded STRING property.
    param = merge_calls[0][1]["non_vehicle_lines"]
    assert isinstance(param, str)
    assert json.loads(param) == non_vehicle


def test_merge_order_normalizes_loyalty_params_independently() -> None:
    tx = _OrderTx()
    sales_source_key = EkoSalesConnector().get_source_key()
    order: dict[str, object] = {
        "source_order_id": "private-order-persistence-241",
        "order_no": "ORD-1",
        "currency": "SGD",
        "metadata": {},
        "loyalty": {
            "points_used": "14000.0000000000",
            "points_gained": "bad-points",
            "did_redeem_discount": 1,
            "is_purchase_points": 0,
        },
    }
    _merge_order(
        cast(ManagedTransaction, tx),
        source_system_key=sales_source_key,
        order=cast("_OrderPayload", order),
        non_vehicle_lines=[],
    )
    params = next(kwargs for query, kwargs in tx.calls if query == _queries.MERGE_ORDER)
    assert params["source_system_key"] == "eko_phppos:sales"
    assert sales_source_key in TARGET_LOYALTY_ORDER_SOURCES
    assert params["points_used"] == 14000
    assert params["points_gained"] is None
    assert params["did_redeem_discount"] == 1
    assert params["is_purchase_points"] == 0


def test_merge_order_receives_empty_non_vehicle_lines_as_empty_json_string() -> None:
    """Empty non_vehicle_lines → ``non_vehicle_lines`` param is the string ``"[]"``."""

    tx = _OrderTx()
    order: dict[str, object] = {
        "source_order_id": "o-1",
        "order_no": "ORD-1",
        "currency": "SGD",
        "metadata": {},
        "loyalty": {},
    }
    _merge_order(
        cast(ManagedTransaction, tx),
        source_system_key="eko_phppos",
        order=cast("_OrderPayload", order),
        non_vehicle_lines=[],
    )
    merge_calls = [(query, kw) for query, kw in tx.calls if query == _queries.MERGE_ORDER]
    assert len(merge_calls) == 1
    assert merge_calls[0][1]["non_vehicle_lines"] == "[]"


def test_write_vehicle_observations_creates_vehicle_and_links_for_vehicle_line() -> None:
    """Write a vehicle and its order/person links while skipping a helmet."""
    lines: list[object] = [_vehicle_line(), _non_vehicle_line()]
    tx = _VehicleTx(vehicle_id="v-bike")
    _write_vehicle_observations(
        cast(ManagedTransaction, tx),
        source_system_key="eko_phppos",
        source_record_pk="sr-1",
        source_record_id="sr-id-1",
        source_order_id="o-1",
        observed_at="2026-07-01T00:00:00+00:00",
        line_items=cast(list[JsonValue], lines),
        person_id="person-1",
        exclusion_context=ExclusionContext(),
    )
    queries_run = [q for q, _ in tx.calls]
    assert sum(1 for q in queries_run if q == _UPSERT_VEHICLE_QUERY) == 1
    assert sum(1 for q in queries_run if q == _LINK_ORDER_INVOLVES_VEHICLE_QUERY) == 1
    assert sum(1 for q in queries_run if q == _LINK_PERSON_BOUGHT_VEHICLE_QUERY) == 1

    upsert = [kw for q, kw in tx.calls if q == _UPSERT_VEHICLE_QUERY][0]
    assert upsert["source_system_key"] == "eko_phppos"
    assert upsert["product_sku"] == "EBIKE-001"
    assert upsert["serial_number"] == "SN-100"
    assert upsert["lta_tag"] == "LTA-ABC"

    involves = [kw for q, kw in tx.calls if q == _LINK_ORDER_INVOLVES_VEHICLE_QUERY][0]
    assert involves["vehicle_id"] == "v-bike"
    assert involves["source_order_id"] == "o-1"
    assert involves["source_record_pk"] == "sr-1"

    bought = [kw for q, kw in tx.calls if q == _LINK_PERSON_BOUGHT_VEHICLE_QUERY][0]
    assert bought["person_id"] == "person-1"
    assert bought["vehicle_id"] == "v-bike"
    assert bought["is_active"] is True


def test_write_vehicle_observations_skips_bought_edge_when_person_unresolved() -> None:
    """No person_id → no BOUGHT_VEHICLE edge; INVOLVES_VEHICLE still written."""
    lines: list[object] = [_vehicle_line()]
    tx = _VehicleTx()
    _write_vehicle_observations(
        cast(ManagedTransaction, tx),
        source_system_key="eko_phppos",
        source_record_pk="sr-1",
        source_record_id="sr-id-1",
        source_order_id="o-1",
        observed_at="2026-07-01T00:00:00+00:00",
        line_items=cast(list[JsonValue], lines),
        person_id=None,
        exclusion_context=ExclusionContext(),
    )
    queries_run = [q for q, _ in tx.calls]
    assert sum(1 for q in queries_run if q == _LINK_PERSON_BOUGHT_VEHICLE_QUERY) == 0
    assert sum(1 for q in queries_run if q == _LINK_ORDER_INVOLVES_VEHICLE_QUERY) == 1


def test_write_vehicle_observations_writes_nothing_for_non_vehicle_order() -> None:
    """Order with only non-vehicle lines → no UPSERT_VEHICLE / LINK_VEHICLE calls."""
    lines: list[object] = [_non_vehicle_line(), _non_vehicle_line(source_line_item_id="li-2")]
    tx = _VehicleTx()
    _write_vehicle_observations(
        cast(ManagedTransaction, tx),
        source_system_key="eko_phppos",
        source_record_pk="sr-1",
        source_record_id="sr-id-1",
        source_order_id="o-1",
        observed_at="2026-07-01T00:00:00+00:00",
        line_items=cast(list[JsonValue], lines),
        person_id="person-1",
        exclusion_context=ExclusionContext(),
    )
    queries_run = [q for q, _ in tx.calls]
    assert sum(1 for q in queries_run if q == _UPSERT_VEHICLE_QUERY) == 0
    assert tx.calls == []


def test_write_vehicle_observations_cross_source_lta_writes_both_source_systems() -> None:
    """eko + fundbox lines sharing an LTA each UPSERT_VEHICLE with their source_system_key.

    The cross-source merge into one Vehicle node is asserted at the Task 2 query
    level (test_vehicle_queries); here we assert the pipeline emits one
    UPSERT_VEHICLE per source with the right ``source_system_key`` so the query
    can stamp ``source_systems`` on both.
    """
    eko_line = _vehicle_line(
        source_line_item_id="li-eko",
        sku="EBIKE-EKO",
        serial_number=None,
        lta_tag="LTA-SHARED",
    )
    fundbox_line = _vehicle_line(
        source_line_item_id="li-fb",
        sku="FB-SCOOT",
        name="E-Scooter",
        category="Electric Scooters",
        serial_number=None,
        lta_tag="LTA-SHARED",
        manufacturer="Fundbox",
    )
    tx = _VehicleTx()
    # NOTE: _write_vehicle_observations takes a single source_system_key; the
    # pipeline is invoked once per sales envelope (one source). To exercise the
    # cross-source path we drive it twice — once per source envelope — sharing
    # the recording tx so both UPSERT_VEHICLE calls are captured.
    for source_key, line in (
        ("eko_phppos", eko_line),
        ("fundbox", fundbox_line),
    ):
        _write_vehicle_observations(
            cast(ManagedTransaction, tx),
            source_system_key=source_key,
            source_record_pk=f"sr-{source_key}",
            source_record_id=f"sr-id-{source_key}",
            source_order_id=f"o-{source_key}",
            observed_at="2026-07-01T00:00:00+00:00",
            line_items=cast(list[JsonValue], [line]),
            person_id=None,
            exclusion_context=ExclusionContext(),
        )
    upserts = [kw for q, kw in tx.calls if q == _UPSERT_VEHICLE_QUERY]
    assert len(upserts) == 2
    sources = {kw["source_system_key"] for kw in upserts}
    assert sources == {"eko_phppos", "fundbox"}
    assert all(kw["lta_tag"] == "LTA-SHARED" for kw in upserts)


# ---------------------------------------------------------------------------
# drain_pending_customer_sales — identity-source-key fix
# ---------------------------------------------------------------------------
#
# Regression guard for the cross-source customer link bug: the fundbox sales
# connector emits ``customer_link.source_system_key = "fundbox"``
# (the IDENTITY source) while the sales record itself belongs to
# ``fundbox:sales``. ``LINK_SALES_TO_IDENTITY_RECORD`` MATCHes
# ``(:SourceSystem {source_key: $source_system_key})`` on the IDENTITY record's
# FROM_SOURCE edge, so the drain MUST pass the identity source — not the sales
# source. Previously the drain passed the sales source (or NULL when
# ``sr.source_system_key`` was unread), so the FOR_CUSTOMER_RECORD edge was
# never created and the sale stayed pending forever.


class _DrainTx(_RecordingTx):
    """Routes only the queries ``_drain_one_pending_sale`` issues.

    ``line_items`` is empty so ``_write_vehicle_observations`` produces no
    Vehicle upserts — keeps the mock surface tiny. Reuses ``_Result`` for query
    results (no need for a separate result mock).
    """

    def __init__(self, *, person_id: str = "person-54") -> None:
        super().__init__()
        self._person_id = person_id

    def run(self, query: str, **kwargs: object) -> _Result:
        self._record(query, kwargs)
        if "FOR_CUSTOMER_RECORD" in query and "identity_source_record_id" in kwargs:
            # LINK_SALES_TO_IDENTITY_RECORD — no result needed.
            return _Result()
        if "FOR_CUSTOMER_RECORD]->(identity_sr:SourceRecord)" in query:
            # RESOLVE_SALES_CUSTOMER — return the resolved person_id.
            return _Result(row={"person_id": self._person_id})
        return _Result()


@pytest.mark.parametrize(
    "customer_link, expected_linked, expected_link_source",
    [
        pytest.param(
            {
                "identity_source_record_id": "fundbox-user-54",
                "source_system_key": "fundbox",
            },
            True,
            "fundbox",
            id="cross-source-uses-identity-source",
        ),
        pytest.param(
            {
                "identity_source_record_id": "fundbox-user-54",
                # No source_system_key — the sale must be skipped, not guessed.
            },
            False,
            None,
            id="missing-identity-source-skips",
        ),
        pytest.param(
            {},  # No identity_source_record_id at all.
            False,
            None,
            id="missing-identity-record-skips",
        ),
    ],
)
def test_drain_links_via_identity_source_key(
    customer_link: dict[str, JsonValue],
    expected_linked: bool,
    expected_link_source: str | None,
) -> None:
    """Drain passes customer_link.source_system_key (the IDENTITY source) to
    LINK_SALES_TO_IDENTITY_RECORD, and SKIPS the sale when that key (or the
    identity record id) is absent rather than falling back to the sales source.

    The sales source (e.g. ``fundbox:sales``) is never the
    identity source, so a sales-source fallback would silently fail the MATCH
    against ``(:SourceSystem {source_key: $source_system_key})`` and leave the
    sale pending forever — the exact bug this path exists to fix. Every
    connector sets both fields, so the skip is defensive.
    """
    tx = _DrainTx(person_id="person-54")
    raw_payload: dict[str, JsonValue] = {
        "order": {"source_order_id": "10"},
        "customer_link": customer_link,
        "customer_nric": None,
        "customer_emails": [],
        "customer_phones": [],
        "line_items": [],
    }
    linked = _drain_one_pending_sale(
        cast(ManagedTransaction, tx),
        sales_pk="sr-sales-10",
        # SALES source — must NOT be used for the identity lookup.
        source_system_key="fundbox:sales",
        raw_payload=raw_payload,
        exclusion_context=ExclusionContext(),
    )
    assert linked is expected_linked
    link_calls = [
        (q, kw)
        for q, kw in tx.calls
        if "FOR_CUSTOMER_RECORD" in q and "identity_source_record_id" in kw
    ]
    if expected_link_source is None:
        # Skipped before the identity-link query ran — no FOR_CUSTOMER_RECORD
        # edge was attempted.
        assert link_calls == []
    else:
        assert len(link_calls) == 1
        assert link_calls[0][1]["source_system_key"] == expected_link_source
        assert link_calls[0][1]["identity_source_record_id"] == "fundbox-user-54"
