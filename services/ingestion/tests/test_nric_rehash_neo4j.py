"""Neo4j 5.26 in-place conversion, resume, and lease integration tests."""

from __future__ import annotations

import secrets
import uuid

import pytest
from _nric_rehash_neo4j_fingerprint import (
    _graph_fingerprint,
    _identifier_properties,
    _normalize_migration_clock,
    _relationship_snapshot,
    _safe_equal,
)
from _nric_rehash_neo4j_fixture import (
    _apply,
    _clear_owned_graph,
    _counter,
    _counter_record,
    _database_name,
    _StealingClient,
    neo4j_driver,
)
from _nric_rehash_neo4j_seed import (
    _dataset,
    _seed_dataset,
    _seed_identifier_only,
    _seed_migration,
    _seed_revision_counter,
)
from cryptography.fernet import InvalidToken
from neo4j import Driver
from src.normalizers.nric import (
    _fernet_cipher,
    encrypt_nric,
    mask_nric,
    normalize_nric,
    nric_checksum_valid,
    nric_digest,
)
from src.nric_rehash_apply import run_nric_rehash_apply
from src.nric_rehash_plan import NricRehashError

__all__ = ("neo4j_driver",)


def test_in_place_conversion_preserves_evidence_and_second_apply_is_zero_diff(
    neo4j_driver: Driver,
) -> None:
    key = secrets.token_hex(32)
    dataset = _dataset(key, include_skipped=True)
    _seed_dataset(neo4j_driver, dataset, include_skipped=True)
    revision = secrets.randbelow(100_000) + 1
    _seed_revision_counter(neo4j_driver, revision)
    identifiers = _counter(neo4j_driver, "MATCH (i:Identifier) RETURN count(i) AS count")
    before_relations = {
        identifier_id: _relationship_snapshot(neo4j_driver, identifier_id)
        for identifier_id in dataset.identifier_ids
    }
    skipped_props = (
        _identifier_properties(neo4j_driver, dataset.identifier_ids[4]),
        _identifier_properties(neo4j_driver, dataset.identifier_ids[5]),
    )

    report, exit_code = _apply(neo4j_driver, dataset)
    assert exit_code == 1
    assert report["status"] == "blocked"
    assert report["changes"] == 4
    assert report["converted_in_place"] == 4
    assert report["already_digest"] == 5
    assert report["unprocessable"] == {"masked": 1, "empty": 0, "unknown_format": 0}

    for index in range(4):
        properties = _identifier_properties(neo4j_driver, dataset.identifier_ids[index])
        canonical, _quality = normalize_nric(dataset.raw_values[index])
        if canonical is None:
            pytest.fail("generated fixture did not normalize")
        digest = nric_digest(canonical, key)
        _safe_equal(properties.get("identifier_id"), dataset.identifier_ids[index], "Identifier id")
        _safe_equal(properties.get("identifier_scope"), "global", "Identifier scope")
        _safe_equal(properties.get("normalized_value"), digest, "normalized digest")
        _safe_equal(properties.get("hashed_value"), digest, "hashed digest")
        _safe_equal(properties.get("nric_masked"), mask_nric(canonical), "masked value")
        _safe_equal(
            properties.get("checksum_valid"), nric_checksum_valid(canonical), "checksum flag"
        )
        _safe_equal(properties.get("nric_key_version"), dataset.key_version, "key version")
        ciphertext = properties.get("nric_encrypted")
        if not isinstance(ciphertext, str):
            pytest.fail("converted Identifier ciphertext was absent")
        try:
            decrypted = _fernet_cipher(key).decrypt(ciphertext.encode("ascii"))
        except (InvalidToken, UnicodeEncodeError):
            pytest.fail("converted Identifier ciphertext could not be decrypted")
        _safe_equal(decrypted, canonical.encode("utf-8"), "decrypted canonical")
        _safe_equal(
            _relationship_snapshot(neo4j_driver, dataset.identifier_ids[index]),
            before_relations[dataset.identifier_ids[index]],
            "incident evidence",
        )

    _safe_equal(
        _identifier_properties(neo4j_driver, dataset.identifier_ids[4]),
        skipped_props[0],
        "masked Identifier unchanged",
    )
    _safe_equal(
        _identifier_properties(neo4j_driver, dataset.identifier_ids[5]),
        skipped_props[1],
        "existing digest Identifier unchanged",
    )
    assert _counter(neo4j_driver, "MATCH (i:Identifier) RETURN count(i) AS count") == identifiers
    assert _counter(neo4j_driver, "MATCH (e:IdentityLinkRevision) RETURN count(e) AS count") == 0
    assert (
        _counter(
            neo4j_driver,
            "MATCH (c:IdentityLinkRevisionCounter {stream_key: $key}) "
            "RETURN c.current_revision AS count",
            key="identity_link_revision_stream_v1",
        )
        == revision
    )

    before_retry = _graph_fingerprint(neo4j_driver)
    rerun, rerun_status = _apply(neo4j_driver, dataset)
    assert rerun_status == 1
    assert rerun["changes"] == 0
    _safe_equal(_graph_fingerprint(neo4j_driver), before_retry, "completed rerun fingerprint")


def test_crash_resume_matches_uninterrupted_conversion(
    neo4j_driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = secrets.token_hex(32)
    dataset = _dataset(key, include_skipped=False)
    _seed_dataset(neo4j_driver, dataset, include_skipped=False)
    _seed_revision_counter(neo4j_driver, 53)
    original_encrypt = encrypt_nric
    ciphertext_cache: dict[str, str] = {}
    calls = 0
    fail_on_call: list[int | None] = [4]

    def controlled_encrypt(canonical: str, secret: str) -> str:
        nonlocal calls
        calls += 1
        if fail_on_call[0] == calls:
            raise RuntimeError("synthetic encryption interruption")
        if canonical not in ciphertext_cache:
            ciphertext_cache[canonical] = original_encrypt(canonical, secret)
        return ciphertext_cache[canonical]

    monkeypatch.setattr("src.nric_rehash_passes.encrypt_nric", controlled_encrypt)
    with pytest.raises(RuntimeError):
        _apply(neo4j_driver, dataset, batch_size=2)

    migration = _counter_record(neo4j_driver)
    assert migration["phase"] == "convert"
    _safe_equal(migration.get("cursor"), dataset.identifier_ids[1], "last committed cursor")
    assert migration["batches"] == 1
    assert migration["converted_in_place"] == 2
    for index in range(2):
        properties = _identifier_properties(neo4j_driver, dataset.identifier_ids[index])
        if not isinstance(properties.get("normalized_value"), str):
            pytest.fail("committed Identifier digest was absent")
        if properties.get("normalized_value") == dataset.raw_values[index]:
            pytest.fail("failed batch changed a plaintext Identifier")
    for index in range(2, 4):
        properties = _identifier_properties(neo4j_driver, dataset.identifier_ids[index])
        _safe_equal(
            properties.get("normalized_value"), dataset.raw_values[index], "rolled-back row"
        )

    before_key_rejection = _graph_fingerprint(neo4j_driver)
    with pytest.raises(NricRehashError) as changed_key:
        _apply(neo4j_driver, _dataset(secrets.token_hex(32), include_skipped=False), batch_size=2)
    assert changed_key.value.code == "key_changed"
    _safe_equal(
        _graph_fingerprint(neo4j_driver), before_key_rejection, "key-change no-write fingerprint"
    )

    fail_on_call[0] = None
    resumed, resumed_status = _apply(neo4j_driver, dataset, batch_size=2)
    assert resumed_status == 0
    assert resumed["converted_in_place"] == 4
    _normalize_migration_clock(neo4j_driver)
    resumed_fingerprint = _graph_fingerprint(neo4j_driver)

    _clear_owned_graph(neo4j_driver)
    _seed_dataset(neo4j_driver, dataset, include_skipped=False)
    _seed_revision_counter(neo4j_driver, 53)
    calls = 0
    uninterrupted, uninterrupted_status = _apply(neo4j_driver, dataset, batch_size=2)
    assert uninterrupted_status == 0
    assert uninterrupted["converted_in_place"] == 4
    _normalize_migration_clock(neo4j_driver)
    _safe_equal(
        _graph_fingerprint(neo4j_driver),
        resumed_fingerprint,
        "resumed and uninterrupted graph fingerprints",
    )


def test_owner_overwritten_between_batches_rolls_back_the_lost_batch(
    neo4j_driver: Driver,
) -> None:
    dataset = _dataset(secrets.token_hex(32), include_skipped=False)
    _seed_dataset(neo4j_driver, dataset, include_skipped=False)
    _seed_revision_counter(neo4j_driver, 83)
    client = _StealingClient(neo4j_driver, uuid.uuid4().hex)

    with pytest.raises(NricRehashError) as error:
        run_nric_rehash_apply(
            client,
            batch_size=1,
            key=dataset.key,
            key_version=dataset.key_version,
            expected_database=_database_name(neo4j_driver),
            acknowledge_offline=True,
        )
    assert error.value.code == "lease_lost"
    migration = _counter_record(neo4j_driver)
    _safe_equal(migration.get("cursor"), dataset.identifier_ids[0], "committed cursor")
    assert migration["batches"] == 1
    assert migration["converted_in_place"] == 1
    _safe_equal(
        migration.get("lease_owner"), client.replacement_owner, "replacement owner retained"
    )
    first = _identifier_properties(neo4j_driver, dataset.identifier_ids[0])
    second = _identifier_properties(neo4j_driver, dataset.identifier_ids[1])
    assert first.get("nric_encrypted") is not None
    _safe_equal(second.get("normalized_value"), dataset.raw_values[1], "lost batch rollback")


def test_lease_collision_database_and_null_scope_fail_closed(
    neo4j_driver: Driver,
) -> None:
    key = secrets.token_hex(32)
    dataset = _dataset(key, include_skipped=False)
    _seed_revision_counter(neo4j_driver, 71)
    owner = uuid.uuid4().hex
    _seed_migration(
        neo4j_driver,
        dataset,
        owner=owner,
        lease_seconds=600,
        baseline_total=0,
        baseline_non_digest=0,
        baseline_digest_shaped=0,
        baseline_revision=71,
    )
    before_live_lease = _counter_record(neo4j_driver)
    with pytest.raises(NricRehashError) as live_lease:
        _apply(neo4j_driver, dataset)
    assert live_lease.value.code == "lease_unavailable"
    _safe_equal(_counter_record(neo4j_driver), before_live_lease, "live lease unchanged")

    with neo4j_driver.session() as session:
        session.run(
            "MATCH (migration:DataMigration {migration_key: $key}) "
            "SET migration.lease_until = datetime() - duration({seconds: 1})",
            key="nric_hmac_rehash_v1",
        ).consume()
    expired, expired_status = _apply(neo4j_driver, dataset)
    assert expired_status == 0
    assert expired["phase"] == "convert_complete"
    assert _counter_record(neo4j_driver).get("offline_acknowledged_at") is not None

    changed_key_fingerprint = _graph_fingerprint(neo4j_driver)
    with pytest.raises(NricRehashError) as changed_key:
        _apply(neo4j_driver, _dataset(secrets.token_hex(32), include_skipped=False))
    assert changed_key.value.code == "key_changed"
    _safe_equal(_graph_fingerprint(neo4j_driver), changed_key_fingerprint, "key change no-write")

    _clear_owned_graph(neo4j_driver)
    scope_data = _dataset(secrets.token_hex(32), include_skipped=False)
    _seed_identifier_only(
        neo4j_driver, scope_data.identifier_ids[0], scope_data.raw_values[0], scope=None
    )
    before_scope = _graph_fingerprint(neo4j_driver)
    wrong_database = f"{_database_name(neo4j_driver)}-wrong"
    with pytest.raises(NricRehashError) as mismatch:
        _apply(neo4j_driver, scope_data, expected_database=wrong_database)
    assert mismatch.value.code == "database_mismatch"
    with pytest.raises(NricRehashError) as null_scope:
        _apply(neo4j_driver, scope_data)
    assert null_scope.value.code == "null_scope_present"
    _safe_equal(_graph_fingerprint(neo4j_driver), before_scope, "precondition no-write fingerprint")
    assert (
        _counter(
            neo4j_driver,
            "MATCH (migration:DataMigration {migration_key: $key}) "
            "RETURN count(migration) AS count",
            key="nric_hmac_rehash_v1",
        )
        == 0
    )
