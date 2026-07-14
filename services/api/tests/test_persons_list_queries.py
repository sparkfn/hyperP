from __future__ import annotations

from src.graph.queries import persons as person_queries
from src.graph.queries.entities import (
    LIST_ENTITIES,
    LIST_FILTER_SOURCE_SYSTEMS,
    get_entity_persons_query,
)
from src.graph.queries.persons_list import build_count_persons_query, build_list_persons_query


def test_person_source_reads_select_active_versions_explicitly() -> None:
    assert "sr.lifecycle_status = 'active'" in person_queries.GET_PERSON_SOURCE_RECORDS
    assert "coalesce(sr.is_latest, true)" not in person_queries.GET_PERSON_SOURCE_RECORDS
    assert "sr.lifecycle_status = 'active'" in person_queries.GET_PERSON_BY_ID
    assert "coalesce(sr.is_latest, true)" not in person_queries.GET_PERSON_BY_ID
    effective_active = (
        "sr.lifecycle_status = 'active' OR (sr.lifecycle_status IS NULL AND sr.is_latest = true)"
    )
    for query in (
        person_queries.GET_PERSON_SOURCE_RECORDS,
        person_queries.COUNT_PERSON_SOURCE_RECORDS,
        person_queries.GET_PERSON_SOURCE_RECORD_ENTITY_FACETS,
        person_queries.GET_PERSON_BY_ID,
    ):
        assert effective_active in " ".join(query.split())


def test_bankruptcy_active_domain_reads_exclude_retired_projection_edges() -> None:
    for query in (
        person_queries.GET_PERSON_BANKRUPTCY_CASES,
        person_queries.COUNT_PERSON_BANKRUPTCY_CASES,
    ):
        assert "coalesce(bankruptcy_rel.is_active, true) = true" in query

    listing = build_list_persons_query("bankruptcy_case_count", "desc", has_q=False)
    assert listing.count("coalesce(bankruptcy_rel.is_active, true) = true") >= 3

    assert (None if None is not None else True) is True
    assert (False if False is not None else True) is False


def test_person_summary_counts_only_effective_active_linked_records() -> None:
    effective_active = (
        "sr.lifecycle_status = 'active' OR (sr.lifecycle_status IS NULL AND sr.is_latest = true)"
    )
    for query in (
        person_queries.FIND_PERSON_BY_IDENTIFIER,
        person_queries.GET_PERSON_BY_ID,
        person_queries.SEARCH_PERSONS,
    ):
        compact = " ".join(query.split())
        assert effective_active in compact
        assert "coalesce(link.is_active, true) = true" in compact


def test_person_list_filters_and_counts_only_effective_active_linked_records() -> None:
    query = " ".join(build_list_persons_query("source_record_count", "asc", has_q=False).split())

    assert query.count("lifecycle_status = 'active'") >= 5
    assert query.count("coalesce(link.is_active, true) = true") >= 5
    assert "lifecycle_status IS NULL AND" in query
    assert "is_latest = true" in query


def test_entity_summaries_only_count_effective_active_linked_records() -> None:
    effective_active = (
        "sr.lifecycle_status = 'active' OR (sr.lifecycle_status IS NULL AND sr.is_latest = true)"
    )
    for query in (LIST_ENTITIES, LIST_FILTER_SOURCE_SYSTEMS):
        compact = " ".join(query.split())
        assert effective_active in compact
        assert "coalesce(link.is_active, true) = true" in compact


def test_address_filters_only_join_active_address_assertions() -> None:
    for has_q in (False, True):
        query = build_count_persons_query(has_q=has_q, has_addr_filter=True)
        assert "OPTIONAL MATCH (p)-[addr_link:LIVES_AT]->(addr:Address)" in query
        assert "coalesce(addr_link.is_active, true) = true" in query


def test_entity_person_summary_metrics_only_use_active_lifecycle_assertions() -> None:
    query = get_entity_persons_query("connection_count", "desc")

    for rel_type in ("IDENTIFIED_BY", "LIVES_AT", "KNOWS", "HAS_FACT"):
        assert rel_type in query
    assert "{is_active: true}" not in query
    assert query.count("coalesce(") >= 8
    assert "sr.lifecycle_status = 'active'" in query


def test_person_entities_count_only_effective_active_source_records() -> None:
    query = " ".join(person_queries.GET_PERSON_ENTITIES.split())

    assert (
        "sr.lifecycle_status = 'active' OR (sr.lifecycle_status IS NULL AND sr.is_latest = true)"
    ) in query


def test_shared_identifier_reads_only_traverse_active_relationships() -> None:
    shared_queries = (
        person_queries.GET_PERSON_SHARED_IDENTIFIERS,
        person_queries.COUNT_PERSON_SHARED_IDENTIFIERS,
        person_queries.GET_PERSON_POSSIBLE_MATCH_DETAIL,
    )

    for query in shared_queries:
        assert "{is_active: true}" not in query
        assert query.count("coalesce(") >= 2


def test_possible_match_detail_excludes_superseded_source_record_evidence() -> None:
    query = " ".join(person_queries.GET_PERSON_POSSIBLE_MATCH_DETAIL.split())
    effective_active = (
        "sr.lifecycle_status = 'active' OR (sr.lifecycle_status IS NULL AND sr.is_latest = true)"
    )

    assert query.count(effective_active) == 2


def test_person_list_match_filters_and_counts_ignore_retired_identifier_assertions() -> None:
    query = build_list_persons_query("preferred_full_name", "asc", has_q=False)

    assert "{is_active: true}" not in query
    assert query.count("coalesce(") >= 15


def test_person_connection_and_summary_counts_ignore_retired_identifier_assertions() -> None:
    for query in (
        person_queries.GET_PERSON_BY_ID,
        person_queries.SEARCH_PERSONS,
    ):
        assert "{is_active: true}" not in query
        assert query.count("coalesce(") >= 3

    for query in (
        person_queries.GET_PERSON_CONNECTIONS_IDENTIFIER,
        person_queries.COUNT_PERSON_CONNECTIONS_IDENTIFIER,
    ):
        assert "{is_active: true}" not in query
        assert query.count("coalesce(") >= 2


def test_person_summary_connections_ignore_retired_address_and_knows_assertions() -> None:
    for query in (
        person_queries.FIND_PERSON_BY_IDENTIFIER,
        person_queries.GET_PERSON_BY_ID,
        person_queries.SEARCH_PERSONS,
    ):
        assert "{is_active: true}" not in query
        assert "LIVES_AT" in query and "KNOWS" in query

    listing = build_list_persons_query("connection_count", "desc", has_q=False)
    assert "{is_active: true}" not in listing
    assert "LIVES_AT" in listing and "KNOWS" in listing

    for query in (
        person_queries.GET_PERSON_CONNECTIONS_ADDRESS,
        person_queries.COUNT_PERSON_CONNECTIONS_ADDRESS,
        person_queries.GET_PERSON_CONNECTIONS_KNOWS,
        person_queries.COUNT_PERSON_CONNECTIONS_KNOWS,
        person_queries.GET_PERSON_CONNECTIONS_ALL,
        person_queries.COUNT_PERSON_CONNECTIONS_ALL,
    ):
        assert "{is_active: true}" not in query
        assert "coalesce(" in query


def test_person_listing_connection_count_excludes_identifier_only_connections() -> None:
    query = build_list_persons_query("connection_count", "desc", has_q=False)

    # Shared identifiers legitimately appear elsewhere in the query (match
    # filters, possible_match_count) — only the connection_count subquery must
    # exclude identifier-only connections.
    conn_block = next(
        block for block in query.split("CALL (p) {") if "AS connection_count" in block
    )
    assert "[:IDENTIFIED_BY]->(:Identifier)<-[:IDENTIFIED_BY]" not in conn_block
    assert "LIVES_AT" in conn_block
    assert "KNOWS" in conn_block
    assert "{is_active: true}" not in conn_block


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

    assert "OPTIONAL MATCH (p)-[addr_link:LIVES_AT]->(addr:Address)\n" in query
    assert "coalesce(addr_link.is_active, true) = true" in query


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

    assert "MATCH (p)-[p_any_id:IDENTIFIED_BY]->(:Identifier)" in query
    assert "<-[am_id:IDENTIFIED_BY]-(am:Person)" in query


def test_entity_and_mode_requires_every_key() -> None:
    # AND mode emits ALL(... EXISTS ...) per key (intersection); it must NOT use
    # the OR `entity_key IN $entity_keys` form. Guards the persons-list entity
    # "and" toggle (multi-key AND vs OR) at the builder level.
    query = build_list_persons_query(
        "preferred_full_name", "asc", has_q=False, entity_mode="and", source_mode="or"
    )
    assert "ALL(ek IN $entity_keys WHERE EXISTS" in query
    assert "AND e.entity_key = ek" in query
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
    assert "AND ss.source_key = sk" in query
    assert "ss.source_key IN $source_keys" not in query
