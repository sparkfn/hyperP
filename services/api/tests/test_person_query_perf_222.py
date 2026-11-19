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
    COUNT_PERSON_IDENTIFIERS,
    FIND_PERSON_BY_IDENTIFIER,
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


def test_person_summary_connection_queries_collect_each_path_before_the_next_match() -> None:
    for query, address_binding in (
        (GET_PERSON_BY_ID, "[person_address:LIVES_AT]"),
        (FIND_PERSON_BY_IDENTIFIER, "[p_address:LIVES_AT]"),
    ):
        assert "identifier_conn" in query
        assert "address_conn" in query
        assert "knows_conn" in query
        assert "identifier_conn + address_conn + knows_conn" in query
        assert "collect(DISTINCT ci) + collect(DISTINCT ca) + collect(DISTINCT ck)" not in query
        # The identifier projection (HAS/legacy collection or the legacy
        # aggregate) must be fully collapsed into `identifier_conn` before the
        # address traversal starts, so the two paths cannot form a cartesian
        # product of collected nodes.
        identifier_collected = query.index("AS identifier_conn")
        address_start = query.index(address_binding)
        assert identifier_collected < address_start
        assert query.index("collect(DISTINCT ca) AS address_conn") < query.index("AS knows_conn")


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
    assert "ELSE item.source_record_pks" in GET_PERSON_IDENTIFIERS
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
    """GET_PERSON_IDENTIFIERS paginates BEFORE the enrichment CALL block so only
    page identifiers are enriched, not all identifiers."""
    # The leading CALL is the HAS/legacy UNION that builds the identifier
    # universe; pagination must still precede the per-row enrichment CALL.
    enrichment_idx = GET_PERSON_IDENTIFIERS.index("CALL {\n  WITH page")
    order_idx = GET_PERSON_IDENTIFIERS.index("ORDER BY is_active DESC")
    skip_idx = GET_PERSON_IDENTIFIERS.index("SKIP $skip LIMIT $limit")
    assert order_idx < enrichment_idx, "pagination must precede the enrichment CALL"
    assert skip_idx < enrichment_idx, "pagination must precede the enrichment CALL"
    assert order_idx < skip_idx
    assert GET_PERSON_IDENTIFIERS.index("CALL {\n  WITH p") < order_idx
    return_idx = GET_PERSON_IDENTIFIERS.index("RETURN item.identifier.identifier_type")
    final_order_idx = GET_PERSON_IDENTIFIERS.index("ORDER BY item.is_active DESC", return_idx)
    assert final_order_idx > return_idx


def test_identifiers_query_preserves_empty_provenance_items() -> None:
    """Identifiers with no source-record provenance are retained."""
    assert "THEN [null]" in GET_PERSON_IDENTIFIERS
    assert "UNWIND CASE WHEN size(item.source_record_pks) = 0" in GET_PERSON_IDENTIFIERS


def test_identifiers_entity_subquery_only_returns_entities() -> None:
    """The entity aggregate cannot drop provenance fields owned by the outer scope."""
    _, _, nested_call = GET_PERSON_IDENTIFIERS.rpartition("  CALL {")
    entity_subquery, _, _ = nested_call.partition(
        "  }\n  RETURN item, source_records, source_record_ids, entities"
    )

    assert "WITH sr_entity_pairs" in entity_subquery
    assert "WITH item, source_records, source_record_ids, sr_entity_pairs" not in entity_subquery
    assert "RETURN [entity IN collect" in entity_subquery
    assert "RETURN item, source_records, source_record_ids, entities" not in entity_subquery
    assert "RETURN item, source_records, source_record_ids, entities" in GET_PERSON_IDENTIFIERS


def test_identifiers_count_query_matches_data_row_grouping() -> None:
    """The total counts distinct Identifier nodes over the same HAS-union-legacy
    universe as the data query, not raw provenance edges."""
    compact_count = " ".join(COUNT_PERSON_IDENTIFIERS.split())
    assert "MATCH (p)-[:HAS_IDENTIFIER {status: 'active'}]->(id:Identifier)" in compact_count
    assert "MATCH (p)-[legacy:IDENTIFIED_BY]->(id:Identifier)" in compact_count
    assert "NOT EXISTS { MATCH (p)-[:HAS_IDENTIFIER {status: 'active'}]->(id) }" in compact_count
    assert "RETURN count(DISTINCT id) AS total" in COUNT_PERSON_IDENTIFIERS
    # The data query must use the identical universe: active HAS rows plus
    # legacy rows without an active HAS projection.
    compact_data = " ".join(GET_PERSON_IDENTIFIERS.split())
    assert "MATCH (p)-[has:HAS_IDENTIFIER {status: 'active'}]->(id:Identifier)" in compact_data
    assert "NOT EXISTS { MATCH (p)-[:HAS_IDENTIFIER {status: 'active'}]->(id) }" in compact_data


def test_identifiers_query_aggregates_multi_edge_provenance_per_identifier() -> None:
    """Every Identifier node must yield exactly one row, with edge properties aggregated."""
    assert "WITH id," in GET_PERSON_IDENTIFIERS
    assert "any(r IN collect(rel) WHERE r.is_active = true) AS is_active" in GET_PERSON_IDENTIFIERS
    assert (
        "any(r IN collect(rel) WHERE r.is_verified = true) AS is_verified" in GET_PERSON_IDENTIFIERS
    )
    assert "max(rel.last_confirmed_at) AS last_confirmed_at" in GET_PERSON_IDENTIFIERS
    assert "collect(DISTINCT rel.source_record_pk) AS source_record_pks" in GET_PERSON_IDENTIFIERS
    # The scalar edge projections must not remain grouping keys, otherwise two
    # edges to the same Identifier would produce two rows for one identifier.
    assert "rel.is_active AS is_active" not in GET_PERSON_IDENTIFIERS
    assert "rel.source_system_key AS source_system_key" not in GET_PERSON_IDENTIFIERS


def test_lazy_endpoint_queries_return_person_marker() -> None:
    """Loyalty/vehicle queries return a canonical person marker for 404 handling."""
    assert "person.person_id AS person_id" in GET_PERSON_LOYALTY
    assert "person.person_id AS person_id" in GET_PERSON_VEHICLES
    assert "OPTIONAL MATCH" in GET_PERSON_LOYALTY
