"""Disposable-Neo4j coverage for the maintained Person-list summary counter (#489).

The suite runs against a throwaway Neo4j database and is skipped unless the
environment family is configured:

================================================= ==============================
``HYPERP_NEO4J_PERSON_LIST_SUMMARY_TEST_URI``      bolt URI of the disposable database
``HYPERP_NEO4J_PERSON_LIST_SUMMARY_TEST_USER``      user (default ``neo4j``)
``HYPERP_NEO4J_PERSON_LIST_SUMMARY_TEST_PASSWORD``  password (required)
``HYPERP_NEO4J_PERSON_LIST_SUMMARY_TEST_SERVICE_HOST`` exact extra allowed host
================================================= ==============================

With no URI the suite skips. Setting
``HYPERP_NEO4J_PERSON_LIST_SUMMARY_TEST_REQUIRED=1`` turns that skip into a
failure so a CI job cannot silently stop covering the counter.

The URI must address a loopback host or exactly the configured service host, must
not carry credentials, and must not select a database: the suite wipes the graph
after every test, so it may only ever run against a disposable instance.

The maintained counter is never its own oracle: after every writer the stored
six values are compared with independent copies of the original core and CRM
aggregate queries. A writer test never runs a backfill after its writer: the
counter has to be right on its own.

The synthetic benchmark at the end of the file is informational: it discards
three warm-up runs, measures twelve runs of the legacy aggregates against the
keyed counter read and of a collector-bearing write, and prints p50 and p95. It
asserts no timing and claims nothing about any deployed environment.
"""

from __future__ import annotations

import math
import os
import statistics
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase, ManagedTransaction
from src.config import Settings
from src.graph.bootstrap import SOURCE_KEY_TO_ENTITY
from src.graph.client import Neo4jClient
from src.graph.migrations import (
    migrate_source_record_lifecycle,
    reconcile_source_record_lifecycle,
)
from src.graph.person_list_summary import (
    backfill_person_list_summary,
    invalidate_person_list_summary,
    reconcile_person_list_summary,
)
from src.graph.person_list_summary_deltas import PersonListSummaryDeltaCollector
from src.graph.queries.person_list_summary import (
    CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS,
    CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS_BY_ELEMENT_ID,
    CAPTURE_PERSON_SUMMARY_CONTRIBUTIONS,
    CREATE_PERSON_LIST_SUMMARY_COUNTER_CONSTRAINT,
    ENSURE_PERSON_LIST_SUMMARY_COUNTER,
    PERSON_LIST_SUMMARY_COUNTER_KEY,
)
from src.graph.queries.source_records import CREATE_SOURCE_RECORD_IDENTITY_LOCK_CONSTRAINT
from src.graph.queries.staging_reset import STAGING_RESET_CLEAR_GRAPH
from src.models import JsonValue, SourceRecordEnvelope
from src.pipeline import IngestPipeline
from src.pipeline_addresses import ingest_address_record
from src.pipeline_person_merge import merge_person_pair
from src.pipeline_references import ingest_reference_record
from src.pipeline_sales import (
    drain_pending_customer_sales,
    ingest_sales_record,
    propose_vehicle_matches_for_pending_sales,
)
from src.retirement import retire_source_evidence

_URI_ENV = "HYPERP_NEO4J_PERSON_LIST_SUMMARY_TEST_URI"
_USER_ENV = "HYPERP_NEO4J_PERSON_LIST_SUMMARY_TEST_USER"
_PASSWORD_ENV = "HYPERP_NEO4J_PERSON_LIST_SUMMARY_TEST_PASSWORD"
_SERVICE_HOST_ENV = "HYPERP_NEO4J_PERSON_LIST_SUMMARY_TEST_SERVICE_HOST"
_REQUIRED_ENV = "HYPERP_NEO4J_PERSON_LIST_SUMMARY_TEST_REQUIRED"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: Seed sentinels: the database emits a time-safe timestamp for them.
_MONTH_START = "month-start"

#: The sales source the sales fixtures exercise.
_SALES_SOURCE_KEY = "eko_phppos:sales"

#: The source that owns the customer identity record the sales records link to.
_IDENTITY_SOURCE_KEY = "bitrix_chat"

#: The identity record the sales records reference as their customer.
_IDENTITY_SOURCE_RECORD_ID = "customer-identity"

#: The contact details the vehicle heuristic matches the Person on.
_CUSTOMER_EMAIL = "buyer@example.com"
_CUSTOMER_PHONE = "+6580000042"

#: The vehicle the sale's line item carries.
_VEHICLE_SERIAL = "SN-100"
_VEHICLE_PRODUCT_SKU = "EBIKE-SN100"

# Independent copies of the original aggregates. They are written out here on
# purpose: the maintained counter must never be validated against itself.
_CORE_AGGREGATE = """
MATCH (p:Person)
WHERE p.status <> 'merged'
RETURN count(p) AS all_profiles_count,
       sum(CASE WHEN p.is_high_risk = true THEN 1 ELSE 0 END) AS high_risk_count,
       sum(CASE WHEN p.is_high_value = true THEN 1 ELSE 0 END) AS high_value_count,
       sum(CASE WHEN p.preferred_phone IS NULL AND p.preferred_email IS NULL THEN 1 ELSE 0 END)
         AS no_contact_count
"""

_CRM_AGGREGATE = """
MATCH (deal:SourceRecord {record_type: 'crm_deal'})-[deal_link:LINKED_TO]->(p:Person)
WHERE p.status <> 'merged'
  AND coalesce(deal_link.is_active, true) = true
  AND (deal.lifecycle_status = 'active'
    OR (deal.lifecycle_status IS NULL AND deal.is_latest = true))
RETURN count(DISTINCT CASE
         WHEN deal.observed_at >= datetime.truncate('month', datetime()) THEN deal
       END) AS deals_this_month_count,
       count(DISTINCT deal) AS all_deals_count
"""

_COUNTER_READ = """
MATCH (c:PersonListSummaryCounter {key: $key})
RETURN c.ready AS ready,
       c.generation AS generation,
       c.month AS month,
       c.active_person_count AS active_person_count,
       c.high_risk_count AS high_risk_count,
       c.high_value_count AS high_value_count,
       c.no_contact_count AS no_contact_count,
       c.deals_this_month_count AS deals_this_month_count,
       c.all_deals_count AS all_deals_count
"""

_COUNT_FIELDS = (
    "active_person_count",
    "high_risk_count",
    "high_value_count",
    "no_contact_count",
    "deals_this_month_count",
    "all_deals_count",
)


@dataclass(frozen=True)
class _Environment:
    uri: str
    user: str
    password: str
    service_host: str | None


@pytest.fixture
def summary_environment() -> _Environment:
    uri = os.getenv(_URI_ENV)
    required = os.getenv(_REQUIRED_ENV) == "1"
    if uri is None:
        if required:
            pytest.fail(f"{_URI_ENV} is required for the person-list summary Neo4j suite")
        pytest.skip("disposable person-list summary Neo4j database is not configured")
    parsed = urlparse(uri)
    if parsed.scheme not in {"bolt", "bolt+s", "bolt+ssc"}:
        pytest.fail("the person-list summary suite requires a bolt URI")
    if parsed.username or parsed.password or "@" in parsed.netloc:
        pytest.fail("the person-list summary URI must not carry credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        pytest.fail("the person-list summary URI must not select a database")
    service_host = os.getenv(_SERVICE_HOST_ENV)
    allowed_hosts = set(_LOOPBACK_HOSTS)
    if service_host is not None:
        allowed_hosts.add(service_host)
    if parsed.hostname not in allowed_hosts:
        pytest.fail("the person-list summary suite requires a disposable loopback or service host")
    password = os.getenv(_PASSWORD_ENV)
    if not password:
        pytest.fail(f"{_PASSWORD_ENV} is required")
    return _Environment(
        uri=uri,
        user=os.getenv(_USER_ENV, "neo4j"),
        password=password,
        service_host=service_host,
    )


@pytest.fixture
def neo4j_driver(summary_environment: _Environment) -> Iterator[Driver]:
    driver = GraphDatabase.driver(
        summary_environment.uri,
        auth=(summary_environment.user, summary_environment.password),
    )
    safe_to_clear = False
    try:
        for _ in range(15):
            try:
                driver.verify_connectivity()
                break
            except Exception:  # noqa: BLE001 - disposable service readiness retry
                time.sleep(1)
        else:
            pytest.fail("disposable person-list summary Neo4j database did not become ready")
        with driver.session() as session:
            existing = session.run("MATCH (node) RETURN count(node) AS total").single(strict=True)
            if existing["total"] != 0:
                pytest.fail("the person-list summary suite requires an empty database")
            safe_to_clear = True
            session.run(CREATE_PERSON_LIST_SUMMARY_COUNTER_CONSTRAINT).consume()
            session.run(CREATE_SOURCE_RECORD_IDENTITY_LOCK_CONSTRAINT).consume()
            session.run(
                ENSURE_PERSON_LIST_SUMMARY_COUNTER,
                key=PERSON_LIST_SUMMARY_COUNTER_KEY,
            ).consume()
        yield driver
    finally:
        try:
            if safe_to_clear:
                with driver.session() as session:
                    session.run("MATCH (node) DETACH DELETE node").consume()
        finally:
            driver.close()


@pytest.fixture
def client(summary_environment: _Environment) -> Iterator[Neo4jClient]:
    settings = Settings(
        neo4j_uri=summary_environment.uri,
        neo4j_user=summary_environment.user,
        neo4j_password=summary_environment.password,
    )
    graph_client = Neo4jClient(settings)
    try:
        yield graph_client
    finally:
        graph_client.close()


def _seed_source_system(driver: Driver, source_key: str = "bitrix_chat") -> None:
    with driver.session() as session:
        session.run(
            "MERGE (source:SourceSystem {source_key: $source_key})",
            source_key=source_key,
        ).consume()


def _seed_sales_source(driver: Driver) -> None:
    """Seed the Entity and SourceSystem the sales source needs.

    ``CREATE_SOURCE_RECORD`` matches both, so a sales record is only written
    when the owning Entity and the source's SourceSystem exist.
    """
    entity_key = SOURCE_KEY_TO_ENTITY[_SALES_SOURCE_KEY]
    with driver.session() as session:
        session.run(
            "MERGE (entity:Entity {entity_key: $entity_key})",
            entity_key=entity_key,
        ).consume()
        session.run(
            "MERGE (source:SourceSystem {source_key: $source_key})",
            source_key=_SALES_SOURCE_KEY,
        ).consume()


def _seed_identity_record(driver: Driver, *, person_id: str) -> None:
    """Seed the active identity record the sales records reference.

    The identity carries the identity source's ``source_instance_id``, so the
    sales record finds it through ``LINK_SALES_TO_IDENTITY_RECORD``.
    """
    with driver.session() as session:
        session.run(
            """
            MATCH (source:SourceSystem {source_key: $source_system})
            CREATE (identity:SourceRecord {
              source_record_pk: $source_record_pk,
              source_record_id: $source_record_id,
              source_instance_id: 'legacy-default',
              source_record_version: 1,
              record_hash: 'hash-' + $source_record_id,
              record_type: 'identity',
              lifecycle_status: 'active',
              is_latest: true
            })
            CREATE (identity)-[:FROM_SOURCE]->(source)
            WITH identity
            MATCH (person:Person {person_id: $person_id})
            CREATE (identity)-[:LINKED_TO {is_active: true}]->(person)
            """,
            source_system=_IDENTITY_SOURCE_KEY,
            source_record_pk=_IDENTITY_SOURCE_RECORD_ID,
            source_record_id=_IDENTITY_SOURCE_RECORD_ID,
            person_id=person_id,
        ).consume()


def _seed_vehicle_candidate(driver: Driver, *, person_id: str) -> None:
    """Seed the Vehicle the Person bought and the contact identifiers.

    The vehicle heuristic reads the identifier's ``kind`` and ``value``, so the
    identifiers carry that contact shape next to the scoped identifier shape.
    """
    with driver.session() as session:
        session.run(
            """
            CREATE (vehicle:Vehicle {
              vehicle_id: 'vehicle-1',
              serial_number: $serial_number,
              normalized_serial_number: $serial_number,
              product_sku: $product_sku,
              observed_product_skus_s: [$product_sku],
              source_systems: [$sales_source_key]
            })
            WITH vehicle
            MATCH (person:Person {person_id: $person_id})
            CREATE (person)-[:BOUGHT_VEHICLE {is_active: true}]->(vehicle)
            CREATE (email:Identifier {
              identifier_id: 'identifier-email-1',
              identifier_type: 'email',
              identifier_scope: 'default',
              normalized_value: $email,
              kind: 'email',
              value: $email
            })
            CREATE (phone:Identifier {
              identifier_id: 'identifier-phone-1',
              identifier_type: 'mobile',
              identifier_scope: 'default',
              normalized_value: $phone,
              kind: 'mobile',
              value: $phone
            })
            CREATE (person)-[:IDENTIFIED_BY {is_active: true}]->(email)
            CREATE (person)-[:IDENTIFIED_BY {is_active: true}]->(phone)
            """,
            serial_number=_VEHICLE_SERIAL,
            product_sku=_VEHICLE_PRODUCT_SKU,
            sales_source_key=_SALES_SOURCE_KEY,
            person_id=person_id,
            email=_CUSTOMER_EMAIL,
            phone=_CUSTOMER_PHONE,
        ).consume()


def _seed_person(
    driver: Driver,
    *,
    person_id: str,
    status: str | None = "active",
    is_high_risk: bool = False,
    is_high_value: bool = False,
    preferred_phone: str | None = None,
    preferred_email: str | None = None,
) -> None:
    with driver.session() as session:
        session.run(
            """
            CREATE (person:Person {
              person_id: $person_id,
              status: $status,
              is_high_risk: $is_high_risk,
              is_high_value: $is_high_value,
              preferred_phone: $preferred_phone,
              preferred_email: $preferred_email
            })
            """,
            person_id=person_id,
            status=status,
            is_high_risk=is_high_risk,
            is_high_value=is_high_value,
            preferred_phone=preferred_phone,
            preferred_email=preferred_email,
        ).consume()


def _seed_deal(
    driver: Driver,
    *,
    source_record_pk: str | None,
    person_id: str,
    source_system: str = "bitrix_chat",
    record_type: str = "crm_deal",
    lifecycle_status: str | None = "active",
    is_latest: bool | None = True,
    observed_at: str | None = None,
    link_is_active: bool | None = None,
    source_record_id: str | None = None,
    extra_link_to: str | None = None,
) -> None:
    """Seed one source record with a link to a Person.

    ``observed_at`` accepts the ``month-start`` sentinel, which the database
    turns into a time-safe timestamp inside the current month.
    """
    with driver.session() as session:
        session.run(
            """
            MATCH (source:SourceSystem {source_key: $source_system})
            CREATE (deal:SourceRecord {
              source_record_pk: $source_record_pk,
              source_record_id: coalesce($source_record_id, $source_record_pk),
              source_instance_id: 'legacy-default',
              source_record_version: 1,
              record_hash: 'hash-' + toString($source_record_pk),
              record_type: $record_type,
              lifecycle_status: $lifecycle_status,
              is_latest: $is_latest,
              observed_at: CASE
                WHEN $observed_at = 'month-start'
                  THEN datetime.truncate('month', datetime())
                ELSE datetime($observed_at)
              END
            })
            CREATE (deal)-[:FROM_SOURCE]->(source)
            WITH deal
            MATCH (person:Person {person_id: $person_id})
            CREATE (deal)-[link:LINKED_TO]->(person)
            SET link.is_active = $link_is_active
            WITH deal
            OPTIONAL MATCH (other:Person {person_id: $extra_link_to})
            FOREACH (_ IN CASE WHEN other IS NULL THEN [] ELSE [1] END |
              CREATE (deal)-[duplicate:LINKED_TO]->(other)
              SET duplicate.is_active = $link_is_active
            )
            """,
            source_record_pk=source_record_pk,
            source_record_id=source_record_id,
            source_system=source_system,
            record_type=record_type,
            lifecycle_status=lifecycle_status,
            is_latest=is_latest,
            observed_at=observed_at,
            link_is_active=link_is_active,
            person_id=person_id,
            extra_link_to=extra_link_to,
        ).consume()


def _expected_counts(driver: Driver) -> dict[str, int]:
    """Compute the six published values with the independent aggregate copies."""
    with driver.session() as session:
        core = session.run(_CORE_AGGREGATE).single(strict=True)
        crm = session.run(_CRM_AGGREGATE).single(strict=True)
    return {
        "active_person_count": int(core["all_profiles_count"] or 0),
        "high_risk_count": int(core["high_risk_count"] or 0),
        "high_value_count": int(core["high_value_count"] or 0),
        "no_contact_count": int(core["no_contact_count"] or 0),
        "deals_this_month_count": int(crm["deals_this_month_count"] or 0),
        "all_deals_count": int(crm["all_deals_count"] or 0),
    }


def _stored_counts(driver: Driver) -> dict[str, int]:
    """Read six valid non-negative integers from the maintained counter."""
    with driver.session() as session:
        record = session.run(
            _COUNTER_READ,
            key=PERSON_LIST_SUMMARY_COUNTER_KEY,
        ).single(strict=True)
    counts: dict[str, int] = {}
    for field in _COUNT_FIELDS:
        value = record[field]
        assert isinstance(value, int) and not isinstance(value, bool) and value >= 0
        counts[field] = value
    return counts


def _counter_state(driver: Driver) -> dict[str, object]:
    """Read the counter's readiness, generation and month marker."""
    with driver.session() as session:
        record = session.run(
            _COUNTER_READ,
            key=PERSON_LIST_SUMMARY_COUNTER_KEY,
        ).single(strict=True)
    return {
        "ready": record["ready"],
        "generation": record["generation"],
        "month": record["month"],
    }


def _assert_counter_matches_graph(driver: Driver) -> None:
    """The maintained counter must equal the independent aggregates."""
    assert _stored_counts(driver) == _expected_counts(driver)


def _envelope(
    *,
    source_record_id: str,
    record_type: str = "identity",
    source_system: str = "bitrix_chat",
    source_instance_id: str = "legacy-default",
    raw_payload: dict[str, JsonValue] | None = None,
    attributes: dict[str, JsonValue] | None = None,
    observed_at: str | None = None,
    identifiers: list[dict[str, JsonValue]] | None = None,
) -> SourceRecordEnvelope:
    return SourceRecordEnvelope.model_validate(
        {
            "source_system": source_system,
            "source_instance_id": source_instance_id,
            "source_record_id": source_record_id,
            "record_type": record_type,
            "observed_at": observed_at,
            "record_hash": f"hash-{source_record_id}",
            "raw_payload": raw_payload or {},
            "attributes": attributes or {},
            "identifiers": identifiers or [],
            "addresses": [],
        }
    )


def _sales_payload(
    *,
    source_order_id: str,
    identity_source_record_id: str | None = _IDENTITY_SOURCE_RECORD_ID,
    line_items: list[dict[str, JsonValue]] | None = None,
    customer_emails: list[str] | None = None,
    customer_phones: list[str] | None = None,
) -> dict[str, JsonValue]:
    """Build a sales payload that points at the customer identity record.

    The customer link is mandatory: a sales record without one is skipped for
    good and never reaches the drain or the vehicle proposal.
    """
    payload: dict[str, JsonValue] = {
        "order": {"source_order_id": source_order_id},
        "line_items": line_items or [],
    }
    if identity_source_record_id is not None:
        payload["customer_link"] = {
            "identity_source_record_id": identity_source_record_id,
            "source_system_key": _IDENTITY_SOURCE_KEY,
        }
    if customer_emails:
        payload["customer_emails"] = customer_emails
    if customer_phones:
        payload["customer_phones"] = customer_phones
    return payload


def _publish_person_delta(
    tx: ManagedTransaction,
    *,
    person_id: str,
    phone: str,
) -> None:
    """A collector-bearing mutation of one Person, used by the concurrency tests.

    Storing a preferred phone flips the Person's no-contact contribution, so the
    counter has to move with it.
    """
    collector = PersonListSummaryDeltaCollector()
    collector.capture_before(tx, person_ids=[person_id])
    tx.run(
        "MATCH (person:Person {person_id: $person_id}) SET person.preferred_phone = $phone",
        person_id=person_id,
        phone=phone,
    ).consume()
    collector.finalize(tx)


def _publish_deal_delta(
    tx: ManagedTransaction,
    *,
    source_record_pk: str,
    lifecycle_status: str,
) -> None:
    """A collector-bearing mutation of one shared deal, used by the concurrency tests.

    Flipping the deal's lifecycle status moves it in and out of the counted set,
    so the counter has to move with it.
    """
    collector = PersonListSummaryDeltaCollector()
    collector.capture_before(tx, source_record_pks=[source_record_pk])
    tx.run(
        "MATCH (deal:SourceRecord {source_record_pk: $source_record_pk}) "
        "SET deal.lifecycle_status = $lifecycle_status",
        source_record_pk=source_record_pk,
        lifecycle_status=lifecycle_status,
    ).consume()
    collector.finalize(tx)


def _publish_person_delta_while_locked(
    tx: ManagedTransaction,
    *,
    person_id: str,
    phone: str,
    lock_acquired: threading.Event,
    release_lock: threading.Event,
) -> None:
    """Hold an affected Person lock after its real collector snapshot."""
    collector = PersonListSummaryDeltaCollector()
    collector.capture_before(tx, person_ids=[person_id])
    lock_acquired.set()
    if not release_lock.wait(timeout=30):
        raise TimeoutError("timed out waiting to release the Person lock")
    tx.run(
        "MATCH (person:Person {person_id: $person_id}) SET person.preferred_phone = $phone",
        person_id=person_id,
        phone=phone,
    ).consume()
    collector.finalize(tx)


def _publish_deal_delta_while_locked(
    tx: ManagedTransaction,
    *,
    source_record_pk: str,
    lifecycle_status: str,
    lock_acquired: threading.Event,
    release_lock: threading.Event,
) -> None:
    """Hold an affected deal lock after its real collector snapshot."""
    collector = PersonListSummaryDeltaCollector()
    collector.capture_before(tx, source_record_pks=[source_record_pk])
    lock_acquired.set()
    if not release_lock.wait(timeout=30):
        raise TimeoutError("timed out waiting to release the deal lock")
    tx.run(
        "MATCH (deal:SourceRecord {source_record_pk: $source_record_pk}) "
        "SET deal.lifecycle_status = $lifecycle_status",
        source_record_pk=source_record_pk,
        lifecycle_status=lifecycle_status,
    ).consume()
    collector.finalize(tx)


# --- bootstrap and publication -------------------------------------------------


def test_bootstrap_creates_an_unready_counter(neo4j_driver: Driver) -> None:
    state = _counter_state(neo4j_driver)

    assert state["ready"] is False
    assert state["generation"] == 0
    assert state["month"] is None


def test_backfill_publishes_the_independent_aggregates(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    _seed_person(neo4j_driver, person_id="p-1", is_high_risk=True, preferred_phone="+6580000000")
    _seed_person(neo4j_driver, person_id="p-2", status="suppressed")
    _seed_deal(
        neo4j_driver,
        source_record_pk="deal-1",
        person_id="p-1",
        observed_at=_MONTH_START,
    )

    publication = backfill_person_list_summary(client)

    assert publication is not None
    assert publication.activated is True
    assert publication.previous.ready is False
    _assert_counter_matches_graph(neo4j_driver)
    assert _stored_counts(neo4j_driver)["active_person_count"] == 2
    assert _stored_counts(neo4j_driver)["all_deals_count"] == 1


def test_backfill_is_idempotent(neo4j_driver: Driver, client: Neo4jClient) -> None:
    _seed_person(neo4j_driver, person_id="p-1")

    first = backfill_person_list_summary(client)
    second = backfill_person_list_summary(client)

    assert first is not None
    assert second is not None
    assert second.new == first.new
    _assert_counter_matches_graph(neo4j_driver)


def test_nightly_reconcile_leaves_an_unready_counter_unready(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    _seed_person(neo4j_driver, person_id="p-1")

    publication = reconcile_person_list_summary(client)

    assert publication is None
    assert _counter_state(neo4j_driver)["ready"] is False


def test_invalidation_marks_the_counter_unready_and_advances_the_generation(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    backfill_person_list_summary(client)

    invalidation = invalidate_person_list_summary(client)

    assert invalidation.ready is False
    assert invalidation.generation == 1
    assert _counter_state(neo4j_driver)["ready"] is False


# --- exact predicates -----------------------------------------------------------


def test_statuses_and_flags_match_the_independent_aggregate(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    _seed_person(neo4j_driver, person_id="p-active")
    _seed_person(neo4j_driver, person_id="p-risk", is_high_risk=True)
    _seed_person(neo4j_driver, person_id="p-value", is_high_value=True)
    _seed_person(neo4j_driver, person_id="p-suppressed", status="suppressed")
    _seed_person(neo4j_driver, person_id="p-retired", status="retired")
    _seed_person(neo4j_driver, person_id="p-merged", status="merged")
    _seed_person(neo4j_driver, person_id="p-null-status", status=None)

    backfill_person_list_summary(client)

    stored = _stored_counts(neo4j_driver)
    assert stored == _expected_counts(neo4j_driver)
    assert stored["active_person_count"] == 5
    assert stored["high_risk_count"] == 1
    assert stored["high_value_count"] == 1


def test_null_and_empty_contacts_are_distinguished(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    _seed_person(neo4j_driver, person_id="p-null")
    _seed_person(neo4j_driver, person_id="p-empty", preferred_phone="", preferred_email="")

    backfill_person_list_summary(client)

    stored = _stored_counts(neo4j_driver)
    assert stored == _expected_counts(neo4j_driver)
    # Only a null phone and a null email count as "no contact".
    assert stored["no_contact_count"] == 1


def test_legacy_missing_flags_and_deal_fields_capture_as_false(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    _seed_source_system(neo4j_driver)
    with neo4j_driver.session() as session:
        seeded = session.run(
            """
            MATCH (source:SourceSystem {source_key: 'bitrix_chat'})
            CREATE (person:Person {
              person_id: 'legacy-missing-flags', status: 'active'
            })
            CREATE (deal:SourceRecord {
              source_record_pk: 'legacy-missing-deal-fields',
              lifecycle_status: null, observed_at: datetime()
            })
            CREATE (deal)-[:FROM_SOURCE]->(source)
            CREATE (deal)-[:LINKED_TO {is_active: true}]->(person)
            RETURN elementId(deal) AS deal_element_id
            """
        ).single(strict=True)
    deal_element_id = str(seeded["deal_element_id"])
    backfill_person_list_summary(client)

    with neo4j_driver.session() as session:
        person_capture = session.run(
            CAPTURE_PERSON_SUMMARY_CONTRIBUTIONS,
            person_ids=["legacy-missing-flags"],
        ).single(strict=True)
        deal_capture = session.run(
            CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS,
            source_record_pks=["legacy-missing-deal-fields"],
        ).single(strict=True)
        element_capture = session.run(
            CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS_BY_ELEMENT_ID,
            deal_element_ids=[deal_element_id],
        ).single(strict=True)

    assert person_capture["high_risk"] is False
    assert person_capture["high_value"] is False
    assert deal_capture["qualifying"] is False
    assert element_capture["qualifying"] is False

    def _mutate(tx: ManagedTransaction) -> None:
        collector = PersonListSummaryDeltaCollector()
        collector.capture_before(
            tx,
            person_ids=["legacy-missing-flags"],
            source_record_pks=["legacy-missing-deal-fields"],
        )
        collector.capture_before(tx, deal_element_ids=[deal_element_id])
        tx.run(
            "MATCH (person:Person {person_id: $person_id}) SET person.preferred_phone = $phone",
            person_id="legacy-missing-flags",
            phone="+6580000099",
        ).consume()
        application = collector.finalize(tx)
        assert application.ready is True
        assert application.core_applied is True
        assert application.all_deals_applied is True

    client.execute_write(_mutate)

    _assert_counter_matches_graph(neo4j_driver)
    stored = _stored_counts(neo4j_driver)
    assert stored["high_risk_count"] == 0
    assert stored["high_value_count"] == 0
    assert stored["no_contact_count"] == 0
    assert stored["all_deals_count"] == 0


def test_shared_and_duplicate_links_count_one_distinct_deal(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    _seed_person(neo4j_driver, person_id="p-1")
    _seed_person(neo4j_driver, person_id="p-2")
    _seed_deal(
        neo4j_driver,
        source_record_pk="deal-1",
        person_id="p-1",
        observed_at=_MONTH_START,
    )
    # The same deal linked to a second Person and to the first Person twice.
    _seed_deal(
        neo4j_driver,
        source_record_pk="deal-2",
        person_id="p-1",
        observed_at=_MONTH_START,
        extra_link_to="p-2",
    )
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (deal:SourceRecord {source_record_pk: 'deal-1'})
            MATCH (person:Person {person_id: 'p-1'})
            CREATE (deal)-[duplicate:LINKED_TO {is_active: true}]->(person)
            """
        ).consume()

    backfill_person_list_summary(client)

    stored = _stored_counts(neo4j_driver)
    assert stored == _expected_counts(neo4j_driver)
    assert stored["all_deals_count"] == 2
    assert stored["deals_this_month_count"] == 2


def test_legacy_deals_without_a_source_record_pk_are_still_counted(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    _seed_person(neo4j_driver, person_id="p-1")
    _seed_deal(
        neo4j_driver,
        source_record_pk=None,
        person_id="p-1",
        observed_at=_MONTH_START,
    )
    _seed_deal(
        neo4j_driver,
        source_record_pk="",
        person_id="p-1",
        observed_at=_MONTH_START,
    )

    backfill_person_list_summary(client)

    stored = _stored_counts(neo4j_driver)
    assert stored == _expected_counts(neo4j_driver)
    assert stored["all_deals_count"] == 2
    assert stored["deals_this_month_count"] == 2


def test_inactive_links_and_non_deal_records_do_not_count(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    _seed_person(neo4j_driver, person_id="p-1")
    _seed_deal(
        neo4j_driver,
        source_record_pk="deal-inactive",
        person_id="p-1",
        link_is_active=False,
        observed_at=_MONTH_START,
    )
    _seed_deal(
        neo4j_driver,
        source_record_pk="sr-sales",
        person_id="p-1",
        record_type="sales",
        observed_at=_MONTH_START,
    )

    backfill_person_list_summary(client)

    stored = _stored_counts(neo4j_driver)
    assert stored == _expected_counts(neo4j_driver)
    assert stored["all_deals_count"] == 0
    assert stored["deals_this_month_count"] == 0


def test_deal_version_states_select_the_counted_versions(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    _seed_person(neo4j_driver, person_id="p-1")
    _seed_deal(
        neo4j_driver,
        source_record_pk="deal-active",
        person_id="p-1",
        lifecycle_status="active",
        observed_at=_MONTH_START,
    )
    _seed_deal(
        neo4j_driver,
        source_record_pk="deal-legacy",
        person_id="p-1",
        lifecycle_status=None,
        is_latest=True,
        observed_at=_MONTH_START,
    )
    _seed_deal(
        neo4j_driver,
        source_record_pk="deal-pending",
        person_id="p-1",
        lifecycle_status="pending_review",
        observed_at=_MONTH_START,
    )
    _seed_deal(
        neo4j_driver,
        source_record_pk="deal-superseded",
        person_id="p-1",
        lifecycle_status="superseded",
        observed_at=_MONTH_START,
    )

    backfill_person_list_summary(client)

    stored = _stored_counts(neo4j_driver)
    assert stored == _expected_counts(neo4j_driver)
    assert stored["all_deals_count"] == 2


def test_observed_at_null_future_current_and_old_are_classified(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    _seed_person(neo4j_driver, person_id="p-1")
    _seed_deal(
        neo4j_driver,
        source_record_pk="deal-current",
        person_id="p-1",
        observed_at=_MONTH_START,
    )
    _seed_deal(
        neo4j_driver,
        source_record_pk="deal-future",
        person_id="p-1",
        observed_at="2099-01-01",
    )
    _seed_deal(
        neo4j_driver,
        source_record_pk="deal-old",
        person_id="p-1",
        observed_at="2020-01-01",
    )
    _seed_deal(
        neo4j_driver,
        source_record_pk="deal-null",
        person_id="p-1",
        observed_at=None,
    )

    backfill_person_list_summary(client)

    stored = _stored_counts(neo4j_driver)
    assert stored == _expected_counts(neo4j_driver)
    assert stored["all_deals_count"] == 4
    assert stored["deals_this_month_count"] == 2


# --- production writers ---------------------------------------------------------


def test_ingestion_publishes_the_created_person(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    _seed_source_system(neo4j_driver)
    backfill_person_list_summary(client)

    result = IngestPipeline(client).ingest(
        _envelope(
            source_record_id="identity-1",
            identifiers=[
                {
                    "identifier_type": "nric",
                    "normalized_value": "hash-1",
                    "quality_flag": "valid",
                }
            ],
        )
    )

    assert result.person_id is not None
    _assert_counter_matches_graph(neo4j_driver)
    assert _stored_counts(neo4j_driver)["active_person_count"] == 1


def test_ingestion_of_a_second_record_keeps_the_counter_in_step(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    _seed_source_system(neo4j_driver)
    pipeline = IngestPipeline(client)
    backfill_person_list_summary(client)

    pipeline.ingest(
        _envelope(
            source_record_id="identity-1",
            identifiers=[
                {
                    "identifier_type": "nric",
                    "normalized_value": "hash-1",
                    "quality_flag": "valid",
                }
            ],
        )
    )
    _assert_counter_matches_graph(neo4j_driver)

    pipeline.ingest(
        _envelope(
            source_record_id="identity-2",
            identifiers=[
                {
                    "identifier_type": "nric",
                    "normalized_value": "hash-2",
                    "quality_flag": "valid",
                }
            ],
        )
    )

    _assert_counter_matches_graph(neo4j_driver)
    assert _stored_counts(neo4j_driver)["active_person_count"] == 2


def test_duplicate_version_is_a_no_op_for_the_counter(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    _seed_source_system(neo4j_driver)
    pipeline = IngestPipeline(client)
    envelope = _envelope(
        source_record_id="identity-1",
        identifiers=[
            {
                "identifier_type": "nric",
                "normalized_value": "hash-1",
                "quality_flag": "valid",
            }
        ],
    )
    backfill_person_list_summary(client)
    pipeline.ingest(envelope)
    _assert_counter_matches_graph(neo4j_driver)
    before = _stored_counts(neo4j_driver)

    duplicate = pipeline.ingest(envelope)

    assert duplicate.skipped_duplicate is True
    _assert_counter_matches_graph(neo4j_driver)
    assert _stored_counts(neo4j_driver) == before


def test_auto_merge_retires_the_absorbed_person(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    _seed_person(neo4j_driver, person_id="p-absorbed")
    _seed_person(neo4j_driver, person_id="p-survivor")
    _seed_deal(
        neo4j_driver,
        source_record_pk="deal-1",
        person_id="p-absorbed",
        observed_at=_MONTH_START,
    )
    backfill_person_list_summary(client)

    def _merge(tx: ManagedTransaction) -> str:
        collector = PersonListSummaryDeltaCollector()
        merge_event_id = merge_person_pair(
            tx,
            absorbed_id="p-absorbed",
            survivor_id="p-survivor",
            match_decision_id="md-1",
            reason="threshold met",
            collector=collector,
        )
        collector.finalize(tx)
        return merge_event_id

    merge_event_id = client.execute_write(_merge)

    assert merge_event_id != ""
    _assert_counter_matches_graph(neo4j_driver)
    assert _stored_counts(neo4j_driver)["active_person_count"] == 1


def test_cross_type_deal_replaced_by_an_address_record(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    _seed_source_system(neo4j_driver)
    _seed_person(neo4j_driver, person_id="p-1")
    _seed_deal(
        neo4j_driver,
        source_record_pk="deal-1",
        person_id="p-1",
        source_record_id="shared-1",
        observed_at=_MONTH_START,
    )
    backfill_person_list_summary(client)
    assert _stored_counts(neo4j_driver)["all_deals_count"] == 1

    ingest_address_record(
        client,
        _envelope(
            source_record_id="shared-1",
            record_type="rental_flat",
            attributes={
                "postal_code": "123456",
                "country_code": "SG",
                "block_no": "1",
                "street_name": "Main Street",
                "town_name": "Town",
            },
        ),
    )

    _assert_counter_matches_graph(neo4j_driver)
    assert _stored_counts(neo4j_driver)["all_deals_count"] == 0


def test_cross_type_deal_replaced_by_a_company_reference(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    _seed_source_system(neo4j_driver)
    _seed_person(neo4j_driver, person_id="p-1")
    _seed_deal(
        neo4j_driver,
        source_record_pk="deal-1",
        person_id="p-1",
        source_record_id="shared-2",
        observed_at=_MONTH_START,
    )
    backfill_person_list_summary(client)

    ingest_reference_record(
        client,
        _envelope(
            source_record_id="shared-2",
            record_type="crm_company",
            raw_payload={
                "company_reference": {"type": "crm_company_id", "value": "303"},
                "reference_metadata": {
                    "identity_policy_version": "crm_company_reference_v1",
                    "source_instance_id": "legacy-default",
                    "crm_company_id": "303",
                    "person_matching_prohibited": True,
                },
            },
        ),
    )

    _assert_counter_matches_graph(neo4j_driver)
    assert _stored_counts(neo4j_driver)["all_deals_count"] == 0


def test_cross_type_deal_replaced_by_a_sales_record(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    _seed_source_system(neo4j_driver)
    _seed_sales_source(neo4j_driver)
    _seed_person(neo4j_driver, person_id="p-1")
    _seed_identity_record(neo4j_driver, person_id="p-1")
    _seed_deal(
        neo4j_driver,
        source_record_pk="deal-1",
        person_id="p-1",
        source_system=_SALES_SOURCE_KEY,
        source_record_id="shared-3",
        observed_at=_MONTH_START,
    )
    backfill_person_list_summary(client)
    assert _stored_counts(neo4j_driver)["all_deals_count"] == 1

    ingest_sales_record(
        client,
        _envelope(
            source_record_id="shared-3",
            record_type="sales",
            source_system=_SALES_SOURCE_KEY,
            raw_payload=_sales_payload(source_order_id="so-1"),
        ),
        ingest_run_id="run-1",
    )

    # The sales record replaces the CRM deal of the same source identity.
    _assert_counter_matches_graph(neo4j_driver)
    assert _stored_counts(neo4j_driver)["all_deals_count"] == 0


def test_immediate_sales_ingestion_keeps_the_counter_in_step(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    _seed_source_system(neo4j_driver)
    _seed_sales_source(neo4j_driver)
    _seed_person(neo4j_driver, person_id="p-1")
    _seed_identity_record(neo4j_driver, person_id="p-1")
    _seed_deal(
        neo4j_driver,
        source_record_pk="deal-1",
        person_id="p-1",
        source_system=_SALES_SOURCE_KEY,
        source_record_id="shared-4",
        observed_at=_MONTH_START,
    )
    backfill_person_list_summary(client)
    assert _stored_counts(neo4j_driver)["all_deals_count"] == 1

    ingest_sales_record(
        client,
        _envelope(
            source_record_id="shared-4",
            record_type="sales",
            source_system=_SALES_SOURCE_KEY,
            raw_payload=_sales_payload(source_order_id="so-2"),
        ),
        ingest_run_id="run-2",
    )

    # The customer resolves during the ingestion, so the sale is activated
    # right away and the replaced deal stops counting immediately.
    _assert_counter_matches_graph(neo4j_driver)
    assert _stored_counts(neo4j_driver)["all_deals_count"] == 0


def test_delayed_sales_drain_keeps_the_counter_in_step(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    _seed_source_system(neo4j_driver)
    _seed_sales_source(neo4j_driver)
    _seed_person(neo4j_driver, person_id="p-1")
    _seed_deal(
        neo4j_driver,
        source_record_pk="deal-1",
        person_id="p-1",
        source_system=_SALES_SOURCE_KEY,
        source_record_id="shared-5",
        observed_at=_MONTH_START,
    )
    backfill_person_list_summary(client)
    assert _stored_counts(neo4j_driver)["all_deals_count"] == 1

    # The identity record does not exist yet, so the sale stays pending and
    # the replaced deal keeps counting.
    ingest_sales_record(
        client,
        _envelope(
            source_record_id="shared-5",
            record_type="sales",
            source_system=_SALES_SOURCE_KEY,
            raw_payload=_sales_payload(source_order_id="so-3"),
        ),
        ingest_run_id="run-3",
    )
    _assert_counter_matches_graph(neo4j_driver)
    assert _stored_counts(neo4j_driver)["all_deals_count"] == 1

    _seed_identity_record(neo4j_driver, person_id="p-1")
    drain_pending_customer_sales(client)

    # The drain activates the sale and supersedes the replaced deal.
    _assert_counter_matches_graph(neo4j_driver)
    assert _stored_counts(neo4j_driver)["all_deals_count"] == 0


def test_vehicle_proposal_keeps_the_counter_in_step(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    _seed_source_system(neo4j_driver)
    _seed_sales_source(neo4j_driver)
    _seed_person(neo4j_driver, person_id="p-1")
    _seed_vehicle_candidate(neo4j_driver, person_id="p-1")
    _seed_deal(
        neo4j_driver,
        source_record_pk="deal-1",
        person_id="p-1",
        source_system=_SALES_SOURCE_KEY,
        source_record_id="shared-6",
        observed_at=_MONTH_START,
    )
    backfill_person_list_summary(client)
    assert _stored_counts(neo4j_driver)["all_deals_count"] == 1

    # The identity record does not exist, so the sale stays pending and the
    # vehicle heuristic is the only way it can be resolved.
    ingest_sales_record(
        client,
        _envelope(
            source_record_id="shared-6",
            record_type="sales",
            source_system=_SALES_SOURCE_KEY,
            raw_payload=_sales_payload(
                source_order_id="so-4",
                line_items=[
                    {
                        "source_line_item_id": "line-1",
                        "quantity": 1,
                        "unit_price": 1500.0,
                        "product": {
                            "source_product_id": "product-1",
                            "sku": _VEHICLE_PRODUCT_SKU,
                            "name": "E-Bike",
                            "category": "E-Bikes",
                        },
                        "metadata": {"serial_number": _VEHICLE_SERIAL},
                    }
                ],
                customer_emails=[_CUSTOMER_EMAIL],
                customer_phones=[_CUSTOMER_PHONE],
            ),
        ),
        ingest_run_id="run-4",
    )
    _assert_counter_matches_graph(neo4j_driver)
    assert _stored_counts(neo4j_driver)["all_deals_count"] == 1

    propose_vehicle_matches_for_pending_sales(client)

    # The proposal activates the sale and supersedes the replaced deal.
    _assert_counter_matches_graph(neo4j_driver)
    assert _stored_counts(neo4j_driver)["all_deals_count"] == 0


def test_retirement_invalidates_and_stops_counting_the_retired_deal(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    _seed_source_system(neo4j_driver)
    _seed_person(neo4j_driver, person_id="p-1")
    _seed_deal(
        neo4j_driver,
        source_record_pk="deal-1",
        person_id="p-1",
        source_record_id="shared-4",
        observed_at=_MONTH_START,
    )
    backfill_person_list_summary(client)
    assert _stored_counts(neo4j_driver)["all_deals_count"] == 1

    retired = retire_source_evidence(
        client,
        "bitrix_chat",
        "shared-4",
        "2026-11-02T00:00:00+00:00",
        "2026-11-02T00:00:00+00:00",
    )

    assert retired == 1
    # The retirement publishes its own delta: no backfill may paper over it.
    _assert_counter_matches_graph(neo4j_driver)
    assert _stored_counts(neo4j_driver)["all_deals_count"] == 0


def test_reconcile_source_record_lifecycle_keeps_the_counter_in_step(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    _seed_source_system(neo4j_driver)
    _seed_person(neo4j_driver, person_id="p-1")
    _seed_deal(
        neo4j_driver,
        source_record_pk="deal-1",
        person_id="p-1",
        lifecycle_status=None,
        is_latest=True,
        observed_at=_MONTH_START,
    )
    backfill_person_list_summary(client)

    reconcile_source_record_lifecycle(client)

    _assert_counter_matches_graph(neo4j_driver)


def test_lifecycle_migration_batches_keep_the_counter_in_step(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    _seed_source_system(neo4j_driver)
    _seed_person(neo4j_driver, person_id="p-1")
    _seed_deal(
        neo4j_driver,
        source_record_pk="deal-1",
        person_id="p-1",
        lifecycle_status=None,
        is_latest=True,
        observed_at=_MONTH_START,
    )
    backfill_person_list_summary(client)

    migrate_source_record_lifecycle(client)

    _assert_counter_matches_graph(neo4j_driver)


@pytest.mark.parametrize("operation", ["reconcile", "migrate"])
def test_legacy_crm_deal_without_is_latest_migrates_without_summary_drift(
    neo4j_driver: Driver,
    client: Neo4jClient,
    operation: str,
) -> None:
    _seed_source_system(neo4j_driver)
    _seed_person(neo4j_driver, person_id="legacy-lifecycle-person")
    _seed_deal(
        neo4j_driver,
        source_record_pk="legacy-lifecycle-deal",
        person_id="legacy-lifecycle-person",
        lifecycle_status=None,
        is_latest=None,
        observed_at=_MONTH_START,
    )
    backfill_person_list_summary(client)
    assert _stored_counts(neo4j_driver)["all_deals_count"] == 0

    if operation == "reconcile":
        reconcile_source_record_lifecycle(client)
    else:
        migrate_source_record_lifecycle(client)

    _assert_counter_matches_graph(neo4j_driver)
    assert _stored_counts(neo4j_driver)["all_deals_count"] == 1


def test_reset_preserves_the_counter_node_and_its_generation(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    backfill_person_list_summary(client)
    invalidate_person_list_summary(client)
    generation = _counter_state(neo4j_driver)["generation"]

    with neo4j_driver.session() as session:
        session.run(STAGING_RESET_CLEAR_GRAPH, batch_size=1000).consume()

    state = _counter_state(neo4j_driver)
    assert state["generation"] == generation
    assert state["ready"] is False


# --- concurrency ----------------------------------------------------------------


def test_two_writers_on_the_same_person_keep_the_counter_consistent(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    _seed_person(neo4j_driver, person_id="p-1")
    backfill_person_list_summary(client)
    errors: list[BaseException] = []
    first_locked = threading.Event()
    release_first = threading.Event()
    second_attempting = threading.Event()
    second_completed = threading.Event()
    finished = [threading.Event(), threading.Event()]
    start_second = threading.Barrier(2)

    def _first_writer() -> None:
        try:
            client.execute_write(
                _publish_person_delta_while_locked,
                person_id="p-1",
                phone="+6580000001",
                lock_acquired=first_locked,
                release_lock=release_first,
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced in the assertion
            errors.append(exc)
        finally:
            finished[0].set()

    def _second_writer() -> None:
        try:
            start_second.wait(timeout=30)

            def _work(tx: ManagedTransaction) -> None:
                second_attempting.set()
                _publish_person_delta(tx, person_id="p-1", phone="+6580000002")
                second_completed.set()

            client.execute_write(_work)
        except BaseException as exc:  # noqa: BLE001 - surfaced in the assertion
            errors.append(exc)
        finally:
            finished[1].set()

    first = threading.Thread(target=_first_writer)
    second = threading.Thread(target=_second_writer)
    first.start()
    try:
        assert first_locked.wait(timeout=30)
        second.start()
        start_second.wait(timeout=30)
        assert second_attempting.wait(timeout=30)
        assert not second_completed.wait(timeout=0.2)
    finally:
        release_first.set()
        first.join(timeout=30)
        if second.ident is not None:
            second.join(timeout=30)

    assert errors == []
    assert all(event.is_set() for event in finished)
    assert not first.is_alive()
    assert not second.is_alive()
    _assert_counter_matches_graph(neo4j_driver)


def test_two_writers_on_a_shared_deal_keep_the_counter_consistent(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    _seed_person(neo4j_driver, person_id="p-1")
    _seed_deal(
        neo4j_driver,
        source_record_pk="deal-1",
        person_id="p-1",
        observed_at=_MONTH_START,
    )
    backfill_person_list_summary(client)
    errors: list[BaseException] = []
    first_locked = threading.Event()
    release_first = threading.Event()
    second_attempting = threading.Event()
    second_completed = threading.Event()
    finished = [threading.Event(), threading.Event()]
    start_second = threading.Barrier(2)

    def _first_writer() -> None:
        try:
            client.execute_write(
                _publish_deal_delta_while_locked,
                source_record_pk="deal-1",
                lifecycle_status="superseded",
                lock_acquired=first_locked,
                release_lock=release_first,
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced in the assertion
            errors.append(exc)
        finally:
            finished[0].set()

    def _second_writer() -> None:
        try:
            start_second.wait(timeout=30)

            def _work(tx: ManagedTransaction) -> None:
                second_attempting.set()
                _publish_deal_delta(
                    tx,
                    source_record_pk="deal-1",
                    lifecycle_status="active",
                )
                second_completed.set()

            client.execute_write(_work)
        except BaseException as exc:  # noqa: BLE001 - surfaced in the assertion
            errors.append(exc)
        finally:
            finished[1].set()

    first = threading.Thread(target=_first_writer)
    second = threading.Thread(target=_second_writer)
    first.start()
    try:
        assert first_locked.wait(timeout=30)
        second.start()
        start_second.wait(timeout=30)
        assert second_attempting.wait(timeout=30)
        assert not second_completed.wait(timeout=0.2)
    finally:
        release_first.set()
        first.join(timeout=30)
        if second.ident is not None:
            second.join(timeout=30)

    assert errors == []
    assert all(event.is_set() for event in finished)
    assert not first.is_alive()
    assert not second.is_alive()
    _assert_counter_matches_graph(neo4j_driver)
    assert _stored_counts(neo4j_driver)["all_deals_count"] == 1


def test_a_scan_racing_a_publication_leaves_the_counter_consistent(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    """A backfill overlaps a committed delta while holding the counter lock."""
    _seed_person(neo4j_driver, person_id="p-1")
    backfill_person_list_summary(client)
    errors: list[BaseException] = []
    counter_locked = threading.Event()
    release_writer = threading.Event()
    publisher_attempting = threading.Event()
    publisher_finished = threading.Event()
    writer_finished = threading.Event()
    publisher_barrier = threading.Barrier(2)

    def _write() -> None:
        try:

            def _work(tx: ManagedTransaction) -> None:
                _publish_person_delta(tx, person_id="p-1", phone="+6580000001")
                counter_locked.set()
                if not release_writer.wait(timeout=30):
                    raise TimeoutError("timed out waiting to release the counter lock")

            client.execute_write(_work)
        except BaseException as exc:  # noqa: BLE001 - surfaced in the assertion
            errors.append(exc)
        finally:
            writer_finished.set()

    def _publish() -> None:
        try:
            publisher_barrier.wait(timeout=30)
            publisher_attempting.set()
            backfill_person_list_summary(client)
        except BaseException as exc:  # noqa: BLE001 - surfaced in the assertion
            errors.append(exc)
        finally:
            publisher_finished.set()

    writer = threading.Thread(target=_write)
    publisher: threading.Thread | None = None
    writer.start()
    try:
        assert counter_locked.wait(timeout=30)
        publisher = threading.Thread(target=_publish)
        publisher.start()
        publisher_barrier.wait(timeout=30)
        assert publisher_attempting.wait(timeout=30)
        assert not publisher_finished.wait(timeout=0.2)
    finally:
        release_writer.set()
        writer.join(timeout=30)
        if publisher is not None:
            publisher.join(timeout=60)

    assert errors == []
    assert writer_finished.is_set()
    assert publisher_finished.is_set()
    assert not writer.is_alive()
    assert publisher is not None
    assert not publisher.is_alive()
    _assert_counter_matches_graph(neo4j_driver)
    stored = _stored_counts(neo4j_driver)
    assert stored["active_person_count"] == 1
    assert stored["no_contact_count"] == 0


# --- informational benchmark ----------------------------------------------------


def _percentile(timings: list[float], fraction: float) -> float:
    """Return the requested percentile of the measured samples."""
    ordered = sorted(timings)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


def _bench(
    driver: Driver,
    query: str,
    params: dict[str, JsonValue],
    runs: int = 12,
    warmups: int = 3,
) -> list[float]:
    """Measure a query in seconds, discarding the warm-up runs."""
    timings: list[float] = []
    with driver.session() as session:
        for _ in range(warmups):
            session.run(query, **params).consume()
        for _ in range(runs):
            started = time.perf_counter()
            session.run(query, **params).consume()
            timings.append(time.perf_counter() - started)
    return timings


def _summarise(label: str, timings: list[float]) -> str:
    return (
        f"{label} p50={statistics.median(timings):.6f}s "
        f"p95={_percentile(timings, 0.95):.6f}s samples={len(timings)}"
    )


def test_synthetic_benchmark_reports_legacy_scan_against_the_counter(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    """Compare the legacy aggregates with the maintained counter read.

    The numbers are informational: three warm-up runs are discarded and twelve
    runs are measured. Nothing here asserts a timing, and the result says
    nothing about any deployed environment.
    """
    persons = 400
    deals = 200
    _seed_source_system(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            """
            UNWIND range(1, $persons) AS index
            CREATE (person:Person {
              person_id: 'bench-p-' + toString(index),
              status: 'active',
              is_high_risk: index % 20 = 0,
              is_high_value: index % 50 = 0
            })
            """,
            persons=persons,
        ).consume()
        session.run(
            """
            UNWIND range(1, $deals) AS index
            MATCH (source:SourceSystem {source_key: 'bitrix_chat'})
            CREATE (deal:SourceRecord {
              source_record_pk: 'bench-deal-' + toString(index),
              source_record_id: 'bench-deal-' + toString(index),
              source_instance_id: 'legacy-default',
              source_record_version: 1,
              record_hash: 'hash-bench-' + toString(index),
              record_type: 'crm_deal',
              lifecycle_status: 'active',
              is_latest: true,
              observed_at: datetime()
            })
            CREATE (deal)-[:FROM_SOURCE]->(source)
            WITH deal, index
            MATCH (person:Person {person_id: 'bench-p-' + toString(index % $persons + 1)})
            CREATE (deal)-[:LINKED_TO {is_active: true}]->(person)
            """,
            deals=deals,
            persons=persons,
        ).consume()

    backfill_person_list_summary(client)
    expected = _expected_counts(neo4j_driver)
    assert expected["active_person_count"] == persons
    assert expected["all_deals_count"] == deals

    legacy = _bench(neo4j_driver, _CORE_AGGREGATE, {})
    crm_legacy = _bench(neo4j_driver, _CRM_AGGREGATE, {})
    counter = _bench(
        neo4j_driver,
        _COUNTER_READ,
        {"key": PERSON_LIST_SUMMARY_COUNTER_KEY},
    )

    print(
        "person-list summary benchmark (informational): "
        f"{_summarise('core_aggregate', legacy)} | "
        f"{_summarise('crm_aggregate', crm_legacy)} | "
        f"{_summarise('counter_read', counter)} | "
        f"persons={persons} deals={deals}"
    )
    _assert_counter_matches_graph(neo4j_driver)


def test_synthetic_benchmark_reports_a_collector_bearing_write(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    """Measure a writer that captures, mutates and publishes the counter."""
    _seed_person(neo4j_driver, person_id="bench-p-1")
    backfill_person_list_summary(client)

    def _write(index: int) -> None:
        client.execute_write(
            _publish_person_delta,
            person_id="bench-p-1",
            phone=f"+65800000{index:02d}",
        )

    for index in range(3):
        _write(index)
    timings: list[float] = []
    for index in range(12):
        started = time.perf_counter()
        _write(index + 100)
        timings.append(time.perf_counter() - started)

    print(
        "person-list summary write benchmark (informational): "
        f"{_summarise('collector_write', timings)}"
    )
    _assert_counter_matches_graph(neo4j_driver)


def test_two_writer_contention_reports_the_lock_wait(
    neo4j_driver: Driver,
    client: Neo4jClient,
) -> None:
    """Report how long a second writer waits for the first one's lock.

    The first writer holds the Person's lock inside an open transaction while
    the second writer runs in its own thread and blocks on the same node. The
    measured wait is informational only: no timing is asserted.
    """
    _seed_person(neo4j_driver, person_id="p-1")
    backfill_person_list_summary(client)
    first_write_locked = threading.Event()
    release_first_write = threading.Event()
    errors: list[BaseException] = []
    waits: list[float] = []

    def _slow_write() -> None:
        def _work(tx: ManagedTransaction) -> None:
            tx.run(
                "MATCH (person:Person {person_id: 'p-1'}) SET person.profile_completeness_score = 1"
            ).consume()
            first_write_locked.set()
            release_first_write.wait(timeout=30)

        try:
            client.execute_write(_work)
        except BaseException as exc:  # noqa: BLE001 - surfaced in the assertion
            errors.append(exc)

    def _waiting_write() -> None:
        started = time.perf_counter()
        try:
            client.execute_write(
                _publish_person_delta,
                person_id="p-1",
                phone="+6580000002",
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced in the assertion
            errors.append(exc)
        finally:
            waits.append(time.perf_counter() - started)

    first_writer = threading.Thread(target=_slow_write)
    first_writer.start()
    assert first_write_locked.wait(timeout=30)

    second_writer = threading.Thread(target=_waiting_write)
    second_writer.start()
    time.sleep(0.2)
    release_first_write.set()

    first_writer.join(timeout=30)
    second_writer.join(timeout=30)

    assert errors == []
    assert len(waits) == 1
    print(
        "person-list summary contention (informational): the second writer "
        f"waited {waits[0]:.6f}s for the first writer's Person lock"
    )
    _assert_counter_matches_graph(neo4j_driver)


def test_query_plans_use_the_keyed_index_and_anchor_on_element_ids(
    neo4j_driver: Driver,
) -> None:
    """Check the keyed read and the element-id capture resolve through indexes."""
    with neo4j_driver.session() as session:
        keyed_summary = session.run(
            "EXPLAIN " + _COUNTER_READ,
            key=PERSON_LIST_SUMMARY_COUNTER_KEY,
        ).consume()
        keyed_plan = str(keyed_summary.plan)
        element_summary = session.run(
            "EXPLAIN " + CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS_BY_ELEMENT_ID,
            deal_element_ids=["4:abc:1"],
        ).consume()
        element_plan = str(element_summary.plan)

    assert "PersonListSummaryCounter" in keyed_plan
    assert "IndexSeek" in keyed_plan
    assert "ElementId" in element_plan
    print(
        "person-list summary plans (informational): "
        f"keyed={keyed_plan!r} element_id={element_plan!r}"
    )
