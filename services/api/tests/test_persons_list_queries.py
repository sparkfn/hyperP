from __future__ import annotations

from src.graph.queries.persons_list import build_list_persons_query


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
