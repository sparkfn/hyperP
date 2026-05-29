from __future__ import annotations

from src.graph.queries.persons_list import build_count_persons_query, build_list_persons_query


def test_person_listing_connection_count_excludes_identifier_only_connections() -> None:
    query = build_list_persons_query("connection_count", "desc", has_q=False)

    assert "[:IDENTIFIED_BY]->(:Identifier)<-[:IDENTIFIED_BY]" not in query
    assert "[:LIVES_AT]->(:Address)<-[:LIVES_AT]" in query
    assert "[:KNOWS]-(ck:Person)" in query


def test_person_listing_includes_and_sorts_by_possible_match_count() -> None:
    query = build_list_persons_query("possible_match_count", "desc", has_q=False)

    assert "possible_match_count" in query
    assert "count(DISTINCT other) AS possible_match_count" in query
    assert "ORDER BY possible_match_count DESC" in query


def test_stored_sort_paginates_before_enrichment() -> None:
    query = build_list_persons_query("profile_completeness_score", "desc", has_q=False)

    order_by_pos = query.index("ORDER BY p.profile_completeness_score DESC")
    first_call_pos = query.index("CALL (p)")
    assert order_by_pos < first_call_pos


def test_computed_sort_paginates_after_enrichment() -> None:
    query = build_list_persons_query("source_record_count", "asc", has_q=False)

    order_by_pos = query.index("ORDER BY source_record_count ASC")
    first_call_pos = query.index("CALL (p)")
    assert order_by_pos > first_call_pos


def test_stored_sort_fulltext_paginates_before_enrichment() -> None:
    query = build_list_persons_query("relevance", "desc", has_q=True)

    order_by_pos = query.index("ORDER BY score DESC")
    first_call_pos = query.index("CALL (p)")
    assert order_by_pos < first_call_pos


def test_enrich_and_return_uses_neo4j5_call_syntax() -> None:
    query = build_list_persons_query("profile_completeness_score", "desc", has_q=False)

    assert "CALL (p) {" in query
    assert "CALL {\n  WITH p" not in query


def test_count_query_skips_address_join_when_no_addr_filter() -> None:
    query = build_count_persons_query(has_q=False, has_addr_filter=False)

    assert "OPTIONAL MATCH (p)-[:LIVES_AT]->(addr:Address)\n" not in query
    assert "null AS addr" in query


def test_count_query_includes_address_join_when_addr_filter_active() -> None:
    query = build_count_persons_query(has_q=False, has_addr_filter=True)

    assert "OPTIONAL MATCH (p)-[:LIVES_AT]->(addr:Address)\n" in query
