"""Neo4j coverage for anchor decisions, handoff output, and resume fencing."""

from __future__ import annotations

import json
import secrets
from dataclasses import replace
from pathlib import Path

import pytest
from _nric_rehash_neo4j_fingerprint import (
    _active_attribution_snapshot,
    _graph_fingerprint,
    _identity_revision_snapshot,
    _normalize_migration_clock,
    _safe_equal,
)
from _nric_rehash_neo4j_fixture import (
    _apply,
    _clear_owned_graph,
    _Client,
    _counter,
    _counter_record,
    neo4j_driver,
)
from _nric_rehash_neo4j_seed import _seed_revision_counter
from _nric_rehash_neo4j_seed_anchor import (
    _anchor_dataset,
    _AnchorDataset,
    _AnchorDecisionWorld,
    _invalid_raw,
    _link_id,
    _masked_raw,
    _payload,
    _promoting_source_key,
    _seed_anchor_decision_world,
    _seed_anchor_identifier,
    _seed_anchor_person,
    _seed_anchor_record,
    _seed_single_case,
    _valid_raw,
)
from neo4j import Driver, ManagedTransaction
from src import nric_rehash_anchor
from src.models import NormalizedIdentifier, RecordType
from src.normalizers.nric import encrypt_nric, nric_digest
from src.nric_rehash_plan import build_verify_report

__all__ = ("neo4j_driver",)


def _decode_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError("handoff object was malformed")
    decoded: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise AssertionError("handoff field name was malformed")
        decoded[key] = item
    return decoded


def _seed_resume_world(driver: Driver, dataset: _AnchorDataset) -> None:
    source_key = _promoting_source_key()
    for index in range(4):
        raw_value = _invalid_raw(dataset, index) if index == 2 else _valid_raw(dataset, index)
        _seed_single_case(
            driver,
            dataset,
            person_index=index,
            identifier_index=index,
            record_index=index,
            raw_value=raw_value,
            source_key=source_key,
            record_type=RecordType.IDENTITY.value,
            name=f"Generated Person {index}",
            dob=f"1980-0{index + 1}-15",
        )


def _seed_unattributed_assertion(
    driver: Driver,
    dataset: _AnchorDataset,
    source_key: str,
) -> None:
    with driver.session() as session:
        session.run(
            "MERGE (system:SourceSystem {source_key: $source_key}) "
            "SET system.field_trust = '{}' "
            "WITH system "
            "MATCH (identifier:Identifier {identifier_id: $identifier_id}) "
            "CREATE (record:SourceRecord {source_record_pk: $source_record_pk, "
            "source_record_id: $source_record_pk, source_instance_id: $source_record_pk, "
            "record_type: $record_type, normalized_payload: $payload})-[:FROM_SOURCE]->(system) "
            "CREATE (record)-[:ASSERTS {assertion_id: $assertion_id}]->(identifier)",
            source_key=source_key,
            identifier_id=dataset.identifier_ids[11],
            source_record_pk=dataset.source_record_pks[14],
            record_type=RecordType.IDENTITY.value,
            payload=_payload("Generated Person 10", "1980-11-15"),
            assertion_id=secrets.token_hex(16),
        ).consume()


def _sensitive_values(dataset: _AnchorDataset) -> tuple[str, ...]:
    return (
        dataset.key,
        *dataset.canonicals,
        *(nric_digest(value, dataset.key) for value in dataset.canonicals),
        _masked_raw(dataset, 10),
        encrypt_nric(dataset.canonicals[0], dataset.key),
        *(f"Generated Person {index}" for index in range(11)),
        *(f"1980-{index + 1:02d}-15" for index in range(12)),
        "1991-04-05",
    )


def _read_handoffs(
    report: dict[str, object],
    directory: Path,
    sensitive_values: tuple[str, ...],
) -> tuple[tuple[tuple[str, str], ...], dict[str, object]]:
    summary = _decode_object(report["handoff"])
    basename = summary.get("file")
    if not isinstance(basename, str) or Path(basename).name != basename:
        raise AssertionError("handoff basename was missing or not local")
    contents = (directory / basename).read_bytes()
    for sensitive in sensitive_values:
        if sensitive.encode("utf-8") in contents:
            pytest.fail("sensitive generated value appeared in handoff output")
    rows = tuple(_decode_object(json.loads(line)) for line in contents.decode("utf-8").splitlines())
    pairs: list[tuple[str, str]] = []
    for row in rows:
        if set(row) != {"person_id", "reason", "identifier_ids", "source_record_pks"}:
            raise AssertionError("handoff record contained an unapproved field")
        person_id, reason = row.get("person_id"), row.get("reason")
        if not isinstance(person_id, str) or not isinstance(reason, str):
            raise AssertionError("handoff identity fields were malformed")
        pairs.append((person_id, reason))
    return tuple(pairs), summary


def _assert_decision_report(
    report: dict[str, object],
    exit_code: int,
    world: _AnchorDecisionWorld,
    directory: Path,
) -> None:
    expected_handoffs = world.handoffs
    sensitive_values = _sensitive_values(world.dataset)
    assert exit_code == 0
    assert report["status"] == "ok"
    assert report["phase"] == "anchor_complete"
    assert report["anchored"] == 1
    assert report["already_anchored"] == 2
    assert report["anchorable_remaining"] == 0
    converted_in_place = report["converted_in_place"]
    converted_consolidated = report["converted_consolidated"]
    anchored = report["anchored"]
    if (
        not isinstance(converted_in_place, int)
        or not isinstance(converted_consolidated, int)
        or not isinstance(anchored, int)
    ):
        raise AssertionError("apply change counters were malformed")
    assert report["changes"] == converted_in_place + converted_consolidated + anchored
    pairs, summary = _read_handoffs(report, directory, sensitive_values)
    assert pairs == expected_handoffs
    assert summary["total"] == len(expected_handoffs)


def test_anchor_pass_applies_only_approved_existing_person_anchors(
    neo4j_driver: Driver,
    tmp_path: Path,
) -> None:
    world = _seed_anchor_decision_world(neo4j_driver, secrets.token_hex(32))
    _seed_unattributed_assertion(neo4j_driver, world.dataset, _promoting_source_key())
    revision = secrets.randbelow(100_000) + 1
    _seed_revision_counter(neo4j_driver, revision)
    attribution_before = _active_attribution_snapshot(neo4j_driver)
    revisions_before = _identity_revision_snapshot(neo4j_driver)
    report, status = _apply(neo4j_driver, world.dataset, handoff_dir=tmp_path)
    _assert_decision_report(report, status, world, tmp_path)
    assert report["count_equation_ok"]
    assert report["evidence_equation_ok"]
    assert report["revision_unchanged"]
    assert report["attribution_unchanged"]
    assert _active_attribution_snapshot(neo4j_driver) == attribution_before
    _safe_equal(_identity_revision_snapshot(neo4j_driver), revisions_before, "revision snapshot")
    _assert_anchor_properties(neo4j_driver, world, revision)
    before_retry = _graph_fingerprint(neo4j_driver)
    rerun, rerun_status = _apply(neo4j_driver, world.dataset, handoff_dir=tmp_path)
    assert rerun_status == 0
    assert rerun["changes"] == 0
    _safe_equal(_graph_fingerprint(neo4j_driver), before_retry, "zero-write anchor rerun")


def _assert_anchor_properties(
    driver: Driver,
    world: _AnchorDecisionWorld,
    revision: int,
) -> None:
    _assert_person_keys(driver, world)
    _assert_verify_readiness(driver, world.dataset)
    _assert_revision_counter(driver, revision)


def _assert_person_keys(driver: Driver, world: _AnchorDecisionWorld) -> None:
    dataset = world.dataset
    assert _counter(driver, "MATCH (person:Person) RETURN count(person) AS count") == 10
    assert (
        _counter(
            driver,
            "MATCH (person:Person) WHERE person.nric_key IS NOT NULL RETURN count(person) AS count",
        )
        == 3
    )
    for person_id, nric_key in (
        (world.anchor_person_id, nric_digest(dataset.canonicals[0], dataset.key)),
        (world.conflicting_person_id, world.conflicting_key),
    ):
        assert (
            _counter(
                driver,
                "MATCH (person:Person {person_id: $person_id}) "
                "WHERE person.nric_key = $nric_key RETURN count(person) AS count",
                person_id=person_id,
                nric_key=nric_key,
            )
            == 1
        )
    assert (
        _counter(
            driver,
            "MATCH (person:Person {person_id: $person_id})-[:ANCHORED_BY]->"
            "(:Identifier {identifier_type: 'nric'}) RETURN count(*) AS count",
            person_id=world.anchor_person_id,
        )
        == 1
    )


def _assert_verify_readiness(driver: Driver, dataset: _AnchorDataset) -> None:
    verify = build_verify_report(_Client(driver), dataset.key_version)
    assert verify["ok"] is True
    assert verify["duplicate_key_groups"] == 0
    assert verify["duplicate_scoped_targets"] == 0
    assert verify["anchored_persons"] == 3
    assert verify["identifiers_without_assertion"] == 0
    assert verify["identifiers_without_projection"] == 0


def _assert_revision_counter(driver: Driver, revision: int) -> None:
    _safe_equal(
        _counter(
            driver,
            "MATCH (counter:IdentityLinkRevisionCounter {stream_key: $key}) "
            "RETURN counter.current_revision AS count",
            key="identity_link_revision_stream_v1",
        ),
        revision,
        "revision counter",
    )


def test_masked_sibling_is_a_multi_nric_handoff_not_an_anchor(
    neo4j_driver: Driver,
    tmp_path: Path,
) -> None:
    key = secrets.token_hex(32)
    dataset = _anchor_dataset(key, canonical_count=2)
    person_id = dataset.person_ids[0]
    source_key = _promoting_source_key()
    raw_values = (_valid_raw(dataset, 0), _masked_raw(dataset, 1))
    _seed_anchor_person(neo4j_driver, person_id)
    for index, raw_value in enumerate(raw_values):
        identifier_id = dataset.identifier_ids[index]
        _seed_anchor_identifier(neo4j_driver, identifier_id, raw_value)
        _seed_anchor_record(
            neo4j_driver,
            person_id=person_id,
            identifier_id=identifier_id,
            source_record_pk=dataset.source_record_pks[index],
            source_key=source_key,
            record_type=RecordType.IDENTITY.value,
            normalized_payload=_payload("Generated Masked Person", "1980-01-15"),
            link_id=_link_id(person_id, identifier_id),
        )
    report, status = _apply(neo4j_driver, dataset, handoff_dir=tmp_path)
    assert status == 1
    assert report["anchored"] == 0
    pairs, summary = _read_handoffs(
        report,
        tmp_path,
        (
            key,
            *dataset.canonicals,
            *(nric_digest(value, key) for value in dataset.canonicals),
            _masked_raw(dataset, 1),
            encrypt_nric(dataset.canonicals[0], key),
            "Generated Masked Person",
            "1980-01-15",
        ),
    )
    assert pairs == ((person_id, "multi_nric"),)
    assert summary["total"] == 1
    assert (
        _counter(
            neo4j_driver,
            "MATCH (person:Person {person_id: $person_id}) "
            "WHERE person.nric_key IS NOT NULL RETURN count(person) AS count",
            person_id=person_id,
        )
        == 0
    )


def test_anchor_page_failure_rolls_back_and_resumes_to_same_fingerprint(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = secrets.token_hex(32)
    generated = _anchor_dataset(key, canonical_count=4)
    dataset = replace(generated, person_ids=tuple(f"generated-person-{i}" for i in range(4)))
    _seed_resume_world(neo4j_driver, dataset)
    _seed_revision_counter(neo4j_driver, 83)
    _interrupt_anchor_page(neo4j_driver, dataset, monkeypatch)
    resumed, status = _apply(neo4j_driver, dataset, batch_size=2)
    assert status == 0
    assert resumed["phase"] == "anchor_complete"
    assert resumed["anchored"] == 3
    _normalize_migration_clock(neo4j_driver)
    resumed_fingerprint = _graph_fingerprint(neo4j_driver)
    _clear_owned_graph(neo4j_driver)
    _seed_resume_world(neo4j_driver, dataset)
    _seed_revision_counter(neo4j_driver, 83)
    uninterrupted, status = _apply(neo4j_driver, dataset, batch_size=2)
    assert status == 0
    assert uninterrupted["anchored"] == 3
    _normalize_migration_clock(neo4j_driver)
    _safe_equal(
        _graph_fingerprint(neo4j_driver),
        resumed_fingerprint,
        "resumed and uninterrupted anchor fingerprints",
    )


def _interrupt_anchor_page(
    driver: Driver,
    dataset: _AnchorDataset,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_encrypt = encrypt_nric
    ciphertext_cache: dict[str, str] = {}

    def cached_encrypt(canonical: str, encryption_key: str) -> str:
        if canonical not in ciphertext_cache:
            ciphertext_cache[canonical] = original_encrypt(canonical, encryption_key)
        return ciphertext_cache[canonical]

    original_anchor = nric_rehash_anchor.anchor_existing_person
    calls = 0

    def fail_on_second_anchor(
        tx: ManagedTransaction,
        person_id: str,
        identifier: NormalizedIdentifier,
    ) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic anchor interruption")
        return original_anchor(tx, person_id, identifier)

    monkeypatch.setattr("src.nric_rehash_passes.encrypt_nric", cached_encrypt)
    monkeypatch.setattr(nric_rehash_anchor, "anchor_existing_person", fail_on_second_anchor)
    with pytest.raises(RuntimeError):
        _apply(driver, dataset, batch_size=2)
    migration = _counter_record(driver)
    assert migration["phase"] == "anchor"
    assert migration.get("anchor_cursor") is None
    assert migration["anchored"] == 0
    assert (
        _counter(
            driver,
            "MATCH (person:Person) WHERE person.nric_key IS NOT NULL RETURN count(person) AS count",
        )
        == 0
    )
