"""Real-Neo4j integration baseline for persist-first lifecycle and concurrency."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase
from pytest import MonkeyPatch
from src.graph.bootstrap import bootstrap_entities_and_sources
from src.graph.client import Neo4jClient
from src.graph.schema_init import apply_deferred_identifier_scope_constraints, apply_schema
from src.main import _create_ingest_run
from src.models import IngestResult, RecordType, SourceRecordEnvelope
from src.pipeline import IngestPipeline

_ENV_PREFIX = "HYPERP_NEO4J_CONTROL_MIGRATION_TEST"


def _configuration() -> tuple[str, str, str, str] | None:
    uri = os.getenv(f"{_ENV_PREFIX}_URI")
    user = os.getenv(f"{_ENV_PREFIX}_USER")
    password = os.getenv(f"{_ENV_PREFIX}_PASSWORD")
    service_host = os.getenv(f"{_ENV_PREFIX}_SERVICE_HOST")
    values = (uri, user, password, service_host)
    if all(value is None for value in values):
        return None
    if any(value is None or not value.strip() for value in values):
        pytest.fail("persist-first test requires complete disposable Neo4j configuration")
    assert uri is not None
    assert user is not None
    assert password is not None
    assert service_host is not None
    return uri, user, password, service_host


def _validate_target(uri: str, service_host: str) -> None:
    parsed = urlparse(uri)
    allowed_hosts = {"localhost", "127.0.0.1", "::1", service_host}
    if parsed.scheme != "bolt" or parsed.hostname not in allowed_hosts:
        pytest.fail("persist-first test requires an explicitly disposable Bolt target")
    if parsed.username is not None or parsed.password is not None:
        pytest.fail("persist-first test URI must not embed credentials")


def _require_neo4j_526(driver: Driver) -> None:
    with driver.session() as session:
        row = session.run(
            "CALL dbms.components() YIELD versions RETURN versions[0] AS version"
        ).single(strict=True)
    version = row["version"]
    if not isinstance(version, str) or version.split(".")[:2] != ["5", "26"]:
        pytest.fail("persist-first test requires Neo4j 5.26")


@pytest.fixture
def neo4j_driver() -> Iterator[Driver]:
    configuration = _configuration()
    if configuration is None:
        pytest.skip("disposable Neo4j control migration database is not configured")
    uri, user, password, service_host = configuration
    _validate_target(uri, service_host)
    driver = GraphDatabase.driver(uri, auth=(user, password))
    cleanup_owned = False
    try:
        driver.verify_connectivity()
        _require_neo4j_526(driver)
        with driver.session() as session:
            count = session.run(
                "MATCH (node) WHERE NOT node:PersonListSummaryCounter RETURN count(node) AS count"
            ).single(strict=True)["count"]
        if count != 0:
            pytest.fail("persist-first test database must be empty")
        cleanup_owned = True
        yield driver
    finally:
        try:
            if cleanup_owned:
                with driver.session() as session:
                    session.run(
                        "MATCH (node) WHERE NOT node:PersonListSummaryCounter DETACH DELETE node"
                    ).consume()
        finally:
            driver.close()


class _Client(Neo4jClient):
    def __init__(self, driver: Driver) -> None:
        self._driver = driver
        self._bounded_timeout_seconds = None


def _fundbox_envelope(
    *,
    record_id: str = "fb-user-1",
    phone: str = "+6591234567",
    nric: str | None = None,
    name: str = "Ada Lovelace",
    record_hash: str = "hash-fb-1",
) -> SourceRecordEnvelope:
    identifiers: list[dict[str, object]] = [{"type": "phone", "value": phone, "is_verified": True}]
    if nric is not None:
        identifiers.append({"type": "nric", "value": nric, "is_verified": True})
    return SourceRecordEnvelope(
        source_system="fundbox",
        source_record_id=record_id,
        record_type=RecordType.IDENTITY,
        observed_at="2026-05-08T09:47:25+00:00",
        record_hash=record_hash,
        identifiers=identifiers,
        attributes={"full_name": name},
        raw_payload={"user_id": record_id},
    )


def _bankruptcy_envelope(
    *,
    record_id: str = "bankrupt-1",
    nric: str = "S9999001A",
    name: str = "Ada Lovelace",
    record_hash: str = "hash-bankrupt-1",
) -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="sgbankruptcy",
        source_record_id=record_id,
        record_type=RecordType.BANKRUPTCY,
        observed_at="2026-05-08T09:47:25+00:00",
        record_hash=record_hash,
        identifiers=[{"type": "nric", "value": nric, "is_verified": True}],
        attributes={"full_name": name},
        raw_payload={"case_id": record_id},
    )


def _assert_s1_graph(driver: Driver, person_id: str) -> None:
    with driver.session() as s:
        p_row = s.run("MATCH (p:Person) RETURN count(p) AS count, collect(p.status) AS statuses")
        rec = p_row.single(strict=True)
        assert rec["count"] == 1
        assert rec["statuses"] == ["active"]

        sr_row = s.run(
            """
            MATCH (sr:SourceRecord {source_record_id: 'fb-user-1'})
            RETURN sr.lifecycle_status AS lifecycle_status,
                   sr.is_latest AS is_latest,
                   sr.link_status AS link_status,
                   sr.source_record_version AS version,
                   sr.attribution_status AS attribution_status,
                   sr.reeval_pending AS reeval_pending,
                   sr.attribution_eval_at AS attribution_eval_at
            """
        ).single(strict=True)
        assert sr_row["lifecycle_status"] == "active"
        assert sr_row["is_latest"] is True
        assert sr_row["link_status"] == "linked"
        assert sr_row["version"] == "1"
        assert sr_row["attribution_status"] is None
        assert sr_row["reeval_pending"] is None
        assert sr_row["attribution_eval_at"] is None

        linked_count = s.run(
            "MATCH (sr:SourceRecord {source_record_id: 'fb-user-1'})-[:LINKED_TO]->"
            "(p:Person {person_id: $pid}) RETURN count(p) AS count",
            pid=person_id,
        ).single(strict=True)["count"]
        assert linked_count == 1

        ident_count = s.run(
            "MATCH (p:Person)-[rel:IDENTIFIED_BY]->(:Identifier) "
            "WHERE rel.is_active = true RETURN count(rel) AS count"
        ).single(strict=True)["count"]
        assert ident_count >= 1

        fact_count = s.run(
            "MATCH (p:Person)-[rel:HAS_FACT]->(:SourceRecord) "
            "WHERE rel.is_active = true RETURN count(rel) AS count"
        ).single(strict=True)["count"]
        assert fact_count >= 1

        decision_count = s.run(
            "MATCH (:MatchDecision)-[:ABOUT_LEFT]->"
            "(sr:SourceRecord {source_record_id: 'fb-user-1'}) RETURN count(*) AS count"
        ).single(strict=True)["count"]
        assert decision_count == 1


def _assert_s4_graph(driver: Driver) -> None:
    with driver.session() as s:
        sr_parked = s.run(
            """
            MATCH (sr:SourceRecord {source_record_id: 'bankrupt-s4'})
            RETURN count(sr) AS count,
                   sr.lifecycle_status AS lifecycle_status,
                   sr.link_status AS link_status,
                   sr.is_latest AS is_latest,
                   sr.attribution_status AS attribution_status,
                   sr.reeval_pending AS reeval_pending
            """
        ).single(strict=True)
        assert sr_parked["count"] == 1
        assert sr_parked["lifecycle_status"] == "parked"
        assert sr_parked["link_status"] == "unlinked"
        assert sr_parked["is_latest"] is False
        assert sr_parked["attribution_status"] == "parked"
        assert sr_parked["reeval_pending"] is True

        linked = s.run(
            "MATCH (sr:SourceRecord {source_record_id: 'bankrupt-s4'})-[:LINKED_TO]->(:Person) "
            "RETURN count(*) AS c"
        ).single(strict=True)["c"]
        assert linked == 0

        match_decisions = s.run(
            "MATCH (:MatchDecision)-[:ABOUT_LEFT]->"
            "(sr:SourceRecord {source_record_id: 'bankrupt-s4'}) RETURN count(*) AS c"
        ).single(strict=True)["c"]
        assert match_decisions == 0

        review_cases = s.run(
            "MATCH (rc:ReviewCase)-[:FOR_DECISION]->(:MatchDecision)-[:ABOUT_LEFT]->"
            "(sr:SourceRecord {source_record_id: 'bankrupt-s4'}) RETURN count(*) AS c"
        ).single(strict=True)["c"]
        assert review_cases == 0


def _run_links(driver: Driver, source_record_id: str) -> dict[str, int]:
    """PART_OF_RUN edge count per IngestRun for one source record."""
    with driver.session() as s:
        rows = s.run(
            "MATCH (:SourceRecord {source_record_id: $id})-[r:PART_OF_RUN]->(ir:IngestRun) "
            "RETURN ir.ingest_run_id AS run, count(r) AS links",
            id=source_record_id,
        ).data()
    return {str(row["run"]): int(row["links"]) for row in rows}


def _assert_s5_graph(driver: Driver, person_id: str) -> None:
    with driver.session() as s:
        sr_promoted = s.run(
            """
            MATCH (sr:SourceRecord {source_record_id: 'bankrupt-s4'})
            RETURN count(sr) AS count,
                   sr.lifecycle_status AS lifecycle_status,
                   sr.is_latest AS is_latest,
                   sr.link_status AS link_status,
                   sr.source_record_version AS version,
                   sr.attribution_status AS attribution_status,
                   sr.reeval_pending AS reeval_pending
            """
        ).single(strict=True)
        assert sr_promoted["count"] == 1
        assert sr_promoted["lifecycle_status"] == "active"
        assert sr_promoted["is_latest"] is True
        assert sr_promoted["link_status"] == "linked"
        assert sr_promoted["version"] == "1"
        assert sr_promoted["attribution_status"] is None
        assert sr_promoted["reeval_pending"] is None

        linked_to_babbage = s.run(
            "MATCH (sr:SourceRecord {source_record_id: 'bankrupt-s4'})-[:LINKED_TO]->"
            "(p:Person {person_id: $pid}) RETURN count(p) AS c",
            pid=person_id,
        ).single(strict=True)["c"]
        assert linked_to_babbage == 1


def test_persist_first_baseline_scenarios_s1_to_s5(neo4j_driver: Driver) -> None:
    client = _Client(neo4j_driver)
    apply_schema(client)
    apply_deferred_identifier_scope_constraints(client)
    bootstrap_entities_and_sources(client)

    fb_run = _create_ingest_run(client, "fundbox", "batch")
    bk_run = _create_ingest_run(client, "sgbankruptcy", "batch")
    bk_run_later = _create_ingest_run(client, "sgbankruptcy", "batch")
    pipeline = IngestPipeline(client)

    # --- S1: new person ---
    env1 = _fundbox_envelope(record_id="fb-user-1", phone="+6591234567", record_hash="hash-1")
    res1 = pipeline.ingest(env1, ingest_run_id=fb_run)
    assert res1.parked is False and res1.is_new_person is True and res1.person_id is not None
    _assert_s1_graph(neo4j_driver, res1.person_id)
    # Staged and settled in the same run: still exactly one PART_OF_RUN edge (idempotent link).
    assert _run_links(neo4j_driver, "fb-user-1") == {fb_run: 1}

    # --- S2: rerun same hash ---
    with neo4j_driver.session() as s:
        nb = s.run("MATCH (n) RETURN count(n) AS c").single(strict=True)["c"]
        rb = s.run("MATCH ()-[r]->() RETURN count(r) AS c").single(strict=True)["c"]
    res2 = pipeline.ingest(env1, ingest_run_id=fb_run)
    assert res2.skipped_duplicate is True and res2.source_record_pk == res1.source_record_pk
    with neo4j_driver.session() as s:
        assert s.run("MATCH (n) RETURN count(n) AS c").single(strict=True)["c"] == nb
        assert s.run("MATCH ()-[r]->() RETURN count(r) AS c").single(strict=True)["c"] == rb

    # --- S3: second record sharing a verified phone ---
    env3 = _fundbox_envelope(
        record_id="fb-user-2", phone="+6591234567", record_hash="hash-3", name="Ada Lovelace 2"
    )
    res3 = pipeline.ingest(env3, ingest_run_id=fb_run)
    assert res3.parked is False and res3.is_new_person is False and res3.person_id == res1.person_id
    with neo4j_driver.session() as s:
        assert s.run("MATCH (p:Person) RETURN count(p) AS c").single(strict=True)["c"] == 1
        assert (
            s.run("MATCH ()-[:LINKED_TO]->(:Person) RETURN count(*) AS c").single(strict=True)["c"]
            == 2
        )

    # --- S4: match-only (sgbankruptcy) with no matching Person ---
    env4 = _bankruptcy_envelope(
        record_id="bankrupt-s4", nric="S9999001A", record_hash="hash-bankrupt-s4"
    )
    res4 = pipeline.ingest(env4, ingest_run_id=bk_run)
    assert res4.parked is True and res4.skipped_duplicate is False
    _assert_s4_graph(neo4j_driver)

    res4_rerun = pipeline.ingest(env4, ingest_run_id=bk_run)
    assert res4_rerun.skipped_duplicate is True and res4_rerun.parked is True

    # --- S5: fundbox record carrying NRIC, then re-ingest bankruptcy record ---
    env5_fb = _fundbox_envelope(
        record_id="fb-user-nric",
        phone="+6599990001",
        nric="S9999001A",
        name="Charles Babbage",
        record_hash="hash-fb-nric",
    )
    res5_fb = pipeline.ingest(env5_fb, ingest_run_id=fb_run)
    assert res5_fb.is_new_person is True and res5_fb.person_id is not None

    assert _run_links(neo4j_driver, "bankrupt-s4") == {bk_run: 1}
    res5_bk = pipeline.ingest(env4, ingest_run_id=bk_run_later)
    assert res5_bk.parked is False and res5_bk.person_id == res5_fb.person_id
    _assert_s5_graph(neo4j_driver, res5_fb.person_id)
    # The reused parked row joins the run that activated it, without losing the run that staged it.
    assert _run_links(neo4j_driver, "bankrupt-s4") == {bk_run: 1, bk_run_later: 1}


def test_persist_first_rollback_s6(neo4j_driver: Driver, monkeypatch: MonkeyPatch) -> None:
    client = _Client(neo4j_driver)
    apply_schema(client)
    apply_deferred_identifier_scope_constraints(client)
    bootstrap_entities_and_sources(client)

    fb_run = _create_ingest_run(client, "fundbox", "batch")
    bk_run = _create_ingest_run(client, "sgbankruptcy", "batch")
    pipeline = IngestPipeline(client)

    # 1. Rollback on link path after staging
    monkeypatch.setattr(
        "src.pipeline.persist_match_decision",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("simulated link failure")),
    )
    env_link = _fundbox_envelope(
        record_id="fb-rollback-1",
        phone="+6591112222",
        record_hash="hash-rb-1",
    )
    with pytest.raises(RuntimeError, match="simulated link failure"):
        pipeline.ingest(env_link, ingest_run_id=fb_run)

    with neo4j_driver.session() as s:
        assert (
            s.run(
                "MATCH (sr:SourceRecord {source_record_id: 'fb-rollback-1'}) RETURN count(sr) AS c"
            ).single(strict=True)["c"]
            == 0
        )
        assert (
            s.run(
                "MATCH (sr:SourceRecord {source_record_id: 'fb-rollback-1'})-[:PART_OF_RUN]->"
                "(:IngestRun) RETURN count(*) AS c"
            ).single(strict=True)["c"]
            == 0
        )

    # 2. Rollback on parked path after staging
    monkeypatch.undo()
    monkeypatch.setattr(
        pipeline,
        "_has_usable_match",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("simulated park failure")),
    )
    env_park = _bankruptcy_envelope(
        record_id="bankrupt-rollback-1",
        nric="S8888001Z",
        record_hash="hash-rb-park",
    )
    with pytest.raises(RuntimeError, match="simulated park failure"):
        pipeline.ingest(env_park, ingest_run_id=bk_run)

    with neo4j_driver.session() as s:
        assert (
            s.run(
                "MATCH (sr:SourceRecord {source_record_id: 'bankrupt-rollback-1'}) "
                "RETURN count(sr) AS c"
            ).single(strict=True)["c"]
            == 0
        )


def _run_concurrent_pair(
    driver: Driver,
    run_id: str,
    envelope: SourceRecordEnvelope,
) -> tuple[list[IngestResult | None], list[Exception | None]]:
    barrier = threading.Barrier(2)
    results: list[IngestResult | None] = [None, None]
    errors: list[Exception | None] = [None, None]

    def _worker(index: int) -> None:
        try:
            worker_client = _Client(driver)
            worker_pipeline = IngestPipeline(worker_client)
            barrier.wait(timeout=10.0)
            results[index] = worker_pipeline.ingest(envelope, ingest_run_id=run_id)
        except Exception as exc:  # noqa: BLE001
            errors[index] = exc

    t1 = threading.Thread(target=_worker, args=(0,))
    t2 = threading.Thread(target=_worker, args=(1,))
    t1.start()
    t2.start()
    t1.join(timeout=15.0)
    t2.join(timeout=15.0)
    return results, errors


def test_persist_first_concurrency_s7(neo4j_driver: Driver) -> None:
    client = _Client(neo4j_driver)
    apply_schema(client)
    apply_deferred_identifier_scope_constraints(client)
    bootstrap_entities_and_sources(client)

    fb_run = _create_ingest_run(client, "fundbox", "batch")
    bk_run = _create_ingest_run(client, "sgbankruptcy", "batch")

    # 1. Concurrent ingest of the SAME unlinkable envelope
    unlinkable_env = _bankruptcy_envelope(
        record_id="bankrupt-conc-1",
        nric="S7777001X",
        record_hash="hash-conc-unlinkable",
    )
    un_results, un_errors = _run_concurrent_pair(neo4j_driver, bk_run, unlinkable_env)
    assert un_errors == [None, None], f"Thread errors: {un_errors}"
    assert un_results[0] is not None and un_results[1] is not None
    assert un_results[0].parked is True and un_results[1].parked is True
    dup_flags = [un_results[0].skipped_duplicate, un_results[1].skipped_duplicate]
    assert sorted(dup_flags) == [False, True]

    with neo4j_driver.session() as s:
        assert (
            s.run(
                "MATCH (sr:SourceRecord {source_record_id: 'bankrupt-conc-1'}) RETURN count(sr) AS c"
            ).single(strict=True)["c"]
            == 1
        )

    # 2. Concurrent ingest of the SAME linkable envelope
    linkable_env = _fundbox_envelope(
        record_id="fb-conc-1",
        phone="+6593334444",
        name="Grace Hopper",
        record_hash="hash-conc-linkable",
    )
    lk_results, lk_errors = _run_concurrent_pair(neo4j_driver, fb_run, linkable_env)
    assert lk_errors == [None, None], f"Thread errors: {lk_errors}"

    with neo4j_driver.session() as s:
        assert (
            s.run(
                "MATCH (sr:SourceRecord {source_record_id: 'fb-conc-1'}) RETURN count(sr) AS c"
            ).single(strict=True)["c"]
            == 1
        )
        assert (
            s.run(
                "MATCH (p:Person)-[:IDENTIFIED_BY]->(:Identifier {normalized_value: '+6593334444'}) "
                "RETURN count(p) AS c"
            ).single(strict=True)["c"]
            == 1
        )


def test_persist_first_version_guard_revert_s8(neo4j_driver: Driver) -> None:
    client = _Client(neo4j_driver)
    apply_schema(client)
    apply_deferred_identifier_scope_constraints(client)
    bootstrap_entities_and_sources(client)

    fb_run = _create_ingest_run(client, "fundbox", "batch")
    bk_run = _create_ingest_run(client, "sgbankruptcy", "batch")
    pipeline = IngestPipeline(client)

    # 1. Ingest sgbankruptcy unlinkable -> parked v1 (hash A)
    env_v1 = _bankruptcy_envelope(
        record_id="bankrupt-s8",
        nric="S9999008A",
        name="Original Name",
        record_hash="hash-s8-orig",
    )
    res_v1 = pipeline.ingest(env_v1, ingest_run_id=bk_run)
    assert res_v1.parked is True

    # 2. Create person via fundbox NRIC record
    env_fb = _fundbox_envelope(
        record_id="fb-user-s8",
        phone="+6599990008",
        nric="S9999008A",
        name="Target Person",
        record_hash="hash-fb-s8",
    )
    res_fb = pipeline.ingest(env_fb, ingest_run_id=fb_run)
    assert res_fb.is_new_person is True
    person_id = res_fb.person_id

    # 3. Ingest a CHANGED bankruptcy record (hash B) -> linked active v2
    env_v2 = _bankruptcy_envelope(
        record_id="bankrupt-s8",
        nric="S9999008A",
        name="Changed Name",
        record_hash="hash-s8-changed",
    )
    res_v2 = pipeline.ingest(env_v2, ingest_run_id=bk_run)
    assert res_v2.parked is False
    assert res_v2.person_id == person_id

    # 4. Ingest the ORIGINAL bankruptcy record again (revert to hash A)
    res_v3 = pipeline.ingest(env_v1, ingest_run_id=bk_run)
    assert res_v3.parked is False
    assert res_v3.person_id == person_id

    # 5. Assert: active row has version > 2; v1 row still parked; 1 active row
    with neo4j_driver.session() as s:
        active_row = s.run(
            """
            MATCH (sr:SourceRecord {source_record_id: 'bankrupt-s8', lifecycle_status: 'active'})
            RETURN sr.source_record_version AS version,
                   sr.record_hash AS record_hash,
                   sr.is_latest AS is_latest
            """
        ).single(strict=True)
        assert int(str(active_row["version"])) > 2
        assert active_row["record_hash"] == "hash-s8-orig"
        assert active_row["is_latest"] is True

        v1_row = s.run(
            """
            MATCH (sr:SourceRecord {source_record_id: 'bankrupt-s8', source_record_version: '1'})
            RETURN sr.lifecycle_status AS lifecycle_status,
                   sr.record_hash AS record_hash
            """
        ).single(strict=True)
        assert v1_row["lifecycle_status"] == "parked"
        assert v1_row["record_hash"] == "hash-s8-orig"

        total_active = s.run(
            "MATCH (sr:SourceRecord {source_record_id: 'bankrupt-s8', lifecycle_status: 'active'}) "
            "RETURN count(sr) AS c"
        ).single(strict=True)["c"]
        assert total_active == 1
