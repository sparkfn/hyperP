"""Neo4j replay coverage for preserving historical NRIC assertion identities."""

from __future__ import annotations

import secrets

import pytest
from _nric_rehash_apply_builders import _settings
from _nric_rehash_neo4j_fingerprint import _graph_fingerprint, _safe_equal
from _nric_rehash_neo4j_fixture import _apply, _Client, _counter, neo4j_driver
from _nric_rehash_neo4j_seed_replay import (
    _assertion_snapshot,
    _identifier_ids,
    _replay_dataset,
    _ReplayDataset,
    _seed_replay_record,
    _seed_s1_world,
    _seed_s2_collision_world,
)
from neo4j import Driver
from src import identity_assertion_nric
from src.identity_assertion_nric import LegacyNricReplayError
from src.identity_assertions import audit_assertions, backfill_assertions
from src.normalizers.nric import nric_digest

__all__ = ("neo4j_driver",)


def _use_replay_key(monkeypatch: pytest.MonkeyPatch, dataset: _ReplayDataset) -> None:
    monkeypatch.setattr(
        identity_assertion_nric,
        "get_settings",
        lambda: _settings(dataset.key, dataset.key_version),
    )


def _assert_no_plaintext_identifiers(driver: Driver) -> None:
    assert (
        _counter(
            driver,
            "MATCH (identifier:Identifier {identifier_type: 'nric'}) "
            "WHERE NOT coalesce(identifier.normalized_value =~ '^[0-9a-f]{64}$', false) "
            "RETURN count(identifier) AS count",
        )
        == 0
    )


def _assert_replay_is_idempotent(driver: Driver, dataset: _ReplayDataset) -> None:
    assertions_before = _assertion_snapshot(driver)
    identifiers_before = _identifier_ids(driver)
    assert backfill_assertions(_Client(driver), batch_size=2) == len(dataset.source_record_pks)
    _safe_equal(_assertion_snapshot(driver), assertions_before, "second replay edges")
    _safe_equal(_identifier_ids(driver), identifiers_before, "second replay Identifier ids")
    assert audit_assertions(_Client(driver), batch_size=2) == []


def test_s1_distinct_legacy_values_replay_to_existing_keyed_identifiers(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = _replay_dataset(secrets.token_hex(32), canonical_count=2)
    _seed_s1_world(neo4j_driver, dataset)
    identifiers_before = _identifier_ids(neo4j_driver)
    _use_replay_key(monkeypatch, dataset)

    report, exit_code = _apply(neo4j_driver, dataset, batch_size=2)

    assert exit_code == 0
    assert report["converted_in_place"] == 2
    _safe_equal(_identifier_ids(neo4j_driver), identifiers_before, "S1 Identifier ids")
    _assert_no_plaintext_identifiers(neo4j_driver)
    assertions_before = _assertion_snapshot(neo4j_driver)
    assert len(assertions_before) == 2
    assert backfill_assertions(_Client(neo4j_driver), batch_size=2) == 2
    _safe_equal(_assertion_snapshot(neo4j_driver), assertions_before, "S1 replay edges")
    _safe_equal(_identifier_ids(neo4j_driver), identifiers_before, "S1 replay Identifier ids")
    _assert_no_plaintext_identifiers(neo4j_driver)
    assert audit_assertions(_Client(neo4j_driver), batch_size=2) == []
    _assert_replay_is_idempotent(neo4j_driver, dataset)


def test_s2_collision_survivor_keeps_three_historical_assertions_after_replay(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = _replay_dataset(secrets.token_hex(32), canonical_count=3)
    _seed_s2_collision_world(neo4j_driver, dataset)
    _use_replay_key(monkeypatch, dataset)

    report, exit_code = _apply(neo4j_driver, dataset, batch_size=3)

    assert exit_code == 0
    assert report["converted_consolidated"] == 2
    _safe_equal(
        _identifier_ids(neo4j_driver),
        (dataset.identifier_ids[2],),
        "S2 digest survivor identifier id",
    )
    assertions_before = _assertion_snapshot(neo4j_driver)
    assert len(assertions_before) == 3
    assert len({row[1] for row in assertions_before}) == 3
    _safe_equal(
        tuple(row[0] for row in assertions_before),
        tuple(sorted(dataset.source_record_pks)),
        "S2 source-record assertion set",
    )
    expected_digest = nric_digest(dataset.canonicals[0], dataset.key)
    _safe_equal(
        tuple(row[2] for row in assertions_before),
        (expected_digest, expected_digest, expected_digest),
        "S2 keyed assertion targets",
    )
    assert backfill_assertions(_Client(neo4j_driver), batch_size=3) == 3
    _safe_equal(_assertion_snapshot(neo4j_driver), assertions_before, "S2 replay edges")
    _safe_equal(
        _identifier_ids(neo4j_driver),
        (dataset.identifier_ids[2],),
        "S2 replay Identifier ids",
    )
    _assert_no_plaintext_identifiers(neo4j_driver)
    assert audit_assertions(_Client(neo4j_driver), batch_size=3) == []
    _assert_replay_is_idempotent(neo4j_driver, dataset)


def test_s3_empty_key_refuses_unconverted_replay_without_graph_changes(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = _replay_dataset(secrets.token_hex(32), canonical_count=1)
    _seed_replay_record(neo4j_driver, dataset, 0, dataset.canonicals[0])
    before = _graph_fingerprint(neo4j_driver)
    monkeypatch.setattr(
        identity_assertion_nric,
        "get_settings",
        lambda: _settings("", dataset.key_version),
    )

    with pytest.raises(LegacyNricReplayError):
        backfill_assertions(_Client(neo4j_driver), batch_size=1)

    _safe_equal(_graph_fingerprint(neo4j_driver), before, "S3 refused replay fingerprint")
