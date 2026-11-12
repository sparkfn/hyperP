"""Real-Neo4j coverage for the API person-list summary counter (#489).

Set ``HYPERP_NEO4J_PERSON_LIST_SUMMARY_TEST_URI`` to a disposable Bolt URI to
run these tests. CI sets ``HYPERP_NEO4J_PERSON_LIST_SUMMARY_TEST_REQUIRED=1`` so a
missing database configuration fails rather than silently skipping coverage.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlparse
from uuid import uuid4

import pytest
import src.repositories.neo4j.merge as merge_module
import src.repositories.neo4j.person as person_module
import src.repositories.neo4j.report as report_module
from neo4j import (
    AsyncDriver,
    AsyncManagedTransaction,
    AsyncGraphDatabase,
    AsyncSession,
    Driver,
    GraphDatabase,
)
from neo4j.exceptions import Neo4jError
from src.graph.queries.person_list_summary import (
    CREATE_PERSON_LIST_SUMMARY_COUNTER_CONSTRAINT,
    ENSURE_PERSON_LIST_SUMMARY_COUNTER,
    PERSON_LIST_SUMMARY_COUNTER_KEY,
)
from src.graph.person_list_summary_deltas import PersonListSummaryDeltaCollector
from src.graph.queries.persons_list import (
    GET_PERSON_LIST_CORE_SUMMARY,
    GET_PERSON_LIST_CRM_SUMMARY,
)
from src.repositories.neo4j.merge import Neo4jMergeRepository
from src.repositories.neo4j.person import Neo4jPersonRepository
from src.repositories.neo4j.report import Neo4jReportRepository

_URI_ENV = "HYPERP_NEO4J_PERSON_LIST_SUMMARY_TEST_URI"
_USER_ENV = "HYPERP_NEO4J_PERSON_LIST_SUMMARY_TEST_USER"
_PASSWORD_ENV = "HYPERP_NEO4J_PERSON_LIST_SUMMARY_TEST_PASSWORD"
_SERVICE_HOST_ENV = "HYPERP_NEO4J_PERSON_LIST_SUMMARY_TEST_SERVICE_HOST"
_REQUIRED_ENV = "HYPERP_NEO4J_PERSON_LIST_SUMMARY_TEST_REQUIRED"
_RUN_PROPERTY = "_person_list_summary_api_test_run"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True)
class _TestGraph:
    driver: Driver
    uri: str
    user: str
    password: str
    run_id: str


@pytest.fixture
def neo4j_graph() -> Iterator[_TestGraph]:
    """Connect only to a disposable database and preserve any prior counter."""
    uri = os.getenv(_URI_ENV)
    if uri is None:
        if os.getenv(_REQUIRED_ENV) == "1":
            pytest.fail(f"{_URI_ENV} is required for the person-list summary Neo4j suite")
        pytest.skip("disposable person-list summary Neo4j database is not configured")

    parsed = urlparse(uri)
    if parsed.scheme not in {"bolt", "bolt+s", "bolt+ssc"}:
        pytest.fail("person-list summary tests require a Bolt URI")
    if parsed.username or parsed.password or "@" in parsed.netloc:
        pytest.fail("person-list summary URI must not carry credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        pytest.fail("person-list summary URI must not select a database")
    service_host = os.getenv(_SERVICE_HOST_ENV)
    allowed_hosts = set(_LOOPBACK_HOSTS)
    if service_host:
        allowed_hosts.add(service_host)
    if parsed.hostname not in allowed_hosts:
        pytest.fail("person-list summary tests require a loopback or configured service host")

    password = os.getenv(_PASSWORD_ENV)
    if not password:
        pytest.fail(f"{_PASSWORD_ENV} is required")
    user = os.getenv(_USER_ENV, "neo4j")
    driver = GraphDatabase.driver(uri, auth=(user, password), connection_timeout=5)
    run_id = uuid4().hex
    prior_counter: dict[str, object] | None = None
    connected = False
    counter_touched = False
    try:
        driver.verify_connectivity()
        connected = True
        with driver.session() as session:
            session.run(CREATE_PERSON_LIST_SUMMARY_COUNTER_CONSTRAINT).consume()
            prior = session.run(
                """
                OPTIONAL MATCH (counter:PersonListSummaryCounter {key: $key})
                RETURN properties(counter) AS properties
                """,
                key=PERSON_LIST_SUMMARY_COUNTER_KEY,
            ).single(strict=True)
            raw_properties = prior["properties"]
            prior_counter = dict(raw_properties) if isinstance(raw_properties, dict) else None
            counter_touched = True
            session.run(
                ENSURE_PERSON_LIST_SUMMARY_COUNTER,
                key=PERSON_LIST_SUMMARY_COUNTER_KEY,
            ).consume()
        yield _TestGraph(
            driver=driver,
            uri=uri,
            user=user,
            password=password,
            run_id=run_id,
        )
    finally:
        if connected:
            with driver.session() as session:
                session.run(
                    f"MATCH (node {{{_RUN_PROPERTY}: $run_id}}) DETACH DELETE node",
                    run_id=run_id,
                ).consume()
                if counter_touched and prior_counter is None:
                    session.run(
                        """
                        MATCH (counter:PersonListSummaryCounter {key: $key})
                        DELETE counter
                        """,
                        key=PERSON_LIST_SUMMARY_COUNTER_KEY,
                    ).consume()
                elif counter_touched:
                    session.run(
                        """
                        MERGE (counter:PersonListSummaryCounter {key: $key})
                        SET counter = $properties
                        """,
                        key=PERSON_LIST_SUMMARY_COUNTER_KEY,
                        properties=prior_counter,
                    ).consume()
        driver.close()


def test_database_month_padding_handles_january_and_november(
    neo4j_graph: _TestGraph,
) -> None:
    with neo4j_graph.driver.session() as session:
        rows = list(
            session.run(
                """
                UNWIND [1, 11] AS month_number
                RETURN toString(2026) + '-' + right('0' + toString(month_number), 2)
                  AS month_marker
                ORDER BY month_marker
                """
            )
        )
    assert [row["month_marker"] for row in rows] == ["2026-01", "2026-11"]


def _seed_people_and_shared_deal(test_graph: _TestGraph) -> tuple[str, str]:
    """Seed a legacy Person without flags and a Person sharing one active deal."""
    absorbed_id = f"summary-absorbed-{test_graph.run_id}"
    survivor_id = f"summary-survivor-{test_graph.run_id}"
    with test_graph.driver.session() as session:
        session.run(
            f"""
            MERGE (source:SourceSystem {{source_key: 'bitrix_chat'}})
            ON CREATE SET source.{_RUN_PROPERTY} = $run_id
            WITH source
            CREATE (absorbed:Person {{
              person_id: $absorbed_id, status: 'active', {_RUN_PROPERTY}: $run_id
            }})
            CREATE (survivor:Person {{
              person_id: $survivor_id, status: 'active', is_high_risk: false,
              is_high_value: true, {_RUN_PROPERTY}: $run_id
            }})
            CREATE (deal:SourceRecord {{
              source_record_pk: $deal_pk, record_type: 'crm_deal',
              lifecycle_status: 'active', is_latest: true, observed_at: datetime(),
              {_RUN_PROPERTY}: $run_id
            }})
            CREATE (deal)-[:FROM_SOURCE]->(source)
            CREATE (deal)-[:LINKED_TO {{is_active: true}}]->(absorbed)
            CREATE (deal)-[:LINKED_TO {{is_active: true}}]->(survivor)
            """,
            run_id=test_graph.run_id,
            absorbed_id=absorbed_id,
            survivor_id=survivor_id,
            deal_pk=f"summary-deal-{test_graph.run_id}",
        ).consume()
    return absorbed_id, survivor_id


def _authoritative_counts(test_graph: _TestGraph) -> dict[str, int]:
    """Read the retained full-scan aggregates, not the counter under test."""
    with test_graph.driver.session() as session:
        core = session.run(GET_PERSON_LIST_CORE_SUMMARY).single(strict=True)
        crm = session.run(GET_PERSON_LIST_CRM_SUMMARY).single(strict=True)
    return {
        "all_profiles_count": int(core["all_profiles_count"] or 0),
        "high_risk_count": int(core["high_risk_count"] or 0),
        "high_value_count": int(core["high_value_count"] or 0),
        "no_contact_count": int(core["no_contact_count"] or 0),
        "deals_this_month_count": int(crm["deals_this_month_count"] or 0),
        "all_deals_count": int(crm["all_deals_count"] or 0),
    }


def _publish_counter_from_aggregates(test_graph: _TestGraph) -> dict[str, int]:
    """Initialize the disposable singleton from the authoritative aggregates."""
    counts = _authoritative_counts(test_graph)
    with test_graph.driver.session() as session:
        session.run(
            """
            MATCH (counter:PersonListSummaryCounter {key: $key})
            WITH counter, datetime.truncate('month', datetime()) AS current_month
            SET counter.active_person_count = $active_person_count,
                counter.high_risk_count = $high_risk_count,
                counter.high_value_count = $high_value_count,
                counter.no_contact_count = $no_contact_count,
                counter.deals_this_month_count = $deals_this_month_count,
                counter.all_deals_count = $all_deals_count,
                counter.ready = true,
                counter.generation = 1,
                counter.month = toString(current_month.year) + '-' +
                  right('0' + toString(current_month.month), 2),
                counter.updated_at = datetime()
            """,
            key=PERSON_LIST_SUMMARY_COUNTER_KEY,
            active_person_count=counts["all_profiles_count"],
            high_risk_count=counts["high_risk_count"],
            high_value_count=counts["high_value_count"],
            no_contact_count=counts["no_contact_count"],
            deals_this_month_count=counts["deals_this_month_count"],
            all_deals_count=counts["all_deals_count"],
        ).consume()
    return counts


def _counter_counts(test_graph: _TestGraph) -> dict[str, int]:
    """Read the stored values, mapping the Person count to its API field."""
    with test_graph.driver.session() as session:
        row = session.run(
            """
            MATCH (counter:PersonListSummaryCounter {key: $key})
            RETURN counter.ready AS ready,
                   counter.active_person_count AS active_person_count,
                   counter.high_risk_count AS high_risk_count,
                   counter.high_value_count AS high_value_count,
                   counter.no_contact_count AS no_contact_count,
                   counter.deals_this_month_count AS deals_this_month_count,
                   counter.all_deals_count AS all_deals_count
            """,
            key=PERSON_LIST_SUMMARY_COUNTER_KEY,
        ).single(strict=True)
    assert row["ready"] is True
    return {
        "all_profiles_count": int(row["active_person_count"]),
        "high_risk_count": int(row["high_risk_count"]),
        "high_value_count": int(row["high_value_count"]),
        "no_contact_count": int(row["no_contact_count"]),
        "deals_this_month_count": int(row["deals_this_month_count"]),
        "all_deals_count": int(row["all_deals_count"]),
    }


def _counter_properties(test_graph: _TestGraph) -> dict[str, object]:
    with test_graph.driver.session() as session:
        row = session.run(
            """
            OPTIONAL MATCH (counter:PersonListSummaryCounter {key: $key})
            RETURN properties(counter) AS properties
            """,
            key=PERSON_LIST_SUMMARY_COUNTER_KEY,
        ).single(strict=True)
    properties = row["properties"]
    return dict(properties) if isinstance(properties, dict) else {}


def _graph_snapshot(test_graph: _TestGraph) -> tuple[tuple[object, ...], ...]:
    """Capture tagged nodes and every relationship touching the test graph."""
    with test_graph.driver.session() as session:
        nodes = list(
            session.run(
                f"""
                MATCH (node {{{_RUN_PROPERTY}: $run_id}})
                RETURN elementId(node) AS element_id, labels(node) AS labels,
                       properties(node) AS properties
                ORDER BY element_id
                """,
                run_id=test_graph.run_id,
            )
        )
        relationships = list(
            session.run(
                f"""
                MATCH (left)-[relationship]-(right)
                WHERE left.{_RUN_PROPERTY} = $run_id
                   OR right.{_RUN_PROPERTY} = $run_id
                RETURN elementId(relationship) AS element_id, type(relationship) AS type,
                       elementId(startNode(relationship)) AS start_id,
                       elementId(endNode(relationship)) AS end_id,
                       properties(relationship) AS properties
                ORDER BY element_id
                """,
                run_id=test_graph.run_id,
            )
        )
    node_snapshot = tuple(
        (
            row["element_id"],
            tuple(row["labels"]),
            dict(row["properties"]),
        )
        for row in nodes
    )
    relationship_snapshot = tuple(
        (
            row["element_id"],
            row["type"],
            row["start_id"],
            row["end_id"],
            dict(row["properties"]),
        )
        for row in relationships
    )
    return node_snapshot + relationship_snapshot


@asynccontextmanager
async def _api_driver(test_graph: _TestGraph) -> AsyncIterator[AsyncDriver]:
    """Use the configured test credentials as the API does for Neo4j access."""
    driver = AsyncGraphDatabase.driver(
        test_graph.uri,
        auth=(test_graph.user, test_graph.password),
        connection_timeout=5,
    )
    try:
        await driver.verify_connectivity()
        yield driver
    finally:
        await driver.close()


def _install_repository_sessions(
    monkeypatch: pytest.MonkeyPatch,
    driver: AsyncDriver,
) -> None:
    """Use the application's READ/WRITE access modes with the disposable auth."""

    @asynccontextmanager
    async def _get_session(write: bool = False) -> AsyncIterator[AsyncSession]:
        access_mode = "WRITE" if write else "READ"
        async with driver.session(default_access_mode=access_mode) as session:
            yield session

    monkeypatch.setattr(person_module, "get_session", _get_session)
    monkeypatch.setattr(merge_module, "get_session", _get_session)
    monkeypatch.setattr(report_module, "get_session", _get_session)


@pytest.mark.anyio
async def test_keyed_api_read_matches_all_six_authoritative_aggregates(
    neo4j_graph: _TestGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ready keyed read agrees with scans when one deal has two Person links."""
    _seed_people_and_shared_deal(neo4j_graph)
    expected = _publish_counter_from_aggregates(neo4j_graph)
    assert expected["all_deals_count"] == 1
    assert expected["deals_this_month_count"] == 1

    async with _api_driver(neo4j_graph) as driver:
        _install_repository_sessions(monkeypatch, driver)
        summary = await Neo4jPersonRepository().get_list_summary()

    assert summary.model_dump() == expected


@pytest.mark.anyio
async def test_manual_merge_repository_keeps_counter_equal_to_authority(
    neo4j_graph: _TestGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    absorbed_id, survivor_id = _seed_people_and_shared_deal(neo4j_graph)
    with neo4j_graph.driver.session() as session:
        flags = session.run(
            """
            MATCH (person:Person {person_id: $person_id})
            RETURN person.is_high_risk IS NULL AS risk_missing,
                   person.is_high_value IS NULL AS value_missing
            """,
            person_id=absorbed_id,
        ).single(strict=True)
    assert flags["risk_missing"] is True
    assert flags["value_missing"] is True
    expected_before = _publish_counter_from_aggregates(neo4j_graph)

    def _ignore_recalculation(_review_case_ids: list[str]) -> None:
        return None

    monkeypatch.setattr(merge_module, "enqueue_match_recalculation", _ignore_recalculation)
    async with _api_driver(neo4j_graph) as driver:
        _install_repository_sessions(monkeypatch, driver)
        outcome = await Neo4jMergeRepository().manual_merge(
            absorbed_id,
            survivor_id,
            "integration test merge",
            "summary-test@example.com",
            [],
        )

    assert outcome.merge_event_id is not None
    with neo4j_graph.driver.session() as session:
        session.run(
            """
            MATCH (event:MergeEvent {merge_event_id: $merge_event_id})
            SET event._person_list_summary_api_test_run = $run_id
            """,
            merge_event_id=outcome.merge_event_id,
            run_id=neo4j_graph.run_id,
        ).consume()

    expected_after = _authoritative_counts(neo4j_graph)
    stored_after = _counter_counts(neo4j_graph)
    assert expected_after["all_profiles_count"] == expected_before["all_profiles_count"] - 1
    assert stored_after == expected_after


@pytest.mark.anyio
async def test_unmerge_revalidates_survivor_after_a_concurrent_merge(
    neo4j_graph: _TestGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    absorbed_id, original_survivor_id = _seed_people_and_shared_deal(neo4j_graph)
    third_person_id = f"summary-third-{neo4j_graph.run_id}"
    phone = "+6580000111"
    with neo4j_graph.driver.session() as session:
        session.run(
            f"""
            CREATE (third:Person {{person_id: $third_id, status: 'active',
              is_high_risk: false, is_high_value: false, {_RUN_PROPERTY}: $run_id}})
            CREATE (phone:Identifier {{identifier_id: $identifier_id,
              identifier_type: 'phone', identifier_scope: 'default',
              normalized_value: $phone, {_RUN_PROPERTY}: $run_id}})
            WITH phone
            MATCH (absorbed:Person {{person_id: $absorbed_id}})
            CREATE (absorbed)-[:IDENTIFIED_BY {{is_active: true, is_verified: true,
              quality_flag: 'valid', last_confirmed_at: datetime(),
              source_system_key: 'summary_test',
              source_record_pk: $source_record_pk}}]->(phone)
            """,
            third_id=third_person_id,
            identifier_id=f"summary-identifier-{neo4j_graph.run_id}",
            source_record_pk=f"summary-source-{neo4j_graph.run_id}",
            phone=phone,
            run_id=neo4j_graph.run_id,
            absorbed_id=absorbed_id,
        ).consume()
    _publish_counter_from_aggregates(neo4j_graph)

    def _ignore_recalculation(_review_case_ids: list[str]) -> None:
        return None

    monkeypatch.setattr(merge_module, "enqueue_match_recalculation", _ignore_recalculation)
    async with _api_driver(neo4j_graph) as driver:
        _install_repository_sessions(monkeypatch, driver)
        repository = Neo4jMergeRepository()
        first = await repository.manual_merge(
            absorbed_id,
            original_survivor_id,
            "first race merge",
            "summary-race@example.com",
            [],
        )
        assert first.merge_event_id is not None
        with neo4j_graph.driver.session() as session:
            session.run(
                """
                MATCH (event:MergeEvent {merge_event_id: $merge_event_id})
                SET event._person_list_summary_api_test_run = $run_id
                """,
                merge_event_id=first.merge_event_id,
                run_id=neo4j_graph.run_id,
            ).consume()

        target_read = asyncio.Event()
        resume_unmerge = asyncio.Event()
        gate_open = True
        original_capture = PersonListSummaryDeltaCollector.capture_before

        async def _gate_unmerge_capture(
            collector: PersonListSummaryDeltaCollector,
            tx: AsyncManagedTransaction,
            person_ids: Sequence[str] = (),
            source_record_pks: Sequence[str] = (),
            deal_element_ids: Sequence[str] = (),
        ) -> None:
            nonlocal gate_open
            if gate_open and absorbed_id in person_ids and original_survivor_id in person_ids:
                gate_open = False
                target_read.set()
                await resume_unmerge.wait()
            await original_capture(
                collector,
                tx,
                person_ids=person_ids,
                source_record_pks=source_record_pks,
                deal_element_ids=deal_element_ids,
            )

        monkeypatch.setattr(
            PersonListSummaryDeltaCollector, "capture_before", _gate_unmerge_capture
        )
        unmerge_task = asyncio.create_task(
            repository.unmerge(
                first.merge_event_id,
                "race unmerge",
                "summary-race@example.com",
            )
        )
        await asyncio.wait_for(target_read.wait(), timeout=30)
        try:
            second = await repository.manual_merge(
                original_survivor_id,
                third_person_id,
                "concurrent survivor merge",
                "summary-race@example.com",
                [],
            )
            assert second.merge_event_id is not None
            with neo4j_graph.driver.session() as session:
                session.run(
                    """
                    MATCH (event:MergeEvent {merge_event_id: $merge_event_id})
                    SET event._person_list_summary_api_test_run = $run_id
                    """,
                    merge_event_id=second.merge_event_id,
                    run_id=neo4j_graph.run_id,
                ).consume()
        finally:
            resume_unmerge.set()
        assert await asyncio.wait_for(unmerge_task, timeout=60) == (
            absorbed_id,
            third_person_id,
        )

    with neo4j_graph.driver.session() as session:
        session.run(
            f"""
            MATCH (event:MergeEvent)-[:SURVIVOR]->(person:Person)
            WHERE person.person_id IN $person_ids
            SET event.{_RUN_PROPERTY} = $run_id
            """,
            person_ids=[absorbed_id, original_survivor_id, third_person_id],
            run_id=neo4j_graph.run_id,
        ).consume()

    expected = _authoritative_counts(neo4j_graph)
    assert _counter_counts(neo4j_graph) == expected
    assert expected["all_profiles_count"] == 2
    assert expected["no_contact_count"] == 1


@pytest.mark.anyio
async def test_report_execute_cannot_mutate_the_graph_or_counter(
    neo4j_graph: _TestGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    absorbed_id, _survivor_id = _seed_people_and_shared_deal(neo4j_graph)
    _publish_counter_from_aggregates(neo4j_graph)
    graph_before = _graph_snapshot(neo4j_graph)
    counter_before = _counter_properties(neo4j_graph)

    async with _api_driver(neo4j_graph) as driver:
        _install_repository_sessions(monkeypatch, driver)
        repository = Neo4jReportRepository()
        readable = await repository.execute(
            "MATCH (person:Person {person_id: $person_id}) RETURN person.person_id AS person_id",
            {"person_id": absorbed_id},
        )
        assert readable.row_count == 1
        mutation_succeeded = False
        try:
            await repository.execute(
                """
                MATCH (person:Person {person_id: $person_id})
                SET person.preferred_phone = '+6580000000'
                RETURN person.person_id AS person_id
                """,
                {"person_id": absorbed_id},
            )
        except Neo4jError:
            pass
        else:
            mutation_succeeded = True

    graph_after = _graph_snapshot(neo4j_graph)
    counter_after = _counter_properties(neo4j_graph)
    if mutation_succeeded:
        pytest.fail(
            "BLOCKER: Neo4jReportRepository.execute accepted a write with app-equivalent "
            "credentials; counter cutover is unsafe until report writes are prevented."
        )
    assert graph_after == graph_before
    assert counter_after == counter_before
