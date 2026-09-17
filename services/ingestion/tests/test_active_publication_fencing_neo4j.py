"""Active disposable-Neo4j publication and materializer fencing contracts."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import TypeVar, cast
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase, ManagedTransaction, Session
from src.crm_deal_identity_repair.control_models import RepairControlRequest
from src.graph.client import Neo4jClient
from src.graph.crm_deal_identity_repair_control import CrmDealRepairControlRepository
from src.graph.queries.crm_deal_identity_repair_control import (
    CREATE_CRM_DEAL_REPAIR_CONTROL_SCHEMA,
)
from src.graph.queries.sales import LINK_PERSON_PURCHASED_ORDER
from src.graph.queries.vehicle import LINK_PERSON_BOUGHT_VEHICLE

T = TypeVar("T")
_CONTROL_INSTANCE_ID = "active-ci-publication-control"
_BOUNDARY_DIGEST = "sha256:" + "a" * 64
_CONTROL_SCHEMA_NAMES = {
    "crm_deal_repair_control_run_unique",
    "crm_deal_repair_publication_reservation_unique",
    "crm_deal_repair_allocation_completion_unique",
    "crm_deal_repair_control_state",
}


class _Client:
    def __init__(self, driver: Driver) -> None:
        self._driver = driver

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self._driver.session() as session:
            yield session

    def execute_read(self, work: Callable[[ManagedTransaction], T]) -> T:
        with self._driver.session() as session:
            return session.execute_read(work)

    def execute_write(self, work: Callable[[ManagedTransaction], T]) -> T:
        with self._driver.session() as session:
            return session.execute_write(work)


def _clear_fixture_data(driver: Driver) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (node) "
            "WHERE coalesce(node.control_instance_id, '') STARTS WITH 'active-ci-' "
            "OR coalesce(node.repair_id, '') STARTS WITH 'active-ci-' "
            "OR coalesce(node.person_id, '') STARTS WITH 'active-ci-' "
            "OR coalesce(node.source_system_key, '') STARTS WITH 'active-ci-' "
            "OR coalesce(node.vehicle_id, '') STARTS WITH 'active-ci-' "
            "DETACH DELETE node"
        ).consume()


@pytest.fixture(scope="session")
def neo4j_driver() -> Iterator[Driver]:
    uri = os.getenv("HYPERP_NEO4J_ACTIVE_PUBLICATION_TEST_URI")
    user = os.getenv("HYPERP_NEO4J_ACTIVE_PUBLICATION_TEST_USER")
    password = os.getenv("HYPERP_NEO4J_ACTIVE_PUBLICATION_TEST_PASSWORD")
    if uri is None or user is None or password is None:
        pytest.skip("active publication Neo4j database is not configured")
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    service_host = os.getenv("HYPERP_NEO4J_ACTIVE_PUBLICATION_TEST_SERVICE_HOST")
    if service_host:
        allowed_hosts.add(service_host)
    if urlparse(uri).hostname not in allowed_hosts:
        pytest.fail("active publication tests require an explicitly disposable Neo4j host")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        driver.verify_connectivity()
        _initialize_control_schema(driver)
        yield driver
    finally:
        _clear_fixture_data(driver)
        driver.close()


@pytest.fixture(autouse=True)
def _reset_fixture_data(neo4j_driver: Driver) -> Iterator[None]:
    _clear_fixture_data(neo4j_driver)
    yield
    _clear_fixture_data(neo4j_driver)


def _initialize_control_schema(driver: Driver) -> None:
    with driver.session() as session:
        for statement in CREATE_CRM_DEAL_REPAIR_CONTROL_SCHEMA:
            session.run(statement).consume()


def _seed_claimable_control(
    driver: Driver,
    repair_id: str,
) -> tuple[CrmDealRepairControlRepository, str]:
    run_id = f"{repair_id}-run"
    with driver.session() as session:
        session.run(
            "CREATE (:CrmDealRepairRun {repair_id: $repair_id, run_id: $run_id, "
            "status: 'qualified', boundary_digest: $boundary_digest, "
            "control_instance_id: $control_instance_id, execution_allowed: false}) "
            "CREATE (:BitrixDispatchControl {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id, blocked: false, repair_revision: 0})",
            repair_id=repair_id,
            run_id=run_id,
            boundary_digest=_BOUNDARY_DIGEST,
            control_instance_id=_CONTROL_INSTANCE_ID,
        ).consume()
    client = _Client(driver)
    return CrmDealRepairControlRepository(cast(Neo4jClient, client)), run_id


def _claim(
    control: CrmDealRepairControlRepository,
    repair_id: str,
    run_id: str,
) -> None:
    control.claim(
        RepairControlRequest(repair_id, run_id, "owner", "token", 0),
        boundary_digest=_BOUNDARY_DIGEST,
        control_instance_id=_CONTROL_INSTANCE_ID,
    )


def test_unsettled_publication_blocks_repair_claim(neo4j_driver: Driver) -> None:
    control, run_id = _seed_claimable_control(neo4j_driver, "active-ci-publication")
    reservation = control.prepare_publication(_CONTROL_INSTANCE_ID, "active-ci-generation")

    assert reservation.state == "preparing"
    with pytest.raises(RuntimeError, match="compare-and-set"):
        _claim(control, "active-ci-publication", run_id)


def test_publication_reservation_and_repair_claim_are_mutually_exclusive(
    neo4j_driver: Driver,
) -> None:
    repair_id = "active-ci-publication-race"
    control, run_id = _seed_claimable_control(neo4j_driver, repair_id)

    def reserve() -> bool:
        try:
            control.prepare_publication(_CONTROL_INSTANCE_ID, "active-ci-race")
        except RuntimeError:
            return False
        return True

    def claim() -> bool:
        try:
            _claim(control, repair_id, run_id)
        except RuntimeError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda operation: operation(), (reserve, claim)))
    assert sorted(outcomes) == [False, True]


def test_stale_publication_confirmation_fails_closed(neo4j_driver: Driver) -> None:
    control, _run_id = _seed_claimable_control(neo4j_driver, "active-ci-publication-confirm")
    reservation = control.prepare_publication(_CONTROL_INSTANCE_ID, "active-ci-confirm")
    publishing = control.mark_publishing(reservation)

    with pytest.raises(RuntimeError, match="rejected"):
        control.confirm_publication(reservation, "active-ci-task")
    assert control.confirm_publication(publishing, "active-ci-task").state == "confirmed"


def test_shared_publication_control_schema_remains_initialized(neo4j_driver: Driver) -> None:
    with neo4j_driver.session() as session:
        names = set(
            session.run(
                "SHOW CONSTRAINTS YIELD name "
                "WHERE name STARTS WITH 'crm_deal_repair_' RETURN name"
            ).value("name")
        )
        names.update(
            session.run(
                "SHOW INDEXES YIELD name "
                "WHERE name STARTS WITH 'crm_deal_repair_' RETURN name"
            ).value("name")
        )
    assert _CONTROL_SCHEMA_NAMES <= names


def _materializer_parameters(prefix: str) -> dict[str, object]:
    return {
        "person_id": f"{prefix}-person",
        "source_system_key": f"{prefix}-source",
        "source_order_id": f"{prefix}-order",
        "source_record_pk": f"{prefix}-record",
        "vehicle_id": f"{prefix}-vehicle",
        "raw_context": prefix,
        "observed_at": "2026-09-02T00:00:00+00:00",
        "confidence": 1.0,
        "quality_flag": "verified",
        "is_active": True,
    }


def _materializer_counts(driver: Driver, parameters: dict[str, object]) -> dict[str, int]:
    with driver.session() as session:
        row = session.run(
            "MATCH (person:Person {person_id: $person_id})-[purchase:PURCHASED]->(:Order) "
            "WITH person, count(purchase) AS purchases, "
            "count(CASE WHEN purchase.is_active THEN purchase END) AS active_purchases "
            "MATCH (person)-[vehicle:BOUGHT_VEHICLE]->(:Vehicle) "
            "WITH purchases, active_purchases, count(vehicle) AS vehicles, "
            "count(CASE WHEN vehicle.is_active THEN vehicle END) AS active_vehicles, "
            "count(CASE WHEN vehicle.is_active AND vehicle.retired_at IS NOT NULL "
            "THEN vehicle END) AS contradictory_vehicles "
            "MATCH (:Person {person_id: $person_id})-[purchase_again:PURCHASED]->(:Order) "
            "RETURN purchases, active_purchases, vehicles, active_vehicles, "
            "contradictory_vehicles, count(CASE WHEN purchase_again.is_active "
            "AND purchase_again.retired_at IS NOT NULL THEN purchase_again END) "
            "AS contradictory_purchases",
            **parameters,
        ).single(strict=True)
    return {key: int(row[key]) for key in row.keys()}


def test_sales_materializers_preserve_retired_edges(neo4j_driver: Driver) -> None:
    parameters = _materializer_parameters("active-ci")
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (person:Person {person_id: $person_id}) "
            "CREATE (order:Order {source_system_key: $source_system_key, "
            "source_order_id: $source_order_id}) "
            "CREATE (vehicle:Vehicle {vehicle_id: $vehicle_id}) "
            "CREATE (person)-[:PURCHASED {source_system_key: $source_system_key, "
            "source_order_id: $source_order_id, is_active: true, retired_at: datetime()}]->(order) "
            "CREATE (person)-[:PURCHASED {source_system_key: $source_system_key, "
            "source_order_id: $source_order_id, is_active: false, "
            "retired_at: datetime()}]->(order) "
            "CREATE (person)-[:BOUGHT_VEHICLE {source_system_key: $source_system_key, "
            "source_order_id: $source_order_id, is_active: true, "
            "retired_at: datetime()}]->(vehicle) "
            "CREATE (person)-[:BOUGHT_VEHICLE {source_system_key: $source_system_key, "
            "source_order_id: $source_order_id, is_active: false, "
            "retired_at: datetime()}]->(vehicle)",
            **parameters,
        ).consume()
        for _ in range(2):
            session.run(LINK_PERSON_PURCHASED_ORDER, **parameters).consume()
            session.run(LINK_PERSON_BOUGHT_VEHICLE, **parameters).consume()
    assert _materializer_counts(neo4j_driver, parameters) == {
        "purchases": 2,
        "active_purchases": 1,
        "vehicles": 2,
        "active_vehicles": 1,
        "contradictory_vehicles": 0,
        "contradictory_purchases": 0,
    }


def test_sales_materializers_normalize_legacy_active_edges(neo4j_driver: Driver) -> None:
    parameters = _materializer_parameters("active-ci-legacy")
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (person:Person {person_id: $person_id}) "
            "CREATE (order:Order {source_system_key: $source_system_key, "
            "source_order_id: $source_order_id}) "
            "CREATE (vehicle:Vehicle {vehicle_id: $vehicle_id}) "
            "CREATE (person)-[:PURCHASED {source_system_key: $source_system_key, "
            "source_order_id: $source_order_id}]->(order) "
            "CREATE (person)-[:BOUGHT_VEHICLE {source_system_key: $source_system_key, "
            "source_order_id: $source_order_id}]->(vehicle)",
            **parameters,
        ).consume()
        for _ in range(2):
            session.run(LINK_PERSON_PURCHASED_ORDER, **parameters).consume()
            session.run(LINK_PERSON_BOUGHT_VEHICLE, **parameters).consume()
    assert _materializer_counts(neo4j_driver, parameters) == {
        "purchases": 1,
        "active_purchases": 1,
        "vehicles": 1,
        "active_vehicles": 1,
        "contradictory_vehicles": 0,
        "contradictory_purchases": 0,
    }
