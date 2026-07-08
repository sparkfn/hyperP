from __future__ import annotations

import json
from collections.abc import Iterator
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest
from neo4j import ManagedTransaction

from src.exclusions import ExclusionContext
from src.graph import queries as _queries
from src.matching.vehicle_heuristic import VEHICLE_MATCH_AUTO, VEHICLE_MATCH_REVIEW
from src.models import JsonValue
from src.pipeline_sales import (
    _build_non_vehicle_lines,
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


class _Tx:
    def __init__(
        self,
        *,
        candidates: list[dict[str, object]] | None = None,
        pending_rows: list[dict[str, object]] | None = None,
    ) -> None:
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
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **kwargs: object) -> _Result:
        self.calls.append((query, dict(kwargs)))
        # FIND_VEHICLE_CANDIDATES_FOR_SALES: unique fragment.
        if "INVOLVES_VEHICLE {source_record_pk: $sales_source_record_pk}" in query:
            return _Result(rows=self._candidates)
        # FIND_PENDING_CUSTOMER_SALES: has $limit parameter and the
        # ``pending_customer`` link_status filter.
        if "LIMIT $limit" in query and "pending_customer" in query:
            return _Result(rows=self._pending_rows)
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
    source_system_key: str = "sys",
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
    assert purchased[0]["source_system_key"] == "sys"
    assert len(bought) == 1
    assert bought[0]["vehicle_id"] == "vehicle-1"
    assert bought[0]["is_active"] is True
    assert bought[0]["confidence"] == VEHICLE_MATCH_AUTO
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
        patch("src.pipeline_sales.create_review_case_if_needed", return_value="rc-r") as mock_create,
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


def test_propose_orchestration_returns_count_of_decisions() -> None:
    """Case 10: one pending sale, one candidate -> count == 1."""
    tx = _Tx(candidates=[_candidate()])
    # Two sessions are consumed: one for _get_pending, one for _propose. Both
    # wrap the same _Tx so the candidate query sees the candidate rows.
    client = _Client(tx, tx)
    with (
        patch("src.pipeline_sales.persist_match_decision", return_value="md-1"),
        patch("src.pipeline_sales.create_review_case_if_needed"),
    ):
        count = propose_vehicle_matches_for_pending_sales(client)
    assert count == 1


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
    client = _Client(tx)
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


class _VehicleTx:
    """Recording transaction for ``_write_vehicle_observations``.

    Returns a canned ``{vehicle_id, conflict}`` row from any ``run(...)`` call
    — only ``UPSERT_VEHICLE`` chains ``.single()`` in the pipeline, so the row
    is only consumed there. All ``run`` calls are recorded for assertions.
    """

    def __init__(self, vehicle_id: str = "v-1", conflict: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._row: dict[str, object] = {"vehicle_id": vehicle_id, "conflict": conflict}

    def run(self, query: str, **kwargs: object) -> _VehicleResult:
        self.calls.append((query, dict(kwargs)))
        return _VehicleResult(self._row)


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
    """A vehicle-category line lacking serial+lta produces no Vehicle → goes to non_vehicle_lines."""
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
    class _OrderTx:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def run(self, query: str, **kwargs: object) -> _VehicleResult:
            self.calls.append((query, dict(kwargs)))
            return _VehicleResult({"order_id": "o-1"})

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


def test_merge_order_receives_empty_non_vehicle_lines_as_empty_json_string() -> None:
    """Empty non_vehicle_lines → ``non_vehicle_lines`` param is the string ``"[]"``."""

    class _OrderTx:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def run(self, query: str, **kwargs: object) -> _VehicleResult:
            self.calls.append((query, dict(kwargs)))
            return _VehicleResult({"order_id": "o-1"})

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
    """Vehicle line → UPSERT_VEHICLE + INVOLVES_VEHICLE + BOUGHT_VEHICLE (when person resolved); helmet skipped."""
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
        ("fundbox_consumer_backend", fundbox_line),
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
    assert sources == {"eko_phppos", "fundbox_consumer_backend"}
    assert all(kw["lta_tag"] == "LTA-SHARED" for kw in upserts)
