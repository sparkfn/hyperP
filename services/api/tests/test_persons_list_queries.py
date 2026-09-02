from __future__ import annotations

import re

from src.graph.queries import persons as person_queries
from src.graph.queries.entities import (
    LIST_ENTITIES,
    LIST_FILTER_SOURCE_SYSTEMS,
    get_entity_persons_query,
)
from src.graph.queries.persons_list import (
    GET_PERSON_LIST_SUMMARY,
    build_count_persons_query,
    build_list_persons_query,
)


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

    listing = build_list_persons_query(
        "bankruptcy_case_count",
        "desc",
        has_q=False,
        active_filters=frozenset({"has_bankruptcy_case"}),
    )
    assert listing.count("coalesce(bankruptcy_rel.is_active, true) = true") >= 2


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
    query = " ".join(
        build_list_persons_query(
            "source_record_count",
            "asc",
            has_q=False,
            active_filters=frozenset({"source_keys"}),
        ).split()
    )

    assert query.count("lifecycle_status = 'active'") >= 3
    assert query.count("coalesce(link.is_active, true) = true") >= 3
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


def test_entity_summary_traverses_ownership_before_aggregating() -> None:
    compact = " ".join(LIST_ENTITIES.split())

    assert LIST_ENTITIES.count("CALL (e) {") == 2
    assert "CALL {\n  WITH e" not in LIST_ENTITIES
    assert "MATCH (e)<-[:OWNED_BY]-(sr:SourceRecord)-[link:LINKED_TO]->(p:Person)" in compact
    assert "MATCH (e)<-[:OPERATED_BY]-(:SourceSystem)<-[:FROM_SOURCE]-(sr:SourceRecord)" in compact
    assert "AND NOT EXISTS { MATCH (sr)-[:OWNED_BY]->(:Entity) }" in compact
    assert "RETURN count(DISTINCT p) AS person_count" in compact
    assert "count(DISTINCT sr) AS source_record_count" in compact


def test_entity_queries_support_record_scoped_ownership_with_source_fallback() -> None:
    queries = (
        LIST_ENTITIES,
        get_entity_persons_query("preferred_full_name", "asc"),
        build_list_persons_query(
            "preferred_full_name",
            "asc",
            has_q=False,
            active_filters=frozenset({"entity_keys"}),
        ),
        person_queries.GET_PERSON_SOURCE_RECORDS,
        person_queries.COUNT_PERSON_SOURCE_RECORDS,
        person_queries.GET_PERSON_SOURCE_RECORD_ENTITY_FACETS,
        person_queries.GET_PERSON_ENTITIES,
        person_queries.GET_PERSON_IDENTIFIERS,
    )

    for query in queries:
        assert "OWNED_BY" in query
        assert "OPERATED_BY" in query

    for query in queries[:3]:
        assert "NOT EXISTS" in query


def test_address_filters_only_join_active_address_assertions() -> None:
    for has_q in (False, True):
        query = build_count_persons_query(
            has_q=has_q,
            active_filters=frozenset({"addr_city"}),
        )
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


def test_possible_match_detail_uses_record_owner_with_source_fallback() -> None:
    query = person_queries.GET_PERSON_POSSIBLE_MATCH_DETAIL

    assert query.count("OPTIONAL MATCH (sr)-[:OWNED_BY]->(record_entity:Entity)") == 2
    assert query.count("OPTIONAL MATCH (ss)-[:OPERATED_BY]->(source_entity:Entity)") == 2
    assert query.count("coalesce(record_entity, source_entity) AS entity") == 2
    assert query.count("entity_display_name: entity.display_name") == 2


def test_person_list_match_filters_and_counts_ignore_retired_identifier_assertions() -> None:
    query = build_list_persons_query(
        "preferred_full_name",
        "asc",
        has_q=False,
        active_filters=frozenset({"has_any_match", "has_possible_match", "has_system_match"}),
    )

    assert "{is_active: true}" not in query
    assert query.count("coalesce(") >= 8


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


def test_detail_audit_and_matches_anchor_from_the_selected_person() -> None:
    audit = person_queries.GET_PERSON_AUDIT
    audit_count = person_queries.COUNT_PERSON_AUDIT
    matches = person_queries.GET_PERSON_MATCHES
    matches_count = person_queries.COUNT_PERSON_MATCHES

    for query in (audit, audit_count):
        assert "MATCH (p:Person {person_id: $person_id})" in query
        assert "MATCH (event_person:Person)-[:MERGED_INTO*0..]->(p)" in query
        assert "MATCH (me:MergeEvent)-[:ABSORBED|SURVIVOR]->(event_person)" in query
    for query in (matches, matches_count):
        assert "MATCH (p:Person {person_id: $person_id})" in query
        assert "MATCH (md:MatchDecision)-[:ABOUT_LEFT|ABOUT_RIGHT]->(p)" in query
        assert "WITH DISTINCT md" in query

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


def test_connection_sources_use_record_ownership_with_source_fallback() -> None:
    for query in (
        person_queries.GET_PERSON_CONNECTIONS_ADDRESS,
        person_queries.GET_PERSON_CONNECTIONS_KNOWS,
        person_queries.GET_PERSON_CONNECTIONS_ALL,
    ):
        assert "source_record_pk" in query
        assert "OWNED_BY" in query
        assert "OPERATED_BY" in query
        assert "coalesce(record_entity, source_entity)" in query


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
    assert "WITH p, collect(DISTINCT ca) AS address_conn" in conn_block
    assert "WITH address_conn, collect(DISTINCT ck) AS knows_conn" in conn_block
    assert "WITH address_conn + knows_conn AS all_conn" in conn_block
    assert "collect(DISTINCT ca) + collect(DISTINCT ck)" not in conn_block
    assert conn_block.index("AS address_conn") < conn_block.index("[p_knows:KNOWS]")
    assert conn_block.index("AS knows_conn") < conn_block.index("address_conn + knows_conn")


def test_person_listing_includes_and_sorts_by_possible_match_count() -> None:
    query = build_list_persons_query("possible_match_count", "desc", has_q=False)
    possible_match_block = next(
        block for block in query.split("CALL (p) {") if "AS possible_match_count" in block
    )

    assert "possible_match_count" in query
    assert "coalesce(p_shared_id.is_active, true) = true" in possible_match_block
    assert "coalesce(other_shared_id.is_active, true) = true" in possible_match_block
    assert "WITH DISTINCT p, shared_id" in possible_match_block
    assert "WITH DISTINCT other" in possible_match_block
    assert "shared_id IS NOT NULL" in possible_match_block
    assert "other.person_id <> p.person_id" in possible_match_block
    assert "other.status <> 'merged'" in possible_match_block
    assert "count(DISTINCT other) AS possible_match_count" not in possible_match_block
    assert "RETURN count(other) AS possible_match_count" in possible_match_block
    assert possible_match_block.index("WITH DISTINCT p, shared_id") < possible_match_block.index(
        "OPTIONAL MATCH (shared_id)<-"
    )
    assert possible_match_block.index("WITH DISTINCT other") < possible_match_block.index(
        "RETURN count(other)"
    )
    assert "ORDER BY possible_match_count DESC, p.person_id ASC" in query
    assert "ORDER BY possible_match_count DESC, person.person_id ASC" in query


def test_crm_desc_paginates_before_possible_match_enrichment() -> None:
    query = build_list_persons_query("crm_deal_count", "desc", has_q=False)

    assert query.index("SKIP $skip LIMIT $limit") < query.index("AS possible_match_count")


def test_stored_sort_paginates_before_enrichment() -> None:
    query = build_list_persons_query("profile_completeness_score", "desc", has_q=False)

    order_by_pos = query.index("ORDER BY p.profile_completeness_score DESC, p.person_id ASC")
    first_call_pos = query.index("CALL (p)")
    assert order_by_pos < first_call_pos
    assert query.rstrip().endswith(
        "ORDER BY person.profile_completeness_score DESC, person.person_id ASC"
    )


def test_computed_sort_paginates_after_enrichment() -> None:
    query = build_list_persons_query("source_record_count", "asc", has_q=False)

    order_by_pos = query.index("ORDER BY source_record_count ASC, person.person_id ASC")
    first_call_pos = query.index("CALL (p)")
    assert order_by_pos > first_call_pos


def test_computed_sort_only_calculates_selected_metric_before_pagination() -> None:
    query = build_list_persons_query("connection_count", "desc", has_q=False)

    page_pos = query.index("SKIP $skip LIMIT $limit")
    before_page = query[:page_pos]
    after_page = query[page_pos:]

    assert "AS connection_count" in before_page
    assert "AS source_record_count" not in before_page
    assert "AS possible_match_count" not in before_page
    assert "AS connection_count" not in after_page
    assert "AS source_record_count" in after_page


def test_entity_count_sort_collects_entities_only_after_pagination() -> None:
    query = build_list_persons_query("entity_count", "desc", has_q=False)

    page_pos = query.index("SKIP $skip LIMIT $limit")
    before_page = query[:page_pos]
    after_page = query[page_pos:]

    assert "RETURN count(entity) AS entity_count" in before_page
    assert "collect({" not in before_page
    assert "collect({" in after_page


def test_stored_sort_fulltext_paginates_before_enrichment() -> None:
    query = build_list_persons_query("relevance", "desc", has_q=True)

    order_by_pos = query.index("ORDER BY score DESC, p.person_id ASC")
    first_call_pos = query.index("CALL (p)")
    assert order_by_pos < first_call_pos


def test_person_list_sorts_use_person_id_as_stable_pagination_tiebreaker() -> None:
    stored_query = build_list_persons_query("preferred_full_name", "asc", has_q=False)
    computed_query = build_list_persons_query("connection_count", "desc", has_q=False)
    fulltext_query = build_list_persons_query("relevance", "desc", has_q=True)

    assert "ORDER BY p.preferred_full_name ASC, p.person_id ASC" in stored_query
    assert "ORDER BY connection_count DESC, person.person_id ASC" in computed_query
    assert "ORDER BY score DESC, p.person_id ASC" in fulltext_query


def test_enrich_and_return_uses_neo4j5_call_syntax() -> None:
    query = build_list_persons_query("profile_completeness_score", "desc", has_q=False)

    assert "CALL (p) {" in query
    assert "CALL {\n  WITH p" not in query


def test_count_query_skips_address_join_when_no_addr_filter() -> None:
    query = build_count_persons_query(has_q=False)

    assert "OPTIONAL MATCH (p)-[:LIVES_AT]->(addr:Address)\n" not in query
    assert "null AS addr" in query
    assert "address_id: p.preferred_address_id" not in query
    assert "WITH DISTINCT p, score" not in query


def test_count_query_includes_address_join_when_addr_filter_active() -> None:
    query = build_count_persons_query(
        has_q=False,
        active_filters=frozenset({"addr_city"}),
    )

    assert "OPTIONAL MATCH (p)-[addr_link:LIVES_AT]->(addr:Address)\n" in query
    assert "coalesce(addr_link.is_active, true) = true" in query
    assert "WITH DISTINCT p, score" in query


def test_person_list_preferred_address_hydration_does_not_expand_provenance_edges() -> None:
    address_filter_query = build_list_persons_query(
        "preferred_full_name",
        "asc",
        has_q=False,
        active_filters=frozenset({"addr_city"}),
    )
    queries = (
        build_list_persons_query("profile_completeness_score", "desc", has_q=False),
        build_list_persons_query("connection_count", "desc", has_q=False),
        build_list_persons_query("relevance", "desc", has_q=True),
        address_filter_query,
    )

    direct_lookup = "OPTIONAL MATCH (addr:Address {address_id: p.preferred_address_id})"

    for query in queries:
        preferred_address_lines = [
            line.strip()
            for line in query.splitlines()
            if "address_id: p.preferred_address_id" in line
        ]
        assert preferred_address_lines == [direct_lookup]

    assert "OPTIONAL MATCH (p)-[addr_link:LIVES_AT]->(addr:Address)" in address_filter_query


def test_default_queries_omit_inactive_expensive_filter_traversals() -> None:
    list_query = build_list_persons_query("profile_completeness_score", "desc", has_q=False)
    count_query = build_count_persons_query(has_q=False)

    for query in (list_query, count_query):
        assert "p_any_id:IDENTIFIED_BY" not in query
        assert "p_possible_id:IDENTIFIED_BY" not in query
        assert "$has_any_match" not in query
        assert "$has_possible_match" not in query
        assert "$has_system_match" not in query
        assert "$has_bankruptcy_case" not in query

    assert "HAS_BANKRUPTCY_CASE" not in count_query
    assert "MatchDecision)-[:ABOUT_LEFT|ABOUT_RIGHT]->(p)" not in count_query


def test_scalar_filters_are_emitted_only_when_active() -> None:
    expected_fragments = {
        "is_high_value": "p.is_high_value = $is_high_value",
        "has_phone": "(p.preferred_phone IS NOT NULL) = $has_phone",
        "has_any_contact": "$has_any_contact",
        "updated_after": "p.updated_at >= datetime($updated_after)",
        "has_dob": "(p.preferred_dob IS NOT NULL) = $has_dob",
        "dob_year": "substring(p.preferred_dob, 0, 4) = $dob_year",
        "has_address": "(p.preferred_address_id IS NOT NULL) = $has_address",
    }

    default_query = build_count_persons_query(has_q=False)
    for filter_name, fragment in expected_fragments.items():
        assert fragment not in default_query
        filtered_query = build_count_persons_query(
            has_q=False,
            active_filters=frozenset({filter_name}),
        )
        assert fragment in filtered_query


def test_source_or_mode_and_record_type_emit_only_requested_filters() -> None:
    query = build_count_persons_query(
        has_q=False,
        active_filters=frozenset({"source_keys", "source_record_type"}),
        source_mode="or",
    )

    assert "ss.source_key IN $source_keys" in query
    assert "ALL(sk IN $source_keys" not in query
    assert "sr_t.record_type = $source_record_type" in query
    assert "$entity_keys" not in query
    assert "WITH DISTINCT p, score" not in query


def test_person_list_summary_uses_one_person_scan_with_conditional_counts() -> None:
    assert GET_PERSON_LIST_SUMMARY.count("MATCH (p:Person)") == 1
    assert "p.status <> 'merged'" in GET_PERSON_LIST_SUMMARY
    assert "all_profiles_count" in GET_PERSON_LIST_SUMMARY
    assert "high_risk_count" in GET_PERSON_LIST_SUMMARY
    assert "high_value_count" in GET_PERSON_LIST_SUMMARY
    assert "no_contact_count" in GET_PERSON_LIST_SUMMARY


def test_list_query_filters_dob_by_component() -> None:
    query = build_list_persons_query(
        "profile_completeness_score",
        "desc",
        has_q=False,
        active_filters=frozenset({"dob_year", "dob_month", "dob_day"}),
    )

    assert "substring(p.preferred_dob, 0, 4) = $dob_year" in query
    assert "substring(p.preferred_dob, 5, 2) = $dob_month" in query
    assert "substring(p.preferred_dob, 8, 2) = $dob_day" in query


def test_count_query_filters_dob_by_component() -> None:
    query = build_count_persons_query(
        has_q=False,
        active_filters=frozenset({"dob_year", "dob_month", "dob_day"}),
    )

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
    query = build_list_persons_query(
        "profile_completeness_score",
        "desc",
        has_q=False,
        active_filters=frozenset({"has_any_match", "has_system_match"}),
    )

    # The active any/system filters plus the returned system-match count are
    # all anchored on the candidate person.
    assert query.count(_ANCHORED) == 3
    _assert_anchored_match_decision(query)


def test_count_query_anchors_match_decision_filters_on_person() -> None:
    query = build_count_persons_query(
        has_q=False,
        active_filters=frozenset({"has_any_match", "has_system_match"}),
    )

    assert query.count(_ANCHORED) == 2
    _assert_anchored_match_decision(query)


def test_has_any_match_keeps_identifier_branch_anchored_on_person() -> None:
    # The IDENTIFIED_BY branch of has_any_match was already anchored to p and
    # must stay so — the fix only touches the MatchDecision branch.
    query = build_list_persons_query(
        "profile_completeness_score",
        "desc",
        has_q=False,
        active_filters=frozenset({"has_any_match"}),
    )

    assert "MATCH (p)-[p_any_id:IDENTIFIED_BY]->(:Identifier)" in query
    assert "<-[am_id:IDENTIFIED_BY]-(am:Person)" in query


def test_entity_and_mode_requires_every_key() -> None:
    # AND mode emits ALL(... EXISTS ...) per key (intersection); it must NOT use
    # the OR `entity_key IN $entity_keys` form. Guards the persons-list entity
    # "and" toggle (multi-key AND vs OR) at the builder level.
    query = build_list_persons_query(
        "preferred_full_name",
        "asc",
        has_q=False,
        active_filters=frozenset({"entity_keys"}),
        entity_mode="and",
        source_mode="or",
    )
    assert "ALL(ek IN $entity_keys WHERE EXISTS" in query
    assert "OWNED_BY" in query
    assert "e.entity_key = ek" in query
    assert "e.entity_key IN $entity_keys" not in query


def test_entity_or_mode_matches_any_key() -> None:
    query = build_list_persons_query(
        "preferred_full_name",
        "asc",
        has_q=False,
        active_filters=frozenset({"entity_keys"}),
        entity_mode="or",
        source_mode="or",
    )
    assert "e.entity_key IN $entity_keys" in query
    assert "ALL(ek IN $entity_keys" not in query


def test_source_and_mode_requires_every_key() -> None:
    query = build_list_persons_query(
        "preferred_full_name",
        "asc",
        has_q=False,
        active_filters=frozenset({"source_keys"}),
        entity_mode="or",
        source_mode="and",
    )
    assert "ALL(sk IN $source_keys WHERE EXISTS" in query
    assert "AND ss.source_key = sk" in query
    assert "ss.source_key IN $source_keys" not in query


def _assert_no_crm_deal_aggregation(query: str) -> None:
    normalized = " ".join(query.lower().split())
    assert re.search(r"\bcrm_deal\b", normalized) is None
    assert "bitrix_chat" not in normalized
    assert re.search(r"count\s*\([^)]*\)\s+as\s+crm_deal_count", normalized) is None
    assert re.search(r"count\s*\{[^}]*crm_deal", normalized) is None
    for forbidden in (
        "coalesce(p.crm_deal_count",
        "valuetype(p.crm_deal_count)",
        "tointeger(p.crm_deal_count)",
        "tofloat(p.crm_deal_count)",
    ):
        assert forbidden not in normalized


def test_default_list_projects_stored_crm_deal_count_only_after_pagination() -> None:
    query = build_list_persons_query("profile_completeness_score", "desc", has_q=False)

    page_pos = query.index("SKIP $skip LIMIT $limit")
    assert "p.crm_deal_count" not in query[:page_pos]
    assert "p.crm_deal_count AS crm_deal_count" in query[page_pos:]
    _assert_no_crm_deal_aggregation(query)


def test_crm_deal_count_sort_uses_stored_property_and_stable_tiebreaker() -> None:
    query = build_list_persons_query("crm_deal_count", "desc", has_q=False)
    page_pos = query.index("SKIP $skip LIMIT $limit")

    assert "WHERE p.crm_deal_count IS NOT NULL" in query[:page_pos]
    assert "ORDER BY p.crm_deal_count DESC, p.person_id ASC" in query[:page_pos]
    assert "ORDER BY crm_deal_count DESC, person.person_id ASC" in query[page_pos:]
    _assert_no_crm_deal_aggregation(query)


def test_crm_deal_count_bounds_use_stored_property_before_pagination() -> None:
    query = build_list_persons_query(
        "preferred_full_name",
        "asc",
        has_q=False,
        active_filters=frozenset({"crm_deal_count_min", "crm_deal_count_max"}),
    )

    page_pos = query.index("SKIP $skip LIMIT $limit")
    before_page = query[:page_pos]
    assert "p.crm_deal_count >= $crm_deal_count_min" in before_page
    assert "p.crm_deal_count <= $crm_deal_count_max" in before_page
    assert before_page.index("p.crm_deal_count >=") < before_page.index("WITH p, null")
    _assert_no_crm_deal_aggregation(query)


def test_crm_deal_count_filter_composes_with_computed_sort_without_recalculation() -> None:
    query = build_list_persons_query(
        "connection_count",
        "desc",
        has_q=False,
        active_filters=frozenset({"crm_deal_count_max"}),
    )

    page_pos = query.index("SKIP $skip LIMIT $limit")
    before_page = query[:page_pos]
    after_page = query[page_pos:]
    assert "p.crm_deal_count <= $crm_deal_count_max" in before_page
    assert before_page.count("AS connection_count") == 1
    assert "AS connection_count" not in after_page
    _assert_no_crm_deal_aggregation(query)


def test_count_query_uses_same_stored_crm_predicates_as_list() -> None:
    filters = frozenset({"crm_deal_count_min", "crm_deal_count_max"})
    list_query = build_list_persons_query(
        "crm_deal_count",
        "asc",
        has_q=False,
        active_filters=filters,
    )
    count_query = build_count_persons_query(
        "crm_deal_count",
        "asc",
        has_q=False,
        active_filters=filters,
    )

    for predicate in (
        "p.crm_deal_count IS NOT NULL",
        "p.crm_deal_count >= $crm_deal_count_min",
        "p.crm_deal_count <= $crm_deal_count_max",
    ):
        assert predicate in list_query
        assert predicate in count_query
    _assert_no_crm_deal_aggregation(list_query)
    _assert_no_crm_deal_aggregation(count_query)


def test_fulltext_crm_deal_count_filter_applies_after_person_binding() -> None:
    query = build_list_persons_query(
        "crm_deal_count",
        "desc",
        has_q=True,
        active_filters=frozenset({"crm_deal_count_min"}),
    )

    binding = "WITH p, null AS addr, score"
    predicate = "p.crm_deal_count >= $crm_deal_count_min"
    assert query.index(binding) < query.index(predicate)
    assert "p.crm_deal_count IS NOT NULL" not in query
    _assert_no_crm_deal_aggregation(query)


_COMPLETENESS_SCORE_PREDICATE = "p.profile_completeness_score IS NOT NULL"


def _assert_completeness_predicate_is_in_query_head(
    query: str, *, has_address_filter: bool
) -> None:
    assert query.count(_COMPLETENESS_SCORE_PREDICATE) == 1
    normalized = "\n".join(line.strip() for line in query.splitlines())
    if has_address_filter:
        expected_prefix = "\n".join(
            (
                "MATCH (p:Person)",
                f"WHERE {_COMPLETENESS_SCORE_PREDICATE}",
                "OPTIONAL MATCH (p)-[addr_link:LIVES_AT]->(addr:Address)",
                "WHERE coalesce(addr_link.is_active, true) = true",
                "WITH p, addr, null AS score",
            )
        )
    else:
        expected_prefix = "\n".join(
            (
                "MATCH (p:Person)",
                f"WHERE {_COMPLETENESS_SCORE_PREDICATE}",
                "WITH p, null AS addr, null AS score",
            )
        )
    assert normalized.startswith(expected_prefix)
    assert normalized.index("p.status <> 'merged'") > len(expected_prefix)


def _assert_fulltext_query_head(query: str, *, has_address_filter: bool) -> None:
    normalized = "\n".join(line.strip() for line in query.splitlines())
    call = "CALL db.index.fulltext.queryNodes('person_name_search', $q) YIELD node AS p, score"
    if has_address_filter:
        expected_prefix = "\n".join(
            (
                call,
                "OPTIONAL MATCH (p)-[addr_link:LIVES_AT]->(addr:Address)",
                "WHERE coalesce(addr_link.is_active, true) = true",
                "WITH p, addr, score",
            )
        )
    else:
        expected_prefix = "\n".join((call, "WITH p, null AS addr, score"))
    assert normalized.startswith(expected_prefix)
    assert normalized.index("p.status <> 'merged'") > len(expected_prefix)


def test_non_search_completeness_sort_requires_a_numeric_score_before_the_first_projection() -> (
    None
):
    default_query = build_list_persons_query(None, None, has_q=False)
    descending = build_list_persons_query("profile_completeness_score", "desc", has_q=False)
    ascending = build_list_persons_query("profile_completeness_score", "asc", has_q=False)

    for query in (default_query, descending, ascending):
        _assert_completeness_predicate_is_in_query_head(query, has_address_filter=False)
    assert "ORDER BY p.profile_completeness_score ASC, p.person_id ASC" in ascending


def test_completeness_count_places_the_row_predicate_before_its_first_projection() -> None:
    default_count = build_count_persons_query(None, None, has_q=False)
    explicit_count = build_count_persons_query("profile_completeness_score", "asc", has_q=False)

    _assert_completeness_predicate_is_in_query_head(default_count, has_address_filter=False)
    _assert_completeness_predicate_is_in_query_head(explicit_count, has_address_filter=False)


def test_completeness_query_heads_keep_address_predicates_after_address_binding() -> None:
    filters = frozenset({"addr_city"})
    list_query = build_list_persons_query(
        "profile_completeness_score",
        "desc",
        has_q=False,
        active_filters=filters,
    )
    count_query = build_count_persons_query(
        "profile_completeness_score",
        "desc",
        has_q=False,
        active_filters=filters,
    )

    _assert_completeness_predicate_is_in_query_head(list_query, has_address_filter=True)
    _assert_completeness_predicate_is_in_query_head(count_query, has_address_filter=True)
    assert "toLower(addr.city) CONTAINS toLower($addr_city)" in list_query
    assert "toLower(addr.city) CONTAINS toLower($addr_city)" in count_query


def test_completeness_score_predicate_is_limited_to_non_search_completeness_sorts() -> None:
    fulltext = build_list_persons_query("profile_completeness_score", "desc", has_q=True)
    fulltext_address = build_list_persons_query(
        "profile_completeness_score",
        "desc",
        has_q=True,
        active_filters=frozenset({"addr_city"}),
    )
    stored = build_list_persons_query("preferred_full_name", "asc", has_q=False)
    computed = build_list_persons_query("connection_count", "desc", has_q=False)
    default_count = build_count_persons_query(has_q=False)
    stored_count = build_count_persons_query("preferred_full_name", "asc", has_q=False)
    fulltext_count = build_count_persons_query(
        "profile_completeness_score",
        "desc",
        has_q=True,
    )
    fulltext_address_count = build_count_persons_query(
        "profile_completeness_score",
        "desc",
        has_q=True,
        active_filters=frozenset({"addr_city"}),
    )

    _assert_fulltext_query_head(fulltext, has_address_filter=False)
    _assert_fulltext_query_head(fulltext_address, has_address_filter=True)
    _assert_fulltext_query_head(fulltext_count, has_address_filter=False)
    _assert_fulltext_query_head(fulltext_address_count, has_address_filter=True)
    assert _COMPLETENESS_SCORE_PREDICATE not in fulltext
    assert _COMPLETENESS_SCORE_PREDICATE not in fulltext_address
    assert _COMPLETENESS_SCORE_PREDICATE not in stored
    assert _COMPLETENESS_SCORE_PREDICATE not in computed
    assert _COMPLETENESS_SCORE_PREDICATE in default_count
    assert _COMPLETENESS_SCORE_PREDICATE not in stored_count
    assert _COMPLETENESS_SCORE_PREDICATE not in fulltext_count
    assert _COMPLETENESS_SCORE_PREDICATE not in fulltext_address_count
    address_filter_pos = fulltext_address_count.index(
        "toLower(addr.city) CONTAINS toLower($addr_city)"
    )
    distinct_pos = fulltext_address_count.index("WITH DISTINCT p, score")
    count_pos = fulltext_address_count.index("RETURN count(p) AS total")
    assert address_filter_pos < distinct_pos < count_pos


def test_completeness_score_predicate_composes_with_scalar_and_entity_filters() -> None:
    scalar = build_list_persons_query(
        "profile_completeness_score",
        "desc",
        has_q=False,
        active_filters=frozenset({"is_high_value"}),
    )
    entity = build_list_persons_query(
        "profile_completeness_score",
        "desc",
        has_q=False,
        active_filters=frozenset({"entity_keys"}),
    )
    source_count = build_count_persons_query(
        "profile_completeness_score",
        "desc",
        has_q=False,
        active_filters=frozenset({"source_keys"}),
    )

    _assert_completeness_predicate_is_in_query_head(scalar, has_address_filter=False)
    _assert_completeness_predicate_is_in_query_head(entity, has_address_filter=False)
    _assert_completeness_predicate_is_in_query_head(source_count, has_address_filter=False)
    assert "p.is_high_value = $is_high_value" in scalar
    assert "e.entity_key IN $entity_keys" in entity
    assert "ss.source_key IN $source_keys" in source_count


def test_identifier_pagination_has_complete_deterministic_provenance_tiebreakers() -> None:
    from src.graph.queries.persons import GET_PERSON_IDENTIFIERS

    order = GET_PERSON_IDENTIFIERS.split("SKIP $skip", maxsplit=1)[0]
    assert "coalesce(source_system_key, '')" in order
    assert "coalesce(last_confirmed_at" in order
    assert "coalesce(toString(source_record_pks), '')" in order
    final_order = GET_PERSON_IDENTIFIERS.rsplit("ORDER BY", maxsplit=1)[1]
    assert "coalesce(item.source_system_key, '')" in final_order
    assert "coalesce(item.last_confirmed_at" in final_order
    assert "coalesce(toString(item.source_record_pks), '')" in final_order
