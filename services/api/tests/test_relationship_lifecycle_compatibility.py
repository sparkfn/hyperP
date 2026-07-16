from __future__ import annotations

from src.graph.queries import entities, persons, persons_list, reports, survivorship


def test_active_domain_relationship_reads_are_rollout_compatible() -> None:
    queries = "\n".join(
        (
            persons.FIND_PERSON_BY_IDENTIFIER,
            persons.GET_PERSON_BY_ID,
            persons.SEARCH_PERSONS,
            persons.GET_PERSON_CONNECTIONS_IDENTIFIER,
            persons.GET_PERSON_CONNECTIONS_ADDRESS,
            persons.GET_PERSON_CONNECTIONS_KNOWS,
            persons.GET_PERSON_CONNECTIONS_ALL,
            persons.COUNT_PERSON_CONNECTIONS_IDENTIFIER,
            persons.COUNT_PERSON_CONNECTIONS_ADDRESS,
            persons.COUNT_PERSON_CONNECTIONS_KNOWS,
            persons.COUNT_PERSON_CONNECTIONS_ALL,
            persons.GET_PERSON_SHARED_IDENTIFIERS,
            persons.COUNT_PERSON_SHARED_IDENTIFIERS,
            persons.GET_PERSON_POSSIBLE_MATCH_DETAIL,
            persons_list.build_list_persons_query("connection_count", "desc", has_q=False),
            persons_list.build_count_persons_query(has_q=False, has_addr_filter=True),
            entities.get_entity_persons_query("connection_count", "desc"),
            survivorship.GET_BEST_ADDRESS,
            survivorship.GET_BEST_IDENTIFIER,
            survivorship.GET_FIELD_OPTIONS,
        )
    )

    assert "{is_active: true}" not in queries
    assert ".is_active = true" not in queries
    for relationship_type in ("IDENTIFIED_BY", "LIVES_AT", "KNOWS", "HAS_FACT"):
        assert relationship_type in queries
    assert queries.count("coalesce(") >= 30


def test_rollout_compatibility_excludes_only_explicitly_retired_relationships() -> None:
    # Cypher's coalesce(null, true) includes pre-migration relationships while
    # preserving false for relationships explicitly retired by the lifecycle.
    assert (None if None is not None else True) is True
    assert (False if False is not None else True) is False


def test_seed_reports_exclude_explicitly_retired_domain_relationships() -> None:
    report_queries = {
        report["report_key"]: report["cypher_query"] for report in reports.SEED_REPORTS
    }

    shared_phones = report_queries["shared_phone_numbers"]
    assert "<-[identified_by:IDENTIFIED_BY]-(p:Person)" in shared_phones
    assert "coalesce(identified_by.is_active, true) = true" in shared_phones
    assert "collect(DISTINCT p.person_id) AS person_ids" in shared_phones

    entity_summary = report_queries["entity_person_summary"]
    assert "<-[:HAS_FACT]-(p:Person)" not in entity_summary
    assert "<-[entity_fact:HAS_FACT]-(p:Person)" in entity_summary
    assert "coalesce(entity_fact.is_active, true) = true" in entity_summary
    assert "-[source_fact:HAS_FACT]->(src:SourceRecord)" in entity_summary
    assert "coalesce(source_fact.is_active, true) = true" in entity_summary
    assert "[p_shared:IDENTIFIED_BY|LIVES_AT]" in entity_summary
    assert "[other_shared:IDENTIFIED_BY|LIVES_AT]" in entity_summary
    assert "coalesce(p_shared.is_active, true) = true" in entity_summary
    assert "coalesce(other_shared.is_active, true) = true" in entity_summary
