# ruff: noqa: E501 -- Cypher fixture literals retain executable graph shape.
"""Disposable real-Neo4j execution coverage for #302 fenced source-fact pages."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from typing import cast
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase
from neo4j.exceptions import ServiceUnavailable
from src.graph.client import Neo4jClient
from src.graph.standalone_crm_source_fact_repository import StandaloneCrmSourceFactRepository
from src.standalone_crm_source_fact_models import build_source_fact_commit
from tests.standalone_crm_source_fact_neo4j_support import (
    DriverClient,
    SentinelAdapter,
    assert_raw_authority_conflict,
    assert_raw_claim_finalize_replay,
    repository_request,
    reset_disposable_data,
    retire_current_authority,
    seed_repository_case,
    source_fact_counts,
)


@pytest.fixture
def neo4j_driver() -> Iterator[Driver]:
    uri = os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_URI")
    password = os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_PASSWORD")
    if uri is None or password is None:
        pytest.skip("disposable standalone CRM Lane A Neo4j database is not configured")
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    if os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_SERVICE_HOST") == "neo4j":
        allowed_hosts.add("neo4j")
    if urlparse(uri).hostname not in allowed_hosts:
        pytest.fail("#302 tests require an explicitly disposable Neo4j host")
    driver = GraphDatabase.driver(uri, auth=("neo4j", password))
    try:
        for _ in range(15):
            try:
                driver.verify_connectivity()
                break
            except ServiceUnavailable:
                time.sleep(1)
        else:
            pytest.fail("disposable Neo4j did not become ready")
        reset_disposable_data(driver)
        yield driver
    finally:
        reset_disposable_data(driver)
        driver.close()


def assert_repository_success_replay_conflict(driver: Driver) -> None:
    request = seed_repository_case(driver)
    adapter = SentinelAdapter()
    repository = StandaloneCrmSourceFactRepository(
        cast(Neo4jClient, DriverClient(driver)), adapter=adapter
    )
    assert repository.commit_unit(request).decision == "committed"
    assert source_fact_counts(driver) == {"records": 1, "receipts": 1, "cursor": 6, "processed": 1}
    retire_current_authority(driver)
    before_replay = source_fact_counts(driver)
    assert repository.commit_unit(request).decision == "replayed"
    assert source_fact_counts(driver) == before_replay
    assert adapter.plan_calls == 1 and adapter.persist_calls == 1

    changed_budget = replace(
        request.envelope.budget_authorization,
        authorization_digest="sha256:" + "d" * 64,
    )
    conflict_page = replace(
        request.mutation.page,
        envelope=replace(request.envelope, budget_authorization=changed_budget),
    )
    conflict = build_source_fact_commit(
        replace(request.mutation, page=conflict_page), skipped_rows=0
    )
    assert repository.commit_unit(conflict).decision == "conflict"
    assert source_fact_counts(driver) == {"records": 1, "receipts": 1, "cursor": 6, "processed": 1}


def assert_production_adapter_persists_real_lifecycle_graph(driver: Driver) -> None:
    request = seed_repository_case(driver)
    repository = StandaloneCrmSourceFactRepository(cast(Neo4jClient, DriverClient(driver)))

    assert repository.commit_unit(request).decision == "committed"
    with driver.session() as session:
        row = session.run(
            """
            MATCH (record:SourceRecord {
              source_instance_id: 'portal-a', source_record_id: 'bitrix-crm-lead-6',
              source_record_version: '1'
            })-[:FROM_SOURCE]->(:SourceSystem {source_key: 'bitrix_chat'})
            OPTIONAL MATCH (:Person)-[fact:HAS_FACT {source_record_pk: record.source_record_pk}]->(record)
            RETURN count(DISTINCT record) AS records, count(DISTINCT fact) AS facts
            """
        ).single(strict=True)
    assert dict(row) == {"records": 1, "facts": 1}
    assert source_fact_counts(driver) == {"records": 1, "receipts": 1, "cursor": 6, "processed": 1}


def assert_independent_receipt_duplicate_skips_real_domain_writes(driver: Driver) -> None:
    first = seed_repository_case(driver)
    second = seed_repository_case(
        driver,
        repository_request(census_id="census-b", call_intent_id="call-b"),
    )
    repository = StandaloneCrmSourceFactRepository(cast(Neo4jClient, DriverClient(driver)))

    assert repository.commit_unit(first).decision == "committed"
    duplicate = repository.commit_unit(second)
    assert (duplicate.decision, duplicate.processed_rows, duplicate.skipped_rows) == (
        "committed",
        1,
        1,
    )
    with driver.session() as session:
        row = session.run(
            """
            MATCH (record:SourceRecord {
              source_instance_id: 'portal-a', source_record_id: 'bitrix-crm-lead-6'
            })
            MATCH (checkpoint:StandaloneCrmCensusCheckpoint {census_id: 'census-b', stream_kind: 'lead'})
            RETURN count(record) AS source_versions, checkpoint.processed_rows AS processed,
              checkpoint.skipped_rows AS skipped
            """
        ).single(strict=True)
    assert dict(row) == {"source_versions": 1, "processed": 1, "skipped": 1}


def assert_overlapping_crm_ids_remain_source_instance_isolated(driver: Driver) -> None:
    first = seed_repository_case(driver)
    second = seed_repository_case(
        driver,
        repository_request(
            census_id="census-b",
            source_instance_id="portal-b",
            control_instance_id="control-b",
            call_intent_id="call-b",
        ),
    )
    repository = StandaloneCrmSourceFactRepository(cast(Neo4jClient, DriverClient(driver)))

    assert repository.commit_unit(first).decision == "committed"
    assert repository.commit_unit(second).decision == "committed"
    with driver.session() as session:
        row = session.run(
            """
            MATCH (record:SourceRecord {source_record_id: 'bitrix-crm-lead-6'})
            WHERE record.source_instance_id IN ['portal-a', 'portal-b']
            MATCH (person:Person)-[fact:HAS_FACT {source_record_pk: record.source_record_pk}]->(record)
            MATCH (person)-[:IDENTIFIED_BY {source_record_pk: record.source_record_pk}]->
              (identifier:Identifier {identifier_type: 'crm_lead_id', normalized_value: '6'})
            RETURN count(DISTINCT record) AS source_records,
              count(DISTINCT record.source_instance_id) AS source_instances,
              count(DISTINCT record.source_version_key) AS lifecycle_keys,
              count(DISTINCT person) AS persons,
              count(DISTINCT fact) AS matching_fact_relationships,
              count(DISTINCT identifier.identifier_id) AS identifier_identities,
              collect(DISTINCT identifier.identifier_scope) AS identifier_scopes,
              collect(DISTINCT identifier.source_instance_id) AS identifier_source_instances
            """
        ).single(strict=True)
    values = dict(row)
    assert {
        "source_records": values["source_records"],
        "source_instances": values["source_instances"],
        "lifecycle_keys": values["lifecycle_keys"],
        "persons": values["persons"],
        "matching_fact_relationships": values["matching_fact_relationships"],
        "identifier_identities": values["identifier_identities"],
    } == {
        "source_records": 2,
        "source_instances": 2,
        "lifecycle_keys": 2,
        "persons": 2,
        "matching_fact_relationships": 2,
        "identifier_identities": 2,
    }
    assert set(values["identifier_scopes"]) == {"portal-a", "portal-b"}
    assert set(values["identifier_source_instances"]) == {"portal-a", "portal-b"}


def assert_concurrent_page_cas_commits_once_and_replays_once(driver: Driver) -> None:
    request = seed_repository_case(driver)
    repository = StandaloneCrmSourceFactRepository(cast(Neo4jClient, DriverClient(driver)))
    barrier = Barrier(2)

    def commit_once() -> str:
        barrier.wait()
        return repository.commit_unit(request).decision

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = tuple(executor.submit(commit_once) for _ in range(2))
        results = tuple(future.result() for future in futures)
    assert sorted(results) == ["committed", "replayed"]
    assert source_fact_counts(driver) == {"records": 1, "receipts": 1, "cursor": 6, "processed": 1}


def assert_production_adapter_failpoint_rolls_back_domain_graph(driver: Driver) -> None:
    request = seed_repository_case(driver)

    def fail(name: str) -> None:
        if name == "after_domain_writes":
            raise RuntimeError("production-adapter rollback")

    repository = StandaloneCrmSourceFactRepository(
        cast(Neo4jClient, DriverClient(driver)), failpoint=fail
    )
    with pytest.raises(RuntimeError, match="production-adapter rollback"):
        repository.commit_unit(request)
    assert source_fact_counts(driver) == {"records": 0, "receipts": 0, "cursor": 5, "processed": 0}


def assert_repository_failpoint_rollback(driver: Driver) -> None:
    request = seed_repository_case(driver)
    adapter = SentinelAdapter()

    def fail(name: str) -> None:
        if name == "after_domain_writes":
            raise RuntimeError("rollback")

    repository = StandaloneCrmSourceFactRepository(
        cast(Neo4jClient, DriverClient(driver)), adapter=adapter, failpoint=fail
    )
    with pytest.raises(RuntimeError, match="rollback"):
        repository.commit_unit(request)
    assert source_fact_counts(driver) == {"records": 0, "receipts": 0, "cursor": 5, "processed": 0}


Scenario = Callable[[Driver], None]


def _run_isolated(driver: Driver, scenario: Scenario) -> None:
    reset_disposable_data(driver)
    try:
        scenario(driver)
    finally:
        reset_disposable_data(driver)


_SCENARIOS: tuple[Scenario, ...] = (
    assert_raw_claim_finalize_replay,
    assert_raw_authority_conflict,
    assert_repository_success_replay_conflict,
    assert_production_adapter_persists_real_lifecycle_graph,
    assert_independent_receipt_duplicate_skips_real_domain_writes,
    assert_overlapping_crm_ids_remain_source_instance_isolated,
    assert_concurrent_page_cas_commits_once_and_replays_once,
    assert_production_adapter_failpoint_rolls_back_domain_graph,
    assert_repository_failpoint_rollback,
)


@pytest.mark.parametrize("scenario", _SCENARIOS)
def test_source_fact_real_neo4j_scenario(neo4j_driver: Driver, scenario: Scenario) -> None:
    _run_isolated(neo4j_driver, scenario)


def run_source_fact_neo4j_cases(driver: Driver) -> None:
    """Stable runner imported by the existing CI-invoked Lane A hook."""
    for scenario in _SCENARIOS:
        _run_isolated(driver, scenario)
