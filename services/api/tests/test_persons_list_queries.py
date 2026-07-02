from __future__ import annotations

from src.graph.queries.persons_list import build_count_persons_query, build_list_persons_query


def test_person_listing_connection_count_excludes_identifier_only_connections() -> None:
    query = build_list_persons_query("connection_count", "desc", has_q=False)

    # Shared identifiers legitimately appear elsewhere in the query (match
    # filters, possible_match_count) — only the connection_count subquery must
    # exclude identifier-only connections.
    conn_block = next(
        block for block in query.split("CALL (p) {") if "AS connection_count" in block
    )
    assert "[:IDENTIFIED_BY]->(:Identifier)<-[:IDENTIFIED_BY]" not in conn_block
    assert "[:LIVES_AT]->(:Address)<-[:LIVES_AT]" in conn_block
    assert "[:KNOWS]-(ck:Person)" in conn_block


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


def test_list_query_filters_dob_by_component() -> None:
    query = build_list_persons_query("profile_completeness_score", "desc", has_q=False)

    # Year/month/day each guarded by IS NULL and matched via substring on YYYY-MM-DD.
    assert "$dob_year  IS NULL OR substring(p.preferred_dob, 0, 4) = $dob_year" in query
    assert "$dob_month IS NULL OR substring(p.preferred_dob, 5, 2) = $dob_month" in query
    assert "$dob_day   IS NULL OR substring(p.preferred_dob, 8, 2) = $dob_day" in query


def test_count_query_filters_dob_by_component() -> None:
    query = build_count_persons_query(has_q=False, has_addr_filter=False)

    # The count query must apply the same component filters as the list query.
    assert "substring(p.preferred_dob, 0, 4) = $dob_year" in query
    assert "substring(p.preferred_dob, 5, 2) = $dob_month" in query
    assert "substring(p.preferred_dob, 8, 2) = $dob_day" in query


# The unanchored MatchDecision EXISTS (starting the MATCH from MatchDecision and
# only filtering down to p in a WHERE) forces Neo4j to enumerate the full
# left×right MatchDecision set for every person before SKIP/LIMIT, which times
# the endpoint out. Every MatchDecision lookup must anchor on p first and then
# verify both ABOUT_LEFT and ABOUT_RIGHT persons exist (the both-sides
# requirement is real, so it must be preserved — not dropped).
_UNANCHORED = "MATCH (md:MatchDecision)-[:ABOUT_LEFT]->(:Person)"
_ANCHORED = "MATCH (md:MatchDecision)-[:ABOUT_LEFT|ABOUT_RIGHT]->(p)"


def _assert_anchored_match_decision(query: str) -> None:
    # No occurrence of the unanchored anti-pattern.
    assert _UNANCHORED not in query
    # Every md-on-p lookup keeps the both-sides requirement via EXISTS guards.
    for fragment in query.split(_ANCHORED)[1:]:
        head = fragment[:200]
        assert "EXISTS { (md)-[:ABOUT_LEFT]->(:Person) }" in head
        assert "EXISTS { (md)-[:ABOUT_RIGHT]->(:Person) }" in head


def test_list_query_anchors_match_decision_filters_on_person() -> None:
    query = build_list_persons_query("profile_completeness_score", "desc", has_q=False)

    # has_system_match (true + false branches), has_any_match (true + false),
    # and the system_match_count CALL block: five anchored lookups total.
    assert query.count(_ANCHORED) == 5
    _assert_anchored_match_decision(query)


def test_count_query_anchors_match_decision_filters_on_person() -> None:
    # The count path shares _COMMON_FILTER_CLAUSE, so the same fix must apply,
    # but it has no system_match_count CALL block: four anchored lookups.
    query = build_count_persons_query(has_q=False, has_addr_filter=False)

    assert query.count(_ANCHORED) == 4
    _assert_anchored_match_decision(query)


def test_has_any_match_keeps_identifier_branch_anchored_on_person() -> None:
    # The IDENTIFIED_BY branch of has_any_match was already anchored to p and
    # must stay so — the fix only touches the MatchDecision branch.
    query = build_list_persons_query("profile_completeness_score", "desc", has_q=False)

    assert "MATCH (p)-[:IDENTIFIED_BY]->(:Identifier)<-[:IDENTIFIED_BY]-(am:Person)" in query


def test_entity_and_mode_requires_every_key() -> None:
    # AND mode emits ALL(... EXISTS ...) per key (intersection); it must NOT use
    # the OR `entity_key IN $entity_keys` form. Guards the persons-list entity
    # "and" toggle (multi-key AND vs OR) at the builder level.
    query = build_list_persons_query(
        "preferred_full_name", "asc", has_q=False, entity_mode="and", source_mode="or"
    )
    assert "ALL(ek IN $entity_keys WHERE EXISTS" in query
    assert "WHERE e.entity_key = ek" in query
    assert "e.entity_key IN $entity_keys" not in query


def test_entity_or_mode_matches_any_key() -> None:
    query = build_list_persons_query(
        "preferred_full_name", "asc", has_q=False, entity_mode="or", source_mode="or"
    )
    assert "e.entity_key IN $entity_keys" in query
    assert "ALL(ek IN $entity_keys" not in query


def test_source_and_mode_requires_every_key() -> None:
    query = build_list_persons_query(
        "preferred_full_name", "asc", has_q=False, entity_mode="or", source_mode="and"
    )
    assert "ALL(sk IN $source_keys WHERE EXISTS" in query
    assert "WHERE ss.source_key = sk" in query
    assert "ss.source_key IN $source_keys" not in query
