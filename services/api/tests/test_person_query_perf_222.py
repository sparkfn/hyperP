"""Tests for issue #222 query-layer optimisations.

Verifies:
- count{} pattern for standalone count subqueries
- loyalty/vehicles deferred from GET_PERSON_BY_ID
- New GET_PERSON_LOYALTY and GET_PERSON_VEHICLES query constants
- Identifiers query combines two CALL blocks into one
- count{} for source_record_count in GET_PERSON_BY_ID
"""

from __future__ import annotations

from src.graph.queries.persons import (
    GET_PERSON_BY_ID,
    GET_PERSON_IDENTIFIERS,
    GET_PERSON_LOYALTY,
    GET_PERSON_VEHICLES,
)
from src.graph.queries.persons_list import (
    _BANKRUPTCY_CASE_COUNT,
    _IDENTIFIER_COUNT,
    _SOURCE_RECORD_COUNT,
    build_list_persons_query,
)


def test_source_record_count_uses_count_pattern() -> None:
    """_SOURCE_RECORD_COUNT uses Neo4j 5 count{} instead of OPTIONAL MATCH + count()."""
    assert "count {" in _SOURCE_RECORD_COUNT
    assert "count(sr)" not in _SOURCE_RECORD_COUNT
    assert "coalesce(link.is_active, true) = true" in _SOURCE_RECORD_COUNT
    assert "lifecycle_status = 'active'" in _SOURCE_RECORD_COUNT


def test_identifier_count_uses_count_pattern() -> None:
    """_IDENTIFIER_COUNT uses count{} instead of OPTIONAL MATCH + count()."""
    assert "count {" in _IDENTIFIER_COUNT
    assert "count(idc)" not in _IDENTIFIER_COUNT
    assert "coalesce(id_count.is_active, true) = true" in _IDENTIFIER_COUNT


def test_bankruptcy_case_count_uses_count_pattern() -> None:
    """_BANKRUPTCY_CASE_COUNT uses count{} instead of OPTIONAL MATCH + count()."""
    assert "count {" in _BANKRUPTCY_CASE_COUNT
    assert "count(bankruptcy_rel)" not in _BANKRUPTCY_CASE_COUNT
    assert "coalesce(bankruptcy_rel.is_active, true) = true" in _BANKRUPTCY_CASE_COUNT


def test_count_pattern_preserves_where_clauses_in_list_query() -> None:
    """The count{} change preserves the lifecycle and is_active WHERE patterns."""
    query = build_list_persons_query(None, None, has_q=False)
    compact = " ".join(query.split())
    assert "lifecycle_status = 'active'" in compact
    assert "coalesce(link.is_active, true) = true" in compact


def test_get_person_by_id_uses_count_pattern_for_source_records() -> None:
    """GET_PERSON_BY_ID uses count{} for source_record_count."""
    assert "count {" in GET_PERSON_BY_ID
    assert "count(sr) AS source_record_count" not in GET_PERSON_BY_ID


def test_get_person_by_id_no_longer_includes_loyalty_rows() -> None:
    """GET_PERSON_BY_ID no longer fetches loyalty raw_payload blobs."""
    assert "loyalty_rows" not in GET_PERSON_BY_ID
    assert "raw_payload" not in GET_PERSON_BY_ID


def test_get_person_by_id_no_longer_includes_vehicles() -> None:
    """GET_PERSON_BY_ID no longer fetches vehicle data."""
    assert "OWNS_VEHICLE" not in GET_PERSON_BY_ID
    assert "BOUGHT_VEHICLE" not in GET_PERSON_BY_ID
    assert "vehicles" not in GET_PERSON_BY_ID


def test_get_person_by_id_still_includes_connection_count_and_lifetime_value() -> None:
    """GET_PERSON_BY_ID still computes connection_count and lifetime_value."""
    assert "connection_count" in GET_PERSON_BY_ID
    assert "lifetime_value" in GET_PERSON_BY_ID
    assert "PURCHASED" in GET_PERSON_BY_ID


def test_get_person_by_id_still_resolves_merge_chain() -> None:
    """GET_PERSON_BY_ID still resolves MERGED_INTO canonical person."""
    assert "MERGED_INTO" in GET_PERSON_BY_ID
    assert "coalesce(canonical, p)" in GET_PERSON_BY_ID


def test_get_person_loyalty_query_exists_and_fetches_raw_payload() -> None:
    """GET_PERSON_LOYALTY fetches loyalty rows with raw_payload (deferred from detail)."""
    assert "loyalty_rows" in GET_PERSON_LOYALTY
    assert "raw_payload" in GET_PERSON_LOYALTY
    assert "record_type: 'identity'" in GET_PERSON_LOYALTY
    assert "MERGED_INTO" in GET_PERSON_LOYALTY


def test_get_person_vehicles_query_exists_and_traverses_vehicle_rels() -> None:
    """GET_PERSON_VEHICLES fetches vehicles via OWNS_VEHICLE/BOUGHT_VEHICLE."""
    assert "OWNS_VEHICLE" in GET_PERSON_VEHICLES
    assert "BOUGHT_VEHICLE" in GET_PERSON_VEHICLES
    assert "vehicles" in GET_PERSON_VEHICLES
    assert "MERGED_INTO" in GET_PERSON_VEHICLES


def test_identifiers_query_batches_source_record_lookups() -> None:
    """GET_PERSON_IDENTIFIERS batches source-record lookups across the page."""
    assert "WITH collect({" in GET_PERSON_IDENTIFIERS
    assert "UNWIND page AS item" in GET_PERSON_IDENTIFIERS
    assert "UNWIND item.source_record_pks AS source_record_pk" in GET_PERSON_IDENTIFIERS
    assert (
        "OPTIONAL MATCH (sr:SourceRecord {source_record_pk: source_record_pk})"
        in GET_PERSON_IDENTIFIERS
    )
    # The old implementation ran one correlated CALL block per identifier and
    # repeated the source-record traversal in a second CALL block.
    assert "CALL {\n  WITH source_record_pks" not in GET_PERSON_IDENTIFIERS
    assert "OWNED_BY" in GET_PERSON_IDENTIFIERS
    assert "OPERATED_BY" in GET_PERSON_IDENTIFIERS
    assert "sr_entity_pairs" in GET_PERSON_IDENTIFIERS


def test_identifiers_query_paginates_before_call_block() -> None:
    """GET_PERSON_IDENTIFIERS paginates BEFORE the CALL block so only page
    identifiers are enriched, not all identifiers."""
    call_idx = GET_PERSON_IDENTIFIERS.index("CALL {")
    order_idx = GET_PERSON_IDENTIFIERS.index("ORDER BY is_active DESC")
    skip_idx = GET_PERSON_IDENTIFIERS.index("SKIP $skip LIMIT $limit")
    assert order_idx < call_idx, "ORDER BY should come before CALL block"
    assert skip_idx < call_idx, "SKIP/LIMIT should come before CALL block"
    return_idx = GET_PERSON_IDENTIFIERS.index("RETURN item.identifier.identifier_type")
    final_order_idx = GET_PERSON_IDENTIFIERS.index("ORDER BY item.is_active DESC", return_idx)
    assert final_order_idx > return_idx
